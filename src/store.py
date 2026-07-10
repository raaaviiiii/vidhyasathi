"""Store step: source/locator/ocr-tagged chunk vectors into local Qdrant.

Per-user isolation: each point carries a `user` payload field. Passing user=None
(CLI / full rebuild) writes an untagged, shared point; the web app passes the
logged-in email so retrieval, count, and delete can all be scoped to one student.
"""
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (Distance, VectorParams, PointStruct,
                                  Filter, FieldCondition, MatchValue)
from src.config import QDRANT_PATH, COLLECTION_NAME, EMBED_DIM

# fixed namespace so a point id is deterministic from (user, chunk_id):
# re-uploading the same file for the same user overwrites instead of duplicating.
_NS = uuid.UUID("5f9b1e2a-1c3d-4e5f-8a9b-0c1d2e3f4a5b")

_client = None


def user_filter(user):
    """Qdrant filter that scopes points to one user (or None for all)."""
    if not user:
        return None
    return Filter(must=[FieldCondition(key="user", match=MatchValue(value=user))])


def _source_filter(user, source):
    conds = [FieldCondition(key="source", match=MatchValue(value=source))]
    if user:
        conds.append(FieldCondition(key="user", match=MatchValue(value=user)))
    return Filter(must=conds)


def get_client():
    global _client
    if _client is None:
        _client = QdrantClient(path=QDRANT_PATH)
    return _client


def close():
    global _client
    if _client is not None:
        _client.close(); _client = None


def reset_collection():
    client = get_client()
    if client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )


def _point_id(user, chunk_id):
    return str(uuid.uuid5(_NS, f"{user or ''}:{chunk_id}"))


def upsert_chunks(chunks, vectors, user=None):
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
    points = [
        PointStruct(
            id=_point_id(user, chunks[i]["id"]),
            vector=vectors[i],
            payload={
                "chunk_id": chunks[i]["id"],
                "user": user,
                "source": chunks[i].get("source", "document"),
                "loc": chunks[i].get("loc", ""),
                "order": chunks[i].get("order", 0),
                "text": chunks[i]["text"],
                "ocr": chunks[i].get("ocr", False),
            },
        )
        for i in range(len(chunks))
    ]
    client.upsert(collection_name=COLLECTION_NAME, points=points)


def count(user=None):
    return get_client().count(
        collection_name=COLLECTION_NAME, count_filter=user_filter(user)
    ).count


def delete_source(user, source):
    """Remove one document's chunks for a user (for re-upload / 'remove material')."""
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return
    client.delete(collection_name=COLLECTION_NAME,
                  points_selector=_source_filter(user, source))


def delete_user(user):
    """Remove ALL of a user's chunks (account deletion)."""
    if not user:
        return
    client = get_client()
    if client.collection_exists(COLLECTION_NAME):
        client.delete(collection_name=COLLECTION_NAME, points_selector=user_filter(user))


def user_sources(user):
    """Distinct source labels a user has indexed (to flag which uploads are live)."""
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return set()
    seen, offset = set(), None
    while True:
        pts, offset = client.scroll(
            collection_name=COLLECTION_NAME, scroll_filter=user_filter(user),
            with_payload=["source"], with_vectors=False, limit=256, offset=offset)
        for p in pts:
            s = p.payload.get("source")
            if s:
                seen.add(s)
        if offset is None:
            break
    return seen
