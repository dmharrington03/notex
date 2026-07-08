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

**Phase 5 — Post-processing (vault-facing).** Scope: YAML frontmatter
injection, a final delimiter-balance warning scan (never auto-fix), and
actually assembling + writing the finished Markdown into
`vault/{course}/Lecture NN.md`, per docs/spec.md Stage 5 — as corrected/
detailed below. Phase 4 (figure handling) is VALIDATED — complete; see
"Phase 4 progress" below. See `docs/spec.md` for the full 7-phase roadmap
(note: follow this file, AGENTS.md, not spec.md, where they disagree — see
below for a concrete correction this phase makes to spec.md's Error
Handling table).

Concretely, this phase covers:
- `src/config.py`: `load_paths_config()` extended to read the
  previously-unread `paths.vault_root` from `config.yaml`, added to
  `PathsConfig`. **Required, no default** (same treatment as `input_root`,
  not `cache_dir`/`state_db`) — there's no sensible fallback location for a
  user's Obsidian vault.
- `src/postprocess.py` (new module):
  - `parse_lecture_filename()` — extracts a lecture number (regex
    `lecture[_-]?(\d+)`, optionally followed by a trailing `_<topic>`
    segment, case-insensitive) and a course name (the PDF's immediate
    parent folder name, underscores replaced with spaces) from a source PDF
    path. Raises a new `PostprocessError` on an unparseable filename (no
    lecture-number match) rather than silently guessing.
  - `build_frontmatter()` — assembles the YAML frontmatter block
    (`title`/`course`/`date`/`lecture_number`/`tags`/`source_pdf`/
    `processed`). **`date` is sourced from the source PDF's filesystem
    mtime only** — confirmed with the user: no filename-date parsing this
    phase, since the real `notes_raw/class_1` files have no date segment in
    their names and mtime is already relied on elsewhere (tier-1 change
    detection). `tags` defaults to a hardcoded `["lecture-notes"]` module
    constant (`output.base_tags`/`course_tags` real config wiring is Phase
    6). The output-filename prefix ("Lecture") is likewise a hardcoded
    default pending Phase 6's `naming.lecture_prefix` wiring.
  - `scan_delimiter_issues()` — a warn-only diagnostic scan (never
    auto-fixes anything) of the actual content about to be written to the
    vault: reuses `validate_cleanup()`'s `$`/`$$` and `\left`/`\right`
    count-balance check logic, plus a new check for any literal
    `\(...\)`/`\[...\]` delimiters slipping through (docs/spec.md's
    explicit Stage 5 ask, not covered by Phase 3's `validate_cleanup()`).
    Returns a list of warning strings; printing them is the caller's
    (`src/main.py`'s) job, matching this module's "pure function" pattern.
- `src/vault.py` (new module): `write_lecture_note()` orchestrates
  `postprocess.parse_lecture_filename()` + Phase 4's
  `figures.copy_figures_to_vault()`/`rewrite_image_references()` +
  `postprocess.build_frontmatter()`/`scan_delimiter_issues()` into the
  final `vault/{course}/Lecture NN.md`, overwriting unconditionally on
  rerun (per docs/spec.md's Error Handling table: "Output file already
  exists in vault → Overwrite").
- `src/state.py`: two new nullable columns — `vault_status`
  (`"success"`/`"failed"`, same per-stage-status convention as
  `mathpix_status`/`llm_status`) and `vault_path` (the final vault `.md`
  location). **Kept deliberately separate from the existing `output_path`
  column**, which keeps its Phase 3 meaning unchanged (the cache-stage
  `.llm.md`/`.mathpix.md` path written by `cleanup_pdf()`) — confirmed
  with the user rather than repurposing `output_path` and risking breaking
  Phase 3's established semantics. The already-reserved `vault_written_at`
  column (present since Phase 2, always `NULL` until now) is populated for
  the first time.
- `src/main.py`: `run()`/`_process_file()` wired to call
  `write_lecture_note()` right after `cleanup_pdf()` succeeds (or falls
  back) on the actionable NEW/CHANGED/RETRY path, **and** on the
  UNCHANGED-file LLM-only-rerun path (reprocessed LLM content needs to
  reach the vault too) — confirmed with the user that Phase 5 fully wires
  vault-writing into a real pipeline run this phase, rather than leaving
  `src/postprocess.py`/`src/vault.py` standalone/unwired the way Phase 4
  initially left `src/figures.py` unwired until Phase 5/6.

**Deliberate correction to docs/spec.md's Error Handling table**, confirmed
with the user: spec.md's original wording for a malformed/unparseable
filename is "skip file, do not add to state log" — written for a
single-stage pipeline. By Phase 5, a file's Mathpix and LLM stages may
already have succeeded and been recorded in `state.db` *before*
vault-writing ever looks at the filename. The corrected behavior: a
`PostprocessError` from an unparseable filename is caught per-file,
`vault_status="failed"` is recorded, but that same file's already-successful
`mathpix_status`/`llm_status`/`output_path` are left completely untouched —
a vault-write failure must never retroactively erase or reinterpret an
earlier stage's already-recorded success. Counts toward `RunSummary.errors`;
the run continues to the next file.

Still out of scope this phase: course index generation (`_index.md` per
course, docs/spec.md Stage 6), and the remaining `config.yaml` sections
nothing reads yet (`output.base_tags`/`course_tags`, `naming.lecture_prefix`,
`output.figures_dark_mode_flag`) — those are Phase 6, per the "Remaining
Work" plan below. Also out of scope: a `needs_vault_rewrite()`-style
independent staleness/retry check for a `vault_status == "failed"` row
(mirroring `needs_llm_reprocessing()`) — a failed vault-write is currently
only retried incidentally, whenever that file's Mathpix+LLM stage happens to
run again (e.g. a real content change, or `force_llm=True`); flagged as a
possible fast-follow rather than built speculatively now, to be revisited if
issue #32's real-data validation shows it's actually needed.

Tracked in issues #26-#32 (`phase-5` label). See "Phase 5 progress" below
for status as each issue lands.

## Remaining Work — Phases 4-7 Plan

Living, forward-looking plan for the rest of the pipeline. Phase 4 is now
VALIDATED — complete (issues #23-#25); Phase 5 is the current phase (issues
#26-#32 — see "Current Phase" above for the confirmed, up-to-date design).
Phases 6-7 below are still unimplemented forward-looking plan only.
Supersedes docs/spec.md's Phase 4-7 descriptions wherever a more specific
decision has since been made (same "AGENTS.md wins on disagreement" rule as
everywhere else in this file). Phase numbers below match docs/spec.md's
original roadmap; each phase also folds in whichever newly-requested
features (this planning round) naturally belong there.

### Phase 4 — Figure handling (vault-facing)

Scope: copy each processed file's cached figures
(`_cache/{course}/figures/*.jpg`) into `vault/{course}/figures/`, and rewrite
the LLM-cleaned (or raw-fallback) Markdown's cache-relative
`![](figures/...)` image references to inject a numbered placeholder
caption (and, optionally, a dark-mode marker) into the alt-text slot.

**Correction to this section, per issue #24 (see its Phase 4 progress
entry below for the full decision record):** this section originally
called for rewriting `![](figures/...)` into Obsidian's `![[filename.jpg]]`
wikilink form. That was overridden per explicit user direction during
implementation — the actual, implemented behavior keeps standard Markdown
`![alt](path)` syntax throughout; only the alt-text slot is rewritten
(numbered `Figure N` caption, plus `@darkmode` when enabled), and the image
path itself is left completely untouched. Every "wikilink" reference below
in this section describes the original (superseded) plan, not the
implemented behavior.

- Likely new module `src/figures.py` (matches docs/spec.md's original file
  layout) — a figure-copy function and a Markdown image-reference rewriter.
- **New feature — dark-mode figure alt text.** New `output.figures_dark_mode_flag:
  true|false` key in `config.yaml` — confirmed as a **single global toggle**
  (no per-course override, matching the feature's stated simplicity). When
  enabled, this same rewrite step appends `@darkmode` to every figure's alt
  text/wikilink so the user's Obsidian renderer handles dark-mode display —
  purely a Markdown text transform, no image processing of any kind. Exact
  wikilink alt-text syntax (`![[file.jpg|@darkmode]]` vs. an alt-text-bearing
  variant) is a detail for implementation time, not decided now.
  `config.example.yaml` gets a matching commented-out key.
  - Also worth revisiting here per the Phase 1 smoke test findings: Mathpix
    supplies no alt text at all for figures — consider injecting a
    placeholder caption (e.g. "Figure 1") when `figures_dark_mode_flag` is
    off too, so alt text isn't just empty.

### Phase 5 — Post-processing (vault-facing)

**This is now the current phase — see "Current Phase" above for the full
confirmed, up-to-date design (issues #26-#32).** The bullets below are the
original forward-looking plan from before implementation started; retained
for history, superseded by "Current Phase" wherever more specific decisions
have since been made (e.g. course/lecture-number parsing lands in
`src/postprocess.py`, not derived some other way; date is mtime-only, no
filename-date parsing this phase; `vault_path`/`vault_status` are new
columns kept separate from the existing `output_path`).

Scope: YAML frontmatter injection, a final delimiter-balance validation pass
(warn-only, per docs/spec.md — never auto-fix), and actually writing the
finished Markdown into `vault/{course}/Lecture NN.md`.

- Likely new module(s): `src/postprocess.py` (frontmatter builder +
  delimiter warning scan) and `src/vault.py` (owns the "assemble and write
  the final per-lecture file" responsibility: frontmatter + figures'
  already-rewritten content + LLM/raw body).
- `paths.vault_root` (already present in `config.yaml`/read into no config
  loader function yet) gets consumed for the first time here.
- Course/lecture-number/date parsing from the source filename (docs/spec.md's
  Naming Convention section) also lands here, since frontmatter needs it.

### Phase 6 — Full config wiring + end-to-end validation

Scope: wire up the remaining `config.yaml` sections nothing reads yet
(`output.base_tags`/`course_tags`, `naming.lecture_prefix`, the new
`output.figures_dark_mode_flag`), add course index generation, and validate
the complete pipeline end-to-end on a real course.

- Course index generation (`_index.md` per course, docs/spec.md Stage 6):
  always fully regenerated (not incremental) after a course's files are
  processed, so it stays correct even after reprocessing/renames. Likely
  lives alongside the per-lecture writer in `src/vault.py`.
- Real-data validation pass, same shape as issues #12/#20/#22's precedent:
  run for real against `notes_raw/class_1`, confirming idempotency extends
  correctly to the new vault-writing/index stages (e.g. rerunning doesn't
  rewrite unchanged vault files or duplicate index rows).

### Phase 7 — CLI polish + new feature requests

Absorbs docs/spec.md's original CLI scope, the already-built-but-unwired
`force_llm`/`target_source_path` infrastructure (issue #18 — this is where
`main()` finally parses real flags for them), and every feature requested in
this planning round.

**Existing planned flags (docs/spec.md, still unimplemented):**
- `--dry-run` — report what would be processed, no API calls.
- `--force` — reprocess regardless of state.db.
- `--course NAME` — restrict a run to one course.
- `--refresh-llm-prompt` — CLI surface for `run()`'s existing `force_llm` param.
- A single-file rerun flag (name TBD, e.g. `--file PATH`) — CLI surface for
  `run()`'s existing `target_source_path` param.

**New features (this planning round), with design decisions confirmed with
the user so far:**

1. **`--no-llm` flag** — bypasses the LLM cleanup stage entirely for the run.
   - `run()`/`_process_file()` gain a `no_llm: bool` param, same shape as the
     existing `force_llm` param. When set, `cleanup_pdf()` is never called
     for actionable files; only `mathpix_*` fields are upserted this pass —
     `llm_status`/`llm_*`/`output_path` are left exactly as-is in state.db
     (confirmed: **no new `"skipped"` status value** — a freshly-processed
     file just keeps `llm_status` NULL, identical to "LLM stage never
     attempted").
   - Confirmed consequence, requiring no extra state-machine logic: since
     `needs_llm_reprocessing()` already treats `llm_status is None` as
     "needs reprocessing," a file processed once with `--no-llm` is
     automatically picked up and given a real LLM pass on the very next
     normal run (no `--no-llm`) with zero special-casing.
   - Open detail for implementation time: Phase 4/5's vault-writing step
     needs a content source when `llm_status` is NULL for a file — reuse the
     existing LLM-failure fallback-to-raw-`.mathpix.md` code path, just
     triggered by "never attempted" rather than "attempted and failed."

2. **Manual mode — exact source/destination file, no scanning.** Confirmed
   design: a **separate script**, not a `main.py` flag.
   - New `scripts/manual_convert.py`, following the existing
     `scripts/smoke_test_*.py` convention (hits real APIs, not under
     `pytest`) but — unlike those, which stop at the cache stage — covering
     the **full pipeline through vault writing** (mathpix -> llm -> figures
     -> frontmatter -> final `.md`), confirmed as in scope.
   - Takes an explicit source PDF path and an explicit destination `.md`
     path as CLI args. Never touches `state.db`, never calls
     `discovery.py` — entirely stateless/untracked, for one-off conversions
     outside (or overriding) the normal indexed corpus.
   - Realistically built *after* Phase 4/5 land, since it reuses their
     figure-copy/frontmatter functions directly as a library rather than
     duplicating that logic.
   - Open details, deliberately left for implementation time: where figures
     land relative to an arbitrary destination path (likely a sibling
     `figures/` dir next to the destination file); how frontmatter fields
     normally derived from course-folder structure (course name, lecture
     number) get supplied when there's no course folder to derive them from
     (CLI args vs. best-effort parsing of the destination path).

3. **`--verbose`/`-v` flag** — finer-grained per-stage progress detail
   (Mathpix poll counts via the existing `on_status` hook in
   `src/mathpix.py`, LLM token/cost per file, per-figure copy actions,
   frontmatter/vault-write confirmation lines). Confirmed: **not a separate
   output mode** — it controls detail level within whichever renderer
   (plain or Rich, see below) is active for the run.

4. **Rich Live CLI output.** Confirmed design: a shared reporting
   abstraction threaded through the pipeline, not print statements
   scattered across `main.py`/`mathpix.py`/`llm.py`.
   - New module, likely `src/reporting.py`: a small `Reporter`
     interface/protocol (e.g. `on_stage(file, stage)`, `on_detail(file,
     message)`, `on_done(file, status)`) with two implementations:
     - `PlainReporter` — today's `print()`-based behavior, gains
       `--verbose`-gated extra detail lines.
     - `RichReporter` — a `rich.live.Live` + `rich.table.Table`
       pre-populated with every file `discover_pdfs()` identified (or the
       single target file for a `target_source_path` run), updating each
       row's status cell in place through stages such as waiting ->
       submitting -> polling -> downloading -> cleaning -> writing vault ->
       done/error.
   - **Activation, confirmed:** auto-detected, not an explicit flag —
     `RichReporter` is used when stdout is an interactive TTY and `rich` is
     importable; falls back to `PlainReporter` otherwise (piped output, CI,
     `rich` not installed).
   - Plumbing: `run()` and `_process_file()` gain an optional `reporter`
     param (defaulting to a no-op/plain instance, so existing tests are
     unaffected), threaded down into `process_pdf()`'s existing `on_status`
     callback param and into new equivalent hooks added to `cleanup_pdf()`
     and Phase 4/5's figure-copy/vault-write functions.
   - `rich` will need to be added to `environment.yml`
     (`conda install -n notex -c conda-forge rich`), per the conda-only
     rule — not yet installed.
    - Testing convention (to establish when implemented): tests inject a
      fake/no-op `Reporter`, matching every other injectable-dependency
      precedent in this codebase (`http_client=`, `completion_fn=`,
      `sleep_fn=`) — no test ever asserts on actual Rich terminal rendering.

### Phase 5 progress

**Phase 5 status: planned, issues opened, implementation not yet started.**
Scoped into 7 issues (#26-#32, `phase-5` label), matching the granularity of
Phase 3/4's issue breakdown:

- **#26** — `src/config.py`: extend `load_paths_config()` to read
  `paths.vault_root` (required, no default).
- **#27** — `src/postprocess.py`: `parse_lecture_filename()` +
  `build_frontmatter()`.
- **#28** — `src/postprocess.py`: `scan_delimiter_issues()` warning scan.
- **#29** — `src/vault.py`: `write_lecture_note()` (assembles frontmatter +
  Phase 4's figure copy/rewrite + delimiter scan into the final vault
  `.md`).
- **#30** — `src/state.py`: new `vault_status`/`vault_path` columns
  (reuses the already-existing `vault_written_at` column for the
  timestamp, rather than adding a redundant one).
- **#31** — `src/main.py`: wire the vault-writing stage into `run()`/
  `_process_file()`, including the malformed-filename error-handling
  correction described in "Current Phase" above.
- **#32** — Real-data validation against `notes_raw/class_1` (same shape
  as issues #12/#20/#22/#25's precedent), including an Obsidian visual
  check of rendered frontmatter/figures and a real malformed-filename
  exercise. Marks Phase 5 VALIDATED once complete.

All design decisions listed under "Current Phase" above (the `vault_path`
vs. reusing `output_path` question, the new `vault_status` column, the
Error Handling table correction, mtime-only frontmatter dates, wiring
directly into `run()` this phase rather than leaving it standalone, the
hardcoded-default tags, and the delimiter-scan design) were confirmed with
the user before any issue was opened, following the same "confirm major
decisions up front" convention as Phases 3/4.

### Phase 4 progress

**Phase 4 status: VALIDATED — complete.** All Phase 4 issues (#23-#25) are
implemented and unit-tested (tmp_path-backed, no mocking — 138 tests
passing), and the figure-copy + image-reference-rewrite functions have
additionally been exercised for real against `_cache/class_1`'s actual
cached output (issue #25) — see its entry below for findings. Real wiring
into an actual pipeline call site (`vault_root`, a config-driven
`dark_mode` value) remains Phase 5/6's job.

- **Issue #23 (`src/figures.py`: figure copy-to-vault function) — done.**
  Implemented in new module `src/figures.py`, tested in
  `tests/test_figures.py` (6 tmp_path-backed cases, no mocking, all
  passing — full suite is 131 passing). Conventions established here that
  the rest of Phase 4 (#24, #25) should follow:
  - `copy_figures_to_vault(cache_figures_dir, vault_figures_dir) ->
    list[Path]` takes explicit source/dest `Path` params — no
    `config.py` reading, no `paths_config.vault_root` lookup — matching
    `cleanup_pdf()`'s `dest_dir`-as-param precedent from Phase 3
    (`src/llm.py`). Real wiring of `vault_root` into an actual call site
    is still Phase 5/6's job.
  - **Zero-figure case is a no-op, not an error**: when
    `cache_figures_dir` doesn't exist at all (e.g. `lecture_01.pdf`, which
    has no figures), the function returns `[]` immediately and does
    **not** create `vault_figures_dir` — mirrors
    `fetch_and_extract()`'s existing zero-figure handling (issue #3),
    where no `figures/` directory is created when there's nothing to put
    in it. An *existing-but-empty* `cache_figures_dir` is treated
    differently: `vault_figures_dir` is still created (mkdir), just with
    nothing copied into it, since in that case the caller-observable
    input state is "a figures dir exists" rather than "no figures stage
    ran at all."
  - **No extension filtering** — every non-hidden regular file in
    `cache_figures_dir` is copied regardless of extension, confirmed with
    the user: `cache_figures_dir` is entirely cache-managed by
    `fetch_and_extract()` and only ever contains real figure files
    (currently always `.jpg`, per AGENTS.md's Mathpix API notes), so
    filtering would be unnecessary complexity for no real guardrail
    benefit.
  - **Hidden files (names starting with `.`, e.g. a stray `.DS_Store`)
    are skipped**, confirmed with the user — matches
    `src/discovery.py`'s existing hidden-file-skipping convention for
    course/PDF discovery.
  - Copies via `shutil.copy2` (preserves file metadata, not just bytes),
    overwriting any existing file of the same name — keeps reruns
    idempotent (same deterministic filename in, same file overwritten,
    no duplication) without any `state.db` bookkeeping. `src/figures.py`
    does not touch `state.db` at all, matching the issue's explicit
    scope.
  - **Returns a sorted `list[Path]` of the destination files actually
    written**, not a bare count — confirmed with the user as the more
    useful shape for issue #24's reference rewriter and Phase 5's
    `vault.py` caller to consume directly.
  - Not yet exercised against real cached figures — that's issue #25's
    real-data validation scope, run after #24 also lands.

- **Issue #24 (`src/figures.py`: Markdown image-reference caption
  rewriter + dark mode + placeholder alt text) — done.** Implemented in
  `src/figures.py`, tested in `tests/test_figures.py` (8 new pure-string
  cases — single/multiple refs, dark mode, same-image-twice numbering,
  non-figures/ content untouched, discarded existing alt text, no-op on no
  references — no mocking, no network, full suite is 138 passing).
  **Significant deviation from the issue's original text, confirmed with
  the user during implementation:** the issue as written called for
  rewriting `![](figures/...)` references into Obsidian's `![[filename.jpg]]`
  wikilink form. Per explicit user direction, this was overridden to keep
  **standard Markdown `![alt](path)` syntax instead** — only the
  alt-text slot is rewritten; the underlying design goals (numbered
  placeholder caption, dark-mode marker) are otherwise implemented exactly
  as scoped. Decisions made:
  - `rewrite_image_references(markdown_text, dark_mode=False) -> str`
    matches the issue's exact signature. Internally uses a new
    `_FIGURE_REF_PATTERN` regex (`!\[[^\]]*\]\((figures/[^)\s]+)\)`) — same
    `![alt](path)` shape as `fetch_and_extract()`'s existing image-reference
    parsing (`src/mathpix.py`, issue #3), but scoped specifically to the
    `figures/` prefix, per the issue's explicit requirement that only
    recognized `![](figures/...)` references are touched. Everything else
    in the Markdown (prose, math, headings, non-`figures/` image
    references such as an external URL) is left byte-for-byte untouched.
  - **The image path itself is left completely untouched** — confirmed
    with the user as the specific consequence of dropping the wikilink
    approach: `figures/lecture_02_fig_001.jpg` stays exactly that, since
    it already correctly resolves relative to where Phase 5 will write
    the vault note (`vault/{course}/Lecture NN.md` sitting alongside
    `vault/{course}/figures/`). Only the alt-text slot changes:
    `![](figures/x.jpg)` → `![Figure 1](figures/x.jpg)`.
  - **Any pre-existing alt text is unconditionally discarded and replaced**
    with the numbered placeholder caption — Mathpix supplies no alt text
    at all in practice (`![]()`), so this is a non-issue in real output,
    but the function doesn't attempt to preserve/append to whatever alt
    text (if any) happened to be there.
  - **Caption numbering is per-occurrence, not per-unique-path** —
    confirmed with the user: a plain sequential counter
    (`itertools.count(1)`) advances on every regex match in document
    order, so if the same image file is (unusually) referenced twice, each
    occurrence still gets its own incrementing number (`Figure 1`,
    `Figure 2`, ...) rather than sharing one — matches the issue's literal
    "numbered by order of appearance in the document" wording and needs no
    path-tracking bookkeeping.
  - **Combined dark-mode syntax, confirmed with the user:**
    `" @darkmode"` is appended directly to the caption inside the same
    alt-text slot, space-separated — e.g. `![Figure 1 @darkmode](figures/x.jpg)`.
  - `config.yaml`/`config.example.yaml` both gained a new commented-out
    `output:` section (`figures_dark_mode_flag: false`), documenting the
    intent per AGENTS.md's own Phase 4 bullet — `dark_mode` stays a plain
    function parameter here; wiring a real `load_output_config()` reader
    and threading the config value through an actual call site is still
    deferred to Phase 6, per the issue's explicit scope note. (Key renamed
    from an original `figures_dark_mode` to `figures_dark_mode_flag`,
    per user request after initial implementation.)
  - Not yet exercised against real cached `.llm.md`/`.mathpix.md` output —
    that's issue #25's real-data validation scope, run after this issue.

- **Issue #25 (real-data validation: figure copy + image-reference rewrite
  against `notes_raw/class_1`) — done.** Exercised via ad hoc `python -c`
  calls against the real `_cache/class_1` output (no new source file, no
  mocking) — matching the #12/#20/#22 real-data-validation precedent. No
  code changes were needed; every function behaved exactly as documented.
  **Note on scope vs. the issue's original text:** the issue as originally
  written (opened before #24 landed) asks to validate a wikilink rewrite
  and to visually confirm rendering "in Obsidian." Per #24's already-
  documented correction, the implemented behavior is standard Markdown
  `![alt](path)`, not a wikilink — this validation pass tests that actual
  behavior. The Obsidian-open visual-confirmation step was explicitly
  descoped by the user for this pass (see below) in favor of a structural
  path-resolution check.
  - **`copy_figures_to_vault()`**: run for real against
    `_cache/class_1/figures/lecture_02_fig_001.jpg` into
    `vault/class_1/figures/` (per `config.yaml`'s configured `vault_root`).
    SHA-256 of source and copied file matched exactly (byte-for-byte).
    Rerunning the identical call was confirmed idempotent: the destination
    directory still contained exactly one file afterward (no duplication),
    with unchanged content.
  - **`rewrite_image_references()`**: run against the real cached
    `lecture_01.llm.md` (zero figures) and confirmed a true no-op — output
    byte-for-byte identical to input, since the file has no `figures/`
    references at all. Run against the real `lecture_02.llm.md` (one
    figure): `dark_mode=False` rewrote
    `![](figures/lecture_02_fig_001.jpg)` to
    `![Figure 1](figures/lecture_02_fig_001.jpg)`, with a line-by-line diff
    confirming that was the *only* line in the entire document that
    changed; `dark_mode=True` produced
    `![Figure 1 @darkmode](figures/lecture_02_fig_001.jpg)`. Both match the
    implementation's documented behavior exactly.
  - **Path-resolution check (in place of an Obsidian visual open,
    confirmed with the user as sufficient for this pass):** confirmed
    programmatically that the rewritten reference's relative path
    (`figures/lecture_02_fig_001.jpg`), resolved from a hypothetical
    `vault/class_1/*.md` note location (i.e. sibling to
    `vault/class_1/figures/`), points at exactly the file
    `copy_figures_to_vault()` wrote in the step above. Actually opening the
    vault in Obsidian to visually confirm rendering was explicitly
    descoped: since #24 emits standard Markdown alt text rather than an
    Obsidian wikilink, and no caption-rendering plugin/CSS is configured
    in this vault, Obsidian wouldn't display the "Figure N"/`@darkmode`
    caption text as a visible caption by default anyway (only in the
    underlying `alt` attribute) — revisit if/when a caption
    plugin/CSS snippet is set up (no current phase plan requires one).
  - **Invariants confirmed:** no `state.db` writes occurred during this
    validation (`state.db`'s mtime was unchanged before/after — this
    validation never imports or calls `src.main`/`src.state`, per the
    issue's explicit requirement); `notes_raw/class_1`'s two source PDFs'
    mtimes were also unchanged throughout.
  - Full `pytest` suite reconfirmed passing (138 passed) after this
    validation — no code changes were made, so this is a pure sanity
    check.
  - Confirms **Phase 4 is standalone/unwired**: nothing from `src/figures.py`
    is called from `src/main.py`/`run()` yet — real wiring into an actual
    pipeline call site (with a real `vault_root`-derived destination and a
    real config-driven `dark_mode` value) is Phase 5/6's job, per
    AGENTS.md's existing Phase 4/5/6 scope notes.

### Phase 3 progress

**Phase 3 status: VALIDATED — complete.** All Phase 3 issues (#13-#20) are
implemented, and the full Mathpix+LLM pipeline has additionally been run for
real end-to-end against `notes_raw/class_1`'s two PDFs (issue #20), including
the LLM stage's idempotency and forced-reprocessing guarantees — see the
issue #20 entry below for the real-run findings. Design decisions made
during planning are recorded here so they aren't lost:

- **Heading-count validation is relaxed, not exact-match.** docs/spec.md
  calls for an exact heading-count match; this phase instead fails
  validation only if the cleaned output has *more* headings than the
  original (a hallucinated new heading). Equal or fewer passes. This is
  deliberate: the Phase 1 smoke test found Mathpix emitting a stray heading
  from a handwritten date/title line (`## Lecture 21-4/14`) that isn't real
  document structure, and `prompts/cleanup_v1.txt` (#14) is expected to
  instruct the LLM to drop exactly that kind of artifact. An exact-match
  check would make that cleanup impossible to pass validation.
- **The LLM-staleness check lives in `src/llm.py`, not `src/discovery.py`.**
  `needs_llm_reprocessing(entry) -> bool` is a new, separate function from
  `discovery.py`'s `classify_pdf()` — `discovery.py`'s docstring already
  scopes it to Mathpix-stage change detection only, so LLM-stage staleness
  (a different question: "has this file's cached Mathpix output ever been
  successfully cleaned by the LLM stage?") stays a separate concern owned
  by the module that implements the LLM stage itself.
- **Chunking for long documents is deferred, not implemented this phase.**
  docs/spec.md's Stage 3 calls for splitting on Mathpix page-break markers
  above a configurable token threshold, with overlap/dedup on reassembly.
  Real lecture PDFs observed so far (Phase 1/2's `notes_raw/class_1` smoke
  tests) are 1-2 pages, well under any plausible token threshold. Building
  chunking logic now would be untested speculative logic with no real
  document to validate it against — revisit as its own future issue once an
  actual long lecture is encountered.
- **Default model: Claude Haiku 4.5** (`claude-haiku-4-5-20251001` via
  `litellm`, requires `ANTHROPIC_API_KEY`). Chosen over docs/spec.md's
  primary suggestion (GPT-4o-mini) because the Phase 1 smoke test's hardest
  OCR-cleanup cases are subtle, context-dependent misreads (systematic
  domain-vocabulary substitutions like "parity"→"party", and the "quietly
  dangerous" `|n⟩`→`\ln` misread) rather than simple spelling errors — these
  benefit from a model with stronger contextual understanding and precise
  instruction-following, and cost is negligible either way at this note
  volume (a few pages per lecture). `config.yaml`'s `llm.model` makes this
  trivially swappable (e.g. to Sonnet) if Haiku's output quality doesn't
  hold up during #19's prompt-iteration smoke testing. (Updated during
  issue #13's implementation from the originally-planned Claude 3.5 Haiku
  to Claude Haiku 4.5, per user direction — same rationale, newer model
  generation.)
- **`needs_llm_reprocessing()` deliberately ignores `llm_prompt_version`.**
  See the "Deliberate correction to docs/spec.md's Reprocessing logic
  table" note above (Current Phase section) — this is the phase's most
  significant deviation from docs/spec.md and is why `run()` gains a
  `force_llm` parameter (#18) as forward-looking infrastructure for a Phase
  7 CLI flag.
- **`run()` also gains a `target_source_path` parameter (#18), infrastructure
  for a future single-file rerun CLI flag.** Confirmed use case: after
  processing a lecture and spotting an LLM cleanup error, manually tweak the
  prompt and rerun just that one file rather than the whole corpus.
  `target_source_path` bypasses the full `discover_pdfs()` walk (classifies
  just the one given PDF directly) and, combined with `force_llm=True`,
  reprocesses only that file's LLM stage. `main()` doesn't parse a
  `--file`-style flag for this yet — still Phase 7 — but the parameter and
  code path land now, alongside `force_llm`, so both pieces of Phase 7
  infrastructure are built and tested together rather than retrofitted
  later.

- **Issue #13 (`src/config.py`: `load_llm_config()` + `llm:` config wiring)
  — done.** Implemented in `src/config.py`, tested in `tests/test_config.py`
  (6 new tmp_path-backed cases, all passing — same fully-optional
  file/section/key fallback shape as `load_mathpix_polling_config`'s
  existing tests). Conventions established/extended here:
  - `load_llm_config(config_path=None) -> LLMConfig` mirrors
    `load_mathpix_polling_config()`'s fully-optional fallback pattern, not
    `load_paths_config()`'s required/raising one — every field here
    (`model`, `prompt_version`, `min_length_ratio`, `max_length_ratio`) has
    a sensible hardcoded default, so a missing `config.yaml`, missing
    `llm:` section, missing `validation:` subsection, or missing individual
    keys never raises `ConfigError`; each falls back independently.
  - `LLMConfig` is a new frozen dataclass (`model`, `prompt_version`,
    `min_length_ratio`, `max_length_ratio`), matching the
    `MathpixPollingConfig`/`PathsConfig` convention already in the module.
  - New module constants: `DEFAULT_LLM_MODEL`
    (`claude-haiku-4-5-20251001`), `DEFAULT_PROMPT_VERSION`
    (`cleanup_v1`), `DEFAULT_MIN_LENGTH_RATIO` (0.70),
    `DEFAULT_MAX_LENGTH_RATIO` (1.30).
  - `config.yaml` and `config.example.yaml` both gained a new `llm:`
    section (`model`, `prompt_version`, nested `validation:` with
    `min_length_ratio`/`max_length_ratio`) matching these defaults.
  - `environment.yml` gained `conda-forge::litellm`, installed for real via
    `conda install -n notex -c conda-forge litellm` (v1.90.2) per AGENTS.md's
    conda-only rule.
  - `.env.example` and the real (gitignored) `.env` both gained an
    `ANTHROPIC_API_KEY=` line, matching the existing
    `MATHPIX_APP_ID`/`MATHPIX_APP_KEY` template convention — the real `.env`
    entry is left blank for the user to fill in once a key is available.
  - **Deliberately no credential-loading logic here** (e.g. no
    `load_llm_credentials()` reading `ANTHROPIC_API_KEY`) — `litellm` reads
    `ANTHROPIC_API_KEY` from the environment itself when invoked, and
    wiring/validating that key is `LLMClient`'s concern (issue #15), not
    this config-loading module's.

- **Issue #14 (`prompts/cleanup_v1.txt`: system prompt content) — done.**
  Content-only issue, no code changes. `prompts/` didn't exist yet and was
  created for this file, matching the `prompts/{prompt_version}.txt`
  loading convention `load_llm_config()` (#13) and `load_prompt_text()`
  (#15) rely on. Conventions/decisions made writing the prompt text,
  confirmed with the user:
  - **Plain-prose system-message format**, not nested Markdown
    headers/numbered lists — matches the `litellm`/chat-completion system
    message convention and stays easy to hand-edit for future prompt
    iteration (#19's smoke testing).
  - **The domain-vocabulary hint is worded fully generically** — no
    concrete example baked in (e.g. no literal "parity"→"party" mention)
    — so the prompt doesn't overfit to one course's vocabulary; it
    instead describes the *pattern* (a technical term systematically
    misread as an unrelated common word) and instructs the LLM to correct
    it consistently everywhere it recurs in the document, not just on
    first occurrence.
  - **Hard-constraint wording was tightened to avoid a self-contradiction**
    caught in review: an earlier draft's "must not change mathematical
    content inside LaTeX delimiters" rule read as forbidding the very
    LaTeX-typo-fixing and bra-ket-macro-rewriting the prompt also
    instructs the LLM to do. The final wording distinguishes changing
    mathematical *meaning* (numbers/variables/operators/symbols — always
    forbidden) from syntax/formatting fixes that preserve meaning
    (LaTeX command typo fixes, bra-ket macro rewrites — explicitly
    allowed), so the two sections no longer conflict.
  - **Bra-ket rewrite uses `\braket{x|y}`, not `\braket{x}{y}`** — a
    single-argument-with-pipe form, per user correction during review
    (not the two-argument macro signature originally drafted).
  - **New "worked examples" section, not in the original issue scope but
    added per user request during review:** handwritten notes sometimes
    label a worked example inline as `ex)`/`ex:` followed immediately by
    the example content on the same line; the prompt now instructs the
    LLM to place the `ex)`/`ex:` heading on its own line and the example
    content on the following line(s), without altering the content
    itself. This is a formatting-only reflow (heading/content line
    placement), consistent with the "syntax/formatting fixes are allowed,
    meaning-changing edits are not" distinction above.
  - The stray-heading-artifact permission (dropping exactly one
    non-structural date/title-line heading at the very start of the
    document) is scoped tightly — explicitly forbids touching any other
    heading — so it supports #16's relaxed heading-count validation
    (fails only on an *increase* in heading count) without opening the
    door to broader heading restructuring.
  - Not yet validated against real cached `.mathpix.md` output — that
    happens via #19's smoke test script, once #15-#19 land. This issue's
    scope is content-only, per the issue body.

- **Issue #15 (`src/llm.py`: `LLMClient` + prompt loading) — done.**
  Implemented in `src/llm.py`, tested in `tests/test_llm.py` (11
  fake-`completion_fn` cases, no network, all passing — full suite is 92
  passing). Conventions established here that later Phase 3 issues (#16,
  #17) should follow:
  - `LLMClient(model, completion_fn=None)` mirrors `MathpixClient`'s
    `http_client=` injection pattern precedent (issue #1):
    `completion_fn` defaults to `litellm.completion` itself when omitted,
    and tests always inject a fake, never hitting a real API.
  - `LLMClient.complete(system_prompt, user_content) -> str` is the one
    method this issue adds beyond the constructor (not specified in the
    issue body itself, decided during planning): builds the two-message
    `[{"role": "system", ...}, {"role": "user", ...}]` list internally so
    `cleanup_pdf()` (#17) only ever needs to pass prompt text + raw
    Mathpix markdown, not construct a messages list itself. Wraps any
    `completion_fn` exception, any response missing the expected
    `response.choices[0].message.content` shape, and empty/whitespace-only
    content all into `LLMError` — `complete()` never returns anything
    other than non-blank completion text.
  - **`LLMClient.__init__` calls `load_dotenv()` unconditionally** — a
    deliberate, explicitly-confirmed addition beyond the issue's literal
    text, closing a real gap: `load_dotenv()` was previously only called
    inside `load_mathpix_credentials()` (`src/config.py`), so a run that
    touches only the LLM stage for a file (e.g. once #17/#18's
    `force_llm`/`needs_llm_reprocessing()` land and a file's Mathpix stage
    is already cached/unchanged) could otherwise never load `.env` at all
    in that process, leaving `ANTHROPIC_API_KEY` unset even though it's
    present in `.env`. This does **not** read or validate the key itself
    (still `litellm`'s job, consistent with `config.py`'s "no
    credential-loading logic here" note for issue #13) — a still-missing
    key surfaces as whatever exception `completion_fn` raises, caught and
    wrapped into `LLMError` by `complete()`. No `env_file=` param was
    added (constructor signature stays exactly `LLMClient(model,
    completion_fn=None)` per the issue); `load_dotenv()` uses its default
    upward-search behavior, matching the project's "run the CLI from the
    repo root" convention already established for `DEFAULT_CONFIG_PATH`.
  - No context manager / `close()` on `LLMClient` — unlike
    `MathpixClient` (owns an `httpx.Client` connection), it wraps a
    stateless function call with nothing to clean up.
  - `load_prompt_text(prompt_version, prompts_dir=Path("prompts"))` reads
    `prompts/{prompt_version}.txt` and returns its contents verbatim (no
    `.strip()`) via `.read_text(encoding="utf-8")`, raising `LLMError` (not
    `FileNotFoundError`) when the file doesn't exist — matches the issue's
    explicit requirement that a configured `prompt_version` with no
    matching file is a real config error, not silently ignorable.
  - `ANTHROPIC_API_KEY` is confirmed already populated in the real
    (gitignored) `.env`, added back in issue #13 alongside the existing
    Mathpix credential lines — no further `.env`/`.env.example`/
    `environment.yml` changes were needed for this issue.

- **Issue #16 (`src/llm.py`: `validate_cleanup()`) — done.** Implemented in
  `src/llm.py`, tested in `tests/test_llm.py` (12 new pure-string cases —
  one pass + one fail per check, plus the relaxed-heading-decrease and
  empty-original edge cases — no network, full suite is 104 passing).
  Conventions established/extended here:
  - `ValidationResult` is a new frozen dataclass (`passed: bool`,
    `checks: dict[str, bool]`), matching the `StateEntry`/`ProcessResult`
    frozen-dataclass convention elsewhere in the codebase. `passed` is
    simply `all(checks.values())`.
  - `validate_cleanup(original, cleaned, min_length_ratio,
    max_length_ratio) -> ValidationResult` is a pure function — no I/O,
    no config/client access — matching the issue's exact signature.
  - **`dollar_balance` and `left_right_balance` are checked on `cleaned`
    only**, not compared against `original` — deliberate, since the issue
    body doesn't reference `original` for either of these two checks (unlike
    `length_ratio`/`heading_count`, which explicitly compare both). The
    question being asked is "is the LLM's output internally well-formed,"
    not "does its balance match the original's."
  - `dollar_balance` is a single even/odd check on the total count of `$`
    characters in `cleaned` (`cleaned.count("$") % 2 == 0`) — covers both
    `$...$` and `$$ ... $$` simultaneously, since `$$` is just two adjacent
    `$` chars, per the issue body's explicit guidance.
  - `left_right_balance` counts `\left`/`\right` via a
    word-boundary-aware regex (`\\left(?![a-zA-Z])` /
    `\\right(?![a-zA-Z])`), **not** a naive substring count — a naive count
    would misattribute `\rightarrow`/`\leftarrow`/`\leftrightarrow` (which
    start with `\right`/`\left` as a literal prefix but aren't the
    delimiter command) as delimiter occurrences. Count-only balance (not
    delimiter-*type* pairing), per AGENTS.md's Smoke test findings on why
    type-matching isn't statically checkable.
  - `heading_count` uses a simple per-line ATX regex (`^#{1,6}\s`,
    `re.MULTILINE`) on both `original` and `cleaned`, and is relaxed per
    the issue: fails only if `cleaned` has *more* headings than `original`
    (equal or fewer both pass), so prompts/cleanup_v1.txt's permitted
    single-stray-heading removal doesn't fail validation.
  - **`length_ratio`'s empty-`original` edge case is handled explicitly**
    (not in the issue body, decided during implementation) to avoid a
    `ZeroDivisionError`: an empty `original` passes the length_ratio check
    only if `cleaned` is also empty — this shouldn't occur in practice
    (cached Mathpix output is never actually empty) but the function must
    not crash on it.

- **Issue #17 (`src/llm.py`: `cleanup_pdf()` orchestration + fallback +
  `needs_llm_reprocessing()`) — done.** Implemented in `src/llm.py`, tested
  in `tests/test_llm.py` (9 new cases — success path, fallback on
  `LLMError`, fallback on failed validation, `FileNotFoundError`/missing-
  prompt-file propagation, default-client construction, and
  `needs_llm_reprocessing()`'s truth table — no network, full suite is 114
  passing). Design decisions confirmed with the user before implementation:
  - **`LLMResult` is a new frozen dataclass** (`llm_model`,
    `llm_prompt_version`, `llm_status` [`"success"`/`"failed"` — only two
    values, not docs/spec.md's three-value `success`/`failed`/`skipped`;
    the fallback case is still `"failed"`, not a separate `"skipped"`],
    `llm_validation_result`, `output_path`, `processed_at`). Unlike
    `src/mathpix.py`'s `ProcessResult`, `LLMResult` is returned on **both**
    success and failure — `cleanup_pdf()` never raises for an LLM API
    failure or a failed validation check, since the fallback-to-raw-output
    behavior is intrinsic to the stage per docs/spec.md, not left to the
    caller to catch.
  - **On fallback (whether from an `LLMError` or a failed
    `validate_cleanup()` check), `output_path` points directly at the
    existing `mathpix_markdown_path`** — no new file is written, no copy
    is made. `dest_dir` is left completely untouched in the failure case
    (not even created).
  - **On fallback, `llm_model` and `llm_prompt_version` are both `None`**,
    not the attempted model/prompt_version — confirmed with the user.
    Rationale: AGENTS.md's own stated invariant is that state.db's
    `llm_prompt_version` "always records whichever version actually
    produced that row's currently-stored output," and on fallback the
    stored output is the untouched raw Mathpix Markdown, produced by no
    model/prompt at all.
  - **`llm_validation_result` is `None` only when the failure is an
    `LLMError`** (the completion call itself failed, so `validate_cleanup()`
    never ran — nothing meaningful to serialize). When validation *does*
    run and fails, `llm_validation_result` is still populated with
    `json.dumps(validation_result.checks)` (the failing checks dict), for
    debugging visibility into which specific check(s) failed — confirmed
    with the user as a deliberate asymmetry from the `LLMError` case.
  - **`FileNotFoundError` (missing `mathpix_markdown_path`) and `LLMError`
    from `load_prompt_text()` (missing `prompts/{prompt_version}.txt`)
    both propagate rather than being caught into the fallback** —
    confirmed with the user. These are setup/config errors, not per-file
    LLM failures: a missing cached Mathpix file means the Mathpix stage
    should have run first (mirrors `process_pdf()`'s own
    `FileNotFoundError` propagation for a missing input PDF), and a
    missing prompt file for a configured `prompt_version` is "a real
    config error, not silently ignorable" per `load_prompt_text()`'s own
    docstring (issue #15). Only the LLM completion call and the
    post-cleanup validation are covered by the fallback-to-raw behavior.
  - `cleanup_pdf(mathpix_markdown_path, dest_dir, lecture_stem, llm_config,
    client=None)` matches the issue's exact signature. `client:
    LLMClient | None = None` mirrors `MathpixClient`'s `client=`/
    `http_client=` injection precedent: when omitted, `cleanup_pdf()`
    constructs its own `LLMClient(model=llm_config.model)`. Unlike
    `MathpixClient`, there's no ownership/`close()` bookkeeping needed —
    `LLMClient` owns no closable resources (see issue #15's notes).
  - `needs_llm_reprocessing(entry: StateEntry) -> bool` is exactly
    `entry.llm_status is None or entry.llm_status == "failed"` — never
    compares `entry.llm_prompt_version` against the currently configured
    `llm.prompt_version`, per the issue's explicit requirement and
    AGENTS.md's "Deliberate correction to docs/spec.md's Reprocessing
    logic table" above. Verified directly in
    `test_needs_llm_reprocessing_false_when_successful_with_stale_prompt_version`.
    Lives in `src/llm.py`, not `src/discovery.py` — a separate concern
    from `classify_pdf()`'s Mathpix-stage-only change detection (see issue
    #15/#16 notes and `discovery.py`'s own docstring).

- **Issue #18 (`src/main.py`: wire LLM stage into `run()`, `force_llm` +
  `target_source_path` infra) — done.** Implemented in `src/main.py`,
  tested in `tests/test_main.py` (12 cases total — 3 existing Phase 2 tests
  extended to also cover the LLM stage, 9 new — no network for the LLM
  side: `src.main.cleanup_pdf` is monkeypatched to a fake, never
  `litellm.completion`/a real API; full suite is 119 passing). Design
  decisions confirmed with the user before implementation:
  - **The per-file processing body is factored into a new private
    `_process_file(result, cache_dir, mathpix_client, llm_client,
    llm_config, conn, force_llm, course_label) -> _FileOutcome` helper**,
    called identically by the normal per-course loop and the
    `target_source_path` branch — per the issue's explicit requirement,
    so the two entry points can't drift apart. `_FileOutcome` is a small
    frozen dataclass (`processed`/`skipped`/`errors`/`llm_reprocessed`,
    all defaulting to `0`) that `run()` accumulates into its final
    `RunSummary`. The **ungrouped-skip decision itself stays outside**
    this shared helper (it lives in each branch's own dispatch — see
    below) since the issue scopes the shared helper to "mathpix stage
    handling, LLM stage handling, upsert_entry() calls" specifically, not
    course/ungrouped resolution.
  - **LLM output lands in the same per-course `cache_dir` as the Mathpix
    stage**, not a separate `llm/` subfolder — confirmed with the user.
    `cleanup_pdf()`'s `dest_dir` is passed through as exactly the same
    `paths_config.cache_dir / course` used for `process_pdf()`, so
    `{stem}.llm.md` sits alongside `{stem}.mathpix.md`. Keeps Phase 3
    scope to "cache only" per AGENTS.md — no vault-facing directory
    structure decisions are made here (Phase 4/5).
  - **For the UNCHANGED-file LLM-only-rerun path, `mathpix_markdown_path`
    is derived by naming convention** (`cache_dir / f"{lecture_stem}.mathpix.md"`),
    not read from a `ProcessResult` (there isn't one — Mathpix didn't run
    this pass). Relies on `fetch_and_extract()`'s (issue #3) deterministic
    naming convention holding.
  - **`RunSummary.errors` counts LLM fallbacks too, not just Mathpix
    failures** — confirmed with the user (a deliberate broadening of the
    field's original Phase 2 meaning). Any `cleanup_pdf()` call this run
    that returns `llm_status == "failed"` (whether from the actionable
    NEW/CHANGED/RETRY path or the UNCHANGED-file `llm_reprocessed` path)
    increments `errors` by 1, in addition to whatever else that call
    already counts (`processed`/`llm_reprocessed`) — i.e. a single file
    can contribute to both `processed` (or `llm_reprocessed`) *and*
    `errors` in the same run if its Mathpix stage succeeded but its LLM
    stage fell back to raw output. This is a real deviation from Phase
    2's original "errors == Mathpix API failures only" meaning, made
    explicitly rather than assumed.
  - **`target_source_path` resolving to an ungrouped file (no course
    subfolder) is force-processed, not skipped** — a deliberate asymmetry
    from the normal-run ungrouped-skip behavior (issue #11), confirmed
    with the user: since the caller explicitly named this exact file by
    path, there's no ambiguity about intent the way there is for a stray
    file discovered incidentally during a full `discover_pdfs()` walk.
    Its cache dir is a new reserved sentinel subfolder,
    `paths_config.cache_dir / "_ungrouped"` (module constant
    `_UNGROUPED_CACHE_SUBDIR` in `src/main.py`) — mirrors
    `discovery.py`'s `UNGROUPED_COURSE_KEY` sentinel's *role* but is a
    real filesystem folder name (unlike `UNGROUPED_COURSE_KEY`, which is
    `""` and is never used as a path component), since this code path
    actually does write cache/state for the file rather than just
    warning and skipping it.
  - **`cleanup_pdf()`'s two possible raised exceptions are handled
    differently in the UNCHANGED-file LLM-only-rerun path** — confirmed
    with the user: a `FileNotFoundError` (the cached `.mathpix.md`
    unexpectedly missing, e.g. `_cache` manually cleared) is caught
    per-file, logged, and counted as `errors += 1` without aborting the
    run — a filesystem hiccup local to one file. An `LLMError` from a
    missing `prompts/{prompt_version}.txt` is **not** caught — it
    propagates out of `run()` (and crashes `main()`) — since a missing
    configured prompt file is a global config problem that would fail
    identically for every remaining file this run, not a per-file
    condition worth silently degrading through. (Both exceptions are
    structurally impossible on the actionable NEW/CHANGED/RETRY path,
    since `mathpix_markdown_path` there is `process_result.markdown_path`,
    a file `process_pdf()` just created moments earlier.)
  - **Testing seam: `src.main.cleanup_pdf` is monkeypatched directly**,
    rather than adding a new `llm_client=`/`completion_fn=`-style
    injection parameter to `run()` — confirmed with the user, since the
    issue's exact `run()` signature has no such parameter (only
    `llm_config`, not an injectable client/callable). Mirrors the
    existing convention of monkeypatching `src.main.run` itself for
    `main()`'s tests. This means `run()` still unconditionally
    constructs a real `LLMClient(model=llm_config.model)` every call
    (per the issue's "one `LLMClient` per run" requirement) even in
    tests, but since `LLMClient.__init__` only calls `load_dotenv()` (no
    API call — see issue #15), this is harmless even without a real
    `ANTHROPIC_API_KEY` configured.
  - Three existing Phase 2 tests
    (`test_run_processes_new_file_and_records_success`,
    `test_run_continues_after_one_file_fails`,
    `test_run_second_pass_is_full_noop`) now also install the fake
    `cleanup_pdf` and assert on the new `llm_*`/`output_path` state.db
    columns, since every actionable-file success now triggers the LLM
    stage automatically. `test_run_skips_unchanged_file_without_calling_process_pdf`
    was renamed `test_run_skips_unchanged_and_current_file_entirely` and
    its seeded state.db row now explicitly sets `llm_status="success"`
    (previously unset) so it still exercises a full skip rather than
    incidentally becoming the new stale-LLM-reprocessing case.

- **Issue #19 (`scripts/smoke_test_llm.py`: manual real-API prompt-iteration
  script) — done.** Implemented in `scripts/smoke_test_llm.py`. Not under
  `pytest` (manual, real-API, per Testing Conventions); manually run against
  the real Anthropic API on both of `_cache/class_1`'s cached
  `.mathpix.md` files. Design decisions confirmed with the user before
  implementation:
  - **Calls `LLMClient.complete()` + `validate_cleanup()` directly, not
    `cleanup_pdf()`** — a deliberate departure from the issue text's
    ambiguous "Calls `cleanup_pdf()`/`LLMClient` directly" phrasing,
    confirmed with the user. `cleanup_pdf()` discards the cleaned text
    entirely on a failed `validate_cleanup()` check (by design, for the
    pipeline's fallback-to-raw-output behavior — issue #17), which would
    hide the single most useful case for prompt iteration: seeing *why* a
    cleanup attempt failed validation. Calling `LLMClient`/`validate_cleanup()`
    directly means the script always has the cleaned text in hand
    regardless of pass/fail.
  - CLI: positional `mathpix_md_path` (the cached `.mathpix.md` file to
    clean up); `--prompt-version` (optional override of
    `load_llm_config()`'s configured `prompt_version` for that one run,
    without touching `config.yaml` — confirmed with the user as a useful
    addition for quick side-by-side prompt comparisons); `--out` (optional,
    **no default path** — confirmed with the user specifically to avoid a
    routine run silently overwriting a previously-successful `.llm.md` the
    real pipeline wrote, or leaving a validation-failing file sitting in
    the cache dir with no `state.db` record explaining it isn't the "real"
    output). Model is not overridable — always uses the configured
    `llm.model`.
  - **When `--out` is given, the cleaned Markdown is written to that file
    and NOT also dumped to stdout** (confirmed with the user) — only the
    validation summary prints in that case. Without `--out`, the cleaned
    Markdown is printed in full to stdout under a `--- CLEANED OUTPUT ---`
    separator.
  - Validation summary (always printed, regardless of `--out`): model,
    prompt_version actually used, original/cleaned char counts + length
    ratio, each of `validate_cleanup()`'s 4 checks individually
    (pass/fail), and an overall passed/failed line. The script's exit code
    is `0` even when validation fails (it reports results; it isn't a
    pass/fail gate) — only a missing input file, `ConfigError`, or
    `LLMError` (missing prompt file / completion failure) causes a
    non-zero exit, mirroring `smoke_test_mathpix.py`'s exact exception
    tuple (`ConfigError`/`LLMError`/`FileNotFoundError`).
  - Does not touch `state.db` at all, and never imports `src.state` —
    confirmed as a hard requirement per the issue text.
  - **Real-API run findings (against `_cache/class_1/lecture_02.mathpix.md`
    in particular) drove three `prompts/cleanup_v1.txt` prompt fixes**,
    made as part of this issue's real-API iteration work (the actual point
    of the script):
    1. **Bra-ket closing-delimiter bug**: the model was rewriting
       `|1,0,0\rangle` into the mismatched `\ket{1,0,0\rangle` (closing
       with `\rangle` instead of `}`) in several places (`g.s.
       $\ket{100\rangle=\ket{1,0,0\rangle$`, `\ket{2,0,0\rangle`,
       `\ket{n=2, l=1\rangle`, etc.) — syntactically broken LaTeX that
       `validate_cleanup()`'s count-only `\left`/`\right` check can't catch
       (it isn't a `\left`/`\right` delimiter at all) and that the
       `dollar_balance` check also can't catch (the `$` count is still
       even). Fixed by adding an explicit "Critical — closing delimiter"
       subsection to the bra-ket notation guidance in
       `prompts/cleanup_v1.txt`, spelling out that `\ket{}`/`\bra{}`/
       `\braket{}` arguments are delimited only by `{`/`}` (never
       `\rangle`/`\langle`), with side-by-side correct/incorrect examples
       matching the exact observed failure pattern (including multi-token
       arguments with commas/`=` signs) and an explicit self-check
       instruction.
    2. **Figure references appearing inline**: added a new "Figure
       references" rule instructing the LLM to always place Markdown image
       references (`![](figures/...)`) on their own line, reflowing an
       inline one onto its own line (preserving reading order) without
       altering the path/alt text — same formatting-only-reflow shape as
       the existing worked-examples rule.
    3. **Missing sentence-ending punctuation after inline math**: added a
       new "Sentence-ending punctuation after inline math" rule — when
       text immediately following the closing `$` of an inline equation
       looks like the start of a new sentence, insert a period right
       after the `$` (in the surrounding prose, never inside the math
       delimiters), with an explicit caution against doing this after
       every inline equation indiscriminately.
    All three are prompt-content-only changes (no code touched), following
    issue #14's precedent that `prompts/cleanup_v1.txt` changes don't
    require `prompt_version` to be bumped to a new file unless the user
    wants old and new prompt behavior to coexist side by side — this
    round of fixes was applied in place to `cleanup_v1.txt` per user
    direction. Full `pytest` suite re-confirmed passing (119 passed) after
    each prompt edit, since none of them touch code.
  - Not yet exercised: `--prompt-version`'s override path in a real run
    (tested only via `--help`/argument-parsing sanity, not against a real
    second prompt file) — no side-by-side `cleanup_v2.txt` comparison was
    actually run this issue. Revisit if/when a real prompt fork is needed.

- **Issue #20 (real-data validation: LLM stage idempotency + forced
  reprocessing) — done.** `src/main.py`'s `run()` was exercised for real
  (`python -m src.main`, plus two throwaway `python -c` calls for the
  `force_llm=True`/`target_source_path` cases, per the issue's own "ad hoc
  script/REPL" wording — no new source file was added) against
  `notes_raw/class_1`'s two PDFs, covering every bullet in the issue body.
  `state.db` and `_cache/class_1/` were reset (deleted) first so the cold
  run below is a genuine from-scratch run through both stages together,
  not a reuse of Phase 2 issue #12's already-`mathpix_status=success` rows.
  Findings:
  - **Cold run** (`python -m src.main`, no flags): both files classified
    `new` and processed through Mathpix + LLM successfully in one pass —
    console summary `Processed: 2, Skipped: 0, Errors: 0, Ungrouped: 0,
    LLM reprocessed: 0`. `state.db` got full `llm_*` columns for both rows:
    `llm_status="success"`, `llm_model="claude-haiku-4-5-20251001"`,
    `llm_prompt_version="cleanup_v1"`, `llm_validation_result` with all
    four checks (`length_ratio`/`dollar_balance`/`left_right_balance`/
    `heading_count`) `true`, `output_path` pointing at the real
    `_cache/class_1/{stem}.llm.md`, and `llm_processed_at` a few seconds
    after `mathpix_processed_at`, confirming the combined
    Mathpix-then-LLM `_process_file()` path (src/main.py:193-244) runs
    end to end against the real APIs.
  - **Immediate rerun**: true no-op — `Processed: 0, Skipped: 2` (no
    `llm_reprocessed`). Confirmed at the data level, not just the printed
    summary: every `state.db` column was byte-identical to the cold run
    (verified via a `sort_keys` JSON diff of both full rows), and every
    `_cache/class_1/*.md` file's mtime was untouched. Extends Phase 2
    issue #12's idempotent-rerun confirmation to the LLM stage.
  - **Prompt-version bump, no CLI flag**: `prompts/cleanup_v1.txt` was
    copied verbatim to a throwaway `prompts/cleanup_v2.txt`, and
    `config.yaml`'s `llm.prompt_version` was changed to `cleanup_v2`
    (both reverted after the test — `cleanup_v2.txt` was deleted, and
    `config.yaml` restored to `cleanup_v1`, matching the trivial/throwaway
    treatment decided before starting). A plain rerun of `python -m
    src.main` afterward was still a full no-op (`Skipped: 2`) — confirms
    `needs_llm_reprocessing()`'s deliberate correction to docs/spec.md
    (see "Current Phase" above): `state.db`'s `llm_prompt_version`
    stayed `cleanup_v1` for both rows even with `config.yaml` pointed at
    `cleanup_v2`, i.e. no silent mass reprocessing on a prompt-version
    edit.
  - **`force_llm=True`** (via a throwaway `python -c` snippet calling
    `run(paths_config, conn, force_llm=True)` directly, config.yaml still
    on `cleanup_v2` at this point): both files' LLM stage reran —
    `RunSummary(llm_reprocessed=2)` — and `state.db`'s
    `llm_prompt_version` updated to `cleanup_v2` for both rows, with
    `llm_processed_at` advancing past the cold-run/no-op timestamps.
    Confirms the explicit-opt-in path is the only way to pick up a new
    prompt version, exactly as designed.
  - **`target_source_path` + `force_llm=True`** (throwaway `python -c`
    call scoped to just `lecture_01.pdf`): `RunSummary(llm_reprocessed=1)`
    — only `lecture_01`'s row changed (`llm_processed_at` advanced again);
    `lecture_02`'s row was verified byte-identical (full JSON diff) to its
    state from the previous `force_llm=True` call, confirming the
    single-file-rerun infrastructure doesn't touch any other file's state.
  - **Cleanup after testing**: `config.yaml` reverted to
    `llm.prompt_version: cleanup_v1` and `prompts/cleanup_v2.txt` deleted;
    one final `run(..., force_llm=True)` call was made to bring both
    files' stored `llm_prompt_version` back to `cleanup_v1` for
    consistency (otherwise they'd have been stuck reading `cleanup_v2` in
    `state.db` despite `config.yaml` now saying `cleanup_v1`, since
    `needs_llm_reprocessing()` never triggers on that mismatch alone — see
    above). A final plain rerun confirmed a full no-op again.
  - **LLM output quality, read from the real `_cache/class_1/*.llm.md`
    files produced by this run (not `scripts/smoke_test_llm.py` — these
    went through the real `cleanup_pdf()`/`run()` pipeline):** the
    systematic OCR-misread fixes documented in Phase 1's smoke test
    findings are confirmed working in the actual pipeline, not just
    `smoke_test_llm.py`'s manual iteration — e.g. "potentral"→potential,
    "betore"→before, "initrally mstate"→initially in state, "regnore"→
    ignore, "Persubation"→Perturbation, "party"/"porty"→parity (all 9+
    occurrences), "ergenstate"/"e.jerstate"→eigenstate, "Ingencoal"→In
    general, "degreesate"→degenerate. Bra-ket macro normalization
    (`\ket{}`) was applied to several kets (`\ket{n}`, `\ket{0}`,
    `\ket{1}`, `\ket{2,0,0}`, `\ket{\alpha}`, `\ket{\beta}`,
    `\ket{\psi}`) without the earlier `\rangle`-closing-delimiter bug
    (issue #19's fix held up on this file). The previously-documented
    "quietly dangerous" `\ln`→"in" misread (AGENTS.md's Smoke test
    findings: handwritten "in" OCR'd as the LaTeX command `\ln`) was
    correctly caught and fixed this run (`g.s. $\ln |x\rangle=...$` →
    `g.s. in $|x\rangle$: ...`).
  - **New finding — the bra-ket macro rewrite is applied inconsistently
    within a single document.** Several other raw-notation kets in the
    same `lecture_02.llm.md` output were left unconverted alongside the
    ones that were rewritten: `\langle\vec{x}|1,0,0\rangle` (not rewritten
    to `\braket{\vec{x}}{1,0,0}`), and `|L\rangle`/`|R\rangle`/
    `|S\rangle`/`|A\rangle` later in the same document (not rewritten to
    `\ket{L}`/`\ket{R}`/`\ket{S}`/`\ket{A}`) even though `\ket{}` was used
    for other states earlier in the identical file. Not a validation
    failure (nothing here is syntactically broken), just an inconsistency
    in how thoroughly the prompt's bra-ket normalization instruction gets
    applied — worth keeping in mind if bra-ket consistency ever becomes a
    hard requirement (e.g. for a future MathJax/KaTeX macro-based vault
    render), but out of scope to fix here (would be further
    `prompts/cleanup_v1.txt` prompt-tuning, issue #19's territory, not
    #20's validation-only scope).
  - **New finding — the LLM can confidently substitute a wrong specific
    term for garbled proper-noun OCR text, and no current check can catch
    it.** `lecture_02.mathpix.md`'s garbled OCR read "Mann's rule" (not a
    real term — likely a mangled reading of the actual result being
    referenced, the parity selection rule for electric dipole
    transitions, properly named "Laporte's rule" — confirmed via a web
    search during this issue's writeup, not prior domain knowledge baked
    into the prompt). The LLM's cleanup output confidently rewrote this to
    "**Wigner's rule**" — a real, specific, but *wrong* physics eponym
    (Wigner is associated with parity's quantum-mechanical justification
    per the historical record, but the rule being described here is
    Laporte's, not Wigner's). This is a new, more dangerous variant of the
    "quietly dangerous" misread risk already documented in AGENTS.md's
    Smoke test findings section (the `\ln` case): there, the LLM produced
    syntactically-valid-but-wrong LaTeX; here, it produces
    grammatically-fine, plausible-sounding, *specifically wrong* prose
    that reads as confidently correct to a non-expert. `validate_cleanup()`
    cannot catch this by construction (it's not a length/delimiter/heading
    problem), and no prompt wording currently guards against it. Flagged
    here as a known limitation rather than fixed — addressing it (e.g.
    prompt wording that encourages leaving genuinely ambiguous
    proper-noun/eponym OCR garble as-is, or flagged, rather than
    resolving it to a specific guess) would be new `prompts/cleanup_v1.txt`
    prompt-design work, out of scope for this validation-only issue.
  - No code changes were needed — every mechanism (`needs_llm_reprocessing()`,
    `force_llm`, `target_source_path`, the combined Mathpix+LLM
    `_process_file()` path) behaved exactly as designed and documented in
    issues #13-#18's entries above.

- **Issue #21 (LLM token usage + cost estimate tracking in `state.db`) —
  done.** A `phase-3`-labeled followup opened after Phase 3 was already
  validated/closed (#13-#20) — not part of the original Phase 3 scope, but
  small enough to land without reopening the phase. Implemented across
  `src/llm.py`, `src/state.py`, `src/main.py`, and
  `scripts/smoke_test_llm.py`; tested in `tests/test_llm.py` (5 new/updated
  cases), `tests/test_state.py` (1 new case), and `tests/test_main.py`
  (existing cases extended with token/cost assertions) — no network, full
  suite is 123 passing. Implementation matched the issue body's
  pre-confirmed design decisions exactly, no further decisions needed
  during implementation:
  - **`LLMClient.complete()`'s return type changed from `str` to a new
    frozen dataclass `CompletionResult`** (`content`, `input_tokens`,
    `output_tokens`, `cost`) — a breaking change to `complete()`'s return
    shape, updated at every call site (`cleanup_pdf()`,
    `scripts/smoke_test_llm.py`, every `tests/test_llm.py` case that
    previously asserted a bare string). `input_tokens`/`output_tokens` are
    read directly from the real `litellm.completion()` response's
    `response.usage.prompt_tokens`/`.completion_tokens` — exact/billed
    figures, not a `litellm.token_counter()` re-tokenization pass. `cost`
    comes from `litellm.completion_cost(completion_response=response,
    model=self.model)`. Both usage reading and cost lookup are
    independently best-effort: a response lacking a usable `usage`
    attribute (caught via `AttributeError`) falls back to
    `input_tokens=None, output_tokens=None`, and any exception from
    `completion_cost()` (confirmed locally: it raises
    `litellm.BadRequestError` for a model litellm doesn't recognize, e.g.
    the tests' `"fake-model"`) falls back to `cost=None` — neither ever
    raises `LLMError`, since a missing/unpriceable model shouldn't break
    the actual cleanup call that already succeeded.
  - **`LLMResult` gained three new optional fields**
    (`llm_input_tokens`/`llm_output_tokens`/`llm_cost_estimate`, all
    defaulting to `None` so the dataclass stays backward compatible with
    any positional-arg construction elsewhere), populated from the
    `CompletionResult` on both the success path and the
    validation-failure fallback path (the completion call still happened
    and cost real money even though the cleaned output was discarded —
    mirrors the existing asymmetric treatment of `llm_validation_result`).
    On the `LLMError` fallback (no completion ever returned), all three
    stay `None` — there's no usage to report.
  - **`state.db` gained three new nullable columns**
    (`llm_input_tokens INTEGER`, `llm_output_tokens INTEGER`,
    `llm_cost_estimate REAL`), added to
    `_VALUE_COLUMNS`, `_CREATE_TABLE_SQL`, and `StateEntry`, following the
    exact same partial-upsert convention as every other column (verified
    directly in a new
    `test_upsert_entry_partial_update_preserves_token_and_cost_columns`
    case). **No schema-migration logic was added** — `init_db()` still
    only ever runs `CREATE TABLE IF NOT EXISTS`, per the issue's explicit
    design decision. The real (gitignored) local `state.db`, which
    predated these columns from issue #20's live validation run, was
    deleted as the one-time manual step the issue calls for — it rebuilds
    cleanly on the next real run since `notes_raw/class_1` is only two
    lecture PDFs.
  - **`src/main.py`'s `RunSummary` gained `total_input_tokens`,
    `total_output_tokens`, `total_cost_estimate`** (all defaulting to
    `0`/`0`/`0.0`), accumulated via a matching set of new fields on the
    internal `_FileOutcome` helper. Both of `_process_file()`'s
    `upsert_entry()` call sites (the actionable NEW/CHANGED/RETRY path and
    the UNCHANGED-file LLM-only-rerun path) now also pass through
    `llm_input_tokens`/`llm_output_tokens`/`llm_cost_estimate` from the
    `LLMResult`, and both of `_process_file()`'s return points populate
    `_FileOutcome`'s new accumulator fields from the same `LLMResult`
    (`llm_input_tokens or 0`, etc., so a `None` from a fallen-back-to-raw
    `LLMResult` contributes `0` rather than breaking accumulation).
    `_print_summary()` prints the three new totals (`Input tokens:`,
    `Output tokens:`, `Est. cost:` formatted to 4 decimal places).
  - **`scripts/smoke_test_llm.py`** updated its one `client.complete()`
    call site for the new `CompletionResult` shape and now prints real
    input/output token counts and an estimated cost (falling back to the
    literal string `"unknown"` for any field that came back `None`)
    alongside the existing validation summary — a capability this script
    didn't have before, since token/cost capture previously didn't exist
    anywhere in the codebase.
  - Out of scope, per the issue: any aggregate/all-time cost-reporting CLI
    flag summing `state.db` across the whole vault (future Phase 7
    territory), and Mathpix-side cost tracking (page-priced, not
    token-priced, so none of this applies to that stage).
  - **Real-API validation (post-implementation, ad hoc — not a separate
    issue).** `scripts/smoke_test_llm.py` was run once for real against
    the already-cached `_cache/class_1/lecture_01.mathpix.md` (`--out`
    pointed at a throwaway directory, deleted afterward): cleanup
    succeeded, all 4 validation checks passed, and the new token/cost
    summary lines printed real figures (3133 input / 1407 output tokens,
    ~$0.0102). Separately, `state.db` (deleted per this issue's design
    decision — see above) was reseeded with matching mtime/size/hash +
    `mathpix_status="success"` for both `notes_raw/class_1` PDFs (no
    `llm_status`, so `needs_llm_reprocessing()` triggers the existing
    LLM-only-rerun path) to exercise the real `python -m src.main`
    pipeline's new token/cost wiring **without** re-incurring a real
    Mathpix API charge for OCR that was already validated in issue #20.
    The real run correctly avoided any Mathpix call, ran both files'
    LLM stage for real, and printed `Input tokens: 5729`, `Output
    tokens: 2290`, `Est. cost: $0.0172` — matching the per-file
    `llm_input_tokens`/`llm_output_tokens`/`llm_cost_estimate` values
    written to `state.db` for each row exactly (lecture_01: 3133/1387/
    $0.0101; lecture_02: 2596/903/$0.0071). An immediate second run was
    confirmed a true no-op (`Skipped: 2`, all three new totals `0`),
    matching the LLM stage's existing idempotency guarantee (issue #20)
    with the new fields in place. Total real API usage across this
    validation: 3 completions (1 smoke-test + 2 from the single real
    `src.main` run), 0 Mathpix calls.

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

- **Issue #22 (page_count tracking in state.db + run summary total) —
  VALIDATED — complete.** A `phase-1`-labeled followup opened after Phase 1
  was already validated/closed (#1-#6) —
  same shape as issue #21's followup to Phase 3. Implemented across
  `src/mathpix.py`, `src/state.py`, and `src/main.py`; tested in
  `tests/test_mathpix.py` (2 cases — happy path now asserts
  `ProcessResult.page_count`, plus a new case confirming a completed
  payload missing `num_pages` yields `page_count=None` rather than
  raising), `tests/test_state.py` (1 new dedicated partial-upsert-preserves
  case, matching the issue #21 precedent, plus the existing round-trip/
  no-optional-fields/other-columns-preserved cases extended to cover
  `page_count`), and `tests/test_main.py` (existing mocked "completed"
  payloads extended with `num_pages`, `RunSummary.total_pages_processed`
  and `StateEntry.page_count` asserted alongside the pre-existing
  token/cost assertions) — no network, full suite is 125 passing.
  Implementation matched the issue body's pre-confirmed design exactly, no
  further decisions needed during implementation:
  - **`process_pdf()` now captures `poll_until_complete()`'s return value**
    (previously discarded entirely) and reads
    `payload.get("num_pages")` into a new `ProcessResult.page_count: int |
    None` field, placed immediately alongside `figure_count` — no new API
    call, per the issue's explicit design. Best-effort: a completed
    payload unexpectedly missing `num_pages` yields `page_count=None`
    rather than raising `MathpixError`, mirroring issue #21's
    `llm_input_tokens`/`llm_output_tokens` best-effort precedent.
  - **`state.db` gained one new nullable column, `page_count INTEGER`**,
    added to `_VALUE_COLUMNS`, `_CREATE_TABLE_SQL`, and `StateEntry`
    immediately alongside `figure_count` — same "no schema-migration
    logic, delete-and-rebuild" convention as every prior column addition
    (issue #21's three columns, etc.).
  - **Only written on the actionable NEW/CHANGED/RETRY success path** in
    `_process_file()`'s `upsert_entry()` call, exactly mirroring
    `figure_count`'s existing treatment — left untouched on a Mathpix
    failure (no completed payload to read it from) and on the
    UNCHANGED-file LLM-only-rerun path (no Mathpix call happens there, so
    partial-upsert semantics mean the column just keeps whatever a prior
    successful run already wrote).
  - **`RunSummary` gained `total_pages_processed: int = 0`** — a
    **per-run** total (pages actually OCR'd *this* run only), accumulated
    via a new `_FileOutcome.pages` field populated only on the actionable
    path (`process_result.page_count or 0`) — the LLM-only-rerun path's
    `_FileOutcome` doesn't set `pages` (defaults to `0`), consistent with
    "no Mathpix call, nothing new to report" above. Flows through both the
    normal per-course loop and the `target_source_path` branch since both
    share `_process_file()`. `_print_summary()` prints a new
    `Pages processed:` line, placed directly under `Processed:` (later
    renamed `Documents processed:` — see the follow-up bullet below; a
    deliberate placement choice, confirmed with the user, since page count
    is the natural per-file-count companion metric to `Processed`, ahead
    of the token/cost lines).
  - Skipped/fully-unchanged files are deliberately *not* re-counted into
    `total_pages_processed` (their pages were already tallied whichever
    prior run actually processed them) — avoids double-counting across
    runs, same reasoning as issue #21's per-run (not all-time) token/cost
    totals.
  - **Real-API validation (per AGENTS.md's issue #12/#20 precedent) — done.**
    The local `state.db` was deleted (per the user's explicit go-ahead) and
    `python -m src.main` was run for real against `notes_raw/class_1`'s two
    PDFs (2 of a self-imposed 5-run budget for this validation session).
    **Cold run:** both files classified `new`, processed through Mathpix +
    LLM successfully — console summary `Documents processed: 2, Pages
    processed: 2, Skipped: 0, Errors: 0`. `state.db` recorded
    `page_count=1` for both rows (each source PDF is genuinely one page),
    alongside real `mathpix_pdf_id`s, correct `figure_count` (0 for
    `lecture_01.pdf`, 1 for `lecture_02.pdf`), and successful
    `llm_status="success"` with all four validation checks passing.
    **Immediate rerun** (2nd of the 5-run budget): true no-op — `Documents
    processed: 0, Pages processed: 0, Skipped: 2`, and every `state.db`
    column (including `page_count` and both `*_processed_at` timestamps)
    was confirmed byte-identical to the cold run, i.e. no re-processing,
    no re-tallying of `total_pages_processed` for already-processed files.
    This confirms `ProcessResult.page_count` reads a real `num_pages`
    value from the live Mathpix API (not just mocked test payloads) and
    that the per-run (not cumulative) `total_pages_processed` semantics
    hold against real data. Only 2 of the 5 allotted runs were used.
  - **Follow-up (post-implementation): `_print_summary()`'s `Processed:`
    label was renamed to `Documents processed:`**, at the user's request,
    to read unambiguously alongside the new `Pages processed:` line
    (`Processed` alone was ambiguous once both a document count and a page
    count appear in the same summary). All eight summary lines' label
    widths were made consistent (`f"  {{label:<21}}{{value}}"`) rather than
    hand-spaced per line, so the columns stay aligned regardless of label
    length. Only the printed label changed -- `RunSummary.processed` /
    `_FileOutcome.processed` (the field names) are untouched. Updated
    `tests/test_main.py`'s `test_main_returns_zero_and_prints_summary_even_with_errors`
    assertions to match the new label text and column widths.

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
  "Figure 1") into the alt-text slot, since empty alt text isn't very
  useful on its own. (Resolved in issue #24 — see its Phase 4 progress
  entry: implemented as a numbered caption injected into standard
  Markdown `![alt](path)` syntax, not an Obsidian wikilink rewrite.)
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
│   └── cleanup_v1.txt      ← versioned LLM system prompt (loaded by prompt_version, see src/llm.py, issue #15)
├── src/
│   ├── config.py           ← env/config loading (load_mathpix_credentials, load_mathpix_polling_config, load_paths_config, load_llm_config)
│   ├── mathpix.py          ← Mathpix API client
│   ├── state.py            ← state.db schema + CRUD (StateEntry, init_db, get_entry, upsert_entry)
│   ├── discovery.py        ← per-file two-tier change classification + recursive multi-course walk (Classification, ClassificationResult, classify_pdf, compute_sha256, discover_pdfs, UNGROUPED_COURSE_KEY)
│   ├── llm.py              ← LLM cleanup client + prompt loading + orchestration (LLMClient, LLMError, load_prompt_text, validate_cleanup, cleanup_pdf, needs_llm_reprocessing — issues #15-#17)
│   ├── main.py             ← CLI orchestration entry point (RunSummary, run, main) — wires discovery + state.db + process_pdf() into a runnable pass over input_root
│   ├── figures.py          ← figure copy-to-vault + Markdown image-reference caption rewriter (copy_figures_to_vault, rewrite_image_references — issues #23-#24)
│   ├── postprocess.py      ← [Phase 5, not yet implemented] YAML frontmatter builder + delimiter-balance warning scan
│   ├── vault.py            ← [Phase 5/6, not yet implemented] assembles + writes final per-lecture vault .md; course index generation
│   └── reporting.py        ← [Phase 7, not yet implemented] Reporter interface (PlainReporter/RichReporter) for progress UI
├── scripts/
│   ├── smoke_test_mathpix.py   ← manual, real-API Mathpix validation
│   ├── smoke_test_llm.py       ← manual, real-API LLM prompt-iteration script (issue #19)
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

Issues are tracked on GitHub from the start of the project. Code was
developed locally and held back from the `origin` remote until Phase 1 was
validated against real Mathpix output (see "Phase 1 status" above). Phase 1
is now validated, so local commits are pushed to `origin` going forward.
