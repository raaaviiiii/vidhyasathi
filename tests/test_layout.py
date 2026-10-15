"""The private-files layout: leftovers move into private/, nothing is overwritten,
private/ is owner-only, and git never tracks anything private."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

from src import layout

ROOT = Path(__file__).resolve().parent.parent


def _old_project(base: Path):
    (base / "users.db").write_bytes(b"accounts")
    (base / ".env").write_text("VS_ADMIN_EMAILS=me@x.com\n")
    up = base / "data" / "uploads" / "abc"; up.mkdir(parents=True)
    (up / "Module 1.pptx").write_bytes(b"pptx")
    (base / "data" / "docs").mkdir(parents=True)
    (base / "data" / "docs" / ".gitkeep").write_text("")
    (base / "data" / "docs" / "syllabus.pdf").write_bytes(b"pdf")
    (base / "ocr_cache.json").write_text("{}")
    (base / "eval").mkdir(); (base / "eval" / "report-1.md").write_text("r")
    (base / "logs").mkdir(); (base / "logs" / "autopilot-1.log").write_text("l")
    (base / ".backup-before-update-1").mkdir()
    (base / ".backup-before-update-1" / "users.db").write_bytes(b"old")


def test_migrate_moves_every_private_file_and_cleans_the_root(tmp_path, monkeypatch):
    monkeypatch.delenv("VS_NO_MIGRATE", raising=False)
    priv = tmp_path / "private"
    _old_project(tmp_path)
    layout.migrate(tmp_path, priv, quiet=True)
    assert (priv / "users.db").read_bytes() == b"accounts"
    assert (priv / ".env").exists()
    assert (priv / "uploads" / "abc" / "Module 1.pptx").read_bytes() == b"pptx"
    assert (priv / "docs" / "syllabus.pdf").exists()
    assert (priv / "ocr" / "ocr_cache.json").exists()
    assert (priv / "eval" / "report-1.md").exists()
    assert (priv / "logs" / "autopilot-1.log").exists()
    assert (priv / "backups" / "backup-before-update-1" / "users.db").exists()
    for gone in ("users.db", ".env", "data", "ocr_cache.json", "eval", "logs",
                 ".backup-before-update-1"):
        assert not (tmp_path / gone).exists(), f"{gone} was left in the public project folder"


def test_migrate_never_overwrites_and_is_safe_to_run_twice(tmp_path, monkeypatch):
    monkeypatch.delenv("VS_NO_MIGRATE", raising=False)
    priv = tmp_path / "private"; priv.mkdir()
    (priv / "users.db").write_bytes(b"new")
    (tmp_path / "users.db").write_bytes(b"old")
    moved, kept = layout.migrate(tmp_path, priv, quiet=True)
    assert (priv / "users.db").read_bytes() == b"new"
    assert (tmp_path / "users.db").read_bytes() == b"old", "the conflicting copy must be kept"
    assert kept
    assert layout.migrate(tmp_path, priv, quiet=True)[0] == []


def test_migrate_merges_into_an_existing_uploads_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("VS_NO_MIGRATE", raising=False)
    priv = tmp_path / "private"
    (priv / "uploads" / "abc").mkdir(parents=True)
    (priv / "uploads" / "abc" / "a.pdf").write_bytes(b"a")
    old = tmp_path / "data" / "uploads" / "abc"; old.mkdir(parents=True)
    (old / "b.pdf").write_bytes(b"b")
    layout.migrate(tmp_path, priv, quiet=True)
    assert sorted(p.name for p in (priv / "uploads" / "abc").iterdir()) == ["a.pdf", "b.pdf"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_private_folder_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.delenv("VS_NO_MIGRATE", raising=False)
    priv = tmp_path / "private"
    _old_project(tmp_path)
    layout.migrate(tmp_path, priv, quiet=True)
    assert stat.S_IMODE(priv.stat().st_mode) == 0o700
    assert stat.S_IMODE((priv / "users.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((priv / ".env").stat().st_mode) == 0o600


def test_tests_never_migrate_the_real_project():
    assert os.environ.get("VS_NO_MIGRATE") == "1"
    assert layout.migrate() == ([], [])


def test_git_tracks_nothing_private():
    try:
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    bad = [f for f in files if f.startswith(("private/", "data/", "eval/", "logs/", ".logs/",
                                             "qdrant_storage"))
           or f in (".env", "users.db", "ocr_cache.json", "ocr_cost_log.csv")]
    assert not bad, f"private files are tracked by git: {bad}"
    probe = subprocess.run(["git", "check-ignore", "-q", "private/users.db"], cwd=ROOT)
    assert probe.returncode == 0, "private/ is not git-ignored"


def test_the_page_is_served_from_web(app_client):
    r = app_client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert (ROOT / "web" / "index.html").exists() and not (ROOT / "index.html").exists()
