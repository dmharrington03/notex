"""
Manual conversion mode — hits the REAL Mathpix + LLM APIs and costs money
per run. Issue #50.

Not part of the pytest suite. Runs a single, arbitrary source PDF through
the full pipeline (Mathpix OCR -> LLM cleanup -> figure copy -> frontmatter
-> final vault Markdown) to an exact destination `.md` path given on the
command line, for one-off conversions outside the normal indexed corpus
(e.g. a PDF that isn't under `paths.input_root`, or doesn't follow the
`lecture_NN...` naming convention `src/discovery.py`/`src/postprocess.py`
expect).

Deliberately stateless: never touches `state.db`, never calls
`src/discovery.py`. Reuses the same building blocks
`src/main.py`/`src/vault.py` use for the real pipeline
(`process_pdf()` / `cleanup_pdf()` / `copy_figures_to_vault()` /
`rewrite_image_references()` / `scan_delimiter_issues()`), but does NOT call
`src/vault.py`'s `write_lecture_note()` or `src/postprocess.py`'s
`parse_lecture_filename()`/`build_frontmatter()` directly — both assume a
source PDF living under a course folder and named `lecture_NN...`, neither
of which holds for an arbitrary manual conversion:

    - `write_lecture_note()` derives its own output filename from the
      parsed lecture number; it has no way to target an arbitrary
      caller-supplied destination path.
    - `parse_lecture_filename()` requires a `lecture[_-]?<digits>` match in
      the source filename and derives `course_name` from the immediate
      parent folder — neither is guaranteed here.

Course name / lecture number / tags are instead supplied via CLI flags
(`--course`, `--lecture-number`, `--tags`), all optional — a frontmatter
field is included only when its corresponding flag is given (see
`_build_manual_frontmatter()` below), rather than falling back to a parsed
or empty value. `output.course_tags` is deliberately never consulted here
(no real course-folder key to look it up by) — `--tags` is the only tag
source.

Since there's no `state.db` entry to compare against, there is nothing for
issue #40's manual-vault-edit conflict detection to check — this script
always overwrites `dest_path` unconditionally. This is intentional: the
user named this exact destination file explicitly on the command line,
unlike the indexed pipeline's automatic path selection.

Figures land in `dest_path.parent / "figures"`, mirroring the normal
per-course vault layout's `Lecture NN.md` + `figures/` sibling-directory
convention, just anchored at the destination file's own parent instead of
a course directory.

The intermediate Mathpix/LLM cache (the `.mathpix.md`/`.llm.md`/cache-side
figures) is written to a fresh temporary directory and deleted after the
run (`--keep-cache` to retain it for debugging).

Usage:
    conda activate notex
    python scripts/manual_convert.py path/to/some_notes.pdf vault/Misc/Some Notes.md
    python scripts/manual_convert.py path/to/some_notes.pdf vault/Misc/Some\\ Notes.md \\
        --course "18.06 Linear Algebra" --lecture-number 3 --tags lecture-notes,math
"""

from __future__ import annotations

import argparse
import dataclasses
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Allow running directly as `python scripts/manual_convert.py` from the repo
# root without needing the repo installed as a package -- sys.path[0] is
# this script's own directory (scripts/), not the repo root, so `src` is not
# importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import yaml

from src.config import (
    ConfigError,
    load_llm_config,
    load_mathpix_polling_config,
    load_naming_config,
    load_output_config,
)
from src.figures import copy_figures_to_vault, rewrite_image_references
from src.llm import LLMError, LLMResult, cleanup_pdf
from src.mathpix import MathpixError, ProcessResult, process_pdf
from src.postprocess import scan_delimiter_issues


def _build_manual_frontmatter(
    course: str | None,
    lecture_number: int | None,
    tags: tuple[str, ...],
    source_pdf_path: Path,
    source_mtime: float,
    processed_at: datetime,
    date_format: str,
    lecture_prefix: str,
) -> str:
    """
    Assemble this script's own YAML frontmatter block.

    Deliberately not `src.postprocess.build_frontmatter()` -- that
    function's `course_name`/`lecture_number` params are required (`str`/
    `int`, not optional) and always render `title`/`course`/`lecture_number`
    keys unconditionally. Here, each of those is rendered only when the
    corresponding CLI flag was actually given; `date`/`source_pdf`/
    `processed` are always present since they're derived from the source
    file/run itself, not a CLI flag. Field order otherwise matches
    `build_frontmatter()`'s convention (title/course/date/lecture_number/
    tags/source_pdf/processed) for consistency with the rest of the vault.
    """
    data: dict[str, object] = {}

    if lecture_number is not None:
        data["title"] = f"{lecture_prefix} {lecture_number:02d}"
    if course is not None:
        data["course"] = course

    data["date"] = datetime.fromtimestamp(source_mtime).strftime(date_format)

    if lecture_number is not None:
        data["lecture_number"] = lecture_number
    if tags:
        data["tags"] = list(tags)

    data["source_pdf"] = str(source_pdf_path.resolve())
    data["processed"] = processed_at.astimezone().strftime(date_format)

    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return f"---\n{body}---\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    # Loaded here (not just in main()) so --dark-mode's default can reflect
    # config.yaml's output.figures_dark_mode_flag in --help text.
    # load_output_config() is fully optional and never raises.
    output_config = load_output_config()

    parser = argparse.ArgumentParser(
        description=(
            "Manual conversion: run one PDF through the real Mathpix + LLM "
            "pipeline straight through to a vault Markdown file at an exact "
            "destination path. Stateless -- never touches state.db, never "
            "calls discovery.py. Costs money per run."
        )
    )
    parser.add_argument("pdf_path", type=Path, help="Path to the source PDF to convert.")
    parser.add_argument(
        "dest_path",
        type=Path,
        help="Exact destination .md file path to write (created, parents included).",
    )
    parser.add_argument(
        "--course",
        default=None,
        help=(
            "Course name for the frontmatter's 'course' field. Omitted "
            "entirely from the frontmatter if not given."
        ),
    )
    parser.add_argument(
        "--lecture-number",
        type=int,
        default=None,
        help=(
            "Lecture number for the frontmatter's 'title'/'lecture_number' "
            "fields. Omitted entirely from the frontmatter if not given."
        ),
    )
    parser.add_argument(
        "--tags",
        default=None,
        help=(
            "Comma-separated tags for the frontmatter's 'tags' field (e.g. "
            "'lecture-notes,math'). Omitted entirely if not given -- "
            "output.course_tags from config.yaml is never consulted here."
        ),
    )
    parser.add_argument(
        "--prompt-version",
        default=None,
        help=(
            "Override config.yaml's llm.prompt_version for this run only. "
            "Does not modify config.yaml."
        ),
    )
    parser.add_argument(
        "--dark-mode",
        action=argparse.BooleanOptionalAction,
        default=output_config.figures_dark_mode_flag,
        help=(
            "Append '@darkmode' to every figure caption. Defaults to "
            f"config.yaml's output.figures_dark_mode_flag "
            f"(currently {output_config.figures_dark_mode_flag})."
        ),
    )
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help=(
            "Do not delete the temporary Mathpix/LLM cache directory "
            "(.mathpix.md/.llm.md/cache-side figures) after the run -- "
            "useful for debugging a bad conversion."
        ),
    )
    return parser.parse_args(argv)


def _print_mathpix_status(
    stage: str,
    attempt: int,
    max_poll_attempts: int,
    status: str | None,
    _payload: dict,
) -> None:
    # _payload is unused -- required by src.mathpix.OnStatusCallback's fixed
    # (stage, attempt, max_poll_attempts, status, payload) signature, which
    # process_pdf() always calls positionally with all five args (same
    # unused-payload shape as scripts/smoke_test_mathpix.py's own
    # _print_status()).
    print(f"  [mathpix {stage} poll {attempt}/{max_poll_attempts}] status={status}")


def _print_llm_status(message: str) -> None:
    print(f"  [llm] {message}")


def _print_summary(
    process_result: ProcessResult,
    llm_result: LLMResult,
    figures_copied: list[Path],
    delimiter_warnings: list[str],
    dest_path: Path,
) -> None:
    print()
    print("Done.")
    print(f"  pdf_id:              {process_result.pdf_id}")
    print(f"  page_count:          {process_result.page_count}")
    print(f"  figure_count:        {process_result.figure_count}")
    print(f"  figures copied:      {len(figures_copied)}")
    print(f"  llm_status:          {llm_result.llm_status}")
    print(f"  llm_model:           {llm_result.llm_model}")
    print(f"  llm_input_tokens:    {llm_result.llm_input_tokens}")
    print(f"  llm_output_tokens:   {llm_result.llm_output_tokens}")
    cost = llm_result.llm_cost_estimate
    print(f"  llm_cost_estimate:   {f'${cost:.4f}' if cost is not None else 'unknown'}")
    if delimiter_warnings:
        print(f"  delimiter warnings ({len(delimiter_warnings)}):")
        for warning in delimiter_warnings:
            print(f"    - {warning}")
    else:
        print("  delimiter warnings:  none")
    print(f"  written:             {dest_path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.pdf_path.is_file():
        print(f"ERROR: PDF not found: {args.pdf_path}", file=sys.stderr)
        return 1

    tags = tuple(t.strip() for t in args.tags.split(",") if t.strip()) if args.tags else ()

    llm_config = load_llm_config()
    if args.prompt_version is not None:
        llm_config = dataclasses.replace(llm_config, prompt_version=args.prompt_version)
    polling_config = load_mathpix_polling_config()
    output_config = load_output_config()
    naming_config = load_naming_config()

    cache_dir = Path(tempfile.mkdtemp(prefix="notex_manual_"))

    process_result: ProcessResult | None = None
    llm_result: LLMResult | None = None
    figures_copied: list[Path] = []
    delimiter_warnings: list[str] = []

    try:
        print(f"Submitting {args.pdf_path} to Mathpix...")
        print("Polling for completion (this may take a while for longer PDFs)...")
        process_result = process_pdf(
            args.pdf_path,
            cache_dir,
            poll_interval_seconds=polling_config.poll_interval_seconds,
            max_poll_attempts=polling_config.max_poll_attempts,
            on_status=_print_mathpix_status,
        )

        print("Running LLM cleanup...")
        llm_result = cleanup_pdf(
            process_result.markdown_path,
            cache_dir,
            args.dest_path.stem,
            llm_config,
            on_status=_print_llm_status,
        )

        vault_figures_dir = args.dest_path.parent / "figures"
        figures_copied = copy_figures_to_vault(cache_dir / "figures", vault_figures_dir)

        raw_text = llm_result.output_path.read_text(encoding="utf-8")
        rewritten_body = rewrite_image_references(raw_text, dark_mode=args.dark_mode)
        delimiter_warnings = scan_delimiter_issues(rewritten_body)

        frontmatter = _build_manual_frontmatter(
            course=args.course,
            lecture_number=args.lecture_number,
            tags=tags,
            source_pdf_path=args.pdf_path,
            source_mtime=args.pdf_path.stat().st_mtime,
            processed_at=llm_result.processed_at,
            date_format=output_config.date_format,
            lecture_prefix=naming_config.lecture_prefix,
        )

        full_content = frontmatter + rewritten_body
        args.dest_path.parent.mkdir(parents=True, exist_ok=True)
        args.dest_path.write_text(full_content, encoding="utf-8")
    except (
        ConfigError,
        MathpixError,
        httpx.HTTPStatusError,
        FileNotFoundError,
        LLMError,
        OSError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep_cache:
            print(f"Cache retained at: {cache_dir}")
        else:
            shutil.rmtree(cache_dir, ignore_errors=True)

    assert process_result is not None and llm_result is not None
    _print_summary(process_result, llm_result, figures_copied, delimiter_warnings, args.dest_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
