"""Ingest step: a LOADER REGISTRY that turns any supported document into a
common record shape: {loc, order, text}.
  loc   = human-readable location for citations (p3, "slide 4", "Marks row 2",
          a heading) — generalises "page" across formats.
  order = integer for stable in-document ordering.
Adding a new format = write one loader and register it in LOADERS. Nothing
downstream changes.
"""
import csv
import re
from pathlib import Path

from pypdf import PdfReader
from docx import Document
from pptx import Presentation
from openpyxl import load_workbook

from src.config import DOCS_DIR


def clean(text: str) -> str:
    text = text.replace("\x00", " ")
    return re.sub(r"\s+", " ", text).strip()


def _load_pdf(path: Path) -> list[dict]:
    out = []
    for i, page in enumerate(PdfReader(str(path)).pages, start=1):
        t = clean(page.extract_text() or "")
        if t:
            out.append({"loc": f"p{i}", "order": i, "text": t})
    return out


def _load_docx(path: Path) -> list[dict]:
    """Segment a Word doc by heading; each section becomes one record."""
    doc = Document(str(path))
    out, buf, order, sec = [], [], 0, 0
    label = "body"

    def flush():
        nonlocal order
        if buf:
            t = clean(" ".join(buf))
            if t:
                order += 1
                out.append({"loc": label, "order": order, "text": t})

    for p in doc.paragraphs:
        style = (p.style.name or "").lower()
        if style.startswith("heading") or style.startswith("title"):
            flush(); buf.clear(); sec += 1
            label = clean(p.text)[:40] or f"section {sec}"
        elif p.text.strip():
            buf.append(p.text)
    flush()

    for tbl in doc.tables:                      # capture table rows too
        for row in tbl.rows:
            line = clean(" | ".join(c.text for c in row.cells))
            if line:
                order += 1
                out.append({"loc": "table", "order": order, "text": line})
    return out


def _load_pptx(path: Path) -> list[dict]:
    out = []
    for i, slide in enumerate(Presentation(str(path)).slides, start=1):
        parts = []
        for sh in slide.shapes:
            if sh.has_text_frame:
                for para in sh.text_frame.paragraphs:
                    line = "".join(r.text for r in para.runs)
                    if line.strip():
                        parts.append(line)
            if sh.has_table:
                for row in sh.table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
        t = clean("\n".join(parts))
        if t:
            out.append({"loc": f"slide {i}", "order": i, "text": t})
    return out


def _load_text(path: Path) -> list[dict]:
    t = clean(path.read_text(encoding="utf-8", errors="ignore"))
    return [{"loc": "text", "order": 1, "text": t}] if t else []


def _load_csv(path: Path) -> list[dict]:
    out = []
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        rows = list(csv.reader(f))
    if not rows:
        return out
    header = rows[0]
    for i, row in enumerate(rows[1:], start=1):
        line = "; ".join(f"{h}: {v}" for h, v in zip(header, row) if str(v).strip())
        if line:
            out.append({"loc": f"row {i}", "order": i, "text": line})
    return out


def _load_xlsx(path: Path) -> list[dict]:
    """Each row -> 'Header: value; ...' so tabular data is retrievable."""
    wb = load_workbook(str(path), read_only=True, data_only=True)
    out, order = [], 0
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header = [str(c) if c is not None else "" for c in rows[0]]
        for r, row in enumerate(rows[1:], start=1):
            line = "; ".join(f"{h}: {v}" for h, v in zip(header, row) if v not in (None, ""))
            if line:
                order += 1
                out.append({"loc": f"{ws.title} row {r}", "order": order, "text": line})
    return out


# --- the registry: extension -> loader ---
LOADERS = {
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".pptx": _load_pptx,
    ".txt": _load_text,
    ".md": _load_text,
    ".csv": _load_csv,
    ".xlsx": _load_xlsx,
}
SUPPORTED = set(LOADERS)


def list_documents() -> list[Path]:
    """Every supported document in data/docs/, sorted."""
    return sorted(p for p in DOCS_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in SUPPORTED)


def unsupported_files() -> list[Path]:
    """Files present but not (yet) loadable — e.g. .doc, .xls, images, scans."""
    return sorted(p for p in DOCS_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() not in SUPPORTED
                  and not p.name.startswith("."))


def load_document(path: Path) -> list[dict]:
    return LOADERS[path.suffix.lower()](path)


def source_label(path: Path) -> str:
    return re.sub(r"[\s_]+", " ", path.stem).strip()


if __name__ == "__main__":
    docs = list_documents()
    print(f"{len(docs)} supported document(s):")
    for d in docs:
        recs = load_document(d)
        print(f"  {source_label(d)} ({d.suffix}) -> {len(recs)} records")
    skipped = unsupported_files()
    if skipped:
        print("skipped (unsupported):", ", ".join(p.name for p in skipped))
