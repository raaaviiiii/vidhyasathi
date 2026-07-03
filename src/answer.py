"""Answer step: grounded, cited answer. Size-aware (small KB = whole doc;
large KB = retrieve + re-rank). Adds a quiet "check the equation" note only
when the answer draws on an OCR'd (scanned/handwritten) source AND contains math.
"""
import re
import ollama
from src.config import LLM_MODEL, MIN_RETRIEVAL_SCORE, SMALL_KB_MAX
from src.retrieve import retrieve
from src.rerank import rerank
from src.store import count

SYSTEM_PROMPT = (
    "You are a study assistant. Answer the student's question using ONLY the "
    "context passages provided. Each passage begins with its source tag in "
    "square brackets, like [Syllabus \u00b7 p3] or [Notes \u00b7 Methods]. After "
    "each fact you use, cite that exact tag. If the context does not contain "
    "the answer, say exactly: \"I don't have that in the material.\" Do not use "
    "outside knowledge."
)

_MATH_RE = re.compile(r"\$|\\[a-zA-Z]+|[\u2264\u2265\u230a\u230b\u2211\u222b\u2208\u221a\u2260\u00b1\u00d7\u00f7]|\b\d+\s*[+\-*/=]\s*\d+|\s=\s")


def _has_math(t: str) -> bool:
    return bool(_MATH_RE.search(t))


def ask_llm(system: str, user: str) -> str:
    resp = ollama.chat(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        options={"temperature": 0.1},
    )
    return resp["message"]["content"].strip()


def build_context(hits: list[dict]) -> str:
    return "\n\n".join(f"[{h['source']} \u00b7 {h['loc']}] {h['text']}" for h in hits)


def _doc_order(chunks: list[dict]) -> list[dict]:
    return sorted(chunks, key=lambda c: (c["source"], c["order"], c["chunk_id"]))


def answer(question: str) -> dict:
    total = count()
    small = 0 < total <= SMALL_KB_MAX

    if small:
        candidates = retrieve(question, top_k=total)
        top_score = max((c["score"] for c in candidates), default=0.0)
        hits = _doc_order(candidates)
        mode = f"small-kb ({total} chunks): full context"
    else:
        candidates = retrieve(question)
        ranked = rerank(question, candidates)
        top_score = ranked[0]["score"] if ranked else 0.0
        hits = ranked
        mode = f"large-kb ({total} chunks): retrieve + re-rank"

    weak = top_score < MIN_RETRIEVAL_SCORE
    context = build_context(hits)
    reply = ask_llm(SYSTEM_PROMPT, f"Context passages:\n{context}\n\nQuestion: {question}")

    # quiet nudge: only when a scanned/handwritten source was used AND math is present
    ocr_used = any(h.get("ocr") for h in hits)
    caveat = ("\u21b3 This draws on a handwritten/scanned source \u2014 worth "
              "double-checking the equation.") if (ocr_used and _has_math(reply)) else ""

    return {
        "answer": reply,
        "caveat": caveat,
        "top_score": top_score,
        "weak": weak,
        "mode": mode,
        "sources": [{"source": h["source"], "loc": h["loc"],
                     "score": h["score"], "ocr": h["ocr"]} for h in hits],
    }


if __name__ == "__main__":
    import sys
    from src.store import close
    q = " ".join(sys.argv[1:]) or "State the Division Algorithm"
    print(f"Q: {q}\n")
    result = answer(q)
    print(f"[{result['mode']}]\n")
    print("ANSWER:")
    print(result["answer"])
    if result["caveat"]:
        print("\n" + result["caveat"])
    warn = "  \u26a0 low confidence" if result["weak"] else ""
    print(f"\n(top retrieval score: {result['top_score']:.3f}{warn})")
    seen = []
    for s in result["sources"]:
        tag = f"{s['source']} \u00b7 {s['loc']}" + (" [OCR]" if s["ocr"] else "")
        if tag not in seen:
            seen.append(tag)
    print("sources:", "; ".join(seen))
    close()
