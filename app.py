"""Vidhyasathi web backend — wraps the existing RAG modules and serves the UI.

Run (from your project root, with your venv active):
    pip install fastapi uvicorn
    uvicorn app:app --reload --port 8000
Then open http://localhost:8000

The heavy deps (ollama, qdrant, sentence-transformers) are the ones your CLI already
uses; this just exposes answer()/generate()/grade() over HTTP and serves index.html.
"""
import json, shutil, sqlite3, hashlib, secrets, time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.answer import answer
from src.exam import generate
from src.grade import grade
from src.build_index import ingest_file
from src.store import delete_source, user_sources
from src.ingest import source_label

BASE = Path(__file__).parent


def _uid(user: str | None) -> str | None:
    """Normalise the client-supplied user key (email) — blank means shared/None."""
    user = (user or "").strip().lower()
    return user or None


def _user_dir(user: str | None) -> Path:
    """Per-user upload folder, keyed by a short hash of the email."""
    tag = hashlib.sha256((user or "__shared__").encode()).hexdigest()[:16]
    d = BASE / "data" / "uploads" / tag
    d.mkdir(parents=True, exist_ok=True)
    return d
app = FastAPI(title="Vidhyasathi")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def clean(obj):
    """Cast numpy floats etc. to JSON-safe types."""
    return json.loads(json.dumps(obj, default=float))


class AnswerReq(BaseModel):
    question: str
    mode: str = "quick"      # quick | teach
    web: bool = False
    history: list = []       # [{role, content}] for teach mode
    user: str = ""           # logged-in email → scopes retrieval to their material


class ExamReq(BaseModel):
    topic: str
    n: int = 5
    user: str = ""


class GradeReq(BaseModel):
    question: str
    student_answer: str
    user: str = ""


@app.post("/api/answer")
def api_answer(r: AnswerReq):
    res = answer(r.question, mode=r.mode, history=r.history or None,
                 web=r.web, user=_uid(r.user))
    return JSONResponse(clean(res))


@app.post("/api/exam")
def api_exam(r: ExamReq):
    return JSONResponse(clean(generate(r.topic, r.n, user=_uid(r.user))))


@app.post("/api/grade")
def api_grade(r: GradeReq):
    return JSONResponse(clean(grade(r.question, r.student_answer, user=_uid(r.user))))


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


@app.get("/api/material")
def api_material(user: str = ""):
    """List only THIS user's uploaded files, flagging which are indexed."""
    uid = _uid(user)
    d = _user_dir(uid)
    indexed = user_sources(uid)
    docs = [{"name": p.name, "size": p.stat().st_size,
             "indexed": source_label(p) in indexed}
            for p in sorted(d.iterdir())
            if p.is_file() and not p.name.startswith(".")]
    return {"docs": docs}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), user: str = Form("")):
    """Save the file into the user's own folder and index it in-process (shared
    Qdrant client — no subprocess, so no embedded-storage lock clash), tagging
    every chunk with the user so retrieval only ever sees their own material."""
    uid = _uid(user)
    dest = _user_dir(uid) / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        res = ingest_file(dest, user=uid)
        if not res.get("ok"):
            dest.unlink(missing_ok=True)     # don't leave an unindexable orphan
            return {"ok": True, "name": file.filename, "indexed": False,
                    "error": res.get("error", "indexing failed")}
        return {"ok": True, "name": file.filename, "indexed": True,
                "chunks": res["chunks"], "ocr": res.get("ocr", 0)}
    except Exception as e:
        return {"ok": True, "name": file.filename, "indexed": False, "error": str(e)[:200]}


class DeleteMaterialReq(BaseModel):
    user: str = ""
    name: str


@app.post("/api/material/delete")
def api_material_delete(r: DeleteMaterialReq):
    """Remove one of the user's documents: its chunks from the KB and the file."""
    uid = _uid(r.user)
    d = _user_dir(uid)
    p = d / Path(r.name).name             # guard against path traversal
    if p.exists() and p.is_file():
        delete_source(uid, source_label(p))
        p.unlink(missing_ok=True)
        return {"ok": True, "name": r.name}
    return JSONResponse({"ok": False, "error": "Not found."}, status_code=404)


USERS_DB = BASE / "users.db"


def _db():
    c = sqlite3.connect(USERS_DB)
    c.execute("CREATE TABLE IF NOT EXISTS users("
              "id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, pw TEXT, salt TEXT, created REAL)")
    return c


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


class SignupReq(BaseModel):
    name: str
    email: str
    password: str


class LoginReq(BaseModel):
    email: str
    password: str


@app.post("/api/signup")
def signup(r: SignupReq):
    email = r.email.strip().lower()
    if not email or not r.password or not r.name.strip():
        return JSONResponse({"ok": False, "error": "All fields are required."}, status_code=400)
    salt = secrets.token_hex(8)
    c = _db()
    try:
        c.execute("INSERT INTO users(name,email,pw,salt,created) VALUES(?,?,?,?,?)",
                  (r.name.strip(), email, _hash(r.password, salt), salt, time.time()))
        c.commit()
    except sqlite3.IntegrityError:
        return JSONResponse({"ok": False, "error": "An account with that email already exists."}, status_code=409)
    finally:
        c.close()
    return {"ok": True, "name": r.name.strip(), "email": email}


@app.post("/api/login")
def login(r: LoginReq):
    email = r.email.strip().lower()
    c = _db()
    row = c.execute("SELECT name,pw,salt FROM users WHERE email=?", (email,)).fetchone()
    c.close()
    if not row or _hash(r.password, row[2]) != row[1]:
        return JSONResponse({"ok": False, "error": "Wrong email or password."}, status_code=401)
    return {"ok": True, "name": row[0], "email": email}


class PasswordReq(BaseModel):
    email: str
    old_password: str
    new_password: str


@app.post("/api/account/password")
def change_password(r: PasswordReq):
    email = r.email.strip().lower()
    if len(r.new_password or "") < 4:
        return JSONResponse({"ok": False, "error": "New password is too short."}, status_code=400)
    c = _db()
    row = c.execute("SELECT pw,salt FROM users WHERE email=?", (email,)).fetchone()
    if not row or _hash(r.old_password, row[1]) != row[0]:
        c.close()
        return JSONResponse({"ok": False, "error": "Current password is wrong."}, status_code=401)
    salt = secrets.token_hex(8)
    c.execute("UPDATE users SET pw=?, salt=? WHERE email=?",
              (_hash(r.new_password, salt), salt, email))
    c.commit(); c.close()
    return {"ok": True}


class DeleteAccountReq(BaseModel):
    email: str
    password: str


@app.post("/api/account/delete")
def delete_account(r: DeleteAccountReq):
    """Verify password, then remove the account, its material chunks, and its files."""
    from src.store import delete_user
    email = r.email.strip().lower()
    c = _db()
    row = c.execute("SELECT pw,salt FROM users WHERE email=?", (email,)).fetchone()
    if not row or _hash(r.password, row[1]) != row[0]:
        c.close()
        return JSONResponse({"ok": False, "error": "Wrong password."}, status_code=401)
    c.execute("DELETE FROM users WHERE email=?", (email,)); c.commit(); c.close()
    delete_user(email)
    d = _user_dir(email)
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


@app.get("/api/users")
def list_users():
    """Who has signed up (for you to check who's testing)."""
    c = _db()
    rows = c.execute("SELECT name,email,datetime(created,'unixepoch','localtime') FROM users ORDER BY created DESC").fetchall()
    c.close()
    return {"count": len(rows), "users": [{"name": n, "email": e, "joined": j} for n, e, j in rows]}
