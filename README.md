# NoTeX

NoTeX is a Python CLI tool to digitize and catalog handwritten notes with 
mathematical content. It scans a directory of handwritten lecture note
PDFs, runs them through the Mathpix API for OCR (optical character recognition)
generating text, LaTeX, and figures from handdrawn diagrams. It then cleans up
the extracted text with an LLM and writes organized Markdown. The intended 
application is organization into an Obsidian (or other text-based note software)
markdown file vault.

## Workflow

NoTeX keeps track of the state of the input directory and will find both newly 
added notes and those that have been updated since the last run to process. I 
found that existing approaches for handwriting conversion did not perform well
on the types of notes I take, which may have messy handwriting, figures, or 
equations which when processed resulted in jumbled or incorrect LaTeX. Cleaning
this output up is as task well suited to the capabilities of modern language
models, and the combination of normal handwriting text extraction with an LLM
postprocessing step works remarkably well. The cost for both stages of 
processing a single page of notes is around $0.01.

Since I often write notes on subjects in Obsidian, I wanted to combine this 
digitization process with a state management system to keep everything updated. 
With NoTeX, I can take notes as usual (handwritten on an iPad), save them to the
configured NoTeX input folder, then run the program. My Obsidian notes are then
automatically populated with my notes from lectures, available for searching or
reviewing later.

Several formatting options are available, including a stateless manual mode that
only runs the digitization process and saves the output to a file.

## Dependencies

NoTeX runs inside a conda virtual environment. Package dependencies are in
`environment.yml` and can be installed via 

```
conda env create -f environment.yml
conda activate notex
```

You'll also need:

- A [Mathpix API account](https://console.mathpix.com/) (App ID + App Key) for OCR
- An API key for whichever LLM provider you configure via `litellm`
  (e.g. `ANTHROPIC_API_KEY` for Claude models)

## Usage

### Configuration

Before running, set up configuration files from the examples here:

1. **`.env`** — copy `.env.example` to `.env` and fill in your credentials:

   ```
   MATHPIX_APP_ID=
   MATHPIX_APP_KEY=
   ANTHROPIC_API_KEY=
   ```

2. **`config.yaml`** — copy `config.example.yaml` to `config.yaml` and fill
   in your real paths and settings:

   `input_root` should be set to the path of the top level directory for
   handwritten notes. Then `vault_root` should point to the top level directory
   for the markdown output files.

   The program expects organization with class or subject headings as 
   subdirectories under `input_root` as follows:
   ```
   input_root/
    ├── class_1/
    │   ├── note_1.pdf
    │   ├── note_2.pdf
    │   ├── note_3.pdf
    │   └── ...
    ├── class_2/
    │   ├── note_1.pdf
    │   ├── note_2.pdf
    │   └── ...
    ```
    After running the program, the output will be in `vault_root/` with a 
    mirrored file structure. Any extracted figures will be in a
    `{class_x}/figures/` directory.

    Also in `config.yaml`, LLM settings can be configured, including the model
    (as listed in `litellm`) and prompt, which the user may want to tailor to
    their application.

    Optionally, tags can be added to the beginning of markdown files, which are
    configured under `course_tags` for each course (corresponds with each directory name under `input_root`). The `figures_dark_mode_flag` setting 
    adds `@darkmode` to the alt text of figure embeddings which can separately
    be selected and styled in a markdown viewer.

    Separately from tags, the LLM cleanup step also generates a `keywords`
    frontmatter field for each note automatically — 1-5 keywords (which may
    themselves be multi-word phrases, e.g. "fine structure") describing the
    page's content, chosen at a level of specificity intended to make an
    entire collection of notes searchable/indexable by topic. Unlike tags,
    these are derived per-note from the actual content, not configured per
    course.

    When saving, numbers are extracted from the PDF note filenames, and outputs
    are written as `{lecture_prefix} XX.md` according to the `naming` field.

    A `print_summary` setting is also available to print out details after the
    run, including the LLM API call costs.

    If you have many notes indexed, the live table (shown when running in an
    interactive terminal with `rich` installed) can get noisy with files that
    are already up to date. Setting `hide_up_to_date: true` under `cli:` hides
    those rows and instead shows a `(N) files already up to date` summary
    line. This has no effect on the plain, non-interactive output, which
    already never prints anything for up-to-date files.

### Running the pipeline

```
conda activate notex
python -m src.main
```

This recursively scans `input_root/` for course subdirectories of PDFs,
classifies each file (new/unchanged/changed/retry) against `state.db`, and
for anything needing work: OCRs it via Mathpix, cleans up the text with the
LLM, copies figures, and writes/updates `vault/{course}/{lecture_prefix} NN.md`.

Common flags (see `python -m src.main --help` for the full list):

```
--course NAME             Restrict the run to one course subdirectory
--file PATH               Restrict the run to exactly one PDF
--dry-run                 Report what would happen; no API calls or writes
--force                   Reprocess Mathpix + LLM regardless of state.db
--rerun-llm               Reprocess just the LLM stage (e.g. after a prompt change)
--force-vault-overwrite   Bypass manual-edit conflict detection
--no-llm                  Skip the LLM stage; write raw Mathpix Markdown
--verbose, -v             Print additional per-stage detail
```

Examples:

```
# Preview what a full run would do
python -m src.main --dry-run

# Process one course only
python -m src.main --course "18.06 Linear Algebra"

# Re-run the LLM cleanup on one file after editing the prompt
python -m src.main --file /path/to/notes_raw/18.06/lecture_03.pdf --rerun-llm
```

### Manual conversion mode

For one-off conversions outside the normal structure (e.g. a PDF that
doesn't live under `input_root` or follow the `lecture_NN...` naming
convention), use the separate, stateless `scripts/manual_convert.py`. It
runs the full Mathpix → LLM → figures → frontmatter → vault-write pipeline
for one explicit source PDF and destination `.md` pair, without touching
`state.db`. Here optional flags add tags to the output file, the configuration
in `config.yaml` is not applied.

```
python scripts/manual_convert.py path/to/some_notes.pdf "vault/Misc/Some Notes.md" \
    --course "18.06 Linear Algebra" --lecture-number 3 --tags lecture-notes,math
```
## Example

See `example/` for an example on a handwritten note. The file 
`lecture_02.mathpix.md` is the raw output from the Mathpix API, which is quite
jumbled and contains both spelling and LaTeX compiling errors. The 
`Lecture 02.md` is the final LLM processed note, which is nearly identical to
the handwritten one.