"""Chunk step: split records into overlapping windows, keeping source, locator,
and the ocr flag. Output: [{id, source, loc, order, text, ocr}].
"""
import re
from src.config import CHUNK_SIZE, CHUNK_OVERLAP


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "doc"


def chunk_text(text, size, overlap):
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = start + size
        window = text[start:end]
        if end < len(text):
            ls = window.rfind(" ")
            if ls > size * 0.5:
                window = window[:ls]; end = start + ls
        c = window.strip()
        if c:
            chunks.append(c)
        start = end - overlap
        if start < 0:
            start = 0
    return chunks


def chunk_records(records, source="document"):
    slug = _slug(source)
    out = []
    for rec in records:
        for j, piece in enumerate(chunk_text(rec["text"], CHUNK_SIZE, CHUNK_OVERLAP)):
            out.append({
                "id": f"{slug}_{rec['order']}_c{j}",
                "source": source,
                "loc": rec["loc"],
                "order": rec["order"],
                "text": piece,
                "ocr": rec.get("ocr", False),
            })
    return out
