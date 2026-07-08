"""
Post-processing (vault-facing) — Phase 5, issue #27.

First half of Phase 5's post-processing stage (docs/spec.md Stage 5): parse
lecture metadata (lecture number, course name, optional topic) from a source
PDF's path, and build the YAML frontmatter block to prepend to the final
vault Markdown.

    - PostprocessError        base exception for this module
    - LectureFileInfo         result of parse_lecture_filename()
    - parse_lecture_filename()  regex-parses lecture_NN[_topic] out of a
                                 source PDF's filename; derives course name
                                 from its immediate parent folder
    - build_frontmatter()     assembles the "---\\n...\\n---\\n" YAML block

Implementation status:
    - parse_lecture_filename() / build_frontmatter()   implemented (issue #27)
    - scan_delimiter_issues() (warn-only delimiter-balance scan)   not yet
      implemented — issue #28
    - Actually writing a file into the vault is src/vault.py's job (issue #29),
      not this module's — this module stays a pure, no-I/O (other than the
      read-only Path operations below) building-block layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

# Matches lecture_01.pdf / lecture_1.pdf / Lecture_02_eigenvalues.pdf /
# lecture-03.pdf, case-insensitively — a "lecture" prefix (underscore or
# hyphen separator, or none), a number, and an optional trailing "_<topic>"
# segment. Applied via .search() against the PDF path's filename stem (see
# parse_lecture_filename() below).
_LECTURE_FILENAME_RE = re.compile(r"lecture[_-]?(\d+)(?:_(.+))?", re.IGNORECASE)

# The output-filename prefix ("Lecture NN") is a hardcoded default for now —
# naming.lecture_prefix's real config wiring is Phase 6.
DEFAULT_LECTURE_PREFIX = "Lecture"

# Phase 5 stand-in for output.base_tags/course_tags, which aren't wired to
# any config reader until Phase 6. A tuple (not a list) deliberately, so it
# can safely serve as build_frontmatter()'s default parameter value without
# the classic mutable-default-argument pitfall.
DEFAULT_TAGS: tuple[str, ...] = ("lecture-notes",)

# output.date_format's real config wiring is likewise Phase 6 — hardcoded
# for now.
DATE_FORMAT = "%Y-%m-%d"


class PostprocessError(Exception):
    """
    Raised when a source PDF's filename can't be parsed for lecture
    metadata (no lecture[_-]?<number> match) — a real, unrecoverable error
    for that file rather than something to silently guess around. See
    AGENTS.md's "Deliberate correction to docs/spec.md's Error Handling
    table" for how callers (src/vault.py / src/main.py, issue #29/#31) are
    expected to handle this: caught per-file, vault_status="failed"
    recorded, without touching that file's already-recorded
    mathpix_status/llm_status.
    """


@dataclass(frozen=True)
class LectureFileInfo:
    """Result of parse_lecture_filename()."""

    lecture_number: int
    course_name: str
    topic: str | None


def parse_lecture_filename(pdf_path: str | Path) -> LectureFileInfo:
    """
    Parse lecture metadata out of a source PDF's path.

    Args:
        pdf_path: path to the source PDF, e.g.
            notes_raw/class_1/lecture_02_eigenvalues.pdf.

    Returns:
        A LectureFileInfo with:
            - lecture_number: parsed from a lecture[_-]?<digits> match
              against the filename stem (case-insensitive). Leading zeros
              are dropped (int("01") == 1) — re-applied via :02d formatting
              wherever a zero-padded title is needed (see
              build_frontmatter()).
            - course_name: the PDF's immediate parent folder name, with
              underscores replaced by spaces (e.g. "class_1" -> "class 1").
            - topic: the trailing "_<topic>" segment after the lecture
              number, if present (e.g. "lecture_02_eigenvalues.pdf" ->
              "eigenvalues"), else None. Not currently consumed by
              build_frontmatter() — captured for future use.

    Raises:
        PostprocessError: if the filename stem has no
            lecture[_-]?<digits> match at all.
    """
    pdf_path = Path(pdf_path)
    match = _LECTURE_FILENAME_RE.search(pdf_path.stem)
    if match is None:
        raise PostprocessError(
            f"Could not parse a lecture number from filename: {pdf_path}"
        )

    lecture_number = int(match.group(1))
    topic = match.group(2)
    course_name = pdf_path.parent.name.replace("_", " ")

    return LectureFileInfo(
        lecture_number=lecture_number,
        course_name=course_name,
        topic=topic,
    )


def build_frontmatter(
    course_name: str,
    lecture_number: int,
    topic: str | None,
    source_pdf_path: str | Path,
    source_mtime: float,
    processed_at: datetime,
    tags: tuple[str, ...] | list[str] = DEFAULT_TAGS,
) -> str:
    """
    Assemble the YAML frontmatter block for a vault lecture note.

    Args:
        course_name: e.g. "class 1" (see parse_lecture_filename()).
        lecture_number: e.g. 2 (see parse_lecture_filename()).
        topic: accepted for signature forward-compatibility with
            parse_lecture_filename()'s LectureFileInfo.topic, but not
            currently included in the rendered frontmatter — see
            AGENTS.md's Phase 5 notes.
        source_pdf_path: path to the source PDF; rendered as its absolute,
            resolved form (matches state.db's source_path convention
            elsewhere in the codebase).
        source_mtime: the source PDF's filesystem mtime (raw
            os.stat().st_mtime float, matching state.db's source_mtime
            column / discovery.py's ClassificationResult.source_mtime).
            Rendered as the "date" field, formatted in *local* time (not
            UTC) since this is a human-facing "what day was this lecture"
            date, not a machine timestamp.
        processed_at: the vault-write timestamp. Rendered as the
            "processed" field, also formatted in local time —
            .astimezone() normalizes both a tz-aware (e.g. UTC, matching
            LLMResult.processed_at's convention) and a naive datetime to
            local time before formatting, so "date"/"processed" are always
            consistent local-time calendar dates regardless of what
            tzinfo processed_at happens to carry.
        tags: defaults to DEFAULT_TAGS (("lecture-notes",)) — the Phase 5
            stand-in for real output.base_tags/course_tags config wiring
            (Phase 6).

    Returns:
        A complete "---\\n...\\n---\\n" YAML frontmatter block with keys in
        the order title/course/date/lecture_number/tags/source_pdf/processed,
        matching docs/spec.md's Stage 5 frontmatter schema.
    """
    title = f"{DEFAULT_LECTURE_PREFIX} {lecture_number:02d}"
    date = datetime.fromtimestamp(source_mtime).strftime(DATE_FORMAT)
    processed = processed_at.astimezone().strftime(DATE_FORMAT)
    source_pdf = str(Path(source_pdf_path).resolve())

    data = {
        "title": title,
        "course": course_name,
        "date": date,
        "lecture_number": lecture_number,
        "tags": list(tags),
        "source_pdf": source_pdf,
        "processed": processed,
    }

    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return f"---\n{body}---\n"
