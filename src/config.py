"""Central configuration for Vidhyasathi."""
import os
from pathlib import Path

# --- paths ---
# Code lives in the project folder; everything PRIVATE (accounts, students' uploads,
# the knowledge base, OCR text, logs, evaluation reports, secrets) lives in private/,
# which git never tracks and only the owner's Mac account can open.
BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
PRIVATE_DIR = Path(os.environ.get("VS_PRIVATE_DIR", BASE_DIR / "private"))
DATA_DIR = PRIVATE_DIR
DOCS_DIR = PRIVATE_DIR / "docs"            # optional shared notes for: python -m src.build_index
UPLOADS_DIR = PRIVATE_DIR / "uploads"      # uploads/<user_hash>/<file>
USERS_DB = PRIVATE_DIR / "users.db"
LOG_DIR = PRIVATE_DIR / "logs"
EVAL_DIR = PRIVATE_DIR / "eval"
QDRANT_DOCKER_DIR = PRIVATE_DIR / "qdrant"  # mounted into the Qdrant container
ENV_FILE = PRIVATE_DIR / ".env"

# secrets/settings: private/.env (an old root .env is still read until it is moved)
try:
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

# --- chunking ---
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150

# --- embeddings ---
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384

# --- retrieval ---
TOP_K = 5

# --- vector store ---
import os
COLLECTION_NAME = os.environ.get("VS_COLLECTION", "vidhyasathi_kb")  # override for tests
QDRANT_PATH = str(PRIVATE_DIR / "qdrant-embedded")   # only if QDRANT_URL="" (embedded mode)
# Prefer a Qdrant SERVER (Docker): embedded path mode is single-process and loses
# writes under a web server's threadpool. QDRANT_URL="" falls back to embedded mode.
import os
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")

# --- answering (LLM via Ollama) ---
OLLAMA_BASE_URL = "http://localhost:11434"
LLM_MODEL = "llama3.1:8b"

# --- confidence gate ---
MIN_RETRIEVAL_SCORE = 0.35
ANSWER_SCORE = 0.45   # >= this: confident -> answer directly
HELP_SCORE   = 0.30   # >= this but < ANSWER_SCORE: answer + offer "ask a human"; below this: honest refuse
STRONG_SKIP_SCORE = 0.55  # >= this cosine: trust retrieval, SKIP the LLM groundedness check (halves latency on confident hits)

# --- broad / overview questions ("explain all the concepts in the module") ---
BROAD_TOP_K = 12      # overview questions synthesize from more chunks than a pointed one


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
OCR_TIERS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]  # current vision models (Oct 2026)
OCR_BLUR_FLOOR = 60.0          # sharpness below this = blurry scan -> skip Haiku
OCR_MAX_TOKENS = 2000
OCR_CACHE_PATH = str(PRIVATE_DIR / "ocr" / "ocr_cache.json")   # pay per page once, ever
OCR_COST_LOG = str(PRIVATE_DIR / "ocr" / "ocr_cost_log.csv")
# ROUGH per-page USD estimates for the running spend log — tune to current pricing
OCR_EST_COST = {                      # ~1.6k input + ~1k output tokens per page
    "claude-haiku-5-5": 0.001,
    "claude-sonnet-5-5": 0.015,
    "claude-opus-5-5": 0.03,
}