"""
Phase 7 (issue #47) reporting abstraction.

Introduces a small `Reporter` protocol that `src/main.py`'s per-file
processing/vault-writing code reports through, instead of calling `print()`
directly. `PlainReporter` is the only implementation built by this issue: it
reproduces today's exact `print()`-based output, just routed through the
interface -- a pure refactor, not new behavior. `--verbose` (issue #48) and a
`RichReporter` (issue #49) are separate follow-up issues that build on this
one; neither is implemented here.

Reporter has three hook methods:
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
          be enumerated (an exception's str(), a delimiter-balance warning,
          etc.) -- PlainReporter recognizes these by their absence from
          _STAGE_TEXT and prints them verbatim.
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
      write_lecture_note()'s existing VaultWriteResult.
    - on_done(source_path, status): terminal per-file outcome signal, new
      plumbing with no current visible-output requirement (reserved for a
      future RichReporter to know when to finalize a table row).
      PlainReporter is a no-op here too.

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
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Reporter(Protocol):
    """See module docstring for the full contract of each method."""

    def on_stage(self, source_path: str, stage: str) -> None: ...

    def on_detail(self, source_path: str, message: str) -> None: ...

    def on_done(self, source_path: str, status: str) -> None: ...


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
        # New plumbing, reserved for a future RichReporter (issue #49) --
        # no-op today, no current visible-output requirement.
        pass
