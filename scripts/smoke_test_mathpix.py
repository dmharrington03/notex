"""
Manual smoke test — hits the REAL Mathpix API and costs money per run.

Not part of the pytest suite. Run by hand against a real lecture PDF to
validate actual OCR output quality (text, LaTeX, figures) before trusting
the pipeline against a full course of notes.

Usage (once implemented):
    python scripts/smoke_test_mathpix.py path/to/lecture_01.pdf --out _cache/smoke_test/

TODO(phase-1): implement once src/mathpix.py's process_pdf() exists.
"""
