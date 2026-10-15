#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Vidhyāsathi autopilot — double-click this file in Finder.
#
#  It does everything, start to finish, and writes a full log to logs/:
#    1. starts Docker Desktop and Ollama if they aren't running
#    2. makes sure llama3.1:8b is pulled
#    3. installs / updates the Python packages in .venv
#    4. starts the Qdrant knowledge base (Docker)
#    5. runs the automated test suites (backend + UI)
#    6. starts the app and runs the live end-to-end check against the real model
#    7. runs the evaluation on your real uploaded notes (accuracy, MRR, refusals)
#  Then it stops the app again. Your accounts and notes are never modified:
#  every test uses throwaway accounts and its own temporary collections.
#
#  Options (when run from a terminal):  --no-eval   skip step 7 (saves ~15 min)
#                                       --serve     leave the app + public link running
# ─────────────────────────────────────────────────────────────────────────────
cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"
mkdir -p logs
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="logs/autopilot-$STAMP.log"
SUMMARY="logs/latest-summary.txt"
exec > >(tee -a "$LOG") 2>&1

RUN_EVAL=1; SERVE=0
for a in "$@"; do
  [ "$a" = "--no-eval" ] && RUN_EVAL=0
  [ "$a" = "--serve" ] && SERVE=1
done

: > "$SUMMARY"
say()  { echo; echo "━━━ $* ━━━"; }
note() { echo "$*" | tee -a "$SUMMARY"; }
fail() { note "FAILED: $*"; note "Full log: $ROOT/$LOG"; echo; echo "Press Return to close."; read -r _; exit 1; }
wait_for() {  # wait_for <seconds> <command...>
  local n=$1; shift
  for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

note "Vidhyāsathi autopilot — $(date)"
note "Project: $ROOT"

# ── 1. Docker ────────────────────────────────────────────────────────────────
say "1/7  Docker"
if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop…"
  open -a Docker || fail "Docker Desktop isn't installed (https://www.docker.com/products/docker-desktop/)"
  wait_for 180 docker info || fail "Docker Desktop didn't start within 3 minutes"
fi
note "Docker: running"

# ── 2. Ollama ────────────────────────────────────────────────────────────────
say "2/7  Ollama"
if ! curl -s -m 2 http://localhost:11434/api/tags >/dev/null; then
  echo "Starting Ollama…"
  if [ -d /Applications/Ollama.app ]; then open -a Ollama; else (ollama serve >/dev/null 2>&1 &); fi
  wait_for 90 curl -s -m 2 http://localhost:11434/api/tags || fail "Ollama didn't start"
fi
if ! ollama list 2>/dev/null | grep -q "llama3.1:8b"; then
  echo "Pulling llama3.1:8b (one time, ~5 GB)…"
  ollama pull llama3.1:8b || fail "couldn't pull llama3.1:8b"
fi
note "Ollama: running, llama3.1:8b ready"

# ── 3. Python packages ───────────────────────────────────────────────────────
say "3/7  Python packages"
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv…"
  python3 -m venv .venv || fail "couldn't create a Python virtual environment"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip >/dev/null 2>&1
python -m pip install -q -r requirements.txt -r requirements-dev.txt || fail "pip install failed (see log)"
note "Python: $(python --version 2>&1), packages installed"

# ── 4. Qdrant ────────────────────────────────────────────────────────────────
say "4/7  Qdrant knowledge base"
docker start qdrant >/dev/null 2>&1 || docker run -d --name qdrant -p 6333:6333 \
  -v "$ROOT/qdrant_storage_server:/qdrant/storage" qdrant/qdrant >/dev/null \
  || fail "couldn't start the Qdrant container"
wait_for 60 curl -s -m 2 http://localhost:6333/collections || fail "Qdrant didn't come up on port 6333"
note "Qdrant: running"
export QDRANT_URL=http://localhost:6333

# ── 5. Automated tests ───────────────────────────────────────────────────────
say "5/7  Automated tests"
if command -v node >/dev/null 2>&1; then
  npm install --silent >/dev/null 2>&1 || echo "(npm install failed — UI tests will be skipped)"
else
  echo "(Node isn't installed — UI tests will be skipped)"
fi
export VS_NODE_MODULES="$ROOT/node_modules"
python -m pytest -q -p no:cacheprovider --timeout=180 2>&1 | tail -25
TEST_RC=${PIPESTATUS[0]}
TEST_LINE="$(grep -E '[0-9]+ (passed|failed)' "$LOG" | tail -1)"
if [ "$TEST_RC" = "0" ]; then note "Tests: PASS — $TEST_LINE"; else note "Tests: FAIL — $TEST_LINE"; fi

# ── 6. App + live end-to-end check ───────────────────────────────────────────
say "6/7  Live check against the real model"
lsof -ti tcp:8000 2>/dev/null | xargs kill 2>/dev/null
sleep 1
VS_ADMIN_EMAILS="${VS_ADMIN_EMAILS:-}" caffeinate -s python -m uvicorn app:app --host 127.0.0.1 --port 8000 \
  > "logs/app-$STAMP.log" 2>&1 &
APP_PID=$!
trap 'kill $APP_PID 2>/dev/null' EXIT
wait_for 120 curl -s -m 2 http://localhost:8000/api/health || fail "the app didn't start (see logs/app-$STAMP.log)"
echo "Warming up the model (first answer loads it into memory)…"
python scripts/smoke_live.py http://localhost:8000
SMOKE_RC=$?
SMOKE_LINE="$(grep -E '[0-9]+ passed, [0-9]+ warnings, [0-9]+ failed' "$LOG" | tail -1)"
if [ "$SMOKE_RC" = "0" ]; then note "Live check: PASS — $SMOKE_LINE"; else note "Live check: FAIL — $SMOKE_LINE"; fi

# ── 7. Evaluation on your real notes ─────────────────────────────────────────
say "7/7  Evaluation on your uploaded notes"
if [ "$RUN_EVAL" = "1" ]; then
  python scripts/eval_real.py --n 20 --negatives 10
  EVAL_RC=$?
  REPORT="$(ls -t eval/report-*.md 2>/dev/null | head -1)"
  if [ "$EVAL_RC" = "0" ] && [ -n "$REPORT" ]; then
    note "Evaluation: done — $ROOT/$REPORT"
    grep -E "^\| (Answer accuracy|Retrieval MRR|Beats the keyword)" "$REPORT" | tee -a "$SUMMARY"
  else
    note "Evaluation: FAIL (see log)"
  fi
else
  note "Evaluation: skipped (--no-eval)"
fi

# ── done ─────────────────────────────────────────────────────────────────────
echo
note "Finished $(date). Full log: $ROOT/$LOG"
echo; cat "$SUMMARY"
if [ "$SERVE" = "1" ]; then
  trap - EXIT
  echo; echo "Leaving the app running. Public link:"
  cloudflared tunnel --protocol http2 --url http://localhost:8000
else
  kill $APP_PID 2>/dev/null
  echo; echo "Done — you can close this window."
fi
