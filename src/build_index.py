"""Indexing entry points.

  ingest_file(path, user)  — add/replace ONE file for ONE student (the web upload path).
                             Identity is the exact file name, so notes.pdf and notes.docx
                             are two documents. The new version is written first and the
                             old one removed only after that succeeds.
  build(owner=None)        — (re)seed one bucket from every document in data/docs/.
                             Only that bucket is replaced; other students are untouched.

CLI:  python -m src.build_index                 # seed the shared bucket
      python -m src.build_index --owner a@x.com  # seed one student's bucket
"""
import uuid
from pathlib import Path

from src.ingest import (list_documents, load_document, source_label,
                        unsupported_files, SUPPORTED)
from src.chunk import chunk_records
from src.embed import embed_texts
from src.errors import IngestError
from src.store import upsert_chunks, count, close, _delete, user_filter, _user_cond
from qdrant_client.models import (FieldCondition, Filter, IsEmptyCondition, MatchValue,
                                  PayloadField)


def _drop_other_versions(user, doc, keep_ver, source=None):
    """Retire every other version of this document — including chunks written before
    documents carried a `doc` field (matched by their label)."""
    from src.store import _doc_cond
    _delete(Filter(must=[_user_cond(user), _doc_cond(doc)],
                   must_not=[FieldCondition(key="ver", match=MatchValue(value=keep_ver))]))
    if source:
        _delete(Filter(must=[_user_cond(user),
                             FieldCondition(key="source", match=MatchValue(value=source)),
                             IsEmptyCondition(is_empty=PayloadField(key="doc"))]))


def _drop_version(user, doc, ver):
    from src.store import _doc_cond
    try:
        _delete(user_filter(user, _doc_cond(doc),
                            FieldCondition(key="ver", match=MatchValue(value=ver))))
    except Exception:
        pass


def ingest_file(path, user: str | None = None, doc: str | None = None) -> dict:
    """Index one saved file for `user`, replacing any previous copy of the same file.

    Returns {ok, doc, source, chunks, ocr, warnings[, error]}. A file that can't be
    read returns ok=False with a message safe to show the student. Storage failures
    (e.g. Qdrant down) raise, and leave the previous version intact."""
    path = Path(path)
    doc = doc or path.name
    src = source_label(Path(doc))            # citations use the real name, not a temp file
    base = {"doc": doc, "source": src, "chunks": 0, "ocr": 0, "warnings": []}
    if path.suffix.lower() not in SUPPORTED:
        return {**base, "ok": False,
                "error": f"That file type isn't supported (unsupported file type: {path.suffix or 'none'})."}
    warnings: list[str] = []
    try:
        recs = load_document(path, warnings)
    except IngestError as e:
        return {**base, "ok": False, "error": str(e), "warnings": warnings}
    chunks = chunk_records(recs, source=src)
    if not chunks:
        msg = "This file has no extractable text (it may be empty or image-only)."
        if warnings:
            msg += " " + " ".join(warnings)
        return {**base, "ok": False, "error": msg, "warnings": warnings}

    vectors = embed_texts([c["text"] for c in chunks])
    ver = uuid.uuid4().hex
    try:
        upsert_chunks(chunks, vectors, user=user, doc=doc, ver=ver)
    except Exception:
        _drop_version(user, doc, ver)        # no half-written version left behind
        raise
    try:
        _drop_other_versions(user, doc, ver, source=src)   # only now retire the old copy
    except Exception:
        _drop_version(user, doc, ver)        # never leave two live versions
        raise
    n_ocr = sum(1 for c in chunks if c.get("ocr"))
    return {**base, "ok": True, "chunks": len(chunks), "ocr": n_ocr, "warnings": warnings,
            "ver": ver}


def build(owner: str | None = None):
    docs = list_documents()
    if not docs:
        print("No supported documents in data/docs/.")
        return

    print(f"Found {len(docs)} document(s):")
    per_doc = []
    for path in docs:
        src = source_label(path)
        warnings: list[str] = []
        try:
            recs = load_document(path, warnings)
        except IngestError as e:
            print(f"  - {path.name}: SKIPPED ({e})")
            continue
        chunks = chunk_records(recs, source=src)
        per_doc.append((path.name, chunks))
        note = f"  [{'; '.join(warnings)}]" if warnings else ""
        print(f"  - {src} ({path.suffix}): {len(recs)} records -> {len(chunks)} chunks{note}")

    skipped = unsupported_files()
    if skipped:
        print("skipped (unsupported for now):", ", ".join(p.name for p in skipped))

    total = sum(len(c) for _, c in per_doc)
    print(f"\nTotal chunks: {total}")
    print("Embedding all chunks ...")
    # Embed everything first, write the new version, and only then retire the old
    # bucket — a crash midway leaves the previous knowledge base intact.
    embedded = [(name, chunks, embed_texts([c["text"] for c in chunks]))
                for name, chunks in per_doc if chunks]
    ver = uuid.uuid4().hex
    try:
        for name, chunks, vectors in embedded:
            upsert_chunks(chunks, vectors, user=owner, doc=name, ver=ver)
    except Exception:
        _delete(user_filter(owner, FieldCondition(key="ver", match=MatchValue(value=ver))))
        raise
    _delete(Filter(must=[_user_cond(owner)],          # replace ONLY this bucket
                   must_not=[FieldCondition(key="ver", match=MatchValue(value=ver))]))
    who = f" (owner: {owner})" if owner else " (shared bucket)"
    print(f"Stored {count(owner)} chunks from {len(per_doc)} document(s) in the KB{who}.")
    close()


if __name__ == "__main__":
    import sys
    owner = None
    if "--owner" in sys.argv:
        i = sys.argv.index("--owner")
        owner = sys.argv[i + 1].strip().lower() if i + 1 < len(sys.argv) else None
    build(owner=owner)
