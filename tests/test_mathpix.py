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

Still TODO (later issues):
    - fetch_and_extract() against tests/fixtures/sample_result.md.zip:
      figures renamed sequentially, markdown image refs rewritten to match
"""

import json

import httpx
import pytest
import respx

from src.mathpix import (
    DEFAULT_SUBMIT_OPTIONS,
    MathpixClient,
    MathpixError,
    MathpixProcessingError,
    MathpixTimeoutError,
)

MATHPIX_BASE_URL = "https://api.mathpix.com"


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
