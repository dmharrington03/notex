# NoTeX

NoTeX is a Python CLI tool that scans a directory of handwritten lecture note
PDFs, runs them through the Mathpix API for OCR (text, LaTeX, and figures),
cleans up the extracted text with an LLM, and writes organized Markdown, with the intended application
organization in an Obsidian vault. After a class or study session, a handwritten note PDF is saved, then the script is
run, digitizing the notes into professional quality typsetting.