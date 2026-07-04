"""
Phase 1/2/3 config loading.

Responsible for loading MATHPIX_APP_ID / MATHPIX_APP_KEY from .env, the
mathpix: poll_interval_seconds / max_poll_attempts settings from config.yaml
(falling back to hardcoded defaults if config.yaml or the section/keys are
absent), the paths: input_root / cache_dir / state_db settings from
config.yaml (input_root is required, cache_dir/state_db fall back to
hardcoded defaults), and the llm: model / prompt_version /
validation.min_length_ratio / validation.max_length_ratio settings from
config.yaml (all optional, same fully-optional fallback pattern as the
mathpix: section).

paths.vault_root (already present in config.yaml/config.example.yaml) stays
unread until Phase 4/5. Full config.yaml wiring for the remaining sections
(per-course tags, etc.) arrives in Phase 6 — kept out of scope here
intentionally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Matches the project convention of running the CLI from the repo root
# (config.yaml lives at the repo root, is gitignored/machine-specific — see
# config.example.yaml for the template and AGENTS.md for details).
DEFAULT_CONFIG_PATH = Path("config.yaml")

DEFAULT_POLL_INTERVAL_SECONDS: float = 5
DEFAULT_MAX_POLL_ATTEMPTS: int = 60

# Repo-root-relative defaults, matching the project convention of running
# the CLI from the repo root (same convention as DEFAULT_CONFIG_PATH above).
DEFAULT_CACHE_DIR = Path("_cache")
DEFAULT_STATE_DB = Path("state.db")

# Phase 3 — LLM cleanup defaults. See AGENTS.md's Phase 3 "Current Phase"
# notes for why Claude Haiku 4.5 was chosen over docs/spec.md's GPT-4o-mini
# suggestion.
DEFAULT_LLM_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_PROMPT_VERSION = "cleanup_v1"
DEFAULT_MIN_LENGTH_RATIO: float = 0.70
DEFAULT_MAX_LENGTH_RATIO: float = 1.30


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class MathpixCredentials:
    app_id: str
    app_key: str


@dataclass(frozen=True)
class MathpixPollingConfig:
    poll_interval_seconds: float
    max_poll_attempts: int


@dataclass(frozen=True)
class PathsConfig:
    input_root: Path
    cache_dir: Path
    state_db: Path


@dataclass(frozen=True)
class LLMConfig:
    model: str
    prompt_version: str
    min_length_ratio: float
    max_length_ratio: float


def load_mathpix_credentials(env_file: str | None = None) -> MathpixCredentials:
    """
    Load Mathpix API credentials from the environment (via .env).

    Args:
        env_file: optional explicit path to a .env file. If not given,
            python-dotenv searches upward from the current working directory
            for a .env file, matching the project convention of running the
            CLI from the repo root.

    Raises:
        ConfigError: if MATHPIX_APP_ID or MATHPIX_APP_KEY is missing or blank.
    """
    load_dotenv(dotenv_path=env_file)

    app_id = os.environ.get("MATHPIX_APP_ID", "").strip()
    app_key = os.environ.get("MATHPIX_APP_KEY", "").strip()

    missing = [
        name
        for name, value in (("MATHPIX_APP_ID", app_id), ("MATHPIX_APP_KEY", app_key))
        if not value
    ]
    if missing:
        raise ConfigError(
            "Missing required Mathpix credential(s) in environment/.env: "
            f"{', '.join(missing)}"
        )

    return MathpixCredentials(app_id=app_id, app_key=app_key)


def load_mathpix_polling_config(
    config_path: str | Path | None = None,
) -> MathpixPollingConfig:
    """
    Load the mathpix: poll_interval_seconds / max_poll_attempts settings from
    config.yaml.

    Args:
        config_path: optional explicit path to config.yaml. If not given,
            defaults to DEFAULT_CONFIG_PATH (config.yaml in the current
            working directory), matching the project convention of running
            the CLI from the repo root.

    config.yaml (and the mathpix: section/keys within it) is optional here —
    if the file doesn't exist, or the section/keys are missing, the
    corresponding hardcoded default (DEFAULT_POLL_INTERVAL_SECONDS /
    DEFAULT_MAX_POLL_ATTEMPTS) is used instead.
    """
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH

    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_poll_attempts: int = DEFAULT_MAX_POLL_ATTEMPTS

    if path.is_file():
        with path.open("r") as fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}
        mathpix_section = data.get("mathpix") or {}
        poll_interval_seconds = mathpix_section.get(
            "poll_interval_seconds", poll_interval_seconds
        )
        max_poll_attempts = mathpix_section.get("max_poll_attempts", max_poll_attempts)

    return MathpixPollingConfig(
        poll_interval_seconds=poll_interval_seconds,
        max_poll_attempts=max_poll_attempts,
    )


def load_paths_config(config_path: str | Path | None = None) -> PathsConfig:
    """
    Load the paths: input_root / cache_dir / state_db settings from
    config.yaml.

    Args:
        config_path: optional explicit path to config.yaml. If not given,
            defaults to DEFAULT_CONFIG_PATH (config.yaml in the current
            working directory), matching the project convention of running
            the CLI from the repo root.

    Unlike load_mathpix_polling_config(), config.yaml itself is *not*
    optional here: paths.input_root has no sensible default, so a missing
    file, a missing paths: section, or a missing/blank input_root key all
    raise ConfigError. cache_dir/state_db are optional and independently
    fall back to DEFAULT_CACHE_DIR/DEFAULT_STATE_DB when absent.

    paths.vault_root is deliberately not read here (unneeded until
    Phase 4/5) even though it's already present in config.yaml.

    Raises:
        ConfigError: if config.yaml is missing, the paths: section is
            missing, or paths.input_root is missing/blank.
    """
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH

    if not path.is_file():
        raise ConfigError(f"config.yaml not found at {path} (paths.input_root is required)")

    with path.open("r") as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}

    paths_section = data.get("paths") or {}

    input_root = str(paths_section.get("input_root", "")).strip()
    if not input_root:
        raise ConfigError(f"Missing required paths.input_root in {path}")

    cache_dir = paths_section.get("cache_dir") or DEFAULT_CACHE_DIR
    state_db = paths_section.get("state_db") or DEFAULT_STATE_DB

    return PathsConfig(
        input_root=Path(input_root),
        cache_dir=Path(cache_dir),
        state_db=Path(state_db),
    )


def load_llm_config(config_path: str | Path | None = None) -> LLMConfig:
    """
    Load the llm: model / prompt_version / validation.min_length_ratio /
    validation.max_length_ratio settings from config.yaml.

    Args:
        config_path: optional explicit path to config.yaml. If not given,
            defaults to DEFAULT_CONFIG_PATH (config.yaml in the current
            working directory), matching the project convention of running
            the CLI from the repo root.

    config.yaml (and the llm: section/keys within it) is fully optional
    here, same fallback pattern as load_mathpix_polling_config(): if the
    file doesn't exist, the llm: section is absent, the validation:
    subsection is absent, or individual keys are missing, the corresponding
    hardcoded default (DEFAULT_LLM_MODEL / DEFAULT_PROMPT_VERSION /
    DEFAULT_MIN_LENGTH_RATIO / DEFAULT_MAX_LENGTH_RATIO) is used instead.
    Unlike load_paths_config(), nothing here ever raises ConfigError — every
    value has a sensible default, unlike paths.input_root.

    prompt_version is config-driven (not a hardcoded code constant) so the
    active cleanup prompt (prompts/{prompt_version}.txt) can be swapped by
    editing config.yaml alone. Note that this is deliberately never compared
    against state.db's stored llm_prompt_version by
    needs_llm_reprocessing() — see AGENTS.md's Phase 3 notes.
    """
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH

    model: str = DEFAULT_LLM_MODEL
    prompt_version: str = DEFAULT_PROMPT_VERSION
    min_length_ratio: float = DEFAULT_MIN_LENGTH_RATIO
    max_length_ratio: float = DEFAULT_MAX_LENGTH_RATIO

    if path.is_file():
        with path.open("r") as fh:
            data: dict[str, Any] = yaml.safe_load(fh) or {}
        llm_section = data.get("llm") or {}
        model = llm_section.get("model", model)
        prompt_version = llm_section.get("prompt_version", prompt_version)
        validation_section = llm_section.get("validation") or {}
        min_length_ratio = validation_section.get("min_length_ratio", min_length_ratio)
        max_length_ratio = validation_section.get("max_length_ratio", max_length_ratio)

    return LLMConfig(
        model=model,
        prompt_version=prompt_version,
        min_length_ratio=min_length_ratio,
        max_length_ratio=max_length_ratio,
    )
