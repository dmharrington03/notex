"""
Unit tests for src/main.py (issue #11 — orchestration entry point).

All HTTP calls mocked via respx (see AGENTS.md Testing Conventions); state.db
is a real, temporary SQLite file per test (tmp_path), never mocked, mirroring
tests/test_discovery.py's convention.

Covered here:
    - run(): a NEW file is processed via process_pdf() and recorded to
      state.db with mathpix_status="success" plus pdf_id/figure_count/
      mathpix_processed_at, reusing the hash/mtime/size classify_pdf()
      already computed.
    - run(): an UNCHANGED file is skipped entirely -- process_pdf() (and
      therefore the Mathpix API) is never called for it.
    - run(): a Mathpix failure on one file is recorded as
      mathpix_status="failed" and does not stop a second file in the same
      course from being processed successfully.
    - run(): PDFs grouped under UNGROUPED_COURSE_KEY are skipped (not
      processed, not written to state.db) and tallied separately.
    - run(): a second run over the same unchanged tree is a full no-op
      (idempotency), matching issue #12's real-data validation intent.
    - main(): returns 1 and prints an error without calling run() when
      load_paths_config() raises ConfigError; returns 0 (even with
      errors > 0) once run() completes successfully.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from src.config import ConfigError, PathsConfig
from src.main import RunSummary, main, run
from src.mathpix import MathpixClient
from src.state import get_entry, init_db, upsert_entry

MATHPIX_BASE_URL = "https://api.mathpix.com"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_MD_ZIP = FIXTURES_DIR / "sample_result.md.zip"


def _write_pdf(path: Path, content: bytes = b"%PDF-1.4 fake pdf contents") -> Path:
    path.write_bytes(content)
    return path


def _make_paths_config(tmp_path: Path) -> PathsConfig:
    input_root = tmp_path / "notes_raw"
    input_root.mkdir()
    return PathsConfig(
        input_root=input_root,
        cache_dir=tmp_path / "_cache",
        state_db=tmp_path / "state.db",
    )


@pytest.fixture
def sleep_calls():
    return []


@pytest.fixture
def client(sleep_calls):
    with MathpixClient(
        app_id="test_app_id", app_key="test_app_key", sleep_fn=sleep_calls.append
    ) as c:
        yield c


def _converter_status_response(status: str | None):
    return httpx.Response(
        200, json={"conversion_status": {"md.zip": {"status": status}}}
    )


def _mock_happy_path(pdf_id: str = "abc123"):
    """Mocks a full submit -> poll -> converter -> md.zip download happy
    path, immediately terminal (no polling waits). Returns the submit
    route so callers can assert on its call count."""
    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": pdf_id})
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/{pdf_id}").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/{pdf_id}").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/{pdf_id}.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )
    return submit_route


@respx.mock
def test_run_processes_new_file_and_records_success(client, tmp_path):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    _mock_happy_path()

    summary = run(paths_config, conn, client=client)

    assert summary == RunSummary(processed=1, skipped=0, errors=0, ungrouped=0)

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry is not None
    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "abc123"
    assert entry.figure_count == 2
    assert entry.mathpix_processed_at is not None
    stat = pdf_path.stat()
    assert entry.source_mtime == stat.st_mtime
    assert entry.source_size == stat.st_size
    assert entry.source_hash is not None

    markdown_path = paths_config.cache_dir / "class_1" / "lecture_01.mathpix.md"
    assert markdown_path.is_file()


@respx.mock
def test_run_skips_unchanged_file_without_calling_process_pdf(client, tmp_path):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")
    stat = pdf_path.stat()

    upsert_entry(
        conn,
        str(pdf_path.resolve()),
        source_hash="whatever",
        source_mtime=stat.st_mtime,
        source_size=stat.st_size,
        mathpix_status="success",
    )

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )

    summary = run(paths_config, conn, client=client)

    assert summary == RunSummary(processed=0, skipped=1, errors=0, ungrouped=0)
    assert not submit_route.called


@respx.mock
def test_run_continues_after_one_file_fails(client, tmp_path):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    # Sorted by source_path -- lecture_01 (fails) is processed before
    # lecture_02 (succeeds).
    bad_pdf = _write_pdf(course_dir / "lecture_01.pdf", b"bad pdf contents")
    good_pdf = _write_pdf(course_dir / "lecture_02.pdf", b"good pdf contents")

    respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        side_effect=[
            httpx.Response(200, json={"error": "bad request"}),
            httpx.Response(200, json={"pdf_id": "abc123"}),
        ]
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123").mock(
        return_value=httpx.Response(200, json={"status": "completed"})
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )

    summary = run(paths_config, conn, client=client)

    assert summary == RunSummary(processed=1, skipped=0, errors=1, ungrouped=0)

    bad_entry = get_entry(conn, str(bad_pdf.resolve()))
    assert bad_entry.mathpix_status == "failed"
    assert bad_entry.mathpix_pdf_id is None

    good_entry = get_entry(conn, str(good_pdf.resolve()))
    assert good_entry.mathpix_status == "success"
    assert good_entry.mathpix_pdf_id == "abc123"


@respx.mock
def test_run_skips_ungrouped_pdfs_without_writing_state(client, tmp_path):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    stray_pdf = _write_pdf(paths_config.input_root / "stray.pdf")

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )

    summary = run(paths_config, conn, client=client)

    assert summary == RunSummary(processed=0, skipped=0, errors=0, ungrouped=1)
    assert not submit_route.called
    assert get_entry(conn, str(stray_pdf.resolve())) is None


@respx.mock
def test_run_second_pass_is_full_noop(client, tmp_path):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    _write_pdf(course_dir / "lecture_01.pdf")

    submit_route = _mock_happy_path()

    first_summary = run(paths_config, conn, client=client)
    assert first_summary == RunSummary(processed=1, skipped=0, errors=0, ungrouped=0)
    assert submit_route.call_count == 1

    second_summary = run(paths_config, conn, client=client)

    assert second_summary == RunSummary(processed=0, skipped=1, errors=0, ungrouped=0)
    assert submit_route.call_count == 1


def test_main_returns_error_code_on_config_error(monkeypatch, tmp_path, capsys):
    import src.main as main_module

    def _raise_config_error(*args, **kwargs):
        raise ConfigError("config.yaml not found")

    monkeypatch.setattr(main_module, "load_paths_config", _raise_config_error)

    exit_code = main([])

    assert exit_code == 1
    assert "config.yaml not found" in capsys.readouterr().err


def test_main_returns_zero_and_prints_summary_even_with_errors(monkeypatch, tmp_path, capsys):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)
    monkeypatch.setattr(
        main_module,
        "run",
        lambda paths_config, conn, client=None: RunSummary(
            processed=1, skipped=2, errors=1, ungrouped=0
        ),
    )

    exit_code = main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Processed:  1" in out
    assert "Errors:     1" in out
