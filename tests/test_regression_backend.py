"""QA — REGRESSION + ADVERSARIAL round on the FIXED backend.

Focus: code added by the fix round that had no adversarial testing yet: session
tokens, login lockout, the temp-file upload path, store versioning, broad-ask topic
extraction, teach-mode rescue gating, exam/grade post-processing, error mapping and
concurrency.

Every test states the behaviour qa/SPEC.md asks for (or, for judgement calls, the
behaviour defended in qa/reports/regression_backend.md). A failing test is a bug.
No skip / xfail.

Run: QDRANT_URL=http://localhost:6333 python -m pytest tests/test_regression_backend.py -q
"""
import html
import re
import sqlite3
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import app as app_module
from src import config
from src.answer import answer, REFUSAL, _scrub_web_tags
from src.exam import generate
from src.grade import grade, _strip_marks
from src.store import count, user_docs, user_sources
from tests.samples import NUMBER_THEORY

PW = "passw0rd"
A = "a@x.com"
B = "b@x.com"

GROUNDED_DOC = (
    "Division Algorithm. The Division Algorithm states that for integers a and b "
    "there exist unique integers q and r with a = bq + r. Division Algorithm "
    "quotient remainder."
).encode()
OTHER_DOC = (
    "Photosynthesis converts sunlight water and carbon dioxide into glucose and oxygen "
    "using chlorophyll in the chloroplasts of green plants."
).encode()

NT_STRONG = "Division Algorithm unique integers q and r a = bq + r"
KRUSKAL = "Kruskal algorithm spanning tree"
_HIST = [{"role": "user", "content": NT_STRONG},
         {"role": "assistant", "content": "It says a = bq + r. [Number Theory · p1]"}]


# =========================================================================== helpers
@pytest.fixture(autouse=True)
def _fresh_lockout_table(monkeypatch):
    """The failure table is module-global; never let one test lock out another."""
    monkeypatch.setattr(app_module, "_failures", {})


def H(token):
    return {"Authorization": f"Bearer {token}"}


def signup(c, email, pw=PW, name="Student"):
    r = c.post("/api/signup", json={"name": name, "email": email, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def login(c, email, pw=PW, **kw):
    return c.post("/api/login", json={"email": email, "password": pw}, **kw)


def upload(c, token, name, content=GROUNDED_DOC, ctype="text/plain"):
    return c.post("/api/upload", files={"file": (name, content, ctype)}, headers=H(token))


def material(c, token):
    r = c.get("/api/material", headers=H(token))
    assert r.status_code == 200, r.text
    return {d["name"]: d for d in r.json()["docs"]}


def delete_material(c, token, name):
    return c.post("/api/material/delete", json={"name": name}, headers=H(token))


def ask(c, token, q, **kw):
    return c.post("/api/answer", json={"question": q, **kw}, headers=H(token))


def _say(kind_to_text):
    def r(kind, messages):
        v = kind_to_text.get(kind)
        return v(messages) if callable(v) else v
    return r


def _filler(n, tag="Lecture"):
    return [f"{tag}{i} covers subject{i} with detail{i} and example{i}" for i in range(n)]


# =========================================================================== SESSIONS
def test_signup_and_login_return_distinct_working_tokens(app_client):
    t1 = signup(app_client, A)
    t2 = login(app_client, A).json()["token"]
    assert t1 != t2
    for t in (t1, t2):
        r = app_client.get("/api/me", headers=H(t))
        assert r.status_code == 200 and r.json()["email"] == A


def test_logout_revokes_only_that_token(app_client):
    t1 = signup(app_client, A)
    t2 = login(app_client, A).json()["token"]
    assert app_client.post("/api/logout", headers=H(t1)).status_code == 200
    r = app_client.get("/api/me", headers=H(t1))
    assert r.status_code == 401 and r.json()["ok"] is False
    assert app_client.get("/api/material", headers=H(t1)).status_code == 401
    assert app_client.get("/api/me", headers=H(t2)).status_code == 200   # other device stays
    # logging out twice is a clean 401, not a crash
    assert app_client.post("/api/logout", headers=H(t1)).status_code == 401


def test_expired_session_is_rejected_and_purged(app_client):
    t = signup(app_client, A)
    with sqlite3.connect(app_module.USERS_DB) as c:
        c.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    r = app_client.get("/api/me", headers=H(t))
    assert r.status_code == 401 and "error" in r.json()
    login(app_client, A)                                    # creating a session purges stale ones
    with sqlite3.connect(app_module.USERS_DB) as c:
        assert c.execute("SELECT COUNT(*) FROM sessions WHERE expires < ?",
                         (time.time(),)).fetchone()[0] == 0


def test_change_password_signs_out_other_sessions_but_keeps_this_one(app_client):
    here = signup(app_client, A)
    other = login(app_client, A).json()["token"]
    bob = signup(app_client, B)
    r = app_client.post("/api/account/password", headers=H(here),
                        json={"old_password": PW, "new_password": "n3wpassword"})
    assert r.status_code == 200, r.text
    assert app_client.get("/api/me", headers=H(here)).status_code == 200
    assert app_client.get("/api/me", headers=H(other)).status_code == 401
    assert app_client.get("/api/me", headers=H(bob)).status_code == 200    # never another user
    assert login(app_client, A).status_code == 401
    assert login(app_client, A, pw="n3wpassword").status_code == 200


def test_account_delete_kills_every_session_and_reregistration_does_not_revive_them(app_client):
    t1 = signup(app_client, A)
    t2 = login(app_client, A).json()["token"]
    assert upload(app_client, t1, "nt.txt").json()["indexed"] is True
    r = app_client.post("/api/account/delete", json={"password": PW}, headers=H(t1))
    assert r.status_code == 200, r.text
    for t in (t1, t2):
        assert app_client.get("/api/me", headers=H(t)).status_code == 401
    # someone else registers the same address: the old tokens must still be dead,
    # and the newcomer must inherit nothing
    t3 = signup(app_client, A, pw="different1", name="Newcomer")
    for t in (t1, t2):
        assert app_client.get("/api/me", headers=H(t)).status_code == 401
        assert app_client.get("/api/material", headers=H(t)).status_code == 401
    assert material(app_client, t3) == {}
    assert count(A) == 0


@pytest.mark.parametrize("header", [
    "", "Bearer", "Bearer ", "Bearer    ", "Basic dXNlcjpwYXNz", "Token abc",
    "Bearerabc", "Bearer abc def", "Bearer " + "x" * 20000, "Bearer ' OR 1=1 --",
    "Bearer éè",
])
def test_malformed_authorization_header_is_clean_401(app_client, header):
    signup(app_client, A)
    hdrs = {"Authorization": header.encode("utf-8")} if header else {}
    for method, path in (("get", "/api/me"), ("get", "/api/material"), ("post", "/api/logout")):
        r = getattr(app_client, method)(path, headers=hdrs)
        assert r.status_code == 401, (header[:30], path, r.status_code)
        assert r.json()["ok"] is False


def test_stored_token_hash_is_not_itself_a_token(app_client):
    signup(app_client, A)
    with sqlite3.connect(app_module.USERS_DB) as c:
        th = c.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert app_client.get("/api/me", headers=H(th)).status_code == 401


def test_email_case_variants_are_one_identity_and_one_folder(app_client):
    t1 = signup(app_client, "  Asha@X.com ")
    r = app_client.post("/api/signup", json={"name": "Dup", "email": "asha@x.COM",
                                             "password": PW})
    assert r.status_code == 409
    t2 = login(app_client, "ASHA@x.com").json()["token"]
    assert app_client.get("/api/me", headers=H(t2)).json()["email"] == "asha@x.com"
    upload(app_client, t1, "one.txt")
    upload(app_client, t2, "two.txt", OTHER_DOC)
    assert set(material(app_client, t1)) == set(material(app_client, t2)) == {"one.txt", "two.txt"}
    folders = list((app_client.tmp / "data" / "uploads").iterdir())
    assert len(folders) == 1
    assert count("asha@x.com") > 0 and count("Asha@X.com") == 0


def test_login_racing_account_deletion_leaves_no_session_for_the_next_owner(app_client, monkeypatch):
    """Device 2 signs in (password verified) while device 1 deletes the account. The
    session device 2 then gets must not authenticate as whoever registers the email next."""
    t1 = signup(app_client, A)
    real_new_session = app_module._new_session
    state = {"done": False}

    def racing_new_session(email):
        if not state["done"]:
            state["done"] = True
            r = app_client.post("/api/account/delete", json={"password": PW}, headers=H(t1))
            assert r.status_code == 200, r.text
        return real_new_session(email)

    monkeypatch.setattr(app_module, "_new_session", racing_new_session)
    r = login(app_client, A)
    stale = r.json().get("token")
    monkeypatch.setattr(app_module, "_new_session", real_new_session)
    newcomer = signup(app_client, A, pw="newcomer1", name="Newcomer")
    upload(app_client, newcomer, "private.txt", OTHER_DOC)
    if stale:
        r = app_client.get("/api/material", headers=H(stale))
        assert r.status_code == 401, "a session minted for the deleted account opens the new one"


def test_upload_racing_account_deletion_leaves_no_material_for_the_next_owner(app_client, monkeypatch):
    """An upload already past auth when the account is deleted must not leave chunks
    tagged with the email, which a later registrant of that email would then query."""
    t1 = signup(app_client, A)
    import src.build_index as bi
    real_upsert = bi.upsert_chunks
    state = {"done": False}

    def racing_upsert(*a, **kw):
        if not state["done"]:
            state["done"] = True
            r = app_client.post("/api/account/delete", json={"password": PW}, headers=H(t1))
            assert r.status_code == 200, r.text
        return real_upsert(*a, **kw)

    monkeypatch.setattr(bi, "upsert_chunks", racing_upsert)
    r = upload(app_client, t1, "diary.txt")
    assert r.status_code != 200 or r.json().get("indexed") is not True
    monkeypatch.setattr(bi, "upsert_chunks", real_upsert)
    assert count(A) == 0, "chunks of a deleted account survived the deletion"
    newcomer = signup(app_client, A, pw="newcomer1", name="Newcomer")
    r = ask(app_client, newcomer, "State the Division Algorithm")
    assert r.json()["decision"] == "refuse"


# =========================================================================== LOCKOUT
def test_lockout_after_ten_failures_blocks_even_the_right_password(app_client):
    """Defended: once locked, a correct password from the SAME client is also refused,
    otherwise the lockout would not slow a guesser down at all."""
    signup(app_client, A)
    for _ in range(10):
        assert login(app_client, A, pw="wrong-pass").status_code == 401
    r = login(app_client, A)
    assert r.status_code == 429 and r.json()["ok"] is False and "error" in r.json()
    r = login(app_client, "  A@X.COM ", pw="wrong-pass")    # same key regardless of case
    assert r.status_code == 429


def _client_at(ip):
    return TestClient(app_module.app, client=(ip, 50000),
                      headers={"CF-Connecting-IP": ip, "X-Forwarded-For": ip})


def test_attacker_cannot_lock_a_student_out_from_another_client(app_client):
    """Anyone with the tunnel link can type a classmate's email ten times. That must not
    lock the classmate out of their own account from their own device."""
    signup(app_client, A)
    attacker, victim = _client_at("203.0.113.9"), _client_at("198.51.100.7")
    for _ in range(12):
        login(attacker, A, pw="guess-guess")
    r = login(victim, A)
    assert r.status_code == 200, f"victim locked out by a stranger: {r.status_code} {r.text}"


def test_successful_login_resets_the_failure_count(app_client):
    signup(app_client, A)
    for _ in range(9):
        login(app_client, A, pw="typo-typo")
    assert login(app_client, A).status_code == 200
    login(app_client, A, pw="typo-again")
    r = login(app_client, A)
    assert r.status_code == 200, "earlier typos still count after a successful sign-in"


def test_old_password_guessing_via_change_password_is_rate_limited(app_client):
    """A borrowed/stolen session must not get unlimited guesses at the real password
    (a correct guess lets it change the password and sign the owner out everywhere)."""
    t = signup(app_client, A)
    codes = [app_client.post("/api/account/password", headers=H(t),
                             json={"old_password": f"guess{i:04d}", "new_password": "hijacked1"}
                             ).status_code for i in range(15)]
    assert 429 in codes, codes


def test_failure_table_forgets_stale_entries(app_client):
    """Every unknown email adds a key; stale keys must be dropped, or a stream of random
    emails grows the table without bound."""
    now = time.time()
    for i in range(2000):
        app_module._failures[f"ghost{i}@x.com"] = [now - 3600]
    login(app_client, "someone@x.com", pw="whatever1")
    assert len(app_module._failures) < 100, len(app_module._failures)


# =========================================================================== UPLOAD
def _user_dir(email):
    return app_module._user_dir(email)


def test_oversized_upload_is_413_json_and_keeps_the_old_copy(app_client, monkeypatch):
    t = signup(app_client, A)
    assert upload(app_client, t, "nt.txt").json()["indexed"] is True
    before = count(A)
    monkeypatch.setattr(app_module, "MAX_UPLOAD_MB", 1)
    big = b"word " * (1024 * 1024 // 5 + 10)
    r = upload(app_client, t, "nt.txt", big)
    assert r.status_code == 413 and r.json()["ok"] is False and "MB" in r.json()["error"]
    assert (_user_dir(A) / "nt.txt").read_bytes() == GROUNDED_DOC
    assert count(A) == before
    assert [p.name for p in _user_dir(A).iterdir()] == ["nt.txt"]       # no temp left


def test_upload_exactly_at_the_limit_is_accepted(app_client, monkeypatch):
    t = signup(app_client, A)
    monkeypatch.setattr(app_module, "MAX_UPLOAD_MB", 1)
    body = (b"Division Algorithm quotient remainder " * 30000)[:1024 * 1024]
    r = upload(app_client, t, "exact.txt", body)
    assert r.status_code == 200 and r.json()["indexed"] is True, r.text


def test_reupload_that_fails_to_index_keeps_old_file_and_old_chunks(app_client):
    t = signup(app_client, A)
    assert upload(app_client, t, "nt.txt").json()["indexed"] is True
    before = count(A)
    r = upload(app_client, t, "nt.txt", b"   \n\t  ")
    assert r.json()["ok"] is False and r.json()["indexed"] is False
    assert (_user_dir(A) / "nt.txt").read_bytes() == GROUNDED_DOC
    assert count(A) == before
    assert material(app_client, t)["nt.txt"]["indexed"] is True
    assert ask(app_client, t, "State the Division Algorithm").json()["decision"] == "answer"


def test_reupload_while_qdrant_is_down_is_503_and_keeps_everything(app_client, monkeypatch):
    t = signup(app_client, A)
    assert upload(app_client, t, "nt.txt").json()["indexed"] is True
    before = count(A)
    import src.store as store
    from qdrant_client import QdrantClient
    real = store._client
    monkeypatch.setattr(store, "_client", QdrantClient(url="http://localhost:1"))
    r = upload(app_client, t, "nt.txt", OTHER_DOC)
    assert r.status_code == 503, r.text
    assert "Qdrant" in r.json()["error"]
    monkeypatch.setattr(store, "_client", real)
    assert (_user_dir(A) / "nt.txt").read_bytes() == GROUNDED_DOC
    assert count(A) == before
    assert [p.name for p in _user_dir(A).iterdir()] == ["nt.txt"]


def test_material_list_never_shows_temp_or_hidden_files(app_client):
    t = signup(app_client, A)
    upload(app_client, t, "nt.txt")
    d = _user_dir(A)
    (d / ".tmp-deadbeef.txt").write_text("half written")
    (d / ".DS_Store").write_text("x")
    (d / "subdir").mkdir()
    assert set(material(app_client, t)) == {"nt.txt"}


@pytest.mark.parametrize("sent, stored", [
    ("ഗണിതം കുറിപ്പുകൾ.txt", "ഗണിതം കുറിപ്പുകൾ.txt"),
    ("गणित नोट्स.md", "गणित नोट्स.md"),
    (".tmp-x.txt", "tmp-x.txt"),
    ("notes.txt.", "notes.txt"),
    ("a" * 300 + ".txt", "a" * 146 + ".txt"),
    (".  spaced.txt", None),            # whatever it is stored as, it must round-trip
])
def test_weird_names_round_trip_upload_list_delete(app_client, sent, stored):
    t = signup(app_client, A)
    r = upload(app_client, t, sent)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    body = r.json()
    assert body["indexed"] is True
    name = body["name"]
    if stored is not None:
        assert name == stored
    listing = material(app_client, t)
    assert name in listing and listing[name]["indexed"] is True
    r = delete_material(app_client, t, name)          # the UI sends the listed name back
    assert r.status_code == 200, (r.status_code, r.text)
    assert material(app_client, t) == {} and count(A) == 0


def test_deleting_a_space_prefixed_file_never_deletes_a_different_file(app_client):
    t = signup(app_client, A)
    # (Judgement, QA lead: ".  notes.txt" legitimately cleans to "notes.txt", i.e. the
    # SAME file. The real risk is a cleaner that isn't idempotent, so the listed name
    # of a space/dot-prefixed upload deletes some OTHER file. Test that.)
    assert upload(app_client, t, "notes.txt", GROUNDED_DOC).json()["indexed"]
    r = upload(app_client, t, ".  other.txt", OTHER_DOC).json()
    listing = material(app_client, t)
    assert "notes.txt" in listing and r["name"] in listing
    impostor = r["name"]
    delete_material(app_client, t, impostor)
    assert "notes.txt" in material(app_client, t), "deleting one file removed ANOTHER file"
    assert "notes.txt" in user_docs(A), "deleting one file removed ANOTHER file's chunks"


def test_overlong_unicode_name_never_leaves_orphan_chunks(app_client):
    """140 Malayalam letters = 420 UTF-8 bytes: within the 150-char cap, over the
    255-byte file-name limit of ext4/APFS/NTFS."""
    t = signup(app_client, A)
    r = upload(app_client, t, "ഗ" * 140 + ".txt")
    if r.status_code == 200 and r.json().get("indexed"):
        assert r.json()["name"] in material(app_client, t)
    else:
        assert count(A) == 0, "upload failed but its chunks are live (invisible, undeletable)"
        assert r.status_code == 400, r.status_code


def test_bidi_and_invisible_characters_are_stripped_from_names(app_client):
    """'invoice\\u202Efdp.md' shows as 'invoicedm.pdf'; 'notes\\u200b.txt' looks identical
    to 'notes.txt'. File names in the list must show what they really are."""
    t = signup(app_client, A)
    a = upload(app_client, t, "lab‮fdp.md").json()["name"]
    b = upload(app_client, t, "notes​.txt").json()["name"]
    for n in (a, b):
        assert not re.search(r"[​-‏‪-‮⁦-⁩﻿]", n), repr(n)


def test_names_differing_only_by_case_stay_consistent(app_client):
    # (Judgement, QA lead: on macOS — where this app runs — Notes.txt and notes.txt
    # are the SAME file on disk. They must therefore be one document whose chunks
    # match the file, not two documents sharing one file.)
    t = signup(app_client, A)
    upload(app_client, t, "Notes.txt", GROUNDED_DOC)
    upload(app_client, t, "notes.txt", OTHER_DOC)
    assert set(material(app_client, t)) == {"Notes.txt"}
    assert user_docs(A) == {"Notes.txt"}
    pts, _ = app_client.fakes.client.scroll(config.COLLECTION_NAME, limit=100, with_payload=True)
    assert {p.payload["text"] for p in pts if p.payload.get("user") == A} == {OTHER_DOC.decode()}
    assert delete_material(app_client, t, "notes.txt").status_code == 200
    assert material(app_client, t) == {} and user_docs(A) == set()


@pytest.mark.parametrize("bad", ["....", "/", "..\\..\\", "   "])
def test_unusable_names_are_400(app_client, bad):
    t = signup(app_client, A)
    r = upload(app_client, t, bad)
    assert r.status_code in (400, 422), r.status_code
    assert list(_user_dir(A).iterdir()) == []


def _raw_upload(declared_mb, sent_mb, token=None):
    """Drive the ASGI app directly and count how many body bytes it pulls in before
    answering (TestClient hides this)."""
    import asyncio
    boundary = "vsboundary"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"big.txt\"\r\nContent-Type: text/plain\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    total = int(sent_mb * 1024 * 1024)
    declared = int(declared_mb * 1024 * 1024) + len(head) + len(tail)
    piece = 256 * 1024
    state = {"sent": 0, "status": None, "head_done": False, "tail_done": False}
    headers = [(b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
               (b"content-length", str(declared).encode())]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/api/upload", "raw_path": b"/api/upload",
             "query_string": b"", "root_path": "", "headers": headers,
             "client": ("203.0.113.1", 1), "server": ("testserver", 80)}

    async def receive():
        if not state["head_done"]:
            state["head_done"] = True
            return {"type": "http.request", "body": head, "more_body": True}
        if state["sent"] < total:
            n = min(piece, total - state["sent"])
            state["sent"] += n
            return {"type": "http.request", "body": b"a" * n, "more_body": True}
        state["tail_done"] = True
        return {"type": "http.request", "body": tail, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            state["status"] = msg["status"]

    asyncio.run(app_module.app(scope, receive, send))
    return state["status"], state["sent"]


def test_unauthenticated_upload_is_refused_before_the_body_is_read(app_client, monkeypatch):
    """Over the public tunnel, anyone can POST a huge body to /api/upload. The server
    must not spool it to disk before checking who is asking."""
    monkeypatch.setattr(app_module, "MAX_UPLOAD_MB", 1)
    status, read = _raw_upload(declared_mb=20, sent_mb=20)
    assert status == 401
    assert read <= 2 * 1024 * 1024, f"read {read / 1e6:.1f} MB from a stranger before saying 401"


def test_oversized_upload_is_refused_before_the_whole_body_is_spooled(app_client, monkeypatch):
    t = signup(app_client, A)
    monkeypatch.setattr(app_module, "MAX_UPLOAD_MB", 1)
    status, read = _raw_upload(declared_mb=20, sent_mb=20, token=t)
    assert status == 413
    assert read <= 2 * 1024 * 1024, f"spooled {read / 1e6:.1f} MB before enforcing a 1 MB limit"


def test_material_list_with_qdrant_down_is_503(app_client, monkeypatch):
    t = signup(app_client, A)
    upload(app_client, t, "nt.txt")
    import src.store as store
    from qdrant_client import QdrantClient
    monkeypatch.setattr(store, "_client", QdrantClient(url="http://localhost:1"))
    r = app_client.get("/api/material", headers=H(t))
    assert r.status_code == 503 and "Qdrant" in r.json()["error"]


# =========================================================================== CONCURRENCY
def test_delete_during_reupload_leaves_a_consistent_state(app_client, monkeypatch):
    """The student re-uploads notes.txt and, while it indexes, removes notes.txt in
    another tab. Either outcome is fine (gone, or the new copy indexed), but the upload
    must not report indexed:true for a file that ends up listed as 'not indexed'."""
    t = signup(app_client, A)
    upload(app_client, t, "notes.txt", GROUNDED_DOC)
    import src.build_index as bi
    real = bi._drop_other_versions
    state = {"done": False}

    def delete_meanwhile(*a, **kw):
        if not state["done"]:
            state["done"] = True
            assert delete_material(app_client, t, "notes.txt").status_code == 200
        return real(*a, **kw)
    monkeypatch.setattr(bi, "_drop_other_versions", delete_meanwhile)
    r = upload(app_client, t, "notes.txt", OTHER_DOC).json()
    listing = material(app_client, t)
    if "notes.txt" in listing:
        assert listing["notes.txt"]["indexed"] is True, \
            f"upload said indexed={r.get('indexed')} but the file is listed as not indexed"
    else:
        assert count(A) == 0 and r.get("indexed") is not True



def test_two_simultaneous_uploads_of_the_same_name_keep_exactly_one_version(app_client, monkeypatch):
    """Both requests write their version, then each retires 'every other version' —
    i.e. each other's. The file must end up indexed, with chunks matching the file."""
    t = signup(app_client, A)
    import src.build_index as bi
    real = bi.upsert_chunks
    barrier = threading.Barrier(2)

    def slow_upsert(*a, **kw):
        n = real(*a, **kw)
        try:
            barrier.wait(timeout=3)      # both versions written before either retires the other
        except threading.BrokenBarrierError:
            pass                         # a serialising fix never meets here: fine
        return n

    monkeypatch.setattr(bi, "upsert_chunks", slow_upsert)
    v1 = b"Version one. Kruskal algorithm builds a spanning tree greedily."
    v2 = b"Version two. Division Algorithm quotient remainder integers."
    results = []
    ths = [threading.Thread(target=lambda c=c: results.append(upload(app_client, t, "notes.txt", c)))
           for c in (v1, v2)]
    for th in ths:
        th.start()
    for th in ths:
        th.join(30)
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    on_disk = (_user_dir(A) / "notes.txt").read_bytes()
    assert material(app_client, t)["notes.txt"]["indexed"] is True, "both versions were deleted"
    pts, _ = app_client.fakes.client.scroll(config.COLLECTION_NAME, limit=100, with_payload=True)
    texts = {p.payload["text"] for p in pts if p.payload.get("user") == A}
    assert texts == {on_disk.decode()}, "chunks don't match the file on disk"


# =========================================================================== STORE
def _chunks(texts, source="notes"):
    from src.chunk import chunk_records
    return chunk_records([{"loc": f"p{i}", "order": i, "text": t}
                          for i, t in enumerate(texts, 1)], source=source)


def test_replace_doc_swaps_versions_and_keeps_old_on_failure(fakes, monkeypatch):
    import src.store as store
    c1 = _chunks(["alpha beta gamma", "delta epsilon"])
    store.replace_doc(A, "notes.txt", c1, fakes.embedder.embed_texts([c["text"] for c in c1]))
    c2 = _chunks(["zeta eta"])
    store.replace_doc(A, "notes.txt", c2, fakes.embedder.embed_texts([c["text"] for c in c2]))
    assert count(A) == 1
    real = store.get_client().upsert
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("qdrant went away")
        return real(*a, **kw)
    monkeypatch.setattr(store, "UPSERT_BATCH", 1)
    monkeypatch.setattr(store.get_client(), "upsert", flaky)
    c3 = _chunks(["one", "two", "three"])
    with pytest.raises(ConnectionError):
        store.replace_doc(A, "notes.txt", c3, fakes.embedder.embed_texts([c["text"] for c in c3]))
    monkeypatch.undo()
    assert count(A) == 1 and {"zeta eta"} == {
        p.payload["text"] for p in store.get_client().scroll(config.COLLECTION_NAME, limit=50)[0]}


def test_shared_bucket_scoping_never_reaches_students(fakes, index_text):
    import src.store as store
    index_text(None, "Shared", ["shared syllabus text"])
    index_text(A, "Mine", ["private notes of a"])
    assert count(None) == 1 and user_sources(None) == {"Shared"}
    store.delete_source(None, "Mine")
    store.delete_bucket(None)
    store.delete_user(None)
    store.delete_user("")
    assert count(A) == 1 and user_sources(A) == {"Mine"} and count(None) == 0


def test_delete_doc_is_exact_name_delete_source_is_label(fakes, tmp_path):
    from src.build_index import ingest_file
    import src.store as store
    for n in ("notes.txt", "notes.md"):
        p = tmp_path / n
        p.write_text(f"content of {n} about graphs")
        ingest_file(p, user=A)
    store.delete_doc(A, "notes.txt")
    assert user_docs(A) == {"notes.md"}
    store.delete_source(A, "notes")
    assert count(A) == 0


def _patch_build(monkeypatch, docs):
    import src.build_index as bi
    monkeypatch.setattr(bi, "list_documents", lambda: docs)
    monkeypatch.setattr(bi, "unsupported_files", lambda: [])
    monkeypatch.setattr(bi, "close", lambda: None)
    return bi


def test_build_that_fails_midway_keeps_the_previous_bucket(fakes, monkeypatch, tmp_path):
    """'Only that bucket is replaced' — and, like uploads, the old copy must survive a
    failed rebuild (embedding model crash, Qdrant blip) instead of being wiped first."""
    docs = []
    for i in range(2):
        p = tmp_path / f"doc{i}.txt"
        p.write_text(f"Document {i} explains topic{i} in detail.")
        docs.append(p)
    bi = _patch_build(monkeypatch, docs)
    bi.build(owner=None)
    before = count(None)
    assert before == 2
    real = bi.embed_texts
    calls = {"n": 0}

    def dies_on_second(texts):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("embedding model crashed")
        return real(texts)
    monkeypatch.setattr(bi, "embed_texts", dies_on_second)
    with pytest.raises(RuntimeError):
        bi.build(owner=None)
    assert count(None) == before, "a failed rebuild wiped the shared bucket"


def test_failure_while_retiring_old_version_does_not_leave_both_versions(fakes, monkeypatch, tmp_path):
    from src.build_index import ingest_file
    import src.build_index as bi
    p = tmp_path / "notes.txt"
    p.write_text("Old version about Kruskal spanning trees.")
    ingest_file(p, user=A)
    real = bi._delete
    calls = {"n": 0}

    def flaky(flt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("qdrant blip")
        return real(flt)
    monkeypatch.setattr(bi, "_delete", flaky)
    p.write_text("New version about the Division Algorithm.")
    with pytest.raises(ConnectionError):
        ingest_file(p, user=A)
    monkeypatch.setattr(bi, "_delete", real)
    assert count(A) == 1, "old and new versions are both live (duplicate / contradictory chunks)"


def test_pre_versioning_chunks_are_listed_and_deletable(app_client):
    """Chunks indexed by the previous release carry no `doc`/`ver` fields. After the
    upgrade the student's existing upload must still show as indexed, and removing it
    must remove its chunks (SPEC: remove deletes the file AND its chunks)."""
    t = signup(app_client, A)
    from qdrant_client.models import PointStruct
    from src.store import ensure_collection
    from tests.conftest import fake_embed_one
    ensure_collection()
    text = GROUNDED_DOC.decode()
    app_client.fakes.client.upsert(config.COLLECTION_NAME, wait=True, points=[PointStruct(
        id=str(uuid.uuid4()), vector=fake_embed_one(text),
        payload={"chunk_id": "nt::1::0", "user": A, "source": "nt", "loc": "text",
                 "order": 1, "text": text, "ocr": False})])
    (_user_dir(A) / "nt.txt").write_bytes(GROUNDED_DOC)
    assert material(app_client, t)["nt.txt"]["indexed"] is True
    assert delete_material(app_client, t, "nt.txt").status_code == 200
    assert count(A) == 0, "legacy chunks keep answering after the file was removed"


def test_reupload_over_pre_versioning_chunks_does_not_duplicate(app_client):
    t = signup(app_client, A)
    from qdrant_client.models import PointStruct
    from src.store import ensure_collection
    from tests.conftest import fake_embed_one
    ensure_collection()
    text = "Old copy. Division Algorithm quotient remainder."
    app_client.fakes.client.upsert(config.COLLECTION_NAME, wait=True, points=[PointStruct(
        id=str(uuid.uuid4()), vector=fake_embed_one(text),
        payload={"chunk_id": "nt::1::0", "user": A, "source": "nt", "loc": "text",
                 "order": 1, "text": text, "ocr": False})])
    r = upload(app_client, t, "nt.txt").json()
    assert count(A) == r["chunks"], "old-format chunks survive a re-upload (stale duplicate)"


# =========================================================================== ENGINE: broad asks
@pytest.mark.parametrize("q", [
    "Could you summarise my notes?",
    "Hey, give me an overview of my notes",
    "Summarise my notes in bullet points",
    "Summarise my notes for tomorrow's exam",
    "Kindly summarise my material",
])
def test_ordinary_phrasings_of_summarise_my_notes_get_the_overview(fakes, index_text, q):
    """Second-worst outcome per SPEC: refusing a request the material covers."""
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer(q, user=A)
    assert r["decision"] == "answer", (q, r["decision"])
    assert fakes.llm.count("overview") == 1


def test_summarise_a_named_document_covers_only_that_document(fakes, index_text):
    index_text(A, "Introduction", ["Kruskal algorithm builds a minimum spanning tree.",
                                   "Prim algorithm grows a spanning tree from a vertex."])
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("summarise my introduction pptx", user=A)
    assert r["decision"] == "answer"
    assert {s["source"] for s in r["sources"]} == {"Introduction"}


def test_summarise_a_document_that_does_not_exist_refuses(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("summarise my introduction pptx", user=A)
    assert r["decision"] == "refuse" and fakes.llm.count("overview") == 0


def test_summarise_module_2_covers_module_2_not_everything(fakes, index_text):
    index_text(A, "Module 1", NUMBER_THEORY)
    index_text(A, "Module 2", ["Kruskal algorithm builds a minimum spanning tree.",
                               "Dijkstra algorithm finds shortest paths."])
    r = answer("summarise module 2", user=A)
    assert r["decision"] == "answer"
    assert {s["source"] for s in r["sources"]} == {"Module 2"}, r["sources"]


def test_summarise_a_name_shared_by_two_documents_covers_both(fakes, index_text):
    index_text(A, "Graphs 1", ["Kruskal algorithm builds a minimum spanning tree."])
    index_text(A, "Graphs 2", ["Dijkstra algorithm finds shortest paths."])
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("summarise my graphs notes", user=A)
    assert r["decision"] == "answer"
    assert {s["source"] for s in r["sources"]} == {"Graphs 1", "Graphs 2"}, r["sources"]


@pytest.mark.parametrize("q", [
    "summarise ക്വാണ്ടം മെക്കാനിക്സ്",       # 'quantum mechanics' in Malayalam
    "summarise भौतिकी",                        # 'physics' in Hindi
    "give me an overview of ഊർജ്ജതന്ത്രം",
])
def test_broad_ask_about_a_non_latin_off_material_topic_refuses(fakes, index_text, q):
    """SPEC: 'summarise quantum mechanics' must refuse — in any script."""
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer(q, user=A)
    assert r["decision"] == "refuse", (q, r["decision"])
    assert r["answer"] == REFUSAL


# =========================================================================== ENGINE: teach rescue
@pytest.mark.parametrize("q", [
    "Can you explain that in simpler terms?",
    "Give me a real-life example",
    "I'm confused, can you go slower?",
])
def test_teach_ordinary_contextless_followups_stay_on_topic(fakes, index_text, q):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer(q, mode="teach", history=_HIST, user=A)
    assert r["decision"] != "refuse", q
    assert fakes.llm.count("teach") == 1


def test_teach_new_topic_that_is_a_substring_of_a_context_word_refuses(fakes, index_text):
    """'ion' is not in the notes; 'division' is. Substring matching lets the window
    rescue answer an off-material topic with the previous topic (deflection)."""
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("And what about an ion?", mode="teach", history=_HIST, user=A)
    assert r["decision"] == "refuse" and r["answer"] == REFUSAL
    assert fakes.llm.count("teach") == 0


def test_teach_new_non_latin_topic_refuses(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("ക്വാണ്ടം മെക്കാനിക്സ്?", mode="teach", history=_HIST, user=A)
    assert r["decision"] == "refuse" and r["answer"] == REFUSAL
    assert fakes.llm.count("teach") == 0


# =========================================================================== ENGINE: web scrub
def test_scrub_keeps_real_web_tags_and_plain_brackets():
    out = _scrub_web_tags("r is in [0, b-1]. [web · example.org] [Notes · p1] arr[i]")
    assert "[web · example.org]" in out and "[0, b-1]" in out and "arr[i]" in out
    assert "[Notes · p1]" not in out


def test_scrub_removes_entity_encoded_notes_tags():
    """The UI renders markdown to HTML, so '&#91;Notes · p1&#93;' becomes a gilded
    notes citation inside a web answer."""
    out = _scrub_web_tags("From the web &#91;Number Theory · p1&#93; [web · example.org]")
    assert not re.search(r"\[(?!web\s*·)[^\]\n]*·[^\]\n]*\]", html.unescape(out)), out


# =========================================================================== ENGINE: exam
def test_exam_small_kb_context_contains_the_topic_not_just_the_first_six_chunks(fakes, index_text):
    """Small KB hits come back in DOCUMENT order; hits[:6] then keeps the first six
    chunks of the alphabetically-first file, not the ones about the topic."""
    index_text(A, "Algebra", _filler(10))
    index_text(A, "Graphs", [KRUSKAL])
    r = generate("Kruskal spanning tree", 3, user=A)
    assert r["grounded"] is True
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "exam"]
    ctx = msgs[-1]["content"].split("\n\nTopic:")[0]
    assert "[Graphs · p1]" in ctx, "the passage about the topic never reached the exam setter"
    assert "Graphs · p1" in r["sources"]


def test_grade_small_kb_context_contains_the_topic(fakes, index_text):
    index_text(A, "Algebra", _filler(10))
    index_text(A, "Graphs", [KRUSKAL])
    r = grade("Kruskal spanning tree", "It picks the cheapest edges.", user=A)
    assert r["grounded"] is True
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "grade"]
    assert "[Graphs · p1]" in msgs[-1]["content"], "graded against unrelated passages"


def test_exam_any_bracket_is_not_a_citation(fakes, index_text):
    """SPEC: each practice answer is cited — '[0, b-1]' or a tag for a source the
    student doesn't have is not a citation."""
    index_text(A, "Number Theory", NUMBER_THEORY)
    fakes.llm.respond = _say({"exam":
        "Q: What range does r lie in?\nA: r lies in [0, b-1].\n---\n"
        "Q: What is a prime?\nA: Only 1 and itself. [Physics · p9]\n---\n"
        "Q: What is the gcd?\nA: The largest common divisor. [Number Theory · p2]"})
    r = generate(NT_STRONG, 3, user=A)
    assert [it["q"] for it in r["items"]] == ["What is the gcd?"], r["items"]


def test_exam_keeps_legit_citation_variants(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    fakes.llm.respond = _say({"exam":
        "**Q1:** What is the gcd?\n**A1:** The largest common divisor. **[Number Theory · p2]**\n---\n"
        "Q: What is a prime?\nA: Only 1 and itself.\n[Number Theory · p3]\n---\n"
        "Q: State the Division Algorithm.\nA: a = bq + r. [Number Theory · p1; Number Theory · p2]"})
    r = generate(NT_STRONG, 3, user=A)
    assert len(r["items"]) == 3, r["items"]


def test_exam_n_above_max_is_422_and_max_is_honoured(app_client):
    from src.exam import MAX_N
    t = signup(app_client, A)
    upload(app_client, t, "nt.txt")
    r = app_client.post("/api/exam", json={"topic": "Division Algorithm", "n": MAX_N + 1},
                        headers=H(t))
    assert r.status_code == 422
    r = app_client.post("/api/exam", json={"topic": "Division Algorithm quotient remainder",
                                           "n": MAX_N}, headers=H(t))
    assert r.status_code == 200 and len(r.json()["items"]) == MAX_N


# =========================================================================== ENGINE: grade
def test_control_characters_are_stripped_from_names():
    # (sent directly: an HTTP client percent-encodes these in multipart names)
    assert app_module._safe_name("no\x07tes\x1b.txt") == "notes.txt"


def test_safe_name_is_idempotent():
    """The UI sends the LISTED name back to /api/material/delete, which runs
    _safe_name again; a name that changes on the second pass can't be removed (or
    removes a different file)."""
    for raw in (".  spaced.txt", "notes.txt .", ". .txt", "x.txt. ", " .notes.txt"):
        once = app_module._safe_name(raw)
        assert once is None or app_module._safe_name(once) == once, (raw, once)


def test_strip_marks_keeps_legitimate_feedback_lines():
    fb = ("Correct: You found that 17/5 gives q = 3 and r = 2. [Number Theory · p1]\n"
          "Missing: The condition 0 <= r < b. [Number Theory · p1]\n"
          "Incorrect: None.\n"
          "Suggestion: Mark the 2 conditions on r explicitly, e.g. for 100/10.")
    out = _strip_marks(fb)
    for h in ("Correct:", "Missing:", "Incorrect:", "Suggestion:"):
        assert h in out, f"{h} line deleted: {out!r}"
    assert "17/5" in out


def test_strip_marks_catches_common_mark_formats():
    for line in ("**Grade:** B+", "Overall: 85% correct.", "Score: 8/10", "I'd give it 7 out of 10.",
                 "**Marks awarded:** 6"):
        out = _strip_marks("Correct: good.\n" + line + "\nSuggestion: add an example.")
        assert "Correct: good." in out and "Suggestion:" in out
        assert not re.search(r"B\+|85%|8/10|7 out of 10|\b6\b", out), (line, out)


# =========================================================================== ERRORS
def test_llm_down_is_503_with_a_model_message(app_client):
    t = signup(app_client, A)
    upload(app_client, t, "nt.txt")
    app_client.fakes.llm.fail = ConnectionError("connection refused")
    for path, body in (("/api/answer", {"question": "State the Division Algorithm"}),
                       ("/api/exam", {"topic": "Division Algorithm quotient remainder", "n": 2}),
                       ("/api/grade", {"question": "State the Division Algorithm",
                                       "student_answer": "a = bq + r"})):
        r = app_client.post(path, json=body, headers=H(t))
        assert r.status_code == 503, (path, r.status_code)
        assert r.json()["ok"] is False and "Ollama" in r.json()["error"]


def test_unexpected_engine_bug_is_json_500_without_internals(app_client, monkeypatch):
    t = signup(app_client, A)

    def boom(*a, **kw):
        raise KeyError("secret_internal_field")
    monkeypatch.setattr(app_module, "answer", boom)
    r = ask(app_client, t, "anything")
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/json")
    assert "secret_internal_field" not in r.text and r.json()["ok"] is False


def test_unguarded_endpoints_also_fail_as_json(app_client, monkeypatch):
    """Login, /api/me, logout, change-password have no `guarded`: a DB error there
    ('database is locked' under load) must still be a JSON error, not text/plain."""
    c = TestClient(app_module.app, raise_server_exceptions=False)

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(app_module, "_db", locked)
    for method, path, body in (("post", "/api/login", {"email": A, "password": PW}),
                               ("get", "/api/me", None)):
        kw = {"json": body} if body else {}
        r = getattr(c, method)(path, headers=H("whatever"), **kw)
        assert r.status_code >= 500
        assert r.headers["content-type"].startswith("application/json"), (path, r.text[:80])
        assert r.json().get("error")


def test_account_delete_with_qdrant_down_keeps_the_account(app_client, monkeypatch):
    t = signup(app_client, A)
    upload(app_client, t, "nt.txt")

    def down(_):
        raise ConnectionError("qdrant down")
    monkeypatch.setattr(app_module, "delete_user", down)
    r = app_client.post("/api/account/delete", json={"password": PW}, headers=H(t))
    assert r.status_code == 503 and "Qdrant" in r.json()["error"]
    assert app_client.get("/api/me", headers=H(t)).status_code == 200
    assert "nt.txt" in material(app_client, t)


@pytest.mark.parametrize("body, status", [
    ({"question": ""}, 400),
    ({"question": "   "}, 400),
    ({"question": "x" * 4001}, 400),
    ({}, 422),
    ({"question": 123}, 422),
    ({"question": "ok", "mode": "bogus"}, 422),
    ({"question": "ok", "history": [{"role": "system", "content": "x"}]}, 422),
])
def test_answer_validation_status_codes(app_client, body, status):
    t = signup(app_client, A)
    r = app_client.post("/api/answer", json=body, headers=H(t))
    assert r.status_code == status, (body, r.status_code, r.text)
    assert r.json()["ok"] is False and r.json()["error"]
    assert app_client.fakes.llm.calls == []


def test_validation_runs_after_auth_never_leaks_shape_to_strangers(app_client):
    r = app_client.post("/api/answer", json={})
    assert r.status_code in (401, 422)


def test_health_is_public_and_data_free(app_client):
    signup(app_client, A)
    r = app_client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "qdrant", "llm"} and body["qdrant"] is True


def test_health_reports_llm_down_when_the_model_is_not_pulled(app_client, monkeypatch):
    """The pill says 'Active' while every question 503s if Ollama runs without
    llama3.1:8b. Health should check the configured model, not just the daemon."""
    import ollama
    monkeypatch.setattr(ollama, "list", lambda: {"models": [{"model": "mistral:7b", "name": "mistral:7b"}]})
    assert app_client.get("/api/health").json()["llm"] is False
    monkeypatch.setattr(ollama, "list", lambda: {"models": [{"model": config.LLM_MODEL,
                                                             "name": config.LLM_MODEL}]})
    assert app_client.get("/api/health").json()["llm"] is True
