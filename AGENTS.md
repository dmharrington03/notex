# NoTeX — Agent Notes

## Project Summary

NoTeX is a Python CLI tool that scans a directory of handwritten lecture note
PDFs, runs them through the Mathpix API for OCR (text, LaTeX, figures), cleans
up the extracted text with an LLM, and writes organized Markdown into an
Obsidian vault. It is run manually by the user, is fully idempotent, and never
modifies its input.

**Status:** the original planned scope (docs/spec.md's roadmap, plus CLI
polish and reporting features added along the way) is complete and validated
against real data. There is no active phase or in-progress work right now —
new work starts as fresh, self-contained GitHub issues as it's identified.
This file describes the codebase's current, steady-state shape; it is not a
running implementation log.

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

Keep this file **minimal and durable**: architecture, conventions, config
schema, invariants, and accepted long-term limitations that a new agent
needs before touching the code. It is not a changelog. Detailed
implementation narrative, exact test counts, and real-data-validation
findings for a specific piece of work belong as **comments on the
corresponding GitHub issue**, not here. When a change alters something this
file describes (a config field, an invariant, a CLI flag, a known
limitation), update the relevant section in place rather than appending a
dated note.

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
  no-op. State tracking (`state.db`) is the sole mechanism for deciding
  what's already processed.

## Pipeline Architecture

Data flows through the following stages, each backed by its own module:

1. **Discovery** (`src/discovery.py`) — recursively walks `paths.input_root`,
   grouping PDFs by course subdirectory. Each file is classified as `NEW`,
   `UNCHANGED`, `CHANGED`, or `RETRY` via a two-tier check (mtime+size fast
   path, SHA-256 fallback) against `state.db`. PDFs directly under
   `input_root` (no course subdirectory) are grouped under the sentinel
   `UNGROUPED_COURSE_KEY` and are skipped outright — no course name exists to
   mirror into a cache/vault subdirectory.
2. **Mathpix OCR** (`src/mathpix.py`) — `MathpixClient` submits a PDF, polls
   until complete, then fetches and extracts the `md.zip` bundle (Markdown +
   `.jpg` figures) into the cache directory.
3. **LLM cleanup** (`src/llm.py`) — `cleanup_pdf()` sends the raw Mathpix
   Markdown to an LLM (via `litellm`) using a versioned prompt
   (`prompts/cleanup_v1.txt`), validates the result (length ratio, `$`/
   `\left`/`\right` balance, relaxed heading-count check), and falls back to
   the raw Mathpix Markdown on any failure — cleanup never raises, it just
   records `llm_status="failed"` and leaves `llm_model`/`llm_prompt_version`
   `None`. `needs_llm_reprocessing()` decides whether an `UNCHANGED` file
   still needs an LLM pass (ignores prompt version — only genuine content
   change or an explicit rerun flag triggers reprocessing).
4. **Figures + postprocessing** (`src/figures.py`, `src/postprocess.py`) —
   copies cached figures into the vault, rewrites each Markdown image
   reference's alt text to a numbered caption (`Figure N`, optionally with an
   `@darkmode` suffix — image paths themselves are left untouched, no
   wikilink conversion), parses `lecture_NN...` filenames, and builds YAML
   frontmatter.
5. **Vault write** (`src/vault.py`) — `write_lecture_note()` assembles the
   final `vault/{course}/Lecture NN.md` and writes it, normally
   unconditionally overwriting. It also implements **manual-edit conflict
   detection**: `state.db` stores a SHA-256 `vault_content_hash` of the last
   content the pipeline itself wrote; if the on-disk vault file differs from
   that hash (i.e. a human edited it) the write is skipped and
   `vault_status="conflict"` is recorded instead of clobbering the edit.
   `--force-vault-overwrite` bypasses this check unconditionally.
6. **Orchestration** (`src/main.py`) — `run()` (the testable core) ties
   discovery + `state.db` + every stage above into one pass;
   `main()`/`src/cli.py` provide the CLI wrapper. One `MathpixClient`/
   `LLMClient` pair is constructed per run.
7. **Reporting** (`src/reporting.py`) — a `Reporter` protocol
   (`on_discover`/`on_stage`/`on_detail`/`on_done`) decouples progress output
   from pipeline logic. `PlainReporter` prints line-based progress (gains
   detail lines under `--verbose`); `RichReporter` renders a live-updating
   table with a spinner, auto-selected when stdout is an interactive TTY and
   `rich` is importable, otherwise falling back to `PlainReporter`.
8. **Manual mode** (`scripts/manual_convert.py`) — a separate, stateless
   script (not a `main.py` flag) that runs the full pipeline (Mathpix → LLM
   → figures → frontmatter → vault write) for one explicit source PDF →
   destination `.md` pair, never touching `state.db` or `discovery.py`. Used
   for one-off conversions outside the normal indexed corpus.

`state.db` (`src/state.py`) is a single `pdf_state` table keyed on
`source_path`, updated via a **partial upsert** (`upsert_entry()` only
writes the columns it's given, so one stage's update never clobbers
another's). Tracked per file: source hash/mtime/size; Mathpix status/pdf id/
figure & page counts; LLM model/prompt version/status/validation result/
token counts/cost estimate; the resolved vault output path, vault status,
vault content hash, and per-stage timestamps.

## Configuration

`config.yaml` (gitignored, machine-specific — copy `config.example.yaml` to
create it) has these sections, each loaded by its own `load_*_config()` in
`src/config.py` with graceful fallback to documented defaults when a section/
key is missing:

- `paths` — `input_root`, `vault_root` (both required, no default).
- `mathpix` — `poll_interval_seconds`, `max_poll_attempts`.
- `llm` — `model`, `prompt_version`, and a `validation:` block
  (`min_length_ratio`/`max_length_ratio`).
- `output` — `date_format`; `course_tags`, a per-course tag list keyed by the
  raw course folder name (**there is no global/default tag list** — a course
  with no entry here produces untagged notes, a deliberate divergence from
  docs/spec.md's original `base_tags` concept); `figures_dark_mode_flag`, a
  single global toggle (no per-course override) appending `@darkmode` to
  every figure's alt text.
- `naming` — `lecture_prefix`, a single global prefix (no per-course
  override) used for both the vault filename (`Lecture 01.md`) and the
  frontmatter title.
- `cli` — `print_summary` (default `false`): whether `main()` prints the
  full processed/skipped/errors/tokens/cost breakdown at the end of a run,
  on top of the always-on `Reporter`/`on_done()` progress output.

Mathpix/LLM credentials live in `.env` (gitignored — see `.env.example`),
loaded by `load_mathpix_credentials()`.

**Course index generation is permanently out of scope.** docs/spec.md's
original Stage 6 (`_index.md` per course, a regenerated Markdown table of
lectures) is cancelled per explicit user direction — do not build this at
any point; docs/spec.md is retained purely as historical/superseded
reference where it disagrees with this file.

## CLI Flags

`src/cli.py`'s `build_arg_parser()` is the source of truth; summary:

- `--course NAME` — restrict the run to one course subdirectory (mutually
  exclusive with `--file`). Unknown course name is a clean no-op.
- `--file PATH` — restrict the run to exactly one PDF (must exist, end in
  `.pdf`, live under `paths.input_root`).
- `--dry-run` — report what would happen; no API calls, no
  `state.db`/cache/vault writes.
- `--force` — reprocess Mathpix + LLM regardless of `state.db`'s
  classification.
- `--rerun-llm` — reprocess the LLM stage for every eligible file
  regardless of stored status (reuses cached Mathpix output when
  available). Often combined with `--file`.
- `--force-vault-overwrite` — bypass manual-edit conflict detection for
  the whole run (a blunt, whole-run instrument — no per-file targeting).
- `--no-llm` — skip the LLM stage entirely; vault note is written from raw
  Mathpix Markdown, and `llm_status` stays unset so a later normal run
  picks the file up automatically.
- `--verbose`/`-v` — print additional per-stage detail (Mathpix poll
  counts, LLM token/cost, figure-copy actions, vault-write confirmations).

## Known Limitations / Accepted Behavior

These are deliberate design decisions or accepted edge cases, not open bugs:

- **Orphaned vault files on renaming `naming.lecture_prefix`.**
  `write_lecture_note()` derives its output filename from the *current*
  `lecture_prefix` but never removes a previously-written file under a
  different name for the same source PDF. Explicit user decision: this is
  acceptable, permanent behavior — no cleanup logic will be built for it.
  One narrow interaction: if `lecture_prefix` is changed and later reverted
  to a prior value, the recomputed path points at the old orphaned file, but
  the recorded `vault_content_hash` describes the intervening (different-
  prefix) file — this correctly-but-unhelpfully surfaces as
  `vault_status="conflict"` even though nobody manually edited the orphan.
  `--force-vault-overwrite` is the general escape hatch for this, same as
  any other recorded conflict.
- **No per-file conflict targeting.** `--force-vault-overwrite` is a blunt,
  whole-run instrument; there's no flag to clear one specific file's
  recorded conflict while leaving others alone.
- **`--no-llm` + vault-conflict retry interaction.** A file processed only
  ever with `--no-llm` has `output_path` left `None`, so
  `--force-vault-overwrite` alone can't retry a vault conflict recorded
  against it — combine with `--rerun-llm`/`--force` to get a real LLM pass
  (and a non-null `output_path`) first.
- **LLM cleanup can confidently substitute a wrong but plausible term** for
  garbled proper-noun OCR text (observed: garbled "Mann's rule" rewritten to
  "Wigner's rule" instead of the intended "Laporte's rule"). Undetectable by
  any current automated check — a real, unresolved accuracy risk for future
  prompt work.
- **`validate_cleanup()`'s delimiter-balance check is count-only,** not
  delimiter-type pairing (e.g. it won't catch `\left(` closed by
  `\right\rangle`) — a known, accepted limitation.
- **Heading-count validation is relaxed, not exact-match** — it only fails
  on *new* headings, since Mathpix sometimes emits a stray heading from a
  handwritten date/title line that the cleanup prompt is expected to drop.
- **No chunking for long documents** — not implemented; real PDFs processed
  so far are 1-2 pages.
- **No global/default tag list** (`output.course_tags` has no fallback) and
  **no course index generation** — see "Configuration" above.

## Mathpix API Notes

Corrections to keep in mind, verified against current docs.mathpix.com:

- **Status values:** `received → loaded → split → completed` (or `error`).
- **Upload:** multipart form-data with a `file` field plus an `options_json`
  field (a JSON-encoded string of options) — not a raw PDF binary body.
- **Figures:** request `conversion_formats: {"md.zip": true}` and extract the
  bundle locally. Do not rely on `GET /v3/pdf/{pdf_id}` returning a figure
  asset list (it only returns status/progress fields), and do not rely on
  `cdn.mathpix.com` image URLs staying valid long-term (30-day retention).
  `fetch_and_extract()` also polls `GET /v3/converter/{pdf_id}`'s
  `conversion_status`, which lags behind the main `status` field.
- **Figure format:** images inside the zip are `.jpg`, kept as-is (no PNG
  conversion).
- **Math delimiters:** the `md.zip` bundle's Markdown uses `$...$` for
  inline math and a `$$` fence on its own line before/after the expression
  for display math, never `\(...\)`/`\[...\]` — already matches the target
  vault convention, so the delimiter pass is validation/lint only, not a
  conversion step.
- 429s during polling are retried honoring `Retry-After` and don't count
  against `max_poll_attempts`.

## Smoke Test Findings

Observations from real Mathpix/LLM runs against handwritten lecture PDFs
that shaped the cleanup prompt and validation checks:

- Mathpix sometimes emits a stray Markdown heading from a handwritten
  date/title line (not real structure).
- Cursive handwriting produces frequent word-level OCR errors, including
  systematic (not random) domain-vocabulary misreads of the same word every
  occurrence — the cleanup prompt targets the *pattern*, not one hardcoded
  example.
- A dangerous OCR failure mode: garbled math can produce **syntactically
  valid but semantically wrong** LaTeX (e.g. handwritten `|n⟩` OCR'd as the
  literal command `\ln`) — passes every automated check; only a
  content-aware LLM pass has any chance of catching it.
- `\left`/`\right` delimiter-type mismatches occur in the raw OCR (e.g.
  `\left(` closed by `\right\rangle`).
- Handwritten bra-ket notation uses raw `\langle`/`\rangle`/`|` rather than
  semantic macros; the cleanup prompt normalizes to `\braket{x|y}` form —
  requires the vault's Obsidian renderer to define these macros (not
  standard LaTeX primitives).

## Testing Conventions

- **Automated unit tests** (`tests/`) mock all HTTP calls via `respx`. They
  must never hit the real Mathpix API — no network, no cost, safe for CI.
- **Manual smoke tests** (`scripts/`) hit a real external API (Mathpix for
  `smoke_test_mathpix.py`, the configured LLM provider via `litellm` for
  `smoke_test_llm.py`) and cost money per run. They are not part of the
  `pytest` suite and are run manually when validating actual output quality
  (OCR correctness on handwriting, or LLM cleanup/prompt quality, can't be
  asserted automatically).
- `RichReporter` tests never assert on actual rendered terminal output —
  they check internal state (row data, style/spinner selection,
  `__enter__`/`__exit__` lifecycle) and protocol conformance only.

## Directory Structure

```
notex/                      ← repo root
├── .env                    ← secrets (gitignored), see .env.example
├── config.yaml             ← machine-specific paths/settings (gitignored), see config.example.yaml
├── environment.yml         ← conda env spec (reproduce with `conda env create -f environment.yml`)
├── docs/
│   └── spec.md             ← original full spec (historical reference — this file wins where they disagree)
├── prompts/
│   └── cleanup_v1.txt      ← versioned LLM system prompt (loaded by prompt_version, see src/llm.py)
├── src/
│   ├── config.py           ← env/config loading (credentials, paths, mathpix polling, llm, output, naming, cli)
│   ├── mathpix.py          ← Mathpix API client (MathpixClient, process_pdf)
│   ├── state.py            ← state.db schema + CRUD (StateEntry, init_db, get_entry, upsert_entry)
│   ├── discovery.py        ← per-file two-tier change classification + recursive multi-course walk
│   ├── llm.py              ← LLM cleanup client + prompt loading + orchestration (cleanup_pdf, needs_llm_reprocessing)
│   ├── figures.py          ← figure copy-to-vault + Markdown image-reference caption rewriter
│   ├── postprocess.py      ← filename parsing + YAML frontmatter builder + delimiter-balance warning scan
│   ├── vault.py            ← assembles + writes final per-lecture vault .md, incl. manual-edit conflict detection
│   ├── reporting.py        ← Reporter protocol: PlainReporter + RichReporter progress UI
│   ├── cli.py              ← argparse scaffolding (build_arg_parser)
│   └── main.py             ← CLI orchestration entry point (RunSummary, run, main)
├── scripts/
│   ├── smoke_test_mathpix.py   ← manual, real-API Mathpix validation
│   ├── smoke_test_llm.py       ← manual, real-API LLM prompt-iteration script
│   └── manual_convert.py       ← manual mode: exact source PDF -> exact destination .md, full pipeline, stateless
├── tests/
│   ├── fixtures/           ← fixture data for mocked tests
│   └── test_*.py           ← one test module per src/ module, plus test_main.py/test_cli.py/test_manual_convert.py
├── state.db                ← SQLite state log, gitignored, created at runtime
└── _cache/                 ← gitignored, created at runtime
```

## Git / Issue Tracking

Issues are tracked on GitHub. Record detailed per-issue implementation notes
and real-data validation results as comments on the relevant GitHub issue —
see "Documentation Conventions" above.

**Commits and pushes require explicit review — never automatic.** An agent
must not run `git commit` or `git push` on its own initiative. Only do so
when the user's prompt explicitly instructs it for that turn.
