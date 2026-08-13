"""
Unit tests for src/reporting.py (issue #47 -- Reporter protocol +
PlainReporter; issue #48 -- --verbose wiring for on_detail(); issue #49 --
RichReporter + on_discover()/context-manager protocol additions, plus a
follow-up adding the "editing:llm" stage and turning on_done() into a
once-per-run completion signal).

PlainReporter is designed to reproduce, byte-for-byte, the exact print()
output src/main.py used to produce directly before the #47 refactor -- these
tests assert on that exact text for a representative set of stage/status
transitions, plus the free-form-message fallback and the "ungrouped_skip"
special case. on_detail() is a no-op by default (verbose=False) -- issue #48
adds a verbose=True constructor param that makes it actually print, tested
separately below. on_done() is no longer a no-op (a #49 follow-up) -- it's a
once-per-run completion signal called by main() with the run's total
duration; PlainReporter prints a trailing "Finished in X.XX s" line.

RichReporter's tests (below) deliberately never assert on actual rendered
Rich output -- per AGENTS.md's testing conventions, they check its internal
_rows state dict, the Table/Panel objects _render() constructs (title/
subtitle text, not rendered pixels), and Reporter-protocol conformance
instead.
"""

from __future__ import annotations

from src.reporting import PlainReporter


def test_on_stage_canonical_token_renders_exact_text(capsys):
    reporter = PlainReporter()

    reporter.on_stage("/notes_raw/class_1/lecture_01.pdf", "submitting:new")

    out = capsys.readouterr().out
    assert out == "[class_1] lecture_01.pdf: Processing (new)...\n"


def test_on_stage_covers_every_canonical_transition(capsys):
    reporter = PlainReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    cases = {
        "would_process:new": "Would process (new)",
        "would_process:changed": "Would process (changed)",
        "would_process:retry": "Would process (retry)",
        "would_reprocess_llm": "Would reprocess LLM stage only",
        "would_retry_vault": "Would retry vault write (force_vault_overwrite)",
        "submitting:new": "Processing (new)...",
        "submitting:changed": "Processing (changed)...",
        "submitting:retry": "Processing (retry)...",
        "editing:llm": "Editing...",
        "done:no_llm": "Done (LLM stage skipped, --no-llm)",
        "done:llm_success": "✓ Done",
        "done:llm_fallback": "Done (LLM cleanup fell back to raw output)",
        "retrying_vault_write": "Retrying vault write (force_vault_overwrite)...",
        "reprocessing_llm": "Reprocessing LLM stage only...",
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
    assert out == "[class_2] lecture_03.pdf: ✓ Done\n"


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


def test_on_done_prints_runtime(capsys):
    """
    #49 follow-up: on_done() is now a once-per-run completion signal
    (runtime_secs, not the original per-file (source_path, status)) --
    PlainReporter prints a trailing "Finished in X.XX s" line.
    """
    reporter = PlainReporter()

    reporter.on_done(runtime_secs=12.345)

    assert capsys.readouterr().out == "\nFinished in 12.35 s\n"


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
    reporter.on_done(runtime_secs=1.0)


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
    reporter.on_done(runtime_secs=1.0)


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
    # Filenames are shown without their extension (Path.stem, not .name) --
    # a formatting choice for the table's File column.
    assert rows["/notes_raw/class_1/lecture_01.pdf"]["filename"] == "lecture_01"


def test_rich_reporter_on_discover_hides_up_to_date_rows_when_flag_set():
    """
    Issue #53: with hide_up_to_date=True, an "unchanged" classification is
    not added as a row at all -- it's tallied in _hidden_count instead.
    Other classifications are unaffected.
    """
    from src.reporting import RichReporter

    reporter = RichReporter(hide_up_to_date=True)
    reporter.on_discover(
        [
            ("/notes_raw/class_1/lecture_01.pdf", "new"),
            ("/notes_raw/class_1/lecture_02.pdf", "unchanged"),
            ("/notes_raw/class_1/lecture_03.pdf", "changed"),
            ("/notes_raw/class_1/lecture_04.pdf", "unchanged"),
        ]
    )

    rows = reporter._rows
    assert "/notes_raw/class_1/lecture_02.pdf" not in rows
    assert "/notes_raw/class_1/lecture_04.pdf" not in rows
    assert rows["/notes_raw/class_1/lecture_01.pdf"]["status"] == "waiting"
    assert rows["/notes_raw/class_1/lecture_03.pdf"]["status"] == "waiting"
    assert reporter._hidden_count == 2


def test_rich_reporter_on_discover_shows_up_to_date_rows_when_flag_unset():
    """
    Default (hide_up_to_date=False) is unaffected -- "unchanged" rows are
    still seeded exactly as before, and nothing is tallied as hidden.
    """
    from src.reporting import RichReporter

    reporter = RichReporter()
    reporter.on_discover([("/notes_raw/class_1/lecture_02.pdf", "unchanged")])

    assert "/notes_raw/class_1/lecture_02.pdf" in reporter._rows
    assert reporter._hidden_count == 0


def test_rich_reporter_render_caption_reflects_hidden_count():
    """
    _render()'s Table caption shows "(N) files already up to date" once
    hide_up_to_date=True has hidden at least one row, and is None (no
    caption at all) when nothing has been hidden.
    """
    from src.reporting import RichReporter

    reporter = RichReporter(hide_up_to_date=True)
    table = reporter._render().renderable.renderable
    assert table.caption is None

    reporter.on_discover(
        [
            ("/notes_raw/class_1/lecture_01.pdf", "new"),
            ("/notes_raw/class_1/lecture_02.pdf", "unchanged"),
        ]
    )
    table = reporter._render().renderable.renderable
    assert table.caption == "(1) file already up to date"

    reporter.on_discover([("/notes_raw/class_1/lecture_03.pdf", "unchanged")])
    table = reporter._render().renderable.renderable
    assert table.caption == "(2) files already up to date"


def test_rich_reporter_render_no_caption_when_flag_unset():
    from src.reporting import RichReporter

    reporter = RichReporter()
    reporter.on_discover([("/notes_raw/class_1/lecture_02.pdf", "unchanged")])

    table = reporter._render().renderable.renderable
    assert table.caption is None


def test_rich_reporter_on_stage_updates_seeded_row():
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_discover([(source_path, "new")])

    reporter.on_stage(source_path, "submitting:new")
    assert reporter._rows[source_path]["status"] == "Processing (new)..."

    reporter.on_stage(source_path, "done:llm_success")
    assert reporter._rows[source_path]["status"] == "✓ Done"
    assert reporter._rows[source_path]["style"] == "green"


def test_rich_reporter_full_lifecycle_waiting_processing_editing_done():
    """
    The full waiting -> processing -> editing -> done progression a
    live-updating reporter observes for one actionable (NEW/CHANGED/RETRY)
    file: seeded as "waiting" by on_discover, "processing (...)" during the
    Mathpix stage (submitting:*), "Editing..." once the LLM stage begins (a
    #49 follow-up), and finally a "done" outcome.
    """
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    reporter.on_discover([(source_path, "new")])
    assert reporter._rows[source_path]["status"] == "waiting"
    assert reporter._rows[source_path]["spinner"] is None

    reporter.on_stage(source_path, "submitting:new")
    assert reporter._rows[source_path]["status"] == "Processing (new)..."
    assert reporter._rows[source_path]["spinner"] == "white"

    reporter.on_stage(source_path, "editing:llm")
    assert reporter._rows[source_path]["status"] == "Editing..."
    assert reporter._rows[source_path]["spinner"] == "yellow"

    reporter.on_stage(source_path, "done:llm_success")
    assert reporter._rows[source_path]["status"] == "✓ Done"
    assert reporter._rows[source_path]["spinner"] is None


# --- Spinner (a #49 follow-up): submitting:*/editing:llm/reprocessing_llm/
# retrying_vault_write render as an animated rich.spinner.Spinner instead of
# plain text, cleared back to None the moment a row reaches any other
# (terminal) stage. ---


def test_rich_reporter_spinner_set_for_every_in_progress_stage():
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"

    cases = {
        "submitting:new": "white",
        "submitting:changed": "white",
        "submitting:retry": "white",
        "editing:llm": "yellow",
        "reprocessing_llm": "yellow",
        "retrying_vault_write": "cyan",
    }
    for stage, expected_color in cases.items():
        reporter.on_stage(source_path, stage)
        assert reporter._rows[source_path]["spinner"] == expected_color, stage


def test_rich_reporter_spinner_cleared_on_terminal_stage():
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_stage(source_path, "editing:llm")
    assert reporter._rows[source_path]["spinner"] == "yellow"

    reporter.on_stage(source_path, "done:llm_fallback")

    assert reporter._rows[source_path]["spinner"] is None


def test_rich_reporter_on_discover_never_seeds_a_spinner():
    from src.reporting import RichReporter

    reporter = RichReporter()
    reporter.on_discover(
        [
            ("/x/a.pdf", "new"),
            ("/x/b.pdf", "unchanged"),
        ]
    )

    assert reporter._rows["/x/a.pdf"]["spinner"] is None
    assert reporter._rows["/x/b.pdf"]["spinner"] is None


def test_rich_reporter_render_uses_a_spinner_renderable_for_in_progress_row():
    from rich.spinner import Spinner

    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_stage(source_path, "editing:llm")

    rendered = reporter._render()
    table = rendered.renderable.renderable
    cell = table.columns[2]._cells[0]

    assert isinstance(cell, Spinner)
    assert cell.name == "dots"
    assert cell.style == "yellow"
    assert cell.text.plain == "Editing..."


def test_rich_reporter_render_uses_plain_text_for_terminal_row():
    from rich.spinner import Spinner

    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_stage(source_path, "done:llm_success")

    rendered = reporter._render()
    table = rendered.renderable.renderable
    cell = table.columns[2]._cells[0]

    assert not isinstance(cell, Spinner)
    assert cell == "[green]\u2713 Done[/green]"


def test_rich_reporter_spinner_includes_verbose_detail_suffix():
    from src.reporting import RichReporter

    reporter = RichReporter(verbose=True)
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_stage(source_path, "editing:llm")

    reporter.on_detail(source_path, "using model claude-haiku-4-5")

    rendered = reporter._render()
    table = rendered.renderable.renderable
    cell = table.columns[2]._cells[0]

    assert cell.text.plain == "Editing... -- using model claude-haiku-4-5"


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
    assert row["filename"] == "lecture_03"
    assert row["status"] == "Done (LLM cleanup fell back to raw output)"
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


def test_rich_reporter_on_done_does_not_change_row_state():
    """
    #49 follow-up: on_done() is now a once-per-run completion signal, not a
    per-file one -- it only affects the Panel's subtitle (see
    test_rich_reporter_render_title_and_subtitle below), never any row's
    state.
    """
    from src.reporting import RichReporter

    reporter = RichReporter()
    source_path = "/notes_raw/class_1/lecture_01.pdf"
    reporter.on_discover([(source_path, "new")])
    before = dict(reporter._rows[source_path])

    reporter.on_done(runtime_secs=1.0)

    assert reporter._rows[source_path] == before


def test_rich_reporter_render_title_reflects_row_count():
    """
    The Table's title shows a live "{N} documents found" count derived from
    len(self._rows) -- not a hardcoded string -- so it stays accurate as
    files are discovered.
    """
    from src.reporting import RichReporter

    reporter = RichReporter()
    rendered = reporter._render()
    table = rendered.renderable.renderable
    assert table.title == "(0) documents found"

    reporter.on_discover([("/x/a.pdf", "new")])
    rendered = reporter._render()
    table = rendered.renderable.renderable
    assert table.title == "(1) document found"

    reporter.on_discover([("/x/b.pdf", "new")])
    rendered = reporter._render()
    table = rendered.renderable.renderable
    assert table.title == "(2) documents found"


def test_rich_reporter_render_subtitle_reflects_on_done():
    """
    _render()'s subtitle param (set by on_done() via _refresh()) becomes
    the wrapping Panel's subtitle.
    """
    from src.reporting import RichReporter

    reporter = RichReporter()

    rendered = reporter._render()
    panel = rendered.renderable
    assert panel.subtitle is None

    reporter.on_done(runtime_secs=12.3)

    panel = reporter._live.renderable.renderable
    assert panel.subtitle == "Done in 12.3 s"


def test_rich_reporter_context_manager_starts_and_stops_live():
    from src.reporting import RichReporter

    reporter = RichReporter()
    assert reporter._live.is_started is False

    with reporter as entered:
        assert entered is reporter
        assert reporter._live.is_started is True

    assert reporter._live.is_started is False
