"""
Unit tests for src/postprocess.py (issues #27, #28, #35).

Covered here:
    - parse_lecture_filename(): standard/single-digit/uppercase/hyphenated/
      with-topic-suffix cases; unparseable filename raises PostprocessError
    - course-name derivation: underscore replacement
    - build_frontmatter(): full field round-trip, omitted-tags-defaults-to-
      no-tags case, custom-tags-list case, custom date_format case, and
      topic-is-not-leaked-into-output
    - resolve_tags(): returns the configured course_tags entry when present,
      and an empty tuple (no fallback) when absent
    - scan_delimiter_issues(): balanced content is a no-op; unbalanced $,
      unbalanced \\left/\\right, and literal \\(...\\)/\\[...\\] each produce
      exactly one warning; a combined case produces multiple warnings

All pure-function/tmp_path-backed, no network, no mocking.
"""

from datetime import datetime, timezone

import pytest
import yaml

from src.config import OutputConfig
from src.postprocess import (
    LectureFileInfo,
    PostprocessError,
    build_frontmatter,
    parse_lecture_filename,
    resolve_tags,
    scan_delimiter_issues,
)


def test_parse_standard_lecture_filename():
    result = parse_lecture_filename("notes_raw/class_1/lecture_02.pdf")

    assert result == LectureFileInfo(
        lecture_number=2, course_name="class 1", topic=None
    )


def test_parse_single_digit_lecture_number():
    result = parse_lecture_filename("notes_raw/class_1/lecture_1.pdf")

    assert result.lecture_number == 1


def test_parse_uppercase_lecture_prefix():
    result = parse_lecture_filename("notes_raw/class_1/Lecture_02.pdf")

    assert result.lecture_number == 2


def test_parse_hyphenated_separator():
    result = parse_lecture_filename("notes_raw/class_1/lecture-03.pdf")

    assert result.lecture_number == 3


def test_parse_with_topic_suffix():
    result = parse_lecture_filename(
        "notes_raw/class_1/Lecture_02_eigenvalues.pdf"
    )

    assert result.lecture_number == 2
    assert result.topic == "eigenvalues"


def test_parse_no_topic_suffix_is_none():
    result = parse_lecture_filename("notes_raw/class_1/lecture_02.pdf")

    assert result.topic is None


def test_parse_course_name_replaces_underscores_with_spaces():
    result = parse_lecture_filename("notes_raw/quantum_mechanics_2/lecture_01.pdf")

    assert result.course_name == "quantum mechanics 2"


def test_parse_unparseable_filename_raises():
    with pytest.raises(PostprocessError):
        parse_lecture_filename("notes_raw/class_1/scratch_notes.pdf")


def test_build_frontmatter_full_round_trip():
    source_mtime = datetime(2024, 1, 15, 10, 30).timestamp()
    processed_at = datetime(2024, 1, 16, 9, 0, tzinfo=timezone.utc)

    result = build_frontmatter(
        course_name="class 1",
        lecture_number=2,
        topic="eigenvalues",
        source_pdf_path="/abs/notes_raw/class_1/lecture_02.pdf",
        source_mtime=source_mtime,
        processed_at=processed_at,
    )

    assert result.startswith("---\n")
    assert result.endswith("---\n")

    body = result[len("---\n") : -len("---\n")]
    data = yaml.safe_load(body)

    assert data["title"] == "Lecture 02"
    assert data["course"] == "class 1"
    assert data["date"] == "2024-01-15"
    assert data["lecture_number"] == 2
    assert data["tags"] == []
    assert data["source_pdf"] == "/abs/notes_raw/class_1/lecture_02.pdf"
    assert data["processed"] == processed_at.astimezone().strftime("%Y-%m-%d")


def test_build_frontmatter_omitted_tags_defaults_to_no_tags():
    result = build_frontmatter(
        course_name="class 1",
        lecture_number=1,
        topic=None,
        source_pdf_path="/abs/notes_raw/class_1/lecture_01.pdf",
        source_mtime=datetime(2024, 1, 1).timestamp(),
        processed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    body = result[len("---\n") : -len("---\n")]
    data = yaml.safe_load(body)
    assert data["tags"] == []


def test_build_frontmatter_custom_tags():
    result = build_frontmatter(
        course_name="class 1",
        lecture_number=1,
        topic=None,
        source_pdf_path="/abs/notes_raw/class_1/lecture_01.pdf",
        source_mtime=datetime(2024, 1, 1).timestamp(),
        processed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        tags=["lecture-notes", "quantum-mechanics"],
    )

    body = result[len("---\n") : -len("---\n")]
    data = yaml.safe_load(body)
    assert data["tags"] == ["lecture-notes", "quantum-mechanics"]


def test_build_frontmatter_custom_date_format():
    source_mtime = datetime(2024, 1, 15, 10, 30).timestamp()
    processed_at = datetime(2024, 1, 16, 9, 0, tzinfo=timezone.utc)

    result = build_frontmatter(
        course_name="class 1",
        lecture_number=1,
        topic=None,
        source_pdf_path="/abs/notes_raw/class_1/lecture_01.pdf",
        source_mtime=source_mtime,
        processed_at=processed_at,
        date_format="%d/%m/%Y",
    )

    body = result[len("---\n") : -len("---\n")]
    data = yaml.safe_load(body)

    assert data["date"] == "15/01/2024"
    assert data["processed"] == processed_at.astimezone().strftime("%d/%m/%Y")


def test_resolve_tags_returns_configured_course_tags():
    output_config = OutputConfig(
        course_tags={"class_1": ("quantum-mechanics", "core")},
        date_format="%Y-%m-%d",
        figures_dark_mode_flag=False,
    )

    assert resolve_tags("class_1", output_config) == ("quantum-mechanics", "core")


def test_resolve_tags_returns_empty_tuple_when_course_absent():
    output_config = OutputConfig(
        course_tags={"class_1": ("quantum-mechanics",)},
        date_format="%Y-%m-%d",
        figures_dark_mode_flag=False,
    )

    assert resolve_tags("class_2", output_config) == ()


def test_resolve_tags_empty_course_tags_dict_returns_empty_tuple():
    output_config = OutputConfig(
        course_tags={},
        date_format="%Y-%m-%d",
        figures_dark_mode_flag=False,
    )

    assert resolve_tags("class_1", output_config) == ()


def test_build_frontmatter_topic_not_included_in_output():
    result = build_frontmatter(
        course_name="class 1",
        lecture_number=1,
        topic="some-topic",
        source_pdf_path="/abs/notes_raw/class_1/lecture_01.pdf",
        source_mtime=datetime(2024, 1, 1).timestamp(),
        processed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    body = result[len("---\n") : -len("---\n")]
    data = yaml.safe_load(body)
    assert "topic" not in data
    assert "some-topic" not in result


def test_build_frontmatter_source_pdf_is_resolved(tmp_path):
    pdf_path = tmp_path / "notes_raw" / "class_1" / "lecture_01.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"fake-pdf")

    result = build_frontmatter(
        course_name="class 1",
        lecture_number=1,
        topic=None,
        source_pdf_path=pdf_path,
        source_mtime=datetime(2024, 1, 1).timestamp(),
        processed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    body = result[len("---\n") : -len("---\n")]
    data = yaml.safe_load(body)
    assert data["source_pdf"] == str(pdf_path.resolve())


def test_scan_delimiter_issues_balanced_content_is_empty():
    text = "Some prose with $x = 1$ and a display block:\n\n$$\ny = 2\n$$\n"

    assert scan_delimiter_issues(text) == []


def test_scan_delimiter_issues_unbalanced_dollar():
    text = "This has an unbalanced $x = 1 sign."

    warnings = scan_delimiter_issues(text)

    assert len(warnings) == 1
    assert "$" in warnings[0]


def test_scan_delimiter_issues_unbalanced_left_right():
    text = r"An expression \left( x + 1 \right) \right] is unbalanced."

    warnings = scan_delimiter_issues(text)

    assert len(warnings) == 1
    assert "\\left" in warnings[0] and "\\right" in warnings[0]


def test_scan_delimiter_issues_ignores_rightarrow_and_leftarrow():
    text = r"$x \rightarrow y$ and $y \leftarrow x$ and $z \leftrightarrow w$"

    assert scan_delimiter_issues(text) == []


def test_scan_delimiter_issues_literal_paren_delimiters():
    text = r"Some inline math \(x = 1\) using the wrong delimiters."

    warnings = scan_delimiter_issues(text)

    assert len(warnings) == 1
    assert r"\(...\)" in warnings[0]


def test_scan_delimiter_issues_literal_bracket_delimiters():
    text = "Display math:\n\\[\nx = 1\n\\]\nusing the wrong delimiters."

    warnings = scan_delimiter_issues(text)

    assert len(warnings) == 1
    assert r"\[...\]" in warnings[0]


def test_scan_delimiter_issues_combined_multiple_issues():
    text = (
        r"Unbalanced $x = 1 and \left( y + 1 \right) \right] "
        r"and \(z = 1\)."
    )

    warnings = scan_delimiter_issues(text)

    assert len(warnings) == 3


def test_scan_delimiter_issues_never_mutates_input():
    text = "Balanced $x$ text."

    scan_delimiter_issues(text)

    assert text == "Balanced $x$ text."
