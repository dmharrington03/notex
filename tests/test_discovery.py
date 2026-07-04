"""
Unit tests for src/discovery.py.

`classify_pdf()` tests (issue #8 - two-tier per-file classification), one
per row of docs/spec.md's Reprocessing logic table (plus the "previously
mathpix_failed -> retry" rule called out in AGENTS.md), against a real
(tmp_path) state.db - no mocking:
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

`discover_pdfs()` tests (issue #9 - recursive multi-course directory walk):
    - groups multiple courses, sorted by course name then by source_path
    - includes UNCHANGED results, not just actionable ones
    - empty course subdirectory still appears with an empty list
    - non-.pdf files are ignored
    - recursion finds PDFs nested deeper than one level within a course
    - a stray top-level PDF (directly under input_root) is classified but
      grouped under UNGROUPED_COURSE_KEY rather than a real course name
    - hidden directories/files are ignored as course candidates / PDFs
"""

from src.discovery import (
    Classification,
    UNGROUPED_COURSE_KEY,
    classify_pdf,
    compute_sha256,
    discover_pdfs,
)
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


def test_discover_pdfs_groups_by_course_sorted(tmp_path):
    conn = init_db(tmp_path / "state.db")

    class_a = tmp_path / "class_a"
    class_b = tmp_path / "class_b"
    class_a.mkdir()
    class_b.mkdir()
    _write_pdf(class_a / "lecture_02.pdf")
    _write_pdf(class_a / "lecture_01.pdf")
    _write_pdf(class_b / "lecture_01.pdf")

    result = discover_pdfs(tmp_path, conn)

    assert list(result.keys()) == ["class_a", "class_b"]
    assert [r.source_path for r in result["class_a"]] == [
        str((class_a / "lecture_01.pdf").resolve()),
        str((class_a / "lecture_02.pdf").resolve()),
    ]
    assert [r.source_path for r in result["class_b"]] == [
        str((class_b / "lecture_01.pdf").resolve())
    ]
    assert all(r.classification == Classification.NEW for r in result["class_a"])


def test_discover_pdfs_includes_unchanged_results(tmp_path):
    conn = init_db(tmp_path / "state.db")
    course_dir = tmp_path / "class_a"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")
    source_path = str(pdf_path.resolve())
    stat = pdf_path.stat()

    upsert_entry(
        conn,
        source_path,
        source_hash=compute_sha256(pdf_path),
        source_mtime=stat.st_mtime,
        source_size=stat.st_size,
        mathpix_status="success",
    )

    result = discover_pdfs(tmp_path, conn)

    assert len(result["class_a"]) == 1
    assert result["class_a"][0].classification == Classification.UNCHANGED


def test_discover_pdfs_empty_course_folder_included(tmp_path):
    conn = init_db(tmp_path / "state.db")
    (tmp_path / "class_empty").mkdir()

    result = discover_pdfs(tmp_path, conn)

    assert result == {"class_empty": []}


def test_discover_pdfs_ignores_non_pdf_files(tmp_path):
    conn = init_db(tmp_path / "state.db")
    course_dir = tmp_path / "class_a"
    course_dir.mkdir()
    _write_pdf(course_dir / "lecture_01.pdf")
    (course_dir / "notes.txt").write_text("not a pdf")
    (course_dir / ".DS_Store").write_bytes(b"junk")

    result = discover_pdfs(tmp_path, conn)

    assert len(result["class_a"]) == 1
    assert result["class_a"][0].source_path == str(
        (course_dir / "lecture_01.pdf").resolve()
    )


def test_discover_pdfs_recurses_within_course(tmp_path):
    conn = init_db(tmp_path / "state.db")
    course_dir = tmp_path / "class_a"
    nested_dir = course_dir / "midterm_review"
    nested_dir.mkdir(parents=True)
    _write_pdf(course_dir / "lecture_01.pdf")
    _write_pdf(nested_dir / "lecture_02.pdf")

    result = discover_pdfs(tmp_path, conn)

    assert [r.source_path for r in result["class_a"]] == sorted(
        [
            str((course_dir / "lecture_01.pdf").resolve()),
            str((nested_dir / "lecture_02.pdf").resolve()),
        ]
    )


def test_discover_pdfs_ungrouped_top_level_pdf(tmp_path):
    conn = init_db(tmp_path / "state.db")
    course_dir = tmp_path / "class_a"
    course_dir.mkdir()
    _write_pdf(course_dir / "lecture_01.pdf")
    stray_pdf = _write_pdf(tmp_path / "stray_lecture.pdf")

    result = discover_pdfs(tmp_path, conn)

    assert UNGROUPED_COURSE_KEY in result
    assert [r.source_path for r in result[UNGROUPED_COURSE_KEY]] == [
        str(stray_pdf.resolve())
    ]
    assert result[UNGROUPED_COURSE_KEY][0].classification == Classification.NEW
    assert "class_a" in result


def test_discover_pdfs_ignores_hidden_dirs_and_files(tmp_path):
    conn = init_db(tmp_path / "state.db")
    course_dir = tmp_path / "class_a"
    hidden_course_dir = tmp_path / ".hidden_course"
    course_dir.mkdir()
    hidden_course_dir.mkdir()
    _write_pdf(course_dir / "lecture_01.pdf")
    _write_pdf(hidden_course_dir / "lecture_01.pdf")
    _write_pdf(tmp_path / ".hidden_stray.pdf")

    result = discover_pdfs(tmp_path, conn)

    assert list(result.keys()) == ["class_a"]
    assert UNGROUPED_COURSE_KEY not in result
