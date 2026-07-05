"""
LLM cleanup client — Phase 3, issue #15.

Thin wrapper around litellm.completion() plus prompt-text loading. This
module deliberately does not implement the cleanup orchestration itself
(cleanup_pdf(), issue #17) or post-cleanup validation (validate_cleanup(),
issue #16) -- just the pieces those depend on:

    - LLMClient          thin, injectable litellm.completion() wrapper
    - LLMError           raised on completion failures / bad responses
    - load_prompt_text() reads prompts/{prompt_version}.txt

Implementation status:
    - LLMClient / complete()  implemented (issue #15)
    - load_prompt_text()      implemented (issue #15)
    - validate_cleanup()      not yet implemented (issue #16)
    - cleanup_pdf()           not yet implemented (issue #17)
    - needs_llm_reprocessing() not yet implemented (issue #17)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import litellm
from dotenv import load_dotenv


class LLMError(Exception):
    """Raised on LLM completion failures, malformed responses, or a missing
    prompt file for a configured prompt_version."""


class LLMClient:
    """
    Thin wrapper around litellm.completion().

    completion_fn is constructor-injectable (mirrors MathpixClient's
    http_client= pattern -- see src/mathpix.py) so tests supply a fake and
    never hit a real API (see AGENTS.md Testing Conventions).
    """

    def __init__(
        self,
        model: str,
        completion_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self._completion_fn = completion_fn or litellm.completion

        # Ensure ANTHROPIC_API_KEY (or whatever credential the configured
        # model needs) is present in os.environ before completion_fn is
        # ever called, regardless of whether load_mathpix_credentials() has
        # already run earlier in this process (it can't be relied on to run
        # first once #17/#18 land -- a run touching only the LLM stage for
        # already-Mathpix-processed files would otherwise never call
        # load_dotenv() at all). Matches load_mathpix_credentials()'s own
        # unconditional load_dotenv() call in src/config.py. Deliberately
        # does not read/validate the key itself -- that stays litellm's job
        # (a missing key surfaces as whatever exception completion_fn
        # raises, wrapped into LLMError by complete() below), per
        # AGENTS.md's "no credential-loading logic in config.py" note.
        load_dotenv()

    def complete(self, system_prompt: str, user_content: str) -> str:
        """
        Run a single chat completion and return the assistant's response
        text.

        Args:
            system_prompt: the system message content (e.g. the loaded
                cleanup prompt text from load_prompt_text()).
            user_content: the user message content (e.g. the raw Mathpix
                Markdown to clean up).

        Returns:
            The completion's message content, verbatim.

        Raises:
            LLMError: if completion_fn raises for any reason, if the
                response doesn't have the expected
                response.choices[0].message.content shape, or if that
                content is empty/whitespace-only.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        try:
            response = self._completion_fn(model=self.model, messages=messages)
        except Exception as exc:
            raise LLMError(f"LLM completion failed: {exc}") from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {response!r}") from exc

        if not content or not content.strip():
            raise LLMError("LLM completion returned empty content")

        return content


def load_prompt_text(
    prompt_version: str,
    prompts_dir: str | Path = Path("prompts"),
) -> str:
    """
    Read prompts/{prompt_version}.txt and return its contents verbatim.

    Args:
        prompt_version: e.g. "cleanup_v1" (see config.yaml's
            llm.prompt_version -- src/config.py's load_llm_config()).
        prompts_dir: directory containing versioned prompt files. Defaults
            to "prompts" relative to the current working directory,
            matching the project convention of running the CLI from the
            repo root (same convention as DEFAULT_CONFIG_PATH in
            src/config.py).

    Raises:
        LLMError: if prompts/{prompt_version}.txt doesn't exist -- a
            configured prompt_version with no matching file is a real
            config error, not silently ignorable.
    """
    path = Path(prompts_dir) / f"{prompt_version}.txt"
    if not path.is_file():
        raise LLMError(f"Prompt file not found for prompt_version={prompt_version!r}: {path}")

    return path.read_text(encoding="utf-8")
