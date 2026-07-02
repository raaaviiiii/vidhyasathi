"""Store step: put chunk vectors into a local Qdrant collection.
Stage 4 of the pipeline. Local on-disk mode — no server needed.
"""
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

from src.config import QDRANT_PATH, COLLECTION_NAME, EMBED_DIM

_client = None


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(path=QDRANT_PATH)   # on-disk, persistent
    return _client

def close() -> None:
    """Release the on-disk lock explicitly (avoids shutdown warnings)."""
    global _client
    if _client is not None:
        _client.close()
        _client = None

def reset_collection() -> None:
    """Create the collection fresh (drops it first if it exists)."""
    client = get_client()
    if client.collection_exists(COLLECTION_NAME):
        client.delete_collection(COLLECTION_NAME)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )


def upsert_chunks(chunks: list[dict], vectors: list[list[float]]) -> None:
    """Store chunks + their vectors. Payload keeps page/text/id for retrieval."""
    client = get_client()
    points = [
        PointStruct(
            id=i,                                  # simple integer id per point
            vector=vectors[i],
            payload={
                "chunk_id": chunks[i]["id"],       # e.g. p1_c0
                "page": chunks[i]["page"],
                "text": chunks[i]["text"],
            },
        )
        for i in range(len(chunks))
    ]
    client.upsert(collection_name=COLLECTION_NAME, points=points)


def count() -> int:
    return get_client().count(collection_name=COLLECTION_NAME).count


if __name__ == "__main__":
    from src.ingest import load_pdf, _first_pdf
    from src.chunk import chunk_pages
    from src.embed import embed_texts

    pages = load_pdf(_first_pdf())
    chunks = chunk_pages(pages)
    vectors = embed_texts([c["text"] for c in chunks])

    reset_collection()
    upsert_chunks(chunks, vectors)

    print(f"Collection: {COLLECTION_NAME}")
    print(f"Points stored: {count()}   (expect {len(chunks)})")
    close()