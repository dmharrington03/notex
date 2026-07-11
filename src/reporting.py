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
    - on_detail(source_path, message): finer-grained detail lines, only
      ever populated once --verbose (issue #48) wires real callers -- this
      issue only wires the plumbing (src/mathpix.py's existing on_status
      hook, and a new equivalent added to src/llm.py's cleanup_pdf()) to
      call it, with messages that never print anything today. PlainReporter
      is a no-op here by design, so today's output is unchanged.
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
    output. See module docstring for the full design rationale.
    """

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
        # Verbose-only (issue #48 wires real callers) -- no-op today so
        # PlainReporter's default output is unchanged from before this
        # refactor.
        pass

    def on_done(self, source_path: str, status: str) -> None:
        # New plumbing, reserved for a future RichReporter (issue #49) --
        # no-op today, no current visible-output requirement.
        pass
