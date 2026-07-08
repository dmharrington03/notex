"""
Unit tests for src/vault.py (issue #29).

Covered here:
    - zero-figure case: content written correctly, figures_copied == [],
      no figures/ dir created
    - with-figures case: figures copied, figure references rewritten with
      numbered captions
    - dark_mode=True propagates the "@darkmode" marker into the written
      content
    - rerun idempotency: same deterministic output path overwritten, no
      duplicate files
    - unparseable source filename propagates PostprocessError uncaught
    - delimiter_warnings populated when the rewritten body has a balance
      issue, empty when clean
    - custom tags round-trip into frontmatter; omitted tags falls back to
      DEFAULT_TAGS
    - frontmatter is prepended before the rewritten body

All tmp_path-backed, no mocking, no network (matches tests/test_figures.py's
precedent).
"""

from datetime import datetime, timezone

import pytest
import yaml

from src.postprocess import DEFAULT_TAGS, PostprocessError
from src.vault import VaultWriteResult, write_lecture_note


def _make_content_file(tmp_path, text):
    content_path = tmp_path / "lecture_02.llm.md"
    content_path.write_text(text, encoding="utf-8")
    return content_path


def test_zero_figure_case_writes_content_no_figures_dir(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Some cleaned notes.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"  # doesn't exist
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15, 10, 30).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    assert result.output_path == vault_course_dir / "Lecture 02.md"
    assert result.output_path.is_file()
    assert result.figures_copied == []
    assert not (vault_course_dir / "figures").exists()

    written = result.output_path.read_text(encoding="utf-8")
    assert written.startswith("---\n")
    assert "Some cleaned notes.\n" in written


def test_with_figures_copies_and_rewrites_references(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(
        tmp_path, "Notes.\n\n![](figures/lecture_02_fig_001.jpg)\n"
    )
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fake-jpeg")
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    expected_fig = vault_course_dir / "figures" / "lecture_02_fig_001.jpg"
    assert result.figures_copied == [expected_fig]
    assert expected_fig.read_bytes() == b"fake-jpeg"

    written = result.output_path.read_text(encoding="utf-8")
    assert "![Figure 1](figures/lecture_02_fig_001.jpg)" in written


def test_dark_mode_marker_propagated(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(
        tmp_path, "![](figures/lecture_02_fig_001.jpg)\n"
    )
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fake-jpeg")
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        dark_mode=True,
    )

    written = result.output_path.read_text(encoding="utf-8")
    assert "![Figure 1 @darkmode](figures/lecture_02_fig_001.jpg)" in written


def test_rerun_overwrites_cleanly_no_duplication(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "First version.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    first = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )
    assert "First version." in first.output_path.read_text(encoding="utf-8")

    content_path.write_text("Second version.\n", encoding="utf-8")
    second = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    assert second.output_path == first.output_path
    written = second.output_path.read_text(encoding="utf-8")
    assert "Second version." in written
    assert "First version." not in written
    assert len(list(vault_course_dir.glob("*.md"))) == 1


def test_unparseable_filename_propagates_postprocess_error(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "scratch_notes.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Notes.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    with pytest.raises(PostprocessError):
        write_lecture_note(
            source_pdf_path=source_pdf,
            content_source_path=content_path,
            course_cache_figures_dir=cache_figures_dir,
            vault_course_dir=vault_course_dir,
            source_mtime=datetime(2024, 1, 15).timestamp(),
            processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        )


def test_delimiter_warnings_populated_on_balance_issue(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Unbalanced $x = 1 sign.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    assert len(result.delimiter_warnings) == 1
    assert "$" in result.delimiter_warnings[0]


def test_delimiter_warnings_empty_when_balanced(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Balanced $x = 1$ sign.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    assert result.delimiter_warnings == []


def test_custom_tags_round_trip_into_frontmatter(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Notes.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        tags=["lecture-notes", "quantum-mechanics"],
    )

    written = result.output_path.read_text(encoding="utf-8")
    frontmatter_body = written.split("---\n")[1]
    data = yaml.safe_load(frontmatter_body)
    assert data["tags"] == ["lecture-notes", "quantum-mechanics"]


def test_omitted_tags_falls_back_to_default_tags(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Notes.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    written = result.output_path.read_text(encoding="utf-8")
    frontmatter_body = written.split("---\n")[1]
    data = yaml.safe_load(frontmatter_body)
    assert data["tags"] == list(DEFAULT_TAGS)


def test_frontmatter_prepended_before_body(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Body content here.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    written = result.output_path.read_text(encoding="utf-8")
    frontmatter_end = written.index("---\n", 4) + len("---\n")
    assert written[:4] == "---\n"
    assert written[frontmatter_end:] == "Body content here.\n"


def test_output_filename_uses_zero_padded_lecture_number(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_2.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Notes.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    assert result.output_path.name == "Lecture 02.md"


def test_result_is_vault_write_result_instance(tmp_path):
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "Notes.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )

    assert isinstance(result, VaultWriteResult)
