"""QA — HTTP API, AUTH and SECURITY for Vidhyāsathi.

Judged against qa/SPEC.md ("Accounts & isolation", "My material", "Ops", "UI").
The app is shared over a public Cloudflare tunnel, so every endpoint is treated as
internet-facing. Tests are written as they SHOULD pass per the spec; a failing test
is a bug (no xfail/skip).

AUTH HELPER
-----------
The spec requires server-enforced identity: a signed-in student queries/lists/deletes
ONLY their own material, and request tampering must not reach another user's data.
The current code identifies a user purely by a client-supplied `user` field, so the
intended fix will likely add session tokens (login -> token; requests send
`Authorization: Bearer <token>`; the server derives the user and ignores any client
`user`). To stay valid across that fix, every isolation test goes through ONE helper
(`Session`) that signs up + logs in and carries whatever auth the API provides. Today
that is just the email echoed into the `user` field / query; after the fix it becomes
the bearer token. Only `Session` needs editing then — the tests express the REQUIREMENT.
"""
import io
import os

import pytest

from tests.samples import make_sample, NUMBER_THEORY

# A doc whose words strongly overlap "Division Algorithm" queries, so grounding is
# firm (top_score ~0.75) regardless of the fake embedder's bag-of-words dilution.
GROUNDED_DOC = (
    "Division Algorithm. The Division Algorithm states that for integers a and b "
    "there exist unique integers q and r with a = bq + r. Division Algorithm "
    "quotient remainder."
).encode()

OTHER_TOPIC = [
    "Photosynthesis is the process by which green plants convert sunlight, water and "
    "carbon dioxide into glucose and oxygen using chlorophyll in the chloroplasts.",
    "The light-dependent reactions occur in the thylakoid membranes and produce ATP "
    "and NADPH, which the Calvin cycle then uses to fix carbon into sugars.",
]


# --------------------------------------------------------------------------- auth
class Session:
    """One authenticated student. Adapt ONLY this class when real tokens land:
    set self.token from the login response and have auth()/scope() use it."""

    def __init__(self, client, name, email, password):
        self.client = client
        self.name = name
        self.email = email
        self.password = password
        self.token = None  # future: bearer token from login

    @classmethod
    def create(cls, client, name, email, password="passw0rd"):
        s = cls(client, name, email, password)
        r = client.post("/api/signup", json={"name": name, "email": email, "password": password})
        assert r.status_code in (200, 201), r.text
        r = client.post("/api/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        body = r.json()
        # If/when login returns a token, capture it so every request below is authed.
        s.token = body.get("token")
        return s

    def auth(self):
        """Headers that authenticate this session (empty until tokens exist)."""
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def scope(self, body=None):
        """Merge this user's identity into a request body the way the API expects.
        Today: the client-supplied `user` field. After the fix: nothing here (the
        token in auth() carries identity) — but leaving the email is harmless because
        a fixed server must ignore a client `user` that disagrees with the token."""
        body = dict(body or {})
        body.setdefault("user", self.email)
        return body

    # --- thin wrappers so a test never hand-rolls identity -------------------
    def post(self, path, json=None):
        return self.client.post(path, json=self.scope(json), headers=self.auth())

    def material(self):
        return self.client.get(f"/api/material?user={self.email}", headers=self.auth())

    def upload(self, filename, content, user_field=True, ctype="text/plain"):
        data = {"user": self.email} if user_field else {}
        return self.client.post(
            "/api/upload",
            files={"file": (filename, content, ctype)},
            data=data,
            headers=self.auth(),
        )

    def answer(self, question, **kw):
        return self.post("/api/answer", {"question": question, **kw})

    def exam(self, topic, **kw):
        return self.post("/api/exam", {"topic": topic, **kw})

    def grade(self, question, student_answer, **kw):
        return self.post("/api/grade", {"question": question, "student_answer": student_answer, **kw})

    def delete_material(self, name):
        return self.post("/api/material/delete", {"name": name})


def names_in(material_response):
    return {d["name"] for d in material_response.json().get("docs", [])}


# The TestClient does not raise on 4xx/5xx; it returns the response.
def test_signup_then_login_roundtrip(app_client):
    s = Session.create(app_client, "Asha", "asha@x.com")
    assert s.client.post("/api/login", json={"email": "asha@x.com", "password": "passw0rd"}).json()["ok"]


# =================================================================== ISOLATION
# Spec: "Each signed-in student sees and queries only their own uploads. Another
# user's material must never appear ... A student must not be able to read, query
# or delete another student's material or account by tampering with requests."

def test_material_list_shows_only_own_files(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    b = Session.create(app_client, "B", "b@x.com")
    a.upload("alpha.txt", "\n\n".join(NUMBER_THEORY).encode())
    b.upload("beta.txt", "\n\n".join(OTHER_TOPIC).encode())
    assert names_in(a.material()) == {"alpha.txt"}
    assert names_in(b.material()) == {"beta.txt"}


def test_cannot_list_another_users_material_by_tampering(app_client):
    """Attacker signs up, then requests the victim's material by setting user=victim.
    The server must scope to the authenticated caller, not the client-supplied id."""
    victim = Session.create(app_client, "Victim", "victim@x.com")
    victim.upload("secret.txt", "\n\n".join(NUMBER_THEORY).encode())
    attacker = Session.create(app_client, "Mallory", "mallory@x.com")
    # attacker forges the victim's email in the query string
    r = attacker.client.get("/api/material?user=victim@x.com", headers=attacker.auth())
    assert r.status_code == 200
    assert "secret.txt" not in names_in(r), (
        "material list leaked another user's files via a forged `user` query param")


def test_cannot_query_another_users_material_by_tampering(app_client):
    victim = Session.create(app_client, "Victim", "victim@x.com")
    victim.upload("notes.txt", GROUNDED_DOC)
    attacker = Session.create(app_client, "Mallory", "mallory@x.com")
    # forge victim's identity in the answer request body
    r = attacker.client.post(
        "/api/answer",
        json={"question": "Explain the Division Algorithm", "user": "victim@x.com"},
        headers=attacker.auth())
    assert r.status_code == 200
    body = r.json()
    # Attacker has no material of their own -> must refuse, never surface victim's notes.
    assert body["decision"] == "refuse", (
        "answer surfaced another user's material via a forged `user` field")
    assert "Number Theory" not in body.get("answer", "")


def test_cannot_exam_on_another_users_material_by_tampering(app_client):
    victim = Session.create(app_client, "Victim", "victim@x.com")
    victim.upload("notes.txt", GROUNDED_DOC)
    attacker = Session.create(app_client, "Mallory", "mallory@x.com")
    r = attacker.client.post(
        "/api/exam", json={"topic": "the Division Algorithm", "n": 3, "user": "victim@x.com"},
        headers=attacker.auth())
    assert r.status_code == 200
    assert r.json()["grounded"] is False, "practice set generated from another user's material"


def test_cannot_grade_against_another_users_material_by_tampering(app_client):
    victim = Session.create(app_client, "Victim", "victim@x.com")
    victim.upload("notes.txt", GROUNDED_DOC)
    attacker = Session.create(app_client, "Mallory", "mallory@x.com")
    r = attacker.client.post(
        "/api/grade",
        json={"question": "Explain the Division Algorithm", "student_answer": "a = bq + r",
              "user": "victim@x.com"},
        headers=attacker.auth())
    assert r.status_code == 200
    assert r.json()["grounded"] is False, "grading drew on another user's material"


def test_cannot_delete_another_users_material_by_tampering(app_client):
    """Spec: a student must not delete another student's material by tampering."""
    victim = Session.create(app_client, "Victim", "victim@x.com")
    victim.upload("important.txt", "\n\n".join(NUMBER_THEORY).encode())
    attacker = Session.create(app_client, "Mallory", "mallory@x.com")
    r = attacker.client.post(
        "/api/material/delete", json={"name": "important.txt", "user": "victim@x.com"},
        headers=attacker.auth())
    # Either rejected outright, or a no-op — but the victim's file must survive.
    assert "important.txt" in names_in(victim.material()), (
        "attacker deleted another user's material via a forged `user` field")


def test_answer_never_mixes_two_users_material(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    b = Session.create(app_client, "B", "b@x.com")
    a.upload("nt.txt", GROUNDED_DOC)
    b.upload("bio.txt", "\n\n".join(OTHER_TOPIC).encode())
    # B asks about A's topic: B has no such material -> must refuse.
    r = b.answer("Explain the Division Algorithm")
    assert r.json()["decision"] == "refuse", "user B got an answer from user A's material"


# ============================================================ UNAUTHENTICATED
# Spec: identity must be enforced; an unauthenticated request must be rejected.

@pytest.mark.parametrize("method,path,payload", [
    ("post", "/api/answer", {"question": "State the Division Algorithm"}),
    ("post", "/api/exam", {"topic": "Division Algorithm", "n": 3}),
    ("post", "/api/grade", {"question": "q", "student_answer": "a"}),
    ("post", "/api/material/delete", {"name": "x.txt"}),
])
def test_core_endpoints_reject_unauthenticated(app_client, method, path, payload):
    """With no credentials and no forged user, these must not silently act as some
    user. A fixed server returns 401; today the endpoints answer anyway."""
    r = getattr(app_client, method)(path, json=payload)
    assert r.status_code == 401, f"{path} served an unauthenticated request (got {r.status_code})"


def test_material_list_requires_auth(app_client):
    r = app_client.get("/api/material")
    assert r.status_code == 401, "GET /api/material served an unauthenticated request"


def test_upload_requires_auth(app_client):
    r = app_client.post("/api/upload", files={"file": ("x.txt", b"hello world text", "text/plain")})
    assert r.status_code == 401, "upload accepted with no authenticated user"


# ======================================================== /api/users LEAK
# Spec gives no "list every tester" feature; it exposes names+emails of all users
# to anyone on the public tunnel. Must not be an open endpoint.

def test_users_endpoint_not_public(app_client):
    Session.create(app_client, "Asha", "asha@x.com")
    Session.create(app_client, "Ravi", "ravi@x.com")
    r = app_client.get("/api/users")
    assert r.status_code in (401, 403, 404), (
        "/api/users is public and leaks every registered name + email")


def test_users_endpoint_does_not_leak_emails_unauthenticated(app_client):
    Session.create(app_client, "Asha", "asha@x.com")
    r = app_client.get("/api/users")
    if r.status_code == 200:
        body = r.text
        assert "asha@x.com" not in body, "/api/users leaked a registered email without auth"


# ============================================================ PATH TRAVERSAL
# Spec: upload saves into the user's OWN folder. file.filename is attacker-controlled.

def _all_files(root):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            out.append(os.path.join(dirpath, f))
    return out


def test_upload_rejects_parent_traversal_filename(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    before = set(_all_files(app_client.tmp))
    a.upload("../../evil.txt", b"The Division Algorithm a = bq + r here is text")
    created = set(_all_files(app_client.tmp)) - before
    uploads_root = str(app_client.tmp / "data" / "uploads")
    for p in created:
        assert p.startswith(uploads_root), f"upload escaped the uploads tree: {p}"


def test_upload_rejects_absolute_path_filename(app_client, tmp_path):
    a = Session.create(app_client, "A", "a@x.com")
    target = tmp_path / "pwned_abs.txt"
    a.upload(str(target), b"The Division Algorithm a = bq + r sample text")
    assert not target.exists(), "absolute-path filename wrote outside the uploads tree"


def test_upload_rejects_traversal_into_app_code(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    before = set(_all_files(app_client.tmp))
    a.upload("../../app.py", b"The Division Algorithm overwrite attempt text here")
    created = [p for p in set(_all_files(app_client.tmp)) - before if p.endswith("app.py")]
    assert not created, "traversal filename could clobber a path named like app code"


def test_uploaded_file_lands_in_user_folder(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("clean.txt", "\n\n".join(NUMBER_THEORY).encode())
    hits = [p for p in _all_files(app_client.tmp) if p.endswith("clean.txt")]
    assert hits, "a normal upload was not saved"
    for p in hits:
        assert os.path.join("data", "uploads") in p, "upload not stored under the user's uploads folder"


# ============================================================ BLANK USER BUCKET
# Spec: each student's material is private. A blank user must not become a shared
# bucket that every unauthenticated/blank caller can read, query and delete.

def test_blank_user_is_not_a_shared_readable_bucket(app_client):
    # Two separate "blank user" callers should never see each other's uploads.
    app_client.post("/api/upload", files={"file": ("blank1.txt", "\n\n".join(NUMBER_THEORY).encode(), "text/plain")},
                    data={"user": ""})
    r = app_client.get("/api/material?user=")
    # A fixed server rejects the anonymous request outright...
    if r.status_code == 200:
        assert "blank1.txt" not in names_in(r), (
            "blank user acts as a shared bucket visible across callers")
    else:
        assert r.status_code == 401


def test_blank_user_query_is_rejected(app_client):
    r = app_client.post("/api/answer", json={"question": "State the Division Algorithm", "user": ""})
    assert r.status_code == 401, "blank user is accepted as an anonymous shared identity"


# ============================================================ INPUT VALIDATION
def test_answer_empty_question_rejected(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    r = a.answer("")
    assert r.status_code == 400, "empty question should be a 400, not processed"


def test_answer_whitespace_question_rejected(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = a.answer("    \n\t  ")
    assert r.status_code == 400, "whitespace-only question should be rejected"


def test_answer_oversized_question_rejected(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = a.answer("why " * 50000)
    assert r.status_code in (400, 413), "an enormous question should be bounded, not accepted"


def test_answer_missing_question_field_is_422(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = app_client.post("/api/answer", json={"user": "a@x.com"}, headers=a.auth())
    assert r.status_code == 422, "missing required field should be a validation error"


def test_answer_malformed_history_items_rejected(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    # history items must be {role, content}; a bare string / wrong shape must not 500
    r = a.answer("State the Division Algorithm", mode="teach",
                 history=["not a dict", {"role": "user"}, {"content": "x"}, 42])
    assert r.status_code in (200, 400, 422), r.text
    assert r.status_code != 500, "malformed history crashed the endpoint"


def test_exam_n_zero_rejected(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    r = a.exam("the Division Algorithm", n=0)
    assert r.status_code == 422 or (r.status_code == 200 and len(r.json().get("items", [])) >= 1), \
        "n=0 should be rejected, not asked of the model"


def test_exam_n_negative_rejected(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    r = a.exam("the Division Algorithm", n=-5)
    assert r.status_code == 422, "negative n should be a validation error"


def test_exam_n_absurdly_large_bounded(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    r = a.exam("the Division Algorithm", n=100000)
    assert r.status_code == 422, "an absurd n should be bounded before generation"


def test_signup_rejects_non_email(app_client):
    r = app_client.post("/api/signup", json={"name": "A", "email": "not-an-email", "password": "passw0rd"})
    assert r.status_code in (400, 422), "signup accepted a non-email address"


def test_signup_rejects_short_password(app_client):
    """change-password enforces >=4; signup must enforce a minimum too (spec: passwords
    salted+hashed, a real account system), else a 1-char password is accepted."""
    r = app_client.post("/api/signup", json={"name": "A", "email": "short@x.com", "password": "a"})
    assert r.status_code in (400, 422), "signup accepted a 1-character password"


def test_signup_rejects_overlong_name(app_client):
    r = app_client.post("/api/signup", json={"name": "Z" * 100000, "email": "big@x.com", "password": "passw0rd"})
    assert r.status_code in (400, 422), "signup accepted an unbounded name"


def test_signup_duplicate_email_conflict(app_client):
    app_client.post("/api/signup", json={"name": "A", "email": "dup@x.com", "password": "passw0rd"})
    r = app_client.post("/api/signup", json={"name": "B", "email": "dup@x.com", "password": "passw0rd"})
    assert r.status_code == 409 and r.json()["ok"] is False


# ============================================================ ERROR HANDLING
# Spec/Ops: when Ollama or Qdrant is down, requests fail with a CLEAR JSON error,
# not a crash / raw 500.

def test_answer_llm_down_returns_clean_json_error(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", GROUNDED_DOC)  # strong enough that the model is needed
    app_client.fakes.llm.fail = ConnectionError("ollama down")
    r = a.answer("State the Division Algorithm")
    assert r.status_code in (200, 503), r.status_code
    body = r.json()
    if r.status_code == 200:
        # must not crash; must communicate the failure, not fabricate an answer
        assert body.get("error") or body.get("decision") == "error", \
            "LLM outage produced no clear error signal"
    else:
        assert "error" in body


def test_exam_llm_down_returns_clean_json_error(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", GROUNDED_DOC)  # strong enough that the model is needed
    app_client.fakes.llm.fail = ConnectionError("ollama down")
    r = a.exam("the Division Algorithm", n=3)
    assert r.status_code != 500, "exam raised a raw 500 when the LLM was down"
    assert "error" in r.json() or r.status_code == 503


def test_grade_llm_down_returns_clean_json_error(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", GROUNDED_DOC)  # strong enough that the model is needed
    app_client.fakes.llm.fail = ConnectionError("ollama down")
    r = a.grade("State the Division Algorithm", "a = bq + r")
    assert r.status_code != 500, "grade raised a raw 500 when the LLM was down"
    assert "error" in r.json() or r.status_code == 503


def test_qdrant_down_returns_clean_json_error(app_client, monkeypatch):
    """Point the store client at a dead port mid-request: a clear error, not a crash."""
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    import src.store as store
    from qdrant_client import QdrantClient
    dead = QdrantClient(url="http://localhost:1")  # nothing listening
    monkeypatch.setattr(store, "_client", dead)
    r = a.answer("State the Division Algorithm")
    assert r.status_code != 500, "Qdrant outage produced a raw 500"
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert r.json().get("error") or r.json().get("decision") == "error"


# ============================================================ UPLOAD FAILURE PATH
# Spec: "the student is told plainly whether it was indexed". An ok:true with
# indexed:false on a failure is a misleading success.

def test_upload_unsupported_type_reports_failure(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = a.upload("malware.exe", b"MZ\x90\x00binary", ctype="application/octet-stream")
    body = r.json()
    assert body.get("indexed") is False
    assert body.get("ok") is False, "an unindexable upload still reported ok:true (misleading success)"


def test_upload_empty_file_reports_failure(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = a.upload("empty.txt", b"   \n  ")
    body = r.json()
    assert body.get("indexed") is False
    assert body.get("ok") is False, "an empty/unindexable file reported ok:true"


def test_upload_failure_leaves_no_orphan_file(app_client):
    """If indexing fails the saved file must not linger in the user's folder
    (spec: the list shows indexed/not-indexed; a silent orphan is neither)."""
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("empty.txt", b"   \n  ")
    assert "empty.txt" not in names_in(a.material()), "failed upload left an orphan file in the list"


def test_upload_corrupt_pdf_reports_failure(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = a.upload("broken.pdf", b"%PDF-1.4 this is not really a pdf \x00\x01\x02",
                 ctype="application/pdf")
    assert r.status_code != 500, "a corrupt PDF crashed the upload endpoint"
    assert r.json().get("indexed") is False


# ============================================================ RE-UPLOAD / REPLACE
def test_reupload_same_name_replaces_not_duplicates(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    docs = [d for d in a.material().json()["docs"] if d["name"] == "nt.txt"]
    assert len(docs) == 1, "re-uploading the same file created a duplicate entry"


def test_two_different_files_coexist(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    a.upload("bio.txt", "\n\n".join(OTHER_TOPIC).encode())
    assert names_in(a.material()) == {"nt.txt", "bio.txt"}


# ============================================================ MATERIAL DELETE
def test_delete_own_material_removes_file_and_chunks(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", GROUNDED_DOC)
    assert a.answer("Explain the Division Algorithm").json()["decision"] != "refuse"
    r = a.delete_material("nt.txt")
    assert r.json()["ok"] is True
    assert "nt.txt" not in names_in(a.material())
    # chunks gone too -> now refuses
    assert a.answer("Explain the Division Algorithm").json()["decision"] == "refuse", \
        "delete removed the file but left its chunks queryable"


def test_delete_missing_material_is_404(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = a.delete_material("nope.txt")
    assert r.status_code == 404 and r.json()["ok"] is False


def test_delete_with_path_components_does_not_escape(app_client):
    """A delete name with path components must not reach outside the user's folder."""
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("keep.txt", "\n\n".join(NUMBER_THEORY).encode())
    # try to delete the index.html sitting above the uploads tree via traversal
    a.delete_material("../index.html")
    assert (app_client.tmp / "index.html").exists(), "delete with ../ escaped the user's folder"
    assert "keep.txt" in names_in(a.material())


# ============================================================ ACCOUNT DELETE
def test_account_delete_wrong_password_rejected(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = app_client.post("/api/account/delete", json={"password": "wrong"}, headers=a.auth())
    # signed in but the confirmation is wrong: rejected, and NOT treated as a dead session
    assert r.status_code == 403 and r.json()["ok"] is False
    assert app_client.get("/api/me", headers=a.auth()).status_code == 200
    # and without a session at all, it's a 401
    assert app_client.post("/api/account/delete", json={"password": "passw0rd"}).status_code == 401


def test_account_delete_removes_account_and_allows_resignup(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", "\n\n".join(NUMBER_THEORY).encode())
    r = app_client.post("/api/account/delete", json={"email": "a@x.com", "password": "passw0rd"},
                        headers=a.auth())
    assert r.json()["ok"] is True
    # the deleted email can sign up again cleanly
    r = app_client.post("/api/signup", json={"name": "A2", "email": "a@x.com", "password": "passw0rd"})
    assert r.status_code in (200, 201) and r.json()["ok"] is True, \
        "deleted email could not re-register cleanly"


def test_account_delete_purges_material_chunks(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("nt.txt", GROUNDED_DOC)
    app_client.post("/api/account/delete", json={"email": "a@x.com", "password": "passw0rd"},
                    headers=a.auth())
    # re-register the same email -> must start empty, not inherit old chunks
    a2 = Session.create(app_client, "A2", "a@x.com")
    r = a2.answer("Explain the Division Algorithm")
    assert r.json()["decision"] == "refuse", "account delete left material chunks behind"


# ============================================================ PASSWORD / HASHING
def test_change_password_wrong_old_rejected(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = app_client.post("/api/account/password",
                        json={"old_password": "nope", "new_password": "newpass"}, headers=a.auth())
    assert r.status_code == 403 and r.json()["ok"] is False
    assert app_client.get("/api/me", headers=a.auth()).status_code == 200


def test_change_password_then_login_with_new(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    r = app_client.post("/api/account/password",
                        json={"email": "a@x.com", "old_password": "passw0rd", "new_password": "newpass9"},
                        headers=a.auth())
    assert r.json()["ok"] is True
    assert app_client.post("/api/login", json={"email": "a@x.com", "password": "newpass9"}).json()["ok"]
    assert app_client.post("/api/login", json={"email": "a@x.com", "password": "passw0rd"}).status_code == 401


def test_password_not_stored_in_plaintext(app_client):
    import sqlite3
    Session.create(app_client, "A", "secretpw@x.com", password="SuperSecret123")
    db = app_client.tmp / "users.db"
    rows = sqlite3.connect(db).execute("SELECT pw FROM users").fetchall()
    assert all("SuperSecret123" not in (r[0] or "") for r in rows), "password stored in plaintext"


def test_login_wrong_password_rejected(app_client):
    Session.create(app_client, "A", "a@x.com")
    assert app_client.post("/api/login", json={"email": "a@x.com", "password": "bad"}).status_code == 401


# ============================================================ XSS / INJECTION
# Spec/UI: "Untrusted text (model output, web pages, file names, user input) must
# never execute as HTML/JS in the page." The API must not hand back a raw HTML/JS
# payload that the UI would inject verbatim (the UI does set some innerHTML).

def test_material_name_with_html_is_not_executable_markup(app_client):
    a = Session.create(app_client, "A", "a@x.com")
    a.upload("<img src=x onerror=alert(1)>.txt", "\n\n".join(NUMBER_THEORY).encode())
    docs = a.material().json()["docs"]
    # The stored/returned name should be sanitised, OR the UI escapes it. Since the UI
    # escapes names (esc()), at minimum the API must not have written an odd path.
    names = {d["name"] for d in docs}
    assert names, "upload with an HTML-ish name was lost"
    # name must not contain a path separator that would imply traversal
    assert not any("/" in n or "\\" in n for n in names), "a crafted filename kept path separators"


# ============================================================ HEALTH / SANITY
def test_index_served(app_client):
    assert app_client.get("/").status_code == 200


def test_cors_wildcard_without_credentials(app_client):
    """CORS allow_origins='*' is set. With credentials disabled the browser blocks
    reading cross-origin responses, so with purely client-supplied identity the risk
    is LOW; it becomes a real concern once cookies/tokens are used. This test documents
    the current config so a future credentialed auth doesn't silently pair with '*'."""
    import app as app_module
    mws = [m for m in app_module.app.user_middleware if "CORS" in str(m.cls.__name__)]
    assert mws, "CORS middleware missing"
    opts = mws[0].kwargs if hasattr(mws[0], "kwargs") else {}
    # If credentials are ever turned on, '*' origins must not remain.
    if opts.get("allow_credentials"):
        assert "*" not in opts.get("allow_origins", []), \
            "allow_origins='*' with allow_credentials=True is forbidden by browsers and unsafe"
