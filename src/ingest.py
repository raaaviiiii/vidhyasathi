"""Ingest step: a LOADER REGISTRY that turns any supported document into the
common record shape {loc, order, text[, ocr]}.

Digital formats load their text directly. Anything with no extractable text
(scanned PDF pages, image files, handwriting) falls through to the adaptive
OCR engine (src/ocr.py) and is tagged ocr=True so the answer step can add a
"check the equation" nudge when needed.
"""
import csv
import io
import re
from pathlib import Path

import pymupdf
from docx import Document
from pptx import Presentation
from openpyxl import load_workbook
from PIL import Image

from src.config import DOCS_DIR, OCR_DPI, OCR_MIN_TEXT
from src.errors import IngestError


def clean(text: str) -> str:
    text = text.replace("\x00", " ")
    return re.sub(r"\s+", " ", text).strip()


# ---------- digital loaders ----------
def _ocr(png: bytes) -> str:
    from src import ocr                                  # lazy: only import the API here
    return clean(ocr.ocr_image(png))


def _load_pdf(path: Path, warnings: list | None = None) -> list[dict]:
    """Per page: use embedded text if present, else OCR the rendered page.
    A page OCR can't read is skipped with a warning; the rest still index."""
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        raise IngestError("Couldn't open this PDF — it may be corrupt or password-protected.") from e
    if doc.needs_pass:
        raise IngestError("This PDF is password-protected. Remove the password and upload again.")
    out, failed = [], []
    for i, page in enumerate(doc, start=1):
        t = clean(page.get_text() or "")
        if len(t) >= OCR_MIN_TEXT:                       # real digital text
            out.append({"loc": f"p{i}", "order": i, "text": t})
            continue
        try:                                             # scanned / handwritten
            ocr_t = _ocr(page.get_pixmap(dpi=OCR_DPI).tobytes("png"))
        except Exception:
            failed.append(i)
            continue
        if ocr_t:
            out.append({"loc": f"p{i}", "order": i, "text": ocr_t, "ocr": True})
    if failed and warnings is not None:
        pages = ", ".join(f"p{i}" for i in failed[:10]) + ("…" if len(failed) > 10 else "")
        warnings.append(f"{len(failed)} scanned page(s) couldn't be read by OCR ({pages}) "
                        "and were skipped.")
    return out


def _load_docx(path: Path) -> list[dict]:
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
    for tbl in doc.tables:
        for row in tbl.rows:
            line = clean(" | ".join(c.text for c in row.cells))
            if line:
                order += 1
                out.append({"loc": "table", "order": order, "text": line})
    return out


def _shape_text(sh, parts):
    """Text from a shape, recursing into grouped shapes."""
    if getattr(sh, "shape_type", None) == 6 or hasattr(sh, "shapes"):   # GROUP
        for inner in getattr(sh, "shapes", []):
            _shape_text(inner, parts)
        return
    if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
        for para in sh.text_frame.paragraphs:
            line = "".join(r.text for r in para.runs)
            if line.strip():
                parts.append(line)
    if getattr(sh, "has_table", False) and sh.has_table:
        for row in sh.table.rows:
            parts.append(" | ".join(c.text for c in row.cells))


def _load_pptx(path: Path) -> list[dict]:
    out = []
    for i, slide in enumerate(Presentation(str(path)).slides, start=1):
        parts = []
        for sh in slide.shapes:
            _shape_text(sh, parts)
        t = clean("\n".join(parts))
        if t:
            out.append({"loc": f"slide {i}", "order": i, "text": t})
    return out


def _decode(raw: bytes) -> str:
    """UTF-8 (with/without BOM), UTF-16 (Notepad 'Unicode'), else Windows-1252."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _load_text(path: Path) -> list[dict]:
    t = clean(_decode(path.read_bytes()))
    return [{"loc": "text", "order": 1, "text": t}] if t else []


def _load_csv(path: Path) -> list[dict]:
    out = []
    rows = list(csv.reader(io.StringIO(_decode(path.read_bytes()), newline="")))
    if not rows:
        return out
    header = [h.strip() for h in rows[0]]
    for i, row in enumerate(rows[1:], start=1):
        cells = []
        for j, v in enumerate(row):
            if not str(v).strip():
                continue
            h = header[j] if j < len(header) and header[j] else f"col {j + 1}"
            cells.append(f"{h}: {v}")
        line = "; ".join(cells)
        if line:
            out.append({"loc": f"row {i}", "order": i, "text": line})
    return out


def _load_xlsx(path: Path) -> list[dict]:
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


# ---------- image loader (pure OCR) ----------
def _load_image(path: Path, warnings: list | None = None) -> list[dict]:
    try:
        img = Image.open(path).convert("RGB")
    except Exception as e:
        raise IngestError("Couldn't open this image — it may be corrupt.") from e
    buf = io.BytesIO(); img.save(buf, format="PNG")
    try:
        text = _ocr(buf.getvalue())
    except Exception as e:
        raise IngestError("Couldn't read the text in this image: OCR is unavailable "
                          "(check ANTHROPIC_API_KEY and your internet connection).") from e
    return [{"loc": "image", "order": 1, "text": text, "ocr": True}] if text else []


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tiff", ".bmp"}
LOADERS = {
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".pptx": _load_pptx,
    ".txt": _load_text,
    ".md": _load_text,
    ".csv": _load_csv,
    ".xlsx": _load_xlsx,
    **{e: _load_image for e in IMAGE_EXTS},
}
SUPPORTED = set(LOADERS)


def list_documents() -> list[Path]:
    return sorted(p for p in DOCS_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in SUPPORTED)


def unsupported_files() -> list[Path]:
    return sorted(p for p in DOCS_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() not in SUPPORTED
                  and not p.name.startswith("."))


def load_document(path: Path, warnings: list | None = None) -> list[dict]:
    """Load any supported file. Raises IngestError with a student-safe message if the
    file can't be read; non-fatal problems are appended to `warnings`."""
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise IngestError(f"Unsupported file type: {path.suffix or '(none)'}")
    try:
        if loader in (_load_pdf, _load_image):
            return loader(path, warnings)
        return loader(path)
    except IngestError:
        raise
    except Exception as e:
        raise IngestError(f"Couldn't read this {path.suffix.lstrip('.').upper()} file — "
                          "it may be corrupt or in an unexpected format.") from e


def source_label(path: Path) -> str:
    return re.sub(r"[\s_]+", " ", path.stem).strip()


if __name__ == "__main__":
    for d in list_documents():
        recs = load_document(d)
        n_ocr = sum(1 for r in recs if r.get("ocr"))
        print(f"{source_label(d)} ({d.suffix}) -> {len(recs)} records"
              f"{f'  [{n_ocr} via OCR]' if n_ocr else ''}")
