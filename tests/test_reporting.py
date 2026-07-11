"""
Unit tests for src/reporting.py (issue #47 -- Reporter protocol +
PlainReporter; issue #48 -- --verbose wiring for on_detail(); issue #49 --
RichReporter + on_discover()/context-manager protocol additions).

PlainReporter is designed to reproduce, byte-for-byte, the exact print()
output src/main.py used to produce directly before the #47 refactor -- these
tests assert on that exact text for a representative set of stage/status
transitions, plus the free-form-message fallback and the "ungrouped_skip"
special case. on_detail() is a no-op by default (verbose=False) -- issue #48
adds a verbose=True constructor param that makes it actually print, tested
separately below. on_done() is verified as a pure no-op regardless (still
unwired as of issue #49 -- see RichReporter's own docstring).

RichReporter's tests (below) deliberately never assert on actual rendered
Rich output -- per AGENTS.md's testing conventions, they check its internal
_rows state dict and Reporter-protocol conformance instead.
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
    reporter.on_discover([("/x/y.pdf", "new")])
    reporter.on_stage("/x/y.pdf", "done:llm_success")
    reporter.on_detail("/x/y.pdf", "detail")
    reporter.on_done("/x/y.pdf", "success")


def test_plain_reporter_context_manager_is_a_no_op(capsys):
    reporter = PlainReporter()

    with reporter as entered:
        assert entered is reporter

    assert capsys.readouterr().out == ""


# --- Issue #49: RichReporter ---
#
# Per AGENTS.md's testing conventions, no test here asserts on actual
# rendered Rich terminal output -- only on RichReporter's internal state
# model (self._rows) and Reporter-protocol conformance, mirroring
# PlainReporter's tests' philosophy but checking state instead of stdout
# text.


def test_rich_reporter_satisfies_reporter_protocol():
    from src.reporting import Reporter, RichReporter

    reporter: Reporter = RichReporter()
    reporter.on_discover([("/x/y.pdf", "new")])
    reporter.on_stage("/x/y.pdf", "done:llm_success")
    reporter.on_detail("/x/y.pdf", "detail")
    reporter.on_done("/x/y.pdf", "success")


def test_rich_reporter_on_discover_seeds_rows_by_classification():
    from src.reporting import RichReporter

    reporter = RichReporter()
    reporter.on_discover(
        [
            ("/notes_raw/class_1/lecture_01.pdf", "new"),
            ("/notes_raw/class_1/lecture_02.pdf", "unchanged"),
            ("/notes_raw/class_1/lecture_03.pdf", "changed"),
            ("/notes_raw/class_1/lecture_04.pdf", "retry"),
        ]
    )

    rows = reporter._rows
    assert rows["/notes_raw/class_1/lecture_01.pdf"]["status"] == "waiting"
    assert rows["/notes_raw/class_1/lecture_02.pdf"]["status"] == "up to date"
    assert rows["/notes_raw/class_1/lecture_03.pdf"]["status"] == "waiting"
    assert rows["/notes_raw/class_1/lecture_04.pdf"]["status"] == "waiting"
    assert rows["/notes_raw/class_1/lecture_01.pdf"]["course"] == "class_1"
    assert rows["/notes_raw/class_1/lecture_01.pdf"]["filename"] == "lecture_01.pdf"


def test_rich_reporter_on_stage_updates_seeded_row():
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_discover([(source_path, "new")])

    reporter.on_stage(source_path, "submitting:new")
    assert reporter._rows[source_path]["status"] == "processing (new)..."

    reporter.on_stage(source_path, "done:llm_success")
    assert reporter._rows[source_path]["status"] == "done (LLM cleanup succeeded)"
    assert reporter._rows[source_path]["style"] == "green"


def test_rich_reporter_on_stage_can_add_a_row_not_seeded_by_on_discover():
    """
    Defensive: on_stage still works even without a prior on_discover call
    for that path (e.g. a test that only exercises on_stage directly).
    """
    from src.reporting import RichReporter

    reporter = RichReporter()
    reporter.on_stage("/notes_raw/class_2/lecture_03.pdf", "done:llm_fallback")

    row = reporter._rows["/notes_raw/class_2/lecture_03.pdf"]
    assert row["course"] == "class_2"
    assert row["filename"] == "lecture_03.pdf"
    assert row["status"] == "done (LLM cleanup fell back to raw output)"
    assert row["style"] == "yellow"


def test_rich_reporter_on_stage_ungrouped_skip_uses_fixed_label():
    from src.reporting import RichReporter

    reporter = RichReporter()
    reporter.on_stage("/some/notes_raw/stray.pdf", "ungrouped_skip")

    row = reporter._rows["/some/notes_raw/stray.pdf"]
    assert row["course"] == "ungrouped"


def test_rich_reporter_on_stage_failed_free_form_message_styled_as_error():
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_stage(source_path, "FAILED: MathpixTimeoutError('too slow')")

    row = reporter._rows[source_path]
    assert row["status"] == "FAILED: MathpixTimeoutError('too slow')"
    assert row["style"] == "bold red"


def test_rich_reporter_on_detail_no_op_by_default():
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_discover([(source_path, "new")])

    reporter.on_detail(source_path, "mathpix pdf: poll 1/40 status=loaded")

    assert reporter._rows[source_path]["detail"] is None


def test_rich_reporter_on_detail_sets_trailing_suffix_when_verbose():
    from src.reporting import RichReporter

    reporter = RichReporter(verbose=True)
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_discover([(source_path, "new")])

    reporter.on_detail(source_path, "mathpix pdf: poll 1/40 status=loaded")

    assert reporter._rows[source_path]["detail"] == "mathpix pdf: poll 1/40 status=loaded"


def test_rich_reporter_on_stage_clears_stale_detail_suffix():
    from src.reporting import RichReporter

    reporter = RichReporter(verbose=True)
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_discover([(source_path, "new")])
    reporter.on_detail(source_path, "mathpix pdf: poll 1/40 status=loaded")
    assert reporter._rows[source_path]["detail"] is not None

    reporter.on_stage(source_path, "done:llm_success")

    assert reporter._rows[source_path]["detail"] is None


def test_rich_reporter_on_done_is_a_no_op():
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_discover([(source_path, "new")])
    before = dict(reporter._rows[source_path])

    reporter.on_done(source_path, "success")

    assert reporter._rows[source_path] == before


def test_rich_reporter_context_manager_starts_and_stops_live():
    from src.reporting import RichReporter

    reporter = RichReporter()
    assert reporter._live.is_started is False

    with reporter as entered:
        assert entered is reporter
        assert reporter._live.is_started is True

    assert reporter._live.is_started is False
