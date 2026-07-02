"""
Unit tests for src/mathpix.py — all HTTP calls mocked via respx.

Must never hit the real Mathpix API (see AGENTS.md Testing Conventions).

Planned cases (see project discussion for full list):
    - submit() sends correct multipart fields/headers, parses pdf_id
    - poll_until_complete() walks received -> loaded -> split -> completed
    - poll_until_complete() raises on status == "error"
    - poll_until_complete() raises/times out after max_poll_attempts
    - HTTP 429 respects Retry-After header
    - fetch_and_extract() against tests/fixtures/sample_result.md.zip:
      figures renamed sequentially, markdown image refs rewritten to match

TODO(phase-1): implement once src/mathpix.py exists.
"""
