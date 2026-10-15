"""Conversation: Vidhyāsathi should follow a chat the way a person does.

These replay the exact exchanges that failed on the real app:
  "teach me it"                      -> refused (searched literally, score 0.16)
  "explain each concept in detail"   -> refused (score 0.41, strict check said NO)
plus greetings, "ok" during a lesson, "what's on page 2", and the guard rails that
stop a follow-up from dragging an old topic into a new one."""
import pytest

from tests.samples import NUMBER_THEORY
from src.answer import answer, REFUSAL
from src import converse

A = "a@x.com"
GRAPHS = [
    "Kruskal's algorithm builds a minimum spanning tree by repeatedly adding the "
    "cheapest edge that does not form a cycle.",
    "Prim's algorithm grows a minimum spanning tree from a start vertex, always adding "
    "the cheapest edge leaving the tree.",
]
EUCLID_TURN = [
    {"role": "user", "content": "What is the Euclidean algorithm?"},
    {"role": "assistant", "content": "It computes the gcd of two integers by repeated "
                                     "division. [Number Theory · p2]"},
]


def _script_rewrite(fakes, text):
    fakes.llm.respond = lambda kind, msgs: text if kind == "rewrite" else None


# ------------------------------------------------------------ the reported failures
def test_teach_me_it_after_a_quick_answer_teaches_that_topic(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    _script_rewrite(fakes, "Teach me the Euclidean algorithm that computes the gcd")
    r = answer("teach me it", mode="teach", history=EUCLID_TURN, user=A)
    assert r["decision"] in ("answer", "answer_uncertain"), r
    assert r["resolved_question"] == "Teach me the Euclidean algorithm that computes the gcd"
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "teach"]
    assert "gcd" in msgs[-1]["content"]                    # the right notes were found
    assert any(m["role"] == "assistant" for m in msgs)     # and the tutor saw the chat


def test_explain_each_concept_in_detail_walks_the_whole_document(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    index_text(A, "Graphs", GRAPHS)
    hist = [{"role": "user", "content": "Explain all the concepts in my Number Theory notes"},
            {"role": "assistant", "content": "- **Division Algorithm** ... [Number Theory · p1]"}]
    r = answer("explain each concept in detail", mode="teach", history=hist, user=A)
    assert r["decision"] == "answer", r
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "overview"]
    ctx = msgs[-1]["content"]
    for needle in ("Division Algorithm", "Euclidean algorithm", "prime number"):
        assert needle in ctx, f"{needle!r} missing: the whole document should be read"
    assert "Kruskal" not in ctx, "another document leaked into the overview"
    assert fakes.llm.count("ground") == 0


def test_quick_answer_also_follows_the_conversation(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    _script_rewrite(fakes, "Give an example of the Euclidean algorithm computing the gcd")
    r = answer("give me an example of it", mode="quick", history=EUCLID_TURN, user=A)
    assert r["decision"] in ("answer", "answer_uncertain"), r
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "quick"]
    assert msgs[1]["content"] == EUCLID_TURN[0]["content"]  # history reaches the model
    assert "What they mean: Give an example of the Euclidean" in msgs[-1]["content"]


# ------------------------------------------------------------ small talk
@pytest.mark.parametrize("msg", ["hi", "Hello!", "good morning", "thanks", "thank you so much",
                                 "bye", "who are you?", "what can you do"])
def test_small_talk_gets_a_natural_reply_without_searching(fakes, index_text, msg):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer(msg, user=A)
    assert r["decision"] == "chat"
    assert r["answer"] and r["answer"] != REFUSAL
    assert not r["offer_help"] and r["sources"] == []
    assert fakes.llm.count("ground") == fakes.llm.count("quick") == 0


def test_greeting_knows_what_material_the_student_has(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    answer("hi", user=A)
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "chat"]
    assert "Number Theory" in msgs[-1]["content"]


def test_what_files_do_i_have_lists_the_material(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    index_text(A, "Graphs", GRAPHS)
    r = answer("what files do I have?", user=A)
    assert r["decision"] == "chat"
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "chat"]
    assert "Number Theory" in msgs[-1]["content"] and "Graphs" in msgs[-1]["content"]


def test_vague_request_asks_which_topic(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("teach me", mode="teach", user=A)
    assert r["decision"] == "chat"


def test_greeting_with_a_real_question_answers_the_question(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("hi, what is the Division Algorithm a = bq + r", user=A)
    assert r["decision"] in ("answer", "answer_uncertain")


def test_ok_during_a_lesson_continues_the_lesson(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    hist = EUCLID_TURN + [{"role": "user", "content": "teach me the gcd"},
                          {"role": "assistant", "content": "The gcd is the largest integer "
                           "dividing both. Does that make sense? [Number Theory · p2]"}]
    _script_rewrite(fakes, "Continue teaching the gcd and the Euclidean algorithm: the next idea")
    r = answer("ok", mode="teach", history=hist, user=A)
    assert r["decision"] in ("answer", "answer_uncertain"), r
    assert fakes.llm.count("teach") == 1


def test_ok_after_a_quick_answer_is_just_acknowledged(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("ok thanks", mode="quick", history=EUCLID_TURN, user=A)
    assert r["decision"] == "chat"


# ------------------------------------------------------------ recall a slide / page
def test_whats_on_page_two_recites_that_page(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("what's on page 2?", user=A)
    assert r["decision"] == "answer" and r["top_score"] == 1.0
    assert {(s["source"], s["loc"]) for s in r["sources"]} == {("Number Theory", "p2")}


def test_page_in_several_documents_asks_which_one(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    index_text(A, "Graphs", GRAPHS)
    r = answer("what is on page 1", user=A)
    assert r["decision"] == "chat"
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "chat"]
    assert "Graphs" in msgs[-1]["content"] and "which document" in msgs[-1]["content"]


def test_page_of_a_named_document(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    index_text(A, "Graphs", GRAPHS)
    r = answer("read page 2 of my graphs notes", user=A)
    assert {(s["source"], s["loc"]) for s in r["sources"]} == {("Graphs", "p2")}


# ------------------------------------------------------------ guard rails
def test_rewriter_that_invents_a_topic_is_not_trusted(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    _script_rewrite(fakes, "Explain photosynthesis in plants and chlorophyll")
    r = answer("tell me more about it", mode="teach", history=EUCLID_TURN, user=A)
    assert "photosynthesis" not in (r["resolved_question"] or "")


def test_rewriter_saying_chat_gets_a_chat_reply(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    _script_rewrite(fakes, "CHAT")
    r = answer("that was it", mode="teach", history=EUCLID_TURN, user=A)
    assert r["decision"] == "chat"


def test_new_off_material_topic_after_a_chat_still_refuses(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("Explain Kruskal's algorithm", mode="teach", history=EUCLID_TURN, user=A)
    assert r["decision"] == "refuse" and r["answer"] == REFUSAL


def test_refusal_suggests_the_closest_topics(fakes, index_text):
    index_text(A, "Number Theory", NUMBER_THEORY)
    r = answer("What is photosynthesis in prime plants?", user=A)
    assert r["decision"] == "refuse"
    assert r["suggestions"] and all(s["label"] and s["ask"] for s in r["suggestions"])


def test_model_gets_a_large_enough_context_window(monkeypatch):
    import src.answer as ans
    seen = {}

    def fake_chat(model, messages, options):
        seen.update(options)
        return {"message": {"content": "ok"}}
    monkeypatch.setattr(ans.ollama, "chat", fake_chat)
    ans.chat_llm([{"role": "user", "content": "hi"}])
    assert seen["num_ctx"] >= 8192


# ------------------------------------------------------------ the classifier itself
@pytest.mark.parametrize("msg,hist,mode,want", [
    ("teach me it", True, "teach", "followup"),
    ("explain each concept in detail", True, "quick", "followup"),
    ("what about the second one?", True, "quick", "followup"),
    ("why?", True, "quick", "followup"),
    ("next", True, "teach", "continue"),
    ("ok", True, "teach", "yes"),
    ("ahh yes it does", True, "teach", "yes"),
    ("no, I don't get it", True, "teach", "followup"),
    ("i dont get it", True, "teach", "no"),
    ("something different was taught to me in class", True, "teach", "feedback"),
    ("my question about earlier is not fully answered", True, "teach", "feedback"),
    ("are you sure?", True, "quick", "feedback"),
    ("not this, about the satellite communication", True, "teach", "followup"),
    ("ok", True, "quick", "chat"),
    ("hi", False, "quick", "chat"),
    ("teach me", False, "teach", "chat"),
    ("what is twisted pair cable", True, "quick", "question"),
    ("Explain all the concepts in the module", False, "quick", "question"),
    ("what is on slide 4", False, "quick", "question"),
    ("നമസ്കാരം", False, "quick", "question"),
])
def test_classify(msg, hist, mode, want):
    h = EUCLID_TURN if hist else []
    assert converse.classify(msg, h, mode) == want


# ------------------------------------------------------------ real document names
DECKS = {
    "1. INTRODUCTION": ["A network is a set of devices connected by communication links."],
    "3. DATA COMMUNICATION CONCEPTS": [
        "Data communication is the exchange of data between two devices over a medium.",
        "The five components are message, sender, receiver, medium and protocol."],
    "4. GUIDED TRANSMISSION MEDIA": [
        "Twisted pair cable has two insulated copper wires twisted together.",
        "Optical fibre carries signals as light through a glass core."],
}


@pytest.mark.parametrize("ask,want", [
    ("explain each concept of module 3 in detail", "3. DATA COMMUNICATION CONCEPTS"),
    ("Explain each concept of data communication in detail", "3. DATA COMMUNICATION CONCEPTS"),
    ("summarise the data communication concepts deck", "3. DATA COMMUNICATION CONCEPTS"),
    ("give me an overview of guided media", "4. GUIDED TRANSMISSION MEDIA"),
])
def test_overview_finds_the_document_from_how_students_name_it(fakes, index_text, ask, want):
    for label, paras in DECKS.items():
        index_text(A, label, paras)
    r = answer(ask, user=A)
    assert r["decision"] == "answer", r
    assert {s["source"] for s in r["sources"]} == {want}


@pytest.mark.parametrize("raw", [
    'Here is the rewritten question:\n"Teach me guided transmission media"',
    "Here is the standalone question: Teach me guided transmission media",
    "Sure!\nTeach me guided transmission media",
    "Standalone question: Teach me guided transmission media",
    "**Teach me guided transmission media**",
])
def test_rewriter_chatter_is_stripped(raw):
    h = [{"role": "user", "content": "what is guided transmission media"}]
    assert converse.clean_rewrite(raw, "teach me it", h).rstrip(".") == \
        "Teach me guided transmission media"


def test_rewriter_that_answers_instead_of_rewriting_is_ignored():
    h = [{"role": "user", "content": "what is guided transmission media"}]
    long_answer = "Guided media uses cables such as twisted pair and fibre. " * 8
    assert converse.clean_rewrite(long_answer, "teach me it", h) is None



# ------------------------------------------------------------ the chat of 15 Oct, 10:53
SAT = ["Satellites act as big microwave repeaters in the sky. They are inherently broadcast media.",
       "Satellites have excellent error rates and can be deployed almost instantly, a major "
       "consideration for disaster response and military communication."]
ETHER = ["Classic Ethernet uses a bus topology: packets are broadcast over a single cable and "
         "if two machines send at once the packets collide and each waits a random time."]
LESSON = [
    {"role": "user", "content": "teach me how ethernet works"},
    {"role": "assistant", "content": "Ethernet broadcasts over one cable... [Ethernet · p1]"},
    {"role": "user", "content": "why is it that a satellite communication is not owned by one induvidual",
     "meant": "Why is satellite communication not owned by one individual?"},
    {"role": "assistant", "content": "Satellites are like microwave repeaters... Does that make sense? "
                                     "[Satellites · p1]"},
]


def _world(index_text):
    index_text(A, "Satellites", SAT)
    index_text(A, "Ethernet", ETHER)


def _sent(fakes, kind):
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == kind]
    return msgs


def test_yes_it_does_moves_on_instead_of_repeating(fakes, index_text):
    _world(index_text)
    r = answer("ahh yes it does", mode="teach", history=LESSON, user=A)
    assert r["intent"] == "yes" and r["decision"] in ("answer", "answer_uncertain")
    assert fakes.llm.count("rewrite") == 0, "no guessing: 'yes it does' means understood"
    last = _sent(fakes, "teach")[-1]["content"]
    assert "NEXT idea" in last and "Never repeat" in last
    assert "Satellites" in last, "stays on the satellite topic"
    assert "satellite" in r["resolved_question"].lower()


def test_no_explains_the_same_idea_more_simply(fakes, index_text):
    _world(index_text)
    r = answer("i dont get it", mode="teach", history=LESSON, user=A)
    assert r["intent"] == "no"
    assert "more simply" in _sent(fakes, "teach")[-1]["content"]


def test_taught_differently_in_class_is_a_conversation_not_a_search(fakes, index_text):
    _world(index_text)
    r = answer("something different was taught to me in class", mode="teach", history=LESSON, user=A)
    assert r["decision"] != "refuse" and r["answer"] != REFUSAL
    assert r["intent"] == "feedback" and r["offer_help"], "a teacher can settle it: offer Ask a helper"
    msgs = _sent(fakes, "feedback")
    assert "Satellites" in msgs[-1]["content"], "re-checks the notes on the topic being discussed"
    assert any(m["role"] == "assistant" for m in msgs), "sees its own previous reply"


def test_my_earlier_question_means_the_most_recent_one(fakes, index_text):
    _world(index_text)
    r = answer("my question about earlier is not fully answered", mode="teach", history=LESSON, user=A)
    assert r["intent"] == "feedback"
    assert "satellite" in r["resolved_question"].lower() and "ethernet" not in r["resolved_question"].lower()


def test_the_model_sees_its_whole_last_reply(fakes, index_text):
    _world(index_text)
    long_reply = "Satellites relay signals. " * 70 + "[Satellites · p1]"     # ~1.8k chars
    hist = LESSON[:3] + [{"role": "assistant", "content": long_reply}]
    answer("ok next", mode="teach", history=hist, user=A)
    msgs = _sent(fakes, "teach")
    assert any(m["content"] == long_reply for m in msgs), "last reply must not be cut, or it gets repeated"


def test_meaning_of_earlier_messages_reaches_the_rewriter(fakes, index_text):
    _world(index_text)
    answer("explain it with an example", mode="quick", history=LESSON, user=A)
    (_, msgs), = [c for c in fakes.llm.calls if c[0] == "rewrite"]
    assert "meaning: Why is satellite communication not owned by one individual?" in msgs[-1]["content"]


def test_teach_checklist_sits_at_the_end_and_respects_a_request_for_detail(fakes, index_text):
    _world(index_text)
    answer("explain satellites as broadcast media", mode="teach", user=A)
    assert "ONE idea" in _sent(fakes, "teach")[-1]["content"].split("Tutor checklist")[1]
    fakes.llm.calls.clear()
    answer("explain satellites as broadcast media in detail", mode="teach", user=A)
    assert "thorough" in _sent(fakes, "teach")[-1]["content"].split("Tutor checklist")[1]


def test_prompts_forbid_invented_reasons():
    from src.answer import SYSTEM_PROMPT, TEACH_PROMPT
    for p in (SYSTEM_PROMPT, TEACH_PROMPT):
        assert "don't give the reason" in p and "Analogies" in p


# ------------------------------------------------------------ typing slips
OSI = ["The OSI model is composed of seven layers: physical, data link, network, transport, "
       "session, presentation and application.",
       "Satellites act as microwave repeaters in the sky. No single individual owns them."]


@pytest.mark.parametrize("typed,meant", [
    ("explain the osimodel fully", "explain the osi model fully"),
    ("what is the datalink layer", "what is the data link layer"),
    ("what do satallites do", "what do satellites do"),
    ("who owns satellites as an induvidual", "who owns satellites as an individual"),
])
def test_joined_words_and_typos_are_fixed_against_the_notes(fakes, index_text, typed, meant):
    from src.answer import _retrieve
    index_text(A, "Network Models", OSI)
    r = answer(typed, user=A)
    assert r["resolved_question"] == meant
    # the notes match the fixed question better than the typed one
    assert _retrieve(meant, user=A)[1] > _retrieve(typed, user=A)[1]


def test_spelling_fix_never_invents_or_touches_real_words(fakes, index_text, monkeypatch):
    index_text(A, "Network Models", OSI)
    monkeypatch.setattr(converse, "_english", lambda: {"together", "photosynthesis"})
    assert converse.fix_spelling("photosynthesis together", A) == "photosynthesis together"
    assert converse.fix_spelling("kruskal", A) == "kruskal"          # nothing close in the notes
    assert converse.fix_spelling("the four layers", A) == "the four layers"   # not "for"
    assert converse.fix_spelling("OSImodel", A).lower() == "osi model"
    r = answer("explain photosynthesis", user=A)
    assert r["decision"] == "refuse"                                  # still honest


def test_practice_and_grading_also_survive_typos(fakes, index_text):
    from src.exam import generate
    index_text(A, "Network Models", OSI)
    assert generate("the osimodel", n=2, user=A)["grounded"]
