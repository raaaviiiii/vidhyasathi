"""Chunk step: split records into overlapping windows, keeping source, locator,
and the ocr flag. Output: [{id, source, loc, order, text, ocr}].

Chunk ids embed the exact source label (not a lossy slug), so two different
documents can never produce the same id — "Notes 1" vs "notes-1", or two
Malayalam/Hindi file names, stay distinct.
"""
from src.config import CHUNK_SIZE, CHUNK_OVERLAP


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    text = text or ""
    if not text.strip():
        return []
    if len(text) <= size:
        return [text]
    text = text.strip()
    overlap = max(0, min(overlap, size - 1))
    chunks, start, n = [], 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:                                   # prefer to break on a space
            ls = text.rfind(" ", start, end)
            if ls > start + size * 0.5:
                end = ls
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:                                  # last window reached the end:
            break                                     # no redundant tail chunk
        start = max(end - overlap, start + 1)         # always make progress
    return chunks


def chunk_records(records, source="document"):
    out = []
    for rec in records:
        for j, piece in enumerate(chunk_text(rec.get("text", ""), CHUNK_SIZE, CHUNK_OVERLAP)):
            out.append({
                "id": f"{source}::{rec['order']}::{j}",
                "source": source,
                "loc": rec["loc"],
                "order": rec["order"],
                "text": piece,
                "ocr": rec.get("ocr", False),
            })
    return out
