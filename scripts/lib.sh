#!/bin/bash
# Shared helpers for start.sh, stop.sh and scripts/check.command.
# Source it after setting PROJECT to the project root.

PRIVATE="$PROJECT/private"
QDRANT_DIR="$PRIVATE/qdrant"

wait_for() {  # wait_for <seconds> <command...>
  local n=$1; shift
  for _ in $(seq 1 "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

# Move leftovers from the old flat layout into private/ and lock it to this Mac account.
tidy_private() {
  mkdir -p "$PRIVATE" && chmod 700 "$PRIVATE"
  # the Qdrant database: stop the container before moving its folder
  if [ -d "$PROJECT/qdrant_storage_server" ] && [ ! -e "$QDRANT_DIR" ]; then
    echo "      moving the knowledge base into private/ (one time)…"
    docker stop qdrant >/dev/null 2>&1
    mv "$PROJECT/qdrant_storage_server" "$QDRANT_DIR" || return 1
  fi
  # an app still running from the old layout would keep writing to the old files
  if [ -e "$PROJECT/users.db" ]; then
    lsof -ti tcp:8000 2>/dev/null | xargs kill 2>/dev/null
  fi
  # old run files (.logs/) from before the move
  if [ -d "$PROJECT/.logs" ]; then
    for name in tunnel app; do
      [ -f "$PROJECT/.logs/$name.pid" ] && kill "$(cat "$PROJECT/.logs/$name.pid")" >/dev/null 2>&1
    done
    mkdir -p "$PRIVATE/logs/old-run-logs"
    mv "$PROJECT/.logs/"*.log "$PRIVATE/logs/old-run-logs/" 2>/dev/null
    rm -f "$PROJECT/.logs/"*.pid "$PROJECT/.logs/share-url.txt"
    rmdir "$PROJECT/.logs" 2>/dev/null
  fi
  # everything else (accounts, uploads, OCR cache, eval, logs, .env) — done in Python
  (cd "$PROJECT" && "${PYTHON:-python3}" -m src.layout) || true
  mkdir -p "$QDRANT_DIR"
}

# Start the Qdrant container (a new one keeps its data in private/qdrant).
start_qdrant() {
  # A Qdrant is already answering on 6333 (e.g. a container with another name that
  # already holds your knowledge base) — use it and don't start a second one.
  if curl -s -m 2 http://localhost:6333/collections >/dev/null 2>&1; then
    return 0
  fi
  local src
  src="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Source}}{{end}}{{end}}' qdrant 2>/dev/null)"
  # Only a container bound to the OLD project folder is recreated: that folder was just
  # moved to private/qdrant, so the data moves with it. A container that keeps its data
  # anywhere else (a Docker volume) is left exactly as it is.
  case "$src" in
    */qdrant_storage_server) docker rm -f qdrant >/dev/null 2>&1 ;;
  esac
  docker start qdrant >/dev/null 2>&1 || docker run -d --name qdrant -p 6333:6333 \
    -v "$QDRANT_DIR:/qdrant/storage" qdrant/qdrant >/dev/null || return 1
  wait_for 60 curl -s -m 2 http://localhost:6333/collections
}

# Settings and secrets (owner email, API key) live in private/.env.
load_env() {
  local f
  for f in "$PRIVATE/.env" "$PROJECT/.env"; do
    if [ -f "$f" ]; then set -a; source "$f"; set +a; fi
  done
}
