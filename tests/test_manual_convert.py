"""
Lightweight argparse-only smoke check for scripts/manual_convert.py (issue
#50).

scripts/manual_convert.py is a manual smoke-test-style script (hits real
paid Mathpix/LLM APIs) -- per AGENTS.md's Testing Conventions, it is not
part of the automated pipeline and has no full pytest coverage. This file
covers only CLI parsing/defaults, mirroring tests/test_cli.py's precedent:
no network, no real config.yaml dependency beyond what
load_output_config()/load_naming_config()'s fully-optional fallback
already handles gracefully.
"""

from __future__ import annotations

from scripts.manual_convert import parse_args


def test_positional_args_required():
    args = parse_args(["source.pdf", "dest.md"])

    assert args.pdf_path.name == "source.pdf"
    assert args.dest_path.name == "dest.md"


def test_optional_flags_default_to_none():
    args = parse_args(["source.pdf", "dest.md"])

    assert args.course is None
    assert args.lecture_number is None
    assert args.tags is None
    assert args.prompt_version is None
    assert args.keep_cache is False


def test_optional_flags_parse_given_values():
    args = parse_args(
        [
            "source.pdf",
            "dest.md",
            "--course",
            "18.06 Linear Algebra",
            "--lecture-number",
            "3",
            "--tags",
            "lecture-notes,math",
            "--prompt-version",
            "cleanup_v2",
            "--keep-cache",
        ]
    )

    assert args.course == "18.06 Linear Algebra"
    assert args.lecture_number == 3
    assert args.tags == "lecture-notes,math"
    assert args.prompt_version == "cleanup_v2"
    assert args.keep_cache is True


def test_dark_mode_toggle():
    on_args = parse_args(["source.pdf", "dest.md", "--dark-mode"])
    off_args = parse_args(["source.pdf", "dest.md", "--no-dark-mode"])

    assert on_args.dark_mode is True
    assert off_args.dark_mode is False
