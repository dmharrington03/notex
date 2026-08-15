"""
Unit tests for src/state.py.

Covered here (issue #7 — state.db schema + CRUD):
    - init_db() creates the pdf_state table and is idempotent
    - get_entry() returns None for a missing source_path
    - upsert_entry() inserts a new row, round-tripping all field types
      (including datetime <-> ISO 8601 string)
    - upsert_entry() partial updates leave previously-set columns intact
      (including issue #21's llm_input_tokens/llm_output_tokens/
      llm_cost_estimate, issue #22's page_count, issue #30's
      vault_status/vault_path columns, and issue #40's
      vault_content_hash column)
    - upsert_entry() rejects unknown field names

No respx/mocking needed - all sqlite, using tmp_path files.
"""

from datetime import datetime, timezone

import pytest

from src.state import StateEntry, get_entry, init_db, upsert_entry


def test_init_db_creates_table(tmp_path):
    db_path = tmp_path / "state.db"

    conn = init_db(db_path)

    assert db_path.is_file()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pdf_state'"
    ).fetchall()
    assert len(tables) == 1


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "state.db"

    conn1 = init_db(db_path)
    upsert_entry(conn1, "/notes_raw/class_1/lecture_01.pdf", mathpix_status="success")
    conn1.close()

    # Re-init against the same path should not wipe existing data or error.
    conn2 = init_db(db_path)
    entry = get_entry(conn2, "/notes_raw/class_1/lecture_01.pdf")

    assert entry is not None
    assert entry.mathpix_status == "success"


def test_init_db_creates_parent_dirs(tmp_path):
    db_path = tmp_path / "nested" / "dir" / "state.db"

    conn = init_db(db_path)

    assert db_path.is_file()
    conn.execute(f"SELECT 1 FROM pdf_state")  # table exists, no error


def test_get_entry_returns_none_for_missing_path(tmp_path):
    conn = init_db(tmp_path / "state.db")

    entry = get_entry(conn, "/notes_raw/class_1/does_not_exist.pdf")

    assert entry is None


def test_upsert_entry_inserts_and_round_trips_all_fields(tmp_path):
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    processed_at = datetime(2026, 7, 4, 12, 30, 0, tzinfo=timezone.utc)

    upsert_entry(
        conn,
        source_path,
        source_hash="abc123",
        source_mtime=1234.5,
        source_size=4096,
        mathpix_pdf_id="pdf_xyz",
        mathpix_status="success",
        llm_model="gpt-4o-mini",
        llm_prompt_version="cleanup_v1",
        llm_status="success",
        llm_validation_result="ok",
        llm_keywords='["radiation", "helium atom"]',
        figure_count=2,
        page_count=3,
        output_path="/_cache/class_1/lecture_01.llm.md",
        vault_status="success",
        vault_path="/vault/class_1/Lecture 01.md",
        vault_content_hash="deadbeef" * 8,
        mathpix_processed_at=processed_at,
        llm_processed_at=processed_at,
        vault_written_at=processed_at,
        llm_input_tokens=1500,
        llm_output_tokens=900,
        llm_cost_estimate=0.0087,
    )

    entry = get_entry(conn, source_path)

    assert entry == StateEntry(
        source_path=source_path,
        source_hash="abc123",
        source_mtime=1234.5,
        source_size=4096,
        mathpix_pdf_id="pdf_xyz",
        mathpix_status="success",
        llm_model="gpt-4o-mini",
        llm_prompt_version="cleanup_v1",
        llm_status="success",
        llm_validation_result="ok",
        llm_keywords='["radiation", "helium atom"]',
        figure_count=2,
        page_count=3,
        output_path="/_cache/class_1/lecture_01.llm.md",
        vault_status="success",
        vault_path="/vault/class_1/Lecture 01.md",
        vault_content_hash="deadbeef" * 8,
        mathpix_processed_at=processed_at,
        llm_processed_at=processed_at,
        vault_written_at=processed_at,
        llm_input_tokens=1500,
        llm_output_tokens=900,
        llm_cost_estimate=0.0087,
    )


def test_upsert_entry_insert_with_no_optional_fields(tmp_path):
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_02.pdf"

    upsert_entry(conn, source_path)

    entry = get_entry(conn, source_path)

    assert entry == StateEntry(
        source_path=source_path,
        source_hash=None,
        source_mtime=None,
        source_size=None,
        mathpix_pdf_id=None,
        mathpix_status=None,
        llm_model=None,
        llm_prompt_version=None,
        llm_status=None,
        llm_validation_result=None,
        llm_keywords=None,
        figure_count=None,
        page_count=None,
        output_path=None,
        vault_status=None,
        vault_path=None,
        vault_content_hash=None,
        mathpix_processed_at=None,
        llm_processed_at=None,
        vault_written_at=None,
        llm_input_tokens=None,
        llm_output_tokens=None,
        llm_cost_estimate=None,
    )


def test_upsert_entry_partial_update_preserves_other_columns(tmp_path):
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    mathpix_processed_at = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)

    upsert_entry(
        conn,
        source_path,
        source_hash="abc123",
        source_mtime=1234.5,
        source_size=4096,
        mathpix_pdf_id="pdf_xyz",
        mathpix_status="success",
        figure_count=1,
        page_count=2,
        mathpix_processed_at=mathpix_processed_at,
    )

    # A later, unrelated stage (e.g. LLM cleanup) only writes its own
    # columns - the earlier Mathpix fields must survive untouched.
    upsert_entry(conn, source_path, llm_status="success", llm_model="gpt-4o-mini")

    entry = get_entry(conn, source_path)

    assert entry.source_hash == "abc123"
    assert entry.source_mtime == 1234.5
    assert entry.source_size == 4096
    assert entry.mathpix_pdf_id == "pdf_xyz"
    assert entry.mathpix_status == "success"
    assert entry.figure_count == 1
    assert entry.page_count == 2
    assert entry.mathpix_processed_at == mathpix_processed_at
    assert entry.llm_status == "success"
    assert entry.llm_model == "gpt-4o-mini"


def test_upsert_entry_partial_update_preserves_token_and_cost_columns(tmp_path):
    # Issue #21's llm_input_tokens/llm_output_tokens/llm_cost_estimate
    # columns follow the same partial-upsert convention as every other
    # column: a later, unrelated call must not null them out.
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    upsert_entry(
        conn,
        source_path,
        llm_status="success",
        llm_input_tokens=1000,
        llm_output_tokens=400,
        llm_cost_estimate=0.0056,
    )

    # A later call touching only mathpix_* fields must not disturb the
    # token/cost columns written above.
    upsert_entry(conn, source_path, mathpix_status="success", mathpix_pdf_id="pdf_xyz")

    entry = get_entry(conn, source_path)

    assert entry.llm_input_tokens == 1000
    assert entry.llm_output_tokens == 400
    assert entry.llm_cost_estimate == 0.0056
    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "pdf_xyz"


def test_upsert_entry_partial_update_preserves_page_count_column(tmp_path):
    # Issue #22's page_count column follows the same partial-upsert
    # convention as every other column: a later, unrelated call must not
    # null it out.
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    upsert_entry(
        conn,
        source_path,
        mathpix_status="success",
        mathpix_pdf_id="pdf_xyz",
        page_count=5,
    )

    # A later call touching only llm_* fields must not disturb page_count.
    upsert_entry(conn, source_path, llm_status="success", llm_model="gpt-4o-mini")

    entry = get_entry(conn, source_path)

    assert entry.page_count == 5
    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "pdf_xyz"
    assert entry.llm_status == "success"
    assert entry.llm_model == "gpt-4o-mini"


def test_upsert_entry_partial_update_preserves_vault_columns(tmp_path):
    # Issue #30's vault_status/vault_path columns follow the same
    # partial-upsert convention as every other column: a later, unrelated
    # call must not null them out, and vice versa.
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    upsert_entry(
        conn,
        source_path,
        vault_status="success",
        vault_path="/vault/class_1/Lecture 01.md",
        vault_content_hash="abc123" * 10,
    )

    # A later call touching only mathpix_*/llm_* fields must not disturb
    # the vault columns written above.
    upsert_entry(
        conn,
        source_path,
        mathpix_status="success",
        mathpix_pdf_id="pdf_xyz",
        llm_status="success",
        llm_model="gpt-4o-mini",
    )

    entry = get_entry(conn, source_path)

    assert entry.vault_status == "success"
    assert entry.vault_path == "/vault/class_1/Lecture 01.md"
    assert entry.vault_content_hash == "abc123" * 10
    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "pdf_xyz"
    assert entry.llm_status == "success"
    assert entry.llm_model == "gpt-4o-mini"


def test_upsert_entry_partial_update_preserves_other_columns_when_vault_written(
    tmp_path,
):
    # Reverse direction of the above: a vault-only upsert (the shape a real
    # vault-write stage performs) must not disturb previously-written
    # mathpix_*/llm_* columns.
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    upsert_entry(
        conn,
        source_path,
        mathpix_status="success",
        mathpix_pdf_id="pdf_xyz",
        llm_status="success",
        llm_model="gpt-4o-mini",
    )

    upsert_entry(
        conn,
        source_path,
        vault_status="success",
        vault_path="/vault/class_1/Lecture 01.md",
    )

    entry = get_entry(conn, source_path)

    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "pdf_xyz"
    assert entry.llm_status == "success"
    assert entry.llm_model == "gpt-4o-mini"
    assert entry.vault_status == "success"
    assert entry.vault_path == "/vault/class_1/Lecture 01.md"


def test_upsert_entry_partial_update_preserves_vault_content_hash_column(tmp_path):
    # Issue #40's vault_content_hash column follows the same partial-upsert
    # convention as every other column: a later, unrelated call must not
    # null it out, and vice versa.
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    upsert_entry(
        conn,
        source_path,
        vault_status="success",
        vault_path="/vault/class_1/Lecture 01.md",
        vault_content_hash="abc123" * 10,
    )

    # A later call recording a conflict (issue #40) touches only
    # vault_status -- vault_path/vault_content_hash must survive untouched,
    # since they still describe the last file actually written.
    upsert_entry(conn, source_path, vault_status="conflict")

    entry = get_entry(conn, source_path)

    assert entry.vault_status == "conflict"
    assert entry.vault_path == "/vault/class_1/Lecture 01.md"
    assert entry.vault_content_hash == "abc123" * 10


def test_upsert_entry_update_overwrites_previous_value(tmp_path):
    conn = init_db(tmp_path / "state.db")
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    upsert_entry(conn, source_path, mathpix_status="failed")
    upsert_entry(conn, source_path, mathpix_status="success", mathpix_pdf_id="pdf_xyz")

    entry = get_entry(conn, source_path)

    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "pdf_xyz"


def test_upsert_entry_rejects_unknown_field(tmp_path):
    conn = init_db(tmp_path / "state.db")

    with pytest.raises(ValueError):
        upsert_entry(conn, "/notes_raw/class_1/lecture_01.pdf", bogus_field="x")


def test_upsert_entry_returns_none(tmp_path):
    conn = init_db(tmp_path / "state.db")

    result = upsert_entry(conn, "/notes_raw/class_1/lecture_01.pdf", mathpix_status="success")

    assert result is None
