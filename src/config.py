"""Central configuration for Vidhyasathi.
Every tunable path, model name, and threshold lives here so the rest of
the code never hard-codes them.
"""
from pathlib import Path

# --- paths ---
BASE_DIR = Path(__file__).resolve().parent.parent   # the vidhyasathi/ root
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = DATA_DIR / "docs"                         # source PDFs / notes go here

# --- chunking (how we split a document before embedding) ---
CHUNK_SIZE = 800        # approx characters per chunk
CHUNK_OVERLAP = 150     # characters shared between neighbouring chunks

# --- embeddings ---
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # small, fast, solid baseline
EMBED_DIM = 384                                         # vector size for that model

# --- retrieval ---
TOP_K = 5               # how many chunks we pull per question

# --- vector store (Qdrant, local on-disk for now) ---
COLLECTION_NAME = "vidhyasathi_kb"
QDRANT_PATH = str(BASE_DIR / "qdrant_storage")

# --- answering (LLM via Ollama; wired up in a later step) ---
OLLAMA_BASE_URL = "http://localhost:11434"
LLM_MODEL = "llama3.1:8b"

# --- confidence gate (tuned later) ---
MIN_RETRIEVAL_SCORE = 0.35   # below this, retrieval counts as "weak"