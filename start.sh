#!/bin/bash
# ============================================================
#  Vidhyāsathi — one-command startup          (alias: vstart)
#  Brings up everything, in order, and runs it in the background:
#    Docker → Qdrant → Ollama (+ model) → Python packages → app → share link
#  Usage:  vstart            app + public share link
#          vstart --local    app only (http://localhost:8000)
#  Stop:   vstop
#  First time only:  ./start.sh --install   (adds vstart / vstop / vcheck)
# ============================================================
cd "$(dirname "$0")" || exit 1
PROJECT="$(pwd)"
LOGDIR="$PROJECT/.logs"
mkdir -p "$LOGDIR"

# ---- one-time: install the shell commands ----
if [ "$1" = "--install" ]; then
  RC="$HOME/.zshrc"
  touch "$RC"
  sed -i '' '/# >>> vidhyasathi >>>/,/# <<< vidhyasathi <<</d' "$RC" 2>/dev/null
  cat >> "$RC" <<EOF
# >>> vidhyasathi >>>
alias vstart='"$PROJECT/start.sh"'
alias vstop='"$PROJECT/stop.sh"'
alias vcheck='"$PROJECT/Run Vidhyasathi Check.command"'
# <<< vidhyasathi <<<
EOF
  echo "Installed. Open a NEW terminal tab, then use:  vstart   vstop   vcheck"
  exit 0
fi

LOCAL=0; [ "$1" = "--local" ] && LOCAL=1
wait_for() { local n=$1; shift; for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
die() { echo; echo "✗ $*"; exit 1; }

echo "=============================================="
echo "  Starting Vidhyāsathi"
echo "=============================================="

# settings (owner email etc.) live in .env
if [ -f .env ]; then set -a; source .env; set +a; fi

# ---- 0. already running? ----
if [ -f "$LOGDIR/app.pid" ] && kill -0 "$(cat "$LOGDIR/app.pid")" 2>/dev/null; then
  echo "Vidhyāsathi is already running (stop it first with: vstop)"
  [ -f "$LOGDIR/share-url.txt" ] && echo "Share link: $(cat "$LOGDIR/share-url.txt")"
  exit 0
fi

# ---- 1. Docker ----
echo "[1/6] Docker…"
if ! docker info >/dev/null 2>&1; then
  open -a Docker || die "Docker Desktop isn't installed."
  echo "      waiting for Docker Desktop to start…"
  wait_for 180 docker info || die "Docker Desktop didn't start. Open it manually, then run vstart again."
fi

# ---- 2. Qdrant ----
echo "[2/6] knowledge base (Qdrant)…"
docker start qdrant >/dev/null 2>&1 || docker run -d --name qdrant -p 6333:6333 \
  -v "$PROJECT/qdrant_storage_server:/qdrant/storage" qdrant/qdrant >/dev/null \
  || die "couldn't start the Qdrant container"
wait_for 60 curl -s -m 2 http://localhost:6333/collections || die "Qdrant didn't come up on port 6333"

# ---- 3. Ollama ----
echo "[3/6] language model (Ollama)…"
if ! curl -s -m 2 http://localhost:11434/api/tags >/dev/null; then
  if [ -d /Applications/Ollama.app ]; then open -a Ollama; else (ollama serve >"$LOGDIR/ollama.log" 2>&1 &); fi
  wait_for 90 curl -s -m 2 http://localhost:11434/api/tags || die "Ollama didn't start"
fi
if ! ollama list 2>/dev/null | grep -q "llama3.1:8b"; then
  echo "      pulling llama3.1:8b (one time, ~5 GB)…"
  ollama pull llama3.1:8b || die "couldn't pull llama3.1:8b"
fi

# ---- 4. Python packages (only reinstalled when requirements.txt changes) ----
echo "[4/6] Python packages…"
[ -x .venv/bin/python ] || python3 -m venv .venv || die "couldn't create .venv"
source .venv/bin/activate
REQ_HASH="$(shasum requirements.txt | cut -d' ' -f1)"
if [ "$(cat .venv/.req-hash 2>/dev/null)" != "$REQ_HASH" ]; then
  python -m pip install -q -r requirements.txt || die "pip install failed"
  echo "$REQ_HASH" > .venv/.req-hash
fi

# ---- 5. the app ----
echo "[5/6] app on http://localhost:8000 …"
lsof -ti tcp:8000 2>/dev/null | xargs kill 2>/dev/null; sleep 1
nohup caffeinate -s python -m uvicorn app:app --host 127.0.0.1 --port 8000 \
  > "$LOGDIR/app.log" 2>&1 &
echo $! > "$LOGDIR/app.pid"
wait_for 120 curl -s -m 2 http://localhost:8000/api/health \
  || die "the app didn't start — see $LOGDIR/app.log"
HEALTH="$(curl -s http://localhost:8000/api/health)"
echo "      health: $HEALTH"
echo "      warming up the model…"
curl -s -m 120 http://localhost:11434/api/generate \
  -d '{"model":"llama3.1:8b","prompt":"hi","stream":false,"options":{"num_predict":1}}' >/dev/null 2>&1

# ---- 6. share link ----
rm -f "$LOGDIR/share-url.txt"
if [ "$LOCAL" = "0" ]; then
  echo "[6/6] public share link (Cloudflare)…"
  if command -v cloudflared >/dev/null 2>&1; then
    nohup cloudflared tunnel --protocol http2 --url http://localhost:8000 > "$LOGDIR/tunnel.log" 2>&1 &
    echo $! > "$LOGDIR/tunnel.pid"
    for _ in $(seq 1 40); do
      URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGDIR/tunnel.log" | head -1)"
      [ -n "$URL" ] && break; sleep 1
    done
    [ -n "$URL" ] && echo "$URL" > "$LOGDIR/share-url.txt" || echo "      (no link yet — see $LOGDIR/tunnel.log)"
  else
    echo "      cloudflared isn't installed (brew install cloudflared) — running locally only"
  fi
else
  echo "[6/6] share link skipped (--local)"
fi

echo
echo "=============================================="
echo "  Vidhyāsathi is running"
echo "  On this Mac:  http://localhost:8000"
[ -f "$LOGDIR/share-url.txt" ] && echo "  Share link:   $(cat "$LOGDIR/share-url.txt")   (opens ~20 s after it appears)"
[ -n "$VS_ADMIN_EMAILS" ] && echo "  Owner tools:  sign in as $VS_ADMIN_EMAILS"
echo "  Logs:         $LOGDIR/app.log"
echo "  Stop with:    vstop"
echo "=============================================="
open "http://localhost:8000" 2>/dev/null
