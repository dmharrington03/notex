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

Also covered here (issue #16 — validate_cleanup()):
    - one pass + one fail case per check (length_ratio, dollar_balance,
      left_right_balance, heading_count)
    - the relaxed-heading-decrease-still-passes case explicitly
    - the empty-original length_ratio edge case

Also covered here (issue #17 — cleanup_pdf() / needs_llm_reprocessing()):
    - success path writes {lecture_stem}.llm.md and returns a
      llm_status="success" LLMResult
    - fallback on LLMError (API failure) -- output_path points at the
      original mathpix_markdown_path, llm_model/llm_prompt_version/
      llm_validation_result are all None
    - fallback on failed validation -- same fallback shape, except
      llm_validation_result carries the (failing) checks dict
    - FileNotFoundError / LLMError (missing prompt file) propagate rather
      than being caught into the fallback
    - needs_llm_reprocessing()'s truth table

No network: completion_fn is always a fake/stub, never litellm.completion
itself (see AGENTS.md Testing Conventions).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import LLMConfig
from src.llm import (
    CompletionResult,
    LLMClient,
    LLMError,
    LLMResult,
    ValidationResult,
    cleanup_pdf,
    load_prompt_text,
    needs_llm_reprocessing,
    validate_cleanup,
)
from src.state import StateEntry


def _fake_response(
    content: str, prompt_tokens: int = 100, completion_tokens: int = 50
) -> SimpleNamespace:
    """Build a stub object shaped like litellm.completion()'s return value:
    response.choices[0].message.content, plus a response.usage matching
    litellm's real usage-reporting shape (issue #21)."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


def _fake_response_no_usage(content: str) -> SimpleNamespace:
    """Same as _fake_response() but without a `usage` attribute at all --
    exercises complete()'s best-effort fallback to None/None for
    input_tokens/output_tokens (issue #21)."""
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

    assert isinstance(result, CompletionResult)
    assert result.content == "cleaned markdown text"
    assert captured["model"] == "fake-model"
    assert captured["messages"] == [
        {"role": "system", "content": "system prompt text"},
        {"role": "user", "content": "raw markdown"},
    ]


def test_complete_captures_usage_from_response(monkeypatch):
    def fake_completion_fn(**kwargs):
        return _fake_response("cleaned text", prompt_tokens=123, completion_tokens=45)

    # completion_cost() itself is best-effort/independent of usage capture;
    # stub it directly here so this test asserts on token capture alone.
    monkeypatch.setattr("src.llm.litellm.completion_cost", lambda **kwargs: 0.0042)

    client = LLMClient(model="fake-model", completion_fn=fake_completion_fn)
    result = client.complete("sys", "user")

    assert result.input_tokens == 123
    assert result.output_tokens == 45
    assert result.cost == 0.0042


def test_complete_usage_and_cost_are_none_when_response_lacks_usage_attribute():
    def fake_completion_fn(**kwargs):
        return _fake_response_no_usage("cleaned text")

    client = LLMClient(model="fake-model", completion_fn=fake_completion_fn)
    result = client.complete("sys", "user")

    assert result.content == "cleaned text"
    assert result.input_tokens is None
    assert result.output_tokens is None


def test_complete_cost_is_none_when_completion_cost_raises(monkeypatch):
    def fake_completion_fn(**kwargs):
        return _fake_response("cleaned text")

    def raising_completion_cost(**kwargs):
        raise ValueError("no pricing info for this model")

    monkeypatch.setattr("src.llm.litellm.completion_cost", raising_completion_cost)

    client = LLMClient(model="fake-model", completion_fn=fake_completion_fn)
    result = client.complete("sys", "user")

    # Usage capture still succeeds independently of the cost lookup failing.
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cost is None


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
    assert client.complete("sys", "user").content == "ok"


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


# --- validate_cleanup() (issue #16) ---


def test_validate_cleanup_passes_when_all_checks_pass():
    original = "# Title\n\nSome text with $x = 1$ and \\left( y \\right)."
    cleaned = "# Title\n\nSome text with $x = 1$ and \\left( y \\right).\n"

    result = validate_cleanup(original, cleaned, min_length_ratio=0.7, max_length_ratio=1.3)

    assert isinstance(result, ValidationResult)
    assert result.passed is True
    assert result.checks == {
        "length_ratio": True,
        "dollar_balance": True,
        "left_right_balance": True,
        "heading_count": True,
    }


def test_validate_cleanup_fails_length_ratio_when_cleaned_too_short():
    original = "word " * 100
    cleaned = "word " * 10  # far below min_length_ratio

    result = validate_cleanup(original, cleaned, min_length_ratio=0.7, max_length_ratio=1.3)

    assert result.passed is False
    assert result.checks["length_ratio"] is False


def test_validate_cleanup_fails_length_ratio_when_cleaned_too_long():
    original = "word " * 10
    cleaned = "word " * 100  # far above max_length_ratio

    result = validate_cleanup(original, cleaned, min_length_ratio=0.7, max_length_ratio=1.3)

    assert result.passed is False
    assert result.checks["length_ratio"] is False


def test_validate_cleanup_length_ratio_empty_original_passes_only_if_cleaned_also_empty():
    passing = validate_cleanup("", "", min_length_ratio=0.7, max_length_ratio=1.3)
    failing = validate_cleanup("", "not empty", min_length_ratio=0.7, max_length_ratio=1.3)

    assert passing.checks["length_ratio"] is True
    assert failing.checks["length_ratio"] is False


def test_validate_cleanup_passes_dollar_balance_with_even_dollar_count():
    original = "text $x$ and $$y$$"
    cleaned = "text $x$ and $$y$$"

    result = validate_cleanup(original, cleaned, min_length_ratio=0.5, max_length_ratio=2.0)

    assert result.checks["dollar_balance"] is True


def test_validate_cleanup_fails_dollar_balance_with_odd_dollar_count():
    original = "text $x$ fine"
    cleaned = "text $x fine"  # missing closing $

    result = validate_cleanup(original, cleaned, min_length_ratio=0.1, max_length_ratio=2.0)

    assert result.passed is False
    assert result.checks["dollar_balance"] is False


def test_validate_cleanup_passes_left_right_balance_when_counts_equal():
    original = "\\left( a \\right)"
    cleaned = "\\left( a \\right) \\left[ b \\right]"

    result = validate_cleanup(original, cleaned, min_length_ratio=0.1, max_length_ratio=5.0)

    assert result.checks["left_right_balance"] is True


def test_validate_cleanup_fails_left_right_balance_when_counts_unequal():
    original = "\\left( a \\right)"
    cleaned = "\\left( a \\right) \\left[ b"  # missing a \\right

    result = validate_cleanup(original, cleaned, min_length_ratio=0.1, max_length_ratio=5.0)

    assert result.passed is False
    assert result.checks["left_right_balance"] is False


def test_validate_cleanup_left_right_balance_ignores_rightarrow_and_leftarrow():
    # \rightarrow / \leftarrow / \leftrightarrow start with \right/\left as
    # a literal prefix but are not the delimiter command -- must not be
    # miscounted as unbalanced \left/\right delimiters.
    original = "a \\rightarrow b"
    cleaned = "a \\rightarrow b, c \\leftrightarrow d"

    result = validate_cleanup(original, cleaned, min_length_ratio=0.1, max_length_ratio=5.0)

    assert result.checks["left_right_balance"] is True


def test_validate_cleanup_passes_heading_count_when_equal():
    original = "# Title\n\ntext"
    cleaned = "# Title\n\ncleaned text"

    result = validate_cleanup(original, cleaned, min_length_ratio=0.1, max_length_ratio=5.0)

    assert result.checks["heading_count"] is True


def test_validate_cleanup_passes_heading_count_when_cleaned_has_fewer_headings():
    # Relaxed check: dropping the single stray non-structural heading
    # artifact (prompts/cleanup_v1.txt's "Stray heading artifact" rule)
    # must still pass validation.
    original = "## Lecture 21-4/14\n\n# Real Section\n\ntext"
    cleaned = "# Real Section\n\ntext"

    result = validate_cleanup(original, cleaned, min_length_ratio=0.1, max_length_ratio=5.0)

    assert result.checks["heading_count"] is True


def test_validate_cleanup_fails_heading_count_when_cleaned_has_more_headings():
    original = "text with no headings"
    cleaned = "# Hallucinated Heading\n\ntext with no headings"

    result = validate_cleanup(original, cleaned, min_length_ratio=0.1, max_length_ratio=5.0)

    assert result.passed is False
    assert result.checks["heading_count"] is False


# --- cleanup_pdf() / needs_llm_reprocessing() (issue #17) ---


def _llm_config(**overrides) -> LLMConfig:
    defaults = dict(
        model="fake-model",
        prompt_version="cleanup_v1",
        min_length_ratio=0.1,
        max_length_ratio=5.0,
    )
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _state_entry(**overrides) -> StateEntry:
    defaults = dict(
        source_path="/notes_raw/class_1/lecture_01.pdf",
        source_hash="abc123",
        source_mtime=1234.5,
        source_size=4096,
        mathpix_pdf_id="pdf_xyz",
        mathpix_status="success",
        llm_model=None,
        llm_prompt_version=None,
        llm_status=None,
        llm_validation_result=None,
        figure_count=0,
        page_count=None,
        output_path=None,
        mathpix_processed_at=None,
        llm_processed_at=None,
        vault_written_at=None,
        llm_input_tokens=None,
        llm_output_tokens=None,
        llm_cost_estimate=None,
    )
    defaults.update(overrides)
    return StateEntry(**defaults)


def test_cleanup_pdf_success_writes_llm_md_and_returns_success_result(tmp_path, monkeypatch):
    mathpix_path = tmp_path / "lecture_01.mathpix.md"
    mathpix_path.write_text("# Lecture 21-4/14\n\nsome raw mathpix text", encoding="utf-8")
    dest_dir = tmp_path / "out"

    def fake_completion_fn(**kwargs):
        return _fake_response(
            "# Real Heading\n\nsome cleaned text", prompt_tokens=200, completion_tokens=80
        )

    monkeypatch.setattr("src.llm.litellm.completion_cost", lambda **kwargs: 0.0123)

    client = LLMClient(model="fake-model", completion_fn=fake_completion_fn)
    llm_config = _llm_config()

    result = cleanup_pdf(mathpix_path, dest_dir, "lecture_01", llm_config, client=client)

    assert isinstance(result, LLMResult)
    assert result.llm_status == "success"
    assert result.llm_model == "fake-model"
    assert result.llm_prompt_version == "cleanup_v1"
    assert result.output_path == dest_dir / "lecture_01.llm.md"
    assert result.output_path.read_text(encoding="utf-8") == "# Real Heading\n\nsome cleaned text"
    assert json.loads(result.llm_validation_result) == {
        "length_ratio": True,
        "dollar_balance": True,
        "left_right_balance": True,
        "heading_count": True,
    }
    assert result.llm_input_tokens == 200
    assert result.llm_output_tokens == 80
    assert result.llm_cost_estimate == 0.0123


def test_cleanup_pdf_falls_back_on_llm_error(tmp_path):
    mathpix_path = tmp_path / "lecture_01.mathpix.md"
    mathpix_path.write_text("some raw mathpix text", encoding="utf-8")
    dest_dir = tmp_path / "out"

    def failing_completion_fn(**kwargs):
        raise RuntimeError("boom")

    client = LLMClient(model="fake-model", completion_fn=failing_completion_fn)
    llm_config = _llm_config()

    result = cleanup_pdf(mathpix_path, dest_dir, "lecture_01", llm_config, client=client)

    assert result.llm_status == "failed"
    assert result.llm_model is None
    assert result.llm_prompt_version is None
    assert result.llm_validation_result is None
    assert result.output_path == mathpix_path
    # No completion was ever returned -- no usage to report.
    assert result.llm_input_tokens is None
    assert result.llm_output_tokens is None
    assert result.llm_cost_estimate is None
    # No new file written on failure.
    assert not dest_dir.exists()


def test_cleanup_pdf_falls_back_on_failed_validation(tmp_path, monkeypatch):
    mathpix_path = tmp_path / "lecture_01.mathpix.md"
    mathpix_path.write_text("word " * 100, encoding="utf-8")
    dest_dir = tmp_path / "out"

    def fake_completion_fn(**kwargs):
        return _fake_response(
            "word " * 10, prompt_tokens=50, completion_tokens=10
        )  # far below min_length_ratio

    monkeypatch.setattr("src.llm.litellm.completion_cost", lambda **kwargs: 0.002)

    client = LLMClient(model="fake-model", completion_fn=fake_completion_fn)
    llm_config = _llm_config(min_length_ratio=0.7, max_length_ratio=1.3)

    result = cleanup_pdf(mathpix_path, dest_dir, "lecture_01", llm_config, client=client)

    assert result.llm_status == "failed"
    assert result.llm_model is None
    assert result.llm_prompt_version is None
    assert result.output_path == mathpix_path
    checks = json.loads(result.llm_validation_result)
    assert checks["length_ratio"] is False
    assert not dest_dir.exists()
    # The completion call still happened and cost real money even though
    # validation failed and the output was discarded -- tokens/cost are
    # still recorded (issue #21).
    assert result.llm_input_tokens == 50
    assert result.llm_output_tokens == 10
    assert result.llm_cost_estimate == 0.002


def test_cleanup_pdf_raises_file_not_found_for_missing_mathpix_markdown(tmp_path):
    llm_config = _llm_config()
    client = LLMClient(model="fake-model", completion_fn=lambda **kwargs: _fake_response("x"))

    with pytest.raises(FileNotFoundError):
        cleanup_pdf(
            tmp_path / "does_not_exist.mathpix.md",
            tmp_path / "out",
            "lecture_01",
            llm_config,
            client=client,
        )


def test_cleanup_pdf_raises_llm_error_for_missing_prompt_file(tmp_path):
    mathpix_path = tmp_path / "lecture_01.mathpix.md"
    mathpix_path.write_text("raw text", encoding="utf-8")
    client = LLMClient(model="fake-model", completion_fn=lambda **kwargs: _fake_response("x"))
    llm_config = _llm_config(prompt_version="nonexistent_version")

    with pytest.raises(LLMError, match="Prompt file not found"):
        cleanup_pdf(mathpix_path, tmp_path / "out", "lecture_01", llm_config, client=client)


def test_cleanup_pdf_constructs_its_own_client_when_none_given(tmp_path, monkeypatch):
    mathpix_path = tmp_path / "lecture_01.mathpix.md"
    mathpix_path.write_text("raw text", encoding="utf-8")
    dest_dir = tmp_path / "out"
    llm_config = _llm_config()

    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _fake_response("cleaned text")

    monkeypatch.setattr("litellm.completion", fake_completion)

    result = cleanup_pdf(mathpix_path, dest_dir, "lecture_01", llm_config)

    assert result.llm_status == "success"
    assert captured["model"] == "fake-model"


def test_needs_llm_reprocessing_true_when_never_run():
    entry = _state_entry(llm_status=None)
    assert needs_llm_reprocessing(entry) is True


def test_needs_llm_reprocessing_true_when_failed():
    entry = _state_entry(llm_status="failed")
    assert needs_llm_reprocessing(entry) is True


def test_needs_llm_reprocessing_false_when_successful_and_up_to_date():
    entry = _state_entry(llm_status="success", llm_prompt_version="cleanup_v1")
    assert needs_llm_reprocessing(entry) is False


def test_needs_llm_reprocessing_false_when_successful_with_stale_prompt_version():
    # Deliberate: switching config.yaml's llm.prompt_version must not
    # silently trigger reprocessing of already-successful files -- see
    # AGENTS.md's "Deliberate correction to docs/spec.md's Reprocessing
    # logic table".
    entry = _state_entry(llm_status="success", llm_prompt_version="cleanup_v0_old")
    assert needs_llm_reprocessing(entry) is False
