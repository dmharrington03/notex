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

**Phase 1 — Core Mathpix pipeline.** Scope: given a single PDF path, submit to
Mathpix, poll to completion, retrieve Markdown + figures, write to `_cache/`.
No discovery loop, no `state.db`, no LLM stage, no vault writing yet — those
arrive in later phases (see `docs/spec.md` for the full 7-phase roadmap and
stage-by-stage detail — state.db schema, LLM prompt structure, error handling
table, CLI flags, etc. Note: follow this file (AGENTS.md), not spec.md, where
they disagree — see "Mathpix API notes" below for known corrections).

### Phase 1 progress

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
│   ├── config.py           ← env/config loading
│   └── mathpix.py          ← Mathpix API client
├── scripts/
│   └── smoke_test_mathpix.py   ← manual, real-API validation
├── tests/
│   ├── fixtures/           ← fixture data for mocked tests
│   └── test_mathpix.py
└── _cache/                 ← gitignored, created at runtime
```

## Git / Issue Tracking

Issues are tracked on GitHub from the start of the project. Code is being
developed locally and will not be pushed to the `origin` remote until Phase 1
is validated against real Mathpix output.
