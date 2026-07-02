"""Retrieve step: given a question, return the most similar chunks.
Stage 5 of the pipeline (read side). Reuses the same embed model.
"""
from src.config import COLLECTION_NAME, TOP_K
from src.embed import embed_query
from src.store import get_client


def retrieve(question: str, top_k: int = TOP_K) -> list[dict]:
    """Return top_k chunks as {score, page, chunk_id, text}, best first."""
    client = get_client()
    qvec = embed_query(question)

    hits = client.query_points(
        collection_name=COLLECTION_NAME,
        query=qvec,
        limit=top_k,
        with_payload=True,
    ).points

    results = []
    for h in hits:
        results.append({
            "score": h.score,
            "page": h.payload["page"],
            "chunk_id": h.payload["chunk_id"],
            "text": h.payload["text"],
        })
    return results


if __name__ == "__main__":
    import sys
    from src.store import close

    question = " ".join(sys.argv[1:]) or "What visualization tools does the course cover?"
    print(f"Q: {question}\n")

    hits = retrieve(question)
    for i, h in enumerate(hits, 1):
        snippet = h["text"][:160].replace("\n", " ")
        print(f"[{i}] score={h['score']:.3f}  page {h['page']}  ({h['chunk_id']})")
        print(f"    {snippet}...\n")

    close()