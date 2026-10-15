"""Exam & Practice \u2014 question generation (part 1 of 2; grading comes next).
 
Generates practice questions GROUNDED in the student's own material: retrieve the
topic, verify it's actually covered (the same groundedness gate used for answering),
then ask the model for exam-style Q/A pairs answerable ONLY from those passages, each
citing its source tag. If the topic isn't in the material, it refuses rather than
inventing questions from outside knowledge.
 
Run:  python -m src.exam "the Division Algorithm"        (default 5 questions)
      python -m src.exam "the Division Algorithm" 3      (choose how many)
"""
import re
from src.answer import _retrieve, _gate, build_context, ask_llm, REFUSAL
from src.config import HELP_SCORE
 
GEN_PROMPT = (
    "You are an exam setter. Write {n} practice questions specifically about the given "
    "TOPIC, using ONLY the context passages. Every question must be about the topic and "
    "answerable purely from the passages. Ignore any passage that is about a different "
    "topic, and never use outside knowledge.\n"
    "For each question, write a COMPLETE reference answer (one to three sentences) that "
    "actually answers it \u2014 the real answer, not just a citation \u2014 and end the answer with "
    "the exact bracketed source tag(s) that appear at the start of the passages you used.\n"
    "FORMAT (strict): output each item as exactly:\n"
    "Q: <the question>\n"
    "A: <the complete answer, ending with the source tag(s)>\n"
    "Separate items with a line containing only ---\n"
    "Produce exactly {n} items, all about the topic. No preamble, no numbering, nothing else."
)
 
TOP_K_GEN = 6   # focus generation on the most-relevant chunks to curb topic drift
 
MAX_N = 20      # practice sets are capped; a bigger ask is clamped, not refused

# Line-anchored markers only, so "formula." or "part (a)" inside a question can
# never be mistaken for the start of an answer.
_Q_MARK = re.compile(r"^\s*(?:\d+\s*[.)]\s*)?(?:Q(?:uestion)?)\s*\d*\s*[:.)\-]\s*(.*)$", re.I)
_A_MARK = re.compile(r"^\s*(?:A(?:nswer)?)\s*\d*\s*[:)\-]\s*(.*)$|^\s*Answer\s*\d*\s*\.\s*(.*)$", re.I)
_DELIM = re.compile(r"^\s*-{3,}\s*$")
_TAG = re.compile(r"\[[^\]\n]+\]")


def _parse_qa(text: str) -> list[dict]:
    """Parse the model's Q/A output into [{'q','a'}]. Tolerant of numbering
    ("1. Q:"), "Question/Answer" wording, markdown and missing delimiters."""
    text = re.sub(r"[*`]", "", text or "")
    items, cur, field = [], None, None
    for raw in text.splitlines():
        line = re.sub(r"^\s*[#>]+\s*", "", raw)
        if _DELIM.match(line):
            continue
        mq = _Q_MARK.match(line)
        ma = None if mq else _A_MARK.match(line)
        if mq:
            if cur and cur["q"] and cur["a"]:
                items.append(cur)
            cur, field = {"q": mq.group(1).strip(), "a": ""}, "q"
        elif ma and cur is not None:
            cur["a"] = (ma.group(1) or ma.group(2) or "").strip()
            field = "a"
        elif cur is not None and field and line.strip():
            cur[field] = (cur[field] + " " + line.strip()).strip()
    if cur and cur["q"] and cur["a"]:
        items.append(cur)
    return items


def generate(topic: str, n: int = 5, user: str | None = None) -> dict:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 5
    if n < 1 or not (topic or "").strip():
        return {"topic": topic, "grounded": False, "items": [], "raw": "",
                "top_score": 0.0, "mode": "", "sources": [],
                "error": "Ask for at least one question on a topic."}
    n = min(n, MAX_N)
    hits, top_score, mode_label = _retrieve(topic, user=user)
    hits = hits[:TOP_K_GEN]                    # focus on the most on-topic chunks
    context = build_context(hits)
    grounded = _gate(topic, context, top_score)
 
    sources = []
    for h in hits:
        tag = f"{h['source']} \u00b7 {h['loc']}" + (" [OCR]" if h.get("ocr") else "")
        if tag not in sources:
            sources.append(tag)
 
    if not grounded:
        return {"topic": topic, "grounded": False, "items": [], "raw": "",
                "top_score": top_score, "mode": mode_label, "sources": sources}
 
    raw = ask_llm(GEN_PROMPT.format(n=n),
                  f"Context passages:\n{context}\n\nTopic: {topic}")
    # every practice answer must carry a citation; never more than asked for
    items = [it for it in _parse_qa(raw) if _TAG.search(it["a"])][:n]
    return {"topic": topic, "grounded": True, "items": items, "raw": raw,
            "top_score": top_score, "mode": mode_label, "sources": sources}
 
 
if __name__ == "__main__":
    import sys
    from src.store import close
 
    args = sys.argv[1:]
    n = 5
    if args and args[-1].isdigit():
        n = int(args[-1])
        args = args[:-1]
    topic = " ".join(args) or "the Division Algorithm"
 
    print(f"Topic: {topic}   (requesting {n} questions)\n")
    result = generate(topic, n)
    print(f"[{result['mode']}]  grounded: {result['grounded']}  "
          f"(top score: {result['top_score']:.3f})\n")
 
    if not result["grounded"]:
        print(REFUSAL, "\u2014 I can't set questions on a topic that isn't in your material.")
    elif not result["items"]:
        print("(couldn't parse Q/A pairs \u2014 raw model output below)\n")
        print(result["raw"])
    else:
        for i, it in enumerate(result["items"], 1):
            print(f"Q{i}. {it['q']}")
            print(f"    {it['a']}\n")
 
    print("sources:", "; ".join(result["sources"]))
    close()
 