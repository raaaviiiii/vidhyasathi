"""Answer step: grounded, cited answer. Size-aware (small KB = whole doc;
large KB = retrieve + re-rank). Citations name the source and locator.
"""
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
    user_msg = f"Context passages:\n{context}\n\nQuestion: {question}"
    reply = ask_llm(SYSTEM_PROMPT, user_msg)

    return {
        "answer": reply,
        "top_score": top_score,
        "weak": weak,
        "mode": mode,
        "sources": [{"source": h["source"], "loc": h["loc"], "score": h["score"]} for h in hits],
    }


if __name__ == "__main__":
    import sys
    from src.store import close
    q = " ".join(sys.argv[1:]) or "What visualization tools does the course cover?"
    print(f"Q: {q}\n")
    result = answer(q)
    print(f"[{result['mode']}]\n")
    print("ANSWER:")
    print(result["answer"])
    warn = "  \u26a0 low confidence" if result["weak"] else ""
    print(f"\n(top retrieval score: {result['top_score']:.3f}{warn})")
    seen = []
    for s in result["sources"]:
        tag = f"{s['source']} \u00b7 {s['loc']}"
        if tag not in seen:
            seen.append(tag)
    print("sources:", "; ".join(seen))
    close()
