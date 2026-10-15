"""Live end-to-end smoke test against the REAL stack (Ollama + Qdrant + the app).

Run on the Mac while the app is running:
    python scripts/smoke_live.py                      # against http://localhost:8000
    python scripts/smoke_live.py https://xyz.trycloudflare.com   # through the tunnel

It signs up two throwaway accounts, uploads a small note, asks real questions, and
deletes the accounts at the end. Hard failures (FAIL) are bugs; model-quality
misses (WARN) are worth a look but can be the 8B model being the 8B model.
Standard library only — no extra installs.
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
RESULTS = []

NOTES = """Number Theory — Module 1

The Division Algorithm states that for integers a and b with b > 0 there exist unique
integers q and r such that a = bq + r and 0 <= r < b. Here q is the quotient and r is
the remainder.

The greatest common divisor gcd(a, b) is the largest integer that divides both a and b.
The Euclidean algorithm computes gcd(a, b) by repeated division: gcd(a, b) = gcd(b, r).

A prime number is an integer greater than 1 whose only positive divisors are 1 and
itself. The Fundamental Theorem of Arithmetic says every integer greater than 1 can be
written uniquely as a product of primes.
"""


def call(method, path, body=None, token=None, raw=None, ctype="application/json", timeout=300):
    headers = {"Content-Type": ctype} if (body is not None or raw is not None) else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, text = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode()
    except (urllib.error.URLError, OSError) as e:          # nothing listening / tunnel down
        return 0, {"error": str(getattr(e, "reason", e))}, time.time() - t0
    dt = time.time() - t0
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {"_raw": text[:200]}
    return status, payload, dt


def record(name, ok, detail="", dt=None, level="FAIL"):
    tag = "PASS" if ok else level
    RESULTS.append((tag, name, detail, dt))
    t = f"{dt:6.1f}s" if dt is not None else "       "
    print(f"[{tag}] {t}  {name}" + (f" — {detail}" if detail else ""), flush=True)


def multipart(field, filename, content: bytes):
    b = "----vs" + uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: text/plain\r\n\r\n").encode() + content + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def main():
    print(f"Vidhyāsathi live smoke test against {BASE}\n")
    st, h, dt = call("GET", "/api/health")
    if st == 0:
        record("app reachable", False, f"nothing answering at {BASE} — start the app (./start.sh) first", dt)
        return finish()
    record("health endpoint", st == 200, json.dumps(h), dt)
    record("Qdrant reachable", bool(h.get("qdrant")), "start it: docker start qdrant", None)
    record("Ollama + llama3.1:8b ready", bool(h.get("llm")),
           "run: ollama serve  and  ollama pull llama3.1:8b", None)
    if not (h.get("qdrant") and h.get("llm")):
        return finish()

    tag = uuid.uuid4().hex[:8]
    ea, eb, pw = f"smoke-a-{tag}@example.com", f"smoke-b-{tag}@example.com", "smoke-pass-1"
    st, a, dt = call("POST", "/api/signup", {"name": "Smoke A", "email": ea, "password": pw})
    record("sign up (A)", st == 200 and a.get("token"), str(a.get("error", "")), dt)
    st, b, _ = call("POST", "/api/signup", {"name": "Smoke B", "email": eb, "password": pw})
    ta, tb = a.get("token"), b.get("token")
    if not (ta and tb):
        return finish()
    try:
        run_checks(ta, tb)
    finally:
        for t in (ta, tb):
            call("POST", "/api/account/delete", {"password": pw}, token=t)
        print("\n(cleaned up both test accounts)")
    finish()


def run_checks(ta, tb):
    raw, ctype = multipart("file", "smoke-number-theory.txt", NOTES.encode())
    st, up, dt = call("POST", "/api/upload", raw=raw, ctype=ctype, token=ta)
    record("upload + index a note", st == 200 and up.get("indexed"),
           f"{up.get('chunks')} chunks" if up.get("indexed") else str(up), dt)

    st, m, dt = call("GET", "/api/material", token=ta)
    names = {d["name"]: d["indexed"] for d in m.get("docs", [])}
    record("material list shows it as indexed", names.get("smoke-number-theory.txt") is True, str(names), dt)

    st, r, dt = call("POST", "/api/answer", {"question": "State the Division Algorithm."}, token=ta)
    record("in-material question answered", r.get("decision") in ("answer", "answer_uncertain"),
           f"decision={r.get('decision')} score={r.get('top_score')}", dt)
    record("answer cites the note", "smoke-number-theory" in (r.get("answer") or "").lower()
           or "smoke number theory" in (r.get("answer") or "").lower(),
           (r.get("answer") or "")[:120].replace("\n", " "), None, level="WARN")

    st, r, dt = call("POST", "/api/answer", {"question": "Explain how photosynthesis works in plants."}, token=ta)
    record("off-material question refused honestly", r.get("decision") == "refuse",
           f"decision={r.get('decision')}", dt)

    st, r, dt = call("POST", "/api/answer", {"question": "Explain all the concepts in my notes"}, token=ta)
    record("broad 'all concepts' ask gets an overview", r.get("decision") == "answer",
           f"decision={r.get('decision')}", dt)

    st, r, dt = call("POST", "/api/answer", {"question": "Summarise quantum mechanics"}, token=ta)
    record("broad ask about an absent topic refuses", r.get("decision") == "refuse",
           f"decision={r.get('decision')}", dt)

    hist = []
    st, r1, dt1 = call("POST", "/api/answer", {"question": "Teach me the Euclidean algorithm",
                                               "mode": "teach", "history": hist}, token=ta)
    hist = [{"role": "user", "content": "Teach me the Euclidean algorithm"},
            {"role": "assistant", "content": r1.get("answer", "")}]
    st, r2, dt2 = call("POST", "/api/answer", {"question": "why does that work?", "mode": "teach",
                                               "history": hist}, token=ta)
    record("teach mode: lesson + follow-up stay grounded",
           r1.get("decision") != "refuse" and r2.get("decision") != "refuse",
           f"{r1.get('decision')} → {r2.get('decision')}", dt1 + dt2, level="WARN")

    st, ex, dt = call("POST", "/api/exam", {"topic": "the Division Algorithm", "n": 3}, token=ta)
    items = ex.get("items", [])
    record("practice set generated with cited answers", ex.get("grounded") and len(items) >= 1,
           f"{len(items)} items", dt, level="WARN" if ex.get("grounded") else "FAIL")

    st, g, dt = call("POST", "/api/grade", {"question": "State the Division Algorithm.",
                                            "student_answer": "a = bq + r where 0 <= r < b"}, token=ta)
    fb = g.get("feedback", "")
    record("grading gives feedback under the 4 headings",
           g.get("grounded") and all(h in fb for h in ("Correct", "Missing", "Incorrect", "Suggestion")),
           fb[:100].replace("\n", " "), dt, level="WARN")

    st, r, dt = call("POST", "/api/answer", {"question": "State the Division Algorithm."}, token=tb)
    record("another student can't see A's notes", r.get("decision") == "refuse",
           f"decision={r.get('decision')}", dt)
    st, m, _ = call("GET", "/api/material", token=tb)
    record("another student's material list is empty", m.get("docs") == [], str(m.get("docs")), None)

    st, r, dt = call("POST", "/api/material/delete", {"name": "smoke-number-theory.txt"}, token=ta)
    record("remove material", st == 200, str(r), dt)
    st, r, dt = call("POST", "/api/answer", {"question": "State the Division Algorithm."}, token=ta)
    record("removed material no longer answers", r.get("decision") == "refuse",
           f"decision={r.get('decision')}", dt)


def finish():
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    print(f"\n{len(RESULTS) - len(fails) - len(warns)} passed, {len(warns)} warnings, {len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
