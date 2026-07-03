"""
Unit tests for src/mathpix.py — all HTTP calls mocked via respx.

Must never hit the real Mathpix API (see AGENTS.md Testing Conventions).

Covered here (issue #1 — submit()):
    - submit() sends correct multipart fields/headers, parses pdf_id
    - submit() raises MathpixError when the response body contains an
      `error` field despite a 2xx status
    - submit() raises httpx.HTTPStatusError on a non-2xx response
    - submit() raises FileNotFoundError for a missing pdf_path

Still TODO (later issues):
    - poll_until_complete() walks received -> loaded -> split -> completed
    - poll_until_complete() raises on status == "error"
    - poll_until_complete() raises/times out after max_poll_attempts
    - HTTP 429 respects Retry-After header
    - fetch_and_extract() against tests/fixtures/sample_result.md.zip:
      figures renamed sequentially, markdown image refs rewritten to match
"""

import json

import httpx
import pytest
import respx

from src.mathpix import DEFAULT_SUBMIT_OPTIONS, MathpixClient, MathpixError

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
