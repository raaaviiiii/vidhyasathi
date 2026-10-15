#!/bin/bash
# ============================================================
#  Vidhyāsathi — stop everything started by start.sh   (alias: vstop)
#  Stops the share link and the app, then the Qdrant container.
#  Ollama and Docker Desktop are left running (other projects use them).
#  Your accounts, notes and knowledge base are kept.
# ============================================================
cd "$(dirname "$0")" || exit 1
PROJECT="$(pwd)"
echo "Stopping Vidhyāsathi…"

# private/logs is where start.sh keeps its run files (.logs = before the tidy-up)
for LOGDIR in "$PROJECT/private/logs" "$PROJECT/.logs"; do
  for name in tunnel app; do
    if [ -f "$LOGDIR/$name.pid" ]; then
      kill "$(cat "$LOGDIR/$name.pid")" 2>/dev/null && echo "  stopped $name"
      rm -f "$LOGDIR/$name.pid"
    fi
  done
  rm -f "$LOGDIR/share-url.txt"
done
# anything still holding port 8000 (e.g. an app started by hand)
lsof -ti tcp:8000 2>/dev/null | xargs kill 2>/dev/null
pkill -f "caffeinate -s python -m uvicorn app:app --host 127.0.0.1 --port 8000" 2>/dev/null

docker stop qdrant >/dev/null 2>&1 && echo "  stopped Qdrant"
echo "Done. Start again with: vstart"
