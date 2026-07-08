"""
Unit tests for src/main.py (issue #11 — orchestration entry point; issue #18
— LLM stage wiring, force_llm / target_source_path infra).

All Mathpix HTTP calls mocked via respx (see AGENTS.md Testing Conventions);
state.db is a real, temporary SQLite file per test (tmp_path), never mocked,
mirroring tests/test_discovery.py's convention. The LLM stage is exercised
via a monkeypatched src.main.cleanup_pdf fake (never litellm.completion /
a real API — see the "Testing strategy" note in AGENTS.md's issue #18
entry), since run()'s signature has no LLMClient/completion_fn injection
point the way client= injects MathpixClient.

Covered here (issue #11):
    - run(): a NEW file is processed via process_pdf() and recorded to
      state.db with mathpix_status="success" plus pdf_id/figure_count/
      mathpix_processed_at, reusing the hash/mtime/size classify_pdf()
      already computed.
    - run(): an UNCHANGED-and-fully-current file is skipped entirely --
      process_pdf() (and therefore the Mathpix API) is never called for it,
      nor is cleanup_pdf().
    - run(): a Mathpix failure on one file is recorded as
      mathpix_status="failed" (and its LLM stage is never attempted) and
      does not stop a second file in the same course from being processed
      successfully.
    - run(): PDFs grouped under UNGROUPED_COURSE_KEY are skipped (not
      processed, not written to state.db) and tallied separately, in the
      normal (non-target_source_path) run.
    - run(): a second run over the same unchanged-and-current tree is a
      full no-op (idempotency), matching issue #12's real-data validation
      intent.
    - main(): returns 1 and prints an error without calling run() when
      load_paths_config() raises ConfigError; returns 0 (even with
      errors > 0) once run() completes successfully.

Covered here (issue #18):
    - run(): a freshly-processed (NEW) file immediately gets its LLM stage
      run too, with both mathpix_* and llm_*/output_path fields recorded.
    - run(): an UNCHANGED file whose LLM stage has never succeeded
      (needs_llm_reprocessing() is True) gets *only* its LLM stage rerun --
      no Mathpix API call -- tallied as llm_reprocessed.
    - run(): an UNCHANGED file whose LLM stage already succeeded is fully
      skipped (cleanup_pdf() never called).
    - run(): force_llm=True reprocesses an UNCHANGED file's LLM stage even
      though it's already up to date.
    - run(): target_source_path restricts the entire run to exactly one
      PDF -- a sibling PDF in the same course directory is never even
      classified/written to state.db (course-subfolder case), and a PDF
      sitting directly under input_root (UNGROUPED_COURSE_KEY case) is
      force-processed rather than skipped, using a paths_config.cache_dir /
      "_ungrouped" cache dir.
    - run(): target_source_path combined with force_llm=True reprocesses
      just that one file's LLM stage, leaving a sibling file's state.db row
      (including its llm_processed_at) completely untouched.

Covered here (issue #22 — page_count tracking):
    - A successfully-processed file's state.db row gets page_count from the
      mocked completed payload's num_pages field, and RunSummary's
      total_pages_processed reflects the sum of pages actually processed
      this run (0 for any skip/LLM-only-rerun/failure path).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from src.config import ConfigError, LLMConfig, PathsConfig
from src.llm import LLMResult
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
        vault_root=tmp_path / "vault",
        cache_dir=tmp_path / "_cache",
        state_db=tmp_path / "state.db",
    )


def _make_llm_config() -> LLMConfig:
    return LLMConfig(
        model="fake-llm-model",
        prompt_version="cleanup_v1",
        min_length_ratio=0.5,
        max_length_ratio=2.0,
    )


def _upsert_unchanged_entry(
    conn,
    pdf_path: Path,
    mathpix_status: str = "success",
    llm_status: str | None = None,
) -> None:
    """Seed a state.db row that will classify pdf_path as UNCHANGED on the
    next classify_pdf() call (matching mtime/size exactly)."""
    stat = pdf_path.stat()
    upsert_entry(
        conn,
        str(pdf_path.resolve()),
        source_hash="whatever",
        source_mtime=stat.st_mtime,
        source_size=stat.st_size,
        mathpix_status=mathpix_status,
        llm_status=llm_status,
    )


def _install_fake_cleanup_pdf(
    monkeypatch,
    status: str = "success",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cost_estimate: float = 0.001,
):
    """
    Monkeypatch src.main.cleanup_pdf with a fake that never touches
    litellm/a real API. Returns the list of call-argument dicts recorded,
    so tests can assert on what run() passed through.

    status="success" writes a real {lecture_stem}.llm.md into dest_dir
    (mirroring cleanup_pdf()'s real success behavior) and returns a
    llm_status="success" LLMResult with the given input_tokens/
    output_tokens/cost_estimate; status="failed" mirrors the
    fallback-to-raw-output shape (llm_model/llm_prompt_version/
    llm_validation_result all None, output_path == mathpix_markdown_path)
    but -- per issue #21 -- still reports the given token/cost figures,
    since the completion call still happened and cost real money even
    though validation failed and the output was discarded.
    """
    import src.main as main_module

    calls: list[dict] = []

    def _fake_cleanup_pdf(mathpix_markdown_path, dest_dir, lecture_stem, llm_config, client=None):
        calls.append(
            {
                "mathpix_markdown_path": Path(mathpix_markdown_path),
                "dest_dir": Path(dest_dir),
                "lecture_stem": lecture_stem,
                "llm_config": llm_config,
            }
        )
        if status == "success":
            dest_dir_path = Path(dest_dir)
            dest_dir_path.mkdir(parents=True, exist_ok=True)
            output_path = dest_dir_path / f"{lecture_stem}.llm.md"
            output_path.write_text("cleaned markdown", encoding="utf-8")
            return LLMResult(
                llm_model=llm_config.model,
                llm_prompt_version=llm_config.prompt_version,
                llm_status="success",
                llm_validation_result=json.dumps({"length_ratio": True}),
                output_path=output_path,
                processed_at=datetime.now(timezone.utc),
                llm_input_tokens=input_tokens,
                llm_output_tokens=output_tokens,
                llm_cost_estimate=cost_estimate,
            )
        return LLMResult(
            llm_model=None,
            llm_prompt_version=None,
            llm_status="failed",
            llm_validation_result=None,
            output_path=Path(mathpix_markdown_path),
            processed_at=datetime.now(timezone.utc),
            llm_input_tokens=input_tokens,
            llm_output_tokens=output_tokens,
            llm_cost_estimate=cost_estimate,
        )

    monkeypatch.setattr(main_module, "cleanup_pdf", _fake_cleanup_pdf)
    return calls


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
        return_value=httpx.Response(200, json={"status": "completed", "num_pages": 2})
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/{pdf_id}").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/{pdf_id}.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )
    return submit_route


@respx.mock
def test_run_processes_new_file_and_records_success(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    _mock_happy_path()
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())

    assert summary == RunSummary(
        processed=1,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=0,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
        total_pages_processed=2,
    )

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry is not None
    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "abc123"
    assert entry.figure_count == 2
    assert entry.page_count == 2
    assert entry.mathpix_processed_at is not None
    stat = pdf_path.stat()
    assert entry.source_mtime == stat.st_mtime
    assert entry.source_size == stat.st_size
    assert entry.source_hash is not None

    # LLM stage ran immediately after Mathpix succeeded.
    assert entry.llm_status == "success"
    assert entry.llm_model == "fake-llm-model"
    assert entry.llm_prompt_version == "cleanup_v1"
    assert entry.llm_processed_at is not None
    assert entry.output_path is not None
    assert Path(entry.output_path).is_file()
    assert Path(entry.output_path).name == "lecture_01.llm.md"
    assert entry.llm_input_tokens == 100
    assert entry.llm_output_tokens == 50
    assert entry.llm_cost_estimate == 0.001

    markdown_path = paths_config.cache_dir / "class_1" / "lecture_01.mathpix.md"
    assert markdown_path.is_file()
    assert len(llm_calls) == 1
    assert llm_calls[0]["mathpix_markdown_path"] == markdown_path
    assert llm_calls[0]["lecture_stem"] == "lecture_01"

    # Vault-writing (issue #31) ran immediately after the LLM stage.
    assert entry.vault_status == "success"
    assert entry.vault_path is not None
    assert Path(entry.vault_path).is_file()
    assert entry.vault_written_at is not None
    vault_path = paths_config.vault_root / "class_1" / "Lecture 01.md"
    assert Path(entry.vault_path) == vault_path
    assert "cleaned markdown" in vault_path.read_text(encoding="utf-8")


@respx.mock
def test_run_skips_unchanged_and_current_file_entirely(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    # llm_status="success" -> fully up to date, not just Mathpix-unchanged.
    _upsert_unchanged_entry(conn, pdf_path, mathpix_status="success", llm_status="success")

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())

    assert summary == RunSummary(processed=0, skipped=1, errors=0, ungrouped=0, llm_reprocessed=0)
    assert not submit_route.called
    assert llm_calls == []


@respx.mock
def test_run_unchanged_and_stale_triggers_llm_only_reprocessing(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    # llm_status=None -> never successfully cleaned up -- needs_llm_reprocessing() is True.
    _upsert_unchanged_entry(conn, pdf_path, mathpix_status="success", llm_status=None)

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    # The cached .mathpix.md must exist for cleanup_pdf() to be pointed at,
    # by the same deterministic-naming convention process_pdf() uses.
    cache_dir = paths_config.cache_dir / "class_1"
    cache_dir.mkdir(parents=True)
    cached_markdown = cache_dir / "lecture_01.mathpix.md"
    cached_markdown.write_text("raw mathpix markdown", encoding="utf-8")

    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())

    assert summary == RunSummary(
        processed=0,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=1,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
    )
    assert not submit_route.called

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry.llm_status == "success"
    assert entry.llm_model == "fake-llm-model"
    assert entry.output_path is not None
    assert entry.llm_input_tokens == 100
    assert entry.llm_output_tokens == 50
    assert entry.llm_cost_estimate == 0.001
    # Mathpix-stage fields were untouched by this LLM-only rerun.
    assert entry.mathpix_status == "success"

    assert len(llm_calls) == 1
    assert llm_calls[0]["mathpix_markdown_path"] == cached_markdown
    assert llm_calls[0]["lecture_stem"] == "lecture_01"

    # The reprocessed LLM content triggered a vault rewrite too (issue #31).
    assert entry.vault_status == "success"
    vault_path = paths_config.vault_root / "class_1" / "Lecture 01.md"
    assert Path(entry.vault_path) == vault_path
    assert vault_path.is_file()
    assert "cleaned markdown" in vault_path.read_text(encoding="utf-8")


@respx.mock
def test_run_force_llm_reprocesses_up_to_date_entry(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    # Already fully up to date -- without force_llm this would be a full skip.
    _upsert_unchanged_entry(conn, pdf_path, mathpix_status="success", llm_status="success")

    cache_dir = paths_config.cache_dir / "class_1"
    cache_dir.mkdir(parents=True)
    cached_markdown = cache_dir / "lecture_01.mathpix.md"
    cached_markdown.write_text("raw mathpix markdown", encoding="utf-8")

    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config, conn, client=client, llm_config=_make_llm_config(), force_llm=True
    )

    assert summary == RunSummary(
        processed=0,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=1,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
    )
    assert len(llm_calls) == 1


@respx.mock
def test_run_continues_after_one_file_fails(client, tmp_path, monkeypatch):
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
        return_value=httpx.Response(200, json={"status": "completed", "num_pages": 3})
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/converter/abc123").mock(
        return_value=_converter_status_response("completed")
    )
    respx.get(f"{MATHPIX_BASE_URL}/v3/pdf/abc123.md.zip").mock(
        return_value=httpx.Response(200, content=SAMPLE_MD_ZIP.read_bytes())
    )
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())

    assert summary == RunSummary(
        processed=1,
        skipped=0,
        errors=1,
        ungrouped=0,
        llm_reprocessed=0,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
        total_pages_processed=3,
    )

    bad_entry = get_entry(conn, str(bad_pdf.resolve()))
    assert bad_entry.mathpix_status == "failed"
    assert bad_entry.mathpix_pdf_id is None
    assert bad_entry.page_count is None
    assert bad_entry.llm_status is None

    good_entry = get_entry(conn, str(good_pdf.resolve()))
    assert good_entry.mathpix_status == "success"
    assert good_entry.mathpix_pdf_id == "abc123"
    assert good_entry.page_count == 3
    assert good_entry.llm_status == "success"


@respx.mock
def test_run_unparseable_filename_records_vault_failure_only(client, tmp_path, monkeypatch):
    """
    A filename that doesn't match parse_lecture_filename()'s
    lecture[_-]?<digits> pattern still succeeds at the Mathpix/LLM stages,
    but write_lecture_note() raises PostprocessError -- vault_status is
    recorded as "failed" without touching the already-successful
    mathpix_status/llm_status/output_path fields, and RunSummary.errors is
    incremented (issue #31).
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "not_a_lecture_filename.pdf")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())

    assert summary == RunSummary(
        processed=1,
        skipped=0,
        errors=1,
        ungrouped=0,
        llm_reprocessed=0,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
        total_pages_processed=2,
    )

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry is not None
    assert entry.mathpix_status == "success"
    assert entry.llm_status == "success"
    assert entry.output_path is not None
    assert entry.vault_status == "failed"
    assert entry.vault_path is None
    assert entry.vault_written_at is None
    assert not (paths_config.vault_root / "class_1").exists()


@respx.mock
def test_run_skips_ungrouped_pdfs_without_writing_state(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    stray_pdf = _write_pdf(paths_config.input_root / "stray.pdf")

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())

    assert summary == RunSummary(processed=0, skipped=0, errors=0, ungrouped=1, llm_reprocessed=0)
    assert not submit_route.called
    assert llm_calls == []
    assert get_entry(conn, str(stray_pdf.resolve())) is None


@respx.mock
def test_run_second_pass_is_full_noop(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    submit_route = _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    first_summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())
    assert first_summary == RunSummary(
        processed=1,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=0,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
        total_pages_processed=2,
    )
    assert submit_route.call_count == 1

    vault_path = paths_config.vault_root / "class_1" / "Lecture 01.md"
    assert vault_path.is_file()
    first_run_mtime = vault_path.stat().st_mtime_ns

    second_summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())

    assert second_summary == RunSummary(
        processed=0, skipped=1, errors=0, ungrouped=0, llm_reprocessed=0
    )
    assert submit_route.call_count == 1
    # A true no-op -- the vault file wasn't rewritten by the second,
    # fully-skipped pass over the unchanged file.
    assert vault_path.stat().st_mtime_ns == first_run_mtime

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry.vault_status == "success"
    assert Path(entry.vault_path) == vault_path


@respx.mock
def test_run_target_source_path_restricts_to_one_file_in_course(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    target_pdf = _write_pdf(course_dir / "lecture_01.pdf")
    sibling_pdf = _write_pdf(course_dir / "lecture_02.pdf")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        target_source_path=target_pdf,
    )

    assert summary == RunSummary(
        processed=1,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=0,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
        total_pages_processed=2,
    )

    target_entry = get_entry(conn, str(target_pdf.resolve()))
    assert target_entry is not None
    assert target_entry.mathpix_status == "success"
    assert target_entry.page_count == 2
    assert target_entry.llm_status == "success"

    markdown_path = paths_config.cache_dir / "class_1" / "lecture_01.mathpix.md"
    assert markdown_path.is_file()

    # The sibling PDF was never even classified -- discover_pdfs() was
    # bypassed entirely, so it has no state.db row at all.
    assert get_entry(conn, str(sibling_pdf.resolve())) is None


@respx.mock
def test_run_target_source_path_force_processes_ungrouped_file(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    stray_pdf = _write_pdf(paths_config.input_root / "stray.pdf")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        target_source_path=stray_pdf,
    )

    # Unlike the normal run (which skips ungrouped files entirely), a
    # directly-targeted ungrouped file is force-processed. Its filename
    # ("stray.pdf") doesn't match parse_lecture_filename()'s
    # lecture[_-]?<digits> pattern, though, so the Mathpix/LLM stages
    # still succeed but the vault-write stage (issue #31) genuinely fails
    # -- counted as an error.
    assert summary == RunSummary(
        processed=1,
        skipped=0,
        errors=1,
        ungrouped=0,
        llm_reprocessed=0,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
        total_pages_processed=2,
    )

    entry = get_entry(conn, str(stray_pdf.resolve()))
    assert entry is not None
    assert entry.mathpix_status == "success"
    assert entry.page_count == 2
    assert entry.llm_status == "success"
    assert entry.vault_status == "failed"
    assert entry.vault_path is None

    markdown_path = paths_config.cache_dir / "_ungrouped" / "stray.mathpix.md"
    assert markdown_path.is_file()


@respx.mock
def test_run_target_source_path_with_force_llm_reprocesses_only_that_file(
    client, tmp_path, monkeypatch
):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    target_pdf = _write_pdf(course_dir / "lecture_01.pdf")
    sibling_pdf = _write_pdf(course_dir / "lecture_02.pdf")

    # Both files already fully up to date.
    _upsert_unchanged_entry(conn, target_pdf, mathpix_status="success", llm_status="success")
    _upsert_unchanged_entry(conn, sibling_pdf, mathpix_status="success", llm_status="success")
    sibling_entry_before = get_entry(conn, str(sibling_pdf.resolve()))

    cache_dir = paths_config.cache_dir / "class_1"
    cache_dir.mkdir(parents=True)
    (cache_dir / "lecture_01.mathpix.md").write_text("raw markdown", encoding="utf-8")

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        force_llm=True,
        target_source_path=target_pdf,
    )

    assert summary == RunSummary(
        processed=0,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=1,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
    )
    assert not submit_route.called
    assert len(llm_calls) == 1

    sibling_entry_after = get_entry(conn, str(sibling_pdf.resolve()))
    assert sibling_entry_after == sibling_entry_before


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
            processed=1,
            skipped=2,
            errors=1,
            ungrouped=0,
            total_input_tokens=1234,
            total_output_tokens=987,
            total_cost_estimate=0.0041,
            total_pages_processed=7,
        ),
    )

    exit_code = main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Documents processed: 1" in out
    assert "Pages processed:     7" in out
    assert "Input tokens:        1234" in out
    assert "Output tokens:       987" in out
    assert "Est. cost:           $0.0041" in out
    assert "Errors:              1" in out
