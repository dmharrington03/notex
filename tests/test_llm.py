"""
Unit tests for src/llm.py.

Covered here (issue #15 — LLMClient + prompt loading):
    - LLMClient.complete() calls the injected completion_fn with the
      expected model/messages shape and returns the response's message
      content
    - LLMClient.complete() wraps completion_fn exceptions, malformed
      response shapes, and empty/whitespace-only content into LLMError
    - load_prompt_text() reads prompts/{prompt_version}.txt verbatim and
      raises LLMError when the file doesn't exist

No network: completion_fn is always a fake/stub, never litellm.completion
itself (see AGENTS.md Testing Conventions).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.llm import LLMClient, LLMError, load_prompt_text


def _fake_response(content: str) -> SimpleNamespace:
    """Build a stub object shaped like litellm.completion()'s return value:
    response.choices[0].message.content."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_complete_returns_message_content_and_calls_completion_fn_correctly():
    captured = {}

    def fake_completion_fn(**kwargs):
        captured.update(kwargs)
        return _fake_response("cleaned markdown text")

    client = LLMClient(model="fake-model", completion_fn=fake_completion_fn)
    result = client.complete("system prompt text", "raw markdown")

    assert result == "cleaned markdown text"
    assert captured["model"] == "fake-model"
    assert captured["messages"] == [
        {"role": "system", "content": "system prompt text"},
        {"role": "user", "content": "raw markdown"},
    ]


def test_complete_wraps_completion_fn_exception_in_llm_error():
    def failing_completion_fn(**kwargs):
        raise RuntimeError("boom")

    client = LLMClient(model="fake-model", completion_fn=failing_completion_fn)

    with pytest.raises(LLMError, match="LLM completion failed"):
        client.complete("system prompt", "user content")


def test_complete_raises_llm_error_on_malformed_response_shape():
    def malformed_completion_fn(**kwargs):
        return SimpleNamespace(choices=[])  # no [0] to index into

    client = LLMClient(model="fake-model", completion_fn=malformed_completion_fn)

    with pytest.raises(LLMError, match="Unexpected LLM response shape"):
        client.complete("system prompt", "user content")


def test_complete_raises_llm_error_on_missing_choices_attribute():
    def malformed_completion_fn(**kwargs):
        return {"not": "the expected shape"}

    client = LLMClient(model="fake-model", completion_fn=malformed_completion_fn)

    with pytest.raises(LLMError, match="Unexpected LLM response shape"):
        client.complete("system prompt", "user content")


@pytest.mark.parametrize("empty_content", ["", "   ", "\n\t"])
def test_complete_raises_llm_error_on_empty_content(empty_content):
    def empty_completion_fn(**kwargs):
        return _fake_response(empty_content)

    client = LLMClient(model="fake-model", completion_fn=empty_completion_fn)

    with pytest.raises(LLMError, match="empty content"):
        client.complete("system prompt", "user content")


def test_llm_client_construction_does_not_require_a_real_api_key(monkeypatch):
    # completion_fn is always faked, so LLMClient() must not require
    # ANTHROPIC_API_KEY to actually be set for construction to succeed --
    # its load_dotenv() call is defensive/best-effort, not validating.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fake_completion_fn(**kwargs):
        return _fake_response("ok")

    client = LLMClient(model="fake-model", completion_fn=fake_completion_fn)
    assert client.complete("sys", "user") == "ok"


def test_load_prompt_text_reads_real_cleanup_v1_prompt():
    text = load_prompt_text("cleanup_v1", prompts_dir=Path("prompts"))

    assert "copy editor" in text
    assert len(text) > 0


def test_load_prompt_text_reads_from_custom_prompts_dir(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "my_version.txt").write_text("a test prompt\n", encoding="utf-8")

    text = load_prompt_text("my_version", prompts_dir=prompts_dir)

    assert text == "a test prompt\n"


def test_load_prompt_text_raises_llm_error_when_file_missing(tmp_path):
    with pytest.raises(LLMError, match="Prompt file not found"):
        load_prompt_text("nonexistent_version", prompts_dir=tmp_path)
