"""
Mathpix API client — Phase 1.

Given a single PDF path, submits it to Mathpix, polls until processing
completes, downloads the md.zip conversion bundle, extracts it, renames
figures to the {lecture_stem}_fig_{NNN} convention, rewrites the Markdown
image references to match, and writes the result to _cache/.

See AGENTS.md "Mathpix API notes" for the verified API behavior this should
be built against (status values, multipart upload shape, figure handling via
md.zip, unconfirmed math delimiter format).

Implementation status:
    - submit()             implemented (issue #1)
    - poll_until_complete() implemented (issue #2)
    - fetch_and_extract()   implemented (issue #3)
    - process_pdf()         implemented (issue #4)
    - on_status callback    implemented (issue #6, for scripts/smoke_test_mathpix.py)
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from src.config import load_mathpix_credentials, load_mathpix_polling_config

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

# Observability hook shared by poll_until_complete(), _wait_for_conversion_ready(),
# fetch_and_extract(), and process_pdf() (see issue #6): called as
# on_status(stage, attempt, max_poll_attempts, status, payload) after every
# real status poll. `stage` is "pdf" for poll_until_complete()'s main status
# poll, or "conversion:{conversion_format}" (e.g. "conversion:md.zip") for
# _wait_for_conversion_ready(). Purely for observability (e.g. the manual
# smoke test script printing progress) -- never affects control flow.
OnStatusCallback = Callable[[str, int, int, str | None, dict[str, Any]], None]


@dataclass(frozen=True)
class FetchResult:
    """
    Result of MathpixClient.fetch_and_extract().

    figures_dir is None and figure_count is 0 when the Markdown contains no
    image references (no figures/ subdirectory is created in that case).
    """

    markdown_path: Path
    figures_dir: Path | None
    figure_count: int


@dataclass(frozen=True)
class ProcessResult:
    """
    Result of process_pdf(): the orchestrated submit -> poll_until_complete
    -> fetch_and_extract pipeline for a single PDF.

    Only ever returned on success -- any failure at any stage propagates as
    an exception (FileNotFoundError, MathpixError/MathpixProcessingError/
    MathpixTimeoutError, or httpx.HTTPStatusError) instead of being encoded
    here. There is deliberately no mathpix_status field: Phase 2's state log
    writer is expected to catch process_pdf()'s exceptions (or lack thereof)
    to decide the mathpix_status ("success"/"failed") column itself.

    Field names anticipate Phase 2's state.db columns (see docs/spec.md
    State Management / AGENTS.md issue #4 notes): pdf_path -> source_path,
    pdf_id -> mathpix_pdf_id, figure_count -> figure_count, processed_at ->
    mathpix_processed_at.
    """

    pdf_path: Path
    pdf_id: str
    markdown_path: Path
    figures_dir: Path | None
    figure_count: int
    processed_at: datetime


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
        on_status: OnStatusCallback | None = None,
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
            on_status: optional callback invoked as
                on_status("pdf", attempt, max_poll_attempts, status, payload)
                after every real status poll (429 retries are not reported),
                including the final terminal poll (whether "completed" or
                right before raising on "error"). Purely an observability
                hook for callers like the manual smoke test script (issue
                #6) — has no effect on control flow, and percent_done /
                num_pages_completed etc. still stay internal to the payload
                otherwise (no logging/printing infra here beyond this hook).

        Returns:
            The full JSON payload from the poll response once
            status == "completed".

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

            if on_status is not None:
                on_status("pdf", attempt + 1, max_poll_attempts, last_status, payload)

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

    def _wait_for_conversion_ready(
        self,
        pdf_id: str,
        conversion_format: str,
        poll_interval_seconds: float,
        max_poll_attempts: int,
        on_status: OnStatusCallback | None = None,
    ) -> None:
        """
        Poll GET /v3/converter/{pdf_id} until
        conversion_status[conversion_format]["status"] == "completed".

        Conversion formats (e.g. md.zip) have their own readiness status,
        separate from the main PDF `status` field checked by
        poll_until_complete() — per docs.mathpix.com, downloading a
        conversion format result requires that format's own conversion
        status to be "completed", which can lag behind the main PDF
        status. This must be checked before downloading the .md.zip
        result even after poll_until_complete() has already returned.

        Args:
            on_status: optional callback invoked as
                on_status(f"conversion:{conversion_format}", attempt,
                max_poll_attempts, status, payload) after every real poll
                (see OnStatusCallback / issue #6). Observability only.

        Raises:
            MathpixProcessingError: if the format's status == "error".
            MathpixTimeoutError: if max_poll_attempts is exhausted without
                the format reaching a terminal status.
            httpx.HTTPStatusError: on a non-2xx, non-429 HTTP response.
        """
        url = f"{self.base_url}/v3/converter/{pdf_id}"
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
            conversion_status = payload.get("conversion_status") or {}
            format_status = conversion_status.get(conversion_format) or {}
            last_status = format_status.get("status")

            if on_status is not None:
                on_status(
                    f"conversion:{conversion_format}",
                    attempt + 1,
                    max_poll_attempts,
                    last_status,
                    payload,
                )

            if last_status == _TERMINAL_SUCCESS_STATUS:
                return

            if last_status == _TERMINAL_ERROR_STATUS:
                raise MathpixProcessingError(
                    f"Mathpix conversion failed for pdf_id={pdf_id} "
                    f"format={conversion_format!r}: "
                    f"error={format_status.get('error')!r} "
                    f"error_info={format_status.get('error_info')!r}"
                )

            attempt += 1
            if attempt < max_poll_attempts:
                self._sleep_fn(poll_interval_seconds)

        raise MathpixTimeoutError(
            f"Mathpix conversion polling timed out for pdf_id={pdf_id} "
            f"format={conversion_format!r} after {max_poll_attempts} attempts "
            f"(last status={last_status!r})"
        )

    def fetch_and_extract(
        self,
        pdf_id: str,
        dest_dir: str | Path,
        lecture_stem: str,
        poll_interval_seconds: float | None = None,
        max_poll_attempts: int | None = None,
        on_status: OnStatusCallback | None = None,
    ) -> FetchResult:
        """
        Download the md.zip conversion bundle for pdf_id, extract it,
        rename figures to the {lecture_stem}_fig_{NNN} convention
        (zero-padded, in order of first appearance in the Markdown),
        rewrite the Markdown's image references to match, and write both
        to dest_dir.

        Args:
            pdf_id: the pdf_id returned by submit().
            dest_dir: directory to write {lecture_stem}.mathpix.md (and a
                figures/ subdirectory, if there are any figures) into.
                Created if it doesn't already exist.
            lecture_stem: filename stem used for both the output Markdown
                file and the figure naming convention (e.g. "lecture_01"
                for a source PDF named lecture_01.pdf). Callers (see
                process_pdf(), issue #4) are expected to pass
                Path(pdf_path).stem.
            poll_interval_seconds: seconds to sleep between conversion
                readiness polls. Defaults to config.yaml's
                mathpix.poll_interval_seconds (see poll_until_complete())
                when not given explicitly.
            max_poll_attempts: maximum number of conversion readiness polls
                before giving up. Defaults to config.yaml's
                mathpix.max_poll_attempts when not given explicitly. HTTP
                429 responses are retried and do not count against this
                limit.
            on_status: optional callback forwarded to
                _wait_for_conversion_ready() (see OnStatusCallback / issue
                #6). Observability only.

        Returns:
            A FetchResult with the written markdown_path, figures_dir
            (None if there were no figures), and figure_count.

        Raises:
            MathpixError: if the md.zip bundle contains no .md file, or a
                Markdown image reference can't be resolved to a file
                inside the bundle.
            MathpixProcessingError: if the md.zip conversion status is
                "error".
            MathpixTimeoutError: if conversion readiness polling exhausts
                max_poll_attempts.
            httpx.HTTPStatusError: on a non-2xx HTTP response from either
                the converter status check or the .md.zip download.
        """
        if poll_interval_seconds is None or max_poll_attempts is None:
            polling_config = load_mathpix_polling_config()
            if poll_interval_seconds is None:
                poll_interval_seconds = polling_config.poll_interval_seconds
            if max_poll_attempts is None:
                max_poll_attempts = polling_config.max_poll_attempts

        conversion_format = "md.zip"
        self._wait_for_conversion_ready(
            pdf_id,
            conversion_format,
            poll_interval_seconds,
            max_poll_attempts,
            on_status=on_status,
        )

        response = self._http.get(
            f"{self.base_url}/v3/pdf/{pdf_id}.md.zip", headers=self._auth_headers
        )
        response.raise_for_status()

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            names = bundle.namelist()
            md_members = [name for name in names if name.endswith(".md")]
            if not md_members:
                raise MathpixError(
                    f"md.zip bundle for pdf_id={pdf_id} contains no .md file: "
                    f"{names}"
                )
            md_member_name = md_members[0]
            markdown_text = bundle.read(md_member_name).decode("utf-8")
            md_dir = posixpath.dirname(md_member_name)

            image_refs = _extract_image_refs_in_order(markdown_text)

            figures_dir: Path | None = None
            path_map: dict[str, str] = {}

            if image_refs:
                figures_dir = dest_dir / "figures"
                figures_dir.mkdir(parents=True, exist_ok=True)

                for index, image_ref in enumerate(image_refs, start=1):
                    member_name = posixpath.normpath(
                        posixpath.join(md_dir, image_ref) if md_dir else image_ref
                    )
                    if member_name not in names:
                        raise MathpixError(
                            f"md.zip bundle for pdf_id={pdf_id} references "
                            f"image {image_ref!r} (resolved to "
                            f"{member_name!r}) not present in bundle: {names}"
                        )

                    ext = Path(image_ref).suffix or ".jpg"
                    figure_number = str(index).zfill(_FIGURE_NUMBER_WIDTH)
                    figure_filename = f"{lecture_stem}_fig_{figure_number}{ext}"
                    (figures_dir / figure_filename).write_bytes(bundle.read(member_name))
                    path_map[image_ref] = f"figures/{figure_filename}"

        rewritten_markdown = _rewrite_image_refs(markdown_text, path_map)
        markdown_path = dest_dir / f"{lecture_stem}.mathpix.md"
        markdown_path.write_text(rewritten_markdown, encoding="utf-8")

        return FetchResult(
            markdown_path=markdown_path,
            figures_dir=figures_dir,
            figure_count=len(image_refs),
        )


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


# Number of zero-padded digits in the {lecture_stem}_fig_{NNN} figure naming
# convention (see AGENTS.md / issue #3).
_FIGURE_NUMBER_WIDTH = 3

# Matches standard Markdown image syntax: ![alt text](path). Deliberately
# does not attempt to handle Mathpix's chemistry SMILES alt-text annotations
# (e.g. ![<smiles>CCC</smiles>](...)) specially -- alt text is treated
# opaquely and passed through unchanged.
_IMAGE_REF_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")


def _extract_image_refs_in_order(markdown_text: str) -> list[str]:
    """
    Return the list of unique image reference paths in markdown_text, in
    order of first appearance. A path referenced more than once appears
    only once, at the position of its first occurrence.
    """
    seen: list[str] = []
    for match in _IMAGE_REF_PATTERN.finditer(markdown_text):
        path = match.group(2)
        if path not in seen:
            seen.append(path)
    return seen


def _rewrite_image_refs(markdown_text: str, path_map: dict[str, str]) -> str:
    """
    Replace each image reference's path with its mapped replacement from
    path_map (leaving alt text untouched). Paths not present in path_map
    are left as-is.
    """

    def _replace(match: re.Match[str]) -> str:
        alt_text, path = match.group(1), match.group(2)
        return f"![{alt_text}]({path_map.get(path, path)})"

    return _IMAGE_REF_PATTERN.sub(_replace, markdown_text)


def process_pdf(
    pdf_path: str | Path,
    cache_dir: str | Path,
    client: MathpixClient | None = None,
    poll_interval_seconds: float | None = None,
    max_poll_attempts: int | None = None,
    on_status: OnStatusCallback | None = None,
) -> ProcessResult:
    """
    Orchestrate submit() -> poll_until_complete() -> fetch_and_extract() for
    a single PDF and return a ProcessResult.

    Args:
        pdf_path: path to the local PDF file to process. Its filename stem
            (Path(pdf_path).stem) is used as fetch_and_extract()'s
            lecture_stem.
        cache_dir: directory to write the cached Markdown (and figures/, if
            any) into -- passed straight through as fetch_and_extract()'s
            dest_dir. Phase 1 has no discovery loop / course-folder mirroring
            yet (see AGENTS.md), so no per-course subdirectory logic happens
            here.
        client: an already-constructed MathpixClient to use, e.g. one wired
            to a respx-mocked http_client and a no-op sleep_fn for tests
            (mirrors the http_client=/sleep_fn= injection pattern on
            MathpixClient itself). When omitted, process_pdf() loads
            credentials via load_mathpix_credentials() and constructs, owns,
            and closes its own MathpixClient.
        poll_interval_seconds: forwarded untouched to both
            poll_until_complete() and fetch_and_extract(); each
            independently falls back to config.yaml's
            mathpix.poll_interval_seconds when left as None.
        max_poll_attempts: forwarded untouched to both
            poll_until_complete() and fetch_and_extract(); each
            independently falls back to config.yaml's
            mathpix.max_poll_attempts when left as None.
        on_status: optional callback forwarded to both
            poll_until_complete() and fetch_and_extract() (see
            OnStatusCallback / issue #6). Observability only, e.g. for the
            manual smoke test script to print live status transitions.

    Returns:
        A ProcessResult on success. There is no failure-path return value --
        see ProcessResult's docstring for why.

    Raises:
        FileNotFoundError: if pdf_path does not exist (from submit()).
        MathpixError: on a 2xx-with-error submit response, a missing pdf_id,
            or an unresolvable/missing figure reference in the md.zip bundle
            (from submit()/fetch_and_extract()).
        MathpixProcessingError: if Mathpix reports status == "error" at
            either the main polling stage or the md.zip conversion-readiness
            stage.
        MathpixTimeoutError: if either polling stage exhausts
            max_poll_attempts without reaching a terminal status.
        httpx.HTTPStatusError: on any non-2xx, non-429 HTTP response.
    """
    owns_client = client is None
    if client is None:
        credentials = load_mathpix_credentials()
        client = MathpixClient(credentials.app_id, credentials.app_key)

    try:
        pdf_id = client.submit(pdf_path)
        client.poll_until_complete(
            pdf_id,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
            on_status=on_status,
        )
        fetch_result = client.fetch_and_extract(
            pdf_id,
            cache_dir,
            Path(pdf_path).stem,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
            on_status=on_status,
        )
    finally:
        if owns_client:
            client.close()

    return ProcessResult(
        pdf_path=Path(pdf_path),
        pdf_id=pdf_id,
        markdown_path=fetch_result.markdown_path,
        figures_dir=fetch_result.figures_dir,
        figure_count=fetch_result.figure_count,
        processed_at=datetime.now(timezone.utc),
    )
