"""Chunk step: split page texts into overlapping windows for embedding.
Stage 2 of the pipeline. Input = the {page, text} records from ingest;
output = {id, page, text} chunks ready to embed.
"""
from src.config import CHUNK_SIZE, CHUNK_OVERLAP


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Slice one string into windows of ~`size` chars that overlap by `overlap`.
    Tries to break on a space near the window end so we don't cut mid-word.
    """
    if len(text) <= size:
        return [text]

    chunks, start = [], 0
    while start < len(text):
        end = start + size
        window = text[start:end]

        # if we're not at the very end, back up to the last space for a clean break
        if end < len(text):
            last_space = window.rfind(" ")
            if last_space > size * 0.5:      # only if the space isn't too early
                window = window[:last_space]
                end = start + last_space

        chunk = window.strip()
        if chunk:
            chunks.append(chunk)

        start = end - overlap                # step forward, keeping the overlap
        if start < 0:
            start = 0
    return chunks


def chunk_pages(pages: list[dict]) -> list[dict]:
    """Turn [{page, text}] into [{id, page, text}] flat list of chunks."""
    out = []
    for rec in pages:
        pieces = chunk_text(rec["text"], CHUNK_SIZE, CHUNK_OVERLAP)
        for j, piece in enumerate(pieces):
            out.append({
                "id": f"p{rec['page']}_c{j}",   # e.g. p1_c0 = page 1, chunk 0
                "page": rec["page"],
                "text": piece,
            })
    return out


if __name__ == "__main__":
    from src.ingest import load_pdf, _first_pdf

    target = _first_pdf()
    if target is None:
        print("No PDF in data/docs/.")
        raise SystemExit(1)

    pages = load_pdf(target)
    chunks = chunk_pages(pages)

    print(f"File:   {target.name}")
    print(f"Pages:  {len(pages)}   ->   Chunks: {len(chunks)}")
    print(f"\n--- first chunk ({chunks[0]['id']}, page {chunks[0]['page']}) ---")
    print(chunks[0]["text"])
    print(f"\n--- chunk sizes (first 8): {[len(c['text']) for c in chunks[:8]]}")