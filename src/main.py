"""
Phase 2/3 orchestration entry point.

Wires discovery (src/discovery.py, issues #8/#9) + the state log
(src/state.py, issue #7) + Phase 1's process_pdf() (src/mathpix.py) + Phase
3's cleanup_pdf() (src/llm.py, issues #15-#17) into a single runnable pass
over paths.input_root. No CLI flags yet (--dry-run / --force / --course /
--verbose, and the eventual --refresh-llm-prompt / --file flags, are Phase 7
per docs/spec.md's roadmap).

Two entry points:
    - run(paths_config, conn, client=None, llm_config=None,
          force_llm=False, target_source_path=None) -> RunSummary
        The core, directly testable orchestration logic. Takes an already-
        loaded PathsConfig and an already-open state.db connection so tests
        can supply a tmp_path input_root tree, a real temp state.db, and an
        injected (respx-mocked) MathpixClient without going through argparse
        or touching the real filesystem/config.yaml/Mathpix API.
    - main(argv=None) -> int
        Thin CLI wrapper: load_paths_config() -> init_db() -> run() ->
        print summary -> exit code.

Per-file flow (see AGENTS.md issue #11/#18 notes):
    - Classification.UNCHANGED: no full Mathpix reprocessing (classify_pdf()
      already persisted any drifted mtime/size metadata itself -- see
      discovery.py). If the file's state.db entry has mathpix_status ==
      "success" and its LLM stage is stale (needs_llm_reprocessing()) or
      force_llm=True, the LLM stage alone is (re)run against the cached
      .mathpix.md -- no Mathpix API call. Otherwise fully skipped. Tallied
      as "skipped" or "llm_reprocessed" respectively.
    - Classification.NEW / CHANGED / RETRY: process_pdf() is called against
      a per-course cache_dir (paths.cache_dir / course, mirroring the
      vault's eventual per-course structure). On success, cleanup_pdf() is
      immediately run against the freshly-produced .mathpix.md (same
      cache_dir), and upsert_entry() records both the mathpix_* fields and
      the llm_*/output_path fields in one go. On MathpixError /
      httpx.HTTPStatusError / FileNotFoundError, upsert_entry() instead
      records mathpix_status="failed" (still refreshing hash/mtime/size so
      tier-1 change detection is correct next run), the LLM stage is never
      attempted for that file this run, and the run continues to the next
      file -- one bad file never aborts the whole run (per docs/spec.md's
      Error Handling table).
    - UNGROUPED_COURSE_KEY files (PDFs directly under input_root, not in any
      course subfolder) are, in the normal (non-target_source_path) run,
      deliberately *not* processed: there's no course to mirror in
      cache_dir, so each one is logged as a warning and left out of
      state.db entirely, tallied separately as RunSummary.ungrouped rather
      than folded into processed/skipped/errors. The one exception is the
      target_source_path branch below, which force-processes an ungrouped
      target file anyway (see its own docstring note).
    - target_source_path (issue #18 infra, no CLI flag yet): when given,
      restricts the entire run to exactly that one PDF instead of walking
      discover_pdfs() over all of input_root. Its course is resolved from
      the path's first component relative to input_root (or
      UNGROUPED_COURSE_KEY if the file sits directly under input_root);
      classify_pdf() is called directly on just that file (no full
      recursive walk needed for a single known path). Unlike the normal
      loop, an ungrouped target file is *not* skipped -- since the caller
      explicitly named this exact file, it's force-processed using
      paths_config.cache_dir / "_ungrouped" as its cache dir (a reserved
      sentinel subfolder, mirroring UNGROUPED_COURSE_KEY's role in
      discovery.py). Combined with force_llm=True, this gives exactly the
      "reprocess just this one lecture's LLM stage after tweaking the
      prompt" workflow described in AGENTS.md.
    - force_llm=True (issue #18 infra, no CLI flag yet): bypasses
      needs_llm_reprocessing() for UNCHANGED files, reprocessing every
      eligible file's LLM stage with the currently configured
      llm.prompt_version regardless of its stored status/version. Never
      triggered automatically by a stale llm_prompt_version -- see
      AGENTS.md's "Deliberate correction to docs/spec.md's Reprocessing
      logic table".

A single MathpixClient and a single LLMClient are each constructed once per
run() call (when not injected/configured) and reused across every file in
the run -- rather than the default per-call client construction inside
process_pdf()/cleanup_pdf() -- so the whole run shares one HTTP connection
(Mathpix) and one client instance (LLM) instead of building one per PDF.

Exit code: main() always returns 0 once run() completes, even if
RunSummary.errors > 0 -- per-file Mathpix/LLM failures are recorded in
state.db and reflected in the printed summary, not treated as a fatal run
failure. main() only returns non-zero for something that prevents the run
from starting at all (e.g. ConfigError from a missing/invalid config.yaml).
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from src.config import (
    ConfigError,
    LLMConfig,
    PathsConfig,
    load_llm_config,
    load_mathpix_credentials,
    load_paths_config,
)
from src.discovery import (
    UNGROUPED_COURSE_KEY,
    Classification,
    ClassificationResult,
    classify_pdf,
    discover_pdfs,
)
from src.llm import LLMClient, cleanup_pdf, needs_llm_reprocessing
from src.mathpix import MathpixClient, MathpixError, process_pdf
from src.state import get_entry, init_db, upsert_entry

# Reserved cache_dir subfolder name for a force-processed ungrouped
# target_source_path file (see module docstring). Not a real course name --
# mirrors discovery.py's UNGROUPED_COURSE_KEY sentinel, but as an actual
# filesystem folder name since (unlike the normal-run ungrouped case)
# target_source_path does write cache/state for this file.
_UNGROUPED_CACHE_SUBDIR = "_ungrouped"

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
    llm_reprocessed: int = 0


@dataclass(frozen=True)
class _FileOutcome:
    """Per-file tally increments returned by _process_file(), accumulated
    by run() into its final RunSummary."""

    processed: int = 0
    skipped: int = 0
    errors: int = 0
    llm_reprocessed: int = 0


def _process_file(
    result: ClassificationResult,
    cache_dir: Path,
    mathpix_client: MathpixClient,
    llm_client: LLMClient,
    llm_config: LLMConfig,
    conn: sqlite3.Connection,
    force_llm: bool,
    course_label: str,
) -> _FileOutcome:
    """
    Shared per-file processing body: Mathpix-stage handling, LLM-stage
    handling, and the upsert_entry() calls that record their outcomes. Used
    identically by run()'s normal per-course loop and its target_source_path
    branch, so the two entry points are guaranteed to behave the same way.

    Args:
        result: a discovery.ClassificationResult for this file.
        cache_dir: the (course-specific, or _ungrouped-sentinel) cache
            directory to pass through as both process_pdf()'s and
            cleanup_pdf()'s dest_dir.
        mathpix_client: the run's shared MathpixClient.
        llm_client: the run's shared LLMClient.
        llm_config: the run's resolved LLMConfig.
        conn: an open state.db connection.
        force_llm: whether to bypass needs_llm_reprocessing() for an
            UNCHANGED file eligible for LLM-only reprocessing.
        course_label: display-only label for progress print lines (a real
            course name, or "_ungrouped").

    Returns:
        A _FileOutcome with the increments this file contributes to the
        run's overall RunSummary.
    """
    filename = Path(result.source_path).name

    if result.classification in _ACTIONABLE_CLASSIFICATIONS:
        print(
            f"[{course_label}] {filename}: processing "
            f"({result.classification.value})..."
        )
        try:
            process_result = process_pdf(result.source_path, cache_dir, client=mathpix_client)
        except _PROCESS_PDF_FAILURE_EXCEPTIONS as exc:
            print(f"[{course_label}] {filename}: FAILED: {exc}")
            upsert_entry(
                conn,
                result.source_path,
                source_hash=result.source_hash,
                source_mtime=result.source_mtime,
                source_size=result.source_size,
                mathpix_status="failed",
            )
            return _FileOutcome(errors=1)

        lecture_stem = Path(result.source_path).stem
        llm_result = cleanup_pdf(
            process_result.markdown_path,
            cache_dir,
            lecture_stem,
            llm_config,
            client=llm_client,
        )

        if llm_result.llm_status == "success":
            print(f"[{course_label}] {filename}: done (LLM cleanup succeeded)")
        else:
            print(f"[{course_label}] {filename}: done (LLM cleanup fell back to raw output)")

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
            llm_model=llm_result.llm_model,
            llm_prompt_version=llm_result.llm_prompt_version,
            llm_status=llm_result.llm_status,
            llm_validation_result=llm_result.llm_validation_result,
            llm_processed_at=llm_result.processed_at,
            output_path=str(llm_result.output_path),
        )
        errors = 0 if llm_result.llm_status == "success" else 1
        return _FileOutcome(processed=1, errors=errors)

    # Classification.UNCHANGED from here on.
    entry = get_entry(conn, result.source_path)
    if entry is None or entry.mathpix_status != "success":
        return _FileOutcome(skipped=1)

    if not (force_llm or needs_llm_reprocessing(entry)):
        return _FileOutcome(skipped=1)

    lecture_stem = Path(result.source_path).stem
    mathpix_markdown_path = cache_dir / f"{lecture_stem}.mathpix.md"

    print(f"[{course_label}] {filename}: reprocessing LLM stage only...")
    try:
        llm_result = cleanup_pdf(
            mathpix_markdown_path,
            cache_dir,
            lecture_stem,
            llm_config,
            client=llm_client,
        )
    except FileNotFoundError as exc:
        # Cached .mathpix.md unexpectedly missing -- a per-file filesystem
        # hiccup (e.g. _cache manually cleared), not a global config error.
        # Record it and continue rather than aborting the whole run.
        print(f"[{course_label}] {filename}: LLM stage FAILED: {exc}")
        return _FileOutcome(errors=1)
    # LLMError from a missing prompts/{prompt_version}.txt is deliberately
    # NOT caught here -- a missing configured prompt file affects every
    # file in this run identically, so it propagates and aborts the run
    # rather than silently degrading N files in a row (see AGENTS.md).

    if llm_result.llm_status == "success":
        print(f"[{course_label}] {filename}: LLM cleanup succeeded")
    else:
        print(f"[{course_label}] {filename}: LLM cleanup fell back to raw output")

    upsert_entry(
        conn,
        result.source_path,
        llm_model=llm_result.llm_model,
        llm_prompt_version=llm_result.llm_prompt_version,
        llm_status=llm_result.llm_status,
        llm_validation_result=llm_result.llm_validation_result,
        llm_processed_at=llm_result.processed_at,
        output_path=str(llm_result.output_path),
    )
    errors = 0 if llm_result.llm_status == "success" else 1
    return _FileOutcome(llm_reprocessed=1, errors=errors)


def run(
    paths_config: PathsConfig,
    conn: sqlite3.Connection,
    client: MathpixClient | None = None,
    llm_config: LLMConfig | None = None,
    force_llm: bool = False,
    target_source_path: str | Path | None = None,
) -> RunSummary:
    """
    Discover new/changed/failed-retry PDFs under paths_config.input_root (or,
    if target_source_path is given, classify just that one file), process
    each through process_pdf() + cleanup_pdf(), and record the outcome to
    state.db.

    Args:
        paths_config: resolved paths (input_root/cache_dir/state_db) --
            only input_root/cache_dir are used here; conn is expected to
            already be open against paths_config.state_db.
        conn: an open sqlite3.Connection from state.init_db().
        client: an already-constructed MathpixClient to reuse across every
            file in the run (e.g. a respx-mocked one for tests). When
            omitted, one is built from load_mathpix_credentials() and
            closed at the end of the run.
        llm_config: the LLMConfig to use for the LLM cleanup stage. When
            omitted, loaded via load_llm_config().
        force_llm: bypass needs_llm_reprocessing() for UNCHANGED files,
            reprocessing every eligible file's LLM stage regardless of its
            stored status/version. Infrastructure for a future
            --refresh-llm-prompt CLI flag (Phase 7) -- main() hardcodes
            False for now.
        target_source_path: when given, restrict the entire run to exactly
            this one PDF (resolved to an absolute path) instead of walking
            discover_pdfs() over all of input_root. Infrastructure for a
            future single-file rerun CLI flag (Phase 7) -- main() doesn't
            pass this yet.

    Returns:
        A RunSummary with processed/skipped/errors/ungrouped/llm_reprocessed
        counts.
    """
    owns_client = client is None
    if client is None:
        credentials = load_mathpix_credentials()
        client = MathpixClient(credentials.app_id, credentials.app_key)

    if llm_config is None:
        llm_config = load_llm_config()
    llm_client = LLMClient(model=llm_config.model)

    processed = 0
    skipped = 0
    errors = 0
    ungrouped = 0
    llm_reprocessed = 0

    try:
        if target_source_path is not None:
            resolved_target = Path(target_source_path).resolve()
            input_root = Path(paths_config.input_root).resolve()
            relative_parts = resolved_target.relative_to(input_root).parts
            course = relative_parts[0] if len(relative_parts) > 1 else UNGROUPED_COURSE_KEY

            result = classify_pdf(resolved_target, conn)

            if course == UNGROUPED_COURSE_KEY:
                cache_dir = paths_config.cache_dir / _UNGROUPED_CACHE_SUBDIR
                course_label = _UNGROUPED_CACHE_SUBDIR
            else:
                cache_dir = paths_config.cache_dir / course
                course_label = course

            outcome = _process_file(
                result,
                cache_dir,
                client,
                llm_client,
                llm_config,
                conn,
                force_llm,
                course_label,
            )
            processed += outcome.processed
            skipped += outcome.skipped
            errors += outcome.errors
            llm_reprocessed += outcome.llm_reprocessed
        else:
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
                    outcome = _process_file(
                        result,
                        cache_dir,
                        client,
                        llm_client,
                        llm_config,
                        conn,
                        force_llm,
                        course,
                    )
                    processed += outcome.processed
                    skipped += outcome.skipped
                    errors += outcome.errors
                    llm_reprocessed += outcome.llm_reprocessed
    finally:
        if owns_client:
            client.close()

    return RunSummary(
        processed=processed,
        skipped=skipped,
        errors=errors,
        ungrouped=ungrouped,
        llm_reprocessed=llm_reprocessed,
    )


def _print_summary(summary: RunSummary) -> None:
    print()
    print("Done.")
    print(f"  Processed:       {summary.processed}")
    print(f"  Skipped:         {summary.skipped}")
    print(f"  Errors:          {summary.errors}")
    print(f"  Ungrouped:       {summary.ungrouped}")
    print(f"  LLM reprocessed: {summary.llm_reprocessed}")


def main(argv: list[str] | None = None) -> int:
    """
    CLI entry point: load config.yaml's paths:, open/init state.db, run the
    discover -> process -> record pipeline once, print a summary.

    No flags yet (--dry-run/--force/--course/--verbose, and the eventual
    --refresh-llm-prompt/--file, are Phase 7) -- force_llm/target_source_path
    stay at their defaults (False/None). Hits the real, paid Mathpix and LLM
    APIs -- same caution as scripts/smoke_test_mathpix.py /
    scripts/smoke_test_llm.py.
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
