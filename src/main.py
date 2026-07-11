"""
Phase 2/3/5/6 orchestration entry point.

Wires discovery (src/discovery.py, issues #8/#9) + the state log
(src/state.py, issue #7) + Phase 1's process_pdf() (src/mathpix.py) + Phase
3's cleanup_pdf() (src/llm.py, issues #15-#17) + Phase 5's
write_lecture_note() (src/vault.py, issue #29) into a single runnable pass
over paths.input_root, with Phase 6's OutputConfig/NamingConfig (issue #37)
threaded through into write_lecture_note()'s dark_mode/tags/date_format/
lecture_prefix params. Phase 7 (issue #41) adds real argparse scaffolding
(src/cli.py's build_arg_parser()) and the first flag, --course NAME; issue
#42 adds --dry-run; issue #43 adds --force; issue #44 adds --rerun-llm and
--file PATH (thin CLI surfaces for the already-existing force_llm/
target_source_path params below -- no new pipeline logic); issue #45 adds
--force-vault-overwrite (issue #40's escape hatch for clearing a detected
vault_status="conflict"); issue #46 adds --no-llm (skip the LLM cleanup
stage entirely for the run). Issue #47 adds the Reporter abstraction
(src/reporting.py) every per-file print() now goes through -- an internal
run()/reporter= param only, with no CLI-facing flag yet. Issue #48 adds
--verbose/-v: main() constructs a PlainReporter(verbose=args.verbose)
directly and passes it as run()'s existing reporter= param, rather than
threading a new verbose param through run()/_process_file() themselves --
--verbose only changes what PlainReporter chooses to print. Issue #49 adds
RichReporter (a rich.live.Live progress table) plus a new
Reporter.on_discover() hook (called once by run(), right after discovery/
force-reclassification completes, before any per-file processing) so a
reporter can pre-populate a full picture of the run's scope up front with
an accurate initial per-file state -- see src/reporting.py's module
docstring for the full rationale. main() now selects between
RichReporter/PlainReporter via _select_reporter() (stdout is an
interactive TTY and rich is importable -> RichReporter; otherwise
PlainReporter) and wraps the run() call in `with reporter:` so a
RichReporter's Live display starts/stops cleanly.

Two entry points:
    - run(paths_config, conn, client=None, llm_config=None,
          output_config=None, naming_config=None, force_llm=False,
          target_source_path=None, course=None, dry_run=False,
          force=False, force_vault_overwrite=False,
          no_llm=False, reporter=None) -> RunSummary
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
      .mathpix.md -- no Mathpix API call -- immediately followed by another
      write_lecture_note() call (issue #31), since a reprocessed LLM stage
      produces new content that needs to reach the vault too. Otherwise
      fully skipped (no LLM call, no vault rewrite). Tallied as "skipped" or
      "llm_reprocessed" respectively.
    - Classification.NEW / CHANGED / RETRY: process_pdf() is called against
      a per-course cache_dir (paths.cache_dir / course, mirroring the
      vault's per-course structure at paths.vault_root / course). On
      success, cleanup_pdf() is immediately run against the
      freshly-produced .mathpix.md (same cache_dir), followed by
      write_lecture_note() (issue #31) against whichever path
      cleanup_pdf()'s LLMResult.output_path points at (the .llm.md on
      success, or the raw .mathpix.md fallback), and upsert_entry() records
      the mathpix_*/llm_*/output_path fields and the vault_*/
      vault_written_at fields in one go. On MathpixError /
      httpx.HTTPStatusError / FileNotFoundError, upsert_entry() instead
      records mathpix_status="failed" (still refreshing hash/mtime/size so
      tier-1 change detection is correct next run), neither the LLM nor
      vault-writing stage is ever attempted for that file this run, and the
      run continues to the next file -- one bad file never aborts the whole
      run (per docs/spec.md's Error Handling table). A write_lecture_note()
      failure (PostprocessError from an unparseable filename, or any
      OSError) is likewise caught per-file: only vault_status="failed" is
      recorded, leaving that file's already-successful
      mathpix_status/llm_status/output_path completely untouched (a
      correction to docs/spec.md's original wording -- see AGENTS.md).
      Separately, if the vault file was manually edited since our last
      write (detected via state.db's vault_content_hash, issue #40), the
      write is skipped rather than failed: vault_status="conflict" is
      recorded and vault_path/vault_written_at/vault_content_hash are left
      untouched (they still correctly describe the last file we actually
      wrote). This is expected, handled behavior, not an error -- tallied
      separately as RunSummary.vault_conflicts, not folded into errors.
      force_vault_overwrite=True (issue #45) bypasses this detection
      entirely -- see its own bullet below. no_llm=True (issue #46) skips
      cleanup_pdf() entirely on this path -- see its own bullet below for
      the full behavior.
    - UNGROUPED_COURSE_KEY files (PDFs directly under input_root, not in any
      course subfolder) are, in the normal (non-target_source_path) run,
      deliberately *not* processed: there's no course to mirror in
      cache_dir, so each one is logged as a warning and left out of
      state.db entirely, tallied separately as RunSummary.ungrouped rather
      than folded into processed/skipped/errors. The one exception is the
      target_source_path branch below, which force-processes an ungrouped
      target file anyway (see its own docstring note).
    - target_source_path (issue #18 infra, wired to --file since issue #44):
      when given,
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
    - force_llm=True (issue #18 infra, wired to --rerun-llm since issue
      #44): bypasses
      needs_llm_reprocessing() for UNCHANGED files, reprocessing every
      eligible file's LLM stage with the currently configured
      llm.prompt_version regardless of its stored status/version. Never
      triggered automatically by a stale llm_prompt_version -- see
      AGENTS.md's "Deliberate correction to docs/spec.md's Reprocessing
      logic table".
    - dry_run=True (issue #42, wired to --dry-run): every file is still
      classified via discover_pdfs()/classify_pdf() (or, with
      target_source_path, classify_pdf() on just that file) so accurate
      would-be classifications/reprocessing decisions can be reported, but
      _process_file() short-circuits before any real work: process_pdf(),
      cleanup_pdf(), _write_to_vault(), and upsert_entry() are never
      called for that file. No MathpixClient is constructed at all when
      one isn't injected (no Mathpix credentials required), and no
      LLMClient is constructed either. RunSummary's processed/skipped/
      llm_reprocessed counts reflect what *would* happen; errors/
      vault_conflicts/token/cost/page fields stay at 0 since no real work
      is attempted.
    - force=True (issue #43, wired to --force): reprocess every discovered
      file's Mathpix + LLM stages regardless of state.db's classification
      -- an UNCHANGED (or UNCHANGED-and-stale) result is reclassified to
      Classification.RETRY (via dataclasses.replace(), see
      _apply_force()) immediately before dispatch to _process_file(), so
      it's routed through the same actionable NEW/CHANGED/RETRY branch as
      any other reprocessed file, with zero changes needed to
      _process_file()'s existing branch structure. force=True implies a
      fresh LLM pass too (no separate force_llm=True needed): the
      actionable branch calls cleanup_pdf() unconditionally regardless of
      force_llm, which only ever gates the *separate* UNCHANGED-only LLM-
      rerun path that force=True bypasses entirely by reclassifying to
      RETRY first. Composes freely with course (filtering happens before
      reclassification) and with dry_run (an UNCHANGED file forced this
      way is reported as "would process" rather than "would reprocess LLM
      stage only", since the short-circuit in _process_file() also
      branches off the already-reclassified result).
    - force_vault_overwrite=True (issue #45, wired to
      --force-vault-overwrite): bypasses issue #40's manually-edited-vault-
      note conflict detection for every file this run -- a file that would
      otherwise be recorded as vault_status="conflict" is instead
      overwritten unconditionally with the pipeline's version, forwarded
      straight through _write_to_vault() into write_lecture_note()'s own
      force_overwrite param (see src/vault.py). A forced write always sets
      written=True, so RunSummary.vault_conflicts never counts a
      force-overwritten file -- there's no code branch left for it to hit.
      Deliberately a blunt, whole-run instrument: there is no way to force
      just one conflicted file while leaving other conflicts (if any)
      alone this run -- a possible future refinement, not in this issue's
      scope. Composes with both _write_to_vault() call sites (the
      actionable NEW/CHANGED/RETRY path and the UNCHANGED LLM-only-rerun
      path), since a conflict can be detected on either one.
    - no_llm=True (issue #46, wired to --no-llm): skips cleanup_pdf()
      entirely for this run. On the actionable NEW/CHANGED/RETRY path,
      process_pdf() still runs as normal, but only mathpix_*/figure_count/
      page_count/mathpix_processed_at are upserted -- llm_status and every
      other llm_*/output_path field are *explicitly* reset to None (issue
      #52 fix; previously these were merely omitted from the upsert, which
      left a prior genuine LLM success's stale data untouched when this
      branch was reached for an already-processed file -- see #52), so
      needs_llm_reprocessing() (llm_status is None) automatically routes
      this file through a real LLM pass on a later normal (non-no_llm)
      run, with no extra state to track, whether the file is genuinely
      NEW or was already LLM-processed before this no_llm reprocess. The
      vault note is still written this run (there's always something to
      write), sourced directly from process_pdf()'s raw
      ProcessResult.markdown_path (the same raw-.mathpix.md fallback
      content cleanup_pdf() itself would use on an LLM failure) -- this is
      tallied as processed=1 with no error contribution from skipping the
      LLM stage (skipping by explicit request isn't a failure, unlike a
      genuine LLM fallback-to-raw). On the UNCHANGED path, no_llm=True
      unconditionally skips the LLM-only-rerun branch regardless of
      force_llm/needs_llm_reprocessing() -- there is nothing for --no-llm
      to do to a file whose Mathpix stage is already cached and whose LLM
      stage isn't being run this pass anyway, so it's simply tallied as
      skipped (falling through to the existing force_vault_overwrite
      conflict-retry check first, same as any other skip).

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

RunSummary also aggregates this run's LLM token usage / cost estimate
(total_input_tokens/total_output_tokens/total_cost_estimate -- issue #21),
summed from each LLMResult's llm_input_tokens/llm_output_tokens/
llm_cost_estimate (a None from any individual LLMResult contributes 0 to
the running total rather than breaking accumulation).

RunSummary also aggregates total_pages_processed (issue #22) -- a per-run
total of pages actually OCR'd this run, summed only from files classified
NEW/CHANGED/RETRY that succeeded (ProcessResult.page_count, or 0 if
unexpectedly None). Skipped/unchanged files are not re-counted, since
their pages were already tallied in whichever prior run actually
processed them -- this avoids double-counting across runs.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import httpx

from src.cli import build_arg_parser
from src.config import (
    ConfigError,
    LLMConfig,
    NamingConfig,
    OutputConfig,
    PathsConfig,
    load_llm_config,
    load_mathpix_credentials,
    load_naming_config,
    load_output_config,
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
from src.postprocess import PostprocessError, resolve_tags
from src.reporting import PlainReporter, Reporter
from src.state import StateEntry, get_entry, init_db, upsert_entry
from src.vault import write_lecture_note

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


def _apply_force(result: ClassificationResult, force: bool) -> ClassificationResult:
    """
    Issue #43: when force=True, reclassify an UNCHANGED result as RETRY so
    it's routed through _process_file()'s actionable NEW/CHANGED/RETRY
    branch instead of being skipped or considered for LLM-only
    reprocessing -- see run()'s docstring for the full rationale (this is
    deliberately the only change needed; _process_file() itself is
    untouched). NEW/CHANGED/RETRY results are already actionable and are
    returned unchanged regardless of force.
    """
    if force and result.classification == Classification.UNCHANGED:
        return replace(result, classification=Classification.RETRY)
    return result


def _needs_vault_conflict_retry(entry: StateEntry | None, force_vault_overwrite: bool) -> bool:
    """
    Issue #45 follow-up: whether an UNCHANGED file whose Mathpix/LLM stages
    are both already successful (so neither needs_llm_reprocessing() nor
    force_llm would trigger any reprocessing) still needs its vault-write
    stage retried this run.

    Without this check, such a file is fully skipped by _process_file()
    before it ever reaches _write_to_vault() again -- so
    force_vault_overwrite=True alone could never take effect for a file
    whose *previous* run already reprocessed it but had its vault write
    skipped as a conflict (issue #40): state.db already shows
    mathpix_status="success"/llm_status="success" from that previous run,
    making it ineligible for LLM-only reprocessing on every subsequent run,
    with nothing else short of a real source-file change (or --force) ever
    routing it back through a branch that calls _write_to_vault() again.

    True only when force_vault_overwrite is True, entry exists, its
    vault_status is exactly "conflict" (not "failed" or "success" -- an
    unrelated vault failure, e.g. a bad filename, isn't something
    force_overwrite can fix, and there's nothing to retry if it already
    succeeded), and it has both a cached output_path and llm_processed_at
    to reuse as _write_to_vault()'s content_source_path/processed_at
    (skipping cleanup_pdf() entirely, since the LLM stage itself doesn't
    need to rerun -- only the vault write does).
    """
    return (
        force_vault_overwrite
        and entry is not None
        and entry.vault_status == "conflict"
        and entry.output_path is not None
        and entry.llm_processed_at is not None
    )


@dataclass(frozen=True)
class RunSummary:
    """Basic end-of-run counts (full polish/formatting is Phase 7)."""

    processed: int
    skipped: int
    errors: int
    ungrouped: int
    llm_reprocessed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_estimate: float = 0.0
    total_pages_processed: int = 0
    vault_conflicts: int = 0


@dataclass(frozen=True)
class _FileOutcome:
    """Per-file tally increments returned by _process_file(), accumulated
    by run() into its final RunSummary."""

    processed: int = 0
    skipped: int = 0
    errors: int = 0
    llm_reprocessed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate: float = 0.0
    pages: int = 0
    vault_conflicts: int = 0


def _write_to_vault(
    conn: sqlite3.Connection,
    source_path: str,
    content_source_path: Path,
    course_cache_figures_dir: Path,
    vault_course_dir: Path,
    source_mtime: float,
    processed_at: datetime,
    dark_mode: bool,
    tags: tuple[str, ...],
    date_format: str,
    lecture_prefix: str,
    force_overwrite: bool,
    reporter: Reporter,
) -> tuple[int, int]:
    """
    Write this file's final vault Markdown note (src/vault.py's
    write_lecture_note(), issue #29) and record the outcome to state.db.

    Args:
        source_path: the source PDF's path (state.db's primary key) --
            also passed to write_lecture_note() to derive lecture
            number/course name.
        content_source_path: the content to write -- whichever path
            cleanup_pdf()'s LLMResult.output_path points at (the
            LLM-cleaned .llm.md on success, or the raw .mathpix.md
            fallback if the LLM stage failed -- no extra branching needed
            here, see issue #31).
        course_cache_figures_dir: this course's cached figures/ dir,
            passed straight through to write_lecture_note().
        vault_course_dir: this course's vault directory, passed straight
            through to write_lecture_note().
        source_mtime: forwarded to write_lecture_note()'s date field.
        processed_at: forwarded to write_lecture_note()'s processed field
            and, on success, recorded verbatim as state.db's
            vault_written_at -- reuses the same timestamp as this file's
            llm_processed_at rather than taking a fresh one.
        dark_mode: forwarded to write_lecture_note()'s dark_mode param --
            the caller (_process_file()) resolves this from
            OutputConfig.figures_dark_mode_flag (issue #37).
        tags: forwarded to write_lecture_note()'s tags param -- the caller
            resolves this via src/postprocess.py's resolve_tags(course_label,
            output_config) (issue #37); empty if this course has no
            explicit output.course_tags entry.
        date_format: forwarded to write_lecture_note()'s date_format param
            -- the caller resolves this from OutputConfig.date_format
            (issue #37).
        lecture_prefix: forwarded to write_lecture_note()'s lecture_prefix
            param (used for both the output filename and the frontmatter
            title) -- the caller resolves this from
            NamingConfig.lecture_prefix (issue #37).
        force_overwrite: forwarded verbatim to write_lecture_note()'s
            force_overwrite param (issue #45) -- the caller resolves this
            from run()'s force_vault_overwrite param. When True, bypasses
            the previous_content_hash conflict check below entirely, so
            this call can never return a (0, 1) conflict tuple.
        reporter: issue #47 -- every print() this function used to call
            directly now goes through reporter.on_stage(source_path, ...)
            instead, passing the exact same final message text (never
            None; the caller always supplies a concrete Reporter, defaulting
            to PlainReporter() at run()'s top level). Issue #48 also wires
            two verbose-only reporter.on_detail(source_path, ...) calls:
            one per copied figure (write_lecture_note()'s on_figure_copy
            callback), and one post-write confirmation summary (output
            path, figure count, delimiter-warning count) after a
            successful write -- both no-ops unless the injected reporter
            is a PlainReporter(verbose=True) (or an equivalent).

    Returns:
        A (errors, conflicts) tuple -- each 1 or 0, never both 1 at once.
        errors is 1 if the vault write failed (counts toward the caller's
        _FileOutcome.errors); conflicts is 1 if a manually-edited vault
        note was detected and the write was skipped (counts toward the
        caller's _FileOutcome.vault_conflicts, issue #40) -- this is
        expected, handled behavior, not an error.

    On a PostprocessError (source_path's filename doesn't match
    lecture[_-]?<digits>) or any OSError (I/O failure), the write is
    caught per-file: logged, and only vault_status="failed" is recorded --
    this file's already-recorded mathpix_status/llm_status/output_path
    fields from the same call are left completely untouched (confirmed
    correction to docs/spec.md's original "skip file, no state log entry"
    wording -- see issue #27/#31's notes). Any
    scan_delimiter_issues() warnings on a successful write are printed but
    never affect vault_status -- diagnostic only, per issue #28's design.

    Before writing, the existing state.db entry's vault_content_hash (if
    any) is fetched and passed to write_lecture_note() as
    previous_content_hash, so it can detect a manually-edited vault note
    (issue #40). If write_lecture_note() reports written=False (a conflict
    was detected), the write is skipped entirely: a warning identifying
    both the vault file and its corresponding _cache/ content is printed,
    and only vault_status="conflict" is recorded -- vault_path/
    vault_written_at/vault_content_hash are left untouched, since they
    still correctly describe the last file this pipeline actually wrote.
    force_overwrite=True (issue #45) bypasses this detection entirely --
    write_lecture_note() then always reports written=True, so this
    function always falls through to the normal success-path upsert below
    instead, clearing any previously-recorded vault_status="conflict".
    """
    entry = get_entry(conn, source_path)
    previous_content_hash = entry.vault_content_hash if entry is not None else None

    def _on_figure_copy(dest_path: Path) -> None:
        reporter.on_detail(source_path, f"copied figure: {dest_path.name}")

    try:
        result = write_lecture_note(
            source_path,
            content_source_path,
            course_cache_figures_dir,
            vault_course_dir,
            source_mtime,
            processed_at,
            dark_mode=dark_mode,
            tags=list(tags),
            date_format=date_format,
            lecture_prefix=lecture_prefix,
            previous_content_hash=previous_content_hash,
            force_overwrite=force_overwrite,
            on_figure_copy=_on_figure_copy,
        )
    except (PostprocessError, OSError) as exc:
        reporter.on_stage(source_path, f"vault write FAILED: {exc}")
        upsert_entry(conn, source_path, vault_status="failed")
        return 1, 0

    if not result.written:
        reporter.on_stage(
            source_path,
            f"WARNING: vault note "
            f"{result.output_path} was manually edited since the last "
            f"pipeline write -- skipping overwrite. Diff it against the "
            f"reprocessed content at {content_source_path} to merge "
            f"manually.",
        )
        upsert_entry(conn, source_path, vault_status="conflict")
        return 0, 1

    for warning in result.delimiter_warnings:
        reporter.on_stage(source_path, f"WARNING: {warning}")

    reporter.on_detail(
        source_path,
        f"vault write confirmed: {result.output_path} "
        f"({len(result.figures_copied)} figure(s), "
        f"{len(result.delimiter_warnings)} delimiter warning(s))",
    )

    upsert_entry(
        conn,
        source_path,
        vault_status="success",
        vault_path=str(result.output_path),
        vault_written_at=processed_at,
        vault_content_hash=result.content_hash,
    )
    return 0, 0


def _process_file(
    result: ClassificationResult,
    cache_dir: Path,
    vault_course_dir: Path,
    mathpix_client: MathpixClient | None,
    llm_client: LLMClient | None,
    llm_config: LLMConfig,
    output_config: OutputConfig,
    naming_config: NamingConfig,
    conn: sqlite3.Connection,
    force_llm: bool,
    dry_run: bool,
    force_vault_overwrite: bool,
    no_llm: bool,
    course_label: str,
    reporter: Reporter,
) -> _FileOutcome:
    """
    Shared per-file processing body: Mathpix-stage handling, LLM-stage
    handling, vault-writing (issue #31), and the upsert_entry() calls that
    record their outcomes. Used identically by run()'s normal per-course
    loop and its target_source_path branch, so the two entry points are
    guaranteed to behave the same way.

    Args:
        result: a discovery.ClassificationResult for this file.
        cache_dir: the (course-specific, or _ungrouped-sentinel) cache
            directory to pass through as both process_pdf()'s and
            cleanup_pdf()'s dest_dir. This course's cached figures/ dir
            (cache_dir / "figures") is passed to write_lecture_note() too.
        vault_course_dir: the (course-specific, or _ungrouped-sentinel)
            vault directory to pass through to write_lecture_note() as its
            vault_course_dir.
        mathpix_client: the run's shared MathpixClient, or None in
            dry_run mode (never touched by the dry-run short-circuit
            below).
        llm_client: the run's shared LLMClient, or None in dry_run mode
            (same as mathpix_client).
        llm_config: the run's resolved LLMConfig.
        output_config: the run's resolved OutputConfig (issue #37) --
            figures_dark_mode_flag/date_format are forwarded to every
            _write_to_vault() call verbatim; course_tags is resolved per
            call via src/postprocess.py's resolve_tags(course_label,
            output_config), since course_label is already the raw course
            folder name (or the "_ungrouped" sentinel, which naturally
            resolves to no tags -- no config ever has an entry for it).
        naming_config: the run's resolved NamingConfig (issue #37) --
            lecture_prefix is forwarded to every _write_to_vault() call
            verbatim.
        conn: an open state.db connection.
        force_llm: whether to bypass needs_llm_reprocessing() for an
            UNCHANGED file eligible for LLM-only reprocessing.
        dry_run: issue #42 -- when True, short-circuits before any of the
            real processing below: no process_pdf()/cleanup_pdf()/
            _write_to_vault()/upsert_entry() call is ever made for this
            file. The short-circuit mirrors the real branching logic
            immediately below it (actionable classification -> "would
            process"; UNCHANGED-and-stale-or-force_llm -> "would
            reprocess LLM stage only"; otherwise -> skip) purely from
            result/state.db's already-recorded entry, so it reports
            accurate would-be counts without doing any real work --
            errors/vault_conflicts always stay 0 in dry-run since nothing
            can fail.
        force_vault_overwrite: issue #45 -- forwarded verbatim to every
            _write_to_vault() call's force_overwrite param, bypassing
            issue #40's manually-edited-vault-note conflict check for this
            file. A forced write always succeeds (barring a real I/O
            error), so it can never contribute to the returned
            _FileOutcome.vault_conflicts. Also consulted (via
            _needs_vault_conflict_retry()) on the UNCHANGED path below when
            neither force_llm nor needs_llm_reprocessing() would otherwise
            trigger any work: if this file's last vault-write attempt was
            recorded as a conflict, the vault write alone is retried
            (reusing the cached LLM output, no cleanup_pdf() call) rather
            than the file being fully skipped -- otherwise
            force_vault_overwrite could never take effect for a file whose
            Mathpix/LLM stages already both succeeded in a prior run.
        no_llm: issue #46 -- when True, skips cleanup_pdf() entirely on
            the actionable NEW/CHANGED/RETRY path below: only
            mathpix_*/figure_count/page_count/mathpix_processed_at are
            upserted, and llm_status and every other llm_*/output_path
            field are explicitly reset to None (issue #52 fix -- a no-op
            for a genuinely fresh file, since those columns are already
            NULL, but correctly clears a prior genuine LLM success's now-
            stale data when this branch is reached for an already-
            processed file, e.g. via a real second source edit or
            --force), and the vault note is written straight from
            process_pdf()'s raw ProcessResult.markdown_path -- no error is
            contributed just for skipping the LLM stage by request. On the
            UNCHANGED path, no_llm=True unconditionally skips the LLM-
            only-rerun branch regardless of force_llm/
            needs_llm_reprocessing() -- there's nothing for --no-llm to do
            there, so the file is simply tallied as skipped (falling
            through to the existing force_vault_overwrite conflict-retry
            check first, same as any other skip).
        course_label: display-only label for progress print lines (a real
            course name, or "_ungrouped") -- doubles as resolve_tags()'s
            course_name lookup key (issue #37).
        reporter: issue #47 -- every print() this function used to call
            directly now goes through reporter.on_stage(source_path, ...)
            instead (never None; run() always supplies a concrete Reporter,
            defaulting to PlainReporter()). process_pdf()'s on_status hook
            and cleanup_pdf()'s new equivalent are both wired here to call
            reporter.on_detail(source_path, ...) -- verbose-only, a no-op
            in PlainReporter today.

    Returns:
        A _FileOutcome with the increments this file contributes to the
        run's overall RunSummary.
    """
    if dry_run:
        if result.classification in _ACTIONABLE_CLASSIFICATIONS:
            reporter.on_stage(
                result.source_path, f"would_process:{result.classification.value}"
            )
            return _FileOutcome(processed=1)

        entry = get_entry(conn, result.source_path)
        if entry is None or entry.mathpix_status != "success":
            return _FileOutcome(skipped=1)

        if not no_llm and (force_llm or needs_llm_reprocessing(entry)):
            reporter.on_stage(result.source_path, "would_reprocess_llm")
            return _FileOutcome(llm_reprocessed=1)

        if _needs_vault_conflict_retry(entry, force_vault_overwrite):
            reporter.on_stage(result.source_path, "would_retry_vault")
            return _FileOutcome(skipped=1)

        return _FileOutcome(skipped=1)

    tags = resolve_tags(course_label, output_config)

    def _mathpix_on_status(
        stage: str, attempt: int, max_attempts: int, status: str | None, payload: dict
    ) -> None:
        reporter.on_detail(
            result.source_path,
            f"mathpix {stage}: poll {attempt}/{max_attempts} status={status}",
        )

    def _llm_on_status(message: str) -> None:
        reporter.on_detail(result.source_path, message)

    if result.classification in _ACTIONABLE_CLASSIFICATIONS:
        reporter.on_stage(result.source_path, f"submitting:{result.classification.value}")
        try:
            process_result = process_pdf(
                result.source_path,
                cache_dir,
                client=mathpix_client,
                on_status=_mathpix_on_status,
            )
        except _PROCESS_PDF_FAILURE_EXCEPTIONS as exc:
            reporter.on_stage(result.source_path, f"FAILED: {exc}")
            upsert_entry(
                conn,
                result.source_path,
                source_hash=result.source_hash,
                source_mtime=result.source_mtime,
                source_size=result.source_size,
                mathpix_status="failed",
            )
            return _FileOutcome(errors=1)

        if no_llm:
            # Issue #46: skip cleanup_pdf() entirely -- only the
            # mathpix_*/figure_count/page_count/mathpix_processed_at
            # fields are upserted. Issue #52 fix: every llm_*/output_path
            # field is *explicitly* reset to None here (not simply
            # omitted) -- for a freshly-discovered file these columns are
            # already NULL, so this is a no-op there, but for a file that
            # already had a genuine prior LLM success (a real second edit
            # producing a CHANGED reclassification, or --force
            # reclassifying an UNCHANGED file to RETRY) an *omitted*
            # upsert would leave that old "success" data stale and
            # untouched even though it now describes content from before
            # this run's fresh Mathpix reprocess -- see issue #52's real-
            # data findings (linked from #51's validation comment) for the
            # concretely observed consequence: state.db kept claiming
            # llm_status="success" while the vault note was silently
            # overwritten with fresh raw (uncleaned) OCR text, and
            # needs_llm_reprocessing() then permanently refused to ever
            # auto-pick the file up for a real LLM pass again. Explicitly
            # nulling these columns here instead makes this upsert
            # equivalent to a freshly-discovered file's in every case,
            # restoring needs_llm_reprocessing()'s correct "pick this up
            # on the next normal (non-no_llm) run" behavior uniformly.
            reporter.on_stage(result.source_path, "done:no_llm")
            upsert_entry(
                conn,
                result.source_path,
                source_hash=result.source_hash,
                source_mtime=result.source_mtime,
                source_size=result.source_size,
                mathpix_status="success",
                mathpix_pdf_id=process_result.pdf_id,
                figure_count=process_result.figure_count,
                page_count=process_result.page_count,
                mathpix_processed_at=process_result.processed_at,
                llm_model=None,
                llm_prompt_version=None,
                llm_status=None,
                llm_validation_result=None,
                llm_processed_at=None,
                output_path=None,
                llm_input_tokens=None,
                llm_output_tokens=None,
                llm_cost_estimate=None,
            )
            # Vault-writing still needs a content source -- reuse the raw
            # .mathpix.md, the same fallback content cleanup_pdf() itself
            # would use on an LLM failure (see LLMResult's docstring).
            vault_errors, vault_conflicts = _write_to_vault(
                conn,
                result.source_path,
                process_result.markdown_path,
                cache_dir / "figures",
                vault_course_dir,
                result.source_mtime,
                process_result.processed_at,
                output_config.figures_dark_mode_flag,
                tags,
                output_config.date_format,
                naming_config.lecture_prefix,
                force_vault_overwrite,
                reporter,
            )
            return _FileOutcome(
                processed=1,
                errors=vault_errors,
                pages=process_result.page_count or 0,
                vault_conflicts=vault_conflicts,
            )

        lecture_stem = Path(result.source_path).stem
        llm_result = cleanup_pdf(
            process_result.markdown_path,
            cache_dir,
            lecture_stem,
            llm_config,
            client=llm_client,
            on_status=_llm_on_status,
        )

        if llm_result.llm_status == "success":
            reporter.on_stage(result.source_path, "done:llm_success")
        else:
            reporter.on_stage(result.source_path, "done:llm_fallback")

        upsert_entry(
            conn,
            result.source_path,
            source_hash=result.source_hash,
            source_mtime=result.source_mtime,
            source_size=result.source_size,
            mathpix_status="success",
            mathpix_pdf_id=process_result.pdf_id,
            figure_count=process_result.figure_count,
            page_count=process_result.page_count,
            mathpix_processed_at=process_result.processed_at,
            llm_model=llm_result.llm_model,
            llm_prompt_version=llm_result.llm_prompt_version,
            llm_status=llm_result.llm_status,
            llm_validation_result=llm_result.llm_validation_result,
            llm_processed_at=llm_result.processed_at,
            output_path=str(llm_result.output_path),
            llm_input_tokens=llm_result.llm_input_tokens,
            llm_output_tokens=llm_result.llm_output_tokens,
            llm_cost_estimate=llm_result.llm_cost_estimate,
        )
        errors = 0 if llm_result.llm_status == "success" else 1
        vault_errors, vault_conflicts = _write_to_vault(
            conn,
            result.source_path,
            llm_result.output_path,
            cache_dir / "figures",
            vault_course_dir,
            result.source_mtime,
            llm_result.processed_at,
            output_config.figures_dark_mode_flag,
            tags,
            output_config.date_format,
            naming_config.lecture_prefix,
            force_vault_overwrite,
            reporter,
        )
        errors += vault_errors
        return _FileOutcome(
            processed=1,
            errors=errors,
            input_tokens=llm_result.llm_input_tokens or 0,
            output_tokens=llm_result.llm_output_tokens or 0,
            cost_estimate=llm_result.llm_cost_estimate or 0.0,
            pages=process_result.page_count or 0,
            vault_conflicts=vault_conflicts,
        )

    # Classification.UNCHANGED from here on.
    entry = get_entry(conn, result.source_path)
    if entry is None or entry.mathpix_status != "success":
        return _FileOutcome(skipped=1)

    if no_llm or not (force_llm or needs_llm_reprocessing(entry)):
        # Issue #46: no_llm=True unconditionally skips the LLM-only-rerun
        # branch below, regardless of force_llm/needs_llm_reprocessing() --
        # there's nothing for --no-llm to do to a file whose Mathpix stage
        # is already cached and whose LLM stage isn't being run this pass
        # anyway.
        if _needs_vault_conflict_retry(entry, force_vault_overwrite):
            # Issue #45 follow-up: neither force_llm nor
            # needs_llm_reprocessing() applies (this file's LLM stage
            # already succeeded), but force_vault_overwrite=True and its
            # last vault-write attempt was recorded as a conflict -- retry
            # just the vault write, reusing the already-cached LLM output
            # (entry.output_path) rather than calling cleanup_pdf() again.
            reporter.on_stage(result.source_path, "retrying_vault_write")
            vault_errors, vault_conflicts = _write_to_vault(
                conn,
                result.source_path,
                Path(entry.output_path),
                cache_dir / "figures",
                vault_course_dir,
                result.source_mtime,
                entry.llm_processed_at,
                output_config.figures_dark_mode_flag,
                tags,
                output_config.date_format,
                naming_config.lecture_prefix,
                force_vault_overwrite,
                reporter,
            )
            return _FileOutcome(
                skipped=1,
                errors=vault_errors,
                vault_conflicts=vault_conflicts,
            )
        return _FileOutcome(skipped=1)

    lecture_stem = Path(result.source_path).stem
    mathpix_markdown_path = cache_dir / f"{lecture_stem}.mathpix.md"

    reporter.on_stage(result.source_path, "reprocessing_llm")
    try:
        llm_result = cleanup_pdf(
            mathpix_markdown_path,
            cache_dir,
            lecture_stem,
            llm_config,
            client=llm_client,
            on_status=_llm_on_status,
        )
    except FileNotFoundError as exc:
        # Cached .mathpix.md unexpectedly missing -- a per-file filesystem
        # hiccup (e.g. _cache manually cleared), not a global config error.
        # Record it and continue rather than aborting the whole run.
        reporter.on_stage(result.source_path, f"LLM stage FAILED: {exc}")
        return _FileOutcome(errors=1)
    # LLMError from a missing prompts/{prompt_version}.txt is deliberately
    # NOT caught here -- a missing configured prompt file affects every
    # file in this run identically, so it propagates and aborts the run
    # rather than silently degrading N files in a row (see AGENTS.md).

    if llm_result.llm_status == "success":
        reporter.on_stage(result.source_path, "llm_only:llm_success")
    else:
        reporter.on_stage(result.source_path, "llm_only:llm_fallback")

    upsert_entry(
        conn,
        result.source_path,
        llm_model=llm_result.llm_model,
        llm_prompt_version=llm_result.llm_prompt_version,
        llm_status=llm_result.llm_status,
        llm_validation_result=llm_result.llm_validation_result,
        llm_processed_at=llm_result.processed_at,
        output_path=str(llm_result.output_path),
        llm_input_tokens=llm_result.llm_input_tokens,
        llm_output_tokens=llm_result.llm_output_tokens,
        llm_cost_estimate=llm_result.llm_cost_estimate,
    )
    errors = 0 if llm_result.llm_status == "success" else 1
    vault_errors, vault_conflicts = _write_to_vault(
        conn,
        result.source_path,
        llm_result.output_path,
        cache_dir / "figures",
        vault_course_dir,
        result.source_mtime,
        llm_result.processed_at,
        output_config.figures_dark_mode_flag,
        tags,
        output_config.date_format,
        naming_config.lecture_prefix,
        force_vault_overwrite,
        reporter,
    )
    errors += vault_errors
    return _FileOutcome(
        llm_reprocessed=1,
        errors=errors,
        input_tokens=llm_result.llm_input_tokens or 0,
        output_tokens=llm_result.llm_output_tokens or 0,
        cost_estimate=llm_result.llm_cost_estimate or 0.0,
        vault_conflicts=vault_conflicts,
    )


def run(
    paths_config: PathsConfig,
    conn: sqlite3.Connection,
    client: MathpixClient | None = None,
    llm_config: LLMConfig | None = None,
    output_config: OutputConfig | None = None,
    naming_config: NamingConfig | None = None,
    force_llm: bool = False,
    target_source_path: str | Path | None = None,
    course: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    force_vault_overwrite: bool = False,
    no_llm: bool = False,
    reporter: Reporter | None = None,
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
        output_config: the OutputConfig (course_tags/date_format/
            figures_dark_mode_flag) to use for vault-writing (issue #37).
            When omitted, loaded via load_output_config().
        naming_config: the NamingConfig (lecture_prefix) to use for
            vault-writing (issue #37). When omitted, loaded via
            load_naming_config().
        force_llm: bypass needs_llm_reprocessing() for UNCHANGED files,
            reprocessing every eligible file's LLM stage regardless of its
            stored status/version. Wired to --rerun-llm (issue #44).
        target_source_path: when given, restrict the entire run to exactly
            this one PDF (resolved to an absolute path) instead of walking
            discover_pdfs() over all of input_root. Wired to --file (issue
            #44); main() validates the path exists, ends in .pdf
            (case-insensitive), and lives under paths.input_root before
            ever calling run() with it.
        course: when given, restrict this run to one course subdirectory of
            input_root (an exact, case-sensitive match against
            discover_pdfs()'s results_by_course key -- i.e. the raw course
            folder name). The full directory is still recursively scanned
            (discover_pdfs() has no way to scan a single course subdir
            alone) -- every course except the requested one is simply
            skipped from the outer loop, never classified/written to
            state.db. An unknown course name is a clean no-op (a warning is
            printed, RunSummary comes back all-zero) rather than raising.
            Mutually exclusive with target_source_path -- passing both
            raises ValueError (issue #41; the CLI-level --course/--file
            rejection is issue #44's build_arg_parser() mutually exclusive
            group, which makes this ValueError unreachable from the real
            CLI -- it remains as a direct-call-level guard for run()).
        dry_run: when True (issue #42, wired to --dry-run), report what
            would be processed without doing it -- no MathpixClient/
            LLMClient is constructed (when not injected), and
            _process_file() short-circuits before process_pdf()/
            cleanup_pdf()/_write_to_vault()/upsert_entry() for every file.
            Classification (discover_pdfs()/classify_pdf()) still runs
            normally, so the reported would-be counts are accurate.
        force: when True (issue #43, wired to --force), reprocess every
            discovered file's Mathpix + LLM stages regardless of
            state.db's classification -- an otherwise-UNCHANGED (including
            UNCHANGED-and-stale) result is reclassified to
            Classification.RETRY via _apply_force() immediately before
            being dispatched to _process_file(), so it's routed through
            the same actionable branch as any other reprocessed file with
            no change to _process_file() itself. This deliberately implies
            a fresh LLM pass too, without needing force_llm=True passed
            alongside it: the actionable branch always calls cleanup_pdf()
            unconditionally, regardless of force_llm (which only gates the
            separate UNCHANGED-only LLM-rerun path that force=True
            bypasses entirely). Composes freely with course/
            target_source_path (reclassification happens per-result,
            after any course filtering) and with dry_run (a forced
            UNCHANGED file is reported as "would process" rather than
            "would reprocess LLM stage only", since dry_run's
            short-circuit in _process_file() branches off the
            already-reclassified result too).
        force_vault_overwrite: when True (issue #45, wired to
            --force-vault-overwrite), bypass issue #40's manually-edited-
            vault-note conflict detection for every file this run -- a
            file that would otherwise be recorded as
            vault_status="conflict" is instead overwritten unconditionally
            with the pipeline's version, and any previously-recorded
            conflict is cleared back to vault_status="success" with a
            fresh vault_content_hash. Forwarded straight through
            _process_file() into every _write_to_vault() call's
            force_overwrite param -- see src/vault.py's
            write_lecture_note() for the actual bypass logic. Deliberately
            a blunt, whole-run instrument: there's no way to force-clear
            just one conflicted file while leaving others alone this run
            (a possible future refinement, out of scope here). Composes
            freely with course/target_source_path/dry_run/force -- none of
            them interact with vault-conflict detection. Critically, this
            also retries the vault write *alone* (no cleanup_pdf() call)
            for an UNCHANGED file whose Mathpix/LLM stages already both
            succeeded in a prior run but whose vault write was recorded as
            a conflict -- without this, such a file is fully skipped
            before ever reaching _write_to_vault() again on any later run,
            so force_vault_overwrite by itself would otherwise never take
            effect for it (see _process_file()'s
            _needs_vault_conflict_retry() check). Combining
            force_vault_overwrite with force_llm (or --rerun-llm) still
            works as before -- that reruns cleanup_pdf() too, same as any
            other forced LLM reprocessing.
        no_llm: when True (issue #46, wired to --no-llm), skip the LLM
            cleanup stage entirely for this run. On the actionable
            NEW/CHANGED/RETRY path, process_pdf() still runs as normal,
            but cleanup_pdf() is never called -- only mathpix_*/
            figure_count/page_count/mathpix_processed_at are upserted to
            state.db, leaving llm_status (and every other llm_*/
            output_path field) untouched/NULL, exactly like a freshly-
            discovered-but-not-yet-LLM-processed file. Since
            needs_llm_reprocessing() already treats llm_status is None as
            needing reprocessing, a later normal (non-no_llm) run
            automatically picks the file up for a real LLM pass -- no new
            state.db status value or extra bookkeeping needed. The vault
            note is still written this run, sourced directly from
            process_pdf()'s raw ProcessResult.markdown_path (the same
            raw-.mathpix.md fallback content cleanup_pdf() itself would
            use on an LLM failure); this never contributes an error just
            for skipping the LLM stage by request (unlike a genuine LLM
            fallback-to-raw, which does count as an error today). On the
            UNCHANGED path, no_llm=True unconditionally skips the
            LLM-only-rerun branch regardless of force_llm/
            needs_llm_reprocessing() -- there is nothing for --no-llm to
            do to a file whose Mathpix stage is already cached and whose
            LLM stage isn't being (re)run this pass anyway, so it's simply
            tallied as skipped (still composing with
            force_vault_overwrite's own conflict-retry check on that same
            path).
        reporter: issue #47 -- the Reporter every per-file progress/outcome
            line is routed through, instead of calling print() directly.
            When omitted (the default), a fresh PlainReporter() is
            constructed internally -- reporter is never None past this
            point, so _process_file()/_write_to_vault() never need a null
            check. The real CLI (main(), issue #49) always passes an
            explicit reporter built by _select_reporter(args.verbose)
            instead of relying on this default. Issue #49 also adds a
            single reporter.on_discover([(source_path, classification),
            ...]) call, made once right after discover_pdfs()/
            classify_pdf() and any force reclassification complete (in
            both the per-course loop and the target_source_path branch),
            before any per-file processing begins -- lets a reporter
            (RichReporter) pre-populate its full view of the run's scope
            up front. PlainReporter's on_discover is a no-op (zero output
            change). See src/reporting.py for both implementations' exact
            designs.

    Raises:
        ValueError: if both course and target_source_path are given.

    Returns:
        A RunSummary with processed/skipped/errors/ungrouped/llm_reprocessed
        counts (plus this run's aggregated LLM token/cost totals,
        total_pages_processed, and vault_conflicts -- issue #40's
        manually-edited-vault-note detections, tallied separately from
        errors since they're expected, handled behavior).
    """
    if course is not None and target_source_path is not None:
        raise ValueError(
            "course and target_source_path are mutually exclusive -- restrict "
            "the run to one course, or one exact file, not both"
        )

    if reporter is None:
        reporter = PlainReporter()

    owns_client = client is None
    if client is None and not dry_run:
        credentials = load_mathpix_credentials()
        client = MathpixClient(credentials.app_id, credentials.app_key)

    if llm_config is None:
        llm_config = load_llm_config()
    llm_client = None if dry_run else LLMClient(model=llm_config.model)

    if output_config is None:
        output_config = load_output_config()
    if naming_config is None:
        naming_config = load_naming_config()

    processed = 0
    skipped = 0
    errors = 0
    ungrouped = 0
    llm_reprocessed = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cost_estimate = 0.0
    total_pages_processed = 0
    vault_conflicts = 0

    try:
        if target_source_path is not None:
            resolved_target = Path(target_source_path).resolve()
            input_root = Path(paths_config.input_root).resolve()
            relative_parts = resolved_target.relative_to(input_root).parts
            course = relative_parts[0] if len(relative_parts) > 1 else UNGROUPED_COURSE_KEY

            result = _apply_force(classify_pdf(resolved_target, conn), force)
            reporter.on_discover([(result.source_path, result.classification.value)])

            if course == UNGROUPED_COURSE_KEY:
                cache_dir = paths_config.cache_dir / _UNGROUPED_CACHE_SUBDIR
                vault_course_dir = paths_config.vault_root / _UNGROUPED_CACHE_SUBDIR
                course_label = _UNGROUPED_CACHE_SUBDIR
            else:
                cache_dir = paths_config.cache_dir / course
                vault_course_dir = paths_config.vault_root / course
                course_label = course

            outcome = _process_file(
                result,
                cache_dir,
                vault_course_dir,
                client,
                llm_client,
                llm_config,
                output_config,
                naming_config,
                conn,
                force_llm,
                dry_run,
                force_vault_overwrite,
                no_llm,
                course_label,
                reporter,
            )
            processed += outcome.processed
            skipped += outcome.skipped
            errors += outcome.errors
            llm_reprocessed += outcome.llm_reprocessed
            total_input_tokens += outcome.input_tokens
            total_output_tokens += outcome.output_tokens
            total_cost_estimate += outcome.cost_estimate
            total_pages_processed += outcome.pages
            vault_conflicts += outcome.vault_conflicts
        else:
            results_by_course = discover_pdfs(paths_config.input_root, conn)

            if course is not None:
                if course not in results_by_course:
                    print(
                        f"WARNING: --course {course!r} not found under "
                        f"{paths_config.input_root} -- nothing to process."
                    )
                    results_by_course = {}
                else:
                    results_by_course = {course: results_by_course[course]}

            # Issue #49: apply force reclassification up front and flatten
            # into a single ordered (course_name, result) list before any
            # processing, so reporter.on_discover() can be called exactly
            # once with every file's final, about-to-be-processed
            # classification -- iteration order matches the original
            # nested-loop order (dict insertion order, then each course's
            # results list order), and _apply_force() is still only ever
            # computed once per file.
            flattened: list[tuple[str, ClassificationResult]] = []
            for course_name, results in results_by_course.items():
                if course_name == UNGROUPED_COURSE_KEY:
                    for result in results:
                        flattened.append((course_name, result))
                    continue
                for result in results:
                    flattened.append((course_name, _apply_force(result, force)))

            reporter.on_discover(
                [(result.source_path, result.classification.value) for _, result in flattened]
            )

            for course_name, result in flattened:
                if course_name == UNGROUPED_COURSE_KEY:
                    reporter.on_stage(result.source_path, "ungrouped_skip")
                    ungrouped += 1
                    continue

                cache_dir = paths_config.cache_dir / course_name
                vault_course_dir = paths_config.vault_root / course_name

                outcome = _process_file(
                    result,
                    cache_dir,
                    vault_course_dir,
                    client,
                    llm_client,
                    llm_config,
                    output_config,
                    naming_config,
                    conn,
                    force_llm,
                    dry_run,
                    force_vault_overwrite,
                    no_llm,
                    course_name,
                    reporter,
                )
                processed += outcome.processed
                skipped += outcome.skipped
                errors += outcome.errors
                llm_reprocessed += outcome.llm_reprocessed
                total_input_tokens += outcome.input_tokens
                total_output_tokens += outcome.output_tokens
                total_cost_estimate += outcome.cost_estimate
                total_pages_processed += outcome.pages
                vault_conflicts += outcome.vault_conflicts
    finally:
        if owns_client and client is not None:
            client.close()

    return RunSummary(
        processed=processed,
        skipped=skipped,
        errors=errors,
        ungrouped=ungrouped,
        llm_reprocessed=llm_reprocessed,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cost_estimate=total_cost_estimate,
        total_pages_processed=total_pages_processed,
        vault_conflicts=vault_conflicts,
    )


def _print_summary(summary: RunSummary, dry_run: bool = False) -> None:
    print()
    if dry_run:
        print("Dry run -- no files were modified.")
    print("Done.")
    print(f"  {'Documents processed:':<21}{summary.processed}")
    print(f"  {'Pages processed:':<21}{summary.total_pages_processed}")
    print(f"  {'Skipped:':<21}{summary.skipped}")
    print(f"  {'Errors:':<21}{summary.errors}")
    print(f"  {'Ungrouped:':<21}{summary.ungrouped}")
    print(f"  {'LLM reprocessed:':<21}{summary.llm_reprocessed}")
    print(f"  {'Vault conflicts:':<21}{summary.vault_conflicts}")
    print(f"  {'Input tokens:':<21}{summary.total_input_tokens}")
    print(f"  {'Output tokens:':<21}{summary.total_output_tokens}")
    print(f"  {'Est. cost:':<21}${summary.total_cost_estimate:.4f}")


def _select_reporter(verbose: bool) -> Reporter:
    """
    Issue #49: choose RichReporter when stdout is an interactive TTY and
    rich is importable, falling back to PlainReporter otherwise (piped/
    redirected/CI output, or rich missing). Deliberately implemented here
    in main(), not inside run() -- run()'s own reporter=None default stays
    a plain, dumb PlainReporter() for direct/test callers (see run()'s
    docstring); only the real CLI auto-selects.
    """
    if sys.stdout.isatty():
        try:
            from src.reporting import RichReporter

            return RichReporter(verbose=verbose)
        except ImportError:
            pass
    return PlainReporter(verbose=verbose)


def main(argv: list[str] | None = None) -> int:
    """
    CLI entry point: parse argv (src/cli.py's build_arg_parser(), issue #41),
    load config.yaml's paths:, open/init state.db, run the discover -> process
    -> record pipeline once, print a summary.

    --course (issue #41), --dry-run (issue #42), --force (issue #43),
    --rerun-llm / --file (issue #44), --force-vault-overwrite (issue #45),
    --no-llm (issue #46), and --verbose/-v (issue #48) are wired up so far.
    --force does not set force_llm=True itself, since forcing full
    reprocessing already implies a fresh LLM pass regardless (see run()'s
    docstring). Hits the real, paid Mathpix and LLM APIs -- same caution as
    scripts/smoke_test_mathpix.py / scripts/smoke_test_llm.py (unless
    --dry-run or --no-llm is given, which skip the LLM API entirely --
    --dry-run additionally skips Mathpix too).

    --verbose/-v does not add a new run()/_process_file() param -- main()
    builds a reporter via _select_reporter(args.verbose) (issue #49; a
    RichReporter or PlainReporter, both constructed with
    verbose=args.verbose) and passes it as run()'s existing reporter= param
    (issue #47), since --verbose only ever changes what the reporter itself
    chooses to print, not any pipeline logic. The run() call is wrapped in
    `with reporter:` so a RichReporter's Live display starts/stops cleanly,
    including if run() raises.

    --file is validated here, before run() is ever called: the path must
    exist, end in .pdf (case-insensitive), and resolve to somewhere under
    paths.input_root -- any violation prints a clear error to stderr and
    returns exit code 1 without touching state.db or any API (rather than
    letting an obscure exception surface from deep inside classify_pdf()).
    --course/--file are already mutually exclusive at the argparse level
    (src/cli.py's build_arg_parser()), so run()'s own course/
    target_source_path ValueError guard is unreachable from this path.
    """
    args = build_arg_parser().parse_args(argv)

    try:
        paths_config = load_paths_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    target_source_path = args.file
    if target_source_path is not None:
        file_path = Path(target_source_path)
        input_root = Path(paths_config.input_root).resolve()
        if not file_path.is_file():
            print(f"ERROR: --file {target_source_path!r} does not exist.", file=sys.stderr)
            return 1
        if file_path.suffix.lower() != ".pdf":
            print(f"ERROR: --file {target_source_path!r} is not a .pdf file.", file=sys.stderr)
            return 1
        resolved_file = file_path.resolve()
        if input_root not in resolved_file.parents:
            print(
                f"ERROR: --file {target_source_path!r} is not under "
                f"paths.input_root ({input_root}).",
                file=sys.stderr,
            )
            return 1

    conn = init_db(paths_config.state_db)
    reporter = _select_reporter(args.verbose)
    with reporter:
        summary = run(
            paths_config,
            conn,
            course=args.course,
            dry_run=args.dry_run,
            force=args.force,
            force_llm=args.rerun_llm,
            target_source_path=target_source_path,
            force_vault_overwrite=args.force_vault_overwrite,
            no_llm=args.no_llm,
            reporter=reporter,
        )
    _print_summary(summary, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
