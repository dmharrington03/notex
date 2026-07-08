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
"""

from pathlib import Path

import pytest

from src.config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_LLM_MODEL,
    DEFAULT_MAX_LENGTH_RATIO,
    DEFAULT_MAX_POLL_ATTEMPTS,
    DEFAULT_MIN_LENGTH_RATIO,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_PROMPT_VERSION,
    DEFAULT_STATE_DB,
    ConfigError,
    LLMConfig,
    MathpixPollingConfig,
    PathsConfig,
    load_llm_config,
    load_mathpix_polling_config,
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
