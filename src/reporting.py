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
    - on_done(runtime_secs): a #49 follow-up -- terminal *run*-level (not
      per-file) signal, called exactly once by src/main.py's main(), right
      after run() returns (still inside the `with reporter:` block), with
      the run's total wall-clock duration in seconds. Note the shape
      change from on_done's original (source_path, status) per-file
      signature: nothing ever called it with that signature (run() itself
      never calls on_done at all -- only main() does), so this is a clean
      break, not a migration. PlainReporter prints a trailing
      "Finished in {runtime_secs:.2f} s" line; RichReporter re-renders its
      table with a "Done in {runtime_secs:.1f} s" Panel subtitle.

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

    [class_1] lecture_01.pdf: Processing (new)...
        [class_1] lecture_01.pdf: mathpix pdf: poll 3/40 status=loaded
    [class_1] lecture_01.pdf: ✓ Done

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
    from rich.panel import Panel
    from rich.align import Align
    from rich.spinner import Spinner
    from rich import box

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover -- exercised only without rich installed
    _RICH_AVAILABLE = False


class Reporter(Protocol):
    """See module docstring for the full contract of each method."""

    def on_discover(self, items: Sequence[tuple[str, str]]) -> None: ...

    def on_stage(self, source_path: str, stage: str) -> None: ...

    def on_detail(self, source_path: str, message: str) -> None: ...

    def on_done(self, runtime_secs: float) -> None: ...

    def __enter__(self) -> "Reporter": ...

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...


# Canonical stage tokens -> the exact text PlainReporter has always printed
# for that transition (pre-refactor). Any stage value not present here is
# treated as already-fully-composed free-form text and printed verbatim
# (see PlainReporter.on_stage below) -- this covers exception messages and
# delimiter-balance warnings, which can't be enumerated into a fixed
# vocabulary without losing information.
_STAGE_TEXT: dict[str, str] = {
    "would_process:new": "Would process (new)",
    "would_process:changed": "Would process (changed)",
    "would_process:retry": "Would process (retry)",
    "would_reprocess_llm": "Would reprocess LLM stage only",
    "would_retry_vault": "Would retry vault write (force_vault_overwrite)",
    "submitting:new": "Processing (new)...",
    "submitting:changed": "Processing (changed)...",
    "submitting:retry": "Processing (retry)...",
    "editing:llm": "Editing...",
    "done:no_llm": "Done (LLM stage skipped, --no-llm)",
    # Plain text only -- no embedded Rich markup here (this dict is printed
    # verbatim by PlainReporter's print(), which doesn't interpret Rich
    # markup). RichReporter still renders this in green: its existing
    # _CANONICAL_STYLE mechanism wraps whatever text is looked up here in
    # the row's style, so no color tags need to be embedded in the text
    # itself.
    "done:llm_success": "✓ Done",
    "done:llm_fallback": "Done (LLM cleanup fell back to raw output)",
    "retrying_vault_write": "Retrying vault write (force_vault_overwrite)...",
    "reprocessing_llm": "Reprocessing LLM stage only...",
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

    def on_done(self, runtime_secs: float) -> None:
        # #49 follow-up: on_done is now a once-per-run completion signal
        # (see module docstring) -- prints a trailing line with the run's
        # total wall-clock duration.
        print(f"\nFinished in {runtime_secs:.2f} s")

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
    return "white"


# RichReporter-only: on_stage tokens for genuinely in-progress (not yet
# terminal) work, mapped to the rich.spinner.Spinner color/style to use
# while that row sits in this stage -- an animated spinner replaces the
# plain static text a non-spinner stage would otherwise show (see
# RichReporter.on_stage()/_render()). White for the Mathpix submit
# stages, cyan for the vault-write-retry stage, yellow for the two
# LLM-stage ones -- mirrors _CANONICAL_STYLE's yellow-for-fallback/
# LLM-adjacent convention. Any stage not listed here (including every
# terminal "done:*"/"llm_only:*" outcome) never gets a spinner,
# regardless of _style_for()'s own (unrelated) style heuristic.
_SPINNER_STAGES: dict[str, str] = {
    "submitting:new": "white",
    "submitting:changed": "white",
    "submitting:retry": "white",
    "editing:llm": "yellow",
    "reprocessing_llm": "yellow",
    "retrying_vault_write": "cyan",
}


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
    the status text), "spinner" (None, or a color/style string when this
    row is currently in one of _SPINNER_STAGES' in-progress stages -- see
    below), and "detail" (the latest on_detail message, only ever set when
    verbose=True -- None otherwise). rich.table.Table cells can't be
    mutated in place, so every hook rebuilds a fresh Table from this dict
    and pushes it via Live.update(...) -- the standard Rich Live+Table
    pattern.

    Columns: Course | File | Status. on_detail's message (verbose=True
    only) is rendered as a dim trailing suffix appended to the Status
    cell's text (" -- {message}") rather than a separate column -- picked
    per the issue's explicit "whichever is simpler" allowance, since a
    single mutable Status string is simpler to manage than a second
    per-row mutable field rendered in its own column. The table is wrapped
    in a centered, titled Panel ("NoTeX") -- a formatting pass on top of
    #49's original bare Table, with the Table's own title showing a live
    "{N} documents found" count (derived from len(self._rows), so it's
    always accurate rather than a fixed string) and the Panel's subtitle
    left blank until on_done() sets it to "Done in {runtime_secs:.1f} s".

    Spinners (a further follow-up): while a row is in one of
    _SPINNER_STAGES' four in-progress stages (submitting:*/editing:llm/
    reprocessing_llm/retrying_vault_write), its Status cell renders as an
    animated rich.spinner.Spinner instead of plain static text -- e.g.
    Spinner("dots", text="[yellow]Editing...", style="yellow") for
    editing:llm. _render() constructs a brand-new Spinner object every
    time it runs (same as every other cell), but this still animates
    correctly: Spinner computes its current frame from real elapsed wall-
    clock time (not an internal counter advanced by repeated render
    calls), and between our own on_stage()/on_detail()-triggered
    Live.update() calls, Live's own background auto-refresh thread
    (refresh_per_second=8) keeps re-rendering whatever Table/Spinner
    object we last pushed -- so the spinner keeps animating during a
    stage even though nothing in our code calls _render() again until the
    next hook fires. Every other (non-spinner) stage, including every
    terminal "done:*"/"llm_only:*" outcome, renders as plain styled text
    exactly as before.

    on_discover(items) seeds one row per (source_path, classification) --
    see _INITIAL_STATUS above for each classification's starting
    status/style (spinner always starts None -- "waiting"/"up to date"
    are not in-progress stages). on_stage(source_path, stage) updates that
    row's status/style (via _STAGE_TEXT's canonical-token text, same dict
    PlainReporter uses, plus _style_for()'s heuristic) and spinner (via
    _SPINNER_STAGES.get(stage), None for anything not listed there -- so a
    row's spinner is automatically cleared the moment it moves to a
    terminal stage) and clears any stale detail suffix from a previous
    stage. on_detail is a no-op unless verbose=True, in which case it
    sets/replaces the row's detail suffix -- shown as a trailing suffix
    inside the Spinner's own text when a spinner is active, same as the
    plain-text case. on_done (a #49 follow-up) is no longer a no-op: see
    module docstring -- it's now a once-per-run completion signal, and
    sets the Panel's subtitle to "Done in {runtime_secs:.1f} s".

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
        # A blank line before the Live display starts, purely cosmetic
        # spacing -- printed once at construction, before __enter__ starts
        # the Live (so it doesn't interfere with in-place updates).
        self._console.print()

    def _render(self, subtitle: str | None = None) -> "Align":
        count = len(self._rows)
        title = f"({count}) document{'' if count == 1 else 's'} found"
        table = Table(title=title, box=box.SIMPLE_HEAD)
        table.add_column("Course", style="cyan")
        table.add_column("File", style="magenta")
        table.add_column("Status")
        for row in self._rows.values():
            status = row["status"]
            if row["detail"]:
                status = f"{status} -- [dim]{row['detail']}[/dim]"
            spinner_style = row["spinner"]
            if spinner_style:
                cell = Spinner("dots", text=f"[{spinner_style}]{status}", style=spinner_style)
            else:
                cell = f"[{row['style']}]{status}[/{row['style']}]"
            table.add_row(row["course"], row["filename"], cell)

        return Align(
            Panel(table, title="[bold]NoTeX", expand=False, padding=(1, 5), subtitle=subtitle),
            "center",
        )

    def _refresh(self, subtitle: str | None = None) -> None:
        self._live.update(self._render(subtitle=subtitle))

    def on_discover(self, items: Sequence[tuple[str, str]]) -> None:
        for source_path, classification in items:
            status, style = _INITIAL_STATUS.get(classification, ("waiting", "dim"))
            self._rows[source_path] = {
                "course": Path(source_path).parent.name,
                "filename": Path(source_path).stem,
                "status": status,
                "style": style,
                "spinner": None,
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
                "filename": Path(source_path).stem,
                "status": text,
                "style": _style_for(stage, text),
                "spinner": None,
                "detail": None,
            },
        )
        row["course"] = label
        row["status"] = text
        row["style"] = _style_for(stage, text)
        row["spinner"] = _SPINNER_STAGES.get(stage)
        row["detail"] = None
        self._refresh()

    def on_detail(self, source_path: str, message: str) -> None:
        if not self.verbose:
            return
        row = self._rows.setdefault(
            source_path,
            {
                "course": Path(source_path).parent.name,
                "filename": Path(source_path).stem,
                "status": "waiting",
                "style": "dim",
                "spinner": None,
                "detail": None,
            },
        )
        row["detail"] = message
        self._refresh()

    def on_done(self, runtime_secs: float) -> None:
        # #49 follow-up: on_done is now a once-per-run completion signal
        # (see module docstring) -- sets the Panel's subtitle to the run's
        # total wall-clock duration.
        self._refresh(subtitle=f"Done in {runtime_secs:.1f} s")

    def __enter__(self) -> "RichReporter":
        self._live.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._live.stop()
        return None
