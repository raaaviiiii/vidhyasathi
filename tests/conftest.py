"""Shared QA harness for Vidhyasathi.

What is REAL in these tests: the Qdrant server (QDRANT_URL), the FastAPI app, the
file parsers (pdf/docx/pptx/xlsx/csv/txt), chunking, the gate logic, the auth DB.

What is FAKED (no Hugging Face / Ollama / Anthropic in CI): the embedder, the
cross-encoder re-ranker, the LLM, web search, and OCR. The fakes are deterministic
and RECORD every call, so tests can assert behaviour (e.g. "the groundedness check
was skipped") as well as output.

Run:  QDRANT_URL=http://localhost:6333 pytest -q
Each pytest session uses its own Qdrant collection (VS_COLLECTION) and a temp dir
for users.db + uploads, so parallel runs never collide.
"""
import hashlib
import math
import os
import re
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# tests must never tidy (move) the real project's private files
os.environ["VS_NO_MIGRATE"] = "1"

os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("VS_COLLECTION", f"vs_test_{uuid.uuid4().hex[:10]}")

from src import config  # noqa: E402

DIM = config.EMBED_DIM
_WORD = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "is", "are", "what", "how",
        "explain", "me", "my", "about", "for", "on", "with", "does", "do", "it", "this",
        "that", "be", "by", "as", "at", "from", "describe", "define", "state", "tell"}


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in STOP]


# ---------------------------------------------------------------- fake embedder
def fake_embed_one(text: str) -> list[float]:
    """Bag-of-words hashed into DIM buckets, L2-normalised. Cosine similarity then
    tracks real word overlap, which is enough to exercise retrieval + the gate."""
    v = [0.0] * DIM
    for w in words(text):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v))
    if n == 0:
        v[0] = 1.0
        n = 1.0
    return [x / n for x in v]


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def embed_texts(self, texts):
        self.calls += 1
        return [fake_embed_one(t) for t in texts]

    def embed_query(self, text):
        return self.embed_texts([text])[0]


# ---------------------------------------------------------------- fake re-ranker
class FakeCrossEncoder:
    def predict(self, pairs):
        out = []
        for q, t in pairs:
            qs, ts = set(words(q)), set(words(t))
            out.append(len(qs & ts) / (len(qs) or 1))
        return out


# ---------------------------------------------------------------- fake LLM
class FakeLLM:
    """Scripted stand-in for Ollama.

    Default behaviour by system prompt:
      - groundedness check -> 'YES' if >=50% of question words appear in context, else 'NO'
      - answer / teach / overview -> echoes a cited answer built from the first context tag
      - exam generator -> n well-formed Q/A items
      - grader -> the four feedback headings
      - web prompt -> a [web · …] answer
    Tests can override with llm.respond = lambda system, messages: "...".
    Every call is logged in llm.calls as (kind, messages).
    """

    def __init__(self):
        self.calls: list[tuple[str, list]] = []
        self.respond = None
        self.fail = None  # set to an Exception to simulate Ollama being down

    @staticmethod
    def kind_of(system: str) -> str:
        s = system or ""
        if "query rewriter" in s:
            return "rewrite"
        if "friendly study companion" in s:
            return "chat"
        if "re-checking your previous reply" in s:
            return "feedback"
        if "retrieval checker" in s:
            return "ground"
        if "STRUCTURED OVERVIEW" in s:
            return "overview"
        if "exam setter" in s:
            return "exam"
        if "supportive tutor giving feedback" in s:
            return "grade"
        if "WEB passages" in s:
            return "web"
        if "patient study tutor" in s:
            return "teach"
        if "study assistant" in s:
            return "quick"
        return "other"

    def __call__(self, messages):
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        kind = self.kind_of(system)
        self.calls.append((kind, messages))
        if self.fail:
            raise self.fail
        if self.respond:
            out = self.respond(kind, messages)
            if out is not None:
                return out
        user = messages[-1]["content"]
        return self._default(kind, user, system)

    def count(self, kind):
        return sum(1 for k, _ in self.calls if k == kind)

    @staticmethod
    def _first_tag(user: str) -> str:
        m = re.search(r"\[([^\]]+)\]", user)
        return f"[{m.group(1)}]" if m else ""

    def _default(self, kind, user, system):
        if kind == "rewrite":
            # a plausible rewriter: the latest message, anchored to the last thing the
            # student asked about (tests override this for specific cases)
            latest = user.split("Latest message:", 1)[-1].split("\n")[0].strip()
            convo = user.split("Latest message:", 1)[0]
            prior = [l[len("Student: "):] for l in convo.splitlines() if l.startswith("Student: ")]
            return f"{latest} ({prior[-1]})" if prior else latest
        if kind == "chat":
            return "Hi! Ask me anything from your notes."
        if kind == "feedback":
            return f"Fair point. Here is exactly what your notes say. {self._first_tag(user)}"
        if kind == "ground":
            ctx, _, q = user.partition("Question:")
            q = q.replace("Answer YES or NO:", "")
            qw = set(words(q))
            cw = set(words(ctx))
            return "YES" if qw and len(qw & cw) / len(qw) >= 0.5 else "NO"
        tag = self._first_tag(user)
        if kind in ("quick", "teach"):
            if not tag:
                return "I don't have that in the material."
            return f"Here is the explanation from your notes. {tag}"
        if kind == "overview":
            return f"- **Concept** — summary of what the notes cover. {tag}" if tag else \
                "I don't have that in the material."
        if kind == "exam":
            m = re.search(r"Write (\d+) practice", system)
            n = int(m.group(1)) if m else 5
            return "\n---\n".join(f"Q: Question {i + 1}?\nA: Answer {i + 1}. {tag}"
                                  for i in range(n))
        if kind == "grade":
            return (f"Correct: good. {tag}\nMissing: None.\nIncorrect: None.\n"
                    "Suggestion: add an example.")
        if kind == "web":
            m = re.search(r"\[(web[^\]]*)\]", user)
            return f"From the web: the answer. [{m.group(1)}]" if m else \
                "I don't have that in the material."
        return "ok"


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def qdrant_up():
    from qdrant_client import QdrantClient
    try:
        QdrantClient(url=os.environ["QDRANT_URL"]).get_collections()
    except Exception as e:  # pragma: no cover
        pytest.exit(f"Qdrant server not reachable at {os.environ['QDRANT_URL']}: {e}")
    return True


@pytest.fixture
def fakes(monkeypatch, qdrant_up):
    """Install all fakes and give each TEST a clean collection."""
    emb = FakeEmbedder()
    llm = FakeLLM()
    import src.embed, src.retrieve, src.build_index, src.rerank, src.answer, src.store
    monkeypatch.setattr(src.embed, "embed_texts", emb.embed_texts)
    monkeypatch.setattr(src.embed, "embed_query", emb.embed_query)
    monkeypatch.setattr(src.retrieve, "embed_query", emb.embed_query)
    monkeypatch.setattr(src.build_index, "embed_texts", emb.embed_texts)
    monkeypatch.setattr(src.rerank, "get_reranker", lambda: FakeCrossEncoder())
    monkeypatch.setattr(src.answer, "chat_llm", llm)

    web_calls = []

    def fake_web_context(query, k=3):
        web_calls.append(query)
        return list(fake_web_context.pages)
    fake_web_context.pages = [{"source": "web · example.org", "loc": "Example page",
                               "url": "https://example.org/page",
                               "text": "Kruskal's algorithm builds a minimum spanning tree.",
                               "layer": "web"}]
    fake_web_context.calls = web_calls
    monkeypatch.setattr(src.answer, "web_context", fake_web_context)

    ocr_calls = []

    def fake_ocr(png: bytes) -> str:
        ocr_calls.append(len(png))
        return fake_ocr.text
    fake_ocr.text = "Handwritten note: a = bq + r where 0 <= r < b"
    fake_ocr.calls = ocr_calls
    import src.ocr
    monkeypatch.setattr(src.ocr, "ocr_image", fake_ocr)

    # fresh collection per test
    client = src.store.get_client()
    if client.collection_exists(config.COLLECTION_NAME):
        client.delete_collection(config.COLLECTION_NAME)

    class F:
        pass
    f = F()
    f.embedder, f.llm, f.web, f.ocr, f.client = emb, llm, fake_web_context, fake_ocr, client
    yield f
    if client.collection_exists(config.COLLECTION_NAME):
        client.delete_collection(config.COLLECTION_NAME)


@pytest.fixture
def app_client(fakes, monkeypatch, tmp_path):
    """FastAPI TestClient with users.db and uploads redirected to tmp_path."""
    from fastapi.testclient import TestClient
    import app as app_module
    monkeypatch.setattr(app_module, "BASE", tmp_path)
    monkeypatch.setattr(app_module, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(app_module, "WEB_DIR", tmp_path)
    if not (tmp_path / "index.html").exists():
        (tmp_path / "index.html").write_text((ROOT / "web" / "index.html").read_text())
    c = TestClient(app_module.app)
    c.fakes = fakes
    c.tmp = tmp_path
    return c


@pytest.fixture
def make_files(tmp_path):
    """Factory for real sample documents of every supported format."""
    from tests.samples import make_sample
    def _make(kind, name=None, **kw):
        return make_sample(kind, tmp_path / "samples", name=name, **kw)
    return _make


# The fake LLM / embedder are reachable for test modules that need them directly.
@pytest.fixture
def index_text(fakes):
    """Index raw text paragraphs for a user without going through files.
    index_text(user, source, [para, ...]) -> number of chunks."""
    from src.chunk import chunk_records
    from src.store import upsert_chunks

    def _ix(user, source, paras, ocr=False):
        recs = [{"loc": f"p{i + 1}", "order": i + 1, "text": t, "ocr": ocr}
                for i, t in enumerate(paras)]
        chunks = chunk_records(recs, source=source)
        upsert_chunks(chunks, fakes.embedder.embed_texts([c["text"] for c in chunks]), user=user)
        return len(chunks)
    return _ix
