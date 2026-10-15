"""Evaluation harness — measures the claims in the review deck instead of eyeballing.

    python -m src.eval make --user you@x.com            # draft a test set from YOUR notes
    python -m src.eval run  --user you@x.com            # run it, write private/eval/report-*.md/.json

`make` picks passages from the student's indexed material and has the model write one
question per passage (so the passage that answers it is known), then adds off-material
questions that must be refused. Open the .jsonl and fix or delete weak questions before
relying on the numbers — a hand-checked set is what makes the results defensible.

`run` reports:
  - retrieval quality: MRR@10 and Recall@5 for the real pipeline (vector + re-rank), for
    vector-only, and for a keyword (BM25) baseline        -> deck target: MRR >= 0.80
  - answer accuracy on in-material questions (LLM-judged against the source passage)
                                                           -> deck target: >= 90%
  - honest refusals on off-material questions, false refusals, citation accuracy, latency
  - a threshold sweep: which HELP/STRONG score cut-offs give the best balance while keeping
    refusals honest, with how many groundedness checks each setting saves
"""
import argparse
import json
import math
import random
import re
import statistics
import time
from collections import Counter
from pathlib import Path

from src import config
from src.answer import (GROUND_CHECK, _words, _STOP, _retrieve, answer, ask_llm,
                        build_context, call_llm)
from src.config import COLLECTION_NAME, EVAL_DIR, RETRIEVE_CANDIDATES
from src.store import get_client, user_filter

K = 10

OFF_MATERIAL = [
    "Who painted the Mona Lisa?",
    "What is the capital of Australia?",
    "Explain how photosynthesis works in plants.",
    "What causes the seasons on Earth?",
    "Who won the 2011 Cricket World Cup?",
    "How do you make a sourdough starter?",
    "What is the boiling point of water at sea level in Fahrenheit?",
    "Summarise the plot of Romeo and Juliet.",
    "What is the function of mitochondria in a cell?",
    "Explain the causes of the First World War.",
    "How does a refrigerator keep food cold?",
    "What is the tallest mountain in Africa?",
    "Explain supply and demand in economics.",
    "What language is spoken in Brazil?",
    "How many players are on a football (soccer) team?",
    "What is the chemical formula of table salt?",
    "Explain the water cycle.",
    "Who wrote the Indian national anthem?",
    "What is a black hole?",
    "How do vaccines train the immune system?",
    "What is the speed of light in a vacuum?",
    "Who was the first person to walk on the Moon?",
    "How do bees make honey?",
    "What is the currency of Japan?",
    "Explain how a rainbow forms.",
    "What does a cardiologist specialise in?",
    "How is glass made?",
    "Name the planets of the solar system in order.",
    "What is the main ingredient of guacamole?",
    "Explain how tides are caused.",
]

GEN_PROMPT = (
    "You write evaluation questions for a study assistant. Given ONE passage from a "
    "student's notes, write ONE clear exam-style question that can be answered using ONLY "
    "that passage, and its short correct answer. Do not mention 'the passage'.\n"
    "Reply in exactly this format:\nQUESTION: <question>\nANSWER: <short answer>"
)

JUDGE_PROMPT = (
    "You are grading a study assistant. Given a SOURCE passage, a QUESTION and the "
    "assistant's ANSWER, decide whether the answer is correct according to the source and "
    "does not add claims that contradict it. Ignore citation tags and style. "
    "Reply with ONLY one word: YES or NO."
)


# ----------------------------------------------------------------------------- data
def user_chunks(user: str) -> list[dict]:
    """Every chunk in a student's bucket (helper answers excluded: they aren't notes)."""
    client = get_client()
    if not client.collection_exists(COLLECTION_NAME):
        return []
    out, offset = [], None
    while True:
        pts, offset = client.scroll(COLLECTION_NAME, scroll_filter=user_filter(user),
                                    with_payload=True, with_vectors=False, limit=512,
                                    offset=offset)
        for p in pts:
            pl = p.payload or {}
            if pl.get("doc") == "__helper_answers__":
                continue
            out.append({"chunk_id": _eid(pl), "source": pl.get("source", ""),
                        "loc": pl.get("loc", ""), "text": pl.get("text", ""), "doc": pl.get("doc")})
        if offset is None:
            return out


def _eid(payload_or_hit: dict) -> str:
    """Evaluation id of a passage: document + chunk, so two documents with the same
    stem (notes.txt / notes.md) can never be confused."""
    return f"{payload_or_hit.get('doc') or payload_or_hit.get('source', '')}::{payload_or_hit.get('chunk_id')}"


def _tokens(text: str) -> list[str]:
    return [w for w in _words(text) if w not in _STOP]


class BM25:
    """Okapi BM25 — the keyword baseline the deck compares retrieval against."""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(_tokens(d)) for d in docs]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if self.len else 0.0
        df = Counter(w for t in self.tf for w in t)
        n = len(docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = _tokens(query)
        out = []
        for tf, ln in zip(self.tf, self.len):
            s = 0.0
            for w in q:
                f = tf.get(w, 0)
                if f:
                    s += self.idf.get(w, 0) * f * (self.k1 + 1) / (
                        f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out


def reciprocal_rank(ranked_ids: list, target, k: int = K) -> float:
    for i, cid in enumerate(ranked_ids[:k], start=1):
        if cid == target:
            return 1.0 / i
    return 0.0


def hit_at(ranked_ids: list, target, k: int) -> float:
    return 1.0 if target in ranked_ids[:k] else 0.0


# ----------------------------------------------------------------------------- make
def _parse_qa(text: str):
    q = re.search(r"QUESTION\s*:\s*(.+)", text or "", re.I)
    a = re.search(r"ANSWER\s*:\s*(.+)", text or "", re.I | re.S)
    if not q:
        return None, None
    return q.group(1).strip(), (a.group(1).strip() if a else "")


def make_set(user: str, n: int = 30, negatives: int = 15, seed: int = 7,
             progress=print) -> list[dict]:
    every = user_chunks(user)
    if not every:
        raise SystemExit(f"No indexed material for {user!r}. Upload notes in the app first.")
    chunks = [c for c in every if len(c["text"]) >= 120]
    if not chunks:
        raise SystemExit(f"All {len(every)} passages for {user!r} are very short (under 120 "
                         "characters), so there's nothing to write questions from. Upload fuller notes.")
    rng = random.Random(seed)
    by_src: dict[str, list] = {}
    for c in chunks:
        by_src.setdefault(c["source"], []).append(c)
    for v in by_src.values():
        rng.shuffle(v)
    picked, pools = [], list(by_src.values())
    while len(picked) < n and any(pools):            # round-robin across documents
        for pool in pools:
            if pool and len(picked) < n:
                picked.append(pool.pop())
    items = []
    for i, c in enumerate(picked, start=1):
        q, ref = _parse_qa(ask_llm(GEN_PROMPT, f"Passage [{c['source']} · {c['loc']}]:\n{c['text']}"))
        if not q:
            continue
        items.append({"id": f"in{i}", "question": q, "in_material": True,
                      "expected_chunk": c["chunk_id"], "expected_source": c["source"],
                      "expected_loc": c["loc"], "reference": ref})
    # "off-material" must really be off THIS student's material: a biology student's
    # notes may well cover photosynthesis. Drop any that retrieve a confident match.
    pool, kept, dropped = rng.sample(OFF_MATERIAL, len(OFF_MATERIAL)), [], []
    for q in pool:
        if len(kept) >= negatives:
            break
        _, score, _ = _retrieve(q, user=user)
        (dropped if score >= config.HELP_SCORE else kept).append(q)
    if dropped:
        progress(f"Skipped {len(dropped)} off-material question(s) your notes may cover: "
                 + "; ".join(dropped))
    for j, q in enumerate(kept, start=1):
        items.append({"id": f"off{j}", "question": q, "in_material": False})
    return items


# ----------------------------------------------------------------------------- run
def _judge(question: str, answer_text: str, source_text: str) -> bool:
    out = ask_llm(JUDGE_PROMPT, f"SOURCE:\n{source_text}\n\nQUESTION: {question}\n\n"
                                f"ANSWER: {answer_text}\n\nIs the answer correct? YES or NO:")
    return out.strip().lower().startswith("y")


def _check(question: str, hits: list[dict]) -> bool:
    """The groundedness verdict for EVERY item (the live app only asks in the middle
    band), so the threshold sweep can replay any cut-off."""
    out = call_llm([{"role": "system", "content": GROUND_CHECK},
                    {"role": "user", "content": f"Context passages:\n{build_context(hits)}\n\n"
                                                f"Question: {question}\n\nAnswer YES or NO:"}])
    return out.strip().lower().startswith("y")


def run_set(items: list[dict], user: str, judge: bool = True, progress=print) -> dict:
    from src.retrieve import retrieve
    from src.rerank import rerank

    chunks = user_chunks(user)
    if not chunks:
        raise SystemExit(f"No indexed material for {user!r}.")
    by_id = {c["chunk_id"]: c for c in chunks}
    bm25 = BM25([c["text"] for c in chunks])
    rows = []
    for n, it in enumerate(items, start=1):
        q = it["question"]
        row = {"id": it.get("id"), "question": q, "in_material": bool(it.get("in_material"))}
        # ranking quality: the full candidate list, re-ranked, vs vector order vs BM25
        cands = retrieve(q, top_k=max(RETRIEVE_CANDIDATES, 20), user=user)
        for c in cands:
            c["eid"] = _eid(c)
        vec_ids = [c["eid"] for c in cands]
        ranked = rerank(q, [dict(c) for c in cands], top_k=len(cands))
        pipe_ids = [c["eid"] for c in ranked]
        # the gate: replay EXACTLY what the app sees (same retrieval path, same score)
        hits, top_score, _ = _retrieve(q, user=user)
        row["top_score"] = top_score
        row["check"] = _check(q, hits) if hits else False
        target = it.get("expected_chunk")
        if row["in_material"] and target and target not in by_id:
            row["stale"] = True                 # its passage was deleted or re-uploaded since
        elif row["in_material"] and target:
            bm = bm25.scores(q)
            bm_ids = [chunks[i]["chunk_id"] for i in sorted(range(len(chunks)), key=lambda i: -bm[i])]
            row.update(rr_pipeline=reciprocal_rank(pipe_ids, target),
                       rr_vector=reciprocal_rank(vec_ids, target),
                       rr_bm25=reciprocal_rank(bm_ids, target),
                       hit5_pipeline=hit_at(pipe_ids, target, 5),
                       hit5_vector=hit_at(vec_ids, target, 5),
                       hit5_bm25=hit_at(bm_ids, target, 5))
        t0 = time.time()
        res = answer(q, user=user)
        row["latency"] = round(time.time() - t0, 3)
        row["decision"] = res["decision"]
        row["answer"] = res["answer"]
        if row["in_material"]:
            row["answered"] = res["decision"] in ("answer", "answer_uncertain")
            exp = it.get("expected_source", "")
            row["cited_expected"] = bool(exp) and f"[{exp} ·" in (res["answer"] or "")
            src = by_id.get(it.get("expected_chunk"), {}).get("text", "")
            if row["answered"]:
                row["correct"] = _judge(q, res["answer"], src) if (judge and src) else row["cited_expected"]
            else:
                row["correct"] = False
        else:
            row["refused"] = res["decision"] == "refuse"
        rows.append(row)
        progress(f"  [{n}/{len(items)}] {row['decision']:<17} {q[:70]}")
    return {"user": user, "when": time.strftime("%Y-%m-%d %H:%M"), "judge": judge,
            "metrics": summarise(rows), "sweep": sweep(rows), "rows": rows}


def _mean(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 3) if xs else None


def summarise(rows: list[dict]) -> dict:
    stale = [r for r in rows if r.get("stale")]
    pos = [r for r in rows if r["in_material"] and not r.get("stale")]
    neg = [r for r in rows if not r["in_material"]]
    ranked = [r for r in pos if "rr_pipeline" in r]
    lat = sorted(r["latency"] for r in rows)
    m = {
        "n_in_material": len(pos), "n_off_material": len(neg),
        "mrr_pipeline": _mean(r["rr_pipeline"] for r in ranked),
        "mrr_vector_only": _mean(r["rr_vector"] for r in ranked),
        "mrr_bm25_baseline": _mean(r["rr_bm25"] for r in ranked),
        "recall5_pipeline": _mean(r["hit5_pipeline"] for r in ranked),
        "recall5_vector_only": _mean(r["hit5_vector"] for r in ranked),
        "stale_items": len(stale),
        "recall5_bm25_baseline": _mean(r["hit5_bm25"] for r in ranked),
        "answer_accuracy": _mean(r["correct"] for r in pos),
        "answered_rate": _mean(r["answered"] for r in pos),
        "false_refusal_rate": _mean(not r["answered"] for r in pos),
        "citation_accuracy": _mean(r["cited_expected"] for r in pos if r["answered"]),
        "honest_refusal_rate": _mean(r["refused"] for r in neg),
        "latency_p50": round(statistics.median(lat), 2) if lat else None,
        "latency_p90": round(lat[min(len(lat) - 1, int(0.9 * len(lat)))], 2) if lat else None,
    }
    m["target_accuracy_met"] = m["answer_accuracy"] is not None and m["answer_accuracy"] >= 0.90
    m["target_mrr_met"] = m["mrr_pipeline"] is not None and m["mrr_pipeline"] >= 0.80
    m["beats_bm25"] = (m["mrr_pipeline"] is not None and m["mrr_bm25_baseline"] is not None
                       and m["mrr_pipeline"] > m["mrr_bm25_baseline"])
    return m


def sweep(rows: list[dict]) -> dict:
    """Replay the gate at other cut-offs. Grounded = score >= strong, or score >= help
    and the groundedness check said yes. Honesty first: only settings that refuse at
    least 95% of off-material questions are eligible (if none do, only the most honest
    ones are); among those, maximise in-material answered rate, then checks skipped
    (speed), then closeness to the current setting."""
    pos = [r for r in rows if r["in_material"] and not r.get("stale")]
    neg = [r for r in rows if not r["in_material"]]
    if not neg:
        return {"recommended": None, "current": None, "grid": [], "honesty_target_met": None,
                "note": "Add off-material questions to tune the thresholds."}
    grid = []
    for help_cut in [x / 100 for x in range(20, 46, 5)]:
        for strong in [x / 100 for x in range(45, 81, 5)]:
            if strong < help_cut:
                continue

            def grounded(r):
                return r["top_score"] >= strong or (r["top_score"] >= help_cut and r["check"])
            grid.append({"help": help_cut, "strong": strong,
                         "answered_in_material": _mean(grounded(r) for r in pos),
                         "refused_off_material": _mean(not grounded(r) for r in neg),
                         "checks_skipped": _mean(r["top_score"] >= strong for r in rows)})
    met = [g for g in grid if g["refused_off_material"] >= 0.95]
    if met:
        pool = met
    else:
        most = max(g["refused_off_material"] for g in grid)
        pool = [g for g in grid if g["refused_off_material"] == most]
    best = max(pool, key=lambda g: (g["answered_in_material"] or 0, g["checks_skipped"] or 0,
                                    -abs(g["help"] - config.HELP_SCORE)
                                    - abs(g["strong"] - config.STRONG_SKIP_SCORE)))
    current = next((g for g in grid if abs(g["help"] - config.HELP_SCORE) < 1e-9
                    and abs(g["strong"] - config.STRONG_SKIP_SCORE) < 1e-9), None)
    return {"recommended": best, "current": current, "grid": grid,
            "honesty_target_met": bool(met)}


# ----------------------------------------------------------------------------- report
def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def _num(x):
    return "—" if x is None else f"{x:.2f}"


def _sec(x):
    return "—" if x is None else f"{x} s"


def report_md(result: dict) -> str:
    m, sw = result["metrics"], result["sweep"]
    rec, cur = sw["recommended"], sw["current"]
    ok = lambda b: "✅ met" if b else "❌ not met"
    lines = [
        f"# Vidhyāsathi evaluation — {result['when']}",
        "",
        f"Student bucket: `{result['user']}` · {m['n_in_material']} in-material questions · "
        f"{m['n_off_material']} off-material questions · judge: {'LLM' if result['judge'] else 'citation proxy'}",
        "",
        "## Against the review targets",
        "",
        "| Target | Result | |",
        "|---|---|---|",
        f"| Answer accuracy on grounded questions ≥ 90% | {_pct(m['answer_accuracy'])} | {ok(m['target_accuracy_met'])} |",
        f"| Retrieval MRR@10 ≥ 0.80 | {_num(m['mrr_pipeline'])} | {ok(m['target_mrr_met'])} |",
        f"| Beats the keyword (BM25) baseline | {_num(m['mrr_pipeline'])} vs {_num(m['mrr_bm25_baseline'])} | {ok(m['beats_bm25'])} |",
        "",
        "## Retrieval",
        "",
        "| Method | MRR@10 | Recall@5 |",
        "|---|---|---|",
        f"| Vector + re-rank (the app) | {_num(m['mrr_pipeline'])} | {_pct(m['recall5_pipeline'])} |",
        f"| Vector only | {_num(m['mrr_vector_only'])} | {_pct(m['recall5_vector_only'])} |",
        f"| BM25 keyword baseline | {_num(m['mrr_bm25_baseline'])} | {_pct(m['recall5_bm25_baseline'])} |",
        "",
        "## Honesty and answers",
        "",
        f"- Off-material questions refused honestly: **{_pct(m['honest_refusal_rate'])}**",
        f"- In-material questions answered: {_pct(m['answered_rate'])} (false refusals {_pct(m['false_refusal_rate'])})",
        f"- Answers citing the right document: {_pct(m['citation_accuracy'])}",
        f"- Latency: median {_sec(m['latency_p50'])}, 90th percentile {_sec(m['latency_p90'])}",
        "",
        "## Confidence-gate thresholds",
        "",
    ]
    if rec is None:
        lines.append(f"- {sw.get('note', 'Not enough data to tune thresholds.')}")
    else:
        lines += [
            f"- Current (HELP_SCORE={config.HELP_SCORE}, STRONG_SKIP_SCORE={config.STRONG_SKIP_SCORE}): "
            + (f"answers {_pct(cur['answered_in_material'])} of in-material, refuses "
               f"{_pct(cur['refused_off_material'])} of off-material, skips {_pct(cur['checks_skipped'])} of checks"
               if cur else "not on the sweep grid"),
            f"- Recommended: **HELP_SCORE={rec['help']}, STRONG_SKIP_SCORE={rec['strong']}** — answers "
            f"{_pct(rec['answered_in_material'])} of in-material, refuses {_pct(rec['refused_off_material'])} "
            f"of off-material, skips {_pct(rec['checks_skipped'])} of checks",
        ]
        if not sw.get("honesty_target_met"):
            lines.append("- ⚠️ No setting refuses 95% of off-material questions — this is the most "
                         "honest one available. Look at the misses below before changing anything.")
    if m.get("stale_items"):
        lines.append(f"- {m['stale_items']} question(s) point at passages no longer in your material "
                     "(deleted or re-uploaded) and were left out of retrieval scores — re-run `make`.")
    lines += [
        "",
        "## Misses worth reading",
        "",
    ]
    misses = [r for r in result["rows"] if (r["in_material"] and not r.get("correct"))
              or (not r["in_material"] and not r.get("refused"))]
    if not misses:
        lines.append("None.")
    for r in misses[:25]:
        kind = "in-material" if r["in_material"] else "off-material"
        lines.append(f"- *{kind}* — {r['question']} → `{r['decision']}` (score {r['top_score']:.2f})")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m src.eval", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make", help="draft a test set from a student's indexed notes")
    mk.add_argument("--user", required=True, help="the student's sign-in email")
    mk.add_argument("--n", type=int, default=30, help="in-material questions (default 30)")
    mk.add_argument("--negatives", type=int, default=15, help="off-material questions (default 15)")
    mk.add_argument("--out", help="output .jsonl (default private/eval/<user>.jsonl)")
    rn = sub.add_parser("run", help="run a test set and write a report")
    rn.add_argument("--user", required=True)
    rn.add_argument("--set", help="test set .jsonl (default private/eval/<user>.jsonl)")
    rn.add_argument("--no-judge", action="store_true", help="skip the LLM judge (faster)")
    a = ap.parse_args(argv)

    user = a.user.strip().lower()
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    default_set = EVAL_DIR / f"{re.sub(r'[^a-z0-9]+', '_', user)}.jsonl"
    if a.cmd == "make":
        items = make_set(user, a.n, a.negatives)
        out = Path(a.out) if a.out else default_set
        out.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))
        print(f"Wrote {len(items)} questions to {out}. Read through it and fix or delete weak "
              f"questions, then: python -m src.eval run --user {user}")
        return
    path = Path(a.set) if a.set else default_set
    if not path.exists():
        raise SystemExit(f"No test set at {path}. Create one first: python -m src.eval make --user {user}")
    items = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            it = json.loads(line)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}:{lineno}: not valid JSON ({e.msg}). Fix or delete that line.")
        if not isinstance(it, dict) or not str(it.get("question", "")).strip():
            raise SystemExit(f"{path}:{lineno}: every line needs a non-empty \"question\".")
        items.append(it)
    print(f"Running {len(items)} questions from {path} …")
    result = run_set(items, user, judge=not a.no_judge)
    stamp = time.strftime("%Y%m%d-%H%M")
    (EVAL_DIR / f"report-{stamp}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    md = report_md(result)
    (EVAL_DIR / f"report-{stamp}.md").write_text(md)
    print("\n" + md)
    print(f"Saved private/eval/report-{stamp}.md and .json")


if __name__ == "__main__":
    main()
