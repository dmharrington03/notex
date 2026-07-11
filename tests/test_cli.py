"""
Unit tests for src/cli.py (issue #41 — argparse scaffolding + --course NAME;
issue #42 — --dry-run; issue #43 — --force; issue #44 — --rerun-llm and
--file PATH; issue #45 — --force-vault-overwrite; issue #46 — --no-llm;
issue #48 — --verbose/-v).

Pure argparse-level tests: no PathsConfig/state.db/tmp_path fixtures needed,
since build_arg_parser() has no dependency on the pipeline itself. See
tests/test_main.py for run()/main()-level coverage of --course's/--dry-run's/
--force's/--rerun-llm's/--file's/--force-vault-overwrite's/--no-llm's/
--verbose's actual behavior (including --file's exists/.pdf/under-input_root
validation, which lives in main(), not here).
"""

from __future__ import annotations

import pytest

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


def test_rerun_llm_defaults_to_false_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.rerun_llm is False


def test_rerun_llm_flag_sets_true():
    args = build_arg_parser().parse_args(["--rerun-llm"])

    assert args.rerun_llm is True


def test_file_defaults_to_none_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.file is None


def test_file_parses_given_value():
    args = build_arg_parser().parse_args(["--file", "/tmp/notes_raw/class_1/lecture_01.pdf"])

    assert args.file == "/tmp/notes_raw/class_1/lecture_01.pdf"


def test_course_and_file_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--course", "class_1", "--file", "lecture_01.pdf"])


def test_force_vault_overwrite_defaults_to_false_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.force_vault_overwrite is False


def test_force_vault_overwrite_flag_sets_true():
    args = build_arg_parser().parse_args(["--force-vault-overwrite"])

    assert args.force_vault_overwrite is True


def test_no_llm_defaults_to_false_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.no_llm is False


def test_no_llm_flag_sets_true():
    args = build_arg_parser().parse_args(["--no-llm"])

    assert args.no_llm is True


def test_verbose_defaults_to_false_when_omitted():
    args = build_arg_parser().parse_args([])

    assert args.verbose is False


def test_verbose_long_flag_sets_true():
    args = build_arg_parser().parse_args(["--verbose"])

    assert args.verbose is True


def test_verbose_short_flag_sets_true():
    args = build_arg_parser().parse_args(["-v"])

    assert args.verbose is True
