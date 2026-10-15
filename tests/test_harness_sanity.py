"""Proves the harness itself works before the testers rely on it."""
from src.answer import answer


def test_grounded_answer_roundtrip(fakes, index_text):
    index_text("a@x.com", "Number Theory", ["The Division Algorithm: a = bq + r with 0 <= r < b."])
    r = answer("State the Division Algorithm", user="a@x.com")
    assert r["decision"] in ("answer", "answer_uncertain")
    assert "[Number Theory" in r["answer"]


def test_off_material_refuses(fakes, index_text):
    index_text("a@x.com", "Number Theory", ["The Division Algorithm: a = bq + r with 0 <= r < b."])
    r = answer("Explain photosynthesis in plants", user="a@x.com")
    assert r["decision"] == "refuse"


def test_app_serves_index(app_client):
    assert app_client.get("/").status_code == 200
