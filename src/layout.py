"""Keep the project folder tidy and the private data out of public view.

Everything that belongs to the owner or the students — accounts, uploaded notes, the
OCR text cache, evaluation reports, logs, secrets — lives in private/. That folder is
never tracked by git and only the owner's Mac account can open it (chmod 700).

Older copies of the project kept those files loose in the project root. `migrate()`
moves any leftovers into private/ the first time the new code runs. It never
overwrites: if something already exists at the destination the old copy is left where
it is and reported, so nothing can be lost.

The Qdrant database folder (qdrant_storage_server/) is moved by start.sh instead,
because the Qdrant container has to be stopped first.

    python -m src.layout          # migrate + show what moved
"""
import os
import shutil
from pathlib import Path

from src.config import BASE_DIR, PRIVATE_DIR

# old location (relative to the project root) -> new location inside private/
MOVES = [
    ("users.db", "users.db"),
    ("users.db-wal", "users.db-wal"),
    ("users.db-shm", "users.db-shm"),
    ("users.db-journal", "users.db-journal"),
    (".env", ".env"),
    ("data/uploads", "uploads"),
    ("data/docs", "docs"),
    ("ocr_cache.json", "ocr/ocr_cache.json"),
    ("ocr_cost_log.csv", "ocr/ocr_cost_log.csv"),
    ("eval", "eval"),
    ("logs", "logs"),
    ("qdrant_storage", "qdrant-embedded"),      # the old embedded-mode database
]
SKIP_NAMES = {".gitkeep", ".DS_Store"}


def _merge(src: Path, dst: Path, moved: list, kept: list):
    """Move src to dst; for folders, move child by child so nothing is overwritten."""
    if src.is_dir() and not src.is_symlink():
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            # an empty or .gitkeep-only folder isn't worth moving
            if not [p for p in src.iterdir() if p.name not in SKIP_NAMES]:
                return
            shutil.move(str(src), str(dst))
            moved.append((src, dst))
            return
        for child in sorted(src.iterdir()):
            if child.name in SKIP_NAMES:
                continue
            _merge(child, dst / child.name, moved, kept)
        return
    if dst.exists():
        kept.append(src)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    moved.append((src, dst))


def _remove_empty(folder: Path):
    """Delete folders that the move left empty (only if truly empty)."""
    if not folder.is_dir():
        return
    for child in folder.iterdir():
        if child.is_dir():
            _remove_empty(child)
    leftovers = [p for p in folder.iterdir() if p.name not in SKIP_NAMES]
    if not leftovers:
        for p in folder.iterdir():
            p.unlink()
        folder.rmdir()


def lock_down(private: Path = PRIVATE_DIR):
    """Owner-only access: private/ is 700, the account database and secrets are 600."""
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    for name in ("users.db", ".env", "users.db-wal", "users.db-shm"):
        p = private / name
        if p.exists():
            os.chmod(p, 0o600)


def migrate(base: Path = BASE_DIR, private: Path = PRIVATE_DIR, quiet: bool = False):
    """Move leftovers from the old flat layout into private/. Safe to run every start."""
    if os.environ.get("VS_NO_MIGRATE"):
        return [], []
    moved, kept = [], []
    private.mkdir(parents=True, exist_ok=True)
    for old, new in MOVES:
        src = base / old
        if src.exists() or src.is_symlink():
            try:
                _merge(src, private / new, moved, kept)
            except OSError as e:                     # never stop the app over tidying
                if not quiet:
                    print(f"  could not move {old}: {e}")
    for backup in sorted(base.glob(".backup-before-update-*")):
        try:
            _merge(backup, private / "backups" / backup.name.lstrip("."), moved, kept)
        except OSError:
            pass
    for old in ("data", "eval", "logs"):
        try:
            _remove_empty(base / old)
        except OSError:
            pass
    try:
        lock_down(private)
    except OSError:
        pass
    if not quiet:
        rel = lambda p: os.path.relpath(p, base)
        for src, dst in moved:
            print(f"  moved {rel(src)} -> {rel(dst)}")
        for src in kept:
            print(f"  left {rel(src)} in place (a copy already exists in private/)")
    return moved, kept


if __name__ == "__main__":
    m, k = migrate()
    if not m and not k:
        print("  private/ is up to date — nothing to move")
