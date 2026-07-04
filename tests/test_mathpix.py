"""
Unit tests for src/mathpix.py — all HTTP calls mocked via respx.

Must never hit the real Mathpix API (see AGENTS.md Testing Conventions).

Covered here (issue #1 — submit()):
    - submit() sends correct multipart fields/headers, parses pdf_id
    - submit() raises MathpixError when the response body contains an
      `error` field despite a 2xx status
    - submit() raises httpx.HTTPStatusError on a non-2xx response
    - submit() raises FileNotFoundError for a missing pdf_path

Covered here (issue #2 — poll_until_complete()):
    - walks received -> loaded -> split -> completed, returns final payload
    - raises MathpixProcessingError on status == "error"
    - raises MathpixTimeoutError after max_poll_attempts exhausted
    - raises httpx.HTTPStatusError on a non-2xx (non-429) response
    - HTTP 429 responses are retried (honoring Retry-After) without
      counting against max_poll_attempts

Covered here (issue #3 — fetch_and_extract()), against
tests/fixtures/sample_result.md.zip:
    - waits for md.zip conversion_status to be "completed" before
      downloading, figures renamed sequentially in order of first
      appearance (with dedup for a repeated reference), markdown image
      refs rewritten to point at the renamed figures/ files
    - zero-figure case: no figures/ dir created, figure_count == 0
    - raises MathpixProcessingError when conversion_status == "error"
    - raises MathpixTimeoutError when conversion never becomes ready
    - raises httpx.HTTPStatusError on a non-2xx (non-429) response from
      either the converter status check or the .md.zip download
    - raises MathpixError when a referenced image is missing from the
      bundle

Covered here (issue #4 — process_pdf()):
    - happy path: orchestrates submit() -> poll_until_complete() ->
      fetch_and_extract() and returns a ProcessResult reflecting the
      fetch_and_extract() output, using Path(pdf_path).stem as
      lecture_stem
    - an injected client is used as-is and left open (not closed) by
      process_pdf()
    - when no client is injected, process_pdf() builds one from
      load_mathpix_credentials() and closes it afterward
    - failures at any stage (submit/poll/fetch) propagate unchanged rather
      than being caught/translated -- no mathpix_status field exists on
      ProcessResult (see AGENTS.md issue #4 notes)

Covered here (issue #6 — on_status observability callback):
    - poll_until_complete() invokes on_status("pdf", attempt,
      max_poll_attempts, status, payload) for every real poll (not 429
      retries), including the final terminal poll
    - _wait_for_conversion_ready() (via fetch_and_extract()) invokes
      on_status("conversion:md.zip", attempt, max_poll_attempts, status,
      payload) the same way
    - on_status defaults to None (no-op) and is optional everywhere it's
      threaded through
"""

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from src.mathpix import (
    DEFAULT_SUBMIT_OPTIONS,
    MathpixClient,
    MathpixError,
    MathpixProcessingError,
    MathpixTimeoutError,
    ProcessResult,
    process_pdf,
)

MATHPIX_BASE_URL = "https://api.mathpix.com"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_MD_ZIP = FIXTURES_DIR / "sample_result.md.zip"


@pytest.fixture
def fake_pdf(tmp_path):
    pdf_path = tmp_path / "lecture_01.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake content for testing\n")
    return pdf_path


@pytest.fixture
def client():
    with MathpixClient(app_id="test_app_id", app_key="test_app_key") as c:
        yield c


@pytest.fixture
def sleep_calls():
    """Records sleep_fn calls in place of a real time.sleep()."""
    calls: list[float] = []
    return calls


@pytest.fixture
def polling_client(sleep_calls):
    """A MathpixClient with a no-op, call-recording sleep_fn injected so
    poll_until_complete() tests never actually wait."""
    with MathpixClient(
        app_id="test_app_id",
        app_key="test_app_key",
        sleep_fn=sleep_calls.append,
    ) as c:
        yield c


def _status_response(status: str, **extra):
    return httpx.Response(200, json={"status": status, **extra})


@respx.mock
def test_submit_sends_correct_multipart_fields_and_headers(client, fake_pdf):
    route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )

    pdf_id = client.submit(fake_pdf)

    assert pdf_id == "abc123"
    assert route.called
    request = route.calls.last.request

    assert request.headers["app_id"] == "test_app_id"
    assert request.headers["app_key"] == "test_app_key"

    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data")

    body = request.content.decode("utf-8", errors="replace")
    assert 'name="file"' in body
    assert 'filename="lecture_01.pdf"' in body
    assert 'name="options_json"' in body

    # Extract the options_json field value and confirm it matches defaults.
    boundary = content_type.split("boundary=")[1]
    parts = body.split(f"--{boundary}")
    options_part = next(p for p in parts if 'name="options_json"' in p)
    options_value = options_part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]
    assert json.loads(options_value) == DEFAULT_SUBMIT_OPTIONS


@respx.mock
def test_submit_merges_extra_options(client, fake_pdf):
    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )

    client.submit(fake_pdf, options={"page_ranges": "1-2"})

    request = respx.calls.last.request
    content_type = request.headers["content-type"]
    boundary = content_type.split("boundary=")[1]
    body = request.content.decode("utf-8", errors="replace")
    parts = body.split(f"--{boundary}")
    options_part = next(p for p in parts if 'name="options_json"' in p)
    options_value = options_part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]
    sent_options = json.loads(options_value)

    assert sent_options["conversion_formats"] == {"md.zip": True}
    assert sent_options["page_ranges"] == "1-2"


@respx.mock
def test_submit_raises_on_error_field_in_2xx_response(client, fake_pdf):
    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(
            200, json={"error": "bad request", "error_info": {"id": "opts_invalid"}}
        )
    )

    with pytest.raises(MathpixError, match="bad request"):
        client.submit(fake_pdf)


@respx.mock
def test_submit_raises_on_missing_pdf_id(client, fake_pdf):
    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(MathpixError, match="missing pdf_id"):
        client.submit(fake_pdf)


@respx.mock
def test_submit_raises_on_http_error_status(client, fake_pdf):
    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(500, json={"error": "server error"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.submit(fake_pdf)


def test_submit_raises_filenotfounderror_for_missing_pdf(client, tmp_path):
    missing_path = tmp_path / "does_not_exist.pdf"

    with pytest.raises(FileNotFoundError):
        client.submit(missing_path)


# --- poll_until_complete() -----------------------------------------------


@respx.mock
def test_poll_walks_statuses_to_completed(polling_client, sleep_calls):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123")
    route.side_effect = [
        _status_response("received"),
        _status_response("loaded"),
        _status_response("split"),
        _status_response("completed", md="# Lecture"),
    ]

    payload = polling_client.poll_until_complete(
        "abc123", poll_interval_seconds=1, max_poll_attempts=10
    )

    assert payload == {"status": "completed", "md": "# Lecture"}
    assert route.call_count == 4
    # Slept between each non-terminal poll, but not after the final
    # (completed) response.
    assert sleep_calls == [1, 1, 1]

    request = route.calls.last.request
    assert request.headers["app_id"] == "test_app_id"
    assert request.headers["app_key"] == "test_app_key"


@respx.mock
def test_poll_raises_on_error_status(polling_client, sleep_calls):
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
        return_value=_status_response(
            "error", error="bad scan", error_info={"id": "image_error"}
        )
    )

    with pytest.raises(MathpixProcessingError, match="bad scan"):
        polling_client.poll_until_complete(
            "abc123", poll_interval_seconds=1, max_poll_attempts=10
        )

    assert sleep_calls == []


@respx.mock
def test_poll_raises_timeout_after_max_attempts(polling_client, sleep_calls):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
        return_value=_status_response("split")
    )

    with pytest.raises(MathpixTimeoutError, match="3 attempts"):
        polling_client.poll_until_complete(
            "abc123", poll_interval_seconds=1, max_poll_attempts=3
        )

    assert route.call_count == 3
    # Slept between attempts only (2 sleeps for 3 attempts), not after the
    # final failed attempt before giving up.
    assert sleep_calls == [1, 1]


@respx.mock
def test_poll_raises_on_http_error_status(polling_client):
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
        return_value=httpx.Response(500, json={"error": "server error"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        polling_client.poll_until_complete(
            "abc123", poll_interval_seconds=1, max_poll_attempts=3
        )


@respx.mock
def test_poll_retries_on_429_honoring_retry_after(polling_client, sleep_calls):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "2"}, json={}),
        _status_response("completed", md="# Lecture"),
    ]

    payload = polling_client.poll_until_complete(
        "abc123", poll_interval_seconds=1, max_poll_attempts=3
    )

    assert payload == {"status": "completed", "md": "# Lecture"}
    assert route.call_count == 2
    # The 429 backoff used Retry-After (2), not poll_interval_seconds (1),
    # and did not count against max_poll_attempts.
    assert sleep_calls == [2]


@respx.mock
def test_poll_429_without_retry_after_uses_poll_interval(polling_client, sleep_calls):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123")
    route.side_effect = [
        httpx.Response(429, json={}),
        _status_response("completed"),
    ]

    polling_client.poll_until_complete(
        "abc123", poll_interval_seconds=1, max_poll_attempts=3
    )

    assert sleep_calls == [1]


def test_poll_uses_config_defaults_when_args_omitted(monkeypatch, polling_client, sleep_calls):
    import src.mathpix as mathpix_module
    from src.config import MathpixPollingConfig

    monkeypatch.setattr(
        mathpix_module,
        "load_mathpix_polling_config",
        lambda: MathpixPollingConfig(poll_interval_seconds=1, max_poll_attempts=2),
    )

    with respx.mock:
        route = respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
            return_value=_status_response("split")
        )

        with pytest.raises(MathpixTimeoutError, match="2 attempts"):
            polling_client.poll_until_complete("abc123")

        assert route.call_count == 2
        assert sleep_calls == [1]


@respx.mock
def test_poll_invokes_on_status_for_every_real_poll(polling_client, sleep_calls):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "2"}, json={}),
        _status_response("received"),
        _status_response("split"),
        _status_response("completed", md="# Lecture"),
    ]

    calls: list[tuple] = []
    payload = polling_client.poll_until_complete(
        "abc123",
        poll_interval_seconds=1,
        max_poll_attempts=10,
        on_status=lambda *args: calls.append(args),
    )

    assert payload == {"status": "completed", "md": "# Lecture"}
    # The 429 retry is not reported to on_status -- only the 3 real polls.
    assert calls == [
        ("pdf", 1, 10, "received", {"status": "received"}),
        ("pdf", 2, 10, "split", {"status": "split"}),
        ("pdf", 3, 10, "completed", {"status": "completed", "md": "# Lecture"}),
    ]


@respx.mock
def test_poll_invokes_on_status_before_raising_on_error(polling_client):
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
        return_value=_status_response(
            "error", error="bad scan", error_info={"id": "image_error"}
        )
    )

    calls: list[tuple] = []
    with pytest.raises(MathpixProcessingError):
        polling_client.poll_until_complete(
            "abc123",
            poll_interval_seconds=1,
            max_poll_attempts=10,
            on_status=lambda *args: calls.append(args),
        )

    assert len(calls) == 1
    stage, attempt, max_attempts, status, payload = calls[0]
    assert (stage, attempt, max_attempts, status) == ("pdf", 1, 10, "error")


# --- fetch_and_extract() ---------------------------------------------------


def _converter_status_response(status: str | None, **extra):
    conversion_status: dict = {}
    if status is not None:
        conversion_status["md.zip"] = {"status": status, **extra}
    return httpx.Response(200, json={"status": "completed", "conversion_status": conversion_status})


def _build_zip_bytes(md_content: str, images: dict[str, bytes]) -> bytes:
    """Build an in-memory md.zip-shaped bundle for tests that need a bundle
    shape other than the committed tests/fixtures/sample_result.md.zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("sample_result.md", md_content)
        for name, content in images.items():
            zf.writestr(name, content)
    return buf.getvalue()


@respx.mock
def test_fetch_and_extract_happy_path(polling_client, sleep_calls, tmp_path):
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )

    dest_dir = tmp_path / "out"
    result = polling_client.fetch_and_extract(
        "abc123",
        dest_dir,
        lecture_stem="lecture_01",
        poll_interval_seconds=1,
        max_poll_attempts=5,
    )

    assert result.markdown_path == dest_dir / "lecture_01.mathpix.md"
    assert result.markdown_path.is_file()
    assert result.figures_dir == dest_dir / "figures"
    assert result.figure_count == 2

    fig1 = dest_dir / "figures" / "lecture_01_fig_001.jpg"
    fig2 = dest_dir / "figures" / "lecture_01_fig_002.jpg"
    assert fig1.is_file()
    assert fig2.is_file()

    with zipfile.ZipFile(SAMPLE_MD_ZIP) as original:
        assert fig1.read_bytes() == original.read("images/abc123.jpg")
        assert fig2.read_bytes() == original.read("images/def456.jpg")

    markdown_text = result.markdown_path.read_text()
    assert markdown_text.count("figures/lecture_01_fig_001.jpg") == 2
    assert markdown_text.count("figures/lecture_01_fig_002.jpg") == 1
    assert "images/abc123.jpg" not in markdown_text
    assert "images/def456.jpg" not in markdown_text


@respx.mock
def test_fetch_and_extract_waits_for_conversion_status(polling_client, sleep_calls, tmp_path):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123")
    route.side_effect = [
        _converter_status_response(None),
        _converter_status_response("completed"),
    ]
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )

    result = polling_client.fetch_and_extract(
        "abc123",
        tmp_path / "out",
        lecture_stem="lecture_01",
        poll_interval_seconds=1,
        max_poll_attempts=5,
    )

    assert result.figure_count == 2
    assert route.call_count == 2
    assert sleep_calls == [1]


@respx.mock
def test_fetch_and_extract_invokes_on_status_for_conversion_polls(
    polling_client, sleep_calls, tmp_path
):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123")
    route.side_effect = [
        _converter_status_response(None),
        _converter_status_response("completed"),
    ]
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )

    calls: list[tuple] = []
    polling_client.fetch_and_extract(
        "abc123",
        tmp_path / "out",
        lecture_stem="lecture_01",
        poll_interval_seconds=1,
        max_poll_attempts=5,
        on_status=lambda *args: calls.append(args),
    )

    assert [(c[0], c[1], c[2], c[3]) for c in calls] == [
        ("conversion:md.zip", 1, 5, None),
        ("conversion:md.zip", 2, 5, "completed"),
    ]


@respx.mock
def test_fetch_and_extract_raises_on_conversion_error(polling_client, tmp_path):
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response(
            "error", error="conversion failed", error_info={"id": "convert_error"}
        )
    )
    zip_route = respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip")

    with pytest.raises(MathpixProcessingError, match="conversion failed"):
        polling_client.fetch_and_extract(
            "abc123",
            tmp_path / "out",
            lecture_stem="lecture_01",
            poll_interval_seconds=1,
            max_poll_attempts=5,
        )

    assert not zip_route.called


@respx.mock
def test_fetch_and_extract_raises_timeout_when_conversion_never_ready(
    polling_client, sleep_calls, tmp_path
):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("processing")
    )

    with pytest.raises(MathpixTimeoutError, match="3 attempts"):
        polling_client.fetch_and_extract(
            "abc123",
            tmp_path / "out",
            lecture_stem="lecture_01",
            poll_interval_seconds=1,
            max_poll_attempts=3,
        )

    assert route.call_count == 3
    assert sleep_calls == [1, 1]


@respx.mock
def test_fetch_and_extract_raises_on_http_error_from_converter(polling_client, tmp_path):
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=httpx.Response(500, json={"error": "server error"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        polling_client.fetch_and_extract(
            "abc123",
            tmp_path / "out",
            lecture_stem="lecture_01",
            poll_interval_seconds=1,
            max_poll_attempts=3,
        )


@respx.mock
def test_fetch_and_extract_converter_retries_on_429_honoring_retry_after(
    polling_client, sleep_calls, tmp_path
):
    route = respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "2"}, json={}),
        _converter_status_response("completed"),
    ]
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )

    polling_client.fetch_and_extract(
        "abc123",
        tmp_path / "out",
        lecture_stem="lecture_01",
        poll_interval_seconds=1,
        max_poll_attempts=3,
    )

    assert route.call_count == 2
    assert sleep_calls == [2]


@respx.mock
def test_fetch_and_extract_raises_on_http_error_from_zip_download(polling_client, tmp_path):
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(500, json={"error": "server error"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        polling_client.fetch_and_extract(
            "abc123",
            tmp_path / "out",
            lecture_stem="lecture_01",
            poll_interval_seconds=1,
            max_poll_attempts=3,
        )


@respx.mock
def test_fetch_and_extract_zero_figures(polling_client, tmp_path):
    zip_bytes = _build_zip_bytes("# No figures here\n\nJust text.\n", images={})

    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )

    dest_dir = tmp_path / "out"
    result = polling_client.fetch_and_extract(
        "abc123",
        dest_dir,
        lecture_stem="lecture_02",
        poll_interval_seconds=1,
        max_poll_attempts=3,
    )

    assert result.figure_count == 0
    assert result.figures_dir is None
    assert not (dest_dir / "figures").exists()
    assert result.markdown_path.read_text() == "# No figures here\n\nJust text.\n"


@respx.mock
def test_fetch_and_extract_raises_on_missing_referenced_image(polling_client, tmp_path):
    zip_bytes = _build_zip_bytes(
        "# Lecture\n\n![](images/missing.jpg)\n", images={}
    )

    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )

    with pytest.raises(MathpixError, match="missing.jpg"):
        polling_client.fetch_and_extract(
            "abc123",
            tmp_path / "out",
            lecture_stem="lecture_01",
            poll_interval_seconds=1,
            max_poll_attempts=3,
        )


@respx.mock
def test_fetch_and_extract_raises_on_missing_md_file(polling_client, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("images/abc123.jpg", b"fake-bytes")
    zip_bytes = buf.getvalue()

    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )

    with pytest.raises(MathpixError, match="no .md file"):
        polling_client.fetch_and_extract(
            "abc123",
            tmp_path / "out",
            lecture_stem="lecture_01",
            poll_interval_seconds=1,
            max_poll_attempts=3,
        )


# --- process_pdf() ----------------------------------------------------------


def _mock_submit_poll_and_fetch_happy_path():
    """Mocks submit(), poll_until_complete(), and fetch_and_extract() so a
    full process_pdf() run completes immediately (no polling waits)."""
    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
        return_value=_status_response("completed", md="# Lecture")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )


@respx.mock
def test_process_pdf_happy_path(polling_client, sleep_calls, fake_pdf, tmp_path):
    _mock_submit_poll_and_fetch_happy_path()

    cache_dir = tmp_path / "cache"
    before = datetime.now(timezone.utc)
    result = process_pdf(
        fake_pdf,
        cache_dir,
        client=polling_client,
        poll_interval_seconds=1,
        max_poll_attempts=5,
    )
    after = datetime.now(timezone.utc)

    assert isinstance(result, ProcessResult)
    assert result.pdf_path == fake_pdf
    assert result.pdf_id == "abc123"
    assert result.markdown_path == cache_dir / "lecture_01.mathpix.md"
    assert result.markdown_path.is_file()
    assert result.figures_dir == cache_dir / "figures"
    assert result.figure_count == 2
    assert before <= result.processed_at <= after
    # Nothing here should have needed to sleep -- every poll response is
    # immediately terminal.
    assert sleep_calls == []


@respx.mock
def test_process_pdf_uses_lecture_stem_from_pdf_path(polling_client, tmp_path):
    _mock_submit_poll_and_fetch_happy_path()

    pdf_path = tmp_path / "lecture_07.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake content for testing\n")
    cache_dir = tmp_path / "cache"

    result = process_pdf(
        pdf_path,
        cache_dir,
        client=polling_client,
        poll_interval_seconds=1,
        max_poll_attempts=5,
    )

    assert result.markdown_path == cache_dir / "lecture_07.mathpix.md"


@respx.mock
def test_process_pdf_does_not_close_an_injected_client(client, fake_pdf, tmp_path):
    _mock_submit_poll_and_fetch_happy_path()

    process_pdf(fake_pdf, tmp_path / "cache", client=client, poll_interval_seconds=1, max_poll_attempts=5)

    assert client._http.is_closed is False


@respx.mock
def test_process_pdf_builds_and_closes_own_client_when_none_injected(
    monkeypatch, fake_pdf, tmp_path
):
    import src.mathpix as mathpix_module
    from src.config import MathpixCredentials

    monkeypatch.setattr(
        mathpix_module,
        "load_mathpix_credentials",
        lambda: MathpixCredentials(app_id="env_app_id", app_key="env_app_key"),
    )

    created_clients: list[MathpixClient] = []
    real_client_cls = mathpix_module.MathpixClient

    class RecordingClient(real_client_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_clients.append(self)

    monkeypatch.setattr(mathpix_module, "MathpixClient", RecordingClient)

    _mock_submit_poll_and_fetch_happy_path()

    process_pdf(fake_pdf, tmp_path / "cache", poll_interval_seconds=1, max_poll_attempts=5)

    assert len(created_clients) == 1
    assert created_clients[0].app_id == "env_app_id"
    assert created_clients[0].app_key == "env_app_key"
    assert created_clients[0]._http.is_closed is True


@respx.mock
def test_process_pdf_propagates_filenotfounderror_from_submit(polling_client, tmp_path):
    missing_pdf = tmp_path / "does_not_exist.pdf"

    with pytest.raises(FileNotFoundError):
        process_pdf(missing_pdf, tmp_path / "cache", client=polling_client)


@respx.mock
def test_process_pdf_propagates_mathpixerror_from_submit(polling_client, fake_pdf, tmp_path):
    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"error": "bad request"})
    )

    with pytest.raises(MathpixError, match="bad request"):
        process_pdf(
            fake_pdf,
            tmp_path / "cache",
            client=polling_client,
            poll_interval_seconds=1,
            max_poll_attempts=5,
        )


@respx.mock
def test_process_pdf_propagates_error_from_poll_until_complete(
    polling_client, fake_pdf, tmp_path
):
    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
        return_value=_status_response(
            "error", error="bad scan", error_info={"id": "image_error"}
        )
    )
    converter_route = respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123")

    with pytest.raises(MathpixProcessingError, match="bad scan"):
        process_pdf(
            fake_pdf,
            tmp_path / "cache",
            client=polling_client,
            poll_interval_seconds=1,
            max_poll_attempts=5,
        )

    assert not converter_route.called


@respx.mock
def test_process_pdf_propagates_timeout_from_fetch_and_extract(
    polling_client, fake_pdf, tmp_path
):
    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
        return_value=_status_response("completed", md="# Lecture")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("processing")
    )

    with pytest.raises(MathpixTimeoutError, match="2 attempts"):
        process_pdf(
            fake_pdf,
            tmp_path / "cache",
            client=polling_client,
            poll_interval_seconds=1,
            max_poll_attempts=2,
        )
