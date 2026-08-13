"""
Unit tests for src/config.py.

Covered here (issue #2 — config.yaml mathpix: polling defaults):
    - load_mathpix_polling_config() reads poll_interval_seconds /
      max_poll_attempts from a config.yaml's mathpix: section
    - falls back to DEFAULT_POLL_INTERVAL_SECONDS / DEFAULT_MAX_POLL_ATTEMPTS
      when the file is missing, or the mathpix: section/keys are absent

Also covered here (issue #10 — config.yaml paths: settings; issue #26 —
paths.vault_root):
    - load_paths_config() reads input_root / vault_root / cache_dir /
      state_db from a config.yaml's paths: section
    - cache_dir/state_db fall back to DEFAULT_CACHE_DIR/DEFAULT_STATE_DB
      when absent
    - raises ConfigError when the file is missing, the paths: section is
      missing, or input_root/vault_root itself is missing/blank

Also covered here (issue #13 — config.yaml llm: settings):
    - load_llm_config() reads model / prompt_version /
      validation.min_length_ratio / validation.max_length_ratio from a
      config.yaml's llm: section
    - falls back to DEFAULT_LLM_MODEL / DEFAULT_PROMPT_VERSION /
      DEFAULT_MIN_LENGTH_RATIO / DEFAULT_MAX_LENGTH_RATIO when the file is
      missing, the llm: section is absent, the validation: subsection is
      absent, or individual keys are missing — never raises ConfigError

Also covered here (issue #33 — config.yaml output: settings):
    - load_output_config() reads course_tags / date_format /
      figures_dark_mode_flag from a config.yaml's output: section
    - falls back to {} / DEFAULT_DATE_FORMAT / DEFAULT_FIGURES_DARK_MODE_FLAG
      when the file is missing, the output: section is absent, or
      individual keys are missing — never raises ConfigError
    - there is no global/default tag list: a course with no course_tags
      entry gets no tags at all (deliberate divergence from docs/spec.md's
      base_tags concept, see AGENTS.md's Phase 6 notes)

Also covered here (issue #34 — config.yaml naming: settings):
    - load_naming_config() reads lecture_prefix from a config.yaml's
      naming: section
    - falls back to DEFAULT_LECTURE_PREFIX when the file is missing, the
      naming: section is absent, or lecture_prefix itself is missing —
      never raises ConfigError
    - lecture_prefix is a single global value, no per-course override

Also covered here (Phase 7 follow-up — config.yaml cli: settings):
    - load_cli_config() reads print_summary from a config.yaml's cli:
      section
    - falls back to DEFAULT_PRINT_SUMMARY (False) when the file is
      missing, the cli: section is absent, or print_summary itself is
      missing — never raises ConfigError
    - print_summary gates src/main.py's main()'s full summary print (see
      tests/test_main.py for the main()-level wiring)

Also covered here (issue #53 — config.yaml cli: hide_up_to_date):
    - load_cli_config() reads hide_up_to_date from a config.yaml's cli:
      section
    - falls back to DEFAULT_HIDE_UP_TO_DATE (False) when the file is
      missing, the cli: section is absent, or hide_up_to_date itself is
      missing — never raises ConfigError
    - hide_up_to_date gates RichReporter's live-table row filtering (see
      tests/test_reporting.py for the RichReporter-level behavior)
"""

from pathlib import Path

import pytest

from src.config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_DATE_FORMAT,
    DEFAULT_FIGURES_DARK_MODE_FLAG,
    DEFAULT_HIDE_UP_TO_DATE,
    DEFAULT_IMAGE_LINK_SYNTAX,
    DEFAULT_LECTURE_PREFIX,
    DEFAULT_LLM_MODEL,
    DEFAULT_MAX_LENGTH_RATIO,
    DEFAULT_MAX_POLL_ATTEMPTS,
    DEFAULT_MIN_LENGTH_RATIO,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PRINT_SUMMARY,
    DEFAULT_PROMPT_VERSION,
    DEFAULT_STATE_DB,
    CLIConfig,
    ConfigError,
    LLMConfig,
    MathpixPollingConfig,
    NamingConfig,
    OutputConfig,
    PathsConfig,
    load_cli_config,
    load_llm_config,
    load_mathpix_polling_config,
    load_naming_config,
    load_output_config,
    load_paths_config,
)


def test_loads_polling_config_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mathpix:\n  poll_interval_seconds: 2\n  max_poll_attempts: 5\n"
    )

    config = load_mathpix_polling_config(config_path)

    assert config == MathpixPollingConfig(poll_interval_seconds=2, max_poll_attempts=5)


def test_falls_back_to_defaults_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.yaml"

    config = load_mathpix_polling_config(missing_path)

    assert config == MathpixPollingConfig(
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
        max_poll_attempts=DEFAULT_MAX_POLL_ATTEMPTS,
    )


def test_falls_back_to_defaults_when_mathpix_section_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("paths:\n  input_root: /tmp/notes_raw\n")

    config = load_mathpix_polling_config(config_path)

    assert config == MathpixPollingConfig(
        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
        max_poll_attempts=DEFAULT_MAX_POLL_ATTEMPTS,
    )


def test_falls_back_to_defaults_when_individual_keys_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mathpix:\n  poll_interval_seconds: 2\n")

    config = load_mathpix_polling_config(config_path)

    assert config == MathpixPollingConfig(
        poll_interval_seconds=2,
        max_poll_attempts=DEFAULT_MAX_POLL_ATTEMPTS,
    )


def test_loads_paths_config_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "paths:\n"
        "  input_root: /tmp/notes_raw\n"
        "  vault_root: /tmp/vault\n"
        "  cache_dir: /tmp/notex_cache\n"
        "  state_db: /tmp/notex_state.db\n"
    )

    config = load_paths_config(config_path)

    assert config == PathsConfig(
        input_root=Path("/tmp/notes_raw"),
        vault_root=Path("/tmp/vault"),
        cache_dir=Path("/tmp/notex_cache"),
        state_db=Path("/tmp/notex_state.db"),
    )


def test_paths_config_falls_back_to_defaults_when_optional_keys_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("paths:\n  input_root: /tmp/notes_raw\n  vault_root: /tmp/vault\n")

    config = load_paths_config(config_path)

    assert config == PathsConfig(
        input_root=Path("/tmp/notes_raw"),
        vault_root=Path("/tmp/vault"),
        cache_dir=DEFAULT_CACHE_DIR,
        state_db=DEFAULT_STATE_DB,
    )


def test_paths_config_raises_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.yaml"

    with pytest.raises(ConfigError):
        load_paths_config(missing_path)


def test_paths_config_raises_when_paths_section_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mathpix:\n  poll_interval_seconds: 2\n")

    with pytest.raises(ConfigError):
        load_paths_config(config_path)


def test_paths_config_raises_when_input_root_missing_or_blank(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'paths:\n  input_root: ""\n  vault_root: /tmp/vault\n  cache_dir: /tmp/cache\n'
    )

    with pytest.raises(ConfigError):
        load_paths_config(config_path)


def test_paths_config_raises_when_vault_root_missing_or_blank(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'paths:\n  input_root: /tmp/notes_raw\n  vault_root: ""\n  cache_dir: /tmp/cache\n'
    )

    with pytest.raises(ConfigError):
        load_paths_config(config_path)


def test_loads_llm_config_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "llm:\n"
        '  model: "claude-3-5-sonnet-20241022"\n'
        '  prompt_version: "cleanup_v2"\n'
        "  validation:\n"
        "    min_length_ratio: 0.5\n"
        "    max_length_ratio: 1.5\n"
    )

    config = load_llm_config(config_path)

    assert config == LLMConfig(
        model="claude-3-5-sonnet-20241022",
        prompt_version="cleanup_v2",
        min_length_ratio=0.5,
        max_length_ratio=1.5,
    )


def test_llm_config_falls_back_to_defaults_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.yaml"

    config = load_llm_config(missing_path)

    assert config == LLMConfig(
        model=DEFAULT_LLM_MODEL,
        prompt_version=DEFAULT_PROMPT_VERSION,
        min_length_ratio=DEFAULT_MIN_LENGTH_RATIO,
        max_length_ratio=DEFAULT_MAX_LENGTH_RATIO,
    )


def test_llm_config_falls_back_to_defaults_when_llm_section_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("paths:\n  input_root: /tmp/notes_raw\n")

    config = load_llm_config(config_path)

    assert config == LLMConfig(
        model=DEFAULT_LLM_MODEL,
        prompt_version=DEFAULT_PROMPT_VERSION,
        min_length_ratio=DEFAULT_MIN_LENGTH_RATIO,
        max_length_ratio=DEFAULT_MAX_LENGTH_RATIO,
    )


def test_llm_config_falls_back_to_defaults_when_individual_top_level_keys_missing(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('llm:\n  model: "claude-3-5-sonnet-20241022"\n')

    config = load_llm_config(config_path)

    assert config == LLMConfig(
        model="claude-3-5-sonnet-20241022",
        prompt_version=DEFAULT_PROMPT_VERSION,
        min_length_ratio=DEFAULT_MIN_LENGTH_RATIO,
        max_length_ratio=DEFAULT_MAX_LENGTH_RATIO,
    )


def test_llm_config_falls_back_to_defaults_when_validation_section_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('llm:\n  model: "claude-3-5-sonnet-20241022"\n')

    config = load_llm_config(config_path)

    assert config.min_length_ratio == DEFAULT_MIN_LENGTH_RATIO
    assert config.max_length_ratio == DEFAULT_MAX_LENGTH_RATIO


def test_llm_config_falls_back_to_defaults_when_individual_validation_keys_missing(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("llm:\n  validation:\n    min_length_ratio: 0.6\n")

    config = load_llm_config(config_path)

    assert config == LLMConfig(
        model=DEFAULT_LLM_MODEL,
        prompt_version=DEFAULT_PROMPT_VERSION,
        min_length_ratio=0.6,
        max_length_ratio=DEFAULT_MAX_LENGTH_RATIO,
    )


def test_loads_output_config_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "output:\n"
        "  course_tags:\n"
        "    18.06_Linear_Algebra:\n"
        '      - "linear-algebra"\n'
        "  date_format: \"%d-%m-%Y\"\n"
        "  figures_dark_mode_flag: true\n"
    )

    config = load_output_config(config_path)

    assert config == OutputConfig(
        course_tags={"18.06_Linear_Algebra": ("linear-algebra",)},
        date_format="%d-%m-%Y",
        figures_dark_mode_flag=True,
        image_link_syntax=DEFAULT_IMAGE_LINK_SYNTAX,
    )


def test_output_config_falls_back_to_defaults_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.yaml"

    config = load_output_config(missing_path)

    assert config == OutputConfig(
        course_tags={},
        date_format=DEFAULT_DATE_FORMAT,
        figures_dark_mode_flag=DEFAULT_FIGURES_DARK_MODE_FLAG,
        image_link_syntax=DEFAULT_IMAGE_LINK_SYNTAX,
    )


def test_output_config_falls_back_to_defaults_when_output_section_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("paths:\n  input_root: /tmp/notes_raw\n")

    config = load_output_config(config_path)

    assert config == OutputConfig(
        course_tags={},
        date_format=DEFAULT_DATE_FORMAT,
        figures_dark_mode_flag=DEFAULT_FIGURES_DARK_MODE_FLAG,
        image_link_syntax=DEFAULT_IMAGE_LINK_SYNTAX,
    )


def test_output_config_falls_back_to_defaults_when_individual_keys_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('output:\n  date_format: "%d-%m-%Y"\n')

    config = load_output_config(config_path)

    assert config == OutputConfig(
        course_tags={},
        date_format="%d-%m-%Y",
        figures_dark_mode_flag=DEFAULT_FIGURES_DARK_MODE_FLAG,
        image_link_syntax=DEFAULT_IMAGE_LINK_SYNTAX,
    )


def test_output_config_course_tags_absent_defaults_to_empty_dict(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('output:\n  date_format: "%Y-%m-%d"\n')

    config = load_output_config(config_path)

    assert config.course_tags == {}


def test_output_config_course_tags_present_for_one_course_only(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "output:\n"
        "  course_tags:\n"
        "    class_1:\n"
        '      - "class-1-only"\n'
    )

    config = load_output_config(config_path)

    assert config.course_tags == {"class_1": ("class-1-only",)}


def test_output_config_image_link_syntax_defaults_to_markdown(tmp_path):
    missing_path = tmp_path / "does_not_exist.yaml"

    config = load_output_config(missing_path)

    assert config.image_link_syntax == "markdown" == DEFAULT_IMAGE_LINK_SYNTAX


def test_output_config_image_link_syntax_obsidian_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("output:\n  image_link_syntax: obsidian\n")

    config = load_output_config(config_path)

    assert config.image_link_syntax == "obsidian"


def test_output_config_image_link_syntax_invalid_value_falls_back_to_default(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("output:\n  image_link_syntax: bogus\n")

    config = load_output_config(config_path)

    assert config.image_link_syntax == DEFAULT_IMAGE_LINK_SYNTAX


def test_loads_naming_config_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('naming:\n  lecture_prefix: "Lec"\n')

    config = load_naming_config(config_path)

    assert config == NamingConfig(lecture_prefix="Lec")


def test_naming_config_falls_back_to_defaults_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.yaml"

    config = load_naming_config(missing_path)

    assert config == NamingConfig(lecture_prefix=DEFAULT_LECTURE_PREFIX)


def test_naming_config_falls_back_to_defaults_when_naming_section_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("paths:\n  input_root: /tmp/notes_raw\n")

    config = load_naming_config(config_path)

    assert config == NamingConfig(lecture_prefix=DEFAULT_LECTURE_PREFIX)


def test_naming_config_falls_back_to_defaults_when_lecture_prefix_key_missing(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("naming:\n  unrelated_key: \"foo\"\n")

    config = load_naming_config(config_path)

    assert config == NamingConfig(lecture_prefix=DEFAULT_LECTURE_PREFIX)


def test_default_print_summary_is_false():
    """
    DEFAULT_PRINT_SUMMARY itself is False -- print_summary is off unless
    config.yaml explicitly opts in (confirmed design decision).
    """
    assert DEFAULT_PRINT_SUMMARY is False


def test_default_hide_up_to_date_is_false():
    """
    DEFAULT_HIDE_UP_TO_DATE itself is False -- RichReporter shows every
    discovered file's row unless config.yaml explicitly opts in.
    """
    assert DEFAULT_HIDE_UP_TO_DATE is False


def test_loads_cli_config_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("cli:\n  print_summary: true\n  hide_up_to_date: true\n")

    config = load_cli_config(config_path)

    assert config == CLIConfig(print_summary=True, hide_up_to_date=True)


def test_cli_config_falls_back_to_defaults_when_file_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.yaml"

    config = load_cli_config(missing_path)

    assert config == CLIConfig(
        print_summary=DEFAULT_PRINT_SUMMARY, hide_up_to_date=DEFAULT_HIDE_UP_TO_DATE
    )


def test_cli_config_falls_back_to_defaults_when_cli_section_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("paths:\n  input_root: /tmp/notes_raw\n")

    config = load_cli_config(config_path)

    assert config == CLIConfig(
        print_summary=DEFAULT_PRINT_SUMMARY, hide_up_to_date=DEFAULT_HIDE_UP_TO_DATE
    )


def test_cli_config_falls_back_to_defaults_when_print_summary_key_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("cli:\n  unrelated_key: \"foo\"\n")

    config = load_cli_config(config_path)

    assert config == CLIConfig(
        print_summary=DEFAULT_PRINT_SUMMARY, hide_up_to_date=DEFAULT_HIDE_UP_TO_DATE
    )


def test_cli_config_falls_back_to_defaults_when_hide_up_to_date_key_missing(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("cli:\n  print_summary: true\n")

    config = load_cli_config(config_path)

    assert config == CLIConfig(print_summary=True, hide_up_to_date=DEFAULT_HIDE_UP_TO_DATE)


def test_cli_config_explicit_false_is_respected(tmp_path):
    """
    An explicit `print_summary: false`/`hide_up_to_date: false` in
    config.yaml behaves identically to either being absent.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text("cli:\n  print_summary: false\n  hide_up_to_date: false\n")

    config = load_cli_config(config_path)

    assert config == CLIConfig(print_summary=False, hide_up_to_date=False)
