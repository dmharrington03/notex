"""
Phase 7 CLI argument parsing (issue #41).

Holds the argparse scaffolding for src/main.py's main() -- split into its own
module rather than built inline in main() (a deliberate deviation from issue
#41's literal "Add an argparse.ArgumentParser in main()" wording, confirmed
with the user) since the full Phase 7 scope adds seven more flags across
follow-up issues (#46, #48: --no-llm, --verbose/-v), several with their
own cross-flag validation (e.g. --course/--file mutual exclusion). Keeping
parser construction here mirrors the project's existing one-module-per-
concern convention (src/config.py, src/discovery.py, etc.) and lets
tests/test_cli.py exercise parsing/validation in complete isolation, with no
PathsConfig/state.db/tmp_path fixtures needed at all.

Every later Phase 7 flag issue extends build_arg_parser() here, not
src/main.py; src/main.py's main() only calls it and forwards the parsed
values into run().
"""

from __future__ import annotations

import argparse


def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build the CLI's argument parser.

    Currently --course NAME (issue #41), --dry-run (issue #42), --force
    (issue #43), --rerun-llm / --file PATH (issue #44), and
    --force-vault-overwrite (issue #45); later Phase 7 issues add
    --no-llm / --verbose to this same parser.
    """
    parser = argparse.ArgumentParser(
        prog="notex",
        description=(
            "Scan a directory of handwritten lecture note PDFs, OCR them via "
            "Mathpix, clean up the extracted text with an LLM, and write "
            "organized Markdown into an Obsidian vault."
        ),
    )
    course_or_file = parser.add_mutually_exclusive_group()
    course_or_file.add_argument(
        "--course",
        metavar="NAME",
        default=None,
        help=(
            "Restrict this run to one course subdirectory of paths.input_root "
            "(exact, case-sensitive match against the course folder name). "
            "The full directory is still scanned; every other course is "
            "simply skipped. An unknown course name is a clean no-op (a "
            "warning is printed, nothing is processed) rather than an error. "
            "Mutually exclusive with --file."
        ),
    )
    course_or_file.add_argument(
        "--file",
        metavar="PATH",
        default=None,
        help=(
            "Restrict this run to exactly one PDF (an exact source path, "
            "not a course) instead of scanning paths.input_root. The path "
            "must exist, end in .pdf (case-insensitive), and live under "
            "paths.input_root -- main() rejects anything else with exit "
            "code 1 before any API calls happen. Combine with --rerun-llm "
            "to reprocess just this one lecture's LLM stage after tweaking "
            "the prompt. Mutually exclusive with --course."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Report what would be processed without doing it -- no Mathpix "
            "or LLM API calls, and no state.db/cache/vault writes."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Reprocess every discovered file's Mathpix + LLM stages "
            "regardless of state.db's classification (useful for testing). "
            "Distinct from the LLM-only --rerun-llm flag."
        ),
    )
    parser.add_argument(
        "--rerun-llm",
        action="store_true",
        default=False,
        help=(
            "Reprocess the LLM cleanup stage for every eligible file, "
            "regardless of its stored status/version -- useful after "
            "tweaking the LLM prompt. For an already-up-to-date "
            "(UNCHANGED) file this hits only the LLM API, reusing the "
            "cached Mathpix output -- no Mathpix API call. For a "
            "NEW/CHANGED/RETRY file there's no cached Mathpix output to "
            "reuse yet, so both the Mathpix and LLM stages run regardless "
            "of this flag. Commonly combined with --file to target one "
            "lecture."
        ),
    )
    parser.add_argument(
        "--force-vault-overwrite",
        action="store_true",
        default=False,
        help=(
            "Bypass manually-edited-vault-note conflict detection for "
            "every file this run (issue #40's vault_status='conflict' "
            "escape hatch) -- a detected conflict is overwritten "
            "unconditionally with the pipeline's version instead of being "
            "skipped. A blunt, whole-run instrument: there is no way to "
            "target one conflicted file while leaving others alone."
        ),
    )
    return parser
