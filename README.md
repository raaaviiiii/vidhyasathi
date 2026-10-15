# Vidhyāsathi

A study assistant that answers only from your own notes, cites where each answer came
from, and says so honestly when the notes don't cover a question. Everything runs on
this Mac: FastAPI app, Qdrant knowledge base (Docker), and llama3.1:8b (Ollama).

## Run it

```bash
./start.sh --install   # one time: adds the vstart / vstop / vcheck commands
vstart                 # start everything + public share link   (vstart --local: no link)
vstop                  # stop it
vcheck                 # full automatic test run + evaluation; summary in private/logs/
```

## What's where

```
vidhyasathi/
├── app.py              the web server (accounts, uploads, ask, exam, help desk)
├── start.sh  stop.sh   vstart / vstop
├── src/                the engine: ingest, index, retrieve, answer, exam, grade, eval
├── web/index.html      the page students see
├── scripts/            check.command (vcheck), live smoke test, real-notes evaluation
│   └── dev/            one-off OCR probes
├── tests/              automated tests (backend + UI)
├── docs/               handoff notes, QA spec and reports
└── private/            🔒 never committed, owner-only (chmod 700)
    ├── .env            settings and secrets (owner email, API key)
    ├── users.db        accounts and sessions
    ├── uploads/        each student's uploaded notes
    ├── docs/           optional shared notes for python -m src.build_index
    ├── qdrant/         the knowledge base (mounted into the Qdrant container)
    ├── ocr/            OCR text cache and cost log
    ├── eval/           evaluation sets and reports (they quote your notes)
    ├── logs/           app, tunnel, test and check logs
    └── backups/        copies made before updates
```

Anything private that an older version left in the project root is moved into
`private/` automatically the next time you run `vstart` or `vcheck`. Nothing is ever
overwritten: if a copy already exists in `private/`, the old one is left in place.
