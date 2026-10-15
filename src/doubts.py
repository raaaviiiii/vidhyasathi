"""Ask for Help — the human half of Vidhyāsathi.

When the assistant is unsure (tentative answer) or the material doesn't cover a
question (honest refusal), the student can send the doubt to a human helper: a
teacher or senior who has said which subjects they can help with.

  route()                   — pick the best-matched helper for a doubt
  index_helper_answers()    — feed every answered doubt back into THAT student's
                              knowledge base, so the same question is answered from
                              their material next time (escalations fall over time)
"""
import re

from src.answer import _words, _STOP, _decode_tag_entities
from src.build_index import index_records
from src import embed

HELPER_DOC = "__helper_answers__"       # one document per student, rebuilt on each answer
HELPER_SOURCE = "Helper answers"        # how these passages are cited: [Helper answers · doubt 12]
MATCH_FLOOR = 0.35                      # cosine at which a helper's subjects count as a match
_CITE = re.compile(r"\[([^\[\]\n]*\u00b7[^\[\]\n]*)\]")


def _defang(text: str) -> str:
    """A helper's text must not plant citation tags that later read as if they came
    from the student's own notes: [Number Theory · p1] -> (Number Theory · p1)."""
    return _CITE.sub(r"(\1)", _decode_tag_entities(text or ""))


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def route(question: str, sources: list[str], helpers: list[dict]) -> dict | None:
    """Best helper for a doubt.

    helpers: [{id, name, expertise, open_count}]. Score = semantic similarity between
    the doubt (question + the material it touched) and the helper's subjects, plus a
    bonus for shared subject words, minus a small penalty per open doubt so work is
    spread out. Helpers with no subjects listed are only used if nobody else exists.
    Returns the chosen helper dict, or None if there are no helpers at all."""
    if not helpers:
        return None
    text = " ".join([question or ""] + [s for s in (sources or []) if s])
    tagged = [h for h in helpers if (h.get("expertise") or "").strip()]
    if not tagged:
        return min(helpers, key=lambda h: (h.get("open_count", 0), h["id"]))
    vecs = embed.embed_texts([text] + [h["expertise"] for h in tagged])
    qv, hvs = vecs[0], vecs[1:]
    qwords = set(_words(text)) - _STOP
    scored = []
    for h, hv in zip(tagged, hvs):
        overlap = len(qwords & (set(_words(h["expertise"])) - _STOP))
        scored.append((h, _cos(qv, hv), overlap))
    # subject match first: load only spreads work among helpers who know the subject
    matching = [x for x in scored if x[2] > 0 or x[1] >= MATCH_FLOOR] or scored
    best, best_key = None, None
    for h, sim, overlap in matching:
        score = sim + 0.15 * min(overlap, 3) - 0.05 * min(h.get("open_count", 0), 4)
        key = (round(score, 6), -h.get("open_count", 0), -h["id"])
        if best_key is None or key > best_key:
            best, best_key = h, key
    return best


def index_helper_answers(student_email: str, answered: list[dict]) -> int:
    """Rebuild the student's 'Helper answers' document from all their answered doubts.

    answered: [{id, question, answer, helper_name}]. Returns the number of chunks."""
    records = []
    for d in sorted(answered, key=lambda x: x["id"]):
        q, a = _defang(d.get("question")).strip(), _defang(d.get("answer")).strip()
        if not (q and a):
            continue
        who = d.get("helper_name") or "a helper"
        records.append({"loc": f"doubt {d['id']}", "order": d["id"],
                        "text": f"Question: {q}\nAnswer from {who}: {a}"})
    return index_records(records, user=student_email, doc=HELPER_DOC, source=HELPER_SOURCE)
