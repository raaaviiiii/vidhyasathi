"""Re-rank step: sharpen the first-stage vector hits with a cross-encoder,
then BLEND with the original cosine score so re-ranking refines the order
without fully overriding the vector signal (more stable on small pools).
Own stage, swappable via config.
"""
from sentence_transformers import CrossEncoder

from src.config import RERANK_MODEL, TOP_K, RERANK_WEIGHT

_model = None


def get_reranker() -> CrossEncoder:
    global _model
    if _model is None:
        print(f"Loading re-ranker: {RERANK_MODEL} ...")
        _model = CrossEncoder(RERANK_MODEL)
    return _model


def _minmax(values: list[float]) -> list[float]:
    """Scale a list of scores to [0, 1]. If they're all equal (no signal),
    return a neutral 0.5 for each so this stage contributes nothing."""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rerank(question: str, candidates: list[dict], top_k: int = TOP_K) -> list[dict]:
    """Re-order candidates by a blend of cross-encoder relevance and cosine,
    then keep the best top_k. Adds 'rerank_score' and 'blend_score' to each.
    """
    if not candidates:
        return []

    model = get_reranker()
    pairs = [(question, c["text"]) for c in candidates]
    rr_scores = [float(s) for s in model.predict(pairs)]
    cos_scores = [c["score"] for c in candidates]

    rr_norm = _minmax(rr_scores)
    cos_norm = _minmax(cos_scores)

    for c, rr, rrn, cosn in zip(candidates, rr_scores, rr_norm, cos_norm):
        c["rerank_score"] = rr
        c["blend_score"] = RERANK_WEIGHT * rrn + (1.0 - RERANK_WEIGHT) * cosn

    ranked = sorted(candidates, key=lambda c: c["blend_score"], reverse=True)
    return ranked[:top_k]


if __name__ == "__main__":
    import sys
    from src.retrieve import retrieve
    from src.store import close

    question = " ".join(sys.argv[1:]) or "What visualization tools does the course cover?"
    print(f"Q: {question}\n")

    candidates = retrieve(question)
    print(f"vector candidates: {len(candidates)}  (ordered by cosine)")
    for i, c in enumerate(candidates[:5], 1):
        print(f"  {i}. cos={c['score']:.3f}  p{c['page']}  {c['chunk_id']}")

    reranked = rerank(question, candidates)
    print(f"\nafter blended re-rank (top {len(reranked)}):")
    for i, c in enumerate(reranked, 1):
        snippet = c["text"][:80].replace("\n", " ")
        print(f"  {i}. blend={c['blend_score']:.3f}  rr={c['rerank_score']:.3f}  "
              f"cos={c['score']:.3f}  p{c['page']}  {snippet}...")

    close()