"""
Phase 2 state log: state.db schema + CRUD.

`state.db` is a SQLite database with a single table (`pdf_state`), one row
per source PDF, keyed on its absolute `source_path`. It is the sole
mechanism the pipeline uses to decide what's already been processed (see
AGENTS.md's "Critical Invariants" and docs/spec.md's State Management /
Reprocessing logic sections).

Scope of this module is intentionally narrow: schema definition + a thin,
schema-agnostic CRUD surface (`init_db` / `get_entry` / `upsert_entry`).
It does not know or care what the `mathpix_status` / `llm_status` values
mean, does not resolve the *default* state.db path (that's
`src/config.py`'s `load_paths_config()`, a separate issue), and does not
implement change-detection logic (that's `src/discovery.py`).

Full column set (matching docs/spec.md's State Management table) is created
upfront now even though the `llm_*` / `output_path` / `vault_written_at`
columns stay NULL until Phases 3-5 populate them.

Issue #21 added three further nullable columns for LLM-stage token usage /
cost tracking: `llm_input_tokens`, `llm_output_tokens`,
`llm_cost_estimate`. Issue #22 added one more, `page_count` (the source
PDF's page count, from Mathpix's own status payload). Issue #30 added two
more, `vault_status` (`"success"`/`"failed"`, same per-stage-status
convention as `mathpix_status`/`llm_status`) and `vault_path` (the final
vault `.md` path -- distinct from `output_path`, which keeps its Phase 3
meaning as the cache-stage `.llm.md`/`.mathpix.md` path). Issue #40 added
one more, `vault_content_hash` (SHA-256 hex digest of the exact content
last *successfully* written to the vault file -- used to detect manual
edits to a vault note before overwriting it; `vault_status` also gained a
third value, `"conflict"`, for when a manual edit is detected and the
write is skipped). Issue #56 added one more, `llm_keywords` (a JSON-encoded
list of content-derived keywords the LLM cleanup call returns alongside
the cleaned Markdown -- see src/llm.py's cleanup_pdf(); NULL/empty on both
LLM failure paths, same as llm_model/llm_prompt_version). No
schema-migration logic is provided for these (or any
other column) -- `init_db()` only ever runs `CREATE TABLE IF NOT EXISTS`,
so an existing local `state.db` predating a column addition must be
deleted and left to rebuild rather than migrated in place (cheap given
this project's real data volume -- see AGENTS.md's issue #21 notes).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TABLE_NAME = "pdf_state"

# Columns that hold a `datetime` in Python but are persisted as ISO 8601
# strings in SQLite (sqlite3's built-in datetime adapters are deprecated as
# of Python 3.12, so conversion is handled explicitly here instead).
_DATETIME_FIELDS = frozenset(
    {"mathpix_processed_at", "llm_processed_at", "vault_written_at"}
)

# All columns other than the primary key, in schema order.
_VALUE_COLUMNS = (
    "source_hash",
    "source_mtime",
    "source_size",
    "mathpix_pdf_id",
    "mathpix_status",
    "llm_model",
    "llm_prompt_version",
    "llm_status",
    "llm_validation_result",
    "llm_keywords",
    "figure_count",
    "page_count",
    "output_path",
    "vault_status",
    "vault_path",
    "vault_content_hash",
    "mathpix_processed_at",
    "llm_processed_at",
    "vault_written_at",
    "llm_input_tokens",
    "llm_output_tokens",
    "llm_cost_estimate",
)

_ALL_COLUMNS = ("source_path",) + _VALUE_COLUMNS

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    source_path            TEXT PRIMARY KEY,
    source_hash            TEXT,
    source_mtime           REAL,
    source_size            INTEGER,
    mathpix_pdf_id         TEXT,
    mathpix_status         TEXT,
    llm_model              TEXT,
    llm_prompt_version     TEXT,
    llm_status             TEXT,
    llm_validation_result  TEXT,
    llm_keywords           TEXT,
    figure_count           INTEGER,
    page_count             INTEGER,
    output_path            TEXT,
    vault_status           TEXT,
    vault_path             TEXT,
    vault_content_hash     TEXT,
    mathpix_processed_at   TEXT,
    llm_processed_at       TEXT,
    vault_written_at       TEXT,
    llm_input_tokens       INTEGER,
    llm_output_tokens      INTEGER,
    llm_cost_estimate      REAL
);
"""


@dataclass(frozen=True)
class StateEntry:
    source_path: str
    source_hash: str | None
    source_mtime: float | None
    source_size: int | None
    mathpix_pdf_id: str | None
    mathpix_status: str | None
    llm_model: str | None
    llm_prompt_version: str | None
    llm_status: str | None
    llm_validation_result: str | None
    llm_keywords: str | None
    figure_count: int | None
    page_count: int | None
    output_path: str | None
    vault_status: str | None
    vault_path: str | None
    vault_content_hash: str | None
    mathpix_processed_at: datetime | None
    llm_processed_at: datetime | None
    vault_written_at: datetime | None
    llm_input_tokens: int | None
    llm_output_tokens: int | None
    llm_cost_estimate: float | None


def init_db(path: str | Path) -> sqlite3.Connection:
    """
    Open (creating if necessary) the state.db SQLite database at `path` and
    ensure the `pdf_state` table exists.

    Idempotent: safe to call repeatedly against the same path. Creates
    parent directories if they don't already exist.

    Args:
        path: filesystem path to the state.db file. No default is provided
            here — resolving the pipeline's default state.db location is
            src/config.py's `load_paths_config()`'s responsibility.

    Returns:
        An open `sqlite3.Connection` with the schema in place.
    """
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()
    return conn


def _row_to_entry(row: sqlite3.Row) -> StateEntry:
    values: dict[str, object] = {}
    for column in _ALL_COLUMNS:
        value = row[column]
        if column in _DATETIME_FIELDS and value is not None:
            value = datetime.fromisoformat(value)
        values[column] = value
    return StateEntry(**values)


def get_entry(conn: sqlite3.Connection, source_path: str) -> StateEntry | None:
    """
    Look up the state.db row for `source_path`.

    Returns:
        A `StateEntry`, or `None` if no row exists for that path.
    """
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(
        f"SELECT * FROM {TABLE_NAME} WHERE source_path = ?", (source_path,)
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_entry(row)


def upsert_entry(conn: sqlite3.Connection, source_path: str, **fields: object) -> None:
    """
    Insert or update the state.db row for `source_path`, keyed on
    `source_path`.

    Only the columns explicitly passed as keyword arguments are written; a
    partial call (e.g. `upsert_entry(conn, path, llm_status="success")`)
    leaves every other existing column on that row (e.g. `mathpix_*`
    fields written by an earlier call) untouched rather than nulling them
    out. This is what lets different pipeline stages independently persist
    just the columns they own without stepping on each other.

    Args:
        conn: an open connection from `init_db()`.
        source_path: absolute path to the source PDF; primary key.
        **fields: any subset of the non-key columns (see `_VALUE_COLUMNS`).
            `datetime` values are accepted for the `*_processed_at` /
            `vault_written_at` columns and serialized to ISO 8601 strings
            internally.

    Raises:
        ValueError: if `fields` contains a name that isn't a real column.
    """
    unknown = set(fields) - set(_VALUE_COLUMNS)
    if unknown:
        raise ValueError(
            f"Unknown state.db field(s): {', '.join(sorted(unknown))}"
        )

    serialized: dict[str, object] = {}
    for name, value in fields.items():
        if name in _DATETIME_FIELDS and isinstance(value, datetime):
            value = value.isoformat()
        serialized[name] = value

    columns = ("source_path",) + tuple(serialized)
    placeholders = ", ".join("?" for _ in columns)
    values = (source_path,) + tuple(serialized.values())

    if serialized:
        update_clause = ", ".join(
            f"{name} = excluded.{name}" for name in serialized
        )
        conflict_clause = f"DO UPDATE SET {update_clause}"
    else:
        conflict_clause = "DO NOTHING"

    sql = (
        f"INSERT INTO {TABLE_NAME} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(source_path) {conflict_clause}"
    )
    conn.execute(sql, values)
    conn.commit()
