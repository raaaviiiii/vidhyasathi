"""Ingest step: read a PDF into clean, per-page text.
This is stage 1 of the pipeline. It does NOT chunk or embed yet —
it just turns a document into a list of {page, text} records.
"""
import re
import sys
from pathlib import Path

from pypdf import PdfReader

from src.config import DOCS_DIR


def clean(text: str) -> str:
    """Collapse ALL whitespace (spaces, tabs, and stray newlines) into single
    spaces, so word-per-line PDF extraction artifacts become flowing text.
    We process page-by-page, so real page structure is still preserved.
    """
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)   # any run of whitespace -> one space
    return text.strip()

def load_pdf(path: Path) -> list[dict]:
    """Return a list of {'page': int, 'text': str} for non-empty pages."""
    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        raw = page.extract_text() or ""
        text = clean(raw)
        if text:                       # skip blank / image-only pages
            pages.append({"page": i, "text": text})
    return pages


def _first_pdf() -> Path | None:
    pdfs = sorted(DOCS_DIR.glob("*.pdf"))
    return pdfs[0] if pdfs else None


if __name__ == "__main__":
    # Use a path given on the command line, else the first PDF in data/docs/
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else _first_pdf()

    if target is None or not target.exists():
        print("No PDF found. Drop one into data/docs/ or pass a path.")
        sys.exit(1)

    pages = load_pdf(target)
    print(f"File:  {target.name}")
    print(f"Pages with text: {len(pages)}")
    if pages:
        preview = pages[0]["text"][:300]
        print(f"\n--- page {pages[0]['page']} preview ---\n{preview}...")