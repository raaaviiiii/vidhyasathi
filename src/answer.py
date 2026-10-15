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
from src.config import (LLM_MODEL, ANSWER_SCORE, HELP_SCORE, SMALL_KB_MAX,
                        STRONG_SKIP_SCORE, BROAD_TOP_K, RETRIEVE_CANDIDATES)
from src.errors import LLMUnavailable
from src.retrieve import retrieve
from src.rerank import rerank
from qdrant_client.models import FieldCondition, MatchValue
from src.store import count, user_sources, user_chunks
from src.websearch import web_context
from src import converse

# how much of a named document an overview may read (characters, ~4 per token)
WHOLE_DOC_CHARS = 16000
 
REFUSAL = "I don't have that in the material."
 
# --- Quick Answer: a natural, grounded reply ----------------------------------
_GROUNDING = (
    "Grounding (strict):\n"
    "- Use ONLY facts from the notes passages in the student's message. Each passage "
    "starts with its source tag in square brackets, like [Notes \u00b7 p3] or "
    "[Module 1 \u00b7 slide 4]. Put the exact tag after the facts you use.\n"
    "- If the notes only partly cover the question, answer the part they cover and say "
    "plainly, in one short sentence, what your notes don't cover. Don't fill gaps from "
    "general knowledge.\n"
    "- Never present a reason, conclusion or example as fact unless the notes state it. If "
    "the student asks WHY and the notes don't give the reason, say 'Your notes don't give "
    "the reason for that' and share the closest thing they do say.\n"
    "- Analogies are welcome for explaining, but they must agree with the facts in the notes.\n"
    "- If the notes don't cover it at all, reply with exactly: "
    "\"I don't have that in the material.\""
)
SYSTEM_PROMPT = (
    "You are Vidhy\u0101sathi, a friendly study assistant who has read the student's "
    "notes closely. Chat with the student the way a sharp, helpful classmate would: "
    "natural, warm and direct.\n"
    "How to reply:\n"
    "- Answer straight away. Never start with 'According to the passages', 'Based on the "
    "context' or similar, and never mention 'passages' or 'context' at all; if you need "
    "to, say 'your notes'.\n"
    "- Match the length to the question: a quick question gets a quick answer (one to "
    "four sentences); 'explain', 'in detail', 'teach', 'list' or 'each' gets a fuller one "
    "with short paragraphs or a list.\n"
    "- Use the earlier conversation to understand what the student means by 'it', 'that', "
    "'each concept' and so on, and don't repeat what you already said unless asked.\n"
    "- If they ask what a slide or page says, or to read, recite or quote it, give the "
    "notes' wording faithfully.\n"
    + _GROUNDING
)

# --- Teach Me: a tutor that keeps the conversation going --------------------------
TEACH_PROMPT = (
    "You are Vidhy\u0101sathi, a patient study tutor who has read the student's notes "
    "closely. Teach like a good human tutor in a one-to-one chat.\n"
    "Teaching style:\n"
    "- Teach ONE idea at a time, in plain language, building on what the student already "
    "knows from earlier in the conversation. Never dump everything at once.\n"
    "- Use worked examples and everyday analogies to illustrate (you may invent the "
    "example, but not new facts about the subject).\n"
    "- If an idea needs a prerequisite the student may be missing, cover it briefly first.\n"
    "- Keep each reply short and end with one question that checks understanding or "
    "invites the next step, so it stays a conversation.\n"
    "- If the student says yes / ok / next, move on to the next idea. If they say no or "
    "seem stuck, explain the same idea more simply with a concrete example.\n"
    "- Never mention 'passages' or 'context'; say 'your notes' if you need to.\n"
    + _GROUNDING
)

# --- Pushback: the student says the last reply was wrong / incomplete / differs from class
FEEDBACK_PROMPT = (
    "You are Vidhy\u0101sathi, a study assistant re-checking your previous reply because the "
    "student pushed back: they say it was wrong, incomplete, not what they asked, or that "
    "their class taught something different. Respond like an honest, humble tutor:\n"
    "- Re-read the notes passages and your previous reply. Say plainly what the notes "
    "actually state (cite the [tags]) and openly take back anything you said that the notes "
    "don't state.\n"
    "- If their earlier question wasn't answered, answer exactly that question now, directly, "
    "using only the notes. If the notes don't contain the answer, say so plainly.\n"
    "- If they were taught something different in class, don't argue: say what the notes "
    "say, ask what they were taught, and suggest confirming with their teacher using "
    "'Ask a helper'.\n"
    "- Keep it short and conversational. Never mention 'passages' or 'context'."
)

# what each kind of reaction asks the tutor to do next
DIRECTIVES = {
    "yes": ("The student says they understood your last explanation. Acknowledge it in a few "
            "words, then move on to the NEXT idea of this topic that you have NOT explained "
            "yet. If the notes have nothing more on it, say so and offer a related topic from "
            "the notes or a quick practice question."),
    "continue": "The student wants you to keep going: teach the NEXT idea you have not covered yet.",
    "no": ("The student did not understand your last explanation. Explain that same idea "
           "again, more simply and in different words, with a concrete everyday example."),
}

# --- Groundedness gate: do the notes cover this (fully or partly)? ---------------
GROUND_CHECK = (
    "You are a retrieval checker for a study app. Given some notes passages and a "
    "student's question, decide whether the passages contain information that answers "
    "the question, fully or partly. Reply with ONLY one word: YES or NO.\n"
    "Say YES if the passages explain, define, list or describe what the question asks "
    "about, even if they don't cover every detail. Say NO only if the passages are about "
    "something else or merely share a word with the question."
)

# --- Overview: broad "explain everything in the module" asks -----------------
# A pointed groundedness check ("do these passages answer THIS?") always fails for a
# whole-module ask, so those questions used to refuse. For a broad ask we instead
# synthesise a structured tour of whatever the student's own passages actually cover.
OVERVIEW_PROMPT = (
    "You are Vidhy\u0101sathi, a study assistant giving a STRUCTURED OVERVIEW of the "
    "student's own notes: walk them through the concepts the passages cover, like a "
    "friend going through the module with them.\n"
    "- Go concept by concept, in the order the notes present them: the concept's name in "
    "bold, its explanation, then the exact bracketed [source \u00b7 loc] tag it came from.\n"
    "- Depth: if the student asks for detail ('in detail', 'each concept', 'fully', "
    "'properly'), give each concept a proper explanation of a few sentences using "
    "everything the notes say about it; otherwise one or two lines each.\n"
    "- Only include concepts that genuinely appear in the passages. Do NOT add anything "
    "from outside knowledge. If a concept is only named in the notes without an "
    "explanation, give what the notes say and add '(only named in your notes)'.\n"
    "- Never mention 'passages' or 'context'; say 'your notes'.\n"
    "- If the passages contain no teachable concepts at all, say exactly: "
    "\"I don't have that in the material.\""
)

from src.answer_words import STOP as _STOP, SCOPE as _SCOPE, FOLLOWUP as _FOLLOWUP, words as _words  # noqa: E402
from src.answer_words import BROAD_PHRASES as _BROAD_PHRASES, is_broad as _is_broad  # noqa: E402


def _broad_topic(question: str) -> str:
    """What's left of a broad ask once scope/request words are removed: '' for
    'explain all the concepts in my notes', 'quantum mechanics' for 'summarise
    quantum mechanics'."""
    return " ".join(w for w in _words(question)
                    if w not in _STOP and w not in _SCOPE and not w.isdigit())


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


_NUMBERED_ASK = re.compile(r"\b(?:module|mod|unit|chapter|ch|part|lecture|lesson|deck|"
                           r"section|week|topic)\s*[-#:.]?\s*(\d{1,3})\b", re.I)
_NUMBERED_LABEL = re.compile(r"^\s*(?:(?:module|mod|unit|chapter|ch|part|lecture|lesson|"
                             r"week|copy\s+of\s+module)\s*[-#:.]?\s*)?(\d{1,3})\b", re.I)


def _match_documents(question: str, topic: str, user: str | None) -> list[str]:
    """Labels of the student's documents the ask names:
    "summarise module 3"                       -> "3. DATA COMMUNICATION CONCEPTS"
    "each concept of data communication"       -> the same document
    "my graphs notes"                          -> every document whose name has "graphs"."""
    labels = [l for l in user_sources(user) if l != converse.HELPER_SOURCE]
    # 1. by number: "module 3", "unit 2", "chapter 4"
    nums = {n.lstrip("0") or "0" for n in _NUMBERED_ASK.findall(question or "")}
    if nums:
        hit = []
        for label in labels:
            m = _NUMBERED_LABEL.match(label)
            inner = {x.lstrip("0") or "0" for x in _NUMBERED_ASK.findall(label)}
            if (m and (m.group(1).lstrip("0") or "0") in nums) or (inner & nums):
                hit.append(label)
        if hit:
            return sorted(hit)
    # 2. every meaningful word of the document's name appears in the ask
    qw = {_stem(w) for w in _words(question)}
    named = []
    for label in labels:
        lw = {_stem(w) for w in _words(label) if not w.isdigit()} - _STOP
        meaningful = {w for w in lw if w not in {_stem(x) for x in _SCOPE}}
        if meaningful and lw <= qw:
            named.append((len(lw), label))
    if named:
        best = max(n for n, _ in named)
        return sorted(label for n, label in named if n == best)
    # 3. the topic left in the ask is part of a document's name ("guided media")
    tw = {_stem(w) for w in topic.split()}
    if not tw:
        return []
    return sorted(label for label in labels if tw <= {_stem(w) for w in _words(label)})


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
    r"|\b\d+\s+[+\-*/]\s+\d+\b"
    r"|[\u2264\u2265\u230a\u230b\u2211\u222b\u221a\u220f\u2260\u00b1\u00d7\u00f7]"
)
 
# A citation tag always has the "source · locator" shape; ordinary brackets such as
# an interval [0, b-1] don't, and must survive the scrub.
_NON_WEB_TAG = re.compile(r"\[(?!web\s*\u00b7)[^\]\n]*\u00b7[^\]\n]*\]")
 
 
def _has_math(t: str) -> bool:
    return bool(_MATH_RE.search(t))
 
 
def _is_refusal(text: str) -> bool:
    norm = text.replace("\u2019", "'").lower()
    return "i don't have that in the material" in norm
 
 
_ENTITY = re.compile(r"&(#0*91|#x0*5b|lbrack|lsqb|#0*93|#x0*5d|rbrack|rsqb|#0*183|#x0*b7|middot);",
                     re.I)
_ENTITY_MAP = {"91": "[", "5b": "[", "lbrack": "[", "lsqb": "[", "93": "]", "5d": "]",
               "rbrack": "]", "rsqb": "]", "183": "\u00b7", "b7": "\u00b7", "middot": "\u00b7"}


def _decode_tag_entities(text: str) -> str:
    def rep(m):
        k = m.group(1).lower().lstrip("#").lstrip("x").lstrip("0") if m.group(1).startswith("#") \
            else m.group(1).lower()
        return _ENTITY_MAP.get(k, m.group(0))
    return _ENTITY.sub(rep, text)


def _scrub_web_tags(text: str) -> str:
    """A web answer may ONLY carry [web \u00b7 ...] tags. Strip any hallucinated
    notes-style tag so web content can never be laundered as the student's own notes."""
    text = _NON_WEB_TAG.sub("", _decode_tag_entities(text))
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
 
 
def _style_exemplars(hits: list[dict]) -> str:
    """The notes' VOICE without their source tags, so a web answer can imitate the
    style without copying the tag format into a fake citation."""
    return "\n\n".join(h["text"] for h in hits[:2]) if hits else "(no local notes to match)"
 
 
def chat_llm(messages: list[dict]) -> str:
    # num_ctx: Ollama's default window is small and silently cuts long prompts, so the
    # model would answer from a truncated set of notes. 8k fits notes + conversation.
    resp = ollama.chat(model=LLM_MODEL, messages=messages,
                       options={"temperature": 0.2, "num_ctx": 8192})
    return resp["message"]["content"].strip()
 
 
def call_llm(messages: list[dict]) -> str:
    """Every model call goes through here, so an Ollama outage surfaces as one clear
    LLMUnavailable error instead of a raw stack trace."""
    try:
        return chat_llm(messages)
    except LLMUnavailable:
        raise
    except Exception as e:
        raise LLMUnavailable(str(e)) from e


def ask_llm(system: str, user: str) -> str:
    return call_llm([{"role": "system", "content": system},
                     {"role": "user", "content": user}])


def _clean_history(history) -> list[dict]:
    """Client-supplied chat history: only user/assistant turns with text survive (a
    forged 'system' turn could override the grounding rules). Last 20 turns."""
    out = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            item = {"role": role, "content": content[:4000]}
            meant = m.get("meant")
            if role == "user" and isinstance(meant, str) and meant.strip():
                item["meant"] = meant.strip()[:600]
            out.append(item)
    return out[-20:]
 
 
def _grounded(query: str, context: str) -> bool:
    """CRAG-style retrieval evaluator: do the passages actually cover the question?
    Conservative — anything not a clear YES counts as not grounded."""
    resp = ask_llm(GROUND_CHECK,
                   f"Context passages:\n{context}\n\nQuestion: {query}\n\nAnswer YES or NO:")
    return resp.strip().lower().startswith("y")


def _gate(query: str, context: str, top_score: float) -> bool:
    """Three-band grounding decision, shared by answer/exam/grade.
      score >= STRONG_SKIP_SCORE -> trust the strong match, SKIP the LLM check (latency).
      score <  HELP_SCORE        -> too weak, refuse without a check.
      in between                 -> run the explicit CRAG groundedness check.
    Halves the model calls on confident hits (was: check + generate on every turn)."""
    if top_score >= STRONG_SKIP_SCORE:
        return True
    if top_score < HELP_SCORE:
        return False
    return _grounded(query, context)
 
 
def _cited_hits(text: str, hits: list[dict]) -> list[dict]:
    """The hits an answer actually cites ([source · loc] tags). If it cites nothing
    recognisable, fall back to the top three hits."""
    cited = [h for h in hits if f"[{h['source']} \u00b7 {h['loc']}]" in (text or "")]
    return cited or hits[:3]


def build_context(hits: list[dict]) -> str:
    return "\n\n".join(f"[{h['source']} \u00b7 {h['loc']}] {h['text']}" for h in hits)
 
 
def _doc_order(chunks: list[dict]) -> list[dict]:
    return sorted(chunks, key=lambda c: (c["source"], c["order"], c["chunk_id"]))
 
 
def _hist_for_model(history: list[dict], turns: int = 6) -> list[dict]:
    """Recent conversation for the answering model. The LAST reply is kept (almost) whole,
    so the model knows exactly what it already said and doesn't say it again; older
    turns are shortened so the notes still fit."""
    recent = history[-turns:]
    last_bot = max((i for i, m in enumerate(recent) if m["role"] == "assistant"), default=-1)
    out = []
    for i, m in enumerate(recent):
        limit = 2000 if i == last_bot else (600 if m["role"] == "assistant" else 400)
        c = m["content"] if len(m["content"]) <= limit else m["content"][:limit] + " \u2026"
        meant = m.get("meant")
        if m["role"] == "user" and meant and meant != m["content"]:
            c += f"\n(meaning: {meant})"
        out.append({"role": m["role"], "content": c})
    return out


def _retrieve(query: str, user: str | None = None, broad: bool = False,
              source=None):
    """Return (hits, top_score, mode_label) for a query, scoped to `user` (and to one
    or more document labels when `source` is given). `broad=True` widens the pool and
    keeps more chunks, for overviews. top_score is the best cosine among the kept hits —
    the gate's bands are defined on cosine, not on the re-ranker's order."""
    total = count(user)
    small = 0 < total <= SMALL_KB_MAX
    keep = BROAD_TOP_K if broad else None          # None -> rerank's default TOP_K
    if small:
        candidates = retrieve(query, top_k=total, user=user, source=source)
        top_score = max((c["score"] for c in candidates), default=0.0)
        hits = _doc_order(candidates)
        label = f"small-kb ({total} chunks): full context"
    else:
        cand_k = max(RETRIEVE_CANDIDATES, BROAD_TOP_K * 2) if broad else RETRIEVE_CANDIDATES
        candidates = retrieve(query, top_k=cand_k, user=user, source=source)
        ranked = rerank(query, candidates, top_k=keep) if keep else rerank(query, candidates)
        top_score = max((c["score"] for c in ranked), default=0.0)
        hits = ranked
        label = f"large-kb ({total} chunks): retrieve + re-rank"
    return hits, top_score, label


def _whole_documents(labels: list[str], user: str | None, budget: int = WHOLE_DOC_CHARS):
    """Every chunk of the named documents, in order — "explain each concept of module 2"
    deserves the whole module, not the 12 passages that happen to rank highest. If the
    documents are longer than the budget, chunks are sampled evenly across them."""
    chunks = []
    for label in labels:
        chunks += user_chunks(user, FieldCondition(key="source", match=MatchValue(value=label)))
    if not chunks:
        return []
    total = sum(len(c["text"]) for c in chunks)
    if total > budget:
        keep = max(1, int(len(chunks) * budget / total))
        step = len(chunks) / keep
        chunks = [chunks[int(i * step)] for i in range(keep)]
    for c in chunks:
        c["score"] = 1.0
    return chunks


def _gate_lookup(query: str, user: str | None):
    hits, top_score, label = _retrieve(query, user=user)
    context = build_context(hits)
    return hits, top_score, label, context, _gate(query, context, top_score)


def _material_lookup(question: str, mode: str, history: list[dict] | None,
                     user: str | None = None, original: str | None = None):
    """Find the notes for a (standalone) question.
    Returns (hits, top_score, mode_label, context, grounded, overview)."""
    hist = _clean_history(history)
    original = original or question
    if _is_broad(question):
        topic = _broad_topic(question)
        docs = _match_documents(question, topic, user)
        if docs:
            hits = _whole_documents(docs, user)
            if hits:
                return (hits, 1.0, f"whole document: {', '.join(docs)}", build_context(hits),
                        True, True)
        if not topic or docs:
            # whole material: grounded iff there is material
            hits, top_score, label = _retrieve(question, user=user, broad=True)
            return hits, top_score, label, build_context(hits), bool(hits), True
        # a broad ask ABOUT a topic ("summarise quantum mechanics"): the topic itself
        # has to be in the material, exactly like a pointed question
        hits, top_score, label = _retrieve(topic, user=user, broad=True)
        context = build_context(hits)
        return hits, top_score, label, context, _gate(topic, context, top_score), True

    hits, top_score, label, context, ok = _gate_lookup(question, user)
    if ok:
        return hits, top_score, label, context, True, False

    # rescue: a message that leans on the conversation and still didn't ground — retry
    # with the conversation window, but never let it drag an old topic into a NEW one
    if hist and converse.needs_context(original):
        wq = converse.window_query(original, hist)
        if wq != question:
            h2, s2, l2, c2, ok2 = _gate_lookup(wq, user)
            if ok2 and converse.topic_words(original) <= set(_words(c2)):
                return h2, s2, l2, c2, True, False

    return hits, top_score, label, context, False, False


def _resolve(question: str, history: list[dict]) -> str:
    """A follow-up rewritten into a standalone question (or converse.CHAT)."""
    raw = call_llm(converse.rewrite_messages(question, history))
    q2 = converse.clean_rewrite(raw, question, history)
    if q2 is None:
        return converse.window_query(question, history)
    return q2


def _reply(text: str, decision: str, **extra) -> dict:
    out = {"answer": text, "raw_answer": text, "decision": decision, "grounded": False,
           "offer_help": False, "caveat": "", "web_used": False, "web_sources": [],
           "top_score": 0.0, "weak": False, "mode": "conversation", "sources": [],
           "resolved_question": None, "suggestions": [], "intent": "chat"}
    out.update(extra)
    return out


def _chat(question: str, history: list[dict], user: str | None, hint: str = "") -> dict:
    """Small talk, questions about the app or about what material there is."""
    raw = call_llm(converse.chat_messages(question, history, user, hint)).strip()
    raw = _NON_WEB_TAG.sub("", raw).strip() or "Hi! Ask me anything from your notes."
    return _reply(raw, "chat")


_DETAIL_ASK = ("detail", "each", "every", "all the", "everything", "fully", "thorough",
               "in depth", "elaborate", "whole")


def _checklist(mode: str, kind: str, question: str) -> str:
    """Instructions repeated at the END of the message, where a small model follows
    them best: what to do now, stay grounded, and never repeat earlier explanations."""
    do = DIRECTIVES.get(kind, "")
    if mode == "teach":
        wants_detail = any(w in question.lower() for w in _DETAIL_ASK)
        size = ("The student asked for detail: be thorough but organised." if wants_detail
                else "Teach only the next ONE idea, in under about 150 words.")
        return (f"(Tutor checklist: {do + ' ' if do else ''}{size} Use only what the notes "
                "say and cite their [tags]. Never repeat an explanation you already gave "
                "earlier in this conversation; build on it. End with one short question.)")
    return (f"(Checklist: {do + ' ' if do else ''}Answer from the notes only and cite their "
            "[tags]. If the notes don't state something, such as a reason, say so instead of "
            "guessing. Don't repeat what you already told the student.)")


def _finish(raw, decision, question, query, kind, hits, top_score, mode_label, grounded):
    """Result for a reply that skips the normal answer path (pushback)."""
    return {
        "answer": raw, "raw_answer": raw, "decision": decision, "grounded": grounded,
        "offer_help": True, "caveat": "", "web_used": False, "web_sources": [],
        "top_score": top_score, "weak": True, "mode": mode_label,
        "resolved_question": query if query != question else None, "intent": kind,
        "suggestions": [],
        "sources": [{"source": h["source"], "loc": h["loc"], "score": h["score"],
                     "ocr": h["ocr"]} for h in hits],
    }


def answer(question: str, mode: str = "quick", history: list[dict] | None = None,
           web: bool = False, user: str | None = None) -> dict:
    hist = _clean_history(history)
    if not (question or "").strip():
        return _reply(REFUSAL, "refuse", offer_help=False, weak=True)

    # 1. what does the message mean?
    kind = converse.classify(question, hist, mode)
    if kind == "chat":
        return _chat(question, hist, user)
    query = question
    if kind in ("yes", "no", "continue", "feedback"):
        # a reaction to the last reply: stay on the topic the student was asking about
        query = converse.last_topic(hist) or converse.window_query(question, hist)
        if not count(user):
            return _chat(question, hist, user)
    elif kind == "followup":
        query = _resolve(question, hist)
        if query == converse.CHAT:
            return _chat(question, hist, user)

    # 2. find the notes: a named slide/page directly, otherwise search
    recall = []
    if converse.wanted_locs(query):
        named = _match_documents(query, _broad_topic(query), user)
        recall, ambiguous = converse.locate(query, user, named, hist)
        if ambiguous and not recall:
            return _chat(question, hist, user, hint=(
                "Several of the student's documents have that slide/page: "
                + ", ".join(ambiguous) + ". Ask which document they mean."))
    if recall:
        for c in recall:
            c["score"] = 1.0
        hits, top_score, mode_label = recall, 1.0, "exact slide/page"
        context, grounded, overview = build_context(hits), True, False
    else:
        hits, top_score, mode_label, context, grounded, overview = _material_lookup(
            query, mode, hist, user=user, original=question)

    # 3. answer
    asked = f"Student's message: {question}" + (
        f"\n(What they mean: {query})" if query != question and kind not in DIRECTIVES
        and kind != "feedback" else "")
    if kind == "feedback":
        # pushback: re-check against the notes and own up to anything unsupported — even
        # when the notes are thin, this is a conversation, not a search
        raw = call_llm([{"role": "system", "content": FEEDBACK_PROMPT}, *_hist_for_model(hist),
                        {"role": "user", "content":
                            f"Notes passages about what they asked earlier ({query}):\n"
                            f"{context or '(nothing relevant in the notes)'}\n\n{asked}"}])
        return _finish(raw, "answer_uncertain", question, query, kind, hits, top_score,
                       mode_label, grounded)
    if kind in DIRECTIVES and not grounded and hits:
        grounded = True                     # the lesson topic was found before; keep going
    if not grounded:
        raw = REFUSAL
        decision = "refuse"
    elif overview:
        # whole-module tour, synthesised from the student's own passages
        raw = call_llm([{"role": "system", "content": OVERVIEW_PROMPT},
                        *_hist_for_model(hist, 4),
                        {"role": "user", "content":
                            f"Notes passages:\n{context}\n\n{asked}"}])
        decision = "refuse" if _is_refusal(raw) else "answer"
    else:
        system = TEACH_PROMPT if mode == "teach" else SYSTEM_PROMPT
        messages = [{"role": "system", "content": system}, *_hist_for_model(hist)]
        messages.append({"role": "user", "content":
                         f"Notes passages:\n{context}\n\n{asked}\n\n"
                         + _checklist(mode, kind, question)})
        raw = call_llm(messages)
        if _is_refusal(raw):
            decision = "refuse"
        elif top_score < ANSWER_SCORE:
            decision = "answer_uncertain"
        else:
            decision = "answer"

    # --- Layer 3 (last resort): grounding failed AND the web toggle is on --------
    # Web search uses the standalone question, never the conversation window.
    web_used = False
    web_sources: list[dict] = []
    if web and decision == "refuse":
        passages = web_context(query)
        if passages:
            wctx = build_context(passages)
            style_ref = _style_exemplars(hits)
            web_raw = call_llm([
                {"role": "system", "content": WEB_PROMPT},
                {"role": "user", "content":
                    f"WEB passages (the only facts you may use):\n{wctx}\n\n"
                    f"Untagged samples of the student's own note style to imitate:\n{style_ref}\n\n"
                    f"Question: {query}"},
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
    ocr_used = any(h.get("ocr") for h in _cited_hits(shown, hits))
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
        "resolved_question": query if query != question else None,
        "intent": kind,
        "suggestions": converse.suggestions(user, hits) if decision == "refuse" else [],
        "sources": ([{"source": w["source"], "loc": "", "url": w["url"], "score": None,
                      "ocr": False} for w in web_sources] if web_used else
                    [{"source": h["source"], "loc": h["loc"],
                      "score": h["score"], "ocr": h["ocr"]} for h in hits]),
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
 