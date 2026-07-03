"""
Mathpix API client — Phase 1.

Given a single PDF path, submits it to Mathpix, polls until processing
completes, downloads the md.zip conversion bundle, extracts it, renames
figures to the lecture_N_fig_NNN convention, rewrites the Markdown image
references to match, and writes the result to _cache/.

See AGENTS.md "Mathpix API notes" for the verified API behavior this should
be built against (status values, multipart upload shape, figure handling via
md.zip, unconfirmed math delimiter format).

Implementation status:
    - submit()             implemented (issue #1)
    - poll_until_complete() implemented (issue #2)
    - fetch_and_extract()   not yet implemented (issue #3)
    - process_pdf()         not yet implemented (issue #4)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from src.config import load_mathpix_polling_config

DEFAULT_BASE_URL = "https://api.mathpix.com"

# Statuses documented at docs.mathpix.com; see AGENTS.md "Mathpix API notes"
# for corrections vs. the original spec (received -> loaded -> split ->
# completed, not loading -> processing -> completed).
_TERMINAL_SUCCESS_STATUS = "completed"
_TERMINAL_ERROR_STATUS = "error"

# Minimal Phase 1 options: request the md.zip conversion bundle (Markdown +
# embedded figures). Everything else (include_page_breaks, rm_spaces, math
# delimiter options, etc.) is deferred to later phases / Phase 6 config
# wiring — see AGENTS.md "Mathpix API notes" (math delimiter format is
# unconfirmed and only affects the `text` format, not `md`/`mmd` anyway).
DEFAULT_SUBMIT_OPTIONS: dict[str, Any] = {
    "conversion_formats": {"md.zip": True},
}


class MathpixError(Exception):
    """Base class for Mathpix API errors (bad request, error response body)."""


class MathpixProcessingError(MathpixError):
    """Raised when Mathpix reports status == "error" for a pdf_id while polling."""


class MathpixTimeoutError(MathpixError):
    """Raised when polling exceeds max_poll_attempts without reaching a
    terminal status ("completed" or "error")."""


class MathpixClient:
    """
    Thin client around the Mathpix /v3/pdf endpoints.

    Uses a synchronous httpx.Client so tests can inject one wired to a
    respx-mocked transport (see AGENTS.md Testing Conventions: unit tests
    must never hit the real Mathpix API).
    """

    def __init__(
        self,
        app_id: str,
        app_key: str,
        http_client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_key = app_key
        self.base_url = base_url.rstrip("/")
        self._http = http_client or httpx.Client()
        self._owns_http_client = http_client is None
        # Injectable so tests never actually sleep (see AGENTS.md Testing
        # Conventions / issue #2).
        self._sleep_fn = sleep_fn or time.sleep

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def __enter__(self) -> "MathpixClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"app_id": self.app_id, "app_key": self.app_key}

    def submit(self, pdf_path: str | Path, options: dict[str, Any] | None = None) -> str:
        """
        Upload a PDF to POST /v3/pdf via multipart form-data and return its
        pdf_id.

        Args:
            pdf_path: path to the local PDF file to upload.
            options: optional overrides/additions merged into
                DEFAULT_SUBMIT_OPTIONS before being sent as options_json.

        Raises:
            FileNotFoundError: if pdf_path does not exist.
            httpx.HTTPStatusError: on a non-2xx HTTP response.
            MathpixError: if the response is 2xx but contains an `error`
                field, or is missing `pdf_id`.
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        merged_options = dict(DEFAULT_SUBMIT_OPTIONS)
        if options:
            merged_options.update(options)

        with pdf_path.open("rb") as fh:
            files = {"file": (pdf_path.name, fh, "application/pdf")}
            data = {"options_json": json.dumps(merged_options)}
            response = self._http.post(
                f"{self.base_url}/v3/pdf",
                headers=self._auth_headers,
                files=files,
                data=data,
            )

        response.raise_for_status()
        payload = response.json()

        if payload.get("error"):
            raise MathpixError(
                f"Mathpix submit failed: {payload['error']} "
                f"(error_info={payload.get('error_info')})"
            )

        pdf_id = payload.get("pdf_id")
        if not pdf_id:
            raise MathpixError(f"Mathpix submit response missing pdf_id: {payload}")

        return pdf_id

    def poll_until_complete(
        self,
        pdf_id: str,
        poll_interval_seconds: float | None = None,
        max_poll_attempts: int | None = None,
    ) -> dict[str, Any]:
        """
        Poll GET /v3/pdf/{pdf_id} until status == "completed", returning the
        full JSON response payload.

        Args:
            pdf_id: the pdf_id returned by submit().
            poll_interval_seconds: seconds to sleep between polls. Defaults
                to the mathpix.poll_interval_seconds value from config.yaml
                (or DEFAULT_POLL_INTERVAL_SECONDS if unset/absent) when not
                given explicitly.
            max_poll_attempts: maximum number of status polls before giving
                up. Defaults to the mathpix.max_poll_attempts value from
                config.yaml (or DEFAULT_MAX_POLL_ATTEMPTS if unset/absent)
                when not given explicitly. HTTP 429 responses are retried
                (honoring Retry-After) and do not count against this limit.

        Returns:
            The full JSON payload from the poll response once
            status == "completed" (percent_done/num_pages_completed etc.
            stay internal to the payload for Phase 1 — no logging/printing
            here; see issue #2 discussion in AGENTS.md).

        Raises:
            MathpixProcessingError: if status == "error".
            MathpixTimeoutError: if max_poll_attempts is exhausted without
                reaching a terminal status.
            httpx.HTTPStatusError: on a non-2xx, non-429 HTTP response.
        """
        if poll_interval_seconds is None or max_poll_attempts is None:
            polling_config = load_mathpix_polling_config()
            if poll_interval_seconds is None:
                poll_interval_seconds = polling_config.poll_interval_seconds
            if max_poll_attempts is None:
                max_poll_attempts = polling_config.max_poll_attempts

        url = f"{self.base_url}/v3/pdf/{pdf_id}"
        last_status: str | None = None
        attempt = 0

        while attempt < max_poll_attempts:
            response = self._http.get(url, headers=self._auth_headers)

            if response.status_code == 429:
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"), default=poll_interval_seconds
                )
                self._sleep_fn(retry_after)
                continue

            response.raise_for_status()
            payload = response.json()
            last_status = payload.get("status")

            if last_status == _TERMINAL_SUCCESS_STATUS:
                return payload

            if last_status == _TERMINAL_ERROR_STATUS:
                raise MathpixProcessingError(
                    f"Mathpix processing failed for pdf_id={pdf_id}: "
                    f"error={payload.get('error')!r} "
                    f"error_info={payload.get('error_info')!r}"
                )

            attempt += 1
            if attempt < max_poll_attempts:
                self._sleep_fn(poll_interval_seconds)

        raise MathpixTimeoutError(
            f"Mathpix polling timed out for pdf_id={pdf_id} after "
            f"{max_poll_attempts} attempts (last status={last_status!r})"
        )

    def fetch_and_extract(self, pdf_id: str, dest_dir: str | Path) -> Any:
        """
        TODO(issue #3): download the md.zip bundle, extract it, rename
        figures, rewrite Markdown image references, and write to dest_dir.
        """
        raise NotImplementedError("fetch_and_extract: see issue #3")


def _parse_retry_after(value: str | None, default: float) -> float:
    """
    Parse a Retry-After header value (seconds, per Mathpix's documented
    usage) into a float. Falls back to `default` if the header is absent or
    not a plain number (e.g. an HTTP-date form, which Mathpix does not use).
    """
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def process_pdf(pdf_path: str | Path, cache_dir: str | Path) -> Any:
    """
    TODO(issue #4): orchestrate submit -> poll_until_complete ->
    fetch_and_extract and return a ProcessResult.
    """
    raise NotImplementedError("process_pdf: see issue #4")
