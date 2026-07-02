"""Embed step: turn texts into vectors with sentence-transformers."""
from sentence_transformers import SentenceTransformer
from src.config import EMBED_MODEL

_model = None


def get_model():
    global _model
    if _model is None:
        print(f"Loading embedding model: {EMBED_MODEL} ...")
        _model = SentenceTransformer(EMBED_MODEL)
    return _model


def embed_texts(texts):
    return get_model().encode(
        texts, normalize_embeddings=True, show_progress_bar=len(texts) > 32
    ).tolist()


def embed_query(text):
    return embed_texts([text])[0]


if __name__ == "__main__":
    from src.ingest import list_documents, load_document, source_label
    from src.chunk import chunk_records
    d = list_documents()[0]
    chunks = chunk_records(load_document(d), source=source_label(d))
    vecs = embed_texts([c["text"] for c in chunks])
    print(f"chunks: {len(vecs)}  dim: {len(vecs[0])}")
