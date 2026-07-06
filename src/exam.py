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
from src.answer import _retrieve, _grounded, build_context, ask_llm, REFUSAL
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
 
_QA_RE = re.compile(r"(?is)Q\d*\s*[:.\)]\s*(.+?)\s*A\d*\s*[:.\)]\s*(.+)")
_SPLIT_DASH = re.compile(r"(?m)^\s*-{3,}\s*$")
_SPLIT_Q = re.compile(r"(?im)^\s*(?=Q\d*\s*[:.\)])")
 
 
def _parse_qa(text: str) -> list[dict]:
    """Parse the model's Q/A output into [{'q','a'}]. Tolerant of numbering,
    markdown, and a missing/irregular delimiter."""
    # strip markdown emphasis / heading markers that can hide the Q/A anchors
    text = re.sub(r"[*`]", "", text)
    text = re.sub(r"(?m)^\s*[#>]+\s*", "", text)
 
    def _from_blocks(blocks):
        out = []
        for b in blocks:
            b = b.strip()
            if not b:
                continue
            m = _QA_RE.search(b)
            if m:
                out.append({"q": m.group(1).strip(), "a": m.group(2).strip()})
        return out
 
    items = _from_blocks(_SPLIT_DASH.split(text))
    if len(items) <= 1:                       # model ignored the --- delimiter
        items = _from_blocks(_SPLIT_Q.split(text)) or items
    return items
 
 
def generate(topic: str, n: int = 5) -> dict:
    hits, top_score, mode_label = _retrieve(topic)
    hits = hits[:TOP_K_GEN]                    # focus on the most on-topic chunks
    context = build_context(hits)
    grounded = False if top_score < HELP_SCORE else _grounded(topic, context)
 
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
    return {"topic": topic, "grounded": True, "items": _parse_qa(raw), "raw": raw,
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
 