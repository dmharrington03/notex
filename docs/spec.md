> **Status:** Original planning spec, kept as the full-detail historical
> reference (state.db schema, LLM prompt structure, error handling table,
> CLI flags, config.yaml schema, etc.). **`AGENTS.md` is the living, corrected
> source of truth** — where the two disagree, follow `AGENTS.md`. Known
> corrections so far: the Mathpix API section below (status values, upload
> mechanism, figure asset retrieval, math delimiters) does not match the
> real API; see `AGENTS.md` → "Mathpix API notes" for what's verified.
> Project name has also since been finalized as **NoTeX** / repo `notex`
> (this document predates that decision and refers to it as
> "notes-pipeline").

# Notes Digitization Pipeline — Implementation Specification

> Specification document compiled from design discussion.
> Purpose: guide coding and implementation of the notes digitization pipeline.

---

## Project Overview

A Python CLI tool that scans a directory of handwritten lecture note PDFs, processes
them through the Mathpix API to extract text, LaTeX equations, and figures, runs the
text output through an LLM for cleanup, and writes organized Markdown files to a
mirrored output directory suitable for use as an Obsidian vault.

The tool is run manually by the user after saving new note PDFs. It is not a daemon
or file watcher. It identifies only new (previously unseen) or changed files on each
run and skips anything already processed. It is fully idempotent.

---

## notes_raw/ — Permanent Archive

`notes_raw/` is a **permanent archive**. The script never writes to, moves, deletes,
or modifies any file within it under any circumstances. Every PDF ever processed
remains in `notes_raw/` indefinitely. The state log is the sole mechanism for
determining which files have already been processed and can be skipped.

This design choice is intentional:
- Original source files are always available for reprocessing (e.g. if the LLM
  prompt is improved, or Mathpix output quality changes)
- No risk of data loss or corruption from the script touching source files
- Cloud sync timing issues (iCloud, Dropbox) are irrelevant since files are
  never moved or deleted
- The directory can be backed up or version-controlled independently of the
  processing pipeline

The script treats `input_root` as read-only. Any future extension of the script
must preserve this invariant.

---

## Directory Structure

### Input — Raw PDFs

```
notes_raw/
├── 18.06_Linear_Algebra/
│   ├── lecture_01.pdf
│   ├── lecture_02.pdf
│   └── ...
├── 6.006_Algorithms/
│   ├── lecture_01.pdf
│   └── ...
└── 8.03_Physics/
    └── ...
```

- One subdirectory per course
- One PDF per lecture within each course directory
- PDF filenames should follow a consistent convention (see Naming Convention section)
- PDFs are high-quality digitally handwritten notes (e.g. from GoodNotes or
  Notability), potentially multi-page, containing prose text, handwritten LaTeX
  equations, and hand-drawn figures or diagrams
- The script never modifies, moves, or deletes any file in this directory

### Output — Obsidian Vault

```
vault/
├── 18.06_Linear_Algebra/
│   ├── _index.md
│   ├── Lecture 01.md
│   ├── Lecture 02.md
│   └── figures/
│       ├── lecture_01_fig_001.png
│       ├── lecture_01_fig_002.png
│       └── ...
├── 6.006_Algorithms/
│   ├── _index.md
│   ├── Lecture 01.md
│   └── figures/
│       └── ...
└── ...
```

- Mirrors the input directory structure exactly (one folder per course)
- One `.md` file per lecture PDF
- Figures saved as PNG files in a `figures/` subdirectory within each course folder
- `_index.md` per course: auto-generated table of contents with links to each lecture

### Intermediate Cache

```
_cache/
├── 18.06_Linear_Algebra/
│   ├── lecture_01.mathpix.md
│   ├── lecture_01.llm.md
│   └── ...
└── ...
```

- Stores raw Mathpix output and LLM-cleaned output as separate intermediate files
- Figures from Mathpix are also saved here before being copied to the vault
- Allows re-running the LLM stage without re-querying Mathpix
- Should be gitignored; not part of the vault

### Project Root

```
notes-pipeline/
├── config.yaml
├── .env                  ← gitignored
├── .gitignore
├── requirements.txt
├── state.db              ← SQLite state log, gitignored
├── prompts/
│   └── cleanup_v1.txt    ← versioned LLM system prompt
├── _cache/               ← gitignored
└── src/
    ├── main.py
    ├── discovery.py
    ├── mathpix.py
    ├── llm.py
    ├── figures.py
    ├── postprocess.py
    ├── state.py
    └── config.py
```

---

## Full Pipeline

On each run, the script executes the following stages for each newly discovered PDF:

```
[Run script]
     ↓
[Stage 1: Discovery]
Scan notes_raw/ recursively
For each PDF: check filesystem metadata (mtime + size) against state log
  → If metadata unchanged: skip immediately (no hash computed)
  → If metadata changed or file is new: compute SHA-256 hash
      → If hash matches state log: skip (metadata changed but contents identical)
      → If hash differs or no entry: queue for processing
     ↓
[Stage 2: Mathpix Processing]
Submit PDF binary to Mathpix /v3/pdf endpoint
Poll /v3/pdf/{pdf_id} until status = completed
Download .md result → save to _cache/.../lecture_N.mathpix.md
Download figure image assets → save to _cache/.../figures/
     ↓
[Stage 3: LLM Cleanup]
Load raw .md from cache
Send to LLM with system prompt (see LLM section)
Validate response (length ratio, delimiter balance, heading count)
On pass → save to _cache/.../lecture_N.llm.md
On fail → log warning, fall back to Mathpix output
     ↓
[Stage 4: Figure Handling]
For each figure asset from Mathpix:
  Copy PNG to vault/.../figures/ with consistent naming
  Replace Mathpix image reference in .md with Obsidian wikilink: ![[filename.png]]
     ↓
[Stage 5: Post-processing]
Inject YAML frontmatter
Finalize Markdown
Write to vault/.../Lecture N.md
     ↓
[Stage 6: Index Update]
Regenerate or update _index.md for the course
     ↓
[Stage 7: State Log Update]
Record all stage results to state.db, including current mtime and size
```

After all new files are processed, print a summary (files processed, any errors)
and exit.

---

## Stage Details

### Stage 1 — Discovery

Discovery uses a two-tier change detection strategy to avoid unnecessary file reads.
Filesystem metadata is always checked first; the SHA-256 hash is only computed when
metadata indicates the file may have changed. Since PDFs in `notes_raw/` are written
once and never modified, in practice the hash is computed exactly once per file (on
first encounter) and the metadata pre-check eliminates all redundant reads on
subsequent runs.

**Metadata pre-check (tier 1):**
- For each `.pdf` found by recursively walking `notes_raw/`:
  - Read `mtime` (modification timestamp) and `size` (file size in bytes) from
    the filesystem — these are returned instantly from a directory listing without
    reading file contents
  - Query `state.db` for an existing entry matching the file path
  - If an entry exists AND both `mtime` and `size` match the stored values:
    skip immediately — no hash is computed, file is treated as unchanged
  - If `mtime` or `size` differs, or no entry exists: proceed to tier 2

**Hash check (tier 2):**
- Compute SHA-256 hash by reading full file contents
- If entry exists in state log AND hash matches stored hash:
  - Update stored `mtime` and `size` to current values (metadata drifted but
    file is identical — e.g. file was copied or touched)
  - Skip processing
- If hash differs from stored hash: queue for full reprocessing
- If no entry exists: queue as new file

**Output of discovery stage:** an ordered list of PDF paths requiring processing,
grouped by course.

### Stage 2 — Mathpix API

**Endpoint:** POST `/v3/pdf`

**Request:**
- Body: raw PDF binary
- Headers: `app_id` and `app_key` from environment variables
- Options to include in request body:
  - Math delimiter format: `dollars` (produces `$...$` and `$$...$$`)
  - Include page breaks: yes (useful for chunking reference and figure position)
  - Return formats: `md` (standard Markdown with LaTeX)

**Polling:**
- GET `/v3/pdf/{pdf_id}` on a configurable interval (default: 5 seconds)
- Status field progresses: `loading → processing → completed` or `error`
- Maximum poll attempts configurable (default: 60, i.e. 5 minutes max wait)
- On timeout or error: mark file as `mathpix_failed` in state log, continue
  to next file

**Output retrieval:**
- GET `/v3/pdf/{pdf_id}.md` → raw Markdown text
- GET `/v3/pdf/{pdf_id}` → JSON response including figure asset references
- Each figure listed in the JSON response should be individually downloaded
  as a PNG from its asset URL

**Caching:**
- Save raw `.md` to `_cache/.../lecture_N.mathpix.md`
- Save each figure PNG to `_cache/.../figures/lecture_N_fig_NNN.png`

### Stage 3 — LLM Cleanup

**Purpose:** Mechanical copy-editing only. Fix errors introduced by Mathpix OCR.
Do not alter content, meaning, or mathematical expressions.

**Model:** GPT-4o-mini or Claude Haiku 3.5 (configurable via `config.yaml`)

**LLM Abstraction:** Use `litellm` to abstract API calls, allowing model to be
switched by changing one config value.

**What the LLM should fix:**
- Misspelled English words from OCR errors (e.g. `"eigenvlue"` → `"eigenvalue"`)
- Broken prose spacing (double spaces, missing spaces after punctuation)
- Inconsistent Markdown heading levels within the document
- Inconsistent list formatting (normalize to `-` bullets)
- Obvious LaTeX command name typos (e.g. `\fracction` → `\frac`)
- Stray isolated characters that are clearly OCR artifacts
- Math delimiter inconsistency — ensure inline uses `$...$` and display uses
  `$$...$$`

**What the LLM must NOT do:**
- Change any mathematical content inside LaTeX delimiters
- Change numbers, variables, operators, or symbols in equations
- Add words, sentences, or explanations not present in the input
- Remove content that appears meaningful even if unclear
- Restructure, reorder, or reorganize content
- Summarize or paraphrase

**Prompt structure:**
- System prompt: defines role and enumerates all hard constraints (stored in
  `prompts/cleanup_v1.txt`, versioned independently)
- User prompt: the raw Mathpix Markdown content only, no additional instructions
- Output instruction: return only the corrected Markdown with no preamble,
  commentary, or explanation

**Chunking:**
- For most lecture notes, send entire document in a single call
- If document exceeds a safe token threshold (configurable, default ~60,000
  tokens), split on Mathpix page break markers
- For chunked documents: include small overlap (last paragraph of previous
  chunk) at the start of each new chunk; deduplicate overlap on reassembly

**Validation (after LLM response):**
- Length ratio check: output token count should be between 70% and 130% of input
- LaTeX delimiter balance: `$` signs should appear in pairs; `$$` blocks should
  be even
- Heading count: number of `#`/`##`/`###` headings should match input exactly
- If any check fails:
  - Log the specific failure with details
  - Fall back to raw Mathpix output for this file
  - Mark as `llm_failed` in state log
  - Continue pipeline with fallback content

**Cost reference (approximate):**
- GPT-4o-mini: ~$0.002 per lecture note
- Full semester (~150 lectures): ~$0.30 total

### Stage 4 — Figure Handling

**Approach:** Direct image embedding (raster PNG from Mathpix crop)

**Process:**
- For each figure extracted from Mathpix and saved to cache:
  - Copy PNG to `vault/{course}/figures/{lecture}_fig_{NNN}.png`
  - In the cleaned Markdown, replace the Mathpix-generated image reference
    (e.g. `![](figure_0001.png)`) with an Obsidian wikilink:
    `![[lecture_01_fig_001.png]]`
- Naming convention: `{lecture_stem}_fig_{NNN}.png` where NNN is zero-padded
  (e.g. `lecture_01_fig_001.png`)

**Note:** No LLM or additional processing is applied to figures in this
implementation. They are embedded as faithful image crops of the original PDF.

### Stage 5 — Post-processing

**YAML Frontmatter:**
Inject at the top of every output `.md` file. Fields:

```yaml
---
title: "Lecture 01"
course: "18.06 Linear Algebra"
date: YYYY-MM-DD
lecture_number: 1
tags:
  - lecture-notes
source_pdf: "notes_raw/18.06_Linear_Algebra/lecture_01.pdf"
processed: YYYY-MM-DD
---
```

- `date`: inferred from file modification timestamp, or parsed from filename
  if date is encoded there
- `course`: derived from the parent folder name (underscores replaced with spaces)
- `lecture_number`: parsed from filename
- `tags`: base tags from config, optionally extended per-course in config

**Math delimiter pass:**
After LLM cleanup, do a final lightweight string scan to confirm all math
delimiters are `$...$` and `$$...$$`. Log a warning if any `\(...\)` or
`\[...\]` remain but do not attempt to fix automatically (risk of corruption).

### Stage 6 — Course Index Generation

After all lectures in a course are processed, regenerate `_index.md` for that
course:

```markdown
# 18.06 Linear Algebra

| Lecture | Title | Date |
|---------|-------|------|
| 1 | [[Lecture 01]] | 2024-02-05 |
| 2 | [[Lecture 02]] | 2024-02-07 |
| ... | | |
```

- Always regenerate from the current state of the vault (not incremental)
- This ensures the index is correct even if files were reprocessed or renamed

---

## State Management

**Storage:** SQLite database (`state.db`) in project root

**Schema (conceptual):**

Each row represents one processed PDF and tracks:
- `source_path`: absolute path to input PDF
- `source_hash`: SHA-256 hash of PDF contents at time of processing
- `source_mtime`: filesystem modification timestamp at time of last hash check
- `source_size`: file size in bytes at time of last hash check
- `mathpix_pdf_id`: Mathpix job ID
- `mathpix_status`: `success`, `failed`, `pending`
- `llm_model`: model name used (e.g. `gpt-4o-mini`)
- `llm_prompt_version`: filename/version of system prompt used
- `llm_status`: `success`, `failed`, `skipped` (if fallback used)
- `llm_validation_result`: which checks passed/failed
- `figure_count`: number of figures extracted
- `output_path`: path to final `.md` in vault
- `mathpix_processed_at`: timestamp
- `llm_processed_at`: timestamp
- `vault_written_at`: timestamp

**Change detection logic using stored metadata:**

On each run, for every PDF found in `notes_raw/`:
1. Read current `mtime` and `size` from filesystem (no file read required)
2. Look up stored `mtime`, `size`, and `source_hash` in state log
3. If current `mtime` == stored `mtime` AND current `size` == stored `size`:
   → Skip. File has not changed. No hash computation needed.
4. Else: compute SHA-256 hash
   - If hash == stored `source_hash`: update stored `mtime` and `size`, skip processing
   - If hash differs or no entry: queue for processing

**Reprocessing logic:**

| Scenario | Action |
|---|---|
| PDF mtime and size unchanged | Skip entirely — no hash computed |
| PDF mtime or size changed, hash unchanged | Update metadata in state log, skip processing |
| PDF hash changed | Re-run full pipeline |
| No state log entry | Process as new file |
| `mathpix_status = failed` | Re-run from Mathpix stage |
| `llm_status = failed` | Re-run LLM stage only (use cached Mathpix output) |
| PDF unchanged, prompt version updated | Re-run LLM stage only (use cached Mathpix output) |

---

## Configuration

**`config.yaml`** — all non-secret configuration:

```yaml
paths:
  input_root: "/path/to/notes_raw"
  vault_root: "/path/to/vault"
  cache_dir: "/path/to/notes-pipeline/_cache"
  state_db: "/path/to/notes-pipeline/state.db"
  prompt_file: "prompts/cleanup_v1.txt"

mathpix:
  poll_interval_seconds: 5
  max_poll_attempts: 60
  math_delimiters: "dollars"

llm:
  model: "gpt-4o-mini"          # litellm model string
  max_input_tokens: 60000       # chunk threshold
  validation:
    min_length_ratio: 0.70
    max_length_ratio: 1.30

output:
  date_format: "%Y-%m-%d"
  base_tags:
    - "lecture-notes"
  course_tags:                  # optional per-course tag overrides
    18.06_Linear_Algebra:
      - "lecture-notes"
      - "linear-algebra"
      - "math"

naming:
  lecture_prefix: "Lecture"     # output file prefix
```

**`.env`** — secrets only, never committed to version control:

```
MATHPIX_APP_ID=your_app_id
MATHPIX_APP_KEY=your_app_key
OPENAI_API_KEY=your_openai_key   # or ANTHROPIC_API_KEY etc.
```

---

## Naming Convention

Input PDF filenames should follow a consistent convention so lecture number and
optionally date can be parsed. Recommended convention:

```
lecture_01.pdf
lecture_02.pdf
```

Or with date:
```
lecture_01_2024-02-05.pdf
```

Or with topic:
```
lecture_01_eigenvalues.pdf
```

The script should parse the lecture number from the filename stem. The output
`.md` filename is derived as `Lecture {N:02d}.md` (zero-padded two-digit number).
The full human-readable title in frontmatter can include the topic if present in
the source filename.

---

## Error Handling

| Scenario | Handling |
|---|---|
| Mathpix returns error status | Log, mark `mathpix_failed`, skip to next file |
| Mathpix poll timeout | Log, mark `mathpix_failed`, skip to next file |
| HTTP 429 rate limit | Respect `Retry-After` header; wait and retry |
| Network error during poll | Exponential backoff, up to N retries (configurable) |
| LLM validation failure | Log specific check(s) failed, use Mathpix output as fallback |
| LLM API error | Log, use Mathpix output as fallback, mark `llm_failed` |
| Output file already exists in vault | Overwrite (file may be a reprocess) |
| Malformed or unparseable filename | Log warning, skip file, do not add to state log |
| Figure asset download fails | Log warning, omit figure reference from Markdown |

All errors should be logged with sufficient detail (file path, stage, error
message) to diagnose without re-running. The script should continue processing
remaining files after a per-file error — one bad file should not abort the
entire run.

---

## Technology Stack

| Component | Library/Tool |
|---|---|
| Language | Python 3.11+ |
| HTTP / API calls | `httpx` or `requests` |
| LLM API abstraction | `litellm` |
| Config parsing | `PyYAML` |
| Secrets / env vars | `python-dotenv` |
| State log | `sqlite3` (stdlib) |
| Hashing | `hashlib` (stdlib) |
| Path handling | `pathlib` (stdlib) |
| CLI entry point | `argparse` (stdlib) or `click` |

---

## CLI Interface

The script is invoked directly from the command line:

```
python src/main.py
```

Optional flags to support:

```
--dry-run        Scan and report what would be processed, without making any
                 API calls
--force          Reprocess all files regardless of state log (useful for testing)
--course NAME    Process only a specific course folder
--verbose        Print detailed per-file progress
```

On completion, print a summary:
```
Processed: 3 new files
Skipped:   12 files (already up to date)
Errors:    1 file (see log for details)
```

---

## Development Phases

**Phase 1 — Core Mathpix pipeline**
Single PDF → Mathpix submit → poll → retrieve `.md` and figures → write to cache.
Verify output quality on real notes before proceeding.

**Phase 2 — State management**
SQLite state log, two-tier change detection (metadata pre-check then hash),
idempotent rerun behavior.

**Phase 3 — LLM cleanup**
Single-call LLM pass with system prompt, validation, and fallback. Test prompt
against real Mathpix output; iterate system prompt until satisfied.

**Phase 4 — Figure handling**
Download Mathpix figure assets, copy to vault figures directory, replace image
references with Obsidian wikilinks.

**Phase 5 — Post-processing**
YAML frontmatter injection, course index generation, final delimiter scan.

**Phase 6 — Full folder scanning and config**
Generalize discovery across all courses, wire up `config.yaml` fully, test
end-to-end on a full course's worth of notes.

**Phase 7 — CLI polish**
`--dry-run`, `--force`, `--course` flags, clean summary output, proper logging
to file alongside stdout.

---

## Key Design Decisions Summary

| Decision | Choice | Reason |
|---|---|---|
| Execution model | Manual CLI script | No real-time requirement; simpler; explicit |
| notes_raw/ access | Read-only permanent archive | Source files never at risk; always available for reprocessing |
| Change detection | Metadata pre-check (mtime + size), hash only on change | Avoids reading file contents on every run; hash computed once per file |
| Figure handling | Raster image embed from Mathpix crop | Faithful, always works, no extra API cost |
| Math delimiters | `$...$` and `$$...$$` | Native Obsidian rendering |
| LLM task scope | Copy-editing only, never alter math | Correctness and safety |
| LLM model | GPT-4o-mini / Claude Haiku (configurable) | Best instruction-following at low cost |
| LLM abstraction | `litellm` | Easy model switching via config |
| LLM failure handling | Fall back to raw Mathpix output | Never write corrupted notes to vault |
| State storage | SQLite | Richer than JSON, no external dependency |
| Caching | Separate Mathpix and LLM cache files | Re-run LLM stage without re-querying Mathpix |
| Vault treatment | Plain directory of `.md` files | No Obsidian-specific dependencies in the code |
