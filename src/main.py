"""
Phase 2 orchestration entry point.

Wires discovery (src/discovery.py, issues #8/#9) + the state log
(src/state.py, issue #7) + Phase 1's process_pdf() (src/mathpix.py) into a
single runnable pass over paths.input_root. No CLI flags yet (--dry-run /
--force / --course / --verbose are Phase 7, per docs/spec.md's roadmap).

Two entry points:
    - run(paths_config, conn, client=None) -> RunSummary
        The core, directly testable orchestration logic. Takes an already-
        loaded PathsConfig and an already-open state.db connection so tests
        can supply a tmp_path input_root tree, a real temp state.db, and an
        injected (respx-mocked) MathpixClient without going through argparse
        or touching the real filesystem/config.yaml/Mathpix API.
    - main(argv=None) -> int
        Thin CLI wrapper: load_paths_config() -> init_db() -> run() ->
        print summary -> exit code.

Per-file flow (see AGENTS.md issue #11 notes):
    - Classification.UNCHANGED: no-op (classify_pdf() already persisted any
      drifted mtime/size metadata itself -- see discovery.py). Tallied as
      "skipped".
    - Classification.NEW / CHANGED / RETRY: process_pdf() is called against
      a per-course cache_dir (paths.cache_dir / course, mirroring the
      vault's eventual per-course structure). On success, upsert_entry()
      records mathpix_status="success" plus the hash/mtime/size already
      computed by classify_pdf() (no re-hashing) and the pdf_id/figure_count
      /processed_at from the ProcessResult. On MathpixError /
      httpx.HTTPStatusError / FileNotFoundError, upsert_entry() instead
      records mathpix_status="failed" (still refreshing hash/mtime/size so
      tier-1 change detection is correct next run) and the run continues to
      the next file -- one bad file never aborts the whole run (per
      docs/spec.md's Error Handling table).
    - UNGROUPED_COURSE_KEY files (PDFs directly under input_root, not in any
      course subfolder) are deliberately *not* processed this phase: there's
      no course to mirror in cache_dir, so each one is logged as a warning
      and left out of state.db entirely, tallied separately as
      RunSummary.ungrouped rather than folded into processed/skipped/errors.

A single MathpixClient is constructed once per run() call (when not
injected) and reused across every file in the run -- rather than the default
per-call client construction inside process_pdf() -- so the whole run shares
one HTTP connection instead of opening/closing one per PDF.

Exit code: main() always returns 0 once run() completes, even if
RunSummary.errors > 0 -- per-file Mathpix failures are recorded in state.db
and reflected in the printed summary, not treated as a fatal run failure.
main() only returns non-zero for something that prevents the run from
starting at all (e.g. ConfigError from a missing/invalid config.yaml).
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from src.config import ConfigError, PathsConfig, load_mathpix_credentials, load_paths_config
from src.discovery import UNGROUPED_COURSE_KEY, Classification, discover_pdfs
from src.mathpix import MathpixClient, MathpixError, process_pdf
from src.state import init_db, upsert_entry

# Classifications that require actually calling process_pdf() -- UNCHANGED is
# intentionally excluded (already fully handled by classify_pdf() itself).
_ACTIONABLE_CLASSIFICATIONS = (
    Classification.NEW,
    Classification.CHANGED,
    Classification.RETRY,
)

# Exceptions process_pdf() can raise on failure (see src/mathpix.py's
# process_pdf() docstring) that should be caught, recorded as
# mathpix_status="failed", and not abort the run.
_PROCESS_PDF_FAILURE_EXCEPTIONS = (MathpixError, httpx.HTTPStatusError, FileNotFoundError)


@dataclass(frozen=True)
class RunSummary:
    """Basic end-of-run counts (full polish/formatting is Phase 7)."""

    processed: int
    skipped: int
    errors: int
    ungrouped: int


def run(
    paths_config: PathsConfig,
    conn: sqlite3.Connection,
    client: MathpixClient | None = None,
) -> RunSummary:
    """
    Discover new/changed/failed-retry PDFs under paths_config.input_root,
    process each through process_pdf(), and record the outcome to state.db.

    Args:
        paths_config: resolved paths (input_root/cache_dir/state_db) --
            only input_root/cache_dir are used here; conn is expected to
            already be open against paths_config.state_db.
        conn: an open sqlite3.Connection from state.init_db().
        client: an already-constructed MathpixClient to reuse across every
            file in the run (e.g. a respx-mocked one for tests). When
            omitted, one is built from load_mathpix_credentials() and
            closed at the end of the run.

    Returns:
        A RunSummary with processed/skipped/errors/ungrouped counts.
    """
    owns_client = client is None
    if client is None:
        credentials = load_mathpix_credentials()
        client = MathpixClient(credentials.app_id, credentials.app_key)

    processed = 0
    skipped = 0
    errors = 0
    ungrouped = 0

    try:
        results_by_course = discover_pdfs(paths_config.input_root, conn)

        for course, results in results_by_course.items():
            if course == UNGROUPED_COURSE_KEY:
                for result in results:
                    print(
                        f"[ungrouped] {Path(result.source_path).name}: "
                        "skipping -- no course subfolder to group it under "
                        "(not written to state.db)"
                    )
                    ungrouped += 1
                continue

            cache_dir = paths_config.cache_dir / course

            for result in results:
                filename = Path(result.source_path).name

                if result.classification not in _ACTIONABLE_CLASSIFICATIONS:
                    skipped += 1
                    continue

                print(
                    f"[{course}] {filename}: processing "
                    f"({result.classification.value})..."
                )
                try:
                    process_result = process_pdf(result.source_path, cache_dir, client=client)
                except _PROCESS_PDF_FAILURE_EXCEPTIONS as exc:
                    print(f"[{course}] {filename}: FAILED: {exc}")
                    upsert_entry(
                        conn,
                        result.source_path,
                        source_hash=result.source_hash,
                        source_mtime=result.source_mtime,
                        source_size=result.source_size,
                        mathpix_status="failed",
                    )
                    errors += 1
                    continue

                print(f"[{course}] {filename}: done")
                upsert_entry(
                    conn,
                    result.source_path,
                    source_hash=result.source_hash,
                    source_mtime=result.source_mtime,
                    source_size=result.source_size,
                    mathpix_status="success",
                    mathpix_pdf_id=process_result.pdf_id,
                    figure_count=process_result.figure_count,
                    mathpix_processed_at=process_result.processed_at,
                )
                processed += 1
    finally:
        if owns_client:
            client.close()

    return RunSummary(processed=processed, skipped=skipped, errors=errors, ungrouped=ungrouped)


def _print_summary(summary: RunSummary) -> None:
    print()
    print("Done.")
    print(f"  Processed:  {summary.processed}")
    print(f"  Skipped:    {summary.skipped}")
    print(f"  Errors:     {summary.errors}")
    print(f"  Ungrouped:  {summary.ungrouped}")


def main(argv: list[str] | None = None) -> int:
    """
    CLI entry point: load config.yaml's paths:, open/init state.db, run the
    discover -> process -> record pipeline once, print a summary.

    No flags yet (--dry-run/--force/--course/--verbose are Phase 7). Hits
    the real, paid Mathpix API -- same caution as
    scripts/smoke_test_mathpix.py.
    """
    try:
        paths_config = load_paths_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conn = init_db(paths_config.state_db)
    summary = run(paths_config, conn)
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
