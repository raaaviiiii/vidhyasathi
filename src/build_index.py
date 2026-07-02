"""Build (or rebuild) the whole knowledge base from every supported document
in data/docs/ (pdf, docx, pptx, txt, md, csv, xlsx). Single entry point.
"""
from src.ingest import (list_documents, load_document, source_label,
                        unsupported_files)
from src.chunk import chunk_records
from src.embed import embed_texts
from src.store import reset_collection, upsert_chunks, count, close


def build():
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
    upsert_chunks(all_chunks, vectors)
    print(f"Stored {count()} chunks from {len(docs)} document(s) in the KB.")
    close()


if __name__ == "__main__":
    build()
