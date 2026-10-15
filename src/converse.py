"""Conversation layer: makes Vidhyāsathi talk like a person who has read your notes.

A student chats the way people chat: "teach me it", "explain each concept in detail",
"what about the second one?", "ok", "thanks", "what's on slide 4?". Searching the notes
with those literal words finds nothing, so before any search this layer works out what
the message actually MEANS:

  classify()        small talk / what-do-I-have / vague / follow-up / plain question
  needs_context()   does this message lean on the conversation ("it", "each concept")?
  rewrite prompt    turns a follow-up into one standalone question, using the history
  outline()         the student's material as a table of contents (cached), so the
                    assistant can say what it knows, list topics, suggest the closest
                    topic, and answer "what's on slide 4 of X"
  locate()          finds the slide / page a message points at
"""
import re
import threading

from src.store import count, user_chunks, chunks_at

# words that are never the topic of a question
from src.answer_words import STOP, FOLLOWUP, SCOPE, words, is_broad

CHAT = "CHAT"

# ---------------------------------------------------------------- small talk
_GREETING = re.compile(
    r"^(hi+|hey+|hello+|helo|hai|yo|namaste|namaskaram|vanakkam|hola|sup|howdy|"
    r"good\s+(morning|afternoon|evening|night|day)|what'?s\s+up|wassup)\b[\s!,.?]*", re.I)
_THANKS = re.compile(
    r"^(thank\s+you(\s+(so|very)\s+much)?|thanks?(\s+(a\s+lot|so\s+much|again|man|bro|buddy))?|"
    r"thx|ty|tysm|cheers|much\s+appreciated)\b[\s!,.?]*", re.I)
_BYE = re.compile(r"^(bye+|goodbye|good\s*bye|see\s+(you|ya)|cya|gn|take\s+care|"
                  r"that'?s\s+all|done\s+for\s+(now|today))\b[\s!,.?]*", re.I)
_ABOUT = re.compile(
    r"\b(who\s+are\s+you|what\s+are\s+you|your\s+name|what\s+can\s+you\s+do|"
    r"how\s+(do|can)\s+i\s+use\s+(you|this|the\s+app)|how\s+does\s+this\s+(app\s+)?work|"
    r"are\s+you\s+(an?\s+)?(ai|bot|robot|human|real)|how\s+are\s+you|"
    r"what\s+is\s+vidhy)", re.I)
_MATERIAL_META = re.compile(
    r"\b(what|which)\s+(files?|documents?|docs?|notes|materials?|slides|decks|modules|"
    r"chapters|units|subjects)\s+(do|did|have|are|is)\b|"
    r"\bwhat\s+(have|did)\s+i\s+upload|"
    r"\bwhat\s+(do|can)\s+you\s+(know|teach|help)|"
    r"\bwhat\s+(all\s+)?(topics|subjects)\s+(do|can|are|have)\b|"
    r"\blist\s+(my\s+|the\s+|all\s+)?(files|documents|notes|topics|modules)\b", re.I)

# a reply that only acknowledges the last answer
_ACK = {
    "ok", "okay", "okk", "k", "kk", "cool", "nice", "great", "good", "got it", "gotcha",
    "understood", "alright", "fine", "sure", "yes", "yeah", "yep", "yup", "hmm", "hm",
    "oh", "ohh", "ah", "wow", "awesome", "perfect", "right", "i see", "makes sense",
    "that makes sense", "clear", "it's clear", "its clear", "got it thanks", "ok thanks",
    "okay thanks", "nice thanks", "no", "nope", "no thanks", "not really",
}
# a reply that asks to keep going
_CONTINUE = {
    "continue", "next", "go on", "more", "tell me more", "and", "and?", "then", "then?",
    "go ahead", "keep going", "carry on", "what next", "what's next", "whats next",
    "next one", "next topic", "proceed",
}
# words that point back at something said earlier
_REFERS_BACK = {
    "it", "its", "this", "that", "these", "those", "they", "them", "their", "he", "she",
    "one", "ones", "above", "previous", "same", "former", "latter", "first", "second",
    "third", "last", "each", "both", "former", "earlier", "here", "there",
}


# words that say what kind of help is wanted, never what it is about
_REQUEST = {
    "teach", "learn", "define", "definition", "detail", "detailed", "depth", "elaborate",
    "anything", "something", "stuff", "basics", "basic", "start", "begin", "beginning",
    "know", "tell", "talk", "discuss", "revise", "recap", "review", "read", "recite",
    "quote", "say", "says", "written", "write", "lesson", "lessons", "teaching",
    "properly", "fully", "completely", "thoroughly", "nicely", "clearly", "again",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[!?.,;:]+$", "", (text or "").strip().lower()))


def topic_words(text: str) -> set[str]:
    """Words that could name a topic (not request, scope or follow-up words)."""
    return {w for w in words(text) if w not in STOP and w not in FOLLOWUP and w not in SCOPE
            and w not in _REFERS_BACK and w not in _REQUEST and not w.isdigit()}


def _strip_pleasantries(text: str) -> str:
    t = text.strip()
    for rx in (_GREETING, _THANKS):
        t = rx.sub("", t, count=1).strip()
    return t


def classify(question: str, history: list[dict], mode: str = "quick") -> str:
    """'chat' | 'followup' | 'question'.

    chat     — small talk, questions about the app, "what material do I have", or a
               request too vague to search ("teach me") with nothing earlier to lean on
    followup — leans on the conversation ("teach me it", "each concept", "why?", "ok"
               in a lesson) and must be rewritten before searching
    question — a self-contained question; search with it as it is"""
    q = _norm(question)
    if not q:
        return "chat"
    if _BYE.match(q) or _ABOUT.search(q) or _MATERIAL_META.search(q):
        return "chat"
    rest = _norm(_strip_pleasantries(q))
    if not rest:                                   # only "hi" / "thanks"
        return "chat"
    if rest in _ACK:
        # in a lesson "yes"/"ok"/"no" answers the tutor's question: keep teaching
        return "followup" if (history and mode == "teach") else "chat"
    if rest in _CONTINUE:
        return "followup" if history else "chat"
    if wanted_locs(rest):                          # "what's on slide 4"
        return "question"
    if is_broad(rest) and not (history and set(words(rest)) & _REFERS_BACK):
        return "question"                          # "explain all the concepts in my notes"
    if not history:
        return "question" if topic_words(rest) else "chat"
    return "followup" if needs_context(rest) else "question"


def needs_context(question: str) -> bool:
    """True when the message can't be understood without the conversation."""
    ws = words(question)
    if not ws:
        return True
    if set(ws) & _REFERS_BACK:
        return True
    tw = topic_words(question)
    if not tw:                                     # "explain in detail", "why?", "teach me"
        return True
    if len(ws) <= 3 and re.match(r"^(what|how)\s+about\b", _norm(question)):
        return True                                # "what about wireless?"
    return False


# ---------------------------------------------------------------- rewriting
REWRITE_PROMPT = (
    "You are a query rewriter for a study app. You read a conversation between a student "
    "and their study assistant, then rewrite the student's LATEST message as ONE "
    "standalone question that can be understood without the conversation.\n"
    "Rules:\n"
    "- Replace words like it / this / that / they / each concept / the second one with the "
    "actual topic or document they refer to, taken from the conversation.\n"
    "- Keep what the student wants: explain, teach, define, example, compare, list, "
    "summarise, in detail, simpler, and so on.\n"
    "- If the student says ok / yes / next / continue during a lesson, rewrite it as a "
    "request to continue teaching that topic with the next idea. If they say no / didn't "
    "get it, rewrite it as a request to explain the same idea more simply.\n"
    "- If the latest message is already a complete question, return it unchanged.\n"
    "- Never answer the question. Never add facts. Never add topics the conversation "
    "didn't mention.\n"
    "- If the latest message is only small talk (hi, thanks, bye) reply with exactly: CHAT\n"
    "Output ONLY the rewritten question on one line."
)

_TAG = re.compile(r"\[[^\[\]\n]*·[^\[\]\n]*\]")


def _plain(text: str, limit: int) -> str:
    t = _TAG.sub("", text or "")
    t = re.sub(r"[*_#>`]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + " …"


def rewrite_messages(question: str, history: list[dict]) -> list[dict]:
    turns = history[-6:]
    lines = []
    for m in turns:
        who = "Student" if m["role"] == "user" else "Assistant"
        lines.append(f"{who}: {_plain(m['content'], 500 if m['role'] == 'assistant' else 300)}")
    convo = "\n".join(lines) or "(no earlier messages)"
    return [{"role": "system", "content": REWRITE_PROMPT},
            {"role": "user", "content":
                f"Conversation so far:\n{convo}\n\nLatest message: {question}\n\n"
                "Standalone question:"}]


_LEADS = re.compile(r"^(standalone\s+question|rewritten(\s+question)?|question|query|"
                    r"output)\s*[:\-]\s*", re.I)
_PREAMBLE = re.compile(r"^(here('?s| is)|sure|okay|ok|certainly|the (rewritten|standalone)|"
                       r"rewritten|standalone)\b", re.I)
_PREAMBLE_INLINE = re.compile(r"^(here('?s| is)|the)\s+(the\s+)?(rewritten|standalone)"
                              r"(\s+question)?\s*(is)?\s*:\s*", re.I)
_SAFE_EXTRA = {
    "explain", "teach", "define", "definition", "example", "examples", "difference",
    "differences", "compare", "comparison", "list", "describe", "continue", "next",
    "idea", "more", "detail", "detailed", "about", "steps", "simple", "simpler",
    "simply", "terms", "concept", "concepts", "topic", "topics", "each", "every", "all",
    "covered", "cover", "notes", "material", "module", "document", "slide", "page",
    "work", "works", "used", "use", "types", "type", "advantages", "disadvantages",
    "between", "summarise", "summarize", "summary", "overview", "again", "different",
    "way", "lesson", "teaching", "explaining", "following", "part",
}


def clean_rewrite(raw: str, question: str, history: list[dict]) -> str | None:
    """The rewriter's line, or None if it is unusable (empty, an answer instead of a
    question, or it invented topics the conversation never mentioned)."""
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    # skip a chatty preamble line: "Here is the rewritten question:" / "Sure!"
    while lines and _PREAMBLE.match(lines[0]) and (lines[0].endswith(":") or len(lines[0]) < 25):
        lines = lines[1:]
    if not lines:
        return None
    line = lines[0]
    m = _PREAMBLE_INLINE.match(line)              # "Here is the standalone question: ..."
    if m:
        line = line[m.end():]
    line = _LEADS.sub("", line).strip().strip('"“”\'`* ').strip()
    if re.fullmatch(r"chat\.?", line, re.I):
        return CHAT
    if not line or len(line) > 300:
        return None                                # an answer, not a question
    seen = set(words(question))
    for m in history[-6:]:
        seen |= set(words(m["content"]))
    invented = {w for w in words(line)
                if w not in seen and w not in STOP and w not in FOLLOWUP
                and w not in SCOPE and w not in _SAFE_EXTRA and not w.isdigit()}
    if len(invented) > 2:
        return None
    return line


def window_query(question: str, history: list[dict]) -> str:
    """Fallback when the rewriter can't be used: the last topic-bearing user message,
    plus the start of the last answer, plus the new message."""
    prior = [m["content"] for m in history if m["role"] == "user" and topic_words(m["content"])]
    last_answer = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")
    parts = prior[-1:] + ([_plain(last_answer, 200)] if last_answer else []) + [question]
    return " ".join(parts)


# ---------------------------------------------------------------- material outline
_outline_cache: dict = {}
_outline_lock = threading.Lock()
HELPER_SOURCE = "Helper answers"


def _title(text: str) -> str:
    for line in (text or "").splitlines():
        t = re.sub(r"^[#*\-•\d.)\s]+", "", line).strip()
        if len(t) >= 3:
            return t if len(t) <= 80 else t[:80].rsplit(" ", 1)[0] + "…"
    return ""


def outline(user: str | None) -> dict[str, list[tuple[str, str]]]:
    """{document label: [(locator, title), ...]} in document order. Cached until the
    student's material changes."""
    n = count(user)
    with _outline_lock:
        hit = _outline_cache.get(user)
        if hit and hit[0] == n:
            return hit[1]
    out: dict[str, list[tuple[str, str]]] = {}
    seen = set()
    for c in user_chunks(user):
        key = (c["source"], c["loc"])
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(c["source"], []).append((c["loc"], _title(c["text"])))
    with _outline_lock:
        _outline_cache[user] = (n, out)
    return out


def outline_text(user: str | None, max_chars: int = 2500, per_doc: int = 30) -> str:
    o = outline(user)
    if not o:
        return "(no material uploaded yet)"
    blocks = []
    for src in sorted(o, key=lambda s: (s == HELPER_SOURCE, s.lower())):
        items = [f"{loc}: {t}" for loc, t in o[src] if t][:per_doc]
        blocks.append(f"- {src}" + (": " + "; ".join(items) if items else ""))
    text = "\n".join(blocks)
    return text if len(text) <= max_chars else text[:max_chars].rsplit(";", 1)[0] + " …"


def title_of(user: str | None, source: str, loc: str) -> str:
    for l, t in outline(user).get(source, []):
        if l == loc:
            return t
    return ""


def suggestions(user: str | None, hits: list[dict], limit: int = 3) -> list[dict]:
    """The closest things the notes DO cover, for a refusal: [{label, ask}]."""
    out, seen = [], set()
    for h in sorted(hits, key=lambda x: -(x.get("score") or 0)):
        if h.get("source") == HELPER_SOURCE:
            continue
        t = title_of(user, h["source"], h["loc"]) or h["source"]
        key = t.lower()
        if key in seen or len(t) < 3:
            continue
        seen.add(key)
        out.append({"label": t, "source": h["source"], "loc": h["loc"],
                    "ask": f"Explain {t}"})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- slide / page recall
_LOC = re.compile(r"\b(slides?|pages?|pg|p)\s*(?:no\.?|number|#)?\s*(\d{1,4})\b", re.I)


def wanted_locs(text: str) -> list[str]:
    out = []
    for kind, n in _LOC.findall(text or ""):
        out.append(f"slide {int(n)}" if kind.lower().startswith("slide") else f"p{int(n)}")
    return out


def locate(question: str, user: str | None, named_docs: list[str],
           history: list[dict]) -> tuple[list[dict], list[str]]:
    """Chunks at the slide/page the message names.
    Returns (chunks, ambiguous_documents): ambiguous when several documents have that
    slide and neither the message nor the conversation says which one."""
    locs = wanted_locs(question)
    if not locs:
        return [], []
    o = outline(user)
    have = {loc: [s for s, items in o.items() if any(l == loc for l, _ in items)] for loc in locs}
    chunks, ambiguous = [], []
    recent = " ".join(m["content"] for m in history[-4:]).lower()
    for loc, sources in have.items():
        if not sources:
            continue
        pick = [s for s in sources if s in named_docs] if named_docs else []
        if not pick and len(sources) == 1:
            pick = sources
        if not pick:
            mentioned = [s for s in sources if s.lower() in recent]
            if len(mentioned) == 1:
                pick = mentioned
        if not pick:
            ambiguous = sorted(sources)
            continue
        for s in pick:
            chunks += chunks_at(user, s, loc)
    return chunks, ambiguous


# ---------------------------------------------------------------- small-talk replies
CHAT_PROMPT = (
    "You are Vidhyāsathi, a friendly study companion inside a study app. Talk like a "
    "warm, sharp classmate: natural, short (one to three sentences), no lists unless the "
    "student asks what material they have.\n"
    "What you can do: answer questions from the student's own uploaded notes with "
    "citations (Quick Answer), teach a topic step by step (Teach Me), make practice "
    "questions and give feedback on their answers (Exam & Practice), search the web when "
    "they switch on Web fallback, and pass a doubt to a teacher or senior (Ask a helper).\n"
    "Rules:\n"
    "- This message is small talk, a question about you or the app, a question about what "
    "material they have, or a request too vague to act on. Reply to it naturally.\n"
    "- If they greet you, greet back and invite a question, naming one or two topics from "
    "their material.\n"
    "- If they ask what material or topics they have, list the documents and their main "
    "topics from the material list below.\n"
    "- If they ask you to teach or explain something without saying what, ask which topic "
    "and suggest two or three from their material.\n"
    "- Never explain subject matter here and never state facts about a topic.\n"
    "- If no material is listed, tell them to upload their notes in My material first."
)


def chat_messages(question: str, history: list[dict], user: str | None,
                  hint: str = "") -> list[dict]:
    convo = "\n".join(
        f"{'Student' if m['role'] == 'user' else 'You'}: {_plain(m['content'], 300)}"
        for m in history[-4:])
    content = (f"The student's material:\n{outline_text(user)}\n\n"
               + (f"Earlier conversation:\n{convo}\n\n" if convo else "")
               + (f"Note: {hint}\n\n" if hint else "")
               + f"Student: {question}")
    return [{"role": "system", "content": CHAT_PROMPT}, {"role": "user", "content": content}]
