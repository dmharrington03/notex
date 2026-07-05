"""
LLM cleanup client — Phase 3, issues #15-#17, #21.

Thin wrapper around litellm.completion() plus prompt-text loading,
post-cleanup validation, and end-to-end per-file orchestration:

    - CompletionResult        result of LLMClient.complete() -- content plus
                              best-effort input/output token counts and a
                              cost estimate (issue #21)
    - LLMClient               thin, injectable litellm.completion() wrapper
    - LLMError                raised on completion failures / bad responses
    - load_prompt_text()      reads prompts/{prompt_version}.txt
    - ValidationResult        post-cleanup validation result
    - validate_cleanup()      length ratio / delimiter-balance / heading checks
    - LLMResult               result of cleanup_pdf()
    - cleanup_pdf()           orchestrates cleanup + validation + fallback
    - needs_llm_reprocessing() LLM-stage staleness check (separate concern
                              from src/discovery.py's Mathpix-stage
                              classify_pdf() -- see AGENTS.md)

Implementation status:
    - LLMClient / complete()   implemented (issue #15; return type changed
                                from str to CompletionResult in issue #21)
    - load_prompt_text()       implemented (issue #15)
    - validate_cleanup()       implemented (issue #16)
    - cleanup_pdf()            implemented (issue #17; threads
                                input/output token counts + cost estimate
                                through LLMResult as of issue #21)
    - needs_llm_reprocessing() implemented (issue #17)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import litellm
from dotenv import load_dotenv

from src.config import LLMConfig
from src.state import StateEntry

# Word-boundary-aware: excludes \rightarrow, \leftarrow, \leftrightarrow,
# etc, which start with \right/\left as a literal prefix but aren't the
# delimiter command itself (see AGENTS.md's \left/\right validation notes).
_LEFT_DELIMITER_RE = re.compile(r"\\left(?![a-zA-Z])")
_RIGHT_DELIMITER_RE = re.compile(r"\\right(?![a-zA-Z])")

# ATX-style Markdown heading, e.g. "## Heading" -- matched per line.
_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


class LLMError(Exception):
    """Raised on LLM completion failures, malformed responses, or a missing
    prompt file for a configured prompt_version."""


@dataclass(frozen=True)
class CompletionResult:
    """
    Result of LLMClient.complete() (issue #21).

    input_tokens/output_tokens/cost are captured directly from the real
    litellm.completion() response (response.usage.prompt_tokens/
    .completion_tokens, and litellm.completion_cost()) -- exact/billed
    figures, not a separate re-tokenization pass. All three are best-effort
    and independently fall back to None if the response lacks a usable
    `usage` attribute, or if completion_cost() raises (e.g. an unpriced
    model) -- neither failure raises LLMError.
    """

    content: str
    input_tokens: int | None
    output_tokens: int | None
    cost: float | None


class LLMClient:
    """
    Thin wrapper around litellm.completion().

    completion_fn is constructor-injectable (mirrors MathpixClient's
    http_client= pattern -- see src/mathpix.py) so tests supply a fake and
    never hit a real API (see AGENTS.md Testing Conventions).
    """

    def __init__(
        self,
        model: str,
        completion_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self._completion_fn = completion_fn or litellm.completion

        # Ensure ANTHROPIC_API_KEY (or whatever credential the configured
        # model needs) is present in os.environ before completion_fn is
        # ever called, regardless of whether load_mathpix_credentials() has
        # already run earlier in this process (it can't be relied on to run
        # first once #17/#18 land -- a run touching only the LLM stage for
        # already-Mathpix-processed files would otherwise never call
        # load_dotenv() at all). Matches load_mathpix_credentials()'s own
        # unconditional load_dotenv() call in src/config.py. Deliberately
        # does not read/validate the key itself -- that stays litellm's job
        # (a missing key surfaces as whatever exception completion_fn
        # raises, wrapped into LLMError by complete() below), per
        # AGENTS.md's "no credential-loading logic in config.py" note.
        load_dotenv()

    def complete(self, system_prompt: str, user_content: str) -> CompletionResult:
        """
        Run a single chat completion and return the assistant's response
        text plus best-effort token usage / cost metadata.

        Args:
            system_prompt: the system message content (e.g. the loaded
                cleanup prompt text from load_prompt_text()).
            user_content: the user message content (e.g. the raw Mathpix
                Markdown to clean up).

        Returns:
            A CompletionResult with the completion's message content
            (verbatim) plus input_tokens/output_tokens/cost -- each of the
            latter three is best-effort and falls back to None (never
            raises LLMError) if the response object lacks a usable
            `usage` attribute, or if litellm.completion_cost() raises for
            any reason (e.g. a model missing from litellm's pricing map).
            Token/cost figures come from the real API response's own
            usage reporting (issue #21), not a separate re-tokenization
            pass.

        Raises:
            LLMError: if completion_fn raises for any reason, if the
                response doesn't have the expected
                response.choices[0].message.content shape, or if that
                content is empty/whitespace-only.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            response = self._completion_fn(model=self.model, messages=messages)
        except Exception as exc:
            raise LLMError(f"LLM completion failed: {exc}") from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {response!r}") from exc

        if not content or not content.strip():
            raise LLMError("LLM completion returned empty content")

        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            usage = response.usage
            input_tokens = usage.prompt_tokens
            output_tokens = usage.completion_tokens
        except AttributeError:
            pass

        cost: float | None = None
        try:
            cost = litellm.completion_cost(completion_response=response, model=self.model)
        except Exception:
            cost = None

        return CompletionResult(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
        )


def load_prompt_text(
    prompt_version: str,
    prompts_dir: str | Path = Path("prompts"),
) -> str:
    """
    Read prompts/{prompt_version}.txt and return its contents verbatim.

    Args:
        prompt_version: e.g. "cleanup_v1" (see config.yaml's
            llm.prompt_version -- src/config.py's load_llm_config()).
        prompts_dir: directory containing versioned prompt files. Defaults
            to "prompts" relative to the current working directory,
            matching the project convention of running the CLI from the
            repo root (same convention as DEFAULT_CONFIG_PATH in
            src/config.py).

    Raises:
        LLMError: if prompts/{prompt_version}.txt doesn't exist -- a
            configured prompt_version with no matching file is a real
            config error, not silently ignorable.
    """
    path = Path(prompts_dir) / f"{prompt_version}.txt"
    if not path.is_file():
        raise LLMError(f"Prompt file not found for prompt_version={prompt_version!r}: {path}")

    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class ValidationResult:
    """Result of validate_cleanup(): overall pass/fail plus a per-check
    breakdown, so callers (cleanup_pdf(), issue #17) can log/store which
    specific check(s) failed rather than just a single boolean."""

    passed: bool
    checks: dict[str, bool]


def validate_cleanup(
    original: str,
    cleaned: str,
    min_length_ratio: float,
    max_length_ratio: float,
) -> ValidationResult:
    """
    Validate an LLM cleanup pass's output against the raw Mathpix Markdown
    it was derived from.

    Four independent checks (all must pass for ValidationResult.passed to
    be True):

        - length_ratio: len(cleaned) / len(original) falls within
          [min_length_ratio, max_length_ratio]. Guards against wholesale
          truncation or runaway hallucinated expansion.
        - dollar_balance: `cleaned` has an even number of "$" characters
          (covers both inline "$...$" and display "$$ ... $$" delimiters,
          since "$$" is just two adjacent "$" -- an even total count
          balances both forms simultaneously). Checked on `cleaned` alone,
          not compared against `original`.
        - left_right_balance: `cleaned` has an equal count of "\\left" and
          "\\right" delimiter commands. Count-only, not delimiter-*type*
          matching -- see AGENTS.md's Smoke test findings: "\\left(...\\right]"
          is syntactically valid LaTeX regardless of whether the bracket
          shapes correspond, so there's no static rule to catch a true
          type mismatch. Checked on `cleaned` alone.
        - heading_count: relaxed, not exact-match. Fails only if `cleaned`
          has *more* ATX-style Markdown headings than `original` (a
          hallucinated new heading); equal or fewer passes, since the
          cleanup prompt (prompts/cleanup_v1.txt) is explicitly permitted
          to drop a single stray non-structural heading artifact.

    Args:
        original: the raw Mathpix Markdown before cleanup.
        cleaned: the LLM's cleaned-up Markdown output.
        min_length_ratio: lower bound for len(cleaned)/len(original).
        max_length_ratio: upper bound for len(cleaned)/len(original).

    Returns:
        ValidationResult with `passed` set iff every check in `checks`
        passed.
    """
    if len(original) == 0:
        # Degenerate case (shouldn't occur in practice -- cached Mathpix
        # output is never actually empty) -- avoid a ZeroDivisionError.
        # An empty original can only "pass" the ratio check by staying
        # empty; any non-empty cleaned output against an empty original
        # has no meaningful ratio to bound.
        length_ratio_ok = len(cleaned) == 0
    else:
        ratio = len(cleaned) / len(original)
        length_ratio_ok = min_length_ratio <= ratio <= max_length_ratio

    dollar_balance_ok = cleaned.count("$") % 2 == 0

    left_count = len(_LEFT_DELIMITER_RE.findall(cleaned))
    right_count = len(_RIGHT_DELIMITER_RE.findall(cleaned))
    left_right_balance_ok = left_count == right_count

    original_heading_count = len(_HEADING_RE.findall(original))
    cleaned_heading_count = len(_HEADING_RE.findall(cleaned))
    heading_count_ok = cleaned_heading_count <= original_heading_count

    checks = {
        "length_ratio": length_ratio_ok,
        "dollar_balance": dollar_balance_ok,
        "left_right_balance": left_right_balance_ok,
        "heading_count": heading_count_ok,
    }

    return ValidationResult(passed=all(checks.values()), checks=checks)


@dataclass(frozen=True)
class LLMResult:
    """
    Result of cleanup_pdf().

    Unlike src/mathpix.py's ProcessResult, LLMResult is returned on both
    success *and* failure -- the LLM cleanup stage's fallback-to-raw-output
    behavior is intrinsic to cleanup_pdf() itself (per docs/spec.md /
    AGENTS.md), so failure is represented here rather than via an
    exception.

    On failure (llm_status == "failed", whether from an LLMError raised by
    the completion call itself, or from a failed validate_cleanup() check):
        - llm_model / llm_prompt_version are both None -- the stored
          output_path in that case is the untouched raw Mathpix Markdown,
          which was not actually produced by any model/prompt version (see
          AGENTS.md: state.db's llm_prompt_version "always records
          whichever version actually produced that row's currently-stored
          output").
        - output_path points directly at the original
          mathpix_markdown_path passed into cleanup_pdf() -- no new file
          is written in the failure case.
        - llm_validation_result is None if the failure was an LLMError
          (validation never ran), or the JSON-serialized checks dict from
          ValidationResult if validation ran and failed.

    On success (llm_status == "success"):
        - llm_model / llm_prompt_version reflect the LLMConfig actually
          used to produce the output.
        - output_path points at the newly-written
          dest_dir/{lecture_stem}.llm.md.
        - llm_validation_result is the JSON-serialized (passing) checks
          dict from ValidationResult.

    llm_input_tokens / llm_output_tokens / llm_cost_estimate (issue #21):
    populated from the CompletionResult whenever the completion call
    actually happened and returned -- i.e. on success AND on the
    validation-failure fallback (the API call still happened and cost real
    money even though the cleaned output was discarded), but NOT on an
    LLMError fallback (no completion was ever returned, so there's no
    usage to report -- all three stay None).
    """

    llm_model: str | None
    llm_prompt_version: str | None
    llm_status: str
    llm_validation_result: str | None
    output_path: Path
    processed_at: datetime
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None
    llm_cost_estimate: float | None = None


def cleanup_pdf(
    mathpix_markdown_path: str | Path,
    dest_dir: str | Path,
    lecture_stem: str,
    llm_config: LLMConfig,
    client: LLMClient | None = None,
) -> LLMResult:
    """
    Orchestrate the LLM cleanup stage for a single already-cached Mathpix
    Markdown file: read it, run it through the LLM, validate the result,
    and either write the cleaned output or fall back to the raw input.

    Args:
        mathpix_markdown_path: path to the cached `.mathpix.md` file to
            clean up (see src/mathpix.py's fetch_and_extract()).
        dest_dir: directory to write `{lecture_stem}.llm.md` into on
            success. Created if it doesn't already exist. Not touched at
            all on failure.
        lecture_stem: filename stem used for the output file, e.g.
            "lecture_01" (matching src/mathpix.py's fetch_and_extract()
            convention).
        llm_config: the LLMConfig to use (model, prompt_version,
            min_length_ratio, max_length_ratio) -- see
            src/config.py's load_llm_config().
        client: an already-constructed LLMClient to use, e.g. one wired to
            a fake completion_fn for tests (mirrors src/mathpix.py's
            client= injection pattern). When omitted, cleanup_pdf()
            constructs its own LLMClient(model=llm_config.model). Unlike
            MathpixClient, LLMClient owns no closable resources, so there's
            no ownership/close() bookkeeping here.

    Returns:
        An LLMResult on both success and failure -- see LLMResult's
        docstring. cleanup_pdf() itself does not raise for an LLM API
        failure or a failed validation check; the stage's
        fallback-to-raw-Mathpix-output behavior is intrinsic to it, per
        docs/spec.md / AGENTS.md.

    Raises:
        FileNotFoundError: if mathpix_markdown_path does not exist. This is
            a setup error (the Mathpix stage should already have produced
            this file), not a per-file LLM failure, so it propagates
            rather than falling back -- matches src/mathpix.py's
            process_pdf() letting FileNotFoundError propagate for a
            missing input PDF.
        LLMError: if load_prompt_text() can't find
            prompts/{llm_config.prompt_version}.txt. A missing configured
            prompt_version is a real config error, not silently
            ignorable (see load_prompt_text()'s own docstring), so it
            propagates rather than falling back.
    """
    mathpix_markdown_path = Path(mathpix_markdown_path)
    if not mathpix_markdown_path.is_file():
        raise FileNotFoundError(f"Mathpix Markdown not found: {mathpix_markdown_path}")

    raw_markdown = mathpix_markdown_path.read_text(encoding="utf-8")

    # Propagates LLMError if prompts/{prompt_version}.txt is missing -- a
    # real config error, not covered by the API/validation fallback below.
    system_prompt = load_prompt_text(llm_config.prompt_version)

    if client is None:
        client = LLMClient(model=llm_config.model)

    try:
        completion_result = client.complete(system_prompt, raw_markdown)
    except LLMError:
        return LLMResult(
            llm_model=None,
            llm_prompt_version=None,
            llm_status="failed",
            llm_validation_result=None,
            output_path=mathpix_markdown_path,
            processed_at=datetime.now(timezone.utc),
        )

    cleaned_markdown = completion_result.content

    validation_result = validate_cleanup(
        raw_markdown,
        cleaned_markdown,
        llm_config.min_length_ratio,
        llm_config.max_length_ratio,
    )

    if not validation_result.passed:
        return LLMResult(
            llm_model=None,
            llm_prompt_version=None,
            llm_status="failed",
            llm_validation_result=json.dumps(validation_result.checks),
            output_path=mathpix_markdown_path,
            processed_at=datetime.now(timezone.utc),
            llm_input_tokens=completion_result.input_tokens,
            llm_output_tokens=completion_result.output_tokens,
            llm_cost_estimate=completion_result.cost,
        )

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    output_path = dest_dir / f"{lecture_stem}.llm.md"
    output_path.write_text(cleaned_markdown, encoding="utf-8")

    return LLMResult(
        llm_model=llm_config.model,
        llm_prompt_version=llm_config.prompt_version,
        llm_status="success",
        llm_validation_result=json.dumps(validation_result.checks),
        output_path=output_path,
        processed_at=datetime.now(timezone.utc),
        llm_input_tokens=completion_result.input_tokens,
        llm_output_tokens=completion_result.output_tokens,
        llm_cost_estimate=completion_result.cost,
    )


def needs_llm_reprocessing(entry: StateEntry) -> bool:
    """
    Whether a file whose Mathpix stage is already cached/unchanged still
    needs its LLM cleanup stage (re)run.

    True only if entry.llm_status is None (never attempted) or "failed"
    (previously failed, including the fallback-to-raw case -- see
    LLMResult). A stored llm_status of "success" always returns False, even
    if entry.llm_prompt_version doesn't match the currently configured
    llm.prompt_version -- deliberately: switching config.yaml's
    llm.prompt_version must never silently trigger mass reprocessing (and
    its associated LLM API cost) on the next ordinary run. See AGENTS.md's
    "Deliberate correction to docs/spec.md's Reprocessing logic table" and
    the forthcoming force_llm parameter on src/main.py's run() (issue #18)
    for the explicit-opt-in path to reprocess under a new prompt version.

    This is a separate concern from src/discovery.py's classify_pdf(),
    which is scoped to Mathpix-stage change detection only (see
    discovery.py's own docstring) -- "has this file's cached Mathpix
    output ever been successfully cleaned by the LLM stage?" is a
    different question, owned by this module instead.

    Args:
        entry: the file's current state.db row (see src/state.py's
            get_entry()).

    Returns:
        True if the LLM stage should run (or re-run) for this file.
    """
    return entry.llm_status is None or entry.llm_status == "failed"
