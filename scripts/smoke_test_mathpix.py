"""
Manual smoke test — hits the REAL Mathpix API and costs money per run.

Not part of the pytest suite. Run by hand against a real lecture PDF to
validate actual OCR output quality (text, LaTeX, figures) before trusting
the pipeline against a full course of notes. This is also where we settle
the open question about which math delimiter style ($...$ vs
\\(...\\)/\\[...\\]) Mathpix actually emits by default in md/mmd output --
see AGENTS.md "Mathpix API notes".

Usage:
    conda activate notex
    python scripts/smoke_test_mathpix.py path/to/lecture_01.pdf
    python scripts/smoke_test_mathpix.py path/to/lecture_01.pdf --out _cache/smoke_test/

Prints status transitions during polling (main pdf status, then md.zip
conversion-readiness status), then a summary of what process_pdf() wrote
to disk. See AGENTS.md issue #6 notes for the on_status callback
conventions this relies on.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running directly as `python scripts/smoke_test_mathpix.py` from the
# repo root without needing the repo installed as a package -- sys.path[0]
# is this script's own directory (scripts/), not the repo root, so `src` is
# not importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from src.config import ConfigError
from src.mathpix import MathpixError, ProcessResult, process_pdf

DEFAULT_OUT_DIR = Path("_cache/smoke_test")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual smoke test: submit a real PDF to the real Mathpix API, "
            "poll to completion, and write the resulting Markdown/figures "
            "to disk for manual review. Costs money per run."
        )
    )
    parser.add_argument("pdf_path", type=Path, help="Path to the lecture PDF to process.")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Output directory for the cached Markdown/figures (default: {DEFAULT_OUT_DIR}).",
    )
    return parser.parse_args(argv)


def _print_status(
    stage: str,
    attempt: int,
    max_poll_attempts: int,
    status: str | None,
    payload: dict,
) -> None:
    print(f"  [{stage} poll {attempt}/{max_poll_attempts}] status={status}")


def _print_summary(result: ProcessResult) -> None:
    print()
    print("Done.")
    print(f"  pdf_id:        {result.pdf_id}")
    print(f"  markdown_path: {result.markdown_path}")
    if result.figures_dir is not None:
        print(f"  figures_dir:   {result.figures_dir} ({result.figure_count} figure(s))")
    else:
        print("  figures_dir:   (none -- no figures found)")
    print(f"  processed_at:  {result.processed_at.isoformat()}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.pdf_path.is_file():
        print(f"ERROR: PDF not found: {args.pdf_path}", file=sys.stderr)
        return 1

    print(f"Submitting {args.pdf_path} to Mathpix...")
    print("Polling for completion (this may take a while for longer PDFs)...")

    try:
        result = process_pdf(args.pdf_path, args.out, on_status=_print_status)
    except (ConfigError, MathpixError, httpx.HTTPStatusError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
