"""Vidhyasathi web backend — wraps the RAG engine and serves the UI.

Run (project root, venv active, Qdrant + Ollama running):
    uvicorn app:app --host 127.0.0.1 --port 8000
Then open http://localhost:8000

Security model (the app is shared over a public tunnel):
  - Sign-up / sign-in return a session token. Every data endpoint requires
    `Authorization: Bearer <token>`; the server derives the student from it and
    IGNORES any `user` field a client sends.
  - Each student's files live in data/uploads/<hash>/ and their chunks are tagged
    with their email, so they can only ever see, query or delete their own material.
  - GET /api/users is admin-only: set VS_ADMIN_EMAILS="you@x.com" before starting.
"""
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from src.answer import answer
from src.build_index import ingest_file
from src.errors import ServiceDown, StoreUnavailable
from src.exam import generate, MAX_N as EXAM_MAX_N
from src.grade import grade
from src.ingest import SUPPORTED
from src.store import delete_doc, delete_user, user_docs

BASE = Path(__file__).parent
USERS_DB = BASE / "users.db"
SESSION_DAYS = 30
MAX_UPLOAD_MB = int(os.environ.get("VS_MAX_UPLOAD_MB", "50"))
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("VS_ADMIN_EMAILS", "").split(",")
                if e.strip()}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title="Vidhyasathi")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# ====================================================================== errors
def _err(status: int, msg: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, **extra}, status_code=status)


@app.exception_handler(HTTPException)
async def _http_exc(_req, exc: HTTPException):
    return _err(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _validation_exc(_req, exc: RequestValidationError):
    errs = exc.errors()
    first = errs[0] if errs else {}
    field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "request"
    msg = str(first.get("msg", "bad value")).removeprefix("Value error, ")
    # a missing/malformed request shape is 422; a bad VALUE (empty, too long) is 400
    text_problem = all(e.get("type") in ("value_error", "string_too_long", "string_too_short")
                       for e in errs)
    status = 400 if errs and text_problem else 422
    return _err(status, f"Invalid {field}: {msg}")


def _is_connection_error(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        name = type(exc).__name__
        if isinstance(exc, (ConnectionError, TimeoutError)) or name in (
                "ConnectError", "ConnectTimeout", "ReadTimeout", "ResponseHandlingException",
                "RemoteProtocolError", "_InactiveRpcError"):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def guarded(fn):
    """Turn outages into a clear 503 and unexpected bugs into a JSON 500 —
    never a raw stack trace or an HTML error page."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except HTTPException:
            raise
        except ServiceDown as e:
            return _err(503, e.user_message)
        except Exception as e:
            if _is_connection_error(e):
                return _err(503, StoreUnavailable.user_message)
            print(f"[vidhyasathi] unexpected error in {fn.__name__}: {e!r}")
            return _err(500, "Something went wrong on the server. Please try again.")
    return wrapper


def clean(obj):
    """Cast numpy floats etc. to JSON-safe types."""
    return json.loads(json.dumps(obj, default=float))


# ====================================================================== auth / db
_db_lock = threading.Lock()


def _db():
    c = sqlite3.connect(USERS_DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, "
              "email TEXT UNIQUE, pw TEXT, salt TEXT, created REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, "
              "email TEXT NOT NULL, created REAL, expires REAL)")
    return c


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_session(email: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with closing(_db()) as c:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                  (_token_hash(token), email, now, now + SESSION_DAYS * 86400))
        c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        c.commit()
    return token


class Student(BaseModel):
    email: str
    name: str
    token_hash: str


def current_user(authorization: str = Header(default="")) -> Student:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(401, "Please sign in.")
    th = _token_hash(token)
    with closing(_db()) as c:
        row = c.execute("SELECT u.email, u.name FROM sessions s JOIN users u "
                        "ON u.email = s.email WHERE s.token_hash=? AND s.expires>?",
                        (th, time.time())).fetchone()
    if not row:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    return Student(email=row[0], name=row[1], token_hash=th)


_fail_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
MAX_FAILS, FAIL_WINDOW = 10, 15 * 60


def _too_many_failures(key: str) -> bool:
    now = time.time()
    with _fail_lock:
        recent = [t for t in _failures.get(key, []) if now - t < FAIL_WINDOW]
        _failures[key] = recent
        return len(recent) >= MAX_FAILS


def _record_failure(key: str):
    with _fail_lock:
        _failures.setdefault(key, []).append(time.time())


def _user_dir(email: str) -> Path:
    """Per-student upload folder, keyed by a short hash of the email."""
    tag = hashlib.sha256(email.encode()).hexdigest()[:16]
    d = BASE / "data" / "uploads" / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


# ====================================================================== request models
def _strip_nonempty(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("must not be empty")
    return v


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class AnswerReq(BaseModel):
    question: str = Field(max_length=4000)
    mode: Literal["quick", "teach"] = "quick"
    web: bool = False
    history: list[HistoryItem] = Field(default_factory=list, max_length=40)
    _q = field_validator("question")(_strip_nonempty)


class ExamReq(BaseModel):
    topic: str = Field(max_length=500)
    n: int = Field(5, ge=1, le=EXAM_MAX_N)
    _t = field_validator("topic")(_strip_nonempty)


class GradeReq(BaseModel):
    question: str = Field(max_length=2000)
    student_answer: str = Field(max_length=8000)
    _q = field_validator("question", "student_answer")(_strip_nonempty)


class SignupReq(BaseModel):
    name: str = Field(max_length=80)
    email: str = Field(max_length=254)
    password: str = Field(min_length=6, max_length=200)
    _n = field_validator("name")(_strip_nonempty)

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        v = (v or "").strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("enter a valid email address")
        return v


class LoginReq(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class PasswordReq(BaseModel):
    old_password: str = Field(max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


class DeleteAccountReq(BaseModel):
    password: str = Field(max_length=200)


class DeleteMaterialReq(BaseModel):
    name: str = Field(max_length=255)


# ====================================================================== pages
@app.get("/")
def index():
    return FileResponse(BASE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    """Unauthenticated, data-free status for the status pill and the smoke test."""
    out = {"ok": True, "qdrant": False, "llm": False}
    try:
        from src.store import get_client
        get_client().get_collections()
        out["qdrant"] = True
    except Exception:
        pass
    try:
        import ollama
        ollama.list()
        out["llm"] = True
    except Exception:
        pass
    out["ok"] = out["qdrant"] and out["llm"]
    return out


# ====================================================================== accounts
@app.post("/api/signup")
def signup(r: SignupReq):
    salt = secrets.token_hex(8)
    with closing(_db()) as c:
        try:
            c.execute("INSERT INTO users(name,email,pw,salt,created) VALUES(?,?,?,?,?)",
                      (r.name, r.email, _hash(r.password, salt), salt, time.time()))
            c.commit()
        except sqlite3.IntegrityError:
            return _err(409, "An account with that email already exists.")
    return {"ok": True, "name": r.name, "email": r.email, "token": _new_session(r.email)}


_DUMMY_SALT = secrets.token_hex(8)


@app.post("/api/login")
def login(r: LoginReq):
    email = r.email.strip().lower()
    if _too_many_failures(email):
        return _err(429, "Too many wrong attempts. Wait 15 minutes and try again.")
    with closing(_db()) as c:
        row = c.execute("SELECT name,pw,salt FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        _hash(r.password, _DUMMY_SALT)          # same work either way (no enumeration)
        _record_failure(email)
        return _err(401, "Wrong email or password.")
    if _hash(r.password, row[2]) != row[1]:
        _record_failure(email)
        return _err(401, "Wrong email or password.")
    return {"ok": True, "name": row[0], "email": email, "token": _new_session(email)}


@app.get("/api/me")
def me(u: Student = Depends(current_user)):
    return {"ok": True, "name": u.name, "email": u.email}


@app.post("/api/logout")
def logout(u: Student = Depends(current_user)):
    with closing(_db()) as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (u.token_hash,))
        c.commit()
    return {"ok": True}


@app.post("/api/account/password")
def change_password(r: PasswordReq, u: Student = Depends(current_user)):
    with closing(_db()) as c:
        row = c.execute("SELECT pw,salt FROM users WHERE email=?", (u.email,)).fetchone()
        if not row or _hash(r.old_password, row[1]) != row[0]:
            return _err(403, "Current password is wrong.")
        salt = secrets.token_hex(8)
        c.execute("UPDATE users SET pw=?, salt=? WHERE email=?",
                  (_hash(r.new_password, salt), salt, u.email))
        # sign out every OTHER device; keep this one
        c.execute("DELETE FROM sessions WHERE email=? AND token_hash<>?", (u.email, u.token_hash))
        c.commit()
    return {"ok": True}


@app.post("/api/account/delete")
@guarded
def delete_account(r: DeleteAccountReq, u: Student = Depends(current_user)):
    """Verify password, then remove the account, its sessions, chunks and files."""
    with closing(_db()) as c:
        row = c.execute("SELECT pw,salt FROM users WHERE email=?", (u.email,)).fetchone()
        if not row or _hash(r.password, row[1]) != row[0]:
            return _err(403, "Wrong password.")
    delete_user(u.email)                       # chunks first: if Qdrant is down, keep the account
    shutil.rmtree(_user_dir(u.email), ignore_errors=True)
    with closing(_db()) as c:
        c.execute("DELETE FROM sessions WHERE email=?", (u.email,))
        c.execute("DELETE FROM users WHERE email=?", (u.email,))
        c.commit()
    return {"ok": True}


@app.get("/api/users")
def list_users(u: Student = Depends(current_user)):
    """Who has signed up — admin only (VS_ADMIN_EMAILS)."""
    if u.email not in ADMIN_EMAILS:
        raise HTTPException(403, "Only the app owner can see the user list.")
    with closing(_db()) as c:
        rows = c.execute("SELECT name,email,datetime(created,'unixepoch','localtime') "
                         "FROM users ORDER BY created DESC").fetchall()
    return {"count": len(rows), "users": [{"name": n, "email": e, "joined": j} for n, e, j in rows]}


# ====================================================================== study modes
@app.post("/api/answer")
@guarded
def api_answer(r: AnswerReq, u: Student = Depends(current_user)):
    res = answer(r.question, mode=r.mode, history=[h.model_dump() for h in r.history],
                 web=r.web, user=u.email)
    return JSONResponse(clean(res))


@app.post("/api/exam")
@guarded
def api_exam(r: ExamReq, u: Student = Depends(current_user)):
    return JSONResponse(clean(generate(r.topic, r.n, user=u.email)))


@app.post("/api/grade")
@guarded
def api_grade(r: GradeReq, u: Student = Depends(current_user)):
    return JSONResponse(clean(grade(r.question, r.student_answer, user=u.email)))


# ====================================================================== material
def _safe_name(filename: str | None) -> str | None:
    """Reduce a client file name to a plain, safe base name (no folders, no
    traversal, no hidden files). None if nothing usable is left."""
    name = (filename or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    if not name or name in (".", ".."):
        return None
    if len(name) > 150:
        p = Path(name)
        name = p.stem[:150 - len(p.suffix)] + p.suffix
    return name


@app.get("/api/material")
@guarded
def api_material(u: Student = Depends(current_user)):
    """Only THIS student's files, each flagged indexed / not indexed."""
    d = _user_dir(u.email)
    live = user_docs(u.email)
    docs = [{"name": p.name, "size": p.stat().st_size, "indexed": p.name in live}
            for p in sorted(d.iterdir(), key=lambda x: x.name.lower())
            if p.is_file() and not p.name.startswith(".")]
    return {"ok": True, "docs": docs}


@app.post("/api/upload")
@guarded
def api_upload(file: UploadFile = File(...), u: Student = Depends(current_user)):
    """Save into the student's own folder and index it, tagged with their email.
    The file only replaces an earlier copy once indexing has succeeded."""
    name = _safe_name(file.filename)
    if not name:
        return _err(400, "That file name isn't usable. Rename the file and try again.",
                    indexed=False)
    if Path(name).suffix.lower() not in SUPPORTED:
        exts = ", ".join(sorted(e.lstrip(".").upper() for e in SUPPORTED))
        return {"ok": False, "name": name, "indexed": False,
                "error": f"That file type isn't supported. Upload one of: {exts}."}
    d = _user_dir(u.email)
    tmp = d / f".tmp-{uuid.uuid4().hex}{Path(name).suffix.lower()}"
    limit = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        with open(tmp, "wb") as f:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"That file is larger than {MAX_UPLOAD_MB} MB.")
                f.write(chunk)
        res = ingest_file(tmp, user=u.email, doc=name)
        if not res.get("ok"):
            return {"ok": False, "name": name, "indexed": False,
                    "error": res.get("error", "Indexing failed.")}
        os.replace(tmp, d / name)
        return {"ok": True, "name": name, "indexed": True, "chunks": res["chunks"],
                "ocr": res.get("ocr", 0), "warnings": res.get("warnings", [])}
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/material/delete")
@guarded
def api_material_delete(r: DeleteMaterialReq, u: Student = Depends(current_user)):
    """Remove one of the student's documents: its chunks and the file."""
    name = _safe_name(r.name)
    p = _user_dir(u.email) / name if name else None
    if not p or not p.is_file():
        return _err(404, "Not found.")
    delete_doc(u.email, name)
    p.unlink(missing_ok=True)
    return {"ok": True, "name": name}
