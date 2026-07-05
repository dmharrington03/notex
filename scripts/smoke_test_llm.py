"""
Manual smoke test — hits the REAL LLM API (via LLMClient/litellm) and costs
money per run.

Not part of the pytest suite. Run by hand against an already-cached
`.mathpix.md` file (see src/mathpix.py's fetch_and_extract()) to iterate on
prompts/{prompt_version}.txt and inspect real cleanup output/validation
results, without needing any new Mathpix API calls.

Deliberately calls LLMClient.complete() + validate_cleanup() directly rather
than src/llm.py's cleanup_pdf() orchestration: cleanup_pdf() discards the
cleaned text entirely on a failed validate_cleanup() check (by design, for
the pipeline's fallback-to-raw-output behavior — see AGENTS.md/issue #17),
which would hide the most useful case for prompt iteration (seeing *why* a
cleanup failed validation). This script always has the cleaned text in hand
regardless of pass/fail.

Does not touch state.db.

Usage:
    conda activate notex
    python scripts/smoke_test_llm.py _cache/class_1/lecture_01.mathpix.md
    python scripts/smoke_test_llm.py _cache/class_1/lecture_01.mathpix.md --prompt-version cleanup_v2
    python scripts/smoke_test_llm.py _cache/class_1/lecture_01.mathpix.md --out _cache/smoke_test_llm/lecture_01.llm.md
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

# Allow running directly as `python scripts/smoke_test_llm.py` from the repo
# root without needing the repo installed as a package -- sys.path[0] is
# this script's own directory (scripts/), not the repo root, so `src` is not
# importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ConfigError, load_llm_config
from src.llm import LLMClient, LLMError, load_prompt_text, validate_cleanup


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual smoke test: run a cached .mathpix.md file through the "
            "real LLM cleanup call, print the cleaned output plus "
            "validate_cleanup() results. Costs money per run."
        )
    )
    parser.add_argument(
        "mathpix_md_path",
        type=Path,
        help="Path to a cached .mathpix.md file (e.g. _cache/class_1/lecture_01.mathpix.md).",
    )
    parser.add_argument(
        "--prompt-version",
        default=None,
        help=(
            "Override config.yaml's llm.prompt_version for this run only "
            "(loads prompts/{prompt_version}.txt). Does not modify "
            "config.yaml."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Write the cleaned Markdown to this file path. When given, the "
            "cleaned Markdown is written to disk instead of printed to "
            "stdout (the validation summary is still printed either way)."
        ),
    )
    return parser.parse_args(argv)


def _print_summary(
    model: str,
    prompt_version: str,
    original: str,
    cleaned: str,
    validation_result,
    input_tokens: int | None,
    output_tokens: int | None,
    cost: float | None,
) -> None:
    ratio = (len(cleaned) / len(original)) if original else float("nan")

    print()
    print("Validation summary")
    print(f"  model:            {model}")
    print(f"  prompt_version:   {prompt_version}")
    print(f"  original length:  {len(original)} chars")
    print(f"  cleaned length:   {len(cleaned)} chars")
    print(f"  length ratio:     {ratio:.3f}")
    print("  checks:")
    for name, passed in validation_result.checks.items():
        status = "PASS" if passed else "FAIL"
        print(f"    - {name}: {status}")
    overall = "PASSED" if validation_result.passed else "FAILED"
    print(f"  overall: {overall}")
    print(f"  input tokens:     {input_tokens if input_tokens is not None else 'unknown'}")
    print(f"  output tokens:    {output_tokens if output_tokens is not None else 'unknown'}")
    cost_str = f"${cost:.4f}" if cost is not None else "unknown"
    print(f"  est. cost:        {cost_str}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.mathpix_md_path.is_file():
        print(f"ERROR: file not found: {args.mathpix_md_path}", file=sys.stderr)
        return 1

    try:
        llm_config = load_llm_config()
        if args.prompt_version is not None:
            llm_config = dataclasses.replace(llm_config, prompt_version=args.prompt_version)

        system_prompt = load_prompt_text(llm_config.prompt_version)
        raw_markdown = args.mathpix_md_path.read_text(encoding="utf-8")

        print(f"Running LLM cleanup on {args.mathpix_md_path} ...")
        client = LLMClient(model=llm_config.model)
        completion_result = client.complete(system_prompt, raw_markdown)
        cleaned_markdown = completion_result.content

        validation_result = validate_cleanup(
            raw_markdown,
            cleaned_markdown,
            llm_config.min_length_ratio,
            llm_config.max_length_ratio,
        )
    except (ConfigError, LLMError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    _print_summary(
        llm_config.model,
        llm_config.prompt_version,
        raw_markdown,
        cleaned_markdown,
        validation_result,
        completion_result.input_tokens,
        completion_result.output_tokens,
        completion_result.cost,
    )

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(cleaned_markdown, encoding="utf-8")
        print()
        print(f"Cleaned output written to {args.out}")
    else:
        print()
        print("--- CLEANED OUTPUT ---")
        print(cleaned_markdown)

    return 0


if __name__ == "__main__":
    sys.exit(main())
