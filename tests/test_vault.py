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
      no tags at all (empty list)
    - frontmatter is prepended before the rewritten body
    - custom lecture_prefix reflected in both the output filename and the
      written frontmatter's title, confirming they match (issue #36)
    - custom date_format reflected in the written frontmatter's date field
      (issue #37)
    - previous_content_hash conflict detection (issue #40): a mismatched
      hash against an existing on-disk vault file skips the write entirely
      (no figure copy, content preserved); a matching hash overwrites
      normally; no baseline (None) with a pre-existing file overwrites,
      matching pre-#40 behavior
    - force_overwrite=True (issue #45): bypasses the previous_content_hash
      conflict check entirely, overwriting a manually-edited vault file
      unconditionally -- the default (force_overwrite=False) leaves
      issue #40's conflict-preserving behavior completely unaffected

All tmp_path-backed, no mocking, no network (matches tests/test_figures.py's
precedent).
"""

import hashlib
from datetime import datetime, timezone

import pytest
import yaml

from src.postprocess import PostprocessError
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


def test_on_figure_copy_forwarded_to_copy_figures_to_vault(tmp_path):
    """Issue #48: on_figure_copy is forwarded verbatim to
    figures.copy_figures_to_vault()'s on_copy param, firing once per
    copied figure file."""
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

    copied_paths = []
    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        on_figure_copy=copied_paths.append,
    )

    assert copied_paths == result.figures_copied
    assert len(copied_paths) == 1


def test_on_figure_copy_not_called_when_conflict_skips_write(tmp_path):
    """Issue #48: on_figure_copy must never fire when the write is skipped
    due to a detected conflict (issue #40) -- figures aren't touched at
    all in that case."""
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "New pipeline content.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fake-jpeg")
    vault_course_dir = tmp_path / "vault" / "class_1"
    vault_course_dir.mkdir(parents=True)

    manual_content = "Manually edited notes -- do not clobber!\n"
    output_path = vault_course_dir / "Lecture 02.md"
    output_path.write_text(manual_content, encoding="utf-8")
    stale_hash = hashlib.sha256(b"original pipeline content").hexdigest()

    copied_paths = []
    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        previous_content_hash=stale_hash,
        on_figure_copy=copied_paths.append,
    )

    assert result.written is False
    assert copied_paths == []


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


def test_omitted_tags_falls_back_to_no_tags(tmp_path):
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
    assert data["tags"] == []


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


def test_custom_lecture_prefix_reflected_in_filename_and_title(tmp_path):
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
        lecture_prefix="Lec",
    )

    assert result.output_path.name == "Lec 02.md"

    written = result.output_path.read_text(encoding="utf-8")
    frontmatter_body = written.split("---\n")[1]
    data = yaml.safe_load(frontmatter_body)
    assert data["title"] == "Lec 02"


def test_custom_date_format_reflected_in_frontmatter(tmp_path):
    """Issue #37: write_lecture_note()'s date_format param is forwarded
    verbatim to postprocess.build_frontmatter()'s same-named param, used
    for both the "date" and "processed" fields."""
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
        date_format="%d/%m/%Y",
    )

    written = result.output_path.read_text(encoding="utf-8")
    frontmatter_body = written.split("---\n")[1]
    data = yaml.safe_load(frontmatter_body)
    assert data["date"] == "15/01/2024"


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


def test_conflict_detected_skips_write_entirely(tmp_path):
    """Issue #40: a previous_content_hash that doesn't match the existing
    vault file's current on-disk content means a manual edit happened --
    the write must be skipped entirely, with figures untouched and the
    manually-edited content preserved."""
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "New pipeline content.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fake-jpeg")
    vault_course_dir = tmp_path / "vault" / "class_1"
    vault_course_dir.mkdir(parents=True)

    # Simulate a vault file the user manually edited after a prior write --
    # its content no longer matches the hash we last recorded for it.
    manual_content = "Manually edited notes -- do not clobber!\n"
    output_path = vault_course_dir / "Lecture 02.md"
    output_path.write_text(manual_content, encoding="utf-8")
    stale_hash = hashlib.sha256(b"original pipeline content").hexdigest()

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        previous_content_hash=stale_hash,
    )

    assert result.written is False
    assert result.content_hash is None
    assert result.output_path == output_path
    assert result.delimiter_warnings == []
    assert result.figures_copied == []
    # The manual edit must survive untouched.
    assert output_path.read_text(encoding="utf-8") == manual_content
    # Figures must never be copied/overwritten on a conflict.
    assert not (vault_course_dir / "figures").exists()


def test_force_overwrite_bypasses_conflict_detection(tmp_path):
    """Issue #45: force_overwrite=True bypasses the previous_content_hash
    conflict check entirely -- a manually-edited vault note is overwritten
    unconditionally, exactly as if previous_content_hash were None."""
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "New pipeline content.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fake-jpeg")
    vault_course_dir = tmp_path / "vault" / "class_1"
    vault_course_dir.mkdir(parents=True)

    # Same manually-edited-vault-note setup as
    # test_conflict_detected_skips_write_entirely -- a stale hash that
    # would otherwise be detected as a conflict.
    manual_content = "Manually edited notes -- do not clobber!\n"
    output_path = vault_course_dir / "Lecture 02.md"
    output_path.write_text(manual_content, encoding="utf-8")
    stale_hash = hashlib.sha256(b"original pipeline content").hexdigest()

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        previous_content_hash=stale_hash,
        force_overwrite=True,
    )

    assert result.written is True
    assert result.content_hash is not None
    assert result.output_path == output_path
    # The manual edit is gone -- overwritten with the pipeline's content.
    written = output_path.read_text(encoding="utf-8")
    assert "New pipeline content." in written
    assert "Manually edited notes" not in written
    # Figures are copied normally too, unlike the skipped-conflict path.
    assert (vault_course_dir / "figures" / "lecture_02_fig_001.jpg").is_file()


def test_no_conflict_hash_matches_overwrites_normally(tmp_path):
    """Issue #40: when the existing vault file's hash matches
    previous_content_hash exactly (no manual edits since our last write),
    the write proceeds and overwrites normally."""
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    vault_course_dir = tmp_path / "vault" / "class_1"

    # First write establishes the baseline (no previous_content_hash yet).
    first_content_path = _make_content_file(tmp_path, "First version.\n")
    first = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=first_content_path,
        course_cache_figures_dir=tmp_path / "_cache" / "class_1" / "figures",
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
    )
    assert first.written is True
    assert first.content_hash is not None

    # Second write passes the first write's hash as the baseline -- since
    # nobody touched the vault file since then, it must overwrite normally.
    second_content_path = _make_content_file(tmp_path, "Second version.\n")
    second = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=second_content_path,
        course_cache_figures_dir=tmp_path / "_cache" / "class_1" / "figures",
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        previous_content_hash=first.content_hash,
    )

    assert second.written is True
    assert second.content_hash is not None
    assert second.content_hash != first.content_hash
    written = second.output_path.read_text(encoding="utf-8")
    assert "Second version." in written
    assert "First version." not in written


def test_no_baseline_previous_content_hash_none_overwrites(tmp_path):
    """Issue #40: previous_content_hash=None (no baseline recorded -- e.g.
    a fresh/rebuilt state.db, or a file never previously vault-written)
    always overwrites unconditionally, even if a file already happens to
    exist on disk at the target path -- matching pre-#40 behavior."""
    source_pdf = tmp_path / "notes_raw" / "class_1" / "lecture_02.pdf"
    source_pdf.parent.mkdir(parents=True)
    source_pdf.write_bytes(b"fake-pdf")
    content_path = _make_content_file(tmp_path, "New content.\n")
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_course_dir = tmp_path / "vault" / "class_1"
    vault_course_dir.mkdir(parents=True)

    output_path = vault_course_dir / "Lecture 02.md"
    output_path.write_text("Pre-existing content on disk.\n", encoding="utf-8")

    result = write_lecture_note(
        source_pdf_path=source_pdf,
        content_source_path=content_path,
        course_cache_figures_dir=cache_figures_dir,
        vault_course_dir=vault_course_dir,
        source_mtime=datetime(2024, 1, 15).timestamp(),
        processed_at=datetime(2024, 1, 16, tzinfo=timezone.utc),
        previous_content_hash=None,
    )

    assert result.written is True
    assert result.content_hash is not None
    written = output_path.read_text(encoding="utf-8")
    assert "New content." in written
    assert "Pre-existing content on disk." not in written
