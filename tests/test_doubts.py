"""Phase 2 — Ask for Help: routing, permissions, privacy, and the feedback loop."""
import pytest

import app as app_module

PW = "passw0rd"
ADMIN = "owner@x.com"


def signup(c, email, name=None):
    r = c.post("/api/signup", json={"name": name or email.split("@")[0].title(), "email": email,
                                    "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def H(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def world(app_client, monkeypatch):
    """An admin, two helpers with different subjects, and a student."""
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", {ADMIN})
    c = app_client
    t = {"admin": signup(c, ADMIN, "Owner"), "maths": signup(c, "maths@x.com", "Meera Teacher"),
         "chem": signup(c, "chem@x.com", "Charles Senior"), "stu": signup(c, "stu@x.com", "Asha Kumar")}
    assert c.post("/api/admin/helpers", headers=H(t["admin"]),
                  json={"email": "maths@x.com", "expertise": "number theory gcd primes division algorithm"}).json()["ok"]
    assert c.post("/api/admin/helpers", headers=H(t["admin"]),
                  json={"email": "chem@x.com", "expertise": "chemistry atoms molecules reactions"}).json()["ok"]
    return c, t


def ask(c, tok, q, **kw):
    return c.post("/api/doubts", headers=H(tok), json={"question": q, **kw})


def doubts(c, tok):
    return c.get("/api/doubts", headers=H(tok)).json()


def test_doubt_is_routed_to_the_helper_whose_subjects_match(world):
    c, t = world
    r = ask(c, t["stu"], "Why is the remainder in the division algorithm less than b?",
            sources=["Number Theory · p1"]).json()
    assert r["ok"] and r["routed_to"] == "Meera Teacher"
    assert [d["question"] for d in doubts(c, t["maths"])["inbox"]] == \
        ["Why is the remainder in the division algorithm less than b?"]
    assert doubts(c, t["chem"])["inbox"] == []
    r = ask(c, t["stu"], "How do atoms form molecules in a reaction?").json()
    assert r["routed_to"] == "Charles Senior"


def test_routing_spreads_load_when_subjects_tie(app_client, monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", {ADMIN})
    c = app_client
    adm = signup(c, ADMIN)
    for e in ("h1@x.com", "h2@x.com"):
        signup(c, e)
        c.post("/api/admin/helpers", headers=H(adm), json={"email": e, "expertise": "graphs"})
    stu = signup(c, "s@x.com")
    who = {ask(c, stu, f"graphs question {i}").json()["routed_to"] for i in range(4)}
    assert who == {"H1", "H2"}, "all doubts went to one helper"


def test_no_helpers_means_unassigned_and_the_admin_sees_it(app_client, monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", {ADMIN})
    c = app_client
    adm, stu = signup(c, ADMIN), signup(c, "s@x.com")
    r = ask(c, stu, "What is Kruskal's algorithm?").json()
    assert r["ok"] and r["routed_to"] is None
    assert [d["question"] for d in doubts(c, adm)["inbox"]] == ["What is Kruskal's algorithm?"]


def test_student_sees_own_doubts_and_helper_sees_only_first_name(world):
    c, t = world
    ask(c, t["stu"], "Explain gcd of two numbers", note="I missed this lecture")
    mine = doubts(c, t["stu"])["mine"]
    assert len(mine) == 1 and mine[0]["status"] == "open" and mine[0]["helper_name"] == "Meera Teacher"
    item = doubts(c, t["maths"])["inbox"][0]
    assert item["student_name"] == "Asha" and "stu@x.com" not in str(item)
    assert item["note"] == "I missed this lecture"


def test_only_the_assigned_helper_or_admin_can_answer(world):
    c, t = world
    d = ask(c, t["stu"], "Explain gcd of two numbers").json()["doubt"]
    for who in ("chem", "stu"):
        r = c.post(f"/api/doubts/{d['id']}/answer", headers=H(t[who]), json={"answer": "x"})
        assert r.status_code == 403, who
    assert c.post(f"/api/doubts/{d['id']}/answer", headers=H(t["admin"]),
                  json={"answer": "Admin can step in."}).json()["ok"]


def test_answer_feeds_back_into_the_students_knowledge_base(world):
    """The deck's promise: human answers feed back, so escalations fall over time."""
    c, t = world
    q = "Why must the remainder r be smaller than the divisor b?"
    first = c.post("/api/answer", headers=H(t["stu"]), json={"question": q}).json()
    assert first["decision"] == "refuse"            # the student has no material on it yet
    d = ask(c, t["stu"], q, attempt=first["answer"], decision="refuse").json()["doubt"]
    ans = ("The remainder r must be smaller than the divisor b because if r were b or larger "
           "you could subtract b once more and increase the quotient.")
    assert c.post(f"/api/doubts/{d['id']}/answer", headers=H(t["maths"]), json={"answer": ans}).json()["ok"]
    mine = doubts(c, t["stu"])["mine"][0]
    assert mine["status"] == "answered" and mine["answer"] == ans
    again = c.post("/api/answer", headers=H(t["stu"]), json={"question": q}).json()
    assert again["decision"] in ("answer", "answer_uncertain"), again["decision"]
    assert any(s["source"] == "Helper answers" for s in again["sources"])
    # ...and it never shows up as an uploaded file
    assert c.get("/api/material", headers=H(t["stu"])).json()["docs"] == []


def test_helper_answer_stays_in_that_students_material_only(world):
    c, t = world
    other = signup(c, "other@x.com")
    q = "Why must the remainder r be smaller than the divisor b?"
    d = ask(c, t["stu"], q).json()["doubt"]
    c.post(f"/api/doubts/{d['id']}/answer", headers=H(t["maths"]),
           json={"answer": "Because otherwise you could subtract the divisor b again from the remainder r."})
    r = c.post("/api/answer", headers=H(other), json={"question": q}).json()
    assert r["decision"] == "refuse"


def test_feedback_and_withdraw_rules(world):
    c, t = world
    d1 = ask(c, t["stu"], "Explain gcd of two numbers").json()["doubt"]
    assert c.post(f"/api/doubts/{d1['id']}/feedback", headers=H(t["stu"]), json={"helpful": True}).status_code == 400
    c.post(f"/api/doubts/{d1['id']}/answer", headers=H(t["maths"]), json={"answer": "The largest common divisor."})
    assert c.post(f"/api/doubts/{d1['id']}/feedback", headers=H(t["stu"]), json={"helpful": True}).json()["ok"]
    assert doubts(c, t["stu"])["mine"][0]["helpful"] == 1
    assert c.post(f"/api/doubts/{d1['id']}/feedback", headers=H(t["maths"]), json={"helpful": False}).status_code == 404
    d2 = ask(c, t["stu"], "What is a prime number?").json()["doubt"]
    assert c.post(f"/api/doubts/{d2['id']}/close", headers=H(t["maths"])).status_code == 404
    assert c.post(f"/api/doubts/{d2['id']}/close", headers=H(t["stu"])).json()["ok"]
    assert all(d["id"] != d2["id"] for d in doubts(c, t["maths"])["inbox"])
    assert c.post(f"/api/doubts/{d2['id']}/answer", headers=H(t["maths"]), json={"answer": "x"}).status_code == 404


def test_validation_and_open_doubt_cap(world, monkeypatch):
    c, t = world
    assert ask(c, t["stu"], "   ").status_code == 400
    assert c.post("/api/doubts", json={"question": "x"}).status_code == 401
    monkeypatch.setattr(app_module, "MAX_OPEN_DOUBTS", 2)
    ask(c, t["stu"], "q one"); ask(c, t["stu"], "q two")
    assert ask(c, t["stu"], "q three").status_code == 429


def test_admin_endpoints_are_admin_only(world):
    c, t = world
    for who in ("stu", "maths"):
        assert c.get("/api/admin/helpers", headers=H(t[who])).status_code == 403
        assert c.get("/api/admin/stats", headers=H(t[who])).status_code == 403
        assert c.post("/api/admin/helpers", headers=H(t[who]),
                      json={"email": "stu@x.com", "expertise": "x"}).status_code == 403
    hs = c.get("/api/admin/helpers", headers=H(t["admin"])).json()["helpers"]
    assert {h["email"] for h in hs} == {"maths@x.com", "chem@x.com"}
    assert c.post("/api/admin/helpers", headers=H(t["admin"]),
                  json={"email": "nobody@x.com"}).status_code == 404


def test_helper_can_update_own_subjects_students_cannot(world):
    c, t = world
    assert c.post("/api/me/expertise", headers=H(t["maths"]), json={"expertise": "graphs trees"}).json()["ok"]
    assert c.post("/api/me/expertise", headers=H(t["stu"]), json={"expertise": "x"}).status_code == 403
    me = c.get("/api/me", headers=H(t["maths"])).json()
    assert me["role"] == "helper" and me["is_helper"] is True and me["is_admin"] is False
    assert doubts(c, t["maths"])["expertise"] == "graphs trees"      # prefills Settings
    assert doubts(c, t["stu"])["expertise"] == ""


def test_demoting_a_helper_returns_their_open_doubts_to_the_pool(world):
    c, t = world
    ask(c, t["stu"], "Explain gcd of two numbers")
    c.post("/api/admin/helpers", headers=H(t["admin"]), json={"email": "maths@x.com", "helper": False})
    assert doubts(c, t["maths"])["inbox"] == []
    assert len(doubts(c, t["admin"])["inbox"]) == 1


def test_stats_track_questions_refusals_and_doubts(world):
    c, t = world
    c.post("/api/answer", headers=H(t["stu"]), json={"question": "What is photosynthesis?"})
    ask(c, t["stu"], "What is photosynthesis?")
    s = c.get("/api/admin/stats", headers=H(t["admin"])).json()
    this_week = s["weeks"][-1]
    assert this_week["questions"] == 1 and this_week["refusals"] == 1 and this_week["doubts"] == 1
    assert this_week["escalation_rate"] == 1.0 and s["open"] == 1


def test_account_deletion_cleans_up_doubts(world):
    c, t = world
    ask(c, t["stu"], "Explain gcd of two numbers")
    assert c.post("/api/account/delete", headers=H(t["stu"]), json={"password": PW}).json()["ok"]
    assert doubts(c, t["maths"])["inbox"] == []
    # a helper deleting their account sends open doubts back to the admin pool
    stu2 = signup(c, "stu2@x.com")
    ask(c, stu2, "Explain gcd of two numbers")
    c.post("/api/account/delete", headers=H(t["maths"]), json={"password": PW})
    assert len(doubts(c, t["admin"])["inbox"]) == 1


def test_existing_database_is_migrated_so_ids_are_never_reused(app_client):
    """Ravi's live users.db predates AUTOINCREMENT: the one-off migration must keep every
    account (and its password) and start new ids above every id ever referenced."""
    import sqlite3, hashlib
    db = app_client.tmp / "users.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, "
                "pw TEXT, salt TEXT, created REAL)")
    salt = "abcd"
    pw = hashlib.pbkdf2_hmac("sha256", PW.encode(), salt.encode(), 100_000).hex()
    old.executemany("INSERT INTO users VALUES(?,?,?,?,?,?)",
                    [(1, "Ravi", "ravi@x.com", pw, salt, 1.0), (2, "Krishna", "k@x.com", pw, salt, 2.0)])
    old.execute("CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, "
                "email TEXT NOT NULL, created REAL, expires REAL)")
    old.execute("INSERT INTO sessions VALUES('stale', 5, 'gone@x.com', 0, 9e12)")   # id 5 was deleted
    old.commit(); old.close()
    c = app_client
    r = c.post("/api/login", json={"email": "ravi@x.com", "password": PW})
    assert r.status_code == 200, "existing account lost in migration"
    con = sqlite3.connect(db)
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
    assert "AUTOINCREMENT" in sql.upper()
    assert [x[0] for x in con.execute("SELECT email FROM users ORDER BY id")] == ["ravi@x.com", "k@x.com"]
    con.close()
    t = signup(c, "new@x.com")
    me = c.get("/api/me", headers=H(t)).json()
    con = sqlite3.connect(db)
    new_id = con.execute("SELECT id FROM users WHERE email='new@x.com'").fetchone()[0]
    con.close()
    assert new_id > 5, "a new account could inherit references to deleted id 5"
    assert me["email"] == "new@x.com"


# ---------------------------------------------------------- who receives it, before sending
def test_preview_shows_who_it_goes_to_before_sending(world):
    c, t = world
    r = c.post("/api/doubts/route", headers=H(t["stu"]),
               json={"question": "Why is gcd computed by repeated division?"}).json()
    assert r["suggested"]["name"] == "Meera Teacher"
    assert r["suggested"]["subjects"].startswith("number theory")
    assert [h["name"] for h in r["helpers"]][0] == "Meera Teacher"
    assert {h["name"] for h in r["helpers"]} == {"Meera Teacher", "Charles Senior"}
    assert all("email" not in h for h in r["helpers"]), "students never see helpers' emails"
    assert all(doubts(c, t[k])["inbox"] == [] for k in ("maths", "chem", "admin")), \
        "a preview must not send anything"


def test_student_can_pick_a_different_helper_and_only_they_get_it(world):
    c, t = world
    helpers = c.post("/api/doubts/route", headers=H(t["stu"]),
                     json={"question": "gcd question"}).json()["helpers"]
    chem = next(h for h in helpers if h["name"] == "Charles Senior")
    r = ask(c, t["stu"], "gcd question", helper_id=chem["id"]).json()
    assert r["routed_to"] == "Charles Senior" and r["routed_subjects"].startswith("chemistry")
    assert len(doubts(c, t["chem"])["inbox"]) == 1
    assert doubts(c, t["maths"])["inbox"] == [], "a doubt goes to exactly one helper"
    assert doubts(c, t["admin"])["inbox"] == [], "the owner only sees unassigned doubts"
    mine = doubts(c, t["stu"])["mine"][0]
    assert mine["helper_name"] == "Charles Senior" and mine["helper_subjects"].startswith("chemistry")


def test_picking_someone_who_is_not_a_helper_is_refused(world):
    c, t = world
    me = c.get("/api/me", headers=H(t["stu"])).json()
    bad_ids = [999999]
    if isinstance(me.get("id"), int):
        bad_ids.append(me["id"])
    for bad in bad_ids:
        r = ask(c, t["stu"], "gcd question", helper_id=bad)
        assert r.status_code == 400
    assert doubts(c, t["stu"])["mine"] == []


def test_preview_with_no_helpers_says_the_owner_gets_it(app_client, monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_EMAILS", {ADMIN})
    c = app_client
    signup(c, ADMIN)
    stu = signup(c, "s@x.com")
    r = c.post("/api/doubts/route", headers=H(stu), json={"question": "anything"}).json()
    assert r["suggested"] is None and r["helpers"] == [] and r["fallback"] == "the app owner"
    r = ask(c, stu, "anything").json()
    assert r["routed_to"] is None and r["fallback"] == "the app owner"
