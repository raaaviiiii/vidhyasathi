"""Phase 2 — the evaluation harness (src/eval.py)."""
import json
import re

import pytest

from src import config, eval as ev
from tests.samples import NUMBER_THEORY

A = "a@x.com"
GRAPHS = ["Kruskal algorithm builds a minimum spanning tree by adding the cheapest edge "
          "that does not form a cycle, repeating until all vertices are connected.",
          "Dijkstra algorithm finds the shortest paths from a source vertex in a weighted graph "
          "with non negative edge weights using a priority queue."]


def test_reciprocal_rank_and_hits():
    assert ev.reciprocal_rank(["a", "b", "c"], "a") == 1.0
    assert ev.reciprocal_rank(["a", "b", "c"], "c") == pytest.approx(1 / 3)
    assert ev.reciprocal_rank(["a"] * 20 + ["x"], "x") == 0.0       # beyond @10
    assert ev.hit_at(["a", "b", "c"], "c", 5) == 1.0 and ev.hit_at(["a", "b"], "z", 5) == 0.0


def test_bm25_ranks_the_matching_document_first():
    docs = ["the cat sat on the mat", "kruskal spanning tree edges", "division algorithm remainder"]
    s = ev.BM25(docs).scores("spanning tree with kruskal")
    assert max(range(3), key=lambda i: s[i]) == 1
    assert ev.BM25([]).scores("x") == []


def _qa_writer(kind, messages):
    """Fake LLM for the harness: writes a question from the passage's own words,
    judges by overlap, and runs the default behaviour for everything else."""
    system, user = messages[0]["content"], messages[-1]["content"]
    if "evaluation questions" in system:
        words = re.findall(r"[A-Za-z]{4,}", user.split("]:", 1)[-1])[:6]
        return f"QUESTION: {' '.join(words)}?\nANSWER: {' '.join(words[:3])}"
    if "grading a study assistant" in system:
        return "YES"
    return None


@pytest.fixture
def material(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    index_text(A, "Graphs", GRAPHS)
    fakes.llm.respond = _qa_writer
    return fakes


def test_make_set_covers_every_document_and_adds_negatives(material):
    items = ev.make_set(A, n=4, negatives=5)
    pos = [i for i in items if i["in_material"]]
    neg = [i for i in items if not i["in_material"]]
    assert len(pos) == 4 and len(neg) == 5
    assert {i["expected_source"] for i in pos} == {"Number Theory", "Graphs"}   # round-robin
    assert all(i["expected_chunk"] and i["question"].endswith("?") for i in pos)
    assert all(q["question"] in ev.OFF_MATERIAL for q in neg)


def test_make_set_without_material_says_so(fakes):
    with pytest.raises(SystemExit, match="No indexed material"):
        ev.make_set("nobody@x.com", n=3)


def test_helper_answers_are_not_used_as_eval_passages(material, monkeypatch):
    from src.doubts import index_helper_answers
    index_helper_answers(A, [{"id": 1, "question": "Why?", "answer": "Because " * 30, "helper_name": "T"}])
    assert all(c["doc"] != "__helper_answers__" for c in ev.user_chunks(A))


def test_run_set_end_to_end_metrics_and_report(material):
    items = ev.make_set(A, n=4, negatives=4)
    result = ev.run_set(items, A, judge=True, progress=lambda *_: None)
    m = result["metrics"]
    assert m["n_in_material"] == 4 and m["n_off_material"] == 4
    for k in ("mrr_pipeline", "mrr_bm25_baseline", "mrr_vector_only", "recall5_pipeline",
              "answer_accuracy", "honest_refusal_rate", "latency_p50"):
        assert m[k] is not None, k
    assert 0 <= m["mrr_pipeline"] <= 1
    assert m["honest_refusal_rate"] == 1.0          # the fake material has no history/cooking/etc.
    assert result["sweep"]["recommended"]["refused_off_material"] >= 0.95
    md = ev.report_md(result)
    assert "Against the review targets" in md and "BM25" in md and "Recommended" in md
    json.dumps(result)                               # report is serialisable


def test_sweep_keeps_honesty_first():
    rows = ([{"in_material": True, "top_score": s, "check": True} for s in (0.35, 0.5, 0.7)] +
            [{"in_material": False, "top_score": s, "check": False} for s in (0.25, 0.4)] +
            [{"in_material": False, "top_score": 0.47, "check": False}])
    rec = ev.sweep(rows)["recommended"]
    # a STRONG cut at or below 0.47 would wave through an off-material question unchecked
    assert rec["strong"] > 0.47 and rec["refused_off_material"] == 1.0
    assert rec["answered_in_material"] == 1.0


def test_cli_make_and_run_write_files(material, tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "EVAL_DIR", tmp_path)
    ev.main(["make", "--user", A, "--n", "2", "--negatives", "2"])
    sets = list(tmp_path.glob("*.jsonl"))
    assert len(sets) == 1 and len(sets[0].read_text().splitlines()) == 4
    ev.main(["run", "--user", A, "--no-judge"])
    assert list(tmp_path.glob("report-*.md")) and list(tmp_path.glob("report-*.json"))


def test_eval_real_script_uses_a_temporary_bucket_and_cleans_up(fakes, tmp_path, monkeypatch):
    """scripts/eval_real.py: indexes uploads into a throwaway bucket, writes a report,
    and leaves no chunks behind (the student's own data is never touched)."""
    import importlib.util, sys
    from src.store import count
    from tests.samples import NUMBER_THEORY
    spec = importlib.util.spec_from_file_location("eval_real", "scripts/eval_real.py")
    er = importlib.util.module_from_spec(spec); spec.loader.exec_module(er)
    up = tmp_path / "data" / "uploads" / "abc"; up.mkdir(parents=True)
    (up / "nt.txt").write_text("\n\n".join(NUMBER_THEORY * 2))
    (up / "graphs.md").write_text("\n\n".join(GRAPHS * 2))
    monkeypatch.setattr(er, "ROOT", tmp_path)
    monkeypatch.setattr(ev, "EVAL_DIR", tmp_path / "eval")
    fakes.llm.respond = _qa_writer
    monkeypatch.setattr(sys, "argv", ["eval_real.py", "--n", "3", "--negatives", "3"])
    assert er.main() == 0
    assert list((tmp_path / "eval").glob("report-*.md"))
    assert count(er.BUCKET) == 0, "temporary evaluation bucket was left behind"
