"""
Phase 7 CLI argument parsing (issue #41).

Holds the argparse scaffolding for src/main.py's main() -- split into its own
module rather than built inline in main() (a deliberate deviation from issue
#41's literal "Add an argparse.ArgumentParser in main()" wording, confirmed
with the user) since the full Phase 7 scope adds seven more flags across
follow-up issues (#43-#46, #48: --force, --refresh-llm-prompt,
--file, --force-vault-overwrite, --no-llm, --verbose/-v), several with their
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

    Currently --course NAME (issue #41) and --dry-run (issue #42); later
    Phase 7 issues add --force / --refresh-llm-prompt / --file /
    --force-vault-overwrite / --no-llm / --verbose to this same parser.
    """
    parser = argparse.ArgumentParser(
        prog="notex",
        description=(
            "Scan a directory of handwritten lecture note PDFs, OCR them via "
            "Mathpix, clean up the extracted text with an LLM, and write "
            "organized Markdown into an Obsidian vault."
        ),
    )
    parser.add_argument(
        "--course",
        metavar="NAME",
        default=None,
        help=(
            "Restrict this run to one course subdirectory of paths.input_root "
            "(exact, case-sensitive match against the course folder name). "
            "The full directory is still scanned; every other course is "
            "simply skipped. An unknown course name is a clean no-op (a "
            "warning is printed, nothing is processed) rather than an error."
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
    return parser
