#!/bin/bash
# Start Vidhyāsathi: Qdrant (Docker) -> Ollama check -> app -> public link.
# Usage:  ./start.sh            (app + public trycloudflare link)
#         ./start.sh --local    (app only, http://localhost:8000)
# Needs: Docker Desktop running, Ollama running (llama3.1:8b is pulled if missing).
set -e
cd "$(dirname "$0")"
[ -f .venv/bin/activate ] && source .venv/bin/activate

echo "1/4  Qdrant (Docker)…"
if ! docker info >/dev/null 2>&1; then
  echo "     Docker isn't running — open Docker Desktop and try again."; exit 1
fi
docker start qdrant >/dev/null 2>&1 || docker run -d --name qdrant -p 6333:6333 \
  -v "$(pwd)/qdrant_storage_server:/qdrant/storage" qdrant/qdrant >/dev/null
for i in $(seq 1 20); do curl -s localhost:6333/collections >/dev/null && break; sleep 1; done

echo "2/4  Ollama…"
if ! ollama list >/dev/null 2>&1; then
  echo "     Ollama isn't running — open the Ollama app (or run: ollama serve) and try again."; exit 1
fi
ollama list | grep -q "llama3.1:8b" || { echo "     Pulling llama3.1:8b (one time)…"; ollama pull llama3.1:8b; }

echo "3/4  App on http://localhost:8000 …"
caffeinate -s uvicorn app:app --host 127.0.0.1 --port 8000 &
APP=$!
trap 'kill $APP 2>/dev/null' EXIT
for i in $(seq 1 60); do curl -s localhost:8000/api/health >/dev/null && break; sleep 1; done

if [ "$1" = "--local" ]; then
  echo "     Ready: http://localhost:8000   (Ctrl-C to stop)"; wait $APP; exit 0
fi

echo "4/4  Public link (keep this window open; Ctrl-C stops everything)…"
echo "     Wait ~20 seconds after the link appears before opening it."
cloudflared tunnel --protocol http2 --url http://localhost:8000
