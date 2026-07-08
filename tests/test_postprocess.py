"""
Unit tests for src/postprocess.py (issue #27).

Covered here:
    - parse_lecture_filename(): standard/single-digit/uppercase/hyphenated/
      with-topic-suffix cases; unparseable filename raises PostprocessError
    - course-name derivation: underscore replacement
    - build_frontmatter(): full field round-trip, default-tags case,
      custom-tags-list case, and topic-is-not-leaked-into-output

All pure-function/tmp_path-backed, no network, no mocking.
"""

from datetime import datetime, timezone

import pytest
import yaml

from src.postprocess import (
    DEFAULT_TAGS,
    LectureFileInfo,
    PostprocessError,
    build_frontmatter,
    parse_lecture_filename,
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
    assert data["tags"] == ["lecture-notes"]
    assert data["source_pdf"] == "/abs/notes_raw/class_1/lecture_02.pdf"
    assert data["processed"] == processed_at.astimezone().strftime("%Y-%m-%d")


def test_build_frontmatter_default_tags():
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
    assert data["tags"] == list(DEFAULT_TAGS)


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
