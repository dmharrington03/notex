"""
Phase 7 (issue #47) reporting abstraction.

Introduces a small `Reporter` protocol that `src/main.py`'s per-file
processing/vault-writing code reports through, instead of calling `print()`
directly. `PlainReporter` reproduces today's exact `print()`-based output,
just routed through the interface -- a pure refactor, not new behavior.
`RichReporter` (issue #49) is a second implementation: a `rich.live.Live` +
`rich.table.Table` progress display, auto-selected by `src/main.py`'s
`main()` when stdout is an interactive TTY and `rich` is importable.

Reporter has five hook methods:
    - on_discover(items): issue #49 -- called exactly once per run(), right
      after discovery/force-reclassification completes and before any
      per-file processing begins, with one (source_path, classification)
      pair (classification is a discovery.Classification.value string:
      "new"/"unchanged"/"changed"/"retry") per file identified this run
      (including ungrouped ones). This exists so a reporter can pre-populate
      a full picture of the run's scope up front, with an accurate initial
      per-file state -- critically, most UNCHANGED-and-current files never
      receive any further on_stage/on_detail call at all (they're silently
      tallied as skipped), so a reporter can't wait for a later hook to know
      those rows are effectively already "done". PlainReporter is a no-op
      here (matches its pre-#49 zero-output-change design); RichReporter
      seeds its internal table from this call (NEW/CHANGED/RETRY -> a
      "waiting" row, UNCHANGED -> an already-settled "up to date" row).
    - on_stage(source_path, stage): called for every real per-file progress
      line the pipeline already prints today (processing started, LLM/vault
      outcome, warnings, failures, dry-run "would ..." lines, etc.) --
      always rendered by PlainReporter. `stage` is one of two shapes:
        * a short canonical token drawn from a closed, enumerable
          vocabulary (see _STAGE_TEXT below) for routine lifecycle
          transitions, e.g. "submitting:new", "done:llm_success" -- these
          exist so a future RichReporter can switch on them to drive a
          live-updating table without parsing free text.
        * the literal, already-fully-composed message text for the
          handful of sites whose content is inherently free-form and can't
          be enumerated (an exception's str(), a delimiter-balance
          warning, etc.) -- PlainReporter recognizes these by their
          absence from _STAGE_TEXT and prints them verbatim.
    - on_detail(source_path, message): finer-grained detail lines. Issue
      #47 (this issue's original scope) only wired the plumbing
      (src/mathpix.py's existing on_status hook, and a new equivalent
      added to src/llm.py's cleanup_pdf()) to call it, with messages that
      never printed anything -- PlainReporter was a no-op here by design,
      so its default output was unchanged. Issue #48 adds a
      `verbose: bool = False` constructor param to PlainReporter: when
      True, on_detail() actually prints (see the module docstring's
      "on_detail()" note further below for the exact format); when False
      (the default), it stays a no-op, so non-verbose output is
      completely unaffected. Issue #48 also wires two more real callers:
      src/figures.py's copy_figures_to_vault() (a new on_copy callback
      param, since it previously had none) and a new post-write
      confirmation call in src/main.py's _write_to_vault(), sourced from
      write_lecture_note()'s existing VaultWriteResult. RichReporter
      (issue #49) treats verbose the same way: when False, on_detail is a
      no-op; when True, the message is appended as a dim trailing suffix
      on that file's Status cell (see RichReporter's own docstring).
    - on_done(source_path, status): terminal per-file outcome signal.
      Still genuinely unused plumbing as of issue #49 -- no call site in
      src/main.py calls it. RichReporter deliberately does not require it
      either: on_discover's classification-seeding plus the last on_stage
      call it observes are sufficient to give every row a sensible
      resting state, so this was consciously left unwired rather than
      adding new call sites to src/main.py as part of #49 (confirmed with
      the user). Both PlainReporter and RichReporter treat it as a no-op.

Reporter also has two context-manager hook methods, `__enter__`/`__exit__`,
used only by src/main.py's main() (issue #49) -- it constructs a reporter
and wraps the run() call in `with reporter:` so a RichReporter's Live
display starts/stops cleanly (including on an exception mid-run). This is
main()-only: run() itself never enters/exits a reporter as a context
manager, so any Reporter (real or a test double) passed directly to
run(reporter=...) never needs to implement these at all in practice, even
though they're part of the Protocol for main()'s benefit. PlainReporter's
are both no-ops (`__enter__` returns self, `__exit__` does nothing and
returns None/falsy so exceptions still propagate).

PlainReporter derives the "[{course}] {filename}: " prefix every on_stage
line is printed with directly from source_path (Path(source_path).parent.name
/ .name) rather than requiring a separate course_label argument -- this
matches every real (grouped-course) call site's course_label exactly, since
source_path is always "{input_root}/{course}/{filename}" there. The one
narrow exception is the "ungrouped_skip" token (the common, tested case of a
stray PDF sitting directly under input_root during a normal recursive scan),
which is special-cased to always render the synthetic "[ungrouped]" label
rather than deriving one from the path -- deriving there would show
input_root's own directory name, which is wrong. A separate, much rarer edge
case (a stray root-level PDF processed via `--file` directly, using the
"_ungrouped" cache-dir sentinel) is not special-cased: its printed bracket
label now shows the real parent directory name instead of the synthetic
"_ungrouped" placeholder used pre-refactor -- a deliberate, approved, purely
cosmetic deviation for that one rare, untested path.

on_detail() (issue #48, when verbose=True) uses the exact same derived
"[{course}] {filename}" label as on_stage, but prefixed with a 4-space
indent to visually distinguish a detail line as a sub-line of whichever
on_stage transition it belongs under, e.g.:

    [class_1] lecture_01.pdf: processing (new)...
        [class_1] lecture_01.pdf: mathpix pdf: poll 3/40 status=loaded
    [class_1] lecture_01.pdf: done (LLM cleanup succeeded)

message is always the already-fully-composed free-form text passed in --
there is no canonical-token vocabulary for detail lines (unlike on_stage),
since every current on_detail() caller already builds a complete,
human-readable message itself (src/mathpix.py's on_status, src/llm.py's
cleanup_pdf() on_status, src/figures.py's on_copy, and _write_to_vault()'s
own post-write confirmation summary).

RichReporter (issue #49) imports `rich` lazily/guarded at module level (a
try/except ImportError setting a module-level flag) so this module -- and
therefore PlainReporter/Reporter -- stays importable even in an environment
where `rich` isn't installed; RichReporter itself raises ImportError from
its own __init__ if `rich` wasn't importable, rather than failing at import
time. See RichReporter's own class docstring for its table layout/state
model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover -- exercised only without rich installed
    _RICH_AVAILABLE = False


class Reporter(Protocol):
    """See module docstring for the full contract of each method."""

    def on_discover(self, items: Sequence[tuple[str, str]]) -> None: ...

    def on_stage(self, source_path: str, stage: str) -> None: ...

    def on_detail(self, source_path: str, message: str) -> None: ...

    def on_done(self, source_path: str, status: str) -> None: ...

    def __enter__(self) -> "Reporter": ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...


# Canonical stage tokens -> the exact text PlainReporter has always printed
# for that transition (pre-refactor). Any stage value not present here is
# treated as already-fully-composed free-form text and printed verbatim
# (see PlainReporter.on_stage below) -- this covers exception messages and
# delimiter-balance warnings, which can't be enumerated into a fixed
# vocabulary without losing information.
_STAGE_TEXT: dict[str, str] = {
    "would_process:new": "would process (new)",
    "would_process:changed": "would process (changed)",
    "would_process:retry": "would process (retry)",
    "would_reprocess_llm": "would reprocess LLM stage only",
    "would_retry_vault": "would retry vault write (force_vault_overwrite)",
    "submitting:new": "processing (new)...",
    "submitting:changed": "processing (changed)...",
    "submitting:retry": "processing (retry)...",
    "editing:llm": "editing (LLM cleanup)...",
    "done:no_llm": "done (LLM stage skipped, --no-llm)",
    "done:llm_success": "done (LLM cleanup succeeded)",
    "done:llm_fallback": "done (LLM cleanup fell back to raw output)",
    "retrying_vault_write": "retrying vault write (force_vault_overwrite)...",
    "reprocessing_llm": "reprocessing LLM stage only...",
    "llm_only:llm_success": "LLM cleanup succeeded",
    "llm_only:llm_fallback": "LLM cleanup fell back to raw output",
}

# A canonical token whose displayed bracket label is always the fixed
# "ungrouped" string, never derived from source_path's parent directory (see
# module docstring's note on the ungrouped_skip special case).
_UNGROUPED_STAGE_TOKENS = frozenset({"ungrouped_skip"})

_UNGROUPED_STAGE_TEXT = (
    "skipping -- no course subfolder to group it under (not written to state.db)"
)


class PlainReporter:
    """
    Default Reporter implementation: reproduces today's exact print()-based
    output (with --verbose, issue #48, adding new detail lines). See module
    docstring for the full design rationale.
    """

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def on_discover(self, items: Sequence[tuple[str, str]]) -> None:
        # Issue #49: no-op -- PlainReporter's output is unaffected by
        # up-front discovery, it only ever reports as things actually
        # happen (via on_stage/on_detail).
        pass

    def on_stage(self, source_path: str, stage: str) -> None:
        if stage in _UNGROUPED_STAGE_TOKENS:
            label = "ungrouped"
            text = _UNGROUPED_STAGE_TEXT
        else:
            label = Path(source_path).parent.name
            text = _STAGE_TEXT.get(stage, stage)
        filename = Path(source_path).name
        print(f"[{label}] {filename}: {text}")

    def on_detail(self, source_path: str, message: str) -> None:
        # Issue #48: only prints when verbose=True -- the default (False)
        # keeps this a no-op, so non-verbose output is completely
        # unchanged from before --verbose existed.
        if not self.verbose:
            return
        label = Path(source_path).parent.name
        filename = Path(source_path).name
        print(f"    [{label}] {filename}: {message}")

    def on_done(self, source_path: str, status: str) -> None:
        # Still unused plumbing as of issue #49 -- no-op, no current
        # visible-output requirement.
        pass

    def __enter__(self) -> "PlainReporter":
        # Issue #49: main() now always does `with reporter:` around the
        # run() call -- PlainReporter has nothing to start, so this is a
        # pure no-op returning self.
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        # Nothing to tear down; returning None (falsy) lets any exception
        # from inside the `with` block propagate normally.
        return None


# Classification.value strings (src/discovery.py) -> a RichReporter row's
# initial Status text/style once seeded by on_discover, before any on_stage
# call has been received for that file. NEW/CHANGED/RETRY are genuinely
# about to be processed; UNCHANGED is very often never touched again this
# run (see module docstring), so it starts in an already-settled state
# rather than a perpetual "waiting".
_INITIAL_STATUS: dict[str, tuple[str, str]] = {
    "new": ("waiting", "dim"),
    "changed": ("waiting", "dim"),
    "retry": ("waiting", "dim"),
    "unchanged": ("up to date", "green"),
}

# Simple, best-effort style heuristics for on_stage text that isn't a
# canonical _STAGE_TEXT token (free-form exception/warning messages) or is
# a canonical token describing an error/warning outcome -- checked in
# order, first match wins. Canonical "done"/"llm_only" success tokens get
# their style from _CANONICAL_STYLE below instead (checked first).
_CANONICAL_STYLE: dict[str, str] = {
    "done:no_llm": "yellow",
    "done:llm_success": "green",
    "done:llm_fallback": "yellow",
    "llm_only:llm_success": "green",
    "llm_only:llm_fallback": "yellow",
    "ungrouped_skip": "dim",
}


def _style_for(stage: str, text: str) -> str:
    if stage in _CANONICAL_STYLE:
        return _CANONICAL_STYLE[stage]
    if "FAILED" in text:
        return "bold red"
    if "WARNING" in text or "conflict" in text:
        return "yellow"
    return "cyan"


class RichReporter:
    """
    Reporter implementation (issue #49): a rich.live.Live + rich.table.Table
    progress display, auto-selected by src/main.py's main() when stdout is
    an interactive TTY and rich is importable (see _select_reporter() in
    src/main.py) -- never selected by run() itself, and never required by
    any test double passed directly to run(reporter=...).

    State model: a plain dict keyed by source_path
    (self._rows: dict[str, dict]), each entry holding "course"/"filename"
    (derived from source_path exactly like PlainReporter),
    "status" (the current display text), "style" (a rich style string for
    the status text), and "detail" (the latest on_detail message, only
    ever set when verbose=True -- None otherwise). rich.table.Table cells
    can't be mutated in place, so every hook rebuilds a fresh Table from
    this dict and pushes it via Live.update(...) -- the standard Rich
    Live+Table pattern.

    Columns: Course | File | Status. on_detail's message (verbose=True
    only) is rendered as a dim trailing suffix appended to the Status
    cell's text (" -- {message}") rather than a separate column -- picked
    per the issue's explicit "whichever is simpler" allowance, since a
    single mutable Status string is simpler to manage than a second
    per-row mutable field rendered in its own column.

    on_discover(items) seeds one row per (source_path, classification) --
    see _INITIAL_STATUS above for each classification's starting
    status/style. on_stage(source_path, stage) updates that row's
    status/style (via _STAGE_TEXT's canonical-token text, same dict
    PlainReporter uses, plus _style_for()'s heuristic) and clears any
    stale detail suffix from a previous stage. on_detail is a no-op unless
    verbose=True, in which case it sets/replaces the row's detail suffix.
    on_done is a no-op (see module docstring -- no call site exists in
    src/main.py as of this issue; on_discover's seeding already avoids the
    "row never updates" problem for silently-skipped UNCHANGED files).

    __enter__ starts the Live display; __exit__ stops it, so the table
    finalizes cleanly (last frame stays visible) even if the run raises.
    """

    def __init__(self, verbose: bool = False) -> None:
        if not _RICH_AVAILABLE:
            raise ImportError(
                "rich is not installed -- RichReporter requires the 'rich' "
                "package (conda install -c conda-forge rich)"
            )
        self.verbose = verbose
        self._rows: dict[str, dict[str, str | None]] = {}
        self._console = Console()
        self._live = Live(self._render(), console=self._console, refresh_per_second=8)

    def _render(self) -> "Table":
        table = Table()
        table.add_column("Course")
        table.add_column("File")
        table.add_column("Status")
        for row in self._rows.values():
            status = row["status"]
            if row["detail"]:
                status = f"{status} -- [dim]{row['detail']}[/dim]"
            table.add_row(row["course"], row["filename"], f"[{row['style']}]{status}[/{row['style']}]")
        return table

    def _refresh(self) -> None:
        self._live.update(self._render())

    def on_discover(self, items: Sequence[tuple[str, str]]) -> None:
        for source_path, classification in items:
            status, style = _INITIAL_STATUS.get(classification, ("waiting", "dim"))
            self._rows[source_path] = {
                "course": Path(source_path).parent.name,
                "filename": Path(source_path).name,
                "status": status,
                "style": style,
                "detail": None,
            }
        self._refresh()

    def on_stage(self, source_path: str, stage: str) -> None:
        if stage in _UNGROUPED_STAGE_TOKENS:
            label = "ungrouped"
            text = _UNGROUPED_STAGE_TEXT
        else:
            label = Path(source_path).parent.name
            text = _STAGE_TEXT.get(stage, stage)
        row = self._rows.setdefault(
            source_path,
            {
                "course": label,
                "filename": Path(source_path).name,
                "status": text,
                "style": _style_for(stage, text),
                "detail": None,
            },
        )
        row["course"] = label
        row["status"] = text
        row["style"] = _style_for(stage, text)
        row["detail"] = None
        self._refresh()

    def on_detail(self, source_path: str, message: str) -> None:
        if not self.verbose:
            return
        row = self._rows.setdefault(
            source_path,
            {
                "course": Path(source_path).parent.name,
                "filename": Path(source_path).name,
                "status": "waiting",
                "style": "dim",
                "detail": None,
            },
        )
        row["detail"] = message
        self._refresh()

    def on_done(self, source_path: str, status: str) -> None:
        # Still unused plumbing as of issue #49 -- see module/class
        # docstrings.
        pass

    def __enter__(self) -> "RichReporter":
        self._live.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._live.stop()
        return None
