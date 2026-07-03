"""
Phase 1 config loading.

Responsible for loading MATHPIX_APP_ID / MATHPIX_APP_KEY from .env, plus a
couple of hardcoded polling defaults (poll_interval_seconds, max_poll_attempts).

Full config.yaml wiring (paths, LLM settings, per-course tags, etc.) arrives
in Phase 6 — kept out of scope here intentionally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class MathpixCredentials:
    app_id: str
    app_key: str


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
