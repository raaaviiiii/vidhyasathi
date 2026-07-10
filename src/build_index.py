"""Build (or rebuild) the whole knowledge base from every supported document
in data/docs/ (pdf, docx, pptx, txt, md, csv, xlsx). Single entry point.

Two paths:
  build(owner=None)        — full rebuild from data/docs/ (CLI seeding).
  ingest_file(path, user)  — add/replace ONE file for ONE user, in-process, no reset.
                             This is what the web upload endpoint calls (same Qdrant
                             client as the running server, so no embedded-lock clash).
"""
from pathlib import Path

from src.ingest import (list_documents, load_document, source_label,
                        unsupported_files, SUPPORTED)
from src.chunk import chunk_records
from src.embed import embed_texts
from src.store import (reset_collection, upsert_chunks, count, close,
                       delete_source)


def ingest_file(path, user: str | None = None) -> dict:
    """Index a single already-saved file for `user`, replacing any prior copy.
    Returns {ok, source, chunks, ocr}. Does NOT close the shared client."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED:
        return {"ok": False, "source": path.name, "chunks": 0,
                "error": f"unsupported file type: {path.suffix}"}
    src = source_label(path)
    recs = load_document(path)
    chunks = chunk_records(recs, source=src)
    if not chunks:
        return {"ok": False, "source": src, "chunks": 0,
                "error": "no extractable text (empty or unreadable file)"}
    vectors = embed_texts([c["text"] for c in chunks])
    delete_source(user, src)                 # idempotent: re-upload overwrites
    upsert_chunks(chunks, vectors, user=user)
    n_ocr = sum(1 for c in chunks if c.get("ocr"))
    return {"ok": True, "source": src, "chunks": len(chunks), "ocr": n_ocr}


def build(owner: str | None = None):
    docs = list_documents()
    if not docs:
        print("No supported documents in data/docs/.")
        return

    print(f"Found {len(docs)} document(s):")
    all_chunks = []
    for path in docs:
        src = source_label(path)
        recs = load_document(path)
        chunks = chunk_records(recs, source=src)
        all_chunks.extend(chunks)
        print(f"  - {src} ({path.suffix}): {len(recs)} records -> {len(chunks)} chunks")

    skipped = unsupported_files()
    if skipped:
        print("skipped (unsupported for now):",
              ", ".join(p.name for p in skipped))

    print(f"\nTotal chunks: {len(all_chunks)}")
    print("Embedding all chunks ...")
    vectors = embed_texts([c["text"] for c in all_chunks])

    reset_collection()
    upsert_chunks(all_chunks, vectors, user=owner)
    who = f" (owner: {owner})" if owner else " (shared / untagged)"
    print(f"Stored {count(owner)} chunks from {len(docs)} document(s) in the KB{who}.")
    close()


if __name__ == "__main__":
    import sys
    # optional: python -m src.build_index --owner you@example.com
    owner = None
    if "--owner" in sys.argv:
        i = sys.argv.index("--owner")
        owner = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    build(owner=owner)
