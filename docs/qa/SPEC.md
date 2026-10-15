# Vidhyāsathi — Intended Behaviour (QA spec)

Testers judge the app against THIS document, not against what the code happens to do.
Sources: project handoff v2, the zeroth-review deck, and the agreed fix list.

## Thesis
Answers **only from the student's own material**, **cites where every line came from**,
and **refuses honestly** ("I don't have that in the material.") instead of guessing.
A wrong-but-confident answer is the worst possible outcome; a refusal on a covered
topic is the second worst.

## Accounts & isolation
- Sign up (name, email, password) / sign in / sign out. Passwords salted + hashed.
- Each signed-in student sees and queries **only their own uploads**. Another user's
  material must never appear in their answers, material list, practice sets or grading.
- A student must not be able to read, query or delete another student's material or
  account by tampering with requests (the app is shared over a public tunnel link).
- Settings: profile (name+email), default mode, web-fallback default, manage material,
  clear recent chats, change password, delete account (removes account + files + chunks),
  sign out.

## My material
- Upload by click or drag-drop: PDF, DOCX, PPTX, XLSX, CSV, TXT, MD, images (PNG/JPG…).
- Scanned/handwritten pages and images go through OCR and are flagged `ocr`.
- After upload the student is told plainly whether it was indexed, and how many chunks.
- The list shows only that student's files, each marked indexed / not indexed, with
  a remove (✕) button that deletes the file AND its chunks.
- Re-uploading the same file replaces it (no duplicates). Two different files must never
  overwrite each other's chunks.
- Uploaded material persists across server restarts.

## Answer modes
- **Quick Answer**: grounded, cited, short.
- **Teach Me**: multi-turn tutor with memory; teaches one idea at a time; follow-ups like
  "why?" stay on topic; a new off-material topic gets the exact refusal (no deflection).
- **Exam & Practice**: generate N practice Q/A on a topic from the material (each answer
  cited); refuse off-material topics. **Grade my answer**: feedback under Correct / Missing /
  Incorrect / Suggestion against the material, **no marks**; refuse off-material.
- **Broad asks** ("explain all the concepts in the module", "summarise my notes") produce a
  structured, cited overview of what the material covers — but a broad ask about a topic
  that is NOT in the material ("summarise quantum mechanics") must still refuse.

## Confidence gate
- Groundedness is decided BEFORE generation. Score bands (cosine): ≥0.55 strong → no extra
  check; 0.30–0.55 → LLM groundedness check; <0.30 → refuse. Decisions:
  `answer` (≥0.45), `answer_uncertain` (+ offer ask-a-human), `refuse`, `answer_web`.
- Web fallback (toggle) is a LAST resort, only after a material refusal; web answers carry
  only `[web · domain]` tags, a 🌐 disclosure and source URLs — never a notes-style tag.
- OCR nudge "worth double-checking the equation" only on material answers that used an
  OCR'd chunk AND contain a real equation.

## UI
- Login → home with sidebar: New chat, Current chat, Recent chats, My material,
  Practice sets, Trust & citations, Settings. The active nav item reflects what's shown.
- Account chip (top right) opens a menu: email, Settings, My material, Sign out.
- Answers render markdown + KaTeX math, gilded `[source]` citation pills, decision badge,
  score, sources line. Errors are shown in plain language (e.g. "the model isn't running"),
  never a misleading message.
- Untrusted text (model output, web pages, file names, user input) must never execute as
  HTML/JS in the page.
- Works on a phone-width screen.

## Ops
- Run: Qdrant server (Docker) + Ollama (llama3.1:8b) + `uvicorn app:app` + cloudflared.
- When Ollama or Qdrant is down, requests fail with a clear JSON error, not a crash.
