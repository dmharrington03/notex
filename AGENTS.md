# NoTeX — Agent Notes

## Project Summary

NoTeX is a Python CLI tool that scans a directory of handwritten lecture note
PDFs, runs them through the Mathpix API for OCR (text, LaTeX, figures), cleans
up the extracted text with an LLM, and writes organized Markdown into an
Obsidian vault. It is run manually by the user, is fully idempotent, and never
modifies its input.

## Delegating Work to Subagents

Prefer delegating exploration and multi-step work to subagents/the Task tool
rather than doing it all in the main agent loop — this keeps the main
context small and reduces token usage. Concretely:

- Use a subagent for open-ended searches or questions about the codebase
  (e.g. "where is X handled", "what's the current shape of Y") instead of
  chaining multiple Read/Grep/Glob calls directly.
- Use a subagent to execute self-contained, well-specified units of work
  (e.g. implementing one issue's scope, writing a batch of tests, running
  down a specific bug) when the task doesn't need to share live context
  with the main conversation.
- Launch multiple subagents in parallel when the work is independent (e.g.
  investigating two unrelated issues, or researching one thing while
  implementing another).
- Give each subagent a precise, self-contained prompt — it starts with no
  context — and tell it exactly what to return, since only its final
  message is visible back in the main conversation.
- Reserve doing the work directly (no subagent) for small, targeted edits
  where the overhead of delegating would exceed the savings, or where the
  task genuinely requires the main conversation's accumulated context.

## Documentation Conventions

Keep issue-by-issue progress notes in this file **minimal** — a short status
line per issue (done/pending + one-line summary, plus any deviation from plan
that future work needs to know about) is enough. Full implementation
narrative, exact test counts, and detailed real-data-validation findings
belong as **comments on the corresponding GitHub issue**, not in AGENTS.md.
This file should stay focused on durable information a new agent needs: the
codebase's current state, established conventions, and forward-looking plans.

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
  no-op. State tracking is the sole mechanism for deciding what's already
  processed.

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

**Phase 6 — Full config wiring + end-to-end validation — in progress.**
Phase 5 is VALIDATED — complete (see "Phase 5" under "Phase Progress" below
for its confirmed design/findings). Phase 6's scope: wire the remaining
still-hardcoded config values (`output.course_tags`, `output.date_format`,
`output.figures_dark_mode_flag`, `naming.lecture_prefix`) into real
`config.yaml` reads, then validate the complete pipeline end-to-end on real
data. See `docs/spec.md` for the full original roadmap (note: follow this
file, AGENTS.md, not spec.md, where they disagree).

**Scope correction — `output.base_tags` (a global/default tag list) is
dropped entirely, not deferred.** docs/spec.md's `output:` schema and this
file's own prior Phase 6 planning both included a `base_tags` global
default tag list, with `course_tags` merging/overriding it per course. Per
explicit user direction, **there is no global/default tag list at all** —
a course only gets tags if it has an explicit `output.course_tags` entry
for its raw folder name; a course with no entry produces untagged notes
(empty `tags` list). This is a deliberate divergence from docs/spec.md,
not a rewording — ignore spec.md's `base_tags` key going forward; it's
retained in that file purely as historical/superseded reference.
`src/config.py`'s `OutputConfig`/`load_output_config()` (issue #33) are
already updated to this design — no `base_tags` field, no
`DEFAULT_BASE_TAGS` constant. `src/postprocess.py`'s `DEFAULT_TAGS`
constant is unaffected for now (still a Phase 5 stand-in pending #35's
real wiring), but #35 must apply this same no-default rule when it wires
`course_tags` resolution into `build_frontmatter()`.

**Scope correction — course index generation is permanently dropped, not
deferred.** docs/spec.md's Stage 6 (`_index.md` per course, a regenerated
Markdown table of lectures) and this file's own prior planning both
described index generation as part of Phase 6. Per explicit user direction,
**this feature is cancelled entirely** — no `_index.md` file, wikilink
table, or per-course index regeneration will be built at any point in this
project, in any future phase. This is a deliberate divergence from
docs/spec.md, not a rewording — ignore spec.md's Stage 6 section going
forward; it's retained in that file purely as historical/superseded
reference.

Concretely, Phase 6 covers:
- `src/config.py`: `load_output_config()` — reads `output.course_tags`
  (dict keyed by the **raw course folder name**, underscores not converted
  to spaces; no global/default tag list — a course with no entry gets no
  tags, see the scope-correction note above), `output.date_format`,
  `output.figures_dark_mode_flag`. `load_naming_config()` — reads
  `naming.lecture_prefix` (a **single global value**, no per-course
  override). Both follow the fully-optional pattern already established by
  `load_llm_config()`: a missing file/section/key silently falls back to
  today's hardcoded defaults, never raises.
- `src/postprocess.py`: `build_frontmatter()` gains a configurable
  `date_format` param (replacing the hardcoded `DATE_FORMAT` constant) and
  a configurable `lecture_prefix` param for the `title` field (replacing
  `DEFAULT_LECTURE_PREFIX`); tag resolution (course_tags-or-nothing, no
  base_tags fallback) is a separate helper, not a change to
  `build_frontmatter()`'s existing `tags` param.
- `src/vault.py`: `write_lecture_note()` gains a `lecture_prefix` param,
  threaded to **both** the output filename and `build_frontmatter()`'s
  title, so the two can never disagree with each other.
- `src/main.py`: `run()` loads `OutputConfig`/`NamingConfig` internally
  when not passed in (same optional-param pattern already used for
  `llm_config`), threading both down through `_process_file()` /
  `_write_to_vault()` into `write_lecture_note()`.
- Real-data validation pass against `notes_raw/class_1` with a populated
  `output:`/`naming:` config (including a `course_tags` override), same
  precedent as every prior phase's closing validation issue.

Tracked in issues #33-#38 (`phase-6` label). See "Phase Progress" below for
status as issues complete.

## Remaining Work — Phases 4-7 Plan

Living, forward-looking plan for the rest of the pipeline. Phase 5 is
VALIDATED — complete; Phase 6 is current (see "Current Phase" above for the
confirmed design). Phases 6-7 below are still unimplemented forward-looking
plan only. Phase numbers match docs/spec.md's original roadmap.

**Follow-up carried forward from Phase 5's real-data validation (#32):**
once Phase 6 wires up `output.figures_dark_mode_flag` for real, re-do the
manual Obsidian visual check against a lecture with a figure (re-run with
the flag on, confirm the `@darkmode`-suffixed alt text actually renders/
behaves as intended in the configured renderer) — #32 only visually
confirmed the flag-off default path.

### Phase 4 — Figure handling (vault-facing)

Scope: copy each processed file's cached figures
(`_cache/{course}/figures/*.jpg`) into `vault/{course}/figures/`, and rewrite
the LLM-cleaned (or raw-fallback) Markdown's cache-relative
`![](figures/...)` image references to inject a numbered placeholder
caption (and, optionally, a dark-mode marker) into the alt-text slot.

**Note:** this section originally called for rewriting `![](figures/...)`
into Obsidian's `![[filename.jpg]]` wikilink form. That was overridden per
explicit user direction during implementation (issue #24) — the actual,
implemented behavior keeps standard Markdown `![alt](path)` syntax
throughout; only the alt-text slot is rewritten, and the image path itself
is left untouched.

- `src/figures.py` — figure-copy function and Markdown image-reference
  rewriter.
- **Dark-mode figure alt text.** `output.figures_dark_mode_flag: true|false`
  in `config.yaml` is a **single global toggle** (no per-course override).
  When enabled, `@darkmode` is appended to every figure's alt text so the
  user's Obsidian renderer handles dark-mode display — purely a Markdown
  text transform, no image processing.

### Phase 5 — Post-processing (vault-facing)

VALIDATED — complete. See "Current Phase" above for the full confirmed
design.

### Phase 6 — Full config wiring + end-to-end validation

Scope: wire up the remaining `config.yaml` sections nothing reads yet
(`output.course_tags`, `naming.lecture_prefix`, the new
`output.figures_dark_mode_flag`), and validate the complete pipeline
end-to-end on a real course. See "Current Phase" above for the full
confirmed design and tracked issue numbers.

**Course index generation is explicitly out of scope, permanently — not a
Phase 6 deferral.** docs/spec.md's Stage 6 (`_index.md` per course) is
cancelled per explicit user direction; see the "Current Phase" section
above for the full correction note. Nothing in the codebase implements or
plans for this, and no future phase revives it.

- Real-data validation pass, same shape as prior phases' precedent: run for
  real against `notes_raw/class_1`, confirming idempotency extends
  correctly to the newly-wired config values.

### Phase 7 — CLI polish + new feature requests

Absorbs docs/spec.md's original CLI scope, the already-built-but-unwired
`force_llm`/`target_source_path` infrastructure (this is where `main()`
finally parses real flags for them), and new features requested during
planning.

**Existing planned flags (docs/spec.md, still unimplemented):**
- `--dry-run` — report what would be processed, no API calls.
- `--force` — reprocess regardless of state.db.
- `--course NAME` — restrict a run to one course.
- `--refresh-llm-prompt` — CLI surface for `run()`'s existing `force_llm` param.
- A single-file rerun flag (name TBD, e.g. `--file PATH`) — CLI surface for
  `run()`'s existing `target_source_path` param.

**New features, with design decisions already confirmed:**

1. **`--no-llm` flag** — bypasses the LLM cleanup stage entirely for the run.
   `run()`/`_process_file()` gain a `no_llm: bool` param, same shape as
   `force_llm`. When set, only `mathpix_*` fields are upserted; no new
   `"skipped"` status value — a freshly-processed file just keeps
   `llm_status` NULL, so it's automatically picked up for a real LLM pass on
   the next normal run via the existing `needs_llm_reprocessing()` check.
   Vault-writing needs a content source when `llm_status` is NULL — reuse
   the existing LLM-failure fallback-to-raw-`.mathpix.md` code path.

2. **Manual mode — exact source/destination file, no scanning.** A
   **separate script**, not a `main.py` flag: `scripts/manual_convert.py`,
   following the `scripts/smoke_test_*.py` convention (hits real APIs, not
   under `pytest`) but covering the **full pipeline through vault writing**
   (mathpix → llm → figures → frontmatter → final `.md`). Takes an explicit
   source PDF path and destination `.md` path as CLI args. Never touches
   `state.db`, never calls `discovery.py` — entirely stateless, for one-off
   conversions outside the normal indexed corpus. Built after Phase 4/5,
   since it reuses their figure-copy/frontmatter functions as a library.
   Open details left for implementation time: where figures land relative
   to an arbitrary destination path; how frontmatter fields normally
   derived from course-folder structure get supplied when there's no course
   folder.

3. **`--verbose`/`-v` flag** — finer-grained per-stage progress detail
   (Mathpix poll counts via the existing `on_status` hook, LLM token/cost
   per file, per-figure copy actions, frontmatter/vault-write confirmation
   lines). Not a separate output mode — controls detail level within
   whichever renderer (plain or Rich) is active.

4. **Rich Live CLI output.** A shared reporting abstraction threaded through
   the pipeline, not print statements scattered across modules.
   - New module `src/reporting.py`: a small `Reporter` interface/protocol
     (e.g. `on_stage(file, stage)`, `on_detail(file, message)`,
     `on_done(file, status)`) with two implementations:
     - `PlainReporter` — today's `print()`-based behavior, gains
       `--verbose`-gated extra detail lines.
     - `RichReporter` — a `rich.live.Live` + `rich.table.Table`
       pre-populated with every file `discover_pdfs()` identified, updating
       each row's status cell in place through stages (waiting →
       submitting → polling → downloading → cleaning → writing vault →
       done/error).
   - **Activation:** auto-detected, not an explicit flag — `RichReporter`
     when stdout is an interactive TTY and `rich` is importable; falls back
     to `PlainReporter` otherwise.
   - `run()`/`_process_file()` gain an optional `reporter` param (defaulting
     to a no-op/plain instance), threaded down into `process_pdf()`'s
     `on_status` callback and new equivalent hooks in `cleanup_pdf()` and
     Phase 4/5's figure-copy/vault-write functions.
   - `rich` needs adding to `environment.yml` (conda-forge), not yet
     installed.
   - Testing convention: tests inject a fake/no-op `Reporter` — no test
     asserts on actual Rich terminal rendering.

## Phase Progress

Brief status only — see each issue's GitHub comments for implementation
narrative and real-data-validation findings.

### Phase 6 (in progress, issues #33-#38)

- **#33 (done)** — `src/config.py`: `OutputConfig` (`course_tags`/
  `date_format`/`figures_dark_mode_flag`) + `load_output_config()`, same
  fully-optional pattern as `load_llm_config()`. Deviates from the issue's
  original description: no `base_tags`/global-default tag field at all —
  see the "Scope correction" note under "Current Phase" above. Also fixed
  #35's stale `base_tags`-fallback description to match.
- **#34 (done)** — `src/config.py`: `NamingConfig` (`lecture_prefix`) +
  `load_naming_config()`, same fully-optional pattern as
  `load_llm_config()`/`load_output_config()`. `DEFAULT_LECTURE_PREFIX` is
  duplicated in `config.py` (matches `postprocess.py`'s existing constant),
  same no-cross-module-import precedent as `DEFAULT_DATE_FORMAT`. Added
  `naming:` section to `config.example.yaml`. No deviations from the
  issue's plan.
- **#35-#38 (not yet started)** — see "Current Phase" above for the full
  scope. Status lines will be added here as each issue completes.

### Phase 5 (VALIDATED — complete, issues #26-#32)

- **#26 (done)** — `vault_root` added to `PathsConfig` (required, no
  default).
- **#27 (done)** — `src/postprocess.py`: `parse_lecture_filename()` +
  `build_frontmatter()`. Unexpected: `date`/`processed` fields use **local
  time, not UTC** (deliberate — human-facing calendar fields, unlike the
  rest of the codebase's UTC convention).
- **#28 (done)** — `src/postprocess.py`: `scan_delimiter_issues()`
  (warn-only `$`/`\left`/`\right`-balance + literal `\(...\)`/`\[...\]`
  diagnostic scan).
- **#29 (done)** — `src/vault.py`: `write_lecture_note()` +
  `VaultWriteResult`. Orchestrates `parse_lecture_filename()` ->
  `copy_figures_to_vault()` -> read content -> `rewrite_image_references()`
  -> `scan_delimiter_issues()` (on the rewritten body) ->
  `build_frontmatter()` -> write `vault/{course}/Lecture NN.md`
  unconditionally overwriting. `vault_course_dir` is always explicitly
  `mkdir(parents=True, exist_ok=True)`'d (needed for the zero-figure case,
  since `copy_figures_to_vault()` only creates the `figures/` subdir when
  there are actual figures — that sub-behavior is untouched).
- **#30 (done)** — `state.py`: `vault_status`/`vault_path` nullable
  columns added (`_VALUE_COLUMNS`, `_CREATE_TABLE_SQL`, `StateEntry`),
  placed right after `output_path`. No schema-migration logic, same
  precedent as #21/#22.
- **#31 (done)** — `src/main.py`: wired `write_lecture_note()` into
  `_process_file()` via a new `_write_to_vault()` helper, called after the
  LLM stage on both the actionable NEW/CHANGED/RETRY path and the
  UNCHANGED-file LLM-only-rerun path. `run()` computes `vault_course_dir`
  (`paths_config.vault_root / course`, or the `_ungrouped` sentinel)
  alongside each existing `cache_dir` computation. `PostprocessError`/
  `OSError` from `write_lecture_note()` are caught, recording
  `vault_status="failed"` only (mathpix/llm fields untouched); success
  records `vault_status`/`vault_path`/`vault_written_at` (reusing
  `llm_result.processed_at`, no fresh timestamp). No new `RunSummary`
  field — folds into the existing `errors` count, matching #18's
  precedent. Found and fixed a pre-existing test gap:
  `test_run_target_source_path_force_processes_ungrouped_file`'s
  `stray.pdf` fixture doesn't match `parse_lecture_filename()`'s pattern,
  so real vault-writing now correctly surfaces that as an error
  (`vault_status="failed"`) — updated the test's expectations rather than
  masking it.
- **#32 (done)** — real-data validation against `notes_raw/class_1`: cold
  run, idempotent rerun, and the malformed-filename path all confirmed
  working as designed (no code changes needed); full findings in the
  issue's GitHub comments. Phase 5 is now marked VALIDATED — complete.

### Phase 4 (VALIDATED — complete, issues #23-#25)

- **#23** — `src/figures.py`: `copy_figures_to_vault()`. Zero-figure input
  is a no-op (no vault figures dir created); copies via `shutil.copy2`,
  overwriting for idempotent reruns.
- **#24** — `rewrite_image_references()`. Unexpected/deviates from original
  plan: only rewrites the alt-text slot (`Figure N` caption + optional
  `@darkmode`), not a wikilink conversion — image paths untouched (see
  Phase 4 plan note above for why).
- **#25** — Real-data validation against `_cache/class_1`: correct and
  idempotent, no code changes needed.

### Phase 3 (VALIDATED — complete, issues #13-#21)

- Heading-count validation is **relaxed, not exact-match** (fails only on
  *new* headings) — Mathpix sometimes emits a stray heading from a
  handwritten date/title line that the cleanup prompt is expected to drop.
- `needs_llm_reprocessing()` deliberately **ignores `llm_prompt_version`** —
  only `force_llm=True` or a real content change triggers reprocessing, not
  editing the prompt file or bumping `prompt_version`. Corrects
  docs/spec.md's reprocessing table.
- Chunking for long documents is deferred (not implemented) — real PDFs
  seen so far are 1-2 pages.
- Default model: **Claude Haiku 4.5** via `litellm` (`ANTHROPIC_API_KEY`),
  configurable via `config.yaml`'s `llm.model`.
- `validate_cleanup()` checks `length_ratio`, `dollar_balance`,
  `left_right_balance` (count-only, not delimiter-type pairing — known
  limitation), and `heading_count`.
- `cleanup_pdf()` never raises on LLM failure/validation failure — falls
  back to the raw `.mathpix.md`, with `llm_status="failed"` and
  `llm_model`/`llm_prompt_version` left `None`.
- Token/cost tracking (`llm_input_tokens`/`llm_output_tokens`/
  `llm_cost_estimate` in `state.db`, matching `RunSummary` totals) added in
  issue #21.
- **Known limitation, real-data validation:** the LLM can confidently
  substitute a wrong but plausible specific term for garbled proper-noun
  OCR text (observed: garbled "Mann's rule" rewritten to "Wigner's rule"
  instead of the intended "Laporte's rule") — undetectable by any current
  check, a real unresolved accuracy risk for future prompt work.

### Phase 2 (VALIDATED — complete, issues #7-#12)

- `src/state.py`: single `pdf_state` table keyed on `source_path`.
  `upsert_entry()` is a **partial** upsert — only passed columns are
  written, so one stage's update never nulls another's.
- `src/discovery.py`: `classify_pdf()` is a two-tier change-detector
  (mtime+size fast path, SHA-256 fallback). `Classification` enum: `NEW`,
  `UNCHANGED`, `CHANGED`, `RETRY`. `discover_pdfs()` recursively walks
  `input_root`, grouping by course subdirectory; ungrouped PDFs (directly
  under `input_root`) use sentinel key `UNGROUPED_COURSE_KEY` and are
  **skipped outright, not written to `state.db`** (no course name to
  mirror into `cache_dir`).
- `src/main.py`: `run()` (testable core) / `main()` (CLI wrapper) with one
  `MathpixClient` per run. `main()` returns `0` even with per-file errors
  recorded in `RunSummary` — only `1` if the run can't start at all.
- Real-data validation confirmed idempotent reruns against
  `notes_raw/class_1`.

### Phase 1 (VALIDATED — complete, issues #1-#6, #22)

- `src/mathpix.py`: `MathpixClient` (context manager, injectable
  `http_client=`/`sleep_fn=`). `submit()` → `poll_until_complete()` →
  `fetch_and_extract()`, orchestrated by `process_pdf()`. Exceptions:
  `MathpixError` base, `MathpixProcessingError`, `MathpixTimeoutError`.
  429s during polling are retried honoring `Retry-After` and don't count
  against `max_poll_attempts`.
- Unexpected API quirk: `fetch_and_extract()` also polls
  `GET /v3/converter/{pdf_id}`'s `conversion_status`, which lags behind the
  main `status` field — not in the original spec.
- Optional `on_status(...)` callback on every real status poll, for
  observability only (never affects control flow).
- `page_count` (issue #22 followup): best-effort, read from the already-
  fetched payload, no extra API call; nullable `state.db` column +
  `RunSummary.total_pages_processed`.
- Real-API smoke testing confirmed both zero-figure and one-figure paths
  end-to-end; findings that shaped later phases are in "Smoke test
  findings" below.

## Mathpix API notes

These correct assumptions in the original planning spec, verified against
current docs.mathpix.com. Follow these, not the original spec's Mathpix
section, when working on `src/mathpix.py`:

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
- **Math delimiters — CONFIRMED:** the `md.zip` bundle's Markdown uses
  `$...$` for inline math and a `$$` fence on its own line before/after the
  expression for display math, never `\(...\)`/`\[...\]`. This already
  matches the target vault convention, so Phase 5's "delimiter pass" is a
  pure validation/lint step (checking balance), not a conversion step.

## Smoke test findings

Observations from real Mathpix runs against handwritten lecture PDFs that
shaped the Phase 3 LLM cleanup prompt and Phase 3/5 validation checks:

- Mathpix sometimes emits a stray Markdown heading from a handwritten
  date/title line (not real structure) — why heading-count validation is
  relaxed, and the cleanup prompt is allowed to drop it.
- Cursive handwriting produces frequent word-level OCR errors, including
  systematic (not random) domain-vocabulary misreads of the same word every
  occurrence — the cleanup prompt targets the *pattern*, not one hardcoded
  example.
- A dangerous OCR failure mode: garbled math can produce **syntactically
  valid but semantically wrong** LaTeX (e.g. handwritten `|n⟩` OCR'd as the
  literal command `\ln`) — passes every automated check; only a
  content-aware LLM pass has any chance of catching it.
- `\left`/`\right` delimiter-type mismatches occur in the raw OCR (e.g.
  `\left(` closed by `\right\rangle`) — `validate_cleanup()`'s balance check
  is count-only by design (type-pairing isn't statically checkable), a
  known accepted limitation.
- Handwritten bra-ket notation uses raw `\langle`/`\rangle`/`|` rather than
  semantic macros; the cleanup prompt normalizes to `\braket{x|y}` form —
  requires the vault's Obsidian renderer to define these macros (not
  standard LaTeX primitives), worth confirming if not already done.

## Testing Conventions

- **Automated unit tests** (`tests/`) mock all HTTP calls via `respx`. They
  must never hit the real Mathpix API — no network, no cost, safe for CI.
- **Manual smoke tests** (`scripts/`) hit a real external API (Mathpix for
  `smoke_test_mathpix.py`, the configured LLM provider via `litellm` for
  `smoke_test_llm.py`) and cost money per run. They are not part of the
  `pytest` suite and are run manually when validating actual output quality
  (OCR correctness on handwriting, or LLM cleanup/prompt quality, can't be
  asserted automatically).

## Directory Structure

```
notex/                      ← repo root
├── .env                    ← secrets (gitignored), see .env.example
├── environment.yml         ← conda env spec (reproduce with `conda env create -f environment.yml`)
├── docs/
│   └── spec.md             ← original full spec (historical reference, see note at top of file)
├── prompts/
│   └── cleanup_v1.txt      ← versioned LLM system prompt (loaded by prompt_version, see src/llm.py)
├── src/
│   ├── config.py           ← env/config loading (load_mathpix_credentials, load_mathpix_polling_config, load_paths_config, load_llm_config)
│   ├── mathpix.py          ← Mathpix API client
│   ├── state.py            ← state.db schema + CRUD (StateEntry, init_db, get_entry, upsert_entry)
│   ├── discovery.py        ← per-file two-tier change classification + recursive multi-course walk (Classification, ClassificationResult, classify_pdf, compute_sha256, discover_pdfs, UNGROUPED_COURSE_KEY)
│   ├── llm.py              ← LLM cleanup client + prompt loading + orchestration (LLMClient, LLMError, load_prompt_text, validate_cleanup, cleanup_pdf, needs_llm_reprocessing)
│   ├── main.py             ← CLI orchestration entry point (RunSummary, run, main) — wires discovery + state.db + process_pdf() into a runnable pass over input_root
│   ├── figures.py          ← figure copy-to-vault + Markdown image-reference caption rewriter (copy_figures_to_vault, rewrite_image_references)
│   ├── postprocess.py      ← filename parsing + YAML frontmatter builder (parse_lecture_filename, build_frontmatter); delimiter-balance warning scan (scan_delimiter_issues)
│   ├── vault.py            ← assembles + writes final per-lecture vault .md (write_lecture_note)
│   └── reporting.py        ← [Phase 7, not yet implemented] Reporter interface (PlainReporter/RichReporter) for progress UI
├── scripts/
│   ├── smoke_test_mathpix.py   ← manual, real-API Mathpix validation
│   ├── smoke_test_llm.py       ← manual, real-API LLM prompt-iteration script
│   └── manual_convert.py       ← [Phase 7, not yet implemented] manual mode: exact source PDF -> exact destination .md, full pipeline, stateless (no state.db/discovery.py)
├── tests/
│   ├── fixtures/           ← fixture data for mocked tests
│   ├── test_mathpix.py
│   ├── test_state.py
│   ├── test_discovery.py
│   ├── test_config.py
│   ├── test_llm.py
│   ├── test_main.py
│   └── test_figures.py
├── state.db                ← SQLite state log, gitignored, created at runtime
└── _cache/                 ← gitignored, created at runtime
```

## Git / Issue Tracking

Issues are tracked on GitHub. Code was developed locally and held back from
the `origin` remote until Phase 1 was validated against real Mathpix output;
Phase 1 is now validated, so local commits are pushed to `origin` going
forward. Record detailed per-issue implementation notes and real-data
validation results as comments on the relevant GitHub issue — see
"Documentation Conventions" above.

**Commits and pushes require explicit review — never automatic.** An agent
must not run `git commit` or `git push` on its own initiative. Only do so
when the user's prompt explicitly instructs it for that turn.
