"""Runs the JSDOM front-end suite (tests/frontend/*.test.mjs) from pytest.

Requires Node >= 22 and the dev packages:  npm install   (in the project root;
reads package.json). Without Node the UI suite is skipped, not failed.
A non-zero exit means at least one UI test failed -> see the printed TAP output.
"""
import os
import shutil

import pytest
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_frontend_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node isn't installed — UI tests skipped (install Node 22+ and run: npm install)")
    env = dict(os.environ)
    extra = os.environ.get("VS_NODE_MODULES", "/tmp/node_modules")
    env["NODE_PATH"] = os.pathsep.join(p for p in (extra, env.get("NODE_PATH", "")) if p)
    p = subprocess.run([node, "--test", "tests/frontend/"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=600)
    out = p.stdout + p.stderr
    failed = [l for l in out.splitlines() if l.lstrip().startswith("not ok")]
    summary = [l for l in out.splitlines() if l.startswith(("# tests", "# pass", "# fail"))]
    assert p.returncode == 0, (
        f"node --test exited {p.returncode}\n" + "\n".join(summary) + "\n" + "\n".join(failed)
        + "\n\n--- full output (tail) ---\n" + out[-8000:])
