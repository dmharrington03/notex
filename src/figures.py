"""
Figure handling (vault-facing) — Phase 4, issues #23-#24.

Copies a course's cached Mathpix figures into the Obsidian vault, and
rewrites the LLM-cleaned/raw-fallback Markdown's cache-relative image
references to add a numbered placeholder caption (and an optional dark-mode
marker).

    - copy_figures_to_vault()     copies every file from a course's cached
                                   figures/ dir into vault/{course}/figures/
    - rewrite_image_references()  injects a numbered "Figure N" caption (and
                                   optionally " @darkmode") into each
                                   ![](figures/...) reference's alt-text slot

Implementation status:
    - copy_figures_to_vault()      implemented (issue #23; gained an
                                    on_copy observability callback in #48)
    - rewrite_image_references()   implemented (issue #24)

Deliberately no config.py reading / vault_root lookup here — both functions
take explicit source/dest Path params (or a plain dark_mode bool), matching
cleanup_pdf()'s dest_dir-as-param precedent from Phase 3 (src/llm.py). Real
wiring of paths_config.vault_root / output.figures_dark_mode_flag into an actual
call site is Phase 5/6's job.

Note on issue #24's implementation: the issue as originally written called
for rewriting references into Obsidian's ![[filename.jpg]] wikilink form.
Per explicit user direction during implementation, this was overridden to
keep standard Markdown ![alt](path) syntax instead -- only the alt-text
slot is rewritten (to the numbered placeholder caption, plus the darkmode
marker when requested); the path itself is left completely untouched, since
it already correctly points at vault/{course}/figures/... relative to where
Phase 5 will write the note file.
"""

from __future__ import annotations

import re
import shutil
from itertools import count
from pathlib import Path
from typing import Callable


def copy_figures_to_vault(
    cache_figures_dir: str | Path,
    vault_figures_dir: str | Path,
    on_copy: Callable[[Path], None] | None = None,
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
    a bare count), so callers such as issue #24's reference rewriter or
    Phase 5's vault.py have the real filenames to work with.

    on_copy (issue #48): optional observability hook, called once per
    copied file with its destination Path, immediately after that file's
    shutil.copy2() call. Purely for a Reporter's on_detail() (verbose-only,
    src/main.py's _write_to_vault()) to surface per-figure copy actions --
    never affects control flow, and never called at all for the
    zero-figure no-op case. Not called in copied-Path sort order -- fires
    in cache_figures_dir.iterdir()'s (arbitrary) order, before the final
    sort; the returned list is still sorted regardless.
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
        if on_copy is not None:
            on_copy(dest_path)

    return sorted(copied)


# Matches standard Markdown image syntax, restricted to cache-relative
# figures/ references only: ![alt text](figures/...). Same ![alt](path)
# regex shape as MathpixClient.fetch_and_extract()'s existing image-reference
# parsing (src/mathpix.py, issue #3), but scoped to the figures/ prefix so
# that only recognized figure references are touched -- everything else in
# the Markdown (prose, math, non-figures/ image references) is left as-is.
_FIGURE_REF_PATTERN = re.compile(r"!\[[^\]]*\]\((figures/[^)\s]+)\)")


def rewrite_image_references(markdown_text: str, dark_mode: bool = False) -> str:
    """
    Rewrite every ![alt](figures/...) reference in markdown_text to carry a
    numbered placeholder caption in its alt-text slot, e.g.:

        ![](figures/lecture_02_fig_001.jpg)
            -> ![Figure 1](figures/lecture_02_fig_001.jpg)

    Mathpix supplies no alt text at all for figures (![]()), so any existing
    alt text is discarded and replaced with "Figure N", numbered strictly by
    order of appearance in the document (the Nth ![](figures/...) reference
    encountered gets "Figure N" -- if the same image is referenced more than
    once, each occurrence still gets its own, incrementing number, rather
    than sharing one).

    The image path itself is left completely untouched -- this stays
    standard Markdown syntax (![alt](path)), not Obsidian's ![[...]]
    wikilink form, since the figures/... relative path already correctly
    points at vault/{course}/figures/... relative to where Phase 5 will
    write the note file.

    When dark_mode is True, " @darkmode" is appended to every caption (e.g.
    "Figure 1 @darkmode"), so the vault's Obsidian renderer/CSS can key off
    that marker for dark-mode display. dark_mode is a plain parameter here,
    not read from config.yaml -- wiring the real output.figures_dark_mode_flag
    config value through to an actual call site is Phase 6's job.

    Only ![alt](figures/...) references are recognized and rewritten;
    anything else in markdown_text (prose, math, headings, non-figures/
    image references such as an external URL) is left byte-for-byte
    untouched.
    """
    counter = count(1)

    def _replace(match: re.Match[str]) -> str:
        path = match.group(1)
        caption = f"Figure {next(counter)}"
        if dark_mode:
            caption += " @darkmode"
        return f"![{caption}]({path})"

    return _FIGURE_REF_PATTERN.sub(_replace, markdown_text)
