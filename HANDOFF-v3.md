# Vidhyāsathi — Handoff v3 (QA + Phase 2)

Picks up from Handoff v2. Three QA rounds by independent testers, every bug fixed,
and Phase 2 (Ask for Help + evaluation harness) built and tested.

## 1. Where things stand

- **Tests:** ~370 backend checks (pytest) + 179 UI checks (JSDOM), all passing.
  The live model is faked in tests; `scripts/smoke_live.py` checks the real stack on the Mac.
- **QA history:** round 1 found ~65 bugs, round 2 ~45, round 3 (phase 2) ~33.
  Reports are in `qa/reports/`; the intended behaviour the testers judged against is `qa/SPEC.md`.

## 2. What changed (the big things)

**Security / accounts**
- Real sessions: sign-in returns a token; every request sends `Authorization: Bearer …`.
  The server decides who you are — the old client-sent `user` field is ignored.
- Sessions are tied to the account row, and account ids are never reused (old `users.db`
  is migrated automatically on first start, keeping every account).
- Login lockout is per email + device, so nobody can lock a classmate out.
- `/api/users` and owner tools are owner-only: start the app with
  `VS_ADMIN_EMAILS="your@email"`.
- Uploads: safe file names (no path tricks, no invisible characters), size limit
  (`VS_MAX_UPLOAD_MB`, default 50) checked **before** the body is read.

**Storage / indexing**
- Qdrant runs as a Docker server (embedded mode lost writes under the web server).
- Each document is identified by its exact file name; re-upload writes the new version
  first and removes the old one only after that succeeds (never zero, never two copies).
- A scanned page OCR can't read is skipped with a warning; the rest still indexes.
- OCR models updated to `claude-haiku-5-5` → `claude-sonnet-5-5` → `claude-opus-5-5`.

**Answers**
- Broad asks ("explain all the concepts", "summarise module 2", "summarise my
  introduction pptx") get an overview of the right document(s); a broad ask about a topic
  that isn't in the notes ("summarise quantum mechanics") still refuses.
- Teach Me follow-ups ("why?", "simpler please") stay on topic; a new off-notes topic refuses.
- Malayalam/Hindi words are handled as whole words.
- Exam answers must cite one of the student's own sources; grading never shows marks.

**UI**
- Everything untrusted is sanitised (model output, web pages, file names, typed text).
- Recent chats are per account and stored as data; keyboard, screen-reader and phone
  layouts work; errors say what actually went wrong.

## 3. Phase 2

**Ask for Help** (the deck's headline feature)
- "Ask a helper" on tentative/refused answers → routed to the best subject match,
  spreading load. Helpers answer in **Helper inbox**; students see **My doubts**.
- Every helper answer is indexed into that student's own material (cited as
  "Helper answers · doubt N"), so the same question is answered from notes next time.
- Owner (Settings → Helpers & escalations): make helpers, see escalation rate by week.
  To make someone a helper, they sign up first, then you add their email + subjects.

**Evaluation harness** (backs the deck's numbers)
```
python -m src.eval make --user you@x.com      # drafts eval/<you>.jsonl from your notes
#  -> read it, fix/delete weak questions
python -m src.eval run  --user you@x.com      # writes eval/report-*.md and .json
```
Reports MRR@10 / Recall@5 vs a BM25 keyword baseline, judged answer accuracy,
honest-refusal rate, citation accuracy, latency, and recommended gate thresholds.
`eval/` is git-ignored (it quotes your notes).

## 4. Run

```
./start.sh            # Docker Qdrant + checks Ollama + app + public link
./start.sh --local    # app only, http://localhost:8000
python scripts/smoke_live.py          # live end-to-end check (app must be running)
```
Owner tools: `VS_ADMIN_EMAILS="you@x.com" ./start.sh`

## 5. Tests

```
pip install -r requirements-dev.txt
npm install                           # UI test packages (Node 22+)
docker start qdrant                   # tests use the same Qdrant server
QDRANT_URL=http://localhost:6333 python -m pytest -q
```
Each run uses its own throwaway Qdrant collection and temp users.db — your real data
is never touched.

## 6. Next (phase 3)

1. Run `python -m src.eval` on real notes; adopt the recommended thresholds if sensible.
2. Streaming answers (token by token).
3. Handwriting grading (OCR in front of `grade()`), voice input.
4. Slide 11 still says Streamlit — it's FastAPI + a custom UI now.
5. Remove `users.db` / uploads from the public GitHub history (see the chat for commands).
