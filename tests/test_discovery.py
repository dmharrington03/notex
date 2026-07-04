"""
Unit tests for src/discovery.py (issue #8 - two-tier per-file classification).

Covered here, one test per row of docs/spec.md's Reprocessing logic table
(plus the "previously mathpix_failed -> retry" rule called out in
AGENTS.md), against a real (tmp_path) state.db - no mocking:
    - No state log entry -> NEW, hash computed
    - PDF mtime and size unchanged -> UNCHANGED, no hash computed (verified
      by corrupting the stored hash and confirming it's never consulted)
    - PDF mtime or size changed, hash unchanged -> UNCHANGED, but stored
      metadata is refreshed in state.db
    - PDF hash changed -> CHANGED
    - mathpix_status == "failed", content unchanged -> RETRY
    - mathpix_status == "failed", metadata drifted but hash unchanged -> RETRY
    - mathpix_status == "failed", hash changed -> CHANGED (content change
      takes precedence over the retry rule)
    - compute_sha256() itself, directly
"""

from src.discovery import Classification, classify_pdf, compute_sha256
from src.state import get_entry, init_db, upsert_entry


def _write_pdf(path, content: bytes = b"%PDF-1.4 fake pdf contents"):
    path.write_bytes(content)
    return path


def test_compute_sha256_matches_hashlib(tmp_path):
    import hashlib

    pdf_path = _write_pdf(tmp_path / "lecture_01.pdf", b"hello world")

    assert compute_sha256(pdf_path) == hashlib.sha256(b"hello world").hexdigest()


def test_new_file_no_state_entry(tmp_path):
    conn = init_db(tmp_path / "state.db")
    pdf_path = _write_pdf(tmp_path / "lecture_01.pdf")

    result = classify_pdf(pdf_path, conn)

    assert result.classification == Classification.NEW
    assert result.source_path == str(pdf_path.resolve())
    assert result.source_hash == compute_sha256(pdf_path)
    stat = pdf_path.stat()
    assert result.source_mtime == stat.st_mtime
    assert result.source_size == stat.st_size


def test_unchanged_mtime_and_size_skips_hash_computation(tmp_path):
    conn = init_db(tmp_path / "state.db")
    pdf_path = _write_pdf(tmp_path / "lecture_01.pdf")
    stat = pdf_path.stat()
    source_path = str(pdf_path.resolve())

    # Deliberately store a wrong hash - if classify_pdf ever fell through to
    # tier 2 here, it would see a mismatch and misclassify as CHANGED. Tier 1
    # matching must short-circuit before that ever happens.
    upsert_entry(
        conn,
        source_path,
        source_hash="deliberately-wrong-hash",
        source_mtime=stat.st_mtime,
        source_size=stat.st_size,
        mathpix_status="success",
    )

    result = classify_pdf(pdf_path, conn)

    assert result.classification == Classification.UNCHANGED
    assert result.source_hash == "deliberately-wrong-hash"  # untouched, unread


def test_metadata_drift_hash_unchanged_updates_state_and_skips(tmp_path):
    conn = init_db(tmp_path / "state.db")
    pdf_path = _write_pdf(tmp_path / "lecture_01.pdf")
    source_path = str(pdf_path.resolve())
    real_hash = compute_sha256(pdf_path)

    # Stored metadata is stale (as if the file was copied/touched) but the
    # hash matches current contents.
    upsert_entry(
        conn,
        source_path,
        source_hash=real_hash,
        source_mtime=1.0,
        source_size=999999,
        mathpix_status="success",
    )

    result = classify_pdf(pdf_path, conn)

    assert result.classification == Classification.UNCHANGED
    stat = pdf_path.stat()
    assert result.source_mtime == stat.st_mtime
    assert result.source_size == stat.st_size

    # Drifted metadata must be persisted back to state.db even though the
    # file was skipped.
    entry = get_entry(conn, source_path)
    assert entry.source_mtime == stat.st_mtime
    assert entry.source_size == stat.st_size
    assert entry.source_hash == real_hash  # unchanged


def test_hash_changed_classifies_as_changed(tmp_path):
    conn = init_db(tmp_path / "state.db")
    pdf_path = _write_pdf(tmp_path / "lecture_01.pdf", b"original contents")
    source_path = str(pdf_path.resolve())

    upsert_entry(
        conn,
        source_path,
        source_hash="stale-hash-from-before",
        source_mtime=1.0,
        source_size=999999,
        mathpix_status="success",
    )

    # Simulate new content actually being written under the same path.
    pdf_path.write_bytes(b"different contents now")

    result = classify_pdf(pdf_path, conn)

    assert result.classification == Classification.CHANGED
    assert result.source_hash == compute_sha256(pdf_path)


def test_previously_failed_content_unchanged_retries(tmp_path):
    conn = init_db(tmp_path / "state.db")
    pdf_path = _write_pdf(tmp_path / "lecture_01.pdf")
    source_path = str(pdf_path.resolve())
    stat = pdf_path.stat()

    upsert_entry(
        conn,
        source_path,
        source_hash=compute_sha256(pdf_path),
        source_mtime=stat.st_mtime,
        source_size=stat.st_size,
        mathpix_status="failed",
    )

    result = classify_pdf(pdf_path, conn)

    assert result.classification == Classification.RETRY


def test_previously_failed_metadata_drifted_hash_unchanged_retries(tmp_path):
    conn = init_db(tmp_path / "state.db")
    pdf_path = _write_pdf(tmp_path / "lecture_01.pdf")
    source_path = str(pdf_path.resolve())
    real_hash = compute_sha256(pdf_path)

    upsert_entry(
        conn,
        source_path,
        source_hash=real_hash,
        source_mtime=1.0,
        source_size=999999,
        mathpix_status="failed",
    )

    result = classify_pdf(pdf_path, conn)

    assert result.classification == Classification.RETRY

    # Still persists the refreshed metadata even on the retry path.
    entry = get_entry(conn, source_path)
    stat = pdf_path.stat()
    assert entry.source_mtime == stat.st_mtime
    assert entry.source_size == stat.st_size


def test_previously_failed_hash_changed_classifies_as_changed_not_retry(tmp_path):
    conn = init_db(tmp_path / "state.db")
    pdf_path = _write_pdf(tmp_path / "lecture_01.pdf", b"original contents")
    source_path = str(pdf_path.resolve())

    upsert_entry(
        conn,
        source_path,
        source_hash="stale-hash-from-before",
        source_mtime=1.0,
        source_size=999999,
        mathpix_status="failed",
    )

    pdf_path.write_bytes(b"different contents now")

    result = classify_pdf(pdf_path, conn)

    # Content actually changed - full reprocessing (CHANGED) takes
    # precedence over the failed-retry classification.
    assert result.classification == Classification.CHANGED
