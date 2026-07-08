"""
Figure handling (vault-facing) — Phase 4, issue #23.

Copies a course's cached Mathpix figures into the Obsidian vault, and (a
later issue) rewrites the LLM-cleaned/raw-fallback Markdown's cache-relative
image references into Obsidian wikilink form.

    - copy_figures_to_vault()     copies every file from a course's cached
                                   figures/ dir into vault/{course}/figures/

Implementation status:
    - copy_figures_to_vault()      implemented (issue #23)
    - rewrite_image_references()   not yet implemented (issue #24)

Deliberately no config.py reading / vault_root lookup here — both functions
take explicit source/dest Path params, matching cleanup_pdf()'s
dest_dir-as-param precedent from Phase 3 (src/llm.py). Real wiring of
paths_config.vault_root into an actual call site is Phase 5/6's job.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def copy_figures_to_vault(
    cache_figures_dir: str | Path, vault_figures_dir: str | Path
) -> list[Path]:
    """
    Copy every figure file from a course's cached figures/ dir into the
    vault's figures/ dir for that course.

    cache_figures_dir is typically _cache/{course}/figures/ (as written by
    MathpixClient.fetch_and_extract(), issue #3) and vault_figures_dir is
    typically vault/{course}/figures/ — but both are taken as explicit
    params, with no config.py/vault_root lookup performed here.

    If cache_figures_dir doesn't exist at all (the zero-figure case, e.g.
    a lecture PDF with no diagrams), this is a no-op: returns an empty
    list and does not create vault_figures_dir. Mirrors
    fetch_and_extract()'s existing zero-figure handling (no figures/
    directory is created when there's nothing to put in it).

    Otherwise, vault_figures_dir is created (parents included) if it
    doesn't already exist, and every non-hidden regular file in
    cache_figures_dir is copied into it via shutil.copy2 (preserving
    file metadata), overwriting any existing file of the same name. No
    extension filtering is performed — cache_figures_dir is entirely
    cache-managed by fetch_and_extract() and only ever contains real
    figure files. Hidden files (names starting with ".", e.g. a stray
    .DS_Store) are skipped, matching src/discovery.py's existing
    hidden-file-skipping convention.

    Copying by the same deterministic filename on every call keeps this
    idempotent on rerun (same content in, same file overwritten, no
    duplication) without needing any state.db bookkeeping — this module
    does not touch state.db at all.

    Returns a sorted list of the destination Paths actually written (not
    a bare count), so callers such as issue #24's wikilink rewriter or
    Phase 5's vault.py have the real filenames to work with.
    """
    cache_figures_dir = Path(cache_figures_dir)
    vault_figures_dir = Path(vault_figures_dir)

    if not cache_figures_dir.is_dir():
        return []

    vault_figures_dir.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    for src_path in cache_figures_dir.iterdir():
        if not src_path.is_file():
            continue
        if src_path.name.startswith("."):
            continue
        dest_path = vault_figures_dir / src_path.name
        shutil.copy2(src_path, dest_path)
        copied.append(dest_path)

    return sorted(copied)
