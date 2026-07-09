"""Vidhyasathi web backend — wraps the existing RAG modules and serves the UI.

Run (from your project root, with your venv active):
    pip install fastapi uvicorn
    uvicorn app:app --reload --port 8000
Then open http://localhost:8000

The heavy deps (ollama, qdrant, sentence-transformers) are the ones your CLI already
uses; this just exposes answer()/generate()/grade() over HTTP and serves index.html.
"""
import json, sys, subprocess, shutil, sqlite3, hashlib, secrets, time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.answer import answer
from src.exam import generate
from src.grade import grade

BASE = Path(__file__).parent
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


class ExamReq(BaseModel):
    topic: str
    n: int = 5


class GradeReq(BaseModel):
    question: str
    student_answer: str


@app.post("/api/answer")
def api_answer(r: AnswerReq):
    res = answer(r.question, mode=r.mode, history=r.history or None, web=r.web)
    return JSONResponse(clean(res))


@app.post("/api/exam")
def api_exam(r: ExamReq):
    return JSONResponse(clean(generate(r.topic, r.n)))


@app.post("/api/grade")
def api_grade(r: GradeReq):
    return JSONResponse(clean(grade(r.question, r.student_answer)))


@app.get("/")
def index():
    return FileResponse(BASE / "index.html")


DOCS = BASE / "data" / "docs"


@app.get("/api/material")
def api_material():
    DOCS.mkdir(parents=True, exist_ok=True)
    docs = [{"name": p.name, "size": p.stat().st_size}
            for p in sorted(DOCS.iterdir())
            if p.is_file() and not p.name.startswith(".")]
    return {"docs": docs}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    DOCS.mkdir(parents=True, exist_ok=True)
    dest = DOCS / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # Re-index. Adjust this command to match how you ingest documents.
    try:
        subprocess.run([sys.executable, "-m", "src.ingest"],
                       cwd=str(BASE), check=True, timeout=1800)
        return {"ok": True, "name": file.filename, "indexed": True}
    except Exception as e:
        return {"ok": True, "name": file.filename, "indexed": False, "error": str(e)[:200]}


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
    return {"ok": True, "name": r.name.strip()}


@app.post("/api/login")
def login(r: LoginReq):
    email = r.email.strip().lower()
    c = _db()
    row = c.execute("SELECT name,pw,salt FROM users WHERE email=?", (email,)).fetchone()
    c.close()
    if not row or _hash(r.password, row[2]) != row[1]:
        return JSONResponse({"ok": False, "error": "Wrong email or password."}, status_code=401)
    return {"ok": True, "name": row[0]}


@app.get("/api/users")
def list_users():
    """Who has signed up (for you to check who's testing)."""
    c = _db()
    rows = c.execute("SELECT name,email,datetime(created,'unixepoch','localtime') FROM users ORDER BY created DESC").fetchall()
    c.close()
    return {"count": len(rows), "users": [{"name": n, "email": e, "joined": j} for n, e, j in rows]}
