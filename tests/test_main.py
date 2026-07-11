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

Covered here (issue #40 — manual vault-edit conflict detection):
    - A vault note manually edited after a prior successful run is left
      untouched on a later reprocessing run: the write is skipped,
      vault_status is recorded as "conflict", vault_path/vault_written_at/
      vault_content_hash are left unchanged from the prior run, and
      RunSummary.vault_conflicts (not errors) reflects the skip -- the
      reprocessed content still lands under _cache/ as normal.

Covered here (issue #41 — --course CLI scaffolding):
    - run(): course restricts the run to one course subdirectory -- a
      sibling course's files are never even classified/written to
      state.db.
    - run(): an unknown course name is a clean, all-zero-summary no-op
      (a warning is printed, not an exception).
    - run(): course and target_source_path together raise ValueError
      (see src/cli.py's module docstring for why the CLI-level --file
      rejection itself is deferred to issue #44).
    - main(): a --course argv value is parsed by src/cli.py's
      build_arg_parser() and forwarded into run() as its course= kwarg.
      See tests/test_cli.py for build_arg_parser()'s own parsing-only
      tests.

Covered here (issue #42 — --dry-run):
    - run(): a NEW file's dry run reports it would be processed (tallied as
      processed) without ever calling the mocked Mathpix submit endpoint or
      writing a state.db row for it.
    - run(): an UNCHANGED file eligible for LLM-only reprocessing reports
      it would be reprocessed (tallied as llm_reprocessed) without the
      fake cleanup_pdf() ever actually being called.
    - run(): a fully up-to-date UNCHANGED file is reported as skipped.
    - run(): dry_run=True with no client= injected never calls
      load_mathpix_credentials() -- no Mathpix credentials are required at
      all in dry-run mode.
    - main(): a --dry-run argv flag is parsed and forwarded into run() as
      its dry_run= kwarg.

Covered here (issue #43 — --force):
    - run(): an UNCHANGED-and-fully-current file (normally a full skip) is
      fully reprocessed under force=True -- process_pdf()/cleanup_pdf() are
      both actually called, and it's tallied as processed rather than
      skipped.
    - run(): force=True implies a fresh LLM pass even with force_llm left
      at its default (False) -- no need to pass both together.
    - run(): force=True on an already-actionable NEW file is a pure no-op
      change (identical RunSummary to a plain, non-forced run) -- no
      regression to the existing actionable path.
    - main(): a --force argv flag is parsed and forwarded into run() as its
      force= kwarg.

Covered here (issue #44 — --rerun-llm / --file):
    - main(): --rerun-llm/--file argv flags are parsed and forwarded into
      run() as its force_llm=/target_source_path= kwargs.
    - main(): --file is rejected (exit code 1, run() never called) when
      the path doesn't exist, doesn't end in .pdf (case-insensitive
      accepted), or resolves outside paths.input_root.
    - main(): --file combined with --rerun-llm reprocesses only the
      target's LLM stage when it's UNCHANGED (no Mathpix call), leaving a
      sibling file's state.db row untouched -- the documented "reprocess
      just this one lecture's LLM stage after tweaking the prompt"
      workflow, exercised end-to-end through main(argv) itself.

Covered here (issue #45 — --force-vault-overwrite):
    - run(): force_vault_overwrite=True clears a previously-recorded
      vault_status="conflict" -- the manually-edited vault note is
      overwritten with the pipeline's content, vault_status returns to
      "success", and a fresh vault_path/vault_written_at/
      vault_content_hash are recorded. RunSummary.vault_conflicts stays 0
      for that file, since the write was never actually skipped.
    - run(): force_vault_overwrite=False (the default) leaves issue #40's
      conflict-preserving behavior completely unaffected.
    - main(): a --force-vault-overwrite argv flag is parsed and forwarded
      into run() as its force_vault_overwrite= kwarg.
    - run(): force_vault_overwrite=True passed *alone* (no force_llm/
      --rerun-llm) still retries the vault write for a file that's
      UNCHANGED with mathpix_status="success"/llm_status="success" already
      recorded but whose vault write was recorded as a conflict -- a
      real-world gap found after this issue's initial implementation (see
      _needs_vault_conflict_retry()): reuses the cached LLM output with no
      new Mathpix/LLM API calls, tallied as skipped (not llm_reprocessed,
      since no LLM call happened).
    - run(): the same scenario under dry_run=True reports the would-be
      vault-write retry (tallied as skipped) without touching the vault
      file, state.db, or calling cleanup_pdf().

Covered here (issue #46 — --no-llm):
    - run(): no_llm=True on a NEW file runs process_pdf() as normal but
      never calls cleanup_pdf() -- only mathpix_*/figure_count/page_count/
      mathpix_processed_at are recorded, llm_status (and every other
      llm_*/output_path field) stays None, and the vault note is written
      from the raw .mathpix.md rather than any LLM-cleaned content.
    - run(): a file processed with no_llm=True is picked up by a later
      normal (non-no_llm) run via needs_llm_reprocessing() for a real LLM
      pass -- no Mathpix API call the second time, exercised end-to-end
      rather than just asserted by inspection.
    - run(): no_llm=True on an UNCHANGED file otherwise eligible for
      LLM-only reprocessing (llm_status=None or force_llm=True) is simply
      skipped -- cleanup_pdf() is never called.
    - main(): a --no-llm argv flag is parsed and forwarded into run() as
      its no_llm= kwarg.

Covered here (issue #52 — found during #51's real-data validation:
no_llm=True used to leave a previously-successful file's stale llm_status
untouched when reprocessing it):
    - run(): force=True + no_llm=True reprocessing a file that already has
      a genuine prior llm_status="success" (and populated llm_model/
      output_path/tokens/etc.) explicitly resets every llm_*/output_path
      field to None, rather than leaving the old success data stale --
      the vault note ends up with fresh raw content, matching what
      state.db now (correctly) says about the LLM stage.
    - run(): after that reset, a later plain run (no flags) still
      auto-picks the file up for a real LLM pass via
      needs_llm_reprocessing() -- proving the fix restores issue #46's
      "later normal run picks it up automatically" guarantee for a
      previously-processed file, not just a genuinely fresh one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
import yaml

from src.config import ConfigError, LLMConfig, NamingConfig, OutputConfig, PathsConfig
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


def _make_output_config(**overrides) -> OutputConfig:
    """Explicit OutputConfig for tests, mirroring _make_llm_config()'s
    precedent of never relying on run()'s internal load_output_config()
    fallback (which reads a real, gitignored config.yaml relative to cwd) --
    keeps every test hermetic/deterministic. See
    test_run_wires_real_output_and_naming_config_end_to_end for the one
    test that deliberately exercises the real internal-load path instead."""
    defaults = dict(
        course_tags={},
        date_format="%Y-%m-%d",
        figures_dark_mode_flag=False,
    )
    defaults.update(overrides)
    return OutputConfig(**defaults)


def _make_naming_config(**overrides) -> NamingConfig:
    """Explicit NamingConfig for tests -- same rationale as
    _make_output_config()."""
    defaults = dict(lecture_prefix="Lecture")
    defaults.update(overrides)
    return NamingConfig(**defaults)


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
    content: str = "cleaned markdown",
):
    """
    Monkeypatch src.main.cleanup_pdf with a fake that never touches
    litellm/a real API. Returns the list of call-argument dicts recorded,
    so tests can assert on what run() passed through.

    status="success" writes a real {lecture_stem}.llm.md into dest_dir
    (mirroring cleanup_pdf()'s real success behavior), containing `content`
    (defaults to the plain "cleaned markdown" string used by most tests;
    override with e.g. a ![](figures/...) reference to exercise vault
    dark-mode alt-text rewriting -- issue #37), and returns a
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

    def _fake_cleanup_pdf(
        mathpix_markdown_path, dest_dir, lecture_stem, llm_config, client=None, on_status=None
    ):
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
            output_path.write_text(content, encoding="utf-8")
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

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
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

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

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

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
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
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force_llm=True,
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
def test_run_force_fully_reprocesses_unchanged_and_current_file(client, tmp_path, monkeypatch):
    """Issue #43: force=True reclassifies an otherwise-UNCHANGED-and-fully-
    current file (which would normally be a full skip, per
    test_run_skips_unchanged_and_current_file_entirely) as RETRY, so both
    process_pdf() and cleanup_pdf() actually run and the file is tallied as
    processed. force_llm is deliberately left at its default (False) here --
    force alone is sufficient to trigger a fresh LLM pass too, since the
    actionable branch always calls cleanup_pdf() unconditionally."""
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    # llm_status="success" -> fully up to date, not just Mathpix-unchanged.
    _upsert_unchanged_entry(conn, pdf_path, mathpix_status="success", llm_status="success")

    submit_route = _mock_happy_path()
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force=True,
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
    assert submit_route.called
    assert len(llm_calls) == 1

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry is not None
    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "abc123"
    assert entry.llm_status == "success"


@respx.mock
def test_run_force_on_new_file_matches_plain_run(client, tmp_path, monkeypatch):
    """Issue #43: force=True on an already-actionable NEW file changes
    nothing -- confirms no regression to the existing non-forced actionable
    path (mirrors test_run_processes_new_file_and_records_success's plain-
    run expectations exactly, just with force=True passed)."""
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    _mock_happy_path()
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force=True,
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
    assert len(llm_calls) == 1

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry is not None
    assert entry.mathpix_status == "success"
    assert entry.llm_status == "success"


@respx.mock
def test_run_no_llm_processes_mathpix_only_and_writes_raw_vault_note(
    client, tmp_path, monkeypatch
):
    """
    Issue #46: no_llm=True on a NEW file runs process_pdf() as normal but
    never calls cleanup_pdf() -- only mathpix_*/figure_count/page_count/
    mathpix_processed_at are recorded, llm_status (and every other llm_*/
    output_path field) stays None, and the vault note is written straight
    from the raw .mathpix.md (not any LLM-cleaned content).
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    _mock_happy_path()
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        no_llm=True,
    )

    assert summary == RunSummary(
        processed=1,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=0,
        total_pages_processed=2,
    )
    # cleanup_pdf() was never called for this file.
    assert len(llm_calls) == 0

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry is not None
    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "abc123"
    assert entry.figure_count == 2
    assert entry.page_count == 2
    assert entry.mathpix_processed_at is not None

    # LLM stage was never attempted.
    assert entry.llm_status is None
    assert entry.llm_model is None
    assert entry.llm_prompt_version is None
    assert entry.llm_processed_at is None
    assert entry.output_path is None
    assert entry.llm_input_tokens is None
    assert entry.llm_output_tokens is None
    assert entry.llm_cost_estimate is None

    # Vault note was still written, sourced from the raw .mathpix.md.
    assert entry.vault_status == "success"
    vault_path = paths_config.vault_root / "class_1" / "Lecture 01.md"
    assert Path(entry.vault_path) == vault_path
    assert vault_path.is_file()
    vault_content = vault_path.read_text(encoding="utf-8")
    assert "Some intro text discussing vectors and matrices." in vault_content
    assert "cleaned markdown" not in vault_content


@respx.mock
def test_run_no_llm_file_picked_up_by_later_normal_run(client, tmp_path, monkeypatch):
    """
    Issue #46: a file processed with no_llm=True has llm_status left None,
    so a later normal run (no_llm=False, no --force/--rerun-llm needed)
    picks it up via needs_llm_reprocessing() for a real LLM pass -- no
    Mathpix API call the second time, and the vault note is rewritten with
    the LLM-cleaned content.
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    submit_route = _mock_happy_path()
    first_run_llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    first_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        no_llm=True,
    )
    assert first_summary.processed == 1
    assert len(first_run_llm_calls) == 0
    assert submit_route.call_count == 1

    entry_after_first_run = get_entry(conn, str(pdf_path.resolve()))
    assert entry_after_first_run.llm_status is None

    second_run_llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    second_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

    # No further Mathpix API call -- the file is UNCHANGED, only its LLM
    # stage is (re)run.
    assert submit_route.call_count == 1
    assert second_summary == RunSummary(
        processed=0,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=1,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
    )
    assert len(second_run_llm_calls) == 1

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry.llm_status == "success"
    assert entry.mathpix_status == "success"

    vault_path = paths_config.vault_root / "class_1" / "Lecture 01.md"
    assert "cleaned markdown" in vault_path.read_text(encoding="utf-8")


@respx.mock
def test_run_no_llm_skips_unchanged_file_eligible_for_llm_reprocessing(
    client, tmp_path, monkeypatch
):
    """
    Issue #46: no_llm=True on an UNCHANGED file that's otherwise eligible
    for LLM-only reprocessing (llm_status=None, or force_llm=True) is
    simply skipped -- there's nothing for --no-llm to do there, and
    cleanup_pdf() is never called.
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    # llm_status=None -> would normally trigger LLM-only reprocessing.
    _upsert_unchanged_entry(conn, pdf_path, mathpix_status="success", llm_status=None)

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        no_llm=True,
        force_llm=True,
    )

    assert summary == RunSummary(
        processed=0,
        skipped=1,
        errors=0,
        ungrouped=0,
        llm_reprocessed=0,
    )
    assert not submit_route.called
    assert len(llm_calls) == 0


@respx.mock
def test_run_no_llm_resets_stale_llm_fields_when_reprocessing_already_successful_file(
    client, tmp_path, monkeypatch
):
    """
    Issue #52 (found live during #51's real-data validation): the
    actionable-path no_llm=True upsert used to only ever be reached for a
    genuinely fresh file, whose llm_*/output_path columns are already
    None -- so simply *omitting* those columns from the upsert (issue
    #46's original implementation) looked like a no-op. But the same
    branch is also reached when force=True reclassifies an
    already-fully-successful UNCHANGED file to RETRY (or, in real usage,
    a genuine second edit to the source PDF) -- and there, omitting those
    columns left the *previous* run's real llm_status="success" (and
    llm_model/output_path/token counts/etc.) stale and untouched, even
    though the vault note was correctly overwritten with fresh raw
    (uncleaned) OCR text this run. Confirms the fix: every llm_*/
    output_path column is explicitly reset to None in this case too.
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    # Seed a state.db row describing a genuine prior mathpix+LLM success,
    # with real-looking llm_* data populated -- mirroring what an actual
    # earlier cleanup_pdf() success would have written.
    stat = pdf_path.stat()
    stale_processed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    upsert_entry(
        conn,
        str(pdf_path.resolve()),
        source_hash="whatever",
        source_mtime=stat.st_mtime,
        source_size=stat.st_size,
        mathpix_status="success",
        mathpix_pdf_id="old-pdf-id",
        llm_model="old-llm-model",
        llm_prompt_version="cleanup_v0",
        llm_status="success",
        llm_validation_result=json.dumps({"length_ratio": True}),
        output_path="_cache/class_1/lecture_01.llm.md",
        mathpix_processed_at=stale_processed_at,
        llm_processed_at=stale_processed_at,
        llm_input_tokens=999,
        llm_output_tokens=888,
        llm_cost_estimate=0.5,
    )

    _mock_happy_path(pdf_id="new-pdf-id")
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force=True,
        no_llm=True,
    )

    assert summary == RunSummary(
        processed=1,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=0,
        total_pages_processed=2,
    )
    # cleanup_pdf() must never be called -- --no-llm skips the LLM stage
    # entirely, even on this reprocessed-not-fresh path.
    assert len(llm_calls) == 0

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry is not None
    # Mathpix stage reprocessed fresh.
    assert entry.mathpix_status == "success"
    assert entry.mathpix_pdf_id == "new-pdf-id"

    # The bug: these used to keep their stale pre-reprocess values. The
    # fix: every llm_*/output_path field is explicitly reset to None.
    assert entry.llm_status is None
    assert entry.llm_model is None
    assert entry.llm_prompt_version is None
    assert entry.llm_validation_result is None
    assert entry.llm_processed_at is None
    assert entry.output_path is None
    assert entry.llm_input_tokens is None
    assert entry.llm_output_tokens is None
    assert entry.llm_cost_estimate is None

    # Vault note reflects the fresh raw (uncleaned) content -- not the old
    # LLM-cleaned content its now-cleared output_path used to point at.
    assert entry.vault_status == "success"
    vault_path = paths_config.vault_root / "class_1" / "Lecture 01.md"
    vault_content = vault_path.read_text(encoding="utf-8")
    assert "Some intro text discussing vectors and matrices." in vault_content
    assert "cleaned markdown" not in vault_content


@respx.mock
def test_run_no_llm_reset_allows_auto_pickup_after_forced_reprocess(
    client, tmp_path, monkeypatch
):
    """
    Issue #52 follow-up to the test above: once a previously-successful
    file's llm_status is correctly reset to None by a force=True,
    no_llm=True reprocess, a later plain run (no flags at all) must still
    auto-pick it up for a real LLM pass via needs_llm_reprocessing() --
    proving the fix restores issue #46's original "a later normal run
    picks this file up automatically" guarantee for a previously-
    processed file, not just a genuinely fresh one (already covered by
    test_run_no_llm_file_picked_up_by_later_normal_run).
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    stat = pdf_path.stat()
    stale_processed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    upsert_entry(
        conn,
        str(pdf_path.resolve()),
        source_hash="whatever",
        source_mtime=stat.st_mtime,
        source_size=stat.st_size,
        mathpix_status="success",
        mathpix_pdf_id="old-pdf-id",
        llm_model="old-llm-model",
        llm_prompt_version="cleanup_v0",
        llm_status="success",
        llm_validation_result=json.dumps({"length_ratio": True}),
        output_path="_cache/class_1/lecture_01.llm.md",
        mathpix_processed_at=stale_processed_at,
        llm_processed_at=stale_processed_at,
        llm_input_tokens=999,
        llm_output_tokens=888,
        llm_cost_estimate=0.5,
    )

    submit_route = _mock_happy_path(pdf_id="new-pdf-id")
    first_run_llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force=True,
        no_llm=True,
    )
    assert len(first_run_llm_calls) == 0
    assert submit_route.call_count == 1

    entry_after_reprocess = get_entry(conn, str(pdf_path.resolve()))
    assert entry_after_reprocess.llm_status is None

    second_run_llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    second_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

    # No further Mathpix API call -- the file is UNCHANGED (relative to
    # the force+no_llm reprocess above), only its LLM stage is (re)run.
    assert submit_route.call_count == 1
    assert second_summary == RunSummary(
        processed=0,
        skipped=0,
        errors=0,
        ungrouped=0,
        llm_reprocessed=1,
        total_input_tokens=100,
        total_output_tokens=50,
        total_cost_estimate=0.001,
    )
    assert len(second_run_llm_calls) == 1

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry.llm_status == "success"
    assert entry.mathpix_status == "success"

    vault_path = paths_config.vault_root / "class_1" / "Lecture 01.md"
    assert "cleaned markdown" in vault_path.read_text(encoding="utf-8")


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

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

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

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

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
def test_run_detects_manually_edited_vault_note_and_skips_overwrite(
    client, tmp_path, monkeypatch
):
    """
    Issue #40: a vault note manually edited after a prior successful run
    must not be silently clobbered by a later reprocessing run.

    Scenario: process a file successfully (vault note written, its
    content hash recorded); manually mutate the written vault file
    directly (bypassing the pipeline, simulating a user edit); change the
    source PDF so the next run reprocesses it fully; assert the vault
    file's content is untouched, vault_status == "conflict",
    vault_path/vault_written_at/vault_content_hash are unchanged from the
    first run, RunSummary.vault_conflicts == 1, RunSummary.errors is
    unaffected, and the reprocessed content is present under _cache/.
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf", b"original pdf bytes")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success", content="first cleaned markdown")

    first_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )
    assert first_summary.errors == 0
    assert first_summary.vault_conflicts == 0

    entry_after_first = get_entry(conn, str(pdf_path.resolve()))
    assert entry_after_first.vault_status == "success"
    assert entry_after_first.vault_content_hash is not None
    vault_path = Path(entry_after_first.vault_path)
    assert vault_path.is_file()
    original_vault_path = entry_after_first.vault_path
    original_vault_written_at = entry_after_first.vault_written_at
    original_vault_content_hash = entry_after_first.vault_content_hash

    # Simulate a manual edit to the vault note, bypassing the pipeline
    # entirely.
    manual_content = "MANUALLY EDITED -- do not clobber!\n"
    vault_path.write_text(manual_content, encoding="utf-8")

    # Change the source PDF so the next run reprocesses it fully (CHANGED
    # classification -> full Mathpix + LLM + vault-write pipeline again).
    _write_pdf(pdf_path, b"changed pdf bytes -- forces reprocessing")
    _install_fake_cleanup_pdf(monkeypatch, status="success", content="second cleaned markdown")

    second_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

    assert second_summary.processed == 1
    assert second_summary.errors == 0
    assert second_summary.vault_conflicts == 1

    entry_after_second = get_entry(conn, str(pdf_path.resolve()))
    assert entry_after_second.vault_status == "conflict"
    # Untouched -- they still correctly describe the last file we
    # actually wrote (the one the user is now editing).
    assert entry_after_second.vault_path == original_vault_path
    assert entry_after_second.vault_written_at == original_vault_written_at
    assert entry_after_second.vault_content_hash == original_vault_content_hash

    # The vault file itself must still hold the manual edit.
    assert vault_path.read_text(encoding="utf-8") == manual_content

    # The reprocessing itself succeeded and its output lives under _cache/.
    assert entry_after_second.mathpix_status == "success"
    assert entry_after_second.llm_status == "success"
    cache_llm_path = Path(entry_after_second.output_path)
    assert cache_llm_path.is_file()
    assert "second cleaned markdown" in cache_llm_path.read_text(encoding="utf-8")


@respx.mock
def test_run_force_vault_overwrite_clears_recorded_conflict(client, tmp_path, monkeypatch):
    """
    Issue #45: force_vault_overwrite=True is the escape hatch for a
    previously-recorded vault_status="conflict" (issue #40) -- the user
    has decided the pipeline's version should win, so a subsequent run
    with the flag set overwrites the manually-edited vault note
    unconditionally and clears vault_status back to "success" with a
    fresh vault_content_hash/vault_path/vault_written_at.

    Scenario: same conflict setup as
    test_run_detects_manually_edited_vault_note_and_skips_overwrite (a
    successful first write, a manual edit, then a reprocessing run that
    detects the conflict and skips), followed by a third run with
    force_vault_overwrite=True.
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf", b"original pdf bytes")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success", content="first cleaned markdown")

    first_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )
    assert first_summary.vault_conflicts == 0
    entry_after_first = get_entry(conn, str(pdf_path.resolve()))
    vault_path = Path(entry_after_first.vault_path)
    original_vault_content_hash = entry_after_first.vault_content_hash

    # Simulate a manual edit, then change the source PDF to force
    # reprocessing (mirrors the #40 test's setup exactly).
    manual_content = "MANUALLY EDITED -- do not clobber!\n"
    vault_path.write_text(manual_content, encoding="utf-8")
    _write_pdf(pdf_path, b"changed pdf bytes -- forces reprocessing")
    _install_fake_cleanup_pdf(monkeypatch, status="success", content="second cleaned markdown")

    second_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )
    assert second_summary.vault_conflicts == 1

    entry_after_second = get_entry(conn, str(pdf_path.resolve()))
    assert entry_after_second.vault_status == "conflict"
    assert vault_path.read_text(encoding="utf-8") == manual_content

    # Third run: same already-reprocessed state (UNCHANGED now), but with
    # force_vault_overwrite=True -- the conflict must clear.
    third_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force_llm=True,
        force_vault_overwrite=True,
    )

    assert third_summary.errors == 0
    assert third_summary.vault_conflicts == 0

    entry_after_third = get_entry(conn, str(pdf_path.resolve()))
    assert entry_after_third.vault_status == "success"
    assert entry_after_third.vault_content_hash is not None
    assert entry_after_third.vault_content_hash != original_vault_content_hash
    # The manual edit is gone -- overwritten with the pipeline's content.
    written = vault_path.read_text(encoding="utf-8")
    assert "second cleaned markdown" in written
    assert "MANUALLY EDITED" not in written


@respx.mock
def test_run_force_vault_overwrite_leaves_non_conflicting_write_unaffected(
    client, tmp_path, monkeypatch
):
    """Issue #45: force_vault_overwrite=True on a file with no recorded
    conflict behaves identically to a normal write -- vault_status is
    "success" and the content matches, same as force_vault_overwrite's
    default (False)."""
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success", content="cleaned markdown")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force_vault_overwrite=True,
    )

    assert summary.errors == 0
    assert summary.vault_conflicts == 0
    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry.vault_status == "success"
    assert "cleaned markdown" in Path(entry.vault_path).read_text(encoding="utf-8")


@respx.mock
def test_run_force_vault_overwrite_alone_retries_vault_write_without_rerun_llm(
    client, tmp_path, monkeypatch
):
    """
    Regression test for a real-world gap found after issue #45's initial
    implementation: force_vault_overwrite=True passed *alone* (no
    force_llm/--rerun-llm) must still retry the vault write for a file
    that's UNCHANGED with mathpix_status="success" and llm_status="success"
    already recorded (e.g. from a prior run that fully reprocessed it) but
    whose vault write was recorded as a conflict.

    Before this fix: needs_llm_reprocessing() returns False once
    llm_status="success" is recorded, so with force_llm left at its
    default (False), the file was fully skipped by _process_file() before
    it ever reached _write_to_vault() again -- force_vault_overwrite had
    no effect at all in this scenario, since nothing routed the file back
    through a branch that calls _write_to_vault().

    Scenario: process a file successfully; manually edit its vault note;
    change the source PDF so the next run reprocesses it fully (mathpix +
    llm), detecting and recording the conflict; then, with the source PDF
    left unchanged (UNCHANGED classification) and llm_status already
    "success", run a third time with force_vault_overwrite=True alone --
    the vault write must be retried (no new Mathpix/LLM API calls), the
    manual edit overwritten, and vault_status returned to "success".
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf", b"original pdf bytes")

    submit_route = _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success", content="first cleaned markdown")

    first_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )
    assert first_summary.vault_conflicts == 0
    assert submit_route.call_count == 1

    entry_after_first = get_entry(conn, str(pdf_path.resolve()))
    vault_path = Path(entry_after_first.vault_path)

    # Manual edit to the vault note, then change the source PDF so the
    # next run reprocesses it fully (same setup as the #40 conflict test).
    manual_content = "MANUALLY EDITED -- do not clobber!\n"
    vault_path.write_text(manual_content, encoding="utf-8")
    _write_pdf(pdf_path, b"changed pdf bytes -- forces reprocessing")
    llm_calls = _install_fake_cleanup_pdf(
        monkeypatch, status="success", content="second cleaned markdown"
    )

    second_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )
    assert second_summary.vault_conflicts == 1
    assert submit_route.call_count == 2
    assert len(llm_calls) == 1

    entry_after_second = get_entry(conn, str(pdf_path.resolve()))
    assert entry_after_second.vault_status == "conflict"
    assert entry_after_second.mathpix_status == "success"
    assert entry_after_second.llm_status == "success"
    assert vault_path.read_text(encoding="utf-8") == manual_content

    # Third run: source PDF unchanged since run 2 (UNCHANGED
    # classification) and llm_status is already "success" -- not eligible
    # for needs_llm_reprocessing(). Only force_vault_overwrite is passed,
    # deliberately without force_llm/--rerun-llm.
    third_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force_vault_overwrite=True,
    )

    assert third_summary.errors == 0
    assert third_summary.vault_conflicts == 0
    assert third_summary.processed == 0
    assert third_summary.llm_reprocessed == 0
    assert third_summary.skipped == 1
    # No additional Mathpix/LLM calls -- only the vault write was retried.
    assert submit_route.call_count == 2
    assert len(llm_calls) == 1

    entry_after_third = get_entry(conn, str(pdf_path.resolve()))
    assert entry_after_third.vault_status == "success"
    assert entry_after_third.vault_content_hash is not None
    written = vault_path.read_text(encoding="utf-8")
    assert "second cleaned markdown" in written
    assert "MANUALLY EDITED" not in written


@respx.mock
def test_run_dry_run_reports_vault_conflict_retry_without_writing(client, tmp_path, monkeypatch):
    """
    Companion dry-run coverage for the regression test above: --dry-run
    --force-vault-overwrite on the same UNCHANGED/llm-already-succeeded/
    recorded-conflict scenario reports the would-be vault-write retry
    (tallied as skipped, mirroring the real run's tally) without touching
    the vault file, state.db, or calling cleanup_pdf().
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf", b"original pdf bytes")

    submit_route = _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success", content="first cleaned markdown")

    run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )
    entry_after_first = get_entry(conn, str(pdf_path.resolve()))
    vault_path = Path(entry_after_first.vault_path)
    manual_content = "MANUALLY EDITED -- do not clobber!\n"
    vault_path.write_text(manual_content, encoding="utf-8")
    _write_pdf(pdf_path, b"changed pdf bytes -- forces reprocessing")
    llm_calls = _install_fake_cleanup_pdf(
        monkeypatch, status="success", content="second cleaned markdown"
    )

    run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )
    assert get_entry(conn, str(pdf_path.resolve())).vault_status == "conflict"
    assert submit_route.call_count == 2
    assert len(llm_calls) == 1

    dry_run_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        force_vault_overwrite=True,
        dry_run=True,
    )

    assert dry_run_summary.skipped == 1
    assert dry_run_summary.processed == 0
    assert dry_run_summary.llm_reprocessed == 0
    assert dry_run_summary.errors == 0
    assert dry_run_summary.vault_conflicts == 0
    # Nothing actually happened -- no new API calls, vault untouched,
    # state.db's vault_status still "conflict".
    assert submit_route.call_count == 2
    assert len(llm_calls) == 1
    assert vault_path.read_text(encoding="utf-8") == manual_content
    assert get_entry(conn, str(pdf_path.resolve())).vault_status == "conflict"


@respx.mock
def test_run_skips_ungrouped_pdfs_without_writing_state(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    stray_pdf = _write_pdf(paths_config.input_root / "stray.pdf")

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

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

    first_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )
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

    second_summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

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
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
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
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
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
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
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


@respx.mock
def test_run_course_restricts_to_one_course(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    class_1_dir = paths_config.input_root / "class_1"
    class_1_dir.mkdir()
    class_2_dir = paths_config.input_root / "class_2"
    class_2_dir.mkdir()
    target_pdf = _write_pdf(class_1_dir / "lecture_01.pdf")
    other_course_pdf = _write_pdf(class_2_dir / "lecture_01.pdf")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        course="class_1",
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

    # class_2's file was never even classified -- discover_pdfs()'s full
    # scan happened, but the outer loop skipped every course except
    # class_1 entirely, so it has no state.db row at all.
    assert get_entry(conn, str(other_course_pdf.resolve())) is None


@respx.mock
def test_run_unknown_course_is_a_clean_noop(client, tmp_path, monkeypatch, capsys):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        course="does_not_exist",
    )

    assert summary == RunSummary(processed=0, skipped=0, errors=0, ungrouped=0, llm_reprocessed=0)
    assert not submit_route.called
    assert llm_calls == []
    assert get_entry(conn, str(pdf_path.resolve())) is None
    assert "does_not_exist" in capsys.readouterr().out


def test_run_course_and_target_source_path_are_mutually_exclusive(tmp_path):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    with pytest.raises(ValueError, match="mutually exclusive"):
        run(
            paths_config,
            conn,
            llm_config=_make_llm_config(),
            output_config=_make_output_config(),
            naming_config=_make_naming_config(),
            course="class_1",
            target_source_path=pdf_path,
        )


def test_main_parses_course_flag_and_forwards_it_to_run(monkeypatch, tmp_path):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    received_kwargs: dict = {}

    def _fake_run(paths_config, conn, **kwargs):
        received_kwargs.update(kwargs)
        return RunSummary(processed=0, skipped=0, errors=0, ungrouped=0)

    monkeypatch.setattr(main_module, "run", _fake_run)

    exit_code = main(["--course", "class_1"])

    assert exit_code == 0
    assert received_kwargs["course"] == "class_1"


@respx.mock
def test_run_dry_run_reports_new_file_without_processing(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        dry_run=True,
    )

    assert summary == RunSummary(processed=1, skipped=0, errors=0, ungrouped=0, llm_reprocessed=0)
    assert not submit_route.called
    assert llm_calls == []
    assert get_entry(conn, str(pdf_path.resolve())) is None


@respx.mock
def test_run_dry_run_reports_llm_only_rerun_without_calling_llm(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    # llm_status=None -> stale, eligible for LLM-only reprocessing.
    _upsert_unchanged_entry(conn, pdf_path, mathpix_status="success", llm_status=None)

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        dry_run=True,
    )

    assert summary == RunSummary(processed=0, skipped=0, errors=0, ungrouped=0, llm_reprocessed=1)
    assert not submit_route.called
    assert llm_calls == []


@respx.mock
def test_run_dry_run_skips_fully_up_to_date_file(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    # llm_status="success" -> fully up to date, not eligible for any rerun.
    _upsert_unchanged_entry(conn, pdf_path, mathpix_status="success", llm_status="success")

    submit_route = respx.post(f"{MATHPIX_BASE_URL}/v3/pdf").mock(
        return_value=httpx.Response(200, json={"pdf_id": "abc123"})
    )
    llm_calls = _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        dry_run=True,
    )

    assert summary == RunSummary(processed=0, skipped=1, errors=0, ungrouped=0, llm_reprocessed=0)
    assert not submit_route.called
    assert llm_calls == []


def test_run_dry_run_requires_no_mathpix_credentials(tmp_path, monkeypatch):
    import src.main as main_module

    def _raise_if_called():
        raise AssertionError("should not be called in dry run")

    monkeypatch.setattr(main_module, "load_mathpix_credentials", _raise_if_called)

    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    _write_pdf(course_dir / "lecture_01.pdf")

    summary = run(
        paths_config,
        conn,
        client=None,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        dry_run=True,
    )

    assert summary.processed == 1


def test_main_parses_dry_run_flag_and_forwards_it_to_run(monkeypatch, tmp_path):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    received_kwargs: dict = {}

    def _fake_run(paths_config, conn, **kwargs):
        received_kwargs.update(kwargs)
        return RunSummary(processed=0, skipped=0, errors=0, ungrouped=0)

    monkeypatch.setattr(main_module, "run", _fake_run)

    exit_code = main(["--dry-run"])

    assert exit_code == 0
    assert received_kwargs["dry_run"] is True


def test_main_parses_force_flag_and_forwards_it_to_run(monkeypatch, tmp_path):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    received_kwargs: dict = {}

    def _fake_run(paths_config, conn, **kwargs):
        received_kwargs.update(kwargs)
        return RunSummary(processed=0, skipped=0, errors=0, ungrouped=0)

    monkeypatch.setattr(main_module, "run", _fake_run)

    exit_code = main(["--force"])

    assert exit_code == 0
    assert received_kwargs["force"] is True


def test_main_parses_force_vault_overwrite_flag_and_forwards_it_to_run(monkeypatch, tmp_path):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    received_kwargs: dict = {}

    def _fake_run(paths_config, conn, **kwargs):
        received_kwargs.update(kwargs)
        return RunSummary(processed=0, skipped=0, errors=0, ungrouped=0)

    monkeypatch.setattr(main_module, "run", _fake_run)

    exit_code = main(["--force-vault-overwrite"])

    assert exit_code == 0
    assert received_kwargs["force_vault_overwrite"] is True


def test_main_parses_no_llm_flag_and_forwards_it_to_run(monkeypatch, tmp_path):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    received_kwargs: dict = {}

    def _fake_run(paths_config, conn, **kwargs):
        received_kwargs.update(kwargs)
        return RunSummary(processed=0, skipped=0, errors=0, ungrouped=0)

    monkeypatch.setattr(main_module, "run", _fake_run)

    exit_code = main(["--no-llm"])

    assert exit_code == 0
    assert received_kwargs["no_llm"] is True


def test_main_parses_rerun_llm_flag_and_forwards_it_to_run(monkeypatch, tmp_path):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    received_kwargs: dict = {}

    def _fake_run(paths_config, conn, **kwargs):
        received_kwargs.update(kwargs)
        return RunSummary(processed=0, skipped=0, errors=0, ungrouped=0)

    monkeypatch.setattr(main_module, "run", _fake_run)

    exit_code = main(["--rerun-llm"])

    assert exit_code == 0
    assert received_kwargs["force_llm"] is True


def test_main_parses_file_flag_and_forwards_it_to_run(monkeypatch, tmp_path):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    received_kwargs: dict = {}

    def _fake_run(paths_config, conn, **kwargs):
        received_kwargs.update(kwargs)
        return RunSummary(processed=0, skipped=0, errors=0, ungrouped=0)

    monkeypatch.setattr(main_module, "run", _fake_run)

    exit_code = main(["--file", str(pdf_path)])

    assert exit_code == 0
    assert received_kwargs["target_source_path"] == str(pdf_path)


def test_main_rejects_nonexistent_file_with_exit_code_1(monkeypatch, tmp_path, capsys):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    def _raise_if_called(*args, **kwargs):
        raise AssertionError("run() should not be called for an invalid --file path")

    monkeypatch.setattr(main_module, "run", _raise_if_called)

    missing_pdf = paths_config.input_root / "class_1" / "lecture_01.pdf"
    exit_code = main(["--file", str(missing_pdf)])

    assert exit_code == 1
    assert "does not exist" in capsys.readouterr().err


def test_main_rejects_non_pdf_file_with_exit_code_1(monkeypatch, tmp_path, capsys):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    def _raise_if_called(*args, **kwargs):
        raise AssertionError("run() should not be called for a non-.pdf --file path")

    monkeypatch.setattr(main_module, "run", _raise_if_called)

    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    not_a_pdf = course_dir / "notes.txt"
    not_a_pdf.write_text("not a pdf", encoding="utf-8")

    exit_code = main(["--file", str(not_a_pdf)])

    assert exit_code == 1
    assert "not a .pdf file" in capsys.readouterr().err


def test_main_accepts_uppercase_pdf_extension(monkeypatch, tmp_path):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.PDF")

    received_kwargs: dict = {}

    def _fake_run(paths_config, conn, **kwargs):
        received_kwargs.update(kwargs)
        return RunSummary(processed=0, skipped=0, errors=0, ungrouped=0)

    monkeypatch.setattr(main_module, "run", _fake_run)

    exit_code = main(["--file", str(pdf_path)])

    assert exit_code == 0
    assert received_kwargs["target_source_path"] == str(pdf_path)


def test_main_rejects_file_outside_input_root_with_exit_code_1(monkeypatch, tmp_path, capsys):
    import src.main as main_module

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)

    def _raise_if_called(*args, **kwargs):
        raise AssertionError(
            "run() should not be called for a --file path outside input_root"
        )

    monkeypatch.setattr(main_module, "run", _raise_if_called)

    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside_pdf = _write_pdf(outside_dir / "lecture_01.pdf")

    exit_code = main(["--file", str(outside_pdf)])

    assert exit_code == 1
    assert "not under paths.input_root" in capsys.readouterr().err


@respx.mock
def test_main_file_and_rerun_llm_combined_reprocesses_only_that_files_llm_stage(
    tmp_path, monkeypatch
):
    """
    Confirms the documented "reprocess just this one lecture's LLM stage
    after tweaking the prompt" workflow (issue #44) end-to-end through
    main(argv), not just run() directly: --rerun-llm on an UNCHANGED
    --file target hits only the LLM API (no Mathpix call), leaving a
    sibling file in the same course completely untouched. No client=
    injection point exists at the main() level, so a fake MathpixClient
    is built internally from monkeypatched credentials -- respx.mock
    intercepts the real httpx call regardless of which MathpixClient
    instance makes it (it's never expected to fire here anyway, since
    this is the UNCHANGED/LLM-only path).
    """
    import src.main as main_module
    from src.config import MathpixCredentials

    paths_config = _make_paths_config(tmp_path)
    monkeypatch.setattr(main_module, "load_paths_config", lambda: paths_config)
    monkeypatch.setattr(
        main_module,
        "load_mathpix_credentials",
        lambda: MathpixCredentials(app_id="test_app_id", app_key="test_app_key"),
    )
    monkeypatch.setattr(main_module, "load_llm_config", _make_llm_config)
    monkeypatch.setattr(main_module, "load_output_config", _make_output_config)
    monkeypatch.setattr(main_module, "load_naming_config", _make_naming_config)

    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    target_pdf = _write_pdf(course_dir / "lecture_01.pdf")
    sibling_pdf = _write_pdf(course_dir / "lecture_02.pdf")

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

    exit_code = main(["--file", str(target_pdf), "--rerun-llm"])

    assert exit_code == 0
    assert not submit_route.called
    assert len(llm_calls) == 1

    sibling_entry_after = get_entry(conn, str(sibling_pdf.resolve()))
    assert sibling_entry_after == sibling_entry_before


@respx.mock
def test_run_wires_real_output_and_naming_config_end_to_end(client, tmp_path, monkeypatch):
    """
    Issue #37: when output_config/naming_config are omitted, run() loads
    them internally via load_output_config()/load_naming_config() -- same
    optional-param fallback pattern as llm_config. This drives that real
    internal-load path (a real config.yaml on disk, not an injected
    OutputConfig/NamingConfig object) end-to-end, confirming the written
    vault note reflects output.course_tags/date_format/
    figures_dark_mode_flag and naming.lecture_prefix rather than the old
    hardcoded defaults.
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        "output:\n"
        "  course_tags:\n"
        "    class_1:\n"
        "      - test-tag\n"
        "  date_format: \"%d/%m/%Y\"\n"
        "  figures_dark_mode_flag: true\n"
        "naming:\n"
        "  lecture_prefix: Lec\n",
        encoding="utf-8",
    )
    # load_output_config()/load_naming_config() (called by run() with no
    # config_path arg, since output_config/naming_config are omitted below)
    # resolve config.yaml relative to cwd -- chdir so they pick up the file
    # just written above.
    monkeypatch.chdir(tmp_path)

    _mock_happy_path()
    _install_fake_cleanup_pdf(
        monkeypatch,
        status="success",
        content="Some notes.\n\n![](figures/lecture_01_fig_001.jpg)\n",
    )

    summary = run(paths_config, conn, client=client, llm_config=_make_llm_config())

    assert summary.errors == 0
    assert summary.processed == 1

    entry = get_entry(conn, str(pdf_path.resolve()))
    assert entry.vault_status == "success"
    vault_path = Path(entry.vault_path)

    # naming.lecture_prefix reflected in the output filename.
    assert vault_path.name == "Lec 01.md"

    written = vault_path.read_text(encoding="utf-8")
    frontmatter_body = written.split("---\n")[1]
    data = yaml.safe_load(frontmatter_body)

    # naming.lecture_prefix also reflected in the frontmatter title.
    assert data["title"] == "Lec 01"
    # output.course_tags's "class_1" entry resolved via resolve_tags().
    assert data["tags"] == ["test-tag"]
    # output.date_format applied to the "date" field.
    stat = pdf_path.stat()
    expected_date = datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y")
    assert data["date"] == expected_date

    # output.figures_dark_mode_flag=true appended "@darkmode" to the
    # figure's rewritten alt text.
    assert "![Figure 1 @darkmode](figures/lecture_01_fig_001.jpg)" in written


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
        lambda paths_config, conn, client=None, course=None, dry_run=False, force=False, force_llm=False, target_source_path=None, force_vault_overwrite=False, no_llm=False: RunSummary(
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


class _RecordingReporter:
    """
    Issue #47 -- a fake Reporter that records every call it receives,
    instead of printing anything. Used to assert on run()'s actual wiring
    (which source_path/stage/message/status values reach the Reporter)
    without depending on any particular rendering of them.
    """

    def __init__(self):
        self.stages: list[tuple[str, str]] = []
        self.details: list[tuple[str, str]] = []
        self.done: list[tuple[str, str]] = []

    def on_stage(self, source_path, stage):
        self.stages.append((source_path, stage))

    def on_detail(self, source_path, message):
        self.details.append((source_path, message))

    def on_done(self, source_path, status):
        self.done.append((source_path, status))


@respx.mock
def test_run_wires_custom_reporter_for_new_file(client, tmp_path, monkeypatch):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "lecture_01.pdf")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    reporter = _RecordingReporter()
    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        reporter=reporter,
    )

    assert summary.processed == 1
    source_path = str(pdf_path.resolve())

    # A custom reporter fully replaces the default PlainReporter -- nothing
    # is ever printed to stdout when one is injected.
    stage_tokens = [stage for path, stage in reporter.stages if path == source_path]
    assert "submitting:new" in stage_tokens
    assert "done:llm_success" in stage_tokens

    # process_pdf()'s on_status hook is now wired to on_detail() -- at
    # least one polling detail should have been recorded for this file.
    assert any(path == source_path for path, _ in reporter.details)


@respx.mock
def test_run_default_reporter_still_prints_to_stdout(client, tmp_path, monkeypatch, capsys):
    """
    Confirms the default (reporter=None) path is unaffected by the
    refactor -- PlainReporter is constructed internally and today's exact
    output still reaches stdout, with no injected reporter.
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    _write_pdf(course_dir / "lecture_01.pdf")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
    )

    assert summary.processed == 1
    out = capsys.readouterr().out
    assert "[class_1] lecture_01.pdf: processing (new)..." in out
    assert "[class_1] lecture_01.pdf: done (LLM cleanup succeeded)" in out


@respx.mock
def test_run_reporter_receives_vault_write_failure(client, tmp_path, monkeypatch):
    """
    _write_to_vault()'s failure path is also wired through reporter --
    exercised here via a source filename that doesn't match
    parse_lecture_filename()'s pattern (mirrors
    test_run_unparseable_filename_records_vault_failure_only's setup).
    """
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    course_dir = paths_config.input_root / "class_1"
    course_dir.mkdir()
    pdf_path = _write_pdf(course_dir / "not_a_lecture_name.pdf")

    _mock_happy_path()
    _install_fake_cleanup_pdf(monkeypatch, status="success")

    reporter = _RecordingReporter()
    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        reporter=reporter,
    )

    assert summary.errors == 1
    source_path = str(pdf_path.resolve())
    messages = [stage for path, stage in reporter.stages if path == source_path]
    assert any("vault write FAILED" in message for message in messages)


@respx.mock
def test_run_ungrouped_skip_wired_through_reporter(client, tmp_path):
    paths_config = _make_paths_config(tmp_path)
    conn = init_db(paths_config.state_db)
    stray_pdf = _write_pdf(paths_config.input_root / "stray.pdf")

    reporter = _RecordingReporter()
    summary = run(
        paths_config,
        conn,
        client=client,
        llm_config=_make_llm_config(),
        output_config=_make_output_config(),
        naming_config=_make_naming_config(),
        reporter=reporter,
    )

    assert summary.ungrouped == 1
    assert (str(stray_pdf.resolve()), "ungrouped_skip") in reporter.stages
