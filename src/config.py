"""Central configuration for Vidhyasathi."""
from pathlib import Path

# --- paths ---
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DOCS_DIR = DATA_DIR / "docs"

# --- chunking ---
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150

# --- embeddings ---
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384

# --- retrieval ---
TOP_K = 5

# --- vector store ---
COLLECTION_NAME = "vidhyasathi_kb"
QDRANT_PATH = str(BASE_DIR / "qdrant_storage")

# --- answering (LLM via Ollama) ---
OLLAMA_BASE_URL = "http://localhost:11434"
LLM_MODEL = "llama3.1:8b"

# --- confidence gate ---
MIN_RETRIEVAL_SCORE = 0.35
ANSWER_SCORE = 0.45   # >= this: confident -> answer directly
HELP_SCORE   = 0.30   # >= this but < ANSWER_SCORE: answer + offer "ask a human"; below this: honest refuse

# --- re-ranking ---
RETRIEVE_CANDIDATES = 15
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
RERANK_WEIGHT = 0.7

# --- size-aware context ---
SMALL_KB_MAX = 30

# --- OCR (adaptive vision fallback for scanned / handwritten / image files) ---
OCR_DPI = 200
OCR_MIN_TEXT = 20      # a page with fewer chars than this is treated as scanned -> OCR
# tiers, easy -> hard; escalation walks down this list
OCR_TIERS = ["claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-4-8"]
OCR_BLUR_FLOOR = 60.0          # sharpness below this = blurry scan -> skip Haiku
OCR_MAX_TOKENS = 2000
OCR_CACHE_PATH = str(BASE_DIR / "ocr_cache.json")   # pay per page once, ever
OCR_COST_LOG = str(BASE_DIR / "ocr_cost_log.csv")
# ROUGH per-page USD estimates for the running spend log — tune to current pricing
OCR_EST_COST = {
    "claude-haiku-4-5-20251001": 0.003,
    "claude-sonnet-5": 0.010,
    "claude-opus-4-8": 0.050,
}