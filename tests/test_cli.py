"""
Unit tests for src/cli.py (issue #41 — argparse scaffolding + --course NAME;
issue #42 — --dry-run; issue #43 — --force).

Pure argparse-level tests: no PathsConfig/state.db/tmp_path fixtures needed,
since build_arg_parser() has no dependency on the pipeline itself. See
tests/test_main.py for run()/main()-level coverage of --course's/--dry-run's/
--force's actual behavior.
"""

from __future__ import annotations

from src.cli import build_arg_parser


def test_course_defaults_to_none_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.course is None


def test_course_parses_given_value():
    args = build_arg_parser().parse_args(["--course", "class_1"])

    assert args.course == "class_1"


def test_dry_run_defaults_to_false_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.dry_run is False


def test_dry_run_flag_sets_true():
    args = build_arg_parser().parse_args(["--dry-run"])

    assert args.dry_run is True


def test_force_defaults_to_false_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.force is False


def test_force_flag_sets_true():
    args = build_arg_parser().parse_args(["--force"])

    assert args.force is True
