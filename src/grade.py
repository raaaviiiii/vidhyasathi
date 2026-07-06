"""Exam & Practice \u2014 grading (part 2a: typed answers; the handwriting/vision OCR
front-end is part 2b and will feed text into this same function).
 
Grades a student's answer AGAINST their own material (not against a model-generated
reference, which can be wrong). Retrieve + ground the question, then give structured,
constructive feedback \u2014 Correct / Missing / Incorrect / Suggestion \u2014 citing the source
tags. Deliberately gives NO numeric score or mark: it's feedback vs the notes, not an
official grade. If the topic isn't in the material, it declines to grade rather than
bluffing.
 
Run:  python -m src.grade "State the Division Algorithm"
      (then type/paste the student's answer and press Ctrl-D)
 
      python -m src.grade "State the Division Algorithm" "a = bq + r where 0<=r<b"
      (or pass the answer as the second argument)
"""
from src.answer import _retrieve, _grounded, build_context, ask_llm, REFUSAL
from src.config import HELP_SCORE
 
TOP_K_GRADE = 6
 
GRADE_PROMPT = (
    "You are a supportive tutor giving feedback on a student's answer.\n"
    "You will receive a QUESTION TO GRADE, some REFERENCE MATERIAL from the student's own "
    "notes, and the STUDENT'S ANSWER.\n"
    "Critical rules:\n"
    "1. Grade the student ONLY on the QUESTION TO GRADE. That single line is the entire task.\n"
    "2. The REFERENCE MATERIAL is only for checking correctness. It often contains its own "
    "worked examples or practice problems (for instance, dividing two specific numbers). "
    "Those examples are NOT the question \u2014 completely ignore them and never require the "
    "student to solve them. Never write \"the question asked for ...\" unless that requirement "
    "literally appears in the QUESTION TO GRADE text.\n"
    "3. If the question asks to STATE, DEFINE, or EXPLAIN something, a correct general "
    "statement is a COMPLETE answer. Do not ask for specific numeric calculations unless the "
    "QUESTION itself explicitly asks for them.\n"
    "4. Judge only against the material, never outside knowledge. Give NO numeric score, "
    "grade, mark, or percentage.\n"
    "5. Before listing anything under Missing or Incorrect, re-read the STUDENT'S ANSWER "
    "carefully. Do NOT call a point missing if the student already stated it, even in "
    "different words (for example, if they wrote 'unique q and r', then uniqueness is "
    "present, not missing). Only list genuine gaps and genuine errors.\n"
    "Give feedback under these exact headings, each one or two sentences, citing the bracketed "
    "[source] tag(s) where relevant:\n"
    "Correct: what the student got right.\n"
    "Missing: points REQUIRED to correctly answer THIS question that are absent.\n"
    "Incorrect: anything the student stated that conflicts with the material.\n"
    "Suggestion: one concrete thing to improve.\n"
    "If a heading does not apply, write 'None.' Be honest but encouraging."
)
 
 
def grade(question: str, student_answer: str) -> dict:
    hits, top_score, mode_label = _retrieve(question)
    hits = hits[:TOP_K_GRADE]
    context = build_context(hits)
    grounded = False if top_score < HELP_SCORE else _grounded(question, context)
 
    sources = []
    for h in hits:
        tag = f"{h['source']} \u00b7 {h['loc']}" + (" [OCR]" if h.get("ocr") else "")
        if tag not in sources:
            sources.append(tag)
 
    if not grounded:
        return {"question": question, "grounded": False, "feedback": "",
                "top_score": top_score, "mode": mode_label, "sources": sources}
 
    feedback = ask_llm(
        GRADE_PROMPT,
        f"QUESTION TO GRADE (this single line is the entire task):\n{question}\n\n"
        f"REFERENCE MATERIAL (for checking correctness only; it may contain example "
        f"problems that are NOT the question \u2014 ignore those):\n{context}\n\n"
        f"STUDENT'S ANSWER:\n{student_answer}",
    )
    return {"question": question, "grounded": True, "feedback": feedback,
            "top_score": top_score, "mode": mode_label, "sources": sources}
 
 
if __name__ == "__main__":
    import sys
    from src.store import close
 
    if len(sys.argv) < 2:
        print('usage: python -m src.grade "the question" ["the student answer"]')
        raise SystemExit(1)
 
    question = sys.argv[1]
    if len(sys.argv) >= 3:
        student_answer = " ".join(sys.argv[2:])
    else:
        print("Paste the student's answer, then press Ctrl-D:\n")
        student_answer = sys.stdin.read().strip()
 
    if not student_answer:
        print("(no answer provided)")
        raise SystemExit(1)
 
    result = grade(question, student_answer)
    print(f"\n[{result['mode']}]  grounded: {result['grounded']}  "
          f"(top score: {result['top_score']:.3f})\n")
 
    if not result["grounded"]:
        print(REFUSAL, "\u2014 I can't grade an answer on a topic that isn't in your material.")
    else:
        print("Feedback vs your notes (not an official grade):\n")
        print(result["feedback"])
 
    print("\nsources:", "; ".join(result["sources"]))
    close()
 