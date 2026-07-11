"""
Unit tests for src/reporting.py (issue #47 -- Reporter protocol +
PlainReporter; issue #48 -- --verbose wiring for on_detail()).

PlainReporter is designed to reproduce, byte-for-byte, the exact print()
output src/main.py used to produce directly before the #47 refactor -- these
tests assert on that exact text for a representative set of stage/status
transitions, plus the free-form-message fallback and the "ungrouped_skip"
special case. on_detail() is a no-op by default (verbose=False) -- issue #48
adds a verbose=True constructor param that makes it actually print, tested
separately below. on_done() is verified as a pure no-op regardless (reserved
for a future RichReporter, issue #49).
"""

from __future__ import annotations

from src.reporting import PlainReporter


def test_on_stage_canonical_token_renders_exact_text(capsys):
    reporter = PlainReporter()

    reporter.on_stage("/notes_raw/class_1/lecture_01.pdf", "submitting:new")

    out = capsys.readouterr().out
    assert out == "[class_1] lecture_01.pdf: processing (new)...\n"


def test_on_stage_covers_every_canonical_transition(capsys):
    reporter = PlainReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    cases = {
        "would_process:new": "would process (new)",
        "would_process:changed": "would process (changed)",
        "would_process:retry": "would process (retry)",
        "would_reprocess_llm": "would reprocess LLM stage only",
        "would_retry_vault": "would retry vault write (force_vault_overwrite)",
        "submitting:new": "processing (new)...",
        "submitting:changed": "processing (changed)...",
        "submitting:retry": "processing (retry)...",
        "done:no_llm": "done (LLM stage skipped, --no-llm)",
        "done:llm_success": "done (LLM cleanup succeeded)",
        "done:llm_fallback": "done (LLM cleanup fell back to raw output)",
        "retrying_vault_write": "retrying vault write (force_vault_overwrite)...",
        "reprocessing_llm": "reprocessing LLM stage only...",
        "llm_only:llm_success": "LLM cleanup succeeded",
        "llm_only:llm_fallback": "LLM cleanup fell back to raw output",
    }

    for token, expected_text in cases.items():
        reporter.on_stage(source_path, token)
        out = capsys.readouterr().out
        assert out == f"[class_1] lecture_01.pdf: {expected_text}\n", token


def test_on_stage_unrecognized_token_is_printed_verbatim(capsys):
    """
    Free-form messages (exception text, delimiter warnings) aren't part of
    the canonical vocabulary -- PlainReporter falls back to printing them
    exactly as given, still prefixed with the derived [course] filename:
    label.
    """
    reporter = PlainReporter()

    reporter.on_stage(
        "/notes_raw/class_1/lecture_01.pdf",
        "FAILED: MathpixTimeoutError('too slow')",
    )

    out = capsys.readouterr().out
    assert out == "[class_1] lecture_01.pdf: FAILED: MathpixTimeoutError('too slow')\n"


def test_on_stage_derives_course_and_filename_from_source_path(capsys):
    reporter = PlainReporter()

    reporter.on_stage("/notes_raw/class_2/lecture_03.pdf", "done:llm_success")

    out = capsys.readouterr().out
    assert out == "[class_2] lecture_03.pdf: done (LLM cleanup succeeded)\n"


def test_on_stage_ungrouped_skip_uses_fixed_label_not_derived_from_path(capsys):
    """
    ungrouped_skip is special-cased: the displayed bracket is always the
    literal "ungrouped" marker, never the real parent directory name (which
    would just be input_root's own basename for a truly ungrouped file) --
    this is the common, tested case of a stray PDF discovered directly under
    input_root during a normal recursive scan.
    """
    reporter = PlainReporter()

    reporter.on_stage("/some/notes_raw/stray.pdf", "ungrouped_skip")

    out = capsys.readouterr().out
    assert out == (
        "[ungrouped] stray.pdf: skipping -- no course subfolder to group it "
        "under (not written to state.db)\n"
    )


def test_on_detail_is_a_no_op_by_default(capsys):
    reporter = PlainReporter()

    reporter.on_detail("/notes_raw/class_1/lecture_01.pdf", "mathpix pdf: poll 1/40 status=loaded")

    assert capsys.readouterr().out == ""


def test_on_detail_is_a_no_op_when_verbose_explicitly_false(capsys):
    reporter = PlainReporter(verbose=False)

    reporter.on_detail("/notes_raw/class_1/lecture_01.pdf", "mathpix pdf: poll 1/40 status=loaded")

    assert capsys.readouterr().out == ""


def test_on_detail_prints_when_verbose_true(capsys):
    reporter = PlainReporter(verbose=True)

    reporter.on_detail(
        "/notes_raw/class_1/lecture_01.pdf", "mathpix pdf: poll 1/40 status=loaded"
    )

    out = capsys.readouterr().out
    assert out == "    [class_1] lecture_01.pdf: mathpix pdf: poll 1/40 status=loaded\n"


def test_on_detail_verbose_derives_course_and_filename_from_source_path(capsys):
    reporter = PlainReporter(verbose=True)

    reporter.on_detail("/notes_raw/class_2/lecture_03.pdf", "copied figure: fig_001.jpg")

    out = capsys.readouterr().out
    assert out == "    [class_2] lecture_03.pdf: copied figure: fig_001.jpg\n"


def test_on_detail_verbose_message_printed_verbatim(capsys):
    """
    Unlike on_stage, on_detail has no canonical-token vocabulary -- message
    is always printed exactly as given.
    """
    reporter = PlainReporter(verbose=True)

    reporter.on_detail(
        "/notes_raw/class_1/lecture_01.pdf",
        "vault write confirmed: vault/class_1/Lecture 01.md (2 figure(s), 0 delimiter warning(s))",
    )

    out = capsys.readouterr().out
    assert out == (
        "    [class_1] lecture_01.pdf: vault write confirmed: "
        "vault/class_1/Lecture 01.md (2 figure(s), 0 delimiter warning(s))\n"
    )


def test_on_done_is_a_no_op(capsys):
    reporter = PlainReporter()

    reporter.on_done("/notes_raw/class_1/lecture_01.pdf", "success")

    assert capsys.readouterr().out == ""


def test_plain_reporter_satisfies_reporter_protocol():
    """
    Sanity check that PlainReporter structurally matches the Reporter
    Protocol (typing.Protocol -- no explicit inheritance required).
    """
    from src.reporting import Reporter

    reporter: Reporter = PlainReporter()
    # No assertion needed beyond "this type-checks and doesn't raise" --
    # calling each method once exercises the structural match at runtime.
    reporter.on_stage("/x/y.pdf", "done:llm_success")
    reporter.on_detail("/x/y.pdf", "detail")
    reporter.on_done("/x/y.pdf", "success")
