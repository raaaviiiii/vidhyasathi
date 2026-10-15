"""Evaluate Vidhyāsathi on your REAL notes, without touching anyone's account.

    python scripts/eval_real.py [--n 20] [--negatives 10] [--keep-set]

Copies every uploaded document (data/uploads/*/ and data/docs/) into a temporary
evaluation bucket, drafts a test set from it, runs the evaluation, writes
eval/report-*.md, and deletes the temporary bucket again. Needs Qdrant + Ollama running.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import eval as ev                     # noqa: E402
from src.build_index import ingest_file       # noqa: E402
from src.ingest import SUPPORTED              # noqa: E402
from src.store import delete_bucket           # noqa: E402

BUCKET = "eval-bot@vidhyasathi.local"


def documents():
    seen, out = set(), []
    for folder in sorted((ROOT / "data" / "uploads").glob("*")) + [ROOT / "data" / "docs"]:
        if not folder.is_dir():
            continue
        for p in sorted(folder.iterdir()):
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in SUPPORTED:
                key = (p.name, p.stat().st_size)
                if key not in seen:               # same file uploaded by two accounts
                    seen.add(key)
                    out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--negatives", type=int, default=10)
    ap.add_argument("--keep-set", action="store_true", help="also save the drafted test set")
    a = ap.parse_args()

    docs = documents()
    if not docs:
        print("No uploaded notes found in data/uploads/ or data/docs/ — upload notes in the app first.")
        return 1
    print(f"Indexing {len(docs)} document(s) into a temporary evaluation bucket…")
    delete_bucket(BUCKET)
    try:
        for p in docs:
            r = ingest_file(p, user=BUCKET)
            print(f"  {p.name}: " + (f"{r['chunks']} chunks" if r.get("ok") else f"skipped ({r.get('error')})"))
        t0 = time.time()
        print(f"\nDrafting {a.n} questions from your notes (+{a.negatives} off-topic ones)…")
        items = ev.make_set(BUCKET, n=a.n, negatives=a.negatives)
        ev.EVAL_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M")
        if a.keep_set:
            (ev.EVAL_DIR / f"set-{stamp}.jsonl").write_text(
                "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))
        print(f"Running {len(items)} questions…")
        result = ev.run_set(items, BUCKET, judge=True)
        result["user"] = "your uploaded notes (temporary evaluation copy)"
        md = ev.report_md(result)
        (ev.EVAL_DIR / f"report-{stamp}.md").write_text(md)
        (ev.EVAL_DIR / f"report-{stamp}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        print("\n" + md)
        print(f"Saved eval/report-{stamp}.md ({(time.time() - t0) / 60:.1f} min)")
        return 0
    finally:
        delete_bucket(BUCKET)


if __name__ == "__main__":
    sys.exit(main())
