"""
Mathpix API client — Phase 1.

Given a single PDF path, submits it to Mathpix, polls until processing
completes, downloads the md.zip conversion bundle, extracts it, renames
figures to the lecture_N_fig_NNN convention, rewrites the Markdown image
references to match, and writes the result to _cache/.

See AGENTS.md "Mathpix API notes" for the verified API behavior this should
be built against (status values, multipart upload shape, figure handling via
md.zip, unconfirmed math delimiter format).

TODO(phase-1): implement MathpixClient (submit / poll_until_complete /
fetch_and_extract) and the process_pdf() orchestration function.
"""
