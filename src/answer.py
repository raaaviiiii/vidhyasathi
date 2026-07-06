"""Answer step: grounded, cited answer. Size-aware (small KB = whole doc;
large KB = retrieve + re-rank).
 
Modes (each reconfigures behaviour over the SAME retrieve->gate spine):
  - "quick"  : single-shot grounded reply + self-check.
  - "teach"  : multi-turn tutor over the student's own material (prompt + memory).
 
Layers (grounding sources, always disclosed):
  1/2 = the student's own material (notes/syllabus KB)
  3   = web retrieval, LAST RESORT, only when web=True; re-expressed in the notes'
        style but ALWAYS tagged [web · site] + footer-disclosed.
 
Retrieval query (teach mode): the student's question is used AS-IS first. Only if that
does not ground do we retry with a conversation-window query (question + previous turn)
to rescue contextless follow-ups ('why?', 'an example'). This keeps a topic-switch from
dragging the previous topic into the new question. Web search always uses the clean
current question, never the window.
 
Confidence gate (CRAG-style), identical in every mode:
  1. GROUNDEDNESS gate FIRST — explicit check that the passages contain the answer,
     run BEFORE generation so leaked outside-knowledge answers are never produced.
  2. Not grounded -> refuse (fall through to web Layer 3 if web=True).
  3. Grounded -> generate, then label by retrieval score
     (>= ANSWER_SCORE = answer; >= HELP_SCORE = answer_uncertain + offer help).
"""
import re
import ollama
from src.config import LLM_MODEL, ANSWER_SCORE, HELP_SCORE, SMALL_KB_MAX
from src.retrieve import retrieve
from src.rerank import rerank
from src.store import count
from src.websearch import web_context
 
REFUSAL = "I don't have that in the material."
 
# --- Quick Answer: terse, grounded, cited ------------------------------------
SYSTEM_PROMPT = (
    "You are a study assistant. Answer the student's question using ONLY the "
    "context passages provided. Each passage begins with its source tag in "
    "square brackets, like [Syllabus \u00b7 p3] or [Notes \u00b7 Methods]. After "
    "each fact you use, cite that exact tag. If the context does not contain "
    "the answer, say exactly: \"I don't have that in the material.\" Do not use "
    "outside knowledge."
)
 
# --- Teach Me: pedagogical delivery + memory, strict off-material refusal ------
TEACH_PROMPT = (
    "You are a patient study tutor. Teach the student using the context passages "
    "provided. Each passage begins with a source tag in square brackets, like "
    "[Notes \u00b7 p3]; cite the exact tag after facts you use.\n"
    "GROUNDING (strict):\n"
    "- For topics that DO appear in the context, you may explain, give worked "
    "examples, and use analogies freely. Invent examples to illustrate, but do not "
    "invent new facts about the subject beyond what the context supports.\n"
    "- If the student asks about a topic that is NOT covered by the context passages, "
    "do NOT redirect to the current topic and do NOT guess. Reply with EXACTLY this, "
    "and nothing else: \"I don't have that in the material.\"\n"
    "Teaching style:\n"
    "- Teach ONE idea at a time, in plain language, building on what the student "
    "already knows from earlier in the conversation.\n"
    "- If an idea needs a prerequisite the student may be missing, cover it briefly first.\n"
    "- Keep each reply short. End by checking understanding or asking one guiding "
    "question, so it stays a back-and-forth, not a lecture.\n"
    "- Adapt: if the student is stuck, slow down and give a concrete example; if they "
    "clearly get it, move on. Never dump everything at once."
)
 
# --- Groundedness gate: strict retrieval checker (one word out) ---------------
GROUND_CHECK = (
    "You are a strict retrieval checker. Given some context passages and a question, "
    "decide whether the passages actually contain the information needed to answer it. "
    "Reply with ONLY one word: YES or NO. Say YES only if the answer is genuinely present "
    "in the passages. If the passages are about a different topic, or only loosely "
    "related, reply NO."
)
 
# --- Layer 3: web-grounded, but re-expressed in the student's note style ------
WEB_PROMPT = (
    "The student's OWN material does not cover this, so you are given WEB passages "
    "(each tagged [web \u00b7 site]) plus a few UNTAGGED samples of the student's own note "
    "style. Answer using ONLY facts found in the WEB passages \u2014 do not add anything from "
    "your own knowledge. Re-express the answer to MATCH the vocabulary, notation, and "
    "step-by-step style of the student's notes, so it reads like their own material.\n"
    "CITATIONS (strict): begin the explanation directly, with NO heading or preamble. The "
    "ONLY square-bracket tag you may ever write is a [web \u00b7 site] tag; never write any "
    "other bracketed tag (e.g. a notes/module tag), because this content is NOT from the "
    "student's notes. Cite the [web \u00b7 site] tag after facts you use. If the web passages "
    "do not actually answer the question, say exactly: \"I don't have that in the material.\""
)
 
 
# Fire ONLY on a genuine equation/expression the OCR could have mangled — NOT on
# a lone variable or a simple inequality wrapped in $...$.
_MATH_RE = re.compile(
    r"\$\$[^$]+\$\$"
    r"|(?<![<>=!])=(?!=)"
    r"|\\(?:frac|sqrt|sum|int|prod|binom|cdot|times|div|pmod|bmod|equiv|leq|geq|neq)\b"
    r"|\b\d+\s*[+\-*/]\s*\d+"
    r"|[\u2264\u2265\u230a\u230b\u2211\u222b\u221a\u220f\u2260\u00b1\u00d7\u00f7]"
)
 
_NON_WEB_TAG = re.compile(r"\[(?!web\b)[^\]]*\]")
 
 
def _has_math(t: str) -> bool:
    return bool(_MATH_RE.search(t))
 
 
def _is_refusal(text: str) -> bool:
    norm = text.replace("\u2019", "'").lower()
    return "i don't have that in the material" in norm
 
 
def _scrub_web_tags(text: str) -> str:
    """A web answer may ONLY carry [web \u00b7 ...] tags. Strip any hallucinated
    notes-style tag so web content can never be laundered as the student's own notes."""
    text = _NON_WEB_TAG.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
 
 
def _style_exemplars(hits: list[dict]) -> str:
    """The notes' VOICE without their source tags, so a web answer can imitate the
    style without copying the tag format into a fake citation."""
    return "\n\n".join(h["text"] for h in hits[:2]) if hits else "(no local notes to match)"
 
 
def chat_llm(messages: list[dict]) -> str:
    resp = ollama.chat(model=LLM_MODEL, messages=messages, options={"temperature": 0.1})
    return resp["message"]["content"].strip()
 
 
def ask_llm(system: str, user: str) -> str:
    return chat_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}])
 
 
def _grounded(query: str, context: str) -> bool:
    """CRAG-style retrieval evaluator: do the passages actually cover the question?
    Conservative — anything not a clear YES counts as not grounded."""
    resp = ask_llm(GROUND_CHECK,
                   f"Context passages:\n{context}\n\nQuestion: {query}\n\nAnswer YES or NO:")
    return resp.strip().lower().startswith("y")
 
 
def build_context(hits: list[dict]) -> str:
    return "\n\n".join(f"[{h['source']} \u00b7 {h['loc']}] {h['text']}" for h in hits)
 
 
def _doc_order(chunks: list[dict]) -> list[dict]:
    return sorted(chunks, key=lambda c: (c["source"], c["order"], c["chunk_id"]))
 
 
def _window_query(question: str, history: list[dict] | None) -> str:
    """Fold in the last user turn so contextless follow-ups still retrieve on the
    current topic. Used ONLY as a rescue when the standalone question fails to ground."""
    prior_user = [m["content"] for m in (history or []) if m.get("role") == "user"]
    window = prior_user[-1:] + [question]
    return " ".join(window)
 
 
def _retrieve(query: str):
    """Return (hits, top_score, mode_label) for a query."""
    total = count()
    small = 0 < total <= SMALL_KB_MAX
    if small:
        candidates = retrieve(query, top_k=total)
        top_score = max((c["score"] for c in candidates), default=0.0)
        hits = _doc_order(candidates)
        label = f"small-kb ({total} chunks): full context"
    else:
        candidates = retrieve(query)
        ranked = rerank(query, candidates)
        top_score = ranked[0]["score"] if ranked else 0.0
        hits = ranked
        label = f"large-kb ({total} chunks): retrieve + re-rank"
    return hits, top_score, label
 
 
def _material_lookup(question: str, mode: str, history: list[dict] | None):
    """Standalone question first; window rescue only if it doesn't ground.
    Returns (hits, top_score, mode_label, context, grounded)."""
    hits, top_score, label = _retrieve(question)
    context = build_context(hits)
    grounded = False if top_score < HELP_SCORE else _grounded(question, context)
    if grounded:
        return hits, top_score, label, context, True
 
    # rescue: a contextless follow-up ('why?') — retry with the conversation window
    if mode == "teach" and history:
        wq = _window_query(question, history)
        if wq != question:
            h2, s2, l2 = _retrieve(wq)
            c2 = build_context(h2)
            g2 = False if s2 < HELP_SCORE else _grounded(wq, c2)
            if g2:
                return h2, s2, l2, c2, True
 
    return hits, top_score, label, context, False
 
 
def answer(question: str, mode: str = "quick", history: list[dict] | None = None,
           web: bool = False) -> dict:
    hits, top_score, mode_label, context, grounded = _material_lookup(question, mode, history)
 
    if not grounded:
        raw = REFUSAL
        decision = "refuse"
    else:
        if mode == "teach":
            messages = [{"role": "system", "content": TEACH_PROMPT}]
            messages += (history or [])
            messages.append({"role": "user",
                             "content": f"Context passages:\n{context}\n\nStudent: {question}"})
            raw = chat_llm(messages)
        else:
            raw = ask_llm(SYSTEM_PROMPT, f"Context passages:\n{context}\n\nQuestion: {question}")
        if _is_refusal(raw):
            decision = "refuse"
        elif top_score < ANSWER_SCORE:
            decision = "answer_uncertain"
        else:
            decision = "answer"
 
    # --- Layer 3 (last resort): grounding failed AND the web toggle is on --------
    # Web search uses the CLEAN current question, never the conversation window.
    web_used = False
    web_sources: list[dict] = []
    if web and decision == "refuse":
        passages = web_context(question)
        if passages:
            wctx = build_context(passages)
            style_ref = _style_exemplars(hits)
            web_raw = chat_llm([
                {"role": "system", "content": WEB_PROMPT},
                {"role": "user", "content":
                    f"WEB passages (the only facts you may use):\n{wctx}\n\n"
                    f"Untagged samples of the student's own note style to imitate:\n{style_ref}\n\n"
                    f"Question: {question}"},
            ])
            web_raw = _scrub_web_tags(web_raw)   # hard guarantee: no notes-style tags survive
            if not _is_refusal(web_raw):
                raw = web_raw
                decision = "answer_web"
                web_used = True
                web_sources = [{"source": p["source"], "url": p["url"]} for p in passages]
 
    shown = REFUSAL if decision == "refuse" else raw
    offer_help = decision in ("answer_uncertain", "refuse")
 
    # OCR nudge applies ONLY to answers drawn from the student's OCR'd material —
    # never to a web answer (its text isn't from a scanned source) or a refusal.
    ocr_used = any(h.get("ocr") for h in hits)
    caveat = ("\u21b3 This draws on a handwritten/scanned source \u2014 worth "
              "double-checking the equation.") if (
        decision in ("answer", "answer_uncertain") and ocr_used and _has_math(shown)) else ""
 
    return {
        "answer": shown,
        "raw_answer": raw,
        "decision": decision,
        "grounded": grounded,
        "offer_help": offer_help,
        "caveat": caveat,
        "web_used": web_used,
        "web_sources": web_sources,
        "top_score": top_score,
        "weak": decision != "answer",
        "mode": mode_label,
        "sources": [{"source": h["source"], "loc": h["loc"],
                     "score": h["score"], "ocr": h["ocr"]} for h in hits],
    }
 
 
if __name__ == "__main__":
    import sys
    from src.store import close
    q = " ".join(sys.argv[1:]) or "State the Division Algorithm"
    print(f"Q: {q}\n")
    result = answer(q)
    print(f"[{result['mode']}]  grounded: {result['grounded']}  decision: {result['decision']}\n")
    print("ANSWER:")
    print(result["answer"])
    if result["caveat"]:
        print("\n" + result["caveat"])
    if result["offer_help"]:
        print("\n\u2192 Not fully sure on this one \u2014 you can ask a human helper. (routing comes later)")
    print(f"\n(top retrieval score: {result['top_score']:.3f})")
    seen = []
    for s in result["sources"]:
        tag = f"{s['source']} \u00b7 {s['loc']}" + (" [OCR]" if s["ocr"] else "")
        if tag not in seen:
            seen.append(tag)
    print("sources:", "; ".join(seen))
    close()
 