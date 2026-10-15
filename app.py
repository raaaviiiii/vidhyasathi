"""Vidhyasathi web backend — wraps the RAG engine and serves the UI.

Run (project root, venv active, Qdrant + Ollama running):
    uvicorn app:app --host 127.0.0.1 --port 8000
Then open http://localhost:8000

Security model (the app is shared over a public tunnel):
  - Sign-up / sign-in return a session token. Every data endpoint requires
    `Authorization: Bearer <token>`; the server derives the student from it and
    IGNORES any `user` field a client sends.
  - Each student's files live in private/uploads/<hash>/ and their chunks are tagged
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
import unicodedata
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
from src.doubts import route as route_doubt, index_helper_answers, HELPER_DOC
from src.errors import ServiceDown, StoreUnavailable
from src.exam import generate, MAX_N as EXAM_MAX_N
from src.grade import grade
from src.ingest import SUPPORTED, source_label
from src.store import delete_doc, delete_legacy_source, delete_user, user_index, count_version

from src import config as _config
from src.layout import migrate as _migrate_layout

BASE = Path(__file__).parent
WEB_DIR = _config.WEB_DIR            # public: the page itself
DATA_DIR = _config.PRIVATE_DIR       # private: accounts, uploads, knowledge base, logs
USERS_DB = _config.USERS_DB
_migrate_layout()                    # move files left over from the old flat layout into private/
SESSION_DAYS = 30
MAX_UPLOAD_MB = int(os.environ.get("VS_MAX_UPLOAD_MB", "50"))
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("VS_ADMIN_EMAILS", "").split(",")
                if e.strip()}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title="Vidhyasathi")


async def _send_json(send, status: int, payload: dict):
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class JsonErrors:
    """Outermost safety net: anything that escapes as an exception (a crash in a
    dependency, a locked database) still reaches the student as a JSON error."""
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        started = False

        async def send_wrapper(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)
        try:
            await self.inner(scope, receive, send_wrapper)
        except Exception as e:
            if started:
                raise
            if isinstance(e, sqlite3.OperationalError) or _is_connection_error(e):
                await _send_json(send, 503, {"ok": False, "error":
                                 "The server is busy. Please try again in a moment."})
            else:
                print(f"[vidhyasathi] unhandled error on {scope.get('path')}: {e!r}")
                await _send_json(send, 500, {"ok": False, "error":
                                 "Something went wrong on the server. Please try again."})


class UploadGate:
    """Refuse an upload BEFORE reading its body: no session -> 401, declared size over
    the limit -> 413. Otherwise anyone with the share link could fill the disk."""
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") == "POST" \
                and scope.get("path") == "/api/upload":
            hdrs = {k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in scope.get("headers", [])}
            if _session_for(hdrs.get("authorization", "")) is None:
                return await _send_json(send, 401, {"ok": False, "error": "Please sign in."})
            cl = hdrs.get("content-length")
            if cl is None or not cl.isdigit():
                return await _send_json(send, 411, {"ok": False, "error":
                                        "Upload size unknown. Please try again."})
            if int(cl) > MAX_UPLOAD_MB * 1024 * 1024 + 64 * 1024:
                return await _send_json(send, 413, {"ok": False, "indexed": False, "error":
                                        f"That file is larger than {MAX_UPLOAD_MB} MB."})
        return await self.inner(scope, receive, send)


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
app.add_middleware(UploadGate)
app.add_middleware(JsonErrors)


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
    c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
              "email TEXT UNIQUE, pw TEXT, salt TEXT, created REAL)")
    _migrate_autoincrement(c)
    ucols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    if "role" not in ucols:            # phase 2: student | helper
        c.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'")
    if "expertise" not in ucols:       # subjects a helper can help with
        c.execute("ALTER TABLE users ADD COLUMN expertise TEXT NOT NULL DEFAULT ''")
    c.execute("CREATE TABLE IF NOT EXISTS doubts(id INTEGER PRIMARY KEY, student_id INTEGER NOT NULL, "
              "student_email TEXT NOT NULL, question TEXT NOT NULL, attempt TEXT, decision TEXT, "
              "sources TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'open', helper_id INTEGER, "
              "created REAL, answer TEXT, answered_by INTEGER, answered_at REAL, helpful INTEGER, "
              "answered_name TEXT, edited_at REAL)")
    dcols = [r[1] for r in c.execute("PRAGMA table_info(doubts)")]
    for col, typ in (("answered_name", "TEXT"), ("edited_at", "REAL")):
        if col not in dcols:
            c.execute(f"ALTER TABLE doubts ADD COLUMN {col} {typ}")
    # one row per question asked: WHEN and the DECISION only (never the question text),
    # so escalation and refusal rates can be tracked over time
    c.execute("CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, user_id INTEGER, "
              "ts REAL, decision TEXT)")
    cols = [r[1] for r in c.execute("PRAGMA table_info(sessions)")]
    if cols and "user_id" not in cols:          # sessions are disposable: rebuild
        c.execute("DROP TABLE sessions")
    # A session belongs to one account ROW (user_id), not to an email: if the account
    # is deleted and the email re-registered, old tokens no longer match anything.
    c.execute("CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, "
              "user_id INTEGER NOT NULL, email TEXT NOT NULL, created REAL, expires REAL)")
    return c


def _migrate_autoincrement(c):
    """Older databases created `users` without AUTOINCREMENT, so SQLite re-used the id of
    the most recently deleted account — and anything keyed by that id (sessions, doubts)
    would attach to the next person who signed up. Rebuild the table once, keeping every
    row, and start the id counter above every id ever referenced."""
    sql = (c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
           .fetchone() or [""])[0] or ""
    if "AUTOINCREMENT" in sql.upper():
        return
    cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    extra = "".join(f", {x}" for x in ("role", "expertise") if x in cols)
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("CREATE TABLE users_new(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                  "email TEXT UNIQUE, pw TEXT, salt TEXT, created REAL"
                  + (", role TEXT NOT NULL DEFAULT 'student'" if "role" in cols else "")
                  + (", expertise TEXT NOT NULL DEFAULT ''" if "expertise" in cols else "") + ")")
        c.execute(f"INSERT INTO users_new(id,name,email,pw,salt,created{extra}) "
                  f"SELECT id,name,email,pw,salt,created{extra} FROM users")
        c.execute("DROP TABLE users")
        c.execute("ALTER TABLE users_new RENAME TO users")
        hi = c.execute("SELECT MAX(id) FROM users").fetchone()[0] or 0
        for t, col in (("sessions", "user_id"), ("doubts", "student_id"), ("doubts", "helper_id"),
                       ("doubts", "answered_by"), ("usage", "user_id")):
            try:
                hi = max(hi, c.execute(f"SELECT MAX({col}) FROM {t}").fetchone()[0] or 0)
            except sqlite3.OperationalError:
                pass
        c.execute("DELETE FROM sqlite_sequence WHERE name='users'")
        c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES('users', ?)", (hi,))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_session(email: str) -> str | None:
    """Mint a session for the account that currently owns `email`. None if that
    account no longer exists (e.g. deleted while the sign-in was in flight)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with closing(_db()) as c:
        cur = c.execute("INSERT INTO sessions(token_hash,user_id,email,created,expires) "
                        "SELECT ?, id, email, ?, ? FROM users WHERE email=?",
                        (_token_hash(token), now, now + SESSION_DAYS * 86400, email))
        c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        c.commit()
    return token if cur.rowcount == 1 else None


class Student(BaseModel):
    id: int
    email: str
    name: str
    token_hash: str
    role: str = "student"

    @property
    def is_admin(self) -> bool:
        return self.email in ADMIN_EMAILS

    @property
    def is_helper(self) -> bool:
        return self.role == "helper" or self.is_admin


def _session_for(authorization: str) -> Student | None:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not token:
        return None
    th = _token_hash(token)
    with closing(_db()) as c:
        row = c.execute("SELECT u.id, u.email, u.name, u.role FROM sessions s JOIN users u "
                        "ON u.id = s.user_id WHERE s.token_hash=? AND s.expires>?",
                        (th, time.time())).fetchone()
    return Student(id=row[0], email=row[1], name=row[2], token_hash=th,
                   role=row[3] or "student") if row else None


def current_user(authorization: str = Header(default="")) -> Student:
    if not authorization.lower().startswith("bearer ") or not authorization[7:].strip():
        raise HTTPException(401, "Please sign in.")
    u = _session_for(authorization)
    if u is None:
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    return u


def _account_exists(user_id: int) -> bool:
    with closing(_db()) as c:
        return c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is not None


_fail_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
MAX_FAILS, FAIL_WINDOW = 10, 15 * 60


def _client_ip(request: Request) -> str:
    """The real client behind the Cloudflare tunnel (which connects from localhost)."""
    h = request.headers
    ip = h.get("cf-connecting-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip()
    return ip or (request.client.host if request.client else "?")


def _prune_failures(now):
    if len(_failures) > 200:
        for k in [k for k, v in _failures.items() if not v or now - v[-1] >= FAIL_WINDOW]:
            _failures.pop(k, None)


def _too_many_failures(key: str) -> bool:
    now = time.time()
    with _fail_lock:
        _prune_failures(now)
        recent = [t for t in _failures.get(key, []) if now - t < FAIL_WINDOW]
        if recent:
            _failures[key] = recent
        else:
            _failures.pop(key, None)
        return len(recent) >= MAX_FAILS


def _record_failure(key: str):
    with _fail_lock:
        _failures.setdefault(key, []).append(time.time())
        _prune_failures(time.time())


def _clear_failures(key: str):
    with _fail_lock:
        _failures.pop(key, None)


_doc_locks: dict[tuple, threading.RLock] = {}
_doc_locks_guard = threading.Lock()


def _doc_lock(email: str, name: str) -> threading.RLock:
    """One upload at a time per (student, file), so two tabs uploading the same file
    always end with exactly one consistent version."""
    key = (email, name.lower())
    with _doc_locks_guard:
        return _doc_locks.setdefault(key, threading.RLock())


def _user_dir(email: str) -> Path:
    """Per-student upload folder, keyed by a short hash of the email."""
    tag = hashlib.sha256(email.encode()).hexdigest()[:16]
    d = DATA_DIR / "uploads" / tag
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

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = " ".join((v or "").split())     # tabs / no-break / ideographic spaces -> one space
        if not v:
            raise ValueError("must not be empty")
        return v

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
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


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
        from src.config import LLM_MODEL
        listed = ollama.list()
        models = listed.get("models", []) if isinstance(listed, dict) else getattr(listed, "models", [])
        names = set()
        for m in models:
            for k in ("model", "name"):
                v = m.get(k) if isinstance(m, dict) else getattr(m, k, None)
                if v:
                    names.add(v)
                    names.add(v.removesuffix(":latest"))
        out["llm"] = LLM_MODEL in names or LLM_MODEL.removesuffix(":latest") in names
    except Exception:
        pass
    out["ok"] = out["qdrant"] and out["llm"]
    return out


# ====================================================================== accounts
@app.post("/api/signup")
@guarded
def signup(r: SignupReq):
    salt = secrets.token_hex(8)
    with closing(_db()) as c:
        try:
            c.execute("INSERT INTO users(name,email,pw,salt,created) VALUES(?,?,?,?,?)",
                      (r.name, r.email, _hash(r.password, salt), salt, time.time()))
            c.commit()
        except sqlite3.IntegrityError:
            return _err(409, "An account with that email already exists.")
    token = _new_session(r.email)
    if not token:
        return _err(500, "Couldn't create your session. Please sign in.")
    return {"ok": True, "name": r.name, "email": r.email, "token": token}


_DUMMY_SALT = secrets.token_hex(8)


@app.post("/api/login")
@guarded
def login(r: LoginReq, request: Request):
    email = r.email.strip().lower()
    # keyed by email AND client: a stranger guessing a classmate's password can't
    # lock the classmate out of their own device
    key = f"login|{email}|{_client_ip(request)}"
    if _too_many_failures(key):
        return _err(429, "Too many wrong attempts. Wait 15 minutes and try again.")
    with closing(_db()) as c:
        row = c.execute("SELECT name,pw,salt FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        _hash(r.password, _DUMMY_SALT)          # same work either way (no enumeration)
        _record_failure(key)
        return _err(401, "Wrong email or password.")
    if _hash(r.password, row[2]) != row[1]:
        _record_failure(key)
        return _err(401, "Wrong email or password.")
    token = _new_session(email)
    if not token:
        return _err(401, "Wrong email or password.")
    _clear_failures(key)
    return {"ok": True, "name": row[0], "email": email, "token": token}


@app.get("/api/me")
@guarded
def me(u: Student = Depends(current_user)):
    return {"ok": True, "name": u.name, "email": u.email, "role": u.role,
            "is_helper": u.is_helper, "is_admin": u.is_admin}


@app.post("/api/logout")
@guarded
def logout(u: Student = Depends(current_user)):
    with closing(_db()) as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (u.token_hash,))
        c.commit()
    return {"ok": True}


@app.post("/api/account/password")
@guarded
def change_password(r: PasswordReq, u: Student = Depends(current_user)):
    key = f"pwchange|{u.id}"
    if _too_many_failures(key):
        return _err(429, "Too many wrong attempts. Wait 15 minutes and try again.")
    with closing(_db()) as c:
        row = c.execute("SELECT pw,salt FROM users WHERE id=?", (u.id,)).fetchone()
        if not row or _hash(r.old_password, row[1]) != row[0]:
            _record_failure(key)
            return _err(403, "Current password is wrong.")
        salt = secrets.token_hex(8)
        c.execute("UPDATE users SET pw=?, salt=? WHERE id=?",
                  (_hash(r.new_password, salt), salt, u.id))
        # sign out every OTHER device; keep this one
        c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (u.id, u.token_hash))
        c.commit()
    _clear_failures(key)
    return {"ok": True}


@app.post("/api/account/delete")
@guarded
def delete_account(r: DeleteAccountReq, u: Student = Depends(current_user)):
    """Verify password, then remove the account, its sessions, chunks and files."""
    with closing(_db()) as c:
        row = c.execute("SELECT pw,salt FROM users WHERE id=?", (u.id,)).fetchone()
        if not row or _hash(r.password, row[1]) != row[0]:
            return _err(403, "Wrong password.")
    # (a helper answer landing mid-deletion is caught by answer_doubt's re-check)
    if True:
        delete_user(u.email)                   # chunks first: if Qdrant is down, keep the account
        shutil.rmtree(_user_dir(u.email), ignore_errors=True)
        with closing(_db()) as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (u.id,))
            c.execute("DELETE FROM doubts WHERE student_id=?", (u.id,))
            # as a helper: open doubts go back to the pool; answered ones keep the
            # snapshot name but lose the link to this (now unused) id
            c.execute("UPDATE doubts SET helper_id=NULL WHERE helper_id=?", (u.id,))
            c.execute("UPDATE doubts SET answered_by=NULL WHERE answered_by=?", (u.id,))
            c.execute("DELETE FROM usage WHERE user_id=?", (u.id,))
            c.execute("DELETE FROM users WHERE id=?", (u.id,))
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
    try:
        with closing(_db()) as c:
            c.execute("INSERT INTO usage(user_id,ts,decision) VALUES(?,?,?)",
                      (u.id, time.time(), res.get("decision")))
            c.commit()
    except sqlite3.Error:
        pass                                    # stats are best-effort, answers are not
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
_KEEP_FORMAT = {"\u200c", "\u200d"}     # ZWNJ / ZWJ are real letters' glue in Malayalam


def _clean_once(name: str) -> str:
    name = unicodedata.normalize("NFC", name)
    name = name.replace("\\", "/").split("/")[-1]
    # drop control and invisible/bidi format characters ("lab\u202efdp.md" spoofing)
    name = "".join(ch for ch in name
                   if ch in _KEEP_FORMAT or unicodedata.category(ch)[0] != "C")
    return name.strip().strip(".").strip()


def _safe_name(filename: str | None) -> str | None:
    """Reduce a client file name to a plain, safe base name: no folders, no traversal,
    no hidden/temp names, no invisible characters, at most 150 bytes. Idempotent, so
    the name the list shows is exactly the name a delete will find."""
    name, prev = filename or "", None
    while name != prev:
        prev, name = name, _clean_once(name)
    if not name or name in (".", ".."):
        return None
    p = Path(name)
    suffix = p.suffix if 1 < len(p.suffix) <= 10 else ""
    stem = name[: len(name) - len(suffix)] if suffix else name
    while len((stem + suffix).encode("utf-8")) > 150 and stem:
        stem = stem[:-1]
    stem = stem.rstrip(" .")
    return (stem + suffix) if stem else None


def _existing_name(folder: Path, name: str) -> str:
    """Case-insensitive file systems (macOS) treat Notes.txt and notes.txt as one
    file: reuse the stored spelling so the file and its chunks stay in step."""
    low = name.lower()
    for p in folder.iterdir():
        if p.is_file() and not p.name.startswith(".") and p.name.lower() == low:
            return p.name
    return name


@app.get("/api/material")
@guarded
def api_material(u: Student = Depends(current_user)):
    """Only THIS student's files, each flagged indexed / not indexed."""
    d = _user_dir(u.email)
    docs, legacy = user_index(u.email)
    out = []
    for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and not p.name.startswith("."):
            live = p.name in docs or source_label(p) in legacy
            out.append({"name": p.name, "size": p.stat().st_size, "indexed": live})
    return {"ok": True, "docs": out}


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
        with _doc_lock(u.email, name):
            name = _existing_name(d, name)
            res = ingest_file(tmp, user=u.email, doc=name)
            if not res.get("ok"):
                return {"ok": False, "name": name, "indexed": False,
                        "error": res.get("error", "Indexing failed.")}
            if not _account_exists(u.id):          # account deleted mid-upload
                delete_doc(u.email, name)
                return _err(401, "Your account no longer exists.", indexed=False)
            removed = {"ok": False, "name": name, "indexed": False,
                       "error": "The file was removed while it was being indexed."}
            if count_version(u.email, name, res["ver"]) == 0:   # removed meanwhile
                return removed
            try:
                os.replace(tmp, d / name)
            except OSError:
                delete_doc(u.email, name)
                return {"ok": False, "name": name, "indexed": False,
                        "error": "Couldn't save that file name on the server. Rename it and try again."}
            if count_version(u.email, name, res["ver"]) == 0:   # removed during the move
                (d / name).unlink(missing_ok=True)
                return removed
        return {"ok": True, "name": name, "indexed": True, "chunks": res["chunks"],
                "ocr": res.get("ocr", 0), "warnings": res.get("warnings", [])}
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/material/delete")
@guarded
def api_material_delete(r: DeleteMaterialReq, u: Student = Depends(current_user)):
    """Remove one of the student's documents: its chunks and the file."""
    name = _safe_name(r.name)
    d = _user_dir(u.email)
    if not name:
        return _err(404, "Not found.")
    # No lock here: a delete is idempotent, and an upload of the same file re-checks
    # its chunks after moving the file into place, so the two always end consistent.
    p = d / _existing_name(d, name)
    if not p.is_file():
        return _err(404, "Not found.")
    p.unlink(missing_ok=True)
    delete_doc(u.email, p.name)
    delete_legacy_source(u.email, source_label(p))
    return {"ok": True, "name": p.name}


# ====================================================================== ask for help
MAX_OPEN_DOUBTS = 20


class DoubtReq(BaseModel):
    question: str = Field(max_length=4000)
    attempt: str = Field("", max_length=8000)       # what the assistant said
    decision: str = Field("", max_length=40)
    sources: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field("", max_length=2000)          # anything the student adds
    helper_id: int | None = Field(None, ge=1, le=2 ** 63 - 1)   # chosen helper (optional)
    _q = field_validator("question")(_strip_nonempty)

    @field_validator("sources")
    @classmethod
    def _src(cls, v):
        return [str(x)[:200] for x in v if str(x).strip()]


class RoutePreviewReq(BaseModel):
    question: str = Field(max_length=4000)
    sources: list[str] = Field(default_factory=list, max_length=20)
    _q = field_validator("question")(_strip_nonempty)

    @field_validator("sources")
    @classmethod
    def _src(cls, v):
        return [str(x)[:200] for x in v if str(x).strip()]


class DoubtAnswerReq(BaseModel):
    answer: str = Field(max_length=8000)
    _a = field_validator("answer")(_strip_nonempty)


class FeedbackReq(BaseModel):
    helpful: bool


class HelperReq(BaseModel):
    email: str = Field(max_length=254)
    helper: bool = True
    expertise: str = Field("", max_length=500)


class ExpertiseReq(BaseModel):
    expertise: str = Field(max_length=500)


def _first(name: str) -> str:
    parts = (name or "").split()            # every kind of Unicode whitespace
    return parts[0] if parts else "A student"


def _doubt_view(row: dict, viewer: Student) -> dict:
    """What one person may see of a doubt. Students see their own doubts in full;
    helpers see the student's first name only, never their email."""
    out = {k: row[k] for k in ("id", "question", "attempt", "decision", "note", "status",
                               "created", "answer", "answered_at", "helpful")}
    out["sources"] = json.loads(row["sources"] or "[]")
    out["helper_name"] = row.get("helper_name") or row.get("answered_name") or (
        "a helper (account removed)" if row.get("status") == "answered" else None)
    out["helper_subjects"] = (row.get("helper_subjects") or "") if row.get("helper_id") else ""
    out["mine"] = row["student_id"] == viewer.id
    if not out["mine"]:
        out["student_name"] = _first(row.get("student_name"))
    return out


_DOUBT_SELECT = ("SELECT d.*, h.name AS helper_name, h.expertise AS helper_subjects, "
                 "s.name AS student_name FROM doubts d "
                 "LEFT JOIN users h ON h.id = d.helper_id LEFT JOIN users s ON s.id = d.student_id ")


def _rows(c, sql, args=()):
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(sql, args).fetchall()]


def _helpers_for(c, student_id: int) -> list[dict]:
    """Everyone a student could send a doubt to (never themselves)."""
    return _rows(c, "SELECT u.id, u.name, u.email, u.expertise, "
                    "(SELECT COUNT(*) FROM doubts d WHERE d.helper_id=u.id AND d.status='open') "
                    "AS open_count FROM users u WHERE u.role='helper' AND u.id<>? ORDER BY u.name",
                 (student_id,))


def _helper_card(h: dict | None) -> dict | None:
    """What a student may see of a helper: name and subjects, never the email."""
    if not h:
        return None
    return {"id": h["id"], "name": h["name"], "subjects": (h.get("expertise") or "").strip(),
            "open": h.get("open_count", 0)}


@app.post("/api/doubts/route")
@guarded
def preview_route(r: RoutePreviewReq, u: Student = Depends(current_user)):
    """Who a doubt WOULD go to, before it is sent: the best-matched helper plus everyone
    else the student may choose instead. A doubt always goes to exactly one person."""
    with closing(_db()) as c:
        helpers = _helpers_for(c, u.id)
    best = route_doubt(r.question, r.sources, helpers)
    others = [h for h in helpers if not best or h["id"] != best["id"]]
    return {"ok": True, "suggested": _helper_card(best),
            "helpers": [_helper_card(h) for h in ([best] if best else []) + others],
            "fallback": None if helpers else "the app owner"}


@app.post("/api/doubts")
@guarded
def ask_helper(r: DoubtReq, u: Student = Depends(current_user)):
    """Send a doubt to ONE helper: the one the student picked, or the best match.
    With no helpers at all it waits for the app owner."""
    with closing(_db()) as c:
        open_n = c.execute("SELECT COUNT(*) FROM doubts WHERE student_id=? AND status='open'",
                           (u.id,)).fetchone()[0]
        if open_n >= MAX_OPEN_DOUBTS:
            return _err(429, f"You already have {MAX_OPEN_DOUBTS} open doubts. "
                             "Wait for replies or withdraw some.")
        helpers = _helpers_for(c, u.id)
    if r.helper_id is not None:
        helper = next((h for h in helpers if h["id"] == r.helper_id), None)
        if not helper:
            return _err(400, "That person isn't a helper you can send doubts to.")
    else:
        helper = route_doubt(r.question, r.sources, helpers)
    with closing(_db()) as c:
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")               # count + insert can't interleave
        cur = c.execute("INSERT INTO doubts(student_id,student_email,question,attempt,decision,"
                        "sources,note,status,helper_id,created) "
                        "SELECT ?,?,?,?,?,?,?,'open',?,? WHERE (SELECT COUNT(*) FROM doubts "
                        "WHERE student_id=? AND status='open') < ?",
                        (u.id, u.email, r.question, r.attempt, r.decision,
                         json.dumps(r.sources), r.note.strip(), helper["id"] if helper else None,
                         time.time(), u.id, MAX_OPEN_DOUBTS))
        c.execute("COMMIT")
        if cur.rowcount != 1:
            return _err(429, f"You already have {MAX_OPEN_DOUBTS} open doubts. "
                             "Wait for replies or withdraw some.")
        row = _rows(c, _DOUBT_SELECT + "WHERE d.id=?", (cur.lastrowid,))[0]
    return {"ok": True, "doubt": _doubt_view(row, u),
            "routed_to": helper["name"] if helper else None,
            "routed_subjects": (helper.get("expertise") or "") if helper else "",
            "fallback": None if helper else "the app owner"}


@app.get("/api/doubts")
@guarded
def list_doubts(u: Student = Depends(current_user)):
    """The student's own doubts, plus (for helpers) the ones assigned to them and
    (for the admin) any that nobody could be matched to."""
    with closing(_db()) as c:
        mine = _rows(c, _DOUBT_SELECT + "WHERE d.student_id=? ORDER BY d.created DESC", (u.id,))
        inbox = []
        if u.is_helper:
            sql = _DOUBT_SELECT + "WHERE d.student_id<>? AND (d.helper_id=?"
            if u.is_admin:
                sql += " OR d.helper_id IS NULL"
            sql += ") AND d.status<>'closed' ORDER BY (d.status='open') DESC, d.created DESC"
            inbox = _rows(c, sql, (u.id, u.id))
        expertise = c.execute("SELECT expertise FROM users WHERE id=?", (u.id,)).fetchone()
    return {"ok": True, "role": u.role, "is_helper": u.is_helper, "is_admin": u.is_admin,
            "expertise": (expertise[0] if expertise else "") if u.role == "helper" else "",
            "mine": [_doubt_view(d, u) for d in mine],
            "inbox": [_doubt_view(d, u) for d in inbox]}


def _load_doubt(c, doubt_id: int) -> dict | None:
    if not (1 <= doubt_id <= 2 ** 63 - 1):
        return None
    rows = _rows(c, _DOUBT_SELECT + "WHERE d.id=?", (doubt_id,))
    return rows[0] if rows else None


@app.post("/api/doubts/{doubt_id}/answer")
@guarded
def answer_doubt(doubt_id: int, r: DoubtAnswerReq, u: Student = Depends(current_user)):
    """A helper answers (or edits their answer). The answer is fed back into the
    student's own knowledge base."""
    with closing(_db()) as c:
        d = _load_doubt(c, doubt_id)
    if not d or d["status"] == "closed":
        return _err(404, "That doubt doesn't exist or was withdrawn.")
    allowed = u.is_admin or (u.role == "helper" and d["helper_id"] == u.id and
                             (d["status"] == "open" or d["answered_by"] == u.id))
    if not allowed or d["student_id"] == u.id:
        return _err(403, "This doubt isn't assigned to you.")
    with _doc_lock(d["student_email"], HELPER_DOC):     # one rebuild at a time per student
        if not _account_exists(d["student_id"]):
            return _err(404, "That student's account no longer exists.")
        now = time.time()
        with closing(_db()) as c:
            c.execute("UPDATE doubts SET answer=?, status='answered', answered_by=?, answered_name=?, "
                      "answered_at=COALESCE(answered_at, ?), edited_at=CASE WHEN answered_at IS NULL "
                      "THEN NULL ELSE ? END, helper_id=COALESCE(helper_id, ?) WHERE id=?",
                      (r.answer.strip(), u.id, u.name, now, now, u.id, doubt_id))
            c.commit()
            answered = _rows(c, "SELECT id, question, answer, answered_name AS helper_name FROM doubts "
                                "WHERE student_id=? AND status='answered'", (d["student_id"],))
        index_helper_answers(d["student_email"], answered)
        if not _account_exists(d["student_id"]):        # deleted while we were indexing
            delete_doc(d["student_email"], HELPER_DOC)
            return _err(404, "That student's account no longer exists.")
    return {"ok": True}


@app.post("/api/doubts/{doubt_id}/feedback")
@guarded
def doubt_feedback(doubt_id: int, r: FeedbackReq, u: Student = Depends(current_user)):
    with closing(_db()) as c:
        d = _load_doubt(c, doubt_id)
        if not d or d["student_id"] != u.id:
            return _err(404, "Not found.")
        if d["status"] != "answered":
            return _err(400, "You can rate an answer once a helper has replied.")
        cur = c.execute("UPDATE doubts SET helpful=? WHERE id=? AND helpful IS NULL",
                        (1 if r.helpful else 0, doubt_id))
        c.commit()
        if cur.rowcount == 0:
            return _err(409, "You've already rated this answer.")
    return {"ok": True}


@app.post("/api/doubts/{doubt_id}/close")
@guarded
def withdraw_doubt(doubt_id: int, u: Student = Depends(current_user)):
    """The student withdraws an open doubt (answered ones stay as part of their notes)."""
    with closing(_db()) as c:
        d = _load_doubt(c, doubt_id)
        if not d or d["student_id"] != u.id:
            return _err(404, "Not found.")
        if d["status"] != "open":
            return _err(400, "Only an open doubt can be withdrawn.")
        c.execute("UPDATE doubts SET status='closed' WHERE id=?", (doubt_id,))
        c.commit()
    return {"ok": True}


@app.post("/api/me/expertise")
@guarded
def set_expertise(r: ExpertiseReq, u: Student = Depends(current_user)):
    """Helpers keep their own subject list up to date (it drives routing)."""
    if not u.is_helper:
        return _err(403, "Only helpers list subjects.")
    with closing(_db()) as c:
        c.execute("UPDATE users SET expertise=? WHERE id=?", (r.expertise.strip(), u.id))
        c.commit()
    return {"ok": True}


def _require_admin(u: Student):
    if not u.is_admin:
        raise HTTPException(403, "Only the app owner can do that.")


@app.get("/api/admin/helpers")
@guarded
def admin_helpers(u: Student = Depends(current_user)):
    _require_admin(u)
    with closing(_db()) as c:
        rows = _rows(c, "SELECT u.name, u.email, u.expertise, "
                        "(SELECT COUNT(*) FROM doubts d WHERE d.helper_id=u.id AND d.status='open') AS open, "
                        "(SELECT COUNT(*) FROM doubts d WHERE d.answered_by=u.id) AS answered "
                        "FROM users u WHERE u.role='helper' ORDER BY u.name")
    return {"ok": True, "helpers": rows}


@app.post("/api/admin/helpers")
@guarded
def admin_set_helper(r: HelperReq, u: Student = Depends(current_user)):
    """Make an existing account a helper (with subjects), or back into a student."""
    _require_admin(u)
    email = r.email.strip().lower()
    with closing(_db()) as c:
        row = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            return _err(404, "No account with that email. Ask them to sign up first.")
        c.execute("UPDATE users SET role=?, expertise=? WHERE id=?",
                  ("helper" if r.helper else "student", r.expertise.strip() if r.helper else "", row[0]))
        if not r.helper:            # their open doubts go back to the unassigned pool
            c.execute("UPDATE doubts SET helper_id=NULL WHERE helper_id=? AND status='open'", (row[0],))
        c.commit()
    return {"ok": True}


@app.get("/api/admin/stats")
@guarded
def admin_stats(u: Student = Depends(current_user)):
    """Escalation trend for the deck's claim: as helper answers feed the knowledge
    base, the share of questions that end in a doubt should fall week on week."""
    _require_admin(u)
    week = 7 * 86400
    now = time.time()
    with closing(_db()) as c:
        weeks = []
        for i in range(7, -1, -1):
            a, b = now - (i + 1) * week, now - i * week
            asked = c.execute("SELECT COUNT(*) FROM usage WHERE ts>=? AND ts<?", (a, b)).fetchone()[0]
            refused = c.execute("SELECT COUNT(*) FROM usage WHERE ts>=? AND ts<? AND decision='refuse'",
                                (a, b)).fetchone()[0]
            doubts = c.execute("SELECT COUNT(*) FROM doubts WHERE created>=? AND created<?", (a, b)).fetchone()[0]
            weeks.append({"week_start": a, "questions": asked, "refusals": refused, "doubts": doubts,
                          "escalation_rate": round(doubts / asked, 3) if asked else None})
        total = c.execute("SELECT COUNT(*), SUM(status='answered'), SUM(status='open'), "
                          "AVG(CASE WHEN answered_at IS NOT NULL THEN answered_at-created END), "
                          "SUM(helpful=1), SUM(helpful IS NOT NULL) FROM doubts").fetchone()
    n, answered, open_, avg_secs, helpful, rated = total
    return {"ok": True, "weeks": weeks, "doubts": n or 0, "answered": answered or 0,
            "open": open_ or 0,
            "avg_hours_to_answer": round(avg_secs / 3600, 1) if avg_secs else None,
            "helpful_rate": round(helpful / rated, 2) if rated else None}
