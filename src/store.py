"""Store step: source/locator/ocr-tagged chunk vectors into local Qdrant."""
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from src.config import QDRANT_PATH, COLLECTION_NAME, EMBED_DIM

_client = None


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


def upsert_chunks(chunks, vectors):
    client = get_client()
    points = [
        PointStruct(
            id=i,
            vector=vectors[i],
            payload={
                "chunk_id": chunks[i]["id"],
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


def count():
    return get_client().count(collection_name=COLLECTION_NAME).count
