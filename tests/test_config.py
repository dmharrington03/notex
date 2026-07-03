"""
Unit tests for src/config.py.

Covered here (issue #2 — config.yaml mathpix: polling defaults):
    - load_mathpix_polling_config() reads poll_interval_seconds /
      max_poll_attempts from a config.yaml's mathpix: section
    - falls back to DEFAULT_POLL_INTERVAL_SECONDS / DEFAULT_MAX_POLL_ATTEMPTS
      when the file is missing, or the mathpix: section/keys are absent
"""

from src.config import (
    DEFAULT_MAX_POLL_ATTEMPTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    MathpixPollingConfig,
    load_mathpix_polling_config,
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
