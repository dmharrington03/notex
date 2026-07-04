"""
Phase 2 discovery: per-file two-tier change classification.

`classify_pdf()` is the per-file primitive behind Stage 1 / State Management
in docs/spec.md: a filesystem mtime+size pre-check against `state.db`
(tier 1), falling back to a SHA-256 hash of the full file contents (tier 2)
only when the pre-check can't rule out a change. It also folds in the
"`mathpix_status == failed` -> retry" rule from the Reprocessing logic
table, independent of whether the file's contents actually changed.

Scope of this module (issue #8) is the single-file primitive only. The
recursive, multi-course directory walk that calls this per file is
`discover_pdfs()` (issue #9, not yet implemented).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from src.state import get_entry, upsert_entry

# Chunk size for streaming SHA-256 reads, so full PDF contents are never
# loaded into memory at once.
_HASH_CHUNK_SIZE = 65536

# `mathpix_status` value written on Mathpix submit/poll/fetch failure (see
# docs/spec.md's State Management schema: "success", "failed", "pending").
_MATHPIX_STATUS_FAILED = "failed"


class Classification(Enum):
    """Outcome of classifying a single PDF against state.db."""

    # No state.db entry exists for this path.
    NEW = "new"
    # Entry exists, content is unchanged (whether or not mtime/size drifted
    # in the meantime), and the last Mathpix attempt did not fail.
    UNCHANGED = "unchanged"
    # Entry exists but the SHA-256 hash differs from the stored value:
    # content actually changed, full reprocessing required.
    CHANGED = "changed"
    # Entry exists and content is unchanged, but the stored `mathpix_status`
    # was "failed" on the last run - reprocess anyway per the Reprocessing
    # logic table, independent of the content-change check.
    RETRY = "retry"


@dataclass(frozen=True)
class ClassificationResult:
    classification: Classification
    source_path: str
    source_mtime: float
    source_size: int
    # SHA-256 hex digest of the file's current contents. `None` only when
    # tier 1 short-circuited the check (UNCHANGED without a metadata drift),
    # since no hash was computed in that case.
    source_hash: str | None


def compute_sha256(path: str | Path) -> str:
    """
    Compute the SHA-256 hex digest of a file's contents, reading it in
    fixed-size chunks so the whole file is never held in memory at once.
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_pdf(pdf_path: str | Path, conn: sqlite3.Connection) -> ClassificationResult:
    """
    Classify a single PDF against its state.db entry using the two-tier
    change detection strategy from docs/spec.md Stage 1 / State Management.

    Tier 1 (mtime + size): if an entry exists and both match the stored
    values, the file is treated as unchanged with no file read at all -
    unless the stored `mathpix_status` was "failed", in which case it's
    classified RETRY instead.

    Tier 2 (SHA-256): computed only when tier 1 can't rule out a change
    (no entry, or mtime/size differ). If the hash matches the stored
    `source_hash`, the file is unchanged (or RETRY, per the same failed
    check) but the drifted mtime/size are persisted back to state.db
    immediately (via a partial `upsert_entry()` touching only those two
    columns) - this is the only state.db write `classify_pdf()` performs
    itself; NEW/CHANGED/RETRY results are left for the caller to persist
    once the actual pipeline stage(s) have run.

    Args:
        pdf_path: path to the PDF file (resolved to an absolute path,
            matching state.db's `source_path` convention).
        conn: an open connection from `state.init_db()`.

    Returns:
        A `ClassificationResult` describing what should happen next.
    """
    resolved_path = Path(pdf_path).resolve()
    source_path = str(resolved_path)

    stat = resolved_path.stat()
    current_mtime = stat.st_mtime
    current_size = stat.st_size

    entry = get_entry(conn, source_path)

    if entry is None:
        source_hash = compute_sha256(resolved_path)
        return ClassificationResult(
            classification=Classification.NEW,
            source_path=source_path,
            source_mtime=current_mtime,
            source_size=current_size,
            source_hash=source_hash,
        )

    metadata_unchanged = (
        current_mtime == entry.source_mtime and current_size == entry.source_size
    )

    if metadata_unchanged:
        classification = (
            Classification.RETRY
            if entry.mathpix_status == _MATHPIX_STATUS_FAILED
            else Classification.UNCHANGED
        )
        return ClassificationResult(
            classification=classification,
            source_path=source_path,
            source_mtime=current_mtime,
            source_size=current_size,
            source_hash=entry.source_hash,
        )

    # Tier 1 couldn't rule out a change - fall back to tier 2.
    source_hash = compute_sha256(resolved_path)

    if source_hash != entry.source_hash:
        return ClassificationResult(
            classification=Classification.CHANGED,
            source_path=source_path,
            source_mtime=current_mtime,
            source_size=current_size,
            source_hash=source_hash,
        )

    # Content is identical; only mtime/size drifted (e.g. file was copied
    # or touched). Persist the refreshed metadata now, since a skipped file
    # has no other pipeline stage that would ever write this update.
    upsert_entry(
        conn,
        source_path,
        source_mtime=current_mtime,
        source_size=current_size,
    )

    classification = (
        Classification.RETRY
        if entry.mathpix_status == _MATHPIX_STATUS_FAILED
        else Classification.UNCHANGED
    )
    return ClassificationResult(
        classification=classification,
        source_path=source_path,
        source_mtime=current_mtime,
        source_size=current_size,
        source_hash=source_hash,
    )
