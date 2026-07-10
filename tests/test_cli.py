"""
Unit tests for src/cli.py (issue #41 — argparse scaffolding + --course NAME).

Pure argparse-level tests: no PathsConfig/state.db/tmp_path fixtures needed,
since build_arg_parser() has no dependency on the pipeline itself. See
tests/test_main.py for run()/main()-level coverage of --course's actual
filtering behavior.
"""

from __future__ import annotations

from src.cli import build_arg_parser


def test_course_defaults_to_none_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.course is None


def test_course_parses_given_value():
    args = build_arg_parser().parse_args(["--course", "class_1"])

    assert args.course == "class_1"
