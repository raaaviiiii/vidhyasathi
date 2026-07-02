"""Embed step: turn chunk texts into vectors with sentence-transformers.
Stage 3 of the pipeline. The model is loaded once and cached.
"""
from sentence_transformers import SentenceTransformer

from src.config import EMBED_MODEL

_model = None   # module-level cache so we load the model only once


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        print(f"Loading embedding model: {EMBED_MODEL} ...")
        _model = SentenceTransformer(EMBED_MODEL)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of strings -> list of vectors (normalised for cosine)."""
    model = get_model()
    vectors = model.encode(
        texts,
        normalize_embeddings=True,      # so dot-product == cosine similarity
        show_progress_bar=len(texts) > 32,
    )
    return vectors.tolist()


def embed_query(text: str) -> list[float]:
    """Embed a single query string -> one vector."""
    return embed_texts([text])[0]


if __name__ == "__main__":
    from src.ingest import load_pdf, _first_pdf
    from src.chunk import chunk_pages

    pages = load_pdf(_first_pdf())
    chunks = chunk_pages(pages)
    texts = [c["text"] for c in chunks]

    vectors = embed_texts(texts)

    print(f"Chunks embedded: {len(vectors)}")
    print(f"Vector dimension: {len(vectors[0])}   (config EMBED_DIM should match)")

    # sanity check: a chunk should be more similar to itself than to another
    import numpy as np
    v = np.array(vectors)
    sim_self = float(v[0] @ v[0])
    sim_other = float(v[0] @ v[1])
    print(f"self-similarity:  {sim_self:.3f}  (expect ~1.0)")
    print(f"neighbour-sim:    {sim_other:.3f}  (expect lower)")