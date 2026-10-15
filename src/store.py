"""Store step: chunk vectors in Qdrant, scoped per student.

Every point carries:
  user    — the student's email; None for the shared/CLI bucket
  doc     — the exact uploaded file name ("notes.pdf"), the identity of a document
  source  — the display label used in citations ("notes")
  ver     — which ingest of that document the chunk belongs to (atomic re-upload)

Scoping rules (never "all users" by accident):
  user="a@x.com"  -> only that student's points
  user=None       -> only the shared bucket (points with no user)
"""
import threading
import uuid

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (Distance, FieldCondition, Filter, FilterSelector,
                                  IsEmptyCondition, MatchValue, PayloadField,
                                  PayloadSchemaType, PointStruct, VectorParams)

from src.config import QDRANT_PATH, QDRANT_URL, COLLECTION_NAME, EMBED_DIM

_NS = uuid.UUID("5f9b1e2a-1c3d-4e5f-8a9b-0c1d2e3f4a5b")
UPSERT_BATCH = 128            # keeps each request far under Qdrant's 32 MB body cap

_client = None
_lock = threading.Lock()


# ------------------------------------------------------------------ filters
def _user_cond(user):
    if user:
        return FieldCondition(key="user", match=MatchValue(value=user))
    return IsEmptyCondition(is_empty=PayloadField(key="user"))


def user_filter(user, *extra):
    """Filter scoped to one student, or to the shared bucket when user is None."""
    return Filter(must=[_user_cond(user), *extra])


def _doc_cond(doc):
    return FieldCondition(key="doc", match=MatchValue(value=doc))


def _source_cond(source):
    return FieldCondition(key="source", match=MatchValue(value=source))


# ------------------------------------------------------------------ client
def get_client():
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = (QdrantClient(url=QDRANT_URL, timeout=30) if QDRANT_URL
                           else QdrantClient(path=QDRANT_PATH))
    return _client


def close():
    global _client
    if _client is not None:
        _client.close()
        _client = None


def collection_exists() -> bool:
    return get_client().collection_exists(COLLECTION_NAME)


def ensure_collection():
    """Create the collection once, safely under concurrent first uploads."""
    client = get_client()
    if client.collection_exists(COLLECTION_NAME):
        return
    with _lock:
        if client.collection_exists(COLLECTION_NAME):
            return
        try:
            client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
            )
        except UnexpectedResponse as e:
            if e.status_code != 409:        # another process created it first: fine
                raise
        for field in ("user", "doc", "source", "ver"):
            try:
                client.create_payload_index(COLLECTION_NAME, field,
                                            field_schema=PayloadSchemaType.KEYWORD)
            except Exception:
                pass                        # index is an optimisation, never fatal


def reset_collection():
    """Drop EVERYTHING (all students). Only for a deliberate full wipe."""
    client = get_client()
    if client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)
    ensure_collection()


# ------------------------------------------------------------------ writes
def _point_id(user, doc, ver, chunk_id):
    return str(uuid.uuid5(_NS, f"{user or ''}\x1f{doc or ''}\x1f{ver or ''}\x1f{chunk_id}"))


def upsert_chunks(chunks, vectors, user=None, doc=None, ver=None):
    """Store chunks for one document. `doc` defaults to each chunk's source label."""
    if not chunks:
        return 0
    if len(chunks) != len(vectors):
        raise ValueError("chunks and vectors differ in length")
    ensure_collection()
    client = get_client()
    points = []
    for c, v in zip(chunks, vectors):
        d = doc or c.get("doc") or c.get("source", "document")
        points.append(PointStruct(
            id=_point_id(user, d, ver, c["id"]),
            vector=v,
            payload={
                "chunk_id": c["id"],
                "user": user,
                "doc": d,
                "ver": ver,
                "source": c.get("source", "document"),
                "loc": c.get("loc", ""),
                "order": c.get("order", 0),
                "text": c["text"],
                "ocr": c.get("ocr", False),
            },
        ))
    for i in range(0, len(points), UPSERT_BATCH):
        client.upsert(collection_name=COLLECTION_NAME, points=points[i:i + UPSERT_BATCH],
                      wait=True)
    return len(points)


def replace_doc(user, doc, chunks, vectors):
    """Atomically swap in a new version of one document: write the new chunks first,
    then remove the old ones. If the write fails, the old version is left intact."""
    ver = uuid.uuid4().hex
    try:
        upsert_chunks(chunks, vectors, user=user, doc=doc, ver=ver)
    except Exception:
        try:                                 # best-effort: drop the half-written version
            _delete(user_filter(user, _doc_cond(doc),
                                FieldCondition(key="ver", match=MatchValue(value=ver))))
        except Exception:
            pass
        raise
    old = Filter(must=[_user_cond(user), _doc_cond(doc)],
                 must_not=[FieldCondition(key="ver", match=MatchValue(value=ver))])
    _delete(old)
    return ver


def _delete(flt):
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return
    client.delete(collection_name=COLLECTION_NAME,
                  points_selector=FilterSelector(filter=flt), wait=True)


def delete_doc(user, doc):
    """Remove one uploaded document (by exact file name) for one student."""
    _delete(user_filter(user, _doc_cond(doc)))


def delete_source(user, source):
    """Remove chunks by display label, within one student's (or the shared) bucket."""
    _delete(user_filter(user, _source_cond(source)))


def delete_bucket(user):
    """Remove every chunk in one bucket (a student, or the shared bucket)."""
    _delete(user_filter(user))


def delete_user(user):
    """Remove ALL of a student's chunks (account deletion). Never touches others."""
    if user:
        delete_bucket(user)


# ------------------------------------------------------------------ reads
def count(user=None):
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return 0
    return client.count(collection_name=COLLECTION_NAME,
                        count_filter=user_filter(user), exact=True).count


def _scroll_field(user, field):
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return set()
    seen, offset = set(), None
    while True:
        pts, offset = client.scroll(
            collection_name=COLLECTION_NAME, scroll_filter=user_filter(user),
            with_payload=[field], with_vectors=False, limit=512, offset=offset)
        for p in pts:
            v = (p.payload or {}).get(field)
            if v:
                seen.add(v)
        if offset is None:
            break
    return seen


def user_chunks(user, *extra, limit: int | None = None) -> list[dict]:
    """Every chunk in a student's bucket (optionally narrowed by extra conditions),
    in document order: [{source, doc, loc, order, chunk_id, text, ocr}]."""
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return []
    out, seen, offset = [], set(), None
    while True:
        pts, offset = client.scroll(
            collection_name=COLLECTION_NAME, scroll_filter=user_filter(user, *extra),
            with_payload=True, with_vectors=False, limit=512, offset=offset)
        for p in pts:
            pl = p.payload or {}
            key = (pl.get("source"), pl.get("loc"), pl.get("chunk_id"))
            if key in seen or not pl.get("text"):
                continue                        # mid-re-upload, two versions can coexist
            seen.add(key)
            out.append({"source": pl.get("source", "document"), "doc": pl.get("doc"),
                        "loc": pl.get("loc", ""), "order": pl.get("order", 0),
                        "chunk_id": pl.get("chunk_id", 0), "text": pl["text"],
                        "ocr": pl.get("ocr", False)})
        if offset is None or (limit and len(out) >= limit):
            break
    out.sort(key=lambda c: (c["source"], c["order"], c["chunk_id"]))
    return out[:limit] if limit else out


def chunks_at(user, source: str, loc: str) -> list[dict]:
    """The chunk(s) at one locator of one document — 'slide 4', 'p12'."""
    return user_chunks(user, _source_cond(source),
                       FieldCondition(key="loc", match=MatchValue(value=loc)))


def user_sources(user):
    """Distinct citation labels in a bucket."""
    return _scroll_field(user, "source")


def user_docs(user):
    """Distinct document file names in a bucket (to flag which uploads are live)."""
    return _scroll_field(user, "doc")


def user_index(user):
    """(document file names, labels of pre-versioning chunks that have no `doc`)."""
    client = get_client()
    docs, legacy = set(), set()
    if not client.collection_exists(COLLECTION_NAME):
        return docs, legacy
    offset = None
    while True:
        pts, offset = client.scroll(
            collection_name=COLLECTION_NAME, scroll_filter=user_filter(user),
            with_payload=["doc", "source"], with_vectors=False, limit=512, offset=offset)
        for p in pts:
            pl = p.payload or {}
            if pl.get("doc"):
                docs.add(pl["doc"])
            elif pl.get("source"):
                legacy.add(pl["source"])
        if offset is None:
            break
    return docs, legacy


def delete_legacy_source(user, source):
    """Remove chunks written before documents had a `doc` field, by their label."""
    _delete(Filter(must=[_user_cond(user), _source_cond(source),
                         IsEmptyCondition(is_empty=PayloadField(key="doc"))]))


def count_version(user, doc, ver):
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return 0
    return client.count(collection_name=COLLECTION_NAME, exact=True,
                        count_filter=user_filter(user, _doc_cond(doc),
                                                 FieldCondition(key="ver", match=MatchValue(value=ver)))).count
