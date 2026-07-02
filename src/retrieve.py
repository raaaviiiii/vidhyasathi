"""Retrieve step: given a question, return the most similar chunks with
their source + locator. Limit capped at KB size (adaptive to document size).
"""
from src.config import COLLECTION_NAME, RETRIEVE_CANDIDATES
from src.embed import embed_query
from src.store import get_client, count


def retrieve(question: str, top_k: int = RETRIEVE_CANDIDATES) -> list[dict]:
    client = get_client()
    total = count()
    limit = min(top_k, total) if total else top_k

    qvec = embed_query(question)
    hits = client.query_points(
        collection_name=COLLECTION_NAME,
        query=qvec,
        limit=limit,
        with_payload=True,
    ).points

    return [{
        "score": h.score,
        "source": h.payload.get("source", "document"),
        "loc": h.payload.get("loc", ""),
        "order": h.payload.get("order", 0),
        "chunk_id": h.payload["chunk_id"],
        "text": h.payload["text"],
    } for h in hits]


if __name__ == "__main__":
    import sys
    from src.store import close
    q = " ".join(sys.argv[1:]) or "What visualization tools does the course cover?"
    print(f"Q: {q}\n")
    for i, h in enumerate(retrieve(q), 1):
        print(f"[{i}] {h['score']:.3f}  {h['source']} · {h['loc']}")
        print(f"    {h['text'][:140]}...\n")
    close()
