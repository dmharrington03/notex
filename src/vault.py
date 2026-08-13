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
    - write_lecture_note()'s previous_content_hash param / manual-edit
      conflict detection   implemented (issue #40)
    - write_lecture_note()'s force_overwrite param   implemented (issue #45)
    - write_lecture_note()'s on_figure_copy param   implemented (issue #48)

Deliberately no config.py reading here — dark_mode/tags/date_format/
lecture_prefix are taken as plain params (same precedent as
src/figures.py's rewrite_image_references()'s dark_mode param); real
output.figures_dark_mode_flag/course_tags/date_format/naming.lecture_prefix
config wiring is src/main.py's job (#37). Likewise, wiring this function
into src/main.py's run() and state.db's vault_status/vault_path columns
are separate issues (#30/#31), not this module's job.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

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

    - output_path: the vault Markdown file this call targets, e.g.
      vault/{course}/Lecture 02.md -- always set, even when written is
      False (a skipped conflict still targets this path, it's just not
      overwritten).
    - delimiter_warnings: postprocess.scan_delimiter_issues()'s warning
      strings for the rewritten body -- empty if no issues were found, or
      if the write was skipped due to a conflict (issue #40; nothing was
      read/rewritten in that case, so there's nothing to scan).
      Printing these (if desired) is the caller's job, not this function's.
    - figures_copied: figures.copy_figures_to_vault()'s return value --
      empty if the source lecture had no cached figures, or if the write
      was skipped due to a conflict (issue #40; figures are never touched
      when a conflict is detected).
    - written: whether output_path was actually (over)written this call.
      False only when force_overwrite is False, previous_content_hash was
      given, output_path already existed on disk, and its current content
      hash didn't match -- i.e. a manually-edited vault note was detected
      (issue #40). force_overwrite=True (issue #45) bypasses this check
      entirely, so written is always True on that path (barring a real
      I/O error).
    - content_hash: the SHA-256 hex digest of the content just written,
      when written is True. None when written is False (nothing was
      written this call, so there's no new hash to report).
    """

    output_path: Path
    delimiter_warnings: list[str]
    figures_copied: list[Path]
    written: bool = True
    content_hash: str | None = None


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
    previous_content_hash: str | None = None,
    force_overwrite: bool = False,
    on_figure_copy: Callable[[Path], None] | None = None,
    image_link_syntax: str = "markdown",
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
        previous_content_hash: SHA-256 hex digest of the content this
            source file's vault note held the last time this function
            successfully wrote it (state.db's vault_content_hash column,
            resolved by the caller -- src/main.py, issue #40). When None
            (the default -- no baseline recorded, e.g. a fresh state.db or
            a file never previously vault-written), the write proceeds
            unconditionally, matching this function's pre-#40 behavior.
            When given and the target output_path already exists on disk,
            its current byte content is hashed and compared: a match means
            no manual edits happened since our last write (proceed with
            the overwrite as normal); a mismatch means the vault file was
            manually edited and is left untouched -- no figure copy, no
            content read/rewrite, nothing under vault/{course}/... is
            touched at all for this call (see VaultWriteResult.written).
        force_overwrite: when True (issue #45), bypasses the
            previous_content_hash conflict check entirely -- the write
            proceeds unconditionally, exactly as if previous_content_hash
            were None, regardless of what previous_content_hash actually
            is or whether it would otherwise mismatch. This is the escape
            hatch for a manually-edited vault note the caller has decided
            the pipeline's version should overwrite. Defaults to False
            (issue #40's conflict-preserving behavior is unaffected unless
            a caller explicitly opts in).
        on_figure_copy: optional observability hook (issue #48), forwarded
            verbatim to figures.copy_figures_to_vault()'s own same-named
            on_copy param -- called once per copied figure file with its
            destination Path. Never called at all when the write is
            skipped due to a detected conflict (figures aren't touched in
            that case either).
        image_link_syntax: forwarded to figures.rewrite_image_references()
            (issue #54) -- "markdown" (the default) keeps standard
            ![alt](path) syntax, "obsidian" rewrites to ![[path]] wikilink
            form, which tolerates whitespace in path where CommonMark does
            not. Plain param, not read from config.yaml -- real production
            callers pass output.image_link_syntax from config.yaml (see
            src/config.py's load_output_config()), threaded through by
            src/main.py.

    Returns:
        A VaultWriteResult recording the target output_path, the
        delimiter-balance warnings for the rewritten body (empty if the
        write was skipped), the list of figure files copied (empty if the
        write was skipped), whether the write actually happened, and the
        SHA-256 hash of the newly-written content (None if the write was
        skipped).

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
    output_path = (
        vault_course_dir / f"{lecture_prefix} {info.lecture_number:02d}.md"
    )

    if not force_overwrite and previous_content_hash is not None and output_path.exists():
        current_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
        if current_hash != previous_content_hash:
            # Manually edited since our last write -- skip the write
            # entirely (issue #40). Nothing under vault/{course}/... is
            # touched: no figure copy, no content read/rewrite.
            return VaultWriteResult(
                output_path=output_path,
                delimiter_warnings=[],
                figures_copied=[],
                written=False,
                content_hash=None,
            )

    figures_copied = copy_figures_to_vault(
        course_cache_figures_dir, vault_course_dir / "figures", on_copy=on_figure_copy
    )

    raw_text = content_source_path.read_text(encoding="utf-8")
    rewritten_body = rewrite_image_references(
        raw_text, dark_mode=dark_mode, image_link_syntax=image_link_syntax
    )

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
    output_path.write_text(full_content, encoding="utf-8")
    content_hash = hashlib.sha256(full_content.encode("utf-8")).hexdigest()

    return VaultWriteResult(
        output_path=output_path,
        delimiter_warnings=delimiter_warnings,
        figures_copied=figures_copied,
        written=True,
        content_hash=content_hash,
    )
