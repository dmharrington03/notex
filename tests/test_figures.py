"""
Unit tests for src/figures.py.

Covered here (issue #23 — figure copy-to-vault function):
    - zero-figure case (missing cache figures/ dir) is a no-op
    - single figure copy
    - multiple figure copy
    - rerun overwrites cleanly (idempotent, no duplication)
    - hidden files in the cache dir are skipped
    - copied file content matches the source byte-for-byte

All tmp_path-backed, no mocking, no network.
"""

from src.figures import copy_figures_to_vault


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
