"""
Vault writing (vault-facing) — Phase 5, issue #29.

Assembles and writes the final per-lecture Markdown file into
`vault/{course}/Lecture NN.md`, gluing together Phase 4's figure handling
(`src/figures.py`) with Phase 5's frontmatter/delimiter-scan building blocks
(`src/postprocess.py`):

    - VaultWriteResult      result of write_lecture_note()
    - write_lecture_note()  orchestrates parse_lecture_filename() +
                             copy_figures_to_vault() + rewrite_image_references()
                             + scan_delimiter_issues() + build_frontmatter()
                             into a single written vault Markdown file

Implementation status:
    - write_lecture_note()   implemented (issue #29)
    - write_lecture_note()'s lecture_prefix param   implemented (issue #36)
    - write_lecture_note()'s date_format param   implemented (issue #37;
      build_frontmatter() itself gained this param in #35, but threading it
      through write_lecture_note() was an oversight not caught until #37's
      real config wiring surfaced the missing param)

Deliberately no config.py reading here — dark_mode/tags/date_format/
lecture_prefix are taken as plain params (same precedent as
src/figures.py's rewrite_image_references()'s dark_mode param); real
output.figures_dark_mode_flag/course_tags/date_format/naming.lecture_prefix
config wiring is src/main.py's job (#37). Likewise, wiring this function
into src/main.py's run() and state.db's vault_status/vault_path columns
are separate issues (#30/#31), not this module's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.figures import copy_figures_to_vault, rewrite_image_references
from src.postprocess import (
    DATE_FORMAT,
    DEFAULT_LECTURE_PREFIX,
    build_frontmatter,
    parse_lecture_filename,
    scan_delimiter_issues,
)


@dataclass(frozen=True)
class VaultWriteResult:
    """
    Result of write_lecture_note().

    - output_path: the vault Markdown file actually written, e.g.
      vault/{course}/Lecture 02.md.
    - delimiter_warnings: postprocess.scan_delimiter_issues()'s warning
      strings for the rewritten body -- empty if no issues were found.
      Printing these (if desired) is the caller's job, not this function's.
    - figures_copied: figures.copy_figures_to_vault()'s return value --
      empty if the source lecture had no cached figures.
    """

    output_path: Path
    delimiter_warnings: list[str]
    figures_copied: list[Path]


def write_lecture_note(
    source_pdf_path: str | Path,
    content_source_path: str | Path,
    course_cache_figures_dir: str | Path,
    vault_course_dir: str | Path,
    source_mtime: float,
    processed_at: datetime,
    dark_mode: bool = False,
    tags: list[str] | None = None,
    date_format: str = DATE_FORMAT,
    lecture_prefix: str = DEFAULT_LECTURE_PREFIX,
) -> VaultWriteResult:
    """
    Assemble and write a single lecture's final vault Markdown file.

    Args:
        source_pdf_path: path to the original source PDF (used to derive
            lecture number/course name via postprocess.parse_lecture_filename(),
            and recorded verbatim -- resolved -- in the frontmatter's
            source_pdf field via postprocess.build_frontmatter()).
        content_source_path: path to the content to write -- the LLM-cleaned
            .llm.md, or the raw .mathpix.md fallback if the LLM stage
            failed. Deciding which file to pass is the caller's job (e.g.
            src/main.py, issue #31); this function has no opinion on it.
        course_cache_figures_dir: this course's cached figures dir, e.g.
            _cache/{course}/figures/. Passed straight through to
            figures.copy_figures_to_vault() -- a missing directory is a
            no-op (zero-figure case), not an error.
        vault_course_dir: this course's vault directory, e.g.
            vault/{course}/. Created (parents included) if it doesn't
            already exist. The figures/ subdirectory beneath it is only
            ever created by figures.copy_figures_to_vault() itself, and
            only when there are actual figures to copy -- a zero-figure
            lecture never gets an empty figures/ dir.
        source_mtime: the source PDF's filesystem mtime, forwarded to
            postprocess.build_frontmatter()'s date field.
        processed_at: the vault-write timestamp, forwarded to
            postprocess.build_frontmatter()'s processed field.
        dark_mode: forwarded to figures.rewrite_image_references() --
            appends " @darkmode" to every figure caption when True. Plain
            param, not read from config.yaml (Phase 6).
        tags: forwarded to postprocess.build_frontmatter(). None (the
            default) falls back to no tags at all (an empty tuple) --
            there is no global/default tag list anywhere in this codebase
            (see AGENTS.md's Phase 6 "Scope correction" note); a course
            only gets tags via an explicit output.course_tags entry,
            resolved by postprocess.resolve_tags() and passed in here by
            the caller (src/main.py, Phase 6 config wiring).
        date_format: forwarded verbatim to postprocess.build_frontmatter()'s
            same-named param (used for both the "date" and "processed"
            fields). Defaults to DATE_FORMAT ("%Y-%m-%d") only for callers
            that don't pass this explicitly -- real production callers pass
            output.date_format from config.yaml (see src/config.py's
            load_output_config()), threaded through by src/main.py (#37).
        lecture_prefix: used for both the output filename ("{lecture_prefix}
            NN.md") and forwarded verbatim to postprocess.build_frontmatter()'s
            same-named param (the "title" field) -- the two are never
            independently configurable, so they can never disagree. Defaults
            to DEFAULT_LECTURE_PREFIX ("Lecture") only for callers that don't
            pass this explicitly -- real production callers pass
            naming.lecture_prefix from config.yaml (see src/config.py's
            load_naming_config()), threaded through by src/main.py (#37).

    Returns:
        A VaultWriteResult recording the written output_path, the
        delimiter-balance warnings for the rewritten body, and the list of
        figure files copied.

    Raises:
        PostprocessError: propagated uncaught from
            postprocess.parse_lecture_filename() if source_pdf_path's
            filename has no lecture[_-]?<digits> match. Per issue #27's
            confirmed design, catching this and recording
            vault_status="failed" without touching that file's
            already-recorded mathpix_status/llm_status is the caller's job
            (src/main.py, issue #31), not this function's.
    """
    source_pdf_path = Path(source_pdf_path)
    content_source_path = Path(content_source_path)
    vault_course_dir = Path(vault_course_dir)

    info = parse_lecture_filename(source_pdf_path)

    figures_copied = copy_figures_to_vault(
        course_cache_figures_dir, vault_course_dir / "figures"
    )

    raw_text = content_source_path.read_text(encoding="utf-8")
    rewritten_body = rewrite_image_references(raw_text, dark_mode=dark_mode)

    delimiter_warnings = scan_delimiter_issues(rewritten_body)

    frontmatter = build_frontmatter(
        course_name=info.course_name,
        lecture_number=info.lecture_number,
        topic=info.topic,
        source_pdf_path=source_pdf_path,
        source_mtime=source_mtime,
        processed_at=processed_at,
        tags=tags if tags is not None else (),
        date_format=date_format,
        lecture_prefix=lecture_prefix,
    )

    full_content = frontmatter + rewritten_body

    vault_course_dir.mkdir(parents=True, exist_ok=True)
    output_path = (
        vault_course_dir / f"{lecture_prefix} {info.lecture_number:02d}.md"
    )
    output_path.write_text(full_content, encoding="utf-8")

    return VaultWriteResult(
        output_path=output_path,
        delimiter_warnings=delimiter_warnings,
        figures_copied=figures_copied,
    )
