"""
Unit tests for src/figures.py.

Covered here (issue #23 — figure copy-to-vault function):
    - zero-figure case (missing cache figures/ dir) is a no-op
    - single figure copy
    - multiple figure copy
    - rerun overwrites cleanly (idempotent, no duplication)
    - hidden files in the cache dir are skipped
    - copied file content matches the source byte-for-byte

Also covered (issue #24 — Markdown image-reference caption rewriter):
    - single/multiple ![](figures/...) refs get a numbered "Figure N"
      caption, path left untouched
    - dark_mode=True appends " @darkmode" to every caption
    - the same image referenced twice still gets two distinct, incrementing
      numbers (per-occurrence, not per-path, numbering)
    - non-figures/ content (prose, math, unrelated image refs) is left
      byte-for-byte untouched
    - any pre-existing alt text is discarded, not preserved

All tmp_path-backed, no mocking, no network.
"""

from src.figures import copy_figures_to_vault, rewrite_image_references


def test_missing_cache_dir_is_noop(tmp_path):
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    result = copy_figures_to_vault(cache_figures_dir, vault_figures_dir)

    assert result == []
    assert not vault_figures_dir.exists()


def test_single_figure_copy(tmp_path):
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    fig = cache_figures_dir / "lecture_02_fig_001.jpg"
    fig.write_bytes(b"fake-jpeg-bytes")
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    result = copy_figures_to_vault(cache_figures_dir, vault_figures_dir)

    dest = vault_figures_dir / "lecture_02_fig_001.jpg"
    assert result == [dest]
    assert dest.is_file()
    assert dest.read_bytes() == b"fake-jpeg-bytes"


def test_multiple_figure_copy(tmp_path):
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fig1")
    (cache_figures_dir / "lecture_02_fig_002.jpg").write_bytes(b"fig2")
    (cache_figures_dir / "lecture_03_fig_001.jpg").write_bytes(b"fig3")
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    result = copy_figures_to_vault(cache_figures_dir, vault_figures_dir)

    assert result == [
        vault_figures_dir / "lecture_02_fig_001.jpg",
        vault_figures_dir / "lecture_02_fig_002.jpg",
        vault_figures_dir / "lecture_03_fig_001.jpg",
    ]
    assert (vault_figures_dir / "lecture_02_fig_001.jpg").read_bytes() == b"fig1"
    assert (vault_figures_dir / "lecture_02_fig_002.jpg").read_bytes() == b"fig2"
    assert (vault_figures_dir / "lecture_03_fig_001.jpg").read_bytes() == b"fig3"


def test_rerun_overwrites_cleanly(tmp_path):
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    fig = cache_figures_dir / "lecture_02_fig_001.jpg"
    fig.write_bytes(b"original-bytes")
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    first_result = copy_figures_to_vault(cache_figures_dir, vault_figures_dir)
    assert (vault_figures_dir / "lecture_02_fig_001.jpg").read_bytes() == b"original-bytes"

    # Simulate a reprocessed source file with updated bytes at the same
    # deterministic filename, then rerun.
    fig.write_bytes(b"updated-bytes")
    second_result = copy_figures_to_vault(cache_figures_dir, vault_figures_dir)

    assert first_result == second_result
    assert len(list(vault_figures_dir.iterdir())) == 1
    assert (vault_figures_dir / "lecture_02_fig_001.jpg").read_bytes() == b"updated-bytes"


def test_hidden_files_are_skipped(tmp_path):
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fig1")
    (cache_figures_dir / ".DS_Store").write_bytes(b"junk")
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    result = copy_figures_to_vault(cache_figures_dir, vault_figures_dir)

    assert result == [vault_figures_dir / "lecture_02_fig_001.jpg"]
    assert not (vault_figures_dir / ".DS_Store").exists()


def test_empty_cache_figures_dir_creates_empty_vault_dir(tmp_path):
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    result = copy_figures_to_vault(cache_figures_dir, vault_figures_dir)

    assert result == []
    assert vault_figures_dir.is_dir()
    assert list(vault_figures_dir.iterdir()) == []


def test_on_copy_called_once_per_copied_file(tmp_path):
    """Issue #48 -- on_copy fires once per copied file with its dest Path."""
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fig1")
    (cache_figures_dir / "lecture_02_fig_002.jpg").write_bytes(b"fig2")
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    copied_paths = []
    result = copy_figures_to_vault(
        cache_figures_dir, vault_figures_dir, on_copy=copied_paths.append
    )

    assert sorted(copied_paths) == result
    assert len(copied_paths) == 2


def test_on_copy_not_called_for_zero_figure_case(tmp_path):
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    copied_paths = []
    copy_figures_to_vault(cache_figures_dir, vault_figures_dir, on_copy=copied_paths.append)

    assert copied_paths == []


def test_on_copy_omittable_default_none(tmp_path):
    """on_copy defaults to None and is safely omittable (no crash)."""
    cache_figures_dir = tmp_path / "_cache" / "class_1" / "figures"
    cache_figures_dir.mkdir(parents=True)
    (cache_figures_dir / "lecture_02_fig_001.jpg").write_bytes(b"fig1")
    vault_figures_dir = tmp_path / "vault" / "class_1" / "figures"

    result = copy_figures_to_vault(cache_figures_dir, vault_figures_dir)

    assert len(result) == 1


def test_rewrite_single_image_reference_gets_figure_1_caption():
    markdown = "Some text.\n\n![](figures/lecture_02_fig_001.jpg)\n\nMore text."

    result = rewrite_image_references(markdown)

    assert result == (
        "Some text.\n\n![Figure 1](figures/lecture_02_fig_001.jpg)\n\nMore text."
    )


def test_rewrite_multiple_image_references_numbered_sequentially():
    markdown = (
        "![](figures/lecture_02_fig_001.jpg)\n"
        "text in between\n"
        "![](figures/lecture_02_fig_002.jpg)\n"
        "![](figures/lecture_02_fig_003.jpg)\n"
    )

    result = rewrite_image_references(markdown)

    assert result == (
        "![Figure 1](figures/lecture_02_fig_001.jpg)\n"
        "text in between\n"
        "![Figure 2](figures/lecture_02_fig_002.jpg)\n"
        "![Figure 3](figures/lecture_02_fig_003.jpg)\n"
    )


def test_rewrite_dark_mode_appends_marker_to_every_caption():
    markdown = (
        "![](figures/lecture_02_fig_001.jpg)\n"
        "![](figures/lecture_02_fig_002.jpg)\n"
    )

    result = rewrite_image_references(markdown, dark_mode=True)

    assert result == (
        "![Figure 1 @darkmode](figures/lecture_02_fig_001.jpg)\n"
        "![Figure 2 @darkmode](figures/lecture_02_fig_002.jpg)\n"
    )


def test_rewrite_same_image_referenced_twice_gets_distinct_numbers():
    markdown = (
        "![](figures/lecture_02_fig_001.jpg)\n"
        "some text\n"
        "![](figures/lecture_02_fig_001.jpg)\n"
    )

    result = rewrite_image_references(markdown)

    assert result == (
        "![Figure 1](figures/lecture_02_fig_001.jpg)\n"
        "some text\n"
        "![Figure 2](figures/lecture_02_fig_001.jpg)\n"
    )


def test_rewrite_leaves_non_figures_content_untouched():
    markdown = (
        "# Lecture 1\n\n"
        "Some prose with $E=mc^2$ inline math.\n\n"
        "![an external image](https://example.com/image.png)\n\n"
        "![diagram](other/path/image.png)\n\n"
        "$$\n\\left(x\\right)\n$$\n"
    )

    result = rewrite_image_references(markdown)

    assert result == markdown


def test_rewrite_discards_existing_alt_text():
    markdown = "![some existing alt text](figures/lecture_02_fig_001.jpg)"

    result = rewrite_image_references(markdown)

    assert result == "![Figure 1](figures/lecture_02_fig_001.jpg)"


def test_rewrite_no_figure_references_is_noop():
    markdown = "Just plain text with no images at all."

    result = rewrite_image_references(markdown)

    assert result == markdown
