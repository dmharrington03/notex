# NoTeX — Agent Notes

## Project Summary

NoTeX is a Python CLI tool that scans a directory of handwritten lecture note
PDFs, runs them through the Mathpix API for OCR (text, LaTeX, figures), cleans
up the extracted text with an LLM, and writes organized Markdown into an
Obsidian vault. It is run manually by the user, is fully idempotent, and never
modifies its input.

## Critical Invariants

- **`notes_raw/` is a permanent, read-only archive.** The pipeline must never
  write to, move, delete, or otherwise modify any file inside it, under any
  circumstances.
- **`notes_raw/` and `vault/` live outside this repository.** They are sibling
  directories to the repo root, not subdirectories of it. Never assume or
  create `notes_raw/`/`vault/` paths relative to the repo. The actual
  absolute paths for this machine are in `config.yaml` (gitignored, machine-
  specific — see `config.example.yaml` for the template). Read `config.yaml`
  directly from disk when you need the real paths; it's present in the
  working tree even though it isn't tracked in git.
- **The pipeline must be idempotent.** Re-running on unchanged input must be a
  no-op. State tracking (later phases) is the sole mechanism for deciding
  what's already processed.

## Environment

All code — running scripts, tests, and installing packages — happens inside
the `notex` conda environment. Never run Python outside of it.

```
conda activate notex
```

Once activated, `python` resolves to the env's own binary
(`~/miniconda3/envs/notex/bin/python`).

**Package installation must always use `conda install`, never `pip install`.**
This applies to every dependency, no exceptions. Install with:

```
conda activate notex
conda install <package>
```

`conda-forge` and other conda channels are fine when a package isn't on the
default channel (e.g. `conda install -c conda-forge <package>`) — the rule is
"use conda instead of pip," not "defaults channel only." Never fall back to
pip to work around a missing package.

Dependencies are tracked in `environment.yml` (reproduce with
`conda env create -f environment.yml`), not `requirements.txt`.

## Current Phase

**Phase 2 — State management.** Scope: SQLite state log (`state.db`),
two-tier change detection (filesystem mtime+size pre-check, SHA-256 fallback
per docs/spec.md Stage 1 / State Management), and idempotent rerun behavior
across all course subfolders in `notes_raw/`. See `docs/spec.md` for the full
7-phase roadmap and stage-by-stage detail (note: follow this file, AGENTS.md,
not spec.md, where they disagree).

Concretely, this phase covers:
- `src/state.py`: `state.db` schema/init + CRUD, matching docs/spec.md's full
  State Management column list (`mathpix_*`, `llm_*`, `output_path`,
  `vault_written_at`, etc.) created upfront even though only the
  `mathpix_*`/discovery-related columns are populated until Phases 3-5.
- `src/discovery.py`: per-file two-tier classification (new/unchanged/changed,
  including the "previously `mathpix_failed` -> retry" rule from the
  Reprocessing logic table) plus a full recursive walk of `paths.input_root`
  across all course subfolders. Note: docs/spec.md nominally assigns
  "generalize discovery across all courses" to Phase 6, but that work is
  being pulled forward into this phase since it's simple on top of the
  per-file primitive and is needed to prove idempotency for real.
- `src/config.py`: new narrowly-scoped `load_paths_config()`
  (`paths.input_root`, `paths.cache_dir`, `paths.state_db`) ahead of Phase
  6's full `config.yaml` wiring, mirroring the `mathpix:` polling precedent
  from Phase 1 issue #2. `paths.vault_root` (already present in
  `config.yaml`/`config.example.yaml`) stays unread until Phase 4/5.
- `src/main.py`: new, permanent CLI entry point (no flags yet — those are
  Phase 7) that discovers new/changed PDFs, runs them through Phase 1's
  `process_pdf()`, and records results (or failures) to `state.db`,
  continuing past per-file errors rather than aborting the run. Hits the
  real, paid Mathpix API when run for real — same caution as
  `scripts/smoke_test_mathpix.py`.

Still out of scope: LLM cleanup, figure copy-to-vault, frontmatter/vault
writing, course index generation, and CLI flags beyond the bare entry point
— those arrive in Phases 3-7.

Tracked in issues #7-#12 (`phase-2` label).

### Phase 2 progress

**Phase 2 status: VALIDATED — complete.** All Phase 2 issues (#7-#12) are
implemented and unit-tested (sqlite-backed with `tmp_path`/respx mocks, no
real API calls in the automated suite — 75 tests passing), and the full
`src/main.py` orchestration pipeline has additionally been run for real
against the live Mathpix API on `notes_raw/class_1`'s two PDFs — see the
issue #12 entry below for the real-run findings. Idempotent rerun behavior
(the phase's core requirement) was confirmed end-to-end on real data, not
just in mocked tests.

- **Issue #7 (`src/state.py` schema + CRUD) — done.** Implemented in
  `src/state.py`, tested in `tests/test_state.py` (10 sqlite-backed cases
  using `tmp_path`, no mocking, all passing). Conventions
  established/extended here that later Phase 2 issues should follow:
  - Single table, `pdf_state`, primary-keyed on `source_path` (absolute
    path string), with the full column list from docs/spec.md's State
    Management section created upfront: `source_hash`, `source_mtime`
    (`REAL`, raw `os.stat().st_mtime`), `source_size` (`INTEGER`),
    `mathpix_pdf_id`, `mathpix_status`, `llm_model`, `llm_prompt_version`,
    `llm_status`, `llm_validation_result`, `figure_count` (`INTEGER`),
    `output_path`, `mathpix_processed_at`, `llm_processed_at`,
    `vault_written_at`. `llm_*`/`output_path`/`vault_written_at` stay NULL
    until Phases 3-5 populate them.
  - `StateEntry` is a frozen dataclass mirroring the table 1:1 — same
    convention as `MathpixCredentials`/`ProcessResult` in `src/config.py`
    / `src/mathpix.py`.
  - `init_db(path)` takes **no default path** — resolving the pipeline's
    actual default `state.db` location (repo root, overridable) is
    issue #10's (`load_paths_config()`) job, not this module's. `init_db()`
    creates parent directories if missing, runs
    `CREATE TABLE IF NOT EXISTS` (idempotent — safe to call repeatedly
    against the same path, including across process restarts), and
    returns the open `sqlite3.Connection` for the caller (`discovery.py`,
    `main.py`) to reuse.
  - The three timestamp columns (`mathpix_processed_at`, `llm_processed_at`,
    `vault_written_at`) are `datetime` in Python but persisted as ISO 8601
    strings in SQLite, converted explicitly in `state.py` rather than
    relying on sqlite3's built-in datetime adapters (deprecated as of
    Python 3.12) — matches the `datetime.now(timezone.utc)` convention
    already established in `src/mathpix.py`'s `ProcessResult`.
  - `upsert_entry(conn, source_path, **fields)` is a **partial** upsert:
    a single `INSERT ... ON CONFLICT(source_path) DO UPDATE SET` that only
    writes the columns actually passed as kwargs. This means a later,
    unrelated call (e.g. Phase 3's LLM stage writing just `llm_status`/
    `llm_model`) never nulls out earlier columns (e.g. Mathpix-stage
    fields) on the same row — verified directly in
    `test_upsert_entry_partial_update_preserves_other_columns`. Unknown
    kwarg names raise `ValueError` (typo guardrail, no ORM layer to catch
    it otherwise). Commits internally after every call (per-file
    durability if a run crashes partway through) and returns `None` —
    callers call `get_entry()` if they need the row back.
  - **Deliberately no validation of `mathpix_status`/`llm_status` values**
    (e.g. no enforced `{"success", "failed", "pending"}` set) —
    `state.py` stays a thin, schema-agnostic CRUD layer; the *meaning* of
    status strings is owned by the callers that write them (`main.py` for
    `mathpix_status`, later Phase 3 code for `llm_status`).
  - `get_entry(conn, source_path) -> StateEntry | None` returns `None` for
    a missing row; otherwise parses the three timestamp columns back to
    `datetime` via `datetime.fromisoformat()`.

- **Issue #8 (`src/discovery.py` two-tier per-file classification) — done.**
  Implemented in `src/discovery.py`, tested in `tests/test_discovery.py`
  (8 sqlite-backed cases using `tmp_path`, no mocking, all passing — one
  per row of docs/spec.md's Reprocessing logic table plus the failed-retry
  rule). Conventions established/extended here that later Phase 2 issues
  (#9, #11) should follow:
  - `classify_pdf(pdf_path, conn) -> ClassificationResult` is the per-file
    primitive; the recursive, multi-course directory walk that calls it
    per file is `discover_pdfs()` (issue #9, done — see below).
  - `Classification` is a 4-value enum: `NEW`, `UNCHANGED`, `CHANGED`,
    `RETRY`. `RETRY` (stored `mathpix_status == "failed"`, content
    otherwise unchanged) is kept distinct from `CHANGED` (content actually
    differs) rather than collapsed together, so callers can log/handle
    "reprocessing a previously-failed file" differently from "reprocessing
    because content changed" even though both currently mean "queue for
    the Mathpix stage." An actual hash change always wins: if content
    differs, the result is `CHANGED` even when `mathpix_status` was also
    `"failed"` on that row.
  - `ClassificationResult` is a frozen dataclass carrying not just the
    enum but the current `source_path` (resolved absolute path string),
    `source_mtime`, `source_size`, and `source_hash` — so a caller that
    proceeds to reprocess (`main.py`, issue #11) can reuse the
    already-computed hash/metadata when writing the final state.db row
    instead of re-reading/re-hashing the file. `source_hash` is `None`
    only when tier 1 short-circuited without ever computing a hash (a
    plain `UNCHANGED` with no metadata drift).
  - `compute_sha256(path)` lives in `discovery.py`, not `state.py` —
    `state.py` stays a thin, schema-agnostic CRUD layer per its own
    docstring; change-detection logic (hashing included) belongs to
    discovery. Reads the file in fixed 64KB chunks rather than loading it
    into memory at once.
  - Tier 1 (mtime + size vs. `state.db`) short-circuits before any file
    read happens; tier 2 (SHA-256) only runs when tier 1 can't rule out a
    change (no entry, or mtime/size differ) — verified directly in
    `test_unchanged_mtime_and_size_skips_hash_computation` by deliberately
    storing a wrong hash and confirming it's never consulted.
  - `classify_pdf()` persists exactly one thing itself: when tier 2 finds
    the hash unchanged despite drifted mtime/size, it writes the refreshed
    metadata immediately via a partial `upsert_entry(conn, source_path,
    source_mtime=..., source_size=...)` — since a skipped/unchanged file
    has no other pipeline stage that would ever perform this write. It
    does **not** write anything for `NEW`/`CHANGED`/`RETRY` results; that's
    left to the caller (`main.py`, issue #11) once the actual Mathpix
    stage has run, consistent with `state.py`'s "callers own what their
    stage writes" convention.
  - The failed-retry check compares against the literal stored string
    `"failed"` (docs/spec.md's State Management schema:
    `mathpix_status`: `success`, `failed`, `pending`) — not the informal
    `mathpix_failed` phrasing used elsewhere in spec.md's prose.
  - `pdf_path` is resolved via `Path(pdf_path).resolve()` before being
    used as (or looked up as) `source_path`, matching `state.py`'s
    "absolute path string" convention for that column.

- **Issue #9 (`src/discovery.py` recursive directory scan across courses)
  — done.** Implemented in `src/discovery.py`, tested in
  `tests/test_discovery.py` (7 new tmp_path-backed cases, all passing).
  Conventions established/extended here that later Phase 2 issues (#11)
  should follow:
  - `discover_pdfs(input_root, conn) -> dict[str, list[ClassificationResult]]`
    walks `input_root` and classifies every PDF found via `classify_pdf()`
    (issue #8), grouping results by course. A "course" is an immediate
    (non-hidden) subdirectory of `input_root`; each course's PDFs are then
    found via a **recursive** walk within that subdirectory (hidden
    files/dirs skipped at any depth), so nested structure underneath a
    course folder is discovered even though today's real `notes_raw/` data
    is flat (`{course}/{lecture}.pdf}`).
  - **Every** classification is included per course — `NEW`, `CHANGED`,
    `RETRY`, and `UNCHANGED` alike — not just the actionable ones. This is
    deliberate: `main.py` (issue #11) needs full processed/skipped counts
    for its end-of-run summary, and re-deriving a skip count would mean
    re-invoking `classify_pdf()` a second time per file. Callers filter on
    `.classification` themselves to decide what to actually process vs.
    just count.
  - A course subdirectory with zero PDFs still appears as a key mapping to
    `[]`, so course enumeration from `discover_pdfs()`'s return value alone
    is complete — useful later for Phase 6's course index generation.
  - PDFs sitting directly under `input_root`, outside any course
    subdirectory, are still run through `classify_pdf()` (cheap and
    course-agnostic) but are grouped under a reserved sentinel key,
    `UNGROUPED_COURSE_KEY` (`""`, the empty string) — not a real course
    name — deliberately kept as a plain `dict[str, list[ClassificationResult]]`
    entry rather than introducing a wrapper dataclass, so `main.py` can
    special-case that one key (e.g. warn/log about stray files) without a
    type change to the function's return value.
  - Non-`.pdf` files are ignored silently at every level (matching
    `state.py`/`discovery.py`'s existing "no logging infrastructure exists
    yet in this phase" precedent — nothing in the codebase does
    print/logging output before `main.py`, issue #11).
  - Hidden directories/files (names starting with `.`) are ignored both as
    course candidates and as PDFs at any depth — verified directly in
    `test_discover_pdfs_ignores_hidden_dirs_and_files` (guards against
    e.g. `.DS_Store`-adjacent hidden dirs or stray dotfiles being picked
    up as real courses/PDFs).
  - Ordering is fully deterministic: course keys are sorted alphabetically
    (`UNGROUPED_COURSE_KEY` sorts first, being the empty string), and each
    course's `ClassificationResult` list is sorted by `source_path` —
    needed for reproducible runs/tests per the issue's own requirement.

- **Issue #10 (`src/config.py` `load_paths_config()`) — done.** Implemented
  in `src/config.py`, tested in `tests/test_config.py` (5 new tmp_path-backed
  cases, all passing). Conventions established/extended here that later
  Phase 2 issues (#11) should follow:
  - `load_paths_config(config_path=None) -> PathsConfig` reads the
    `paths:` section from `config.yaml`, mirroring
    `load_mathpix_polling_config()`'s `config_path=` optional-arg/
    `DEFAULT_CONFIG_PATH` fallback pattern. `PathsConfig` is a frozen
    dataclass with `input_root`, `cache_dir`, `state_db` — all `Path`
    objects (not raw `str`), matching how these values get consumed
    downstream (`Path(input_root)` in `discovery.py`, `path` arg in
    `state.py`'s `init_db()`).
  - `paths.input_root` is **required, with no default** —
    `load_paths_config()` raises `ConfigError` if `config.yaml` doesn't
    exist at all, if the `paths:` section is missing, or if
    `input_root` itself is missing/blank. This is a deliberate asymmetry
    with `load_mathpix_polling_config()`, which silently falls back to
    hardcoded defaults on a missing file/section — `input_root` has no
    sensible default the way `poll_interval_seconds`/`max_poll_attempts`
    do, so a missing config file can't be treated as "use the defaults"
    here.
  - `paths.cache_dir`/`paths.state_db` are optional and fall back
    independently to new `DEFAULT_CACHE_DIR` (`_cache`)/`DEFAULT_STATE_DB`
    (`state.db`) module constants — both repo-root-relative, matching the
    existing `DEFAULT_CONFIG_PATH` convention of running the CLI from the
    repo root — when the file/section exists but those specific keys
    don't.
  - `paths.vault_root` (already present in `config.yaml`/
    `config.example.yaml`) is deliberately **not read** by this function —
    stays unused until Phase 4/5.
  - No `.resolve()` or existence-checking of `input_root` against the
    filesystem happens here — `load_paths_config()` stays a thin,
    schema-level config loader; validating what's actually on disk is
    `discovery.py`/`main.py`'s job, not `config.py`'s.
  - `config.yaml`/`config.example.yaml` were **not** modified — the
    defaults (`_cache`, `state.db` at the repo root) already match
    current behavior, so no explicit `cache_dir`/`state_db` keys were
    added for values nobody's overriding yet.

- **Issue #11 (`src/main.py` orchestration entry point) — done.**
  Implemented in `src/main.py`, tested in `tests/test_main.py` (7 new
  respx-mocked + tmp_path-`state.db` cases, all passing — no mocking of
  `state.db` itself, matching `test_discovery.py`'s convention).
  Conventions established here:
  - Two entry points, not one: `run(paths_config, conn, client=None) ->
    RunSummary` is the directly-testable core (takes an already-loaded
    `PathsConfig` and an already-open `state.db` connection, so tests
    inject a `tmp_path` input_root tree + real temp `state.db` + a
    respx-mocked `MathpixClient` without ever going through argparse or
    `config.yaml`); `main(argv=None) -> int` is the thin CLI wrapper
    (`load_paths_config()` -> `init_db()` -> `run()` -> print summary ->
    exit code). `tests/test_main.py` calls `run()` directly for all the
    substantive cases and only exercises `main()`'s own wiring (with
    `run`/`load_paths_config` monkeypatched) for the exit-code contract.
  - **One `MathpixClient` per run, not per file.** `run()` builds (or
    reuses an injected) client once and passes the same instance to every
    `process_pdf()` call in the loop — a deliberate departure from
    `process_pdf()`'s own default of owning/closing a client per call —
    so a real run shares one HTTP connection across an entire course
    instead of reconnecting per PDF. Still closed in a `finally` (only
    when `run()` itself constructed it) matching `process_pdf()`'s
    `owns_client` pattern.
  - `RunSummary` (frozen dataclass: `processed`, `skipped`, `errors`,
    `ungrouped`) is `run()`'s return value; `_ungrouped` is deliberately
    its own bucket rather than folded into `skipped` or `errors`, so a
    stray top-level PDF under `input_root` doesn't misleadingly read as
    "already processed" or "failed."
  - **`UNGROUPED_COURSE_KEY` files are skipped outright this phase, not
    processed.** There's no course name to mirror into `cache_dir`, so
    each one is printed as a warning and left out of `state.db` entirely
    (no `upsert_entry()` call at all for these) — confirmed with the user
    rather than assumed, since `discover_pdfs()`'s docstring left this
    caller-side decision open. Revisit if/when Phase 6 needs a real
    answer for stray files.
  - `cache_dir` per actionable file is `paths_config.cache_dir / course`
    (e.g. `_cache/class_1/lecture_01.mathpix.md`), passed straight through
    as `process_pdf()`'s `cache_dir` positional arg — the course-subfolder
    mirroring called out in the issue body.
  - The hash/mtime/size written back to `state.db` after processing (both
    success and failure paths) are the ones already computed by
    `classify_pdf()`/`discover_pdfs()` on the `ClassificationResult` —
    `run()` never re-reads or re-hashes the source PDF itself.
  - **Failure handling only catches `MathpixError` /
    `httpx.HTTPStatusError` / `FileNotFoundError`** (the documented
    `process_pdf()` failure modes) — anything else propagates and aborts
    the run, rather than being silently swallowed. On a caught failure,
    `upsert_entry()` still writes the refreshed `source_hash`/
    `source_mtime`/`source_size` alongside `mathpix_status="failed"`, so
    tier-1 change detection stays correct on the next run even for a
    failed file; `mathpix_pdf_id`/`figure_count`/`mathpix_processed_at`
    are left untouched (there is no successful `ProcessResult` to read
    them from).
  - Progress output is one line per file (`[{course}] {filename}:
    processing ({classification})...` / `... done` / `... FAILED: {exc}`)
    plus one warning line per ungrouped file — no per-poll `on_status`
    spam, since a real course run could mean dozens of poll attempts per
    file. A basic end-of-run summary (`Processed`/`Skipped`/`Errors`/
    `Ungrouped` counts) is printed by `main()`, not `run()` itself, so
    `run()`'s return value stays the single source of truth for tests.
  - **`main()` always returns `0` once `run()` completes**, even with
    `RunSummary.errors > 0` — per-file Mathpix failures are recorded in
    `state.db` and visible in the printed summary, not treated as a fatal
    run failure (confirmed with the user). `main()` only returns `1` for
    something that prevents the run from starting at all — currently just
    `ConfigError` from `load_paths_config()` (e.g. missing `config.yaml`/
    `input_root`).
  - No CLI flags (`--dry-run`/`--force`/`--course`/`--verbose`) — still
    Phase 7, per the issue and `docs/spec.md`'s roadmap.

- **Issue #12 (real-data idempotent-rerun validation) — done.** `src/main.py`
  was run for real (`python -m src.main`, no CLI flags) against
  `config.yaml`'s actual `input_root`, covering `notes_raw/class_1`'s two
  existing PDFs. Findings:
  - **Run 1 (cold, hit the real Mathpix API):** both files classified `new`
    and processed successfully — console summary `Processed: 2, Skipped: 0,
    Errors: 0, Ungrouped: 0`. `state.db` got one row per file with
    `mathpix_status="success"`, a real `mathpix_pdf_id` (UUID), correct
    `figure_count` (0 for `lecture_01.pdf`, 1 for `lecture_02.pdf`), a
    64-character SHA-256 `source_hash`, and `source_mtime`/`source_size`
    matching the files' real `os.stat()` values exactly.
    `_cache/class_1/lecture_01.mathpix.md`, `lecture_02.mathpix.md`, and
    `figures/lecture_02_fig_001.jpg` were created, matching Phase 1's
    documented `fetch_and_extract()` output shape (course-subfolder-nested
    this time, per issue #11's `cache_dir` convention) — and are
    byte-for-byte identical to the earlier `scripts/smoke_test_mathpix.py`
    output from the same two PDFs, confirming deterministic extraction
    across independent submissions (different `pdf_id`s, same content).
    `notes_raw/class_1/` itself was left untouched (same permissions,
    mtime, and size on both PDFs before/after).
  - **Run 2 (immediate rerun, no Mathpix cost):** both files classified
    `unchanged` — console summary `Processed: 0, Skipped: 2, Errors: 0,
    Ungrouped: 0`, with no `processing (...)`/`done` lines printed for
    either file. Confirmed a true no-op at the data level, not just in the
    printed summary: every `state.db` column (`source_hash`,
    `mathpix_pdf_id`, `mathpix_status`, and critically
    `mathpix_processed_at`) was byte-identical to Run 1 — no row was
    rewritten — and the `_cache/class_1/` files' and `state.db`'s own
    mtimes stayed at Run 1's wall-clock time, i.e. nothing was touched
    during Run 2. This is the phase's core requirement (idempotent rerun)
    confirmed end-to-end on real data.
  - Tier 1 (mtime+size pre-check) alone was sufficient to short-circuit
    Run 2 for both files, since neither file's mtime/size had drifted —
    consistent with `test_unchanged_mtime_and_size_skips_hash_computation`'s
    mocked-test coverage of the same path.
  - **Optional tier-2 (SHA-256 fallback) exercise — also run and confirmed.**
    `lecture_01.pdf` was `touch`-ed (mtime bumped, bytes unchanged) and
    `main.py` rerun a third time. Result: still classified `unchanged` and
    skipped (no reprocessing, no Mathpix call) — tier 1 alone couldn't rule
    it out (mtime had drifted), so tier 2 computed the SHA-256, found it
    identical to the stored hash, and correctly treated the file as
    unchanged. `state.db`'s `source_mtime` for `lecture_01.pdf` was refreshed
    to the new value (per `classify_pdf()`'s documented partial-upsert
    behavior for drifted-but-unchanged files), while `source_hash`,
    `mathpix_status`, `mathpix_pdf_id`, and `mathpix_processed_at` all stayed
    exactly as written in Run 1 — confirming the refresh is metadata-only
    and doesn't touch the Mathpix-stage fields. `_cache/class_1/*.mathpix.md`
    file mtimes were untouched, confirming `process_pdf()` was never
    invoked. `lecture_02.pdf` (not touched) was unaffected. This is real,
    non-mocked confirmation of the tier-2 fallback path called out in
    `test_discovery.py`'s mocked coverage.

### Phase 1 progress

**Phase 1 status: VALIDATED — complete.** All core-pipeline issues (#1-#6)
are implemented and unit-tested (respx-mocked, no real API calls), and the
pipeline has additionally been run for real against the live Mathpix API via
`scripts/smoke_test_mathpix.py` on two real handwritten lecture PDFs
(`notes_raw/class_1/lecture_01.pdf`, no figures; `lecture_02.pdf`, one
figure) — see "Smoke test findings" below. Both the zero-figure and
one-figure code paths were confirmed working end-to-end, submit → poll →
fetch/extract → cache-write. Per "Git / Issue Tracking" below, this
validation is what unblocks pushing to `origin`.

- **Issue #1 (`MathpixClient.submit()`) — done.** Implemented in
  `src/mathpix.py` / `src/config.py`, tested in `tests/test_mathpix.py`
  (6 respx-mocked cases, all passing). Conventions established here that
  later Phase 1 issues should follow:
  - `MathpixClient` wraps a **synchronous** `httpx.Client` (not async) —
    matches `respx`'s sync mocking. It's a context manager
    (`with MathpixClient(app_id, app_key) as client:`) and accepts an
    optional `http_client=` param so tests can inject one wired to a
    respx-mocked transport; otherwise it creates and owns (and closes) its
    own `httpx.Client`.
  - `MathpixError` (in `src/mathpix.py`) is the base exception for
    Mathpix-specific failures (currently: a 2xx response body containing an
    `error` field, or a 2xx body missing `pdf_id`). Non-2xx HTTP responses
    raise `httpx.HTTPStatusError` via `response.raise_for_status()`.
  - `submit()`'s Phase 1 options are intentionally minimal:
    `DEFAULT_SUBMIT_OPTIONS = {"conversion_formats": {"md.zip": True}}`.
    `include_page_breaks`, `rm_spaces`, math delimiter options, etc. are
    deliberately deferred — delimiter options only affect the `text` format
    anyway, not `md`/`mmd` (see "Mathpix API notes" below).
  - `fetch_and_extract(pdf_id, dest_dir)` is still stubbed on `MathpixClient`
    as `raise NotImplementedError(...)`, pointing at issue #3. Module-level
    `process_pdf()` is stubbed the same way, pointing at issue #4. Fill these
    in rather than changing the established method signatures/shape unless a
    real blocker comes up.
  - Credentials: `src/config.py` → `load_mathpix_credentials()` reads
    `MATHPIX_APP_ID`/`MATHPIX_APP_KEY` from `.env` (via `python-dotenv`),
    returns a frozen `MathpixCredentials` dataclass, raises `ConfigError` if
    either is missing/blank.
  - `.env` exists locally now (gitignored, real path present in working
    tree per usual). Real Mathpix credentials go there when available, but
    are not required to implement/test issue #2 — all unit tests mock HTTP
    and never touch the real API or a real key.

- **Issue #2 (`MathpixClient.poll_until_complete()`) — done.** Implemented
  in `src/mathpix.py` / `src/config.py`, tested in `tests/test_mathpix.py`
  and `tests/test_config.py` (7 new respx-mocked poll cases + 4 config
  cases, all passing). Conventions established/extended here:
  - Two dedicated exception types, both subclassing `MathpixError`:
    `MathpixProcessingError` (poll `status == "error"`) and
    `MathpixTimeoutError` (`max_poll_attempts` exhausted without reaching a
    terminal status) — distinguishable from each other per the issue's
    requirement, even though both currently map to `mathpix_failed` in the
    future state log.
  - `sleep_fn` is a **constructor** param on `MathpixClient` (defaults to
    `time.sleep`), matching the existing `http_client=` injection pattern —
    not a per-call param on `poll_until_complete()`. Tests inject a
    list-`.append`-based recorder so nothing actually sleeps.
  - HTTP `429` responses during polling are retried honoring `Retry-After`
    (parsed as a plain float/int of seconds — Mathpix doesn't use HTTP-date
    `Retry-After` values) and **do not** count against `max_poll_attempts`;
    only real status polls (received/loaded/split) consume an attempt.
  - `percent_done` / `num_pages_completed`: **stay internal for Phase 1** —
    `poll_until_complete()` just returns the full JSON payload dict on
    `status == "completed"`; no logging/printing infra exists yet in this
    phase.
  - `config.yaml` now has a `mathpix:` section (`poll_interval_seconds: 5`,
    `max_poll_attempts: 60`) — added to both `config.yaml` (real,
    gitignored) and `config.example.yaml` (template, tracked). This is
    ahead of the Phase 6 full config.yaml wiring, scoped narrowly to just
    these two polling keys for issue #2.
  - `src/config.py` → new `load_mathpix_polling_config(config_path=None)`
    reads `mathpix.poll_interval_seconds` / `mathpix.max_poll_attempts` from
    `config.yaml` (default path: `config.yaml` in the cwd, matching the
    `.env` convention of running the CLI from the repo root), returning a
    frozen `MathpixPollingConfig` dataclass. Missing file / missing section
    / missing individual keys all fall back to the hardcoded
    `DEFAULT_POLL_INTERVAL_SECONDS` (5) / `DEFAULT_MAX_POLL_ATTEMPTS` (60)
    module constants — config.yaml's mathpix: section is optional, not
    required.
  - `poll_until_complete(pdf_id, poll_interval_seconds=None,
    max_poll_attempts=None)` only calls `load_mathpix_polling_config()` when
    either arg is omitted (`None`) — tests always pass both explicitly so
    they never touch the filesystem/depend on cwd.
  - `pyyaml` added as a new conda dependency (`environment.yml`) to parse
    `config.yaml`; installed via `conda install -n notex pyyaml` per
    AGENTS.md's "conda, never pip" rule.

- **Issue #3 (`MathpixClient.fetch_and_extract()`) — done.** Implemented
  in `src/mathpix.py`, tested in `tests/test_mathpix.py` (16 new
  respx-mocked cases, all passing). Conventions established/extended
  here:
  - **New API behavior discovered, not in the original Mathpix API notes
    below:** conversion formats requested via `conversion_formats` (e.g.
    `md.zip`) have their own `conversion_status` that lags behind the
    main PDF `status` field — `GET /v3/pdf/{pdf_id}.md.zip` isn't
    guaranteed ready just because `poll_until_complete()` returned
    `completed`. `fetch_and_extract()` therefore polls
    `GET /v3/converter/{pdf_id}` first, via a new private
    `_wait_for_conversion_ready()` helper, until
    `conversion_status["md.zip"]["status"] == "completed"` — reusing the
    same `sleep_fn`/429-retry-honoring-`Retry-After` pattern as
    `poll_until_complete()`, and raising the same
    `MathpixProcessingError`/`MathpixTimeoutError` types on
    `"error"`/timeout.
  - **Signature change from the original stub:** `fetch_and_extract()` now
    takes an explicit `lecture_stem: str` third parameter (in addition to
    `pdf_id` and `dest_dir`), since the figure/markdown naming convention
    needs the lecture's filename stem and there was no other way to
    derive it from `pdf_id`/`dest_dir` alone. `process_pdf()` (issue #4)
    is expected to compute `Path(pdf_path).stem` and pass it through.
  - Returns a new frozen `FetchResult` dataclass (`markdown_path`,
    `figures_dir: Path | None`, `figure_count`) rather than the
    still-to-be-designed `ProcessResult` (issue #4) — issue #4 can wrap
    or reuse this shape once it's defined.
  - Cache layout inside `dest_dir`: `{lecture_stem}.mathpix.md` plus a
    `figures/` subdirectory (only created if there's at least one
    figure) containing `{lecture_stem}_fig_{NNN}.{ext}` files —
    zero-padded to 3 digits, 1-indexed, matching the vault's `figures/`
    convention (see Stage 4 in `docs/spec.md`, and the `.jpg` figure
    format decision in "Mathpix API notes" below). `ext` is taken from
    whatever extension the actual zip member has rather than hardcoding
    `.jpg`.
  - Image references are parsed from the Markdown via a simple
    `![alt](path)` regex, in order of first appearance; a path
    referenced more than once is deduped to a single figure number
    (assigned at first occurrence) and all its occurrences are rewritten
    together. Alt text is left untouched. A referenced image path that
    can't be resolved to a real member inside the zip raises
    `MathpixError` (surfaces bundle-shape mismatches loudly rather than
    silently dropping the figure).
  - The `.md` file inside the bundle is located by globbing for any
    `*.md` member rather than assuming a fixed path/filename, since the
    exact internal zip layout is unconfirmed until the issue #6 smoke
    test; image paths are then resolved relative to that `.md` member's
    directory within the archive.
  - No temp directory/temp file is used for extraction — the downloaded
    `.md.zip` response body is read directly into
    `zipfile.ZipFile(io.BytesIO(response.content))` and only the final
    renamed files are written to `dest_dir`.
  - Zero-figure case: no `figures/` directory is created at all;
    `FetchResult.figures_dir` is `None` and `figure_count` is `0`.
  - `dest_dir` (and `figures/` within it) is created via
    `mkdir(parents=True, exist_ok=True)`; re-running overwrites files by
    their same deterministic names rather than clearing the directory
    first.
  - `tests/fixtures/sample_result.md.zip` (issue #5, folded into this
    issue since #3's tests depend on it) is a hand-built zip: one
    `sample_result.md` with 3 image references (two distinct paths, one
    of them — `images/abc123.jpg` — referenced twice, to exercise dedup)
    plus two tiny placeholder (non-decodable, just distinct byte
    strings) `.jpg` files under `images/`.

- **Issue #4 (`process_pdf()` / `ProcessResult`) — done.** Implemented in
  `src/mathpix.py`, tested in `tests/test_mathpix.py` (8 new respx-mocked
  cases, all passing). Conventions established/extended here:
  - `ProcessResult` is a new frozen dataclass (`pdf_path`, `pdf_id`,
    `markdown_path`, `figures_dir: Path | None`, `figure_count`,
    `processed_at: datetime`, UTC) living inline in `src/mathpix.py` next
    to `FetchResult` — no separate `src/models.py` yet. Field names
    anticipate Phase 2's `state.db` columns per `docs/spec.md`
    (`source_path`, `mathpix_pdf_id`, `figure_count`,
    `mathpix_processed_at`).
  - **Deliberately no `mathpix_status` field.** Every other method in this
    module signals failure by raising rather than returning a status
    string (`MathpixError`/`MathpixProcessingError`/`MathpixTimeoutError`/
    `httpx.HTTPStatusError`/`FileNotFoundError`); `process_pdf()` stays
    consistent — it only ever returns a `ProcessResult` on success, and
    Phase 2's state-log writer is expected to catch its exceptions to
    decide `mathpix_status` itself.
  - `process_pdf(pdf_path, cache_dir, client=None,
    poll_interval_seconds=None, max_poll_attempts=None)`: orchestrates
    `submit()` -> `poll_until_complete()` -> `fetch_and_extract()`,
    deriving `lecture_stem` as `Path(pdf_path).stem` and passing
    `cache_dir` straight through as `fetch_and_extract()`'s `dest_dir`
    (no per-course subdirectory logic — that's discovery.py's concern in
    a later phase; Phase 1 scope is a single PDF path). Polling params are
    forwarded untouched to both downstream calls rather than resolved
    once up-front, so each independently falls back to its own
    `config.yaml` default when omitted.
  - `client: MathpixClient | None = None` mirrors the existing
    `http_client=`/`sleep_fn=` constructor-injection pattern:
    when omitted, `process_pdf()` builds its own client via
    `load_mathpix_credentials()` and owns/closes it in a `finally` block;
    when injected (as tests do, alongside a no-op recording `sleep_fn`),
    it's used as-is and left open for the caller to manage.

- **Issue #6 (`scripts/smoke_test_mathpix.py`) — done.** Implemented in
  `scripts/smoke_test_mathpix.py`, plus a new `on_status` observability
  hook added to `src/mathpix.py` (2 new respx-mocked cases in
  `tests/test_mathpix.py` covering the hook itself; the script is
  intentionally *not* under `pytest` — see Testing Conventions). Run for
  real against a live Mathpix key on a real one-page, no-figures
  handwritten lecture PDF (`notes_raw/class_1/lecture_01.pdf`) — see
  "Smoke test findings" below for OCR/formatting observations, including
  the now-confirmed math delimiter answer.
  - `poll_until_complete()`, `_wait_for_conversion_ready()`,
    `fetch_and_extract()`, and `process_pdf()` all gained a new optional,
    keyword-only `on_status` param (`OnStatusCallback`, a module-level type
    alias in `src/mathpix.py`), defaulting to `None` — purely an
    observability hook, never affects control flow, and fully backward
    compatible with all existing (issue #1-#4) call sites/tests.
  - Callback shape: `on_status(stage, attempt, max_poll_attempts, status,
    payload)`, invoked once per **real** status poll (HTTP 429 retries are
    not reported — they aren't a "real" poll and don't count against
    `max_poll_attempts` either, per issue #2). `stage` is `"pdf"` for
    `poll_until_complete()`'s main status poll, or
    `"conversion:{conversion_format}"` (e.g. `"conversion:md.zip"`) for
    `_wait_for_conversion_ready()`. Called on every poll including the
    final terminal one — both on `"completed"` (before returning) and on
    `"error"` (before raising `MathpixProcessingError`) — so callers get a
    complete transition log.
  - `process_pdf()` forwards the same `on_status` value to both
    `poll_until_complete()` and `fetch_and_extract()` untouched (same
    pattern as `poll_interval_seconds`/`max_poll_attempts` forwarding).
  - The script itself is a thin `argparse` CLI: positional `pdf_path` +
    `--out` (default `_cache/smoke_test/`, flat — no per-run
    subdirectory), calling `process_pdf()` directly (no duplicated
    submit/poll/fetch orchestration) with `on_status` wired to a print
    function (`[stage poll N/M] status=...`). No `--poll-interval`/
    `--max-poll-attempts`/`--verbose` flags, no raw JSON payload dumps to
    disk — deliberately out of scope per the issue discussion. On any
    failure (`ConfigError`/`MathpixError`/`httpx.HTTPStatusError`/
    `FileNotFoundError`), prints `ERROR: {exc}` to stderr and exits 1
    rather than a traceback.
  - The script inserts the repo root onto `sys.path` at the top (since
    `sys.path[0]` for a directly-executed script is `scripts/`, not the
    repo root) so `from src.mathpix import ...` resolves without needing
    the project installed as a package or invoked via `python -m`.

## Mathpix API notes

These correct assumptions in the original planning spec, verified against
current docs.mathpix.com. Follow these, not the original spec's Mathpix
section, when implementing `src/mathpix.py`:

- **Status values:** `received → loaded → split → completed` (or `error`).
  Not `loading → processing → completed`.
- **Upload:** multipart form-data with a `file` field plus an `options_json`
  field (a JSON-encoded string of options) — not a raw PDF binary body.
- **Figures:** request `conversion_formats: {"md.zip": true}` and extract the
  bundle locally. Do not rely on `GET /v3/pdf/{pdf_id}` returning a figure
  asset list (it only returns status/progress fields), and do not rely on
  `cdn.mathpix.com` image URLs staying valid long-term (30-day retention).
- **Figure format:** images inside the zip are `.jpg`. Decision: keep as
  `.jpg` in the vault, no PNG conversion.
- **Math delimiters — CONFIRMED (issue #6 smoke test):** the `md.zip`
  bundle's Markdown uses `$...$` for inline math and a `$$` fence on its
  own line before/after the expression for display math (i.e.:
  ```
  $$
  <latex>
  $$
  ```
  ), never `\(...\)`/`\[...\]`. This already matches the target vault
  convention in `docs/spec.md`/AGENTS.md, so Phase 5's "delimiter pass" can
  be a pure validation/lint step (checking balance) rather than needing to
  convert between delimiter styles.

## Smoke test findings

Observations from running `scripts/smoke_test_mathpix.py` for real against
sample lecture PDFs. These inform the Phase 3 LLM cleanup prompt and Phase 5
delimiter/validation pass.

### `notes_raw/class_1/lecture_01.pdf` (one page, cursive handwritten physics
notes, no figures) — see `_cache/smoke_test/lecture_01.mathpix.md`

- **Math delimiters confirmed** as `$...$` / `$$ ... $$` — see above.
- **Stray heading artifact:** Mathpix emitted a Markdown heading
  (`## Lecture 21-4/14`) from what was just the student's handwritten
  date/title line at the top of the page, not a semantically-structured
  section header. The LLM cleanup pass (Phase 3) and/or frontmatter
  injection (Phase 5) will need to handle this — e.g. deciding whether to
  keep, strip, or repurpose Mathpix-generated headings rather than
  assuming they reflect real document structure.
- **Word-level OCR errors on cursive handwriting**, roughly one every few
  lines even though overall structure/LaTeX came through readably —
  observed examples: "potentral" → potential, "betore" → before, "initrally
  mstate i" → initially in state i, "encegy" → energy, "regnore" → ignore,
  "Persubation" → Perturbation, "turned an at" → turned on. Confirms Phase
  3's LLM cleanup pass is doing real, necessary work here, not just
  formatting touch-up.
- **Occasional malformed/mismatched LaTeX in complex nested expressions:**
  a few equations have `\left`/`\right` pairs that don't actually balance
  once nested inside other constructs, e.g.
  `\overrightarrow{\left.V_{n i}\right|^{2}}` (a lone `\left.`/`\right|`
  pair inside `\overrightarrow{}`) and an unusual
  `\left\lvert\, ... \right.` construction. Top-level `$`/`$$` delimiters
  were always balanced in this sample, but internal `\left`/`\right`
  balance was not guaranteed — see the consolidated recommendation below.
- **Zero-figure path confirmed working end-to-end against the real API:**
  only `lecture_01.mathpix.md` was written to `_cache/smoke_test/`, no
  `figures/` directory created, matching the `fetch_and_extract()`
  zero-figure behavior established in issue #3.

### `notes_raw/class_1/lecture_02.pdf` (one page, cursive handwritten physics
notes, one hand-drawn figure) — see `_cache/smoke_test/lecture_02.mathpix.md`
/ `_cache/smoke_test/figures/lecture_02_fig_001.jpg`

- **Figure path confirmed working end-to-end against the real API:** the
  one hand-drawn diagram on the page (a double-well potential sketch with
  labeled `|S⟩`/`|A⟩` wavefunctions) was correctly detected, cropped
  cleanly (no surrounding handwritten text bled into the crop), downloaded
  as a 383×251 baseline JPEG (~10 KB), renamed to
  `lecture_02_fig_001.jpg` under `figures/`, and the Markdown reference
  rewritten to `![](figures/lecture_02_fig_001.jpg)` — matching the
  `fetch_and_extract()` figure-handling behavior established in issue #3.
  Mathpix supplied no alt text for the figure (empty `![]()`); Phase 4/5
  may want to decide whether to inject a placeholder caption (e.g.
  "Figure 1") when rewriting these as Obsidian wikilinks, since empty alt
  text isn't very useful on its own.
- **Systematic domain-vocabulary misreads, not just random noise:** the
  word "parity" was misread as "party" *consistently*, every single time
  it appeared (9 occurrences: "party ergenstate", "party odd", "party
  even", "not party ergensinte", "opposite party", "porty", etc.) — i.e.
  Mathpix's language model is substituting a common English word for an
  unfamiliar physics term rather than making independent per-occurrence
  errors. Other domain-term misreads in the same sample: "ergenstate"
  /"ergensinte"/"e.jerstate" → eigenstate, "degensate"/"degreesate" →
  degenerate, "Ingencoal" → In general, "Selecton mles" → Selection rules,
  "Transtion" → Transition. **Implication for Phase 3's LLM prompt:**
  since these are systematic/predictable substitutions rather than random
  noise, it may be worth seeding the cleanup prompt with a short course-
  specific glossary/context hint (e.g. "this is a quantum mechanics
  course; common OCR misreadings include party→parity, ergenstate
  →eigenstate") rather than relying on the LLM to infer domain vocabulary
  from context alone every time.
- **A quietly dangerous misread inside otherwise-valid LaTeX:** handwritten
  ket notation `|n⟩` was OCR'd as the literal LaTeX command `\ln` (natural
  log) in `g.s. $\ln |x\rangle=|1,00\rangle$` — this produces
  *syntactically valid* LaTeX that renders fine, so no delimiter-balance
  or length-ratio check would ever catch it; only a content-aware pass
  (the LLM cleanup step, ideally with the course context above) has any
  chance of catching this class of error.
- **A near-total OCR failure on one dense equation**, garbling a
  hydrogen-atom wavefunction expression badly enough that it's not
  reasonably recoverable without the source image: `R_{1,0}(r)` →
  `R_{1.0}(6)`, `Y_0^0(\theta,\phi)` → `y_{0}^{0}(\theta, 4)`, and
  `e^{-r/a_0}` → `e^{-2 \%}` — note the last one emits a literal, unescaped
  `%` inside math mode, which is the real LaTeX comment character and could
  break naive line-based LaTeX tooling even though Obsidian's renderer
  doesn't treat it specially. Confirms Mathpix OCR quality on cursive
  handwriting can occasionally fail outright on complex nested notation,
  not just introduce minor typos — Phase 3's validation step should be
  prepared for "this equation is unrecoverable" as an outcome, not just
  "this equation has small errors."
- **A real (not just same-type) `\left`/`\right` type mismatch:** confirms
  and sharpens the lecture_01 finding — `\left(\beta\left|\varepsilon_{\beta}
  (-x) \varepsilon_{\alpha}\right| \alpha\right\rangle` opens with `\left(`
  but closes with `\right\rangle` (a paren opened, an angle bracket
  closed). **Consolidated recommendation for Phase 3/5's validation step:**
  a `\left`/`\right` balance check needs to verify not just that counts of
  `\left` and `\right` match, but that each `\left`/`\right` pair's
  delimiter *types* correspond to a valid pairing (or fall back to the LLM
  pass to repair mismatches like this) — a naive count-only check would
  pass both of the malformed examples found so far (lecture_01's and this
  one).

### Phase 3 prompt design notes

- **Dirac/bra-ket notation formatting is inconsistent and should be
  normalized by the LLM cleanup pass.** `lecture_02.mathpix.md` renders
  every ket/bra/inner-product by hand with raw `\langle`/`\rangle`/`|`
  (e.g. `|n\rangle`, `\langle\vec{x} \mid 100\rangle`,
  `\left\langle\psi_{f}\right|`) rather than using the semantic
  `\ket{}`/`\bra{}`/`\braket{}` macros — plausibly part of why the
  `\left`/`\right` mismatches above happen in the first place (hand-built
  angle-bracket delimiters are easy for Mathpix to mis-nest; a single
  macro call isn't). The Phase 3 system prompt should instruct the LLM to
  rewrite bra-ket expressions into `\ket{}`, `\bra{}`, and `\braket{}`
  form for consistency (e.g. `|n\rangle` → `\ket{n}`,
  `\langle\vec{x} \mid 100\rangle` → `\braket{\vec{x}}{100}`), rather than
  leaving Mathpix's raw angle-bracket output as-is. Note this requires the
  vault's Obsidian math renderer (or a MathJax/KaTeX macro config) to
  actually define `\ket`/`\bra`/`\braket`, which aren't standard LaTeX
  primitives — worth confirming as part of Phase 3/5 setup.

## Testing Conventions

- **Automated unit tests** (`tests/`) mock all HTTP calls via `respx`. They
  must never hit the real Mathpix API — no network, no cost, safe for CI.
- **Manual smoke tests** (`scripts/`) hit the real Mathpix API against real
  PDFs and cost money per run. They are not part of the `pytest` suite and
  are run manually when validating actual output quality (OCR correctness on
  handwriting can't be asserted automatically).

## Directory Structure

```
notex/                      ← repo root
├── .env                    ← secrets (gitignored), see .env.example
├── environment.yml         ← conda env spec (reproduce with `conda env create -f environment.yml`)
├── docs/
│   └── spec.md             ← original full spec (historical reference, see note at top of file)
├── src/
│   ├── config.py           ← env/config loading (load_mathpix_credentials, load_mathpix_polling_config, load_paths_config)
│   ├── mathpix.py          ← Mathpix API client
│   ├── state.py            ← state.db schema + CRUD (StateEntry, init_db, get_entry, upsert_entry)
│   ├── discovery.py        ← per-file two-tier change classification + recursive multi-course walk (Classification, ClassificationResult, classify_pdf, compute_sha256, discover_pdfs, UNGROUPED_COURSE_KEY)
│   └── main.py             ← CLI orchestration entry point (RunSummary, run, main) — wires discovery + state.db + process_pdf() into a runnable pass over input_root
├── scripts/
│   └── smoke_test_mathpix.py   ← manual, real-API validation
├── tests/
│   ├── fixtures/           ← fixture data for mocked tests
│   ├── test_mathpix.py
│   ├── test_state.py
│   ├── test_discovery.py
│   ├── test_config.py
│   └── test_main.py
├── state.db                ← SQLite state log, gitignored, created at runtime
└── _cache/                 ← gitignored, created at runtime
```

## Git / Issue Tracking

Issues are tracked on GitHub from the start of the project. Code was
developed locally and held back from the `origin` remote until Phase 1 was
validated against real Mathpix output (see "Phase 1 status" above). Phase 1
is now validated, so local commits are pushed to `origin` going forward.
