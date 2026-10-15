# Backend regression + adversarial report (round 2)

**Scope:** the fixed backend, judged against `qa/SPEC.md`: `app.py`, `src/store.py`, `src/build_index.py`, `src/ingest.py`, `src/answer.py`, `src/exam.py`, `src/grade.py`, `src/errors.py`. The focus is code the fix round added and nobody has attacked yet:
- session tokens and login lockout
- the temp-file upload path and file names
- store versioning
- broad-ask topic extraction and teach-mode rescue gating
- exam and grade post-processing
- the `guarded` error mapping
- concurrency

**Tests:** `tests/test_regression_backend.py` has 98 tests: **57 pass, 41 fail**. Every failing test maps to one bug below. The suite gives the same result over repeated runs, with and without `-p no:randomly`.

**Run:** `cd /home/claude/vidhyasathi && QDRANT_URL=http://localhost:6333 python -m pytest tests/test_regression_backend.py -q`

**Whole suite:** `QDRANT_URL=http://localhost:6333 python -m pytest -q` gives 311 tests: 269 pass, 42 fail.
- 41 failures are the ones in this file.
- The other failure is `tests/test_frontend.py::test_frontend_suite`. It wraps `tests/frontend/regression.test.mjs`, which the front-end tester added at 17:26 (REG-F1 to REG-F17, see `regression_frontend.md`). That suite passed in my baseline run before the file appeared.
- All earlier backend suites (`test_engine`, `test_ingest_store`, `test_api_security`, `test_harness_sanity`) still pass. I edited no product code and no other test file.

**Severity:** P0 = security, data loss or a crash on a core flow · P1 = feature broken against the spec · P2 = wrong, but a workaround exists, or a narrow trigger · P3 = polish or hardening.

| ID | Sev | Title |
|---|---|---|
| REG-B1 | P1 | Chunks indexed before the upgrade (no `doc`/`ver`) show as "not indexed", survive Remove, and are duplicated by a re-upload |
| REG-B2 | P1 | Exam and Grade on a small KB get the first 6 chunks in document order, not the chunks about the topic |
| REG-B3 | P1 | Malayalam and Hindi words are cut to fragments, so a broad ask on an off-material topic gets an answer and a teach-mode topic switch is deflected |
| REG-B4 | P1 | Ordinary wordings of "summarise my notes" refuse ("Could you…", "…in bullet points", "…for tomorrow's exam") |
| REG-B5 | P1 | Ordinary teach follow-ups refuse ("explain that in simpler terms", "give me a real-life example") |
| REG-B6 | P1 | A long Malayalam file name (over 255 bytes) returns a 500 and leaves live, invisible, undeletable chunks |
| REG-B7 | P2 | `_safe_name` is not idempotent: a file named ". notes.txt" can't be removed, and removing it deletes a *different* file |
| REG-B8 | P2 | Two simultaneous uploads of the same name delete both versions, leaving the file "not indexed" |
| REG-B9 | P2 | The multipart body is spooled to disk before auth and before the size limit: anonymous disk-fill |
| REG-B10 | P2 | Login lockout is per email only: anyone can lock a classmate out of their own account |
| REG-B11 | P2 | `/api/account/password` gives unlimited old-password guesses to whoever holds a session |
| REG-B12 | P2 | An upload in flight while the account is deleted leaves chunks that the next owner of the email inherits |
| REG-B13 | P2 | A failure while retiring the old version leaves BOTH versions live (with a 503 to the student) |
| REG-B14 | P2 | `build()` wipes the bucket before re-embedding, so a failed rebuild loses the bucket |
| REG-B15 | P2 | `_strip_marks` deletes legitimate feedback lines ("17/5 gives q = 3", "Mark the 2 conditions") |
| REG-B16 | P2 | `_strip_marks` lets real grades through ("**Grade:** B+", "Overall: 85% correct.") |
| REG-B17 | P2 | Exam accepts any bracket as a citation ("[0, b-1]", or a tag for a source the student doesn't have) |
| REG-B18 | P2 | "summarise module 2" summarises everything (digits and scope words are dropped from the topic) |
| REG-B19 | P2 | Teach rescue matches by substring: "what about an ion?" passes because "ion" is inside "division" |
| REG-B20 | P3 | A successful login doesn't reset the failure count |
| REG-B21 | P3 | The `_failures` table never forgets stale keys (unbounded growth) |
| REG-B22 | P3 | A login racing account deletion mints a session the next registrant of that email inherits |
| REG-B23 | P3 | Bidi-override and zero-width characters survive in file names |
| REG-B24 | P3 | "summarise my graphs notes" covers only one of "Graphs 1" / "Graphs 2" |
| REG-B25 | P3 | `_scrub_web_tags` misses entity-encoded notes tags (`&#91;Notes · p1&#93;`) |
| REG-B26 | P3 | Endpoints without `guarded` (login, me, logout, password) fail as text/plain 500 |
| REG-B27 | P3 | `/api/health` says the LLM is up when Ollama runs but llama3.1:8b isn't pulled |
| REG-B28 | P3 | Removing a file while its re-upload is indexing: the upload says indexed:true, the list says "not indexed" |

---

## REG-B1 (P1): Pre-upgrade chunks are orphaned: listed as not indexed, not removed, duplicated
- **Tests:** `test_pre_versioning_chunks_are_listed_and_deletable`, `test_reupload_over_pre_versioning_chunks_does_not_duplicate`
- **Repro:** this is a live deployment. `users.db` has 2 accounts, and `data/uploads/9bbd64eba2e2f40c/` holds "1. INTRODUCTION.pptx" and "2. NETWORK MODELS.pptx". The previous release stored points with `chunk_id, user, source, loc, order, text, ocr`, and with no `doc` or `ver` field (ingest_store.md, "verified"). To reproduce, insert such a point for `a@x.com` and place `nt.txt` in the student's folder.
- **Expected (SPEC "My material"):** existing uploads keep working after the upgrade. The list shows them as indexed. ✕ removes the file **and its chunks**. A re-upload replaces the old copy, with no duplicates.
- **Actual:**
  - `user_docs()` reads only `doc`, so every pre-upgrade file shows **"not indexed"**.
  - `delete_doc` filters on `doc`, so Remove deletes the file but **the chunks keep answering questions**.
  - `_drop_other_versions` matches `doc=…`, so a re-upload leaves the old chunks beside the new ones (count 2 instead of 1).
- **Fix:** run a one-time migration at startup (or as a CLI step). For every point where `doc` is empty, take its user's folder `_user_dir(user)` and find the file whose `source_label(Path(f)) == payload.source`, then `set_payload({"doc": f.name, "ver": "legacy"})`. If the match is ambiguous, delete the points and re-ingest that folder. Short of a migration, also match on `source` when the `doc` field is empty, in `delete_doc` and `_drop_other_versions` (`should=[doc==name, (IsEmpty(doc) AND source==label)]`).

## REG-B2 (P1): Exam and Grade on a small KB use the first 6 chunks in document order
- **Tests:** `test_exam_small_kb_context_contains_the_topic_not_just_the_first_six_chunks`, `test_grade_small_kb_context_contains_the_topic`
- **Repro:** index "Algebra" (10 chunks) and "Graphs" (1 chunk: Kruskal). That is 11 chunks, so the small-KB mode applies. Call `generate("Kruskal spanning tree", 3)`, or call `grade(...)` with the same topic.
- **Expected (SPEC "Exam & Practice"):** N practice Q/A *on the topic from the material*. Grading is *against the material*.
- **Actual:**
  - In small-KB mode `_retrieve` returns **every** chunk in `_doc_order`, sorted by source name.
  - `exam.py:79` (`hits[:TOP_K_GEN]`) and `grade.py:76` (`hits[:TOP_K_GRADE]`) then keep Algebra p1–p6.
  - The gate passes, because `top_score` is the max over all chunks. The model then gets **no passage about Kruskal**. It writes questions on Algebra, or grades the student against unrelated text.
  - Most students have ≤30 chunks, so this is the common case, not an edge case.
- **Fix:** in both files, keep the top chunks by `score` before truncating: `hits = sorted(hits, key=lambda h: h["score"], reverse=True)[:TOP_K_GEN]`, then `_doc_order` them again for readability. Alternatively, add a `focused=True` flag to `_retrieve` that skips the small-KB "full context" path.

## REG-B3 (P1): Non-Latin scripts are cut to fragments, which breaks the honesty gates
- **Tests:** `test_broad_ask_about_a_non_latin_off_material_topic_refuses[summarise ക്വാണ്ടം മെക്കാനിക്സ്]`, `[summarise भौतिकी]`, `test_teach_new_non_latin_topic_refuses`
- **Repro:** with Number Theory only, ask `answer("summarise ക്വാണ്ടം മെക്കാനിക്സ്")`. In teach mode, ask "ക്വാണ്ടം മെക്കാനിക്സ്?" after a Division Algorithm turn.
- **Expected (SPEC Broad asks, Teach Me):** "summarise quantum mechanics" refuses, and a new off-material topic gets the exact refusal. This must hold in Malayalam or Hindi too, since this audience studies in both.
- **Actual:**
  - `answer.py:108` defines `_WORD = r"[^\W_]+"`. Python's `\w` excludes the Indic vowel signs and virama (categories Mn/Mc), so "ക്വാണ്ടം മെക്കാനിക്സ്" becomes `[]` and "भौतिकी" becomes `[]`.
  - Broad ask: the topic comes out empty, so it is treated as "summarise my notes". It is **answered** with an overview of the Number Theory notes.
  - Teach: `_new_topic_words` returns the empty set, so `all(...)` is vacuously true. The window rescue then **answers about the Division Algorithm** (the deflection the spec forbids).
  - Other words survive only as fragments ("ഊർജ്ജതന്ത്രം" gives "ഊർജ", "ജതന"). That one refuses by luck.
- **Fix:** tokenise on letters *plus* marks, e.g. `re.compile(r"[^\W\d_][\ẁ-ͯऀ-෿‌‍]*")`, or split on whitespace and strip characters whose `unicodedata.category` is not `L*`, `M*` or `N*`. Then guard both callers: when the question contains letters but tokenises to nothing, treat it as "unknown topic" (gate it), never as "no topic". In `_material_lookup`, never run `all()` over an empty set as a pass.

## REG-B4 (P1): Common wordings of "summarise my notes" refuse
- **Test:** `test_ordinary_phrasings_of_summarise_my_notes_get_the_overview` (5 cases)
- **Repro:** with Number Theory indexed, ask any of these:
  - "Could you summarise my notes?"
  - "Hey, give me an overview of my notes"
  - "Summarise my notes in bullet points"
  - "Summarise my notes for tomorrow's exam"
  - "Kindly summarise my material"
- **Expected (SPEC Broad asks):** a structured, cited overview. Refusing a covered request is the second-worst outcome.
- **Actual:** `_broad_topic` keeps every word not in `_STOP`/`_SCOPE`, so the topics become "could", "hey", "bullet points", "tomorrow exam" and "kindly". These are gated as subjects, they don't ground, and the request **refuses**. The first round's fix for ENG-4 over-corrected.
- **Fix:** extract the subject only from the *object* of the broad phrase, and treat purpose and format tails as no subject:
  - "summarise X", "overview of X", "concepts in/of X": the topic is X only.
  - If X starts with a scope phrase ("my notes", "my material", "the module"), the request covers the whole material regardless of trailing "for …", "in … points", "in simple words".
  - Add politeness and format words to the ignored set: could, would, can, kindly, hey, hi, please, bullet, points, short, brief, simple, words, quick, exam, test, revision, tomorrow.

## REG-B5 (P1): Ordinary teach follow-ups refuse
- **Test:** `test_teach_ordinary_contextless_followups_stay_on_topic` (3 cases)
- **Repro:** teach mode, after a Division Algorithm turn, send "Can you explain that in simpler terms?", "Give me a real-life example", or "I'm confused, can you go slower?".
- **Expected (SPEC Teach Me):** follow-ups like "why?" stay on topic. The spec names a "teaches one idea at a time" tutor, so these are its bread-and-butter turns.
- **Actual:** `_new_topic_words` treats terms, real, life, confused and slower as *new topics*. They are not in the context, so the rescue is blocked and the student gets **"I don't have that in the material."** in the middle of a lesson.
- **Fix:** block the rescue only when the question really introduces a subject:
  - (a) Use a large general-English stop/discourse list (a few hundred words, e.g. the NLTK list plus tutoring verbs), not the 60-word `_FOLLOWUP`.
  - (b) Treat a word as a candidate topic only if it is ≥4 letters and absent from both the context *and* the history.
  - (c) Refuse only when the candidates also *don't* ground on their own. Keep the existing tests `test_teach_new_off_material_topic_gets_exact_refusal` and `…what_about_offtopic…` green.

## REG-B6 (P1): A long Malayalam file name gives a 500 and leaves orphan chunks
- **Test:** `test_overlong_unicode_name_never_leaves_orphan_chunks`
- **Repro:** upload `"ഗ"*140 + ".txt"`. That is 144 characters, so it passes `_safe_name`'s 150-character cap, but it is 424 UTF-8 bytes.
- **Expected (SPEC "told plainly whether it was indexed"):** either it is indexed and listed, or it is refused cleanly with nothing left behind.
- **Actual:** `ingest_file` indexes the chunks under that name. Then `os.replace(tmp, d/name)` raises `OSError(36, 'File name too long')`, and the response is a generic **500**. The chunks are **live**: they answer questions, never appear in the list (no file), and cannot be removed. Any Malayalam title over roughly 85 characters triggers this.
- **Fix:** in `_safe_name`, cap by **bytes**: `while len(name.encode()) > 240: shorten the stem`. Also do the rename *before* the index is committed, or wrap `os.replace` and call `delete_doc(u.email, name)` (roll back the new version) when it fails.

## REG-B7 (P2): `_safe_name` is not idempotent, so Remove can delete the wrong file
- **Tests:** `test_weird_names_round_trip_upload_list_delete[.  spaced.txt-None]`, `test_deleting_a_space_prefixed_file_never_deletes_a_different_file`, `test_safe_name_is_idempotent`
- **Repro:** upload `notes.txt`, then `".  notes.txt"`. The second one is stored as `"  notes.txt"`, because `.strip().strip(".")` strips the dot after the spaces. Remove `"  notes.txt"` from the list.
- **Expected:** the listed file is removed and nothing else is.
- **Actual:**
  - The delete endpoint runs `_safe_name` again, which turns `"  notes.txt"` into `"notes.txt"`.
  - Without a `notes.txt` on disk, the request is a 404, and the file can never be removed.
  - With a `notes.txt` on disk, **the other file and its chunks are deleted**, and the space-prefixed one stays.
  - `"notes.txt ."` gives `"notes.txt "` and fails the same way.
- **Fix:** loop until the name is stable: `while True: n2 = n.strip().strip("."); if n2 == n: break; n = n2`. Then assert `_safe_name(_safe_name(x)) == _safe_name(x)`. In `/api/material/delete`, compare the request name to the listed names exactly, and return 404 if it is not one of them, rather than sanitising and guessing.

## REG-B8 (P2): Two simultaneous uploads of one name delete both versions
- **Test:** `test_two_simultaneous_uploads_of_the_same_name_keep_exactly_one_version`
- **Repro:** two `/api/upload` calls for `notes.txt` (a double-drop, or two tabs). Both versions are written, and then each request's `_drop_other_versions` deletes "everything that isn't mine", which is the *other* version.
- **Expected (SPEC "re-uploading replaces it"):** one version stays indexed, matching the file on disk.
- **Actual:** **both** versions are gone. Both responses say `indexed: true`, and the list says *not indexed*.
- **Fix:** serialise ingest per `(user, doc)` with a `threading.Lock` held in a dict in `app.py` (one uvicorn worker). Or make retirement monotonic: store `ver` as a sortable timestamp-plus-uuid and delete only versions *older* than your own, so the newer write always survives.

## REG-B9 (P2): The upload body is read and spooled before auth and before the size check
- **Tests:** `test_unauthenticated_upload_is_refused_before_the_body_is_read`, `test_oversized_upload_is_refused_before_the_whole_body_is_spooled`
- **Repro:** POST 20 MB of multipart to `/api/upload` with no token, or with a token and `MAX_UPLOAD_MB=1`, driving the ASGI app directly.
- **Expected:** the app is on a public tunnel, so a stranger's body is refused without being stored, and the size limit protects the disk.
- **Actual:**
  - FastAPI parses `UploadFile = File(...)` (Starlette spools to a temp file after 1 MB) **before** `Depends(current_user)` returns 401, and before the loop's 413.
  - All 20 MB were read in both cases.
  - Anyone with the link can fill `/tmp` with concurrent anonymous posts, up to Cloudflare's 100 MB body cap per request.
- **Fix:** take `request: Request` instead of `UploadFile`. Authenticate first (call `current_user(request.headers.get("authorization",""))`). Reject when `Content-Length > limit`. Then stream `request.stream()` through a multipart parser (`python-multipart`'s `MultipartParser`) straight into the `.tmp-*` file, with the byte cap enforced as it streams. Separately, set uvicorn's `--limit-max-requests`, or cap request size at the proxy.

## REG-B10 (P2, trade-off): Anyone can lock a classmate out
- **Test:** `test_attacker_cannot_lock_a_student_out_from_another_client`
- **Repro:** from client A, 12 wrong logins for `victim@x.com`. Then from client B, the victim logs in with the right password.
- **Expected (defended):** the victim gets in. The lockout must slow guessers, not hand anyone a 15-minute "lock this account" button. In a class where everyone knows everyone's college email, that button is trivial to abuse.
- **Actual:** **429** for 15 minutes, renewable forever by the attacker.
- **Fix:** key the counter on `(email, client IP)` and use `CF-Connecting-IP` behind the tunnel. Keep a much higher per-email ceiling (e.g. 100/hour) as a global brake. **Judgement noted:** `test_lockout_after_ten_failures_blocks_even_the_right_password` (passes) defends that, from the *same* client, a correct password is also refused once locked. Otherwise the lockout does nothing against a guesser.

## REG-B11 (P2): Unlimited old-password guesses via change-password
- **Test:** `test_old_password_guessing_via_change_password_is_rate_limited`
- **Repro:** using a session, 15 calls to `/api/account/password` with wrong `old_password`.
- **Expected:** rate-limited like login. Someone holding a session (a shared lab PC, a stolen token) must not be able to brute-force the password. A hit lets them change it and **sign the owner out of every other device**.
- **Actual:** 15× 403, never 429. `/api/account/delete` (password-gated) has the same gap.
- **Fix:** run `_too_many_failures(f"pw:{u.email}")` / `_record_failure` in `change_password` and `delete_account`.

## REG-B12 (P2): An upload racing account deletion leaves chunks for the next owner
- **Test:** `test_upload_racing_account_deletion_leaves_no_material_for_the_next_owner`
- **Repro:** an upload is past auth and is indexing. The account is deleted from another tab, which removes the chunks, the folder and the sessions. The upload's upsert then lands. Someone later signs up with the same email and asks a question.
- **Expected (SPEC "delete account removes account + files + chunks"):** nothing of the old account survives.
- **Actual:**
  - The chunks are written after `delete_user`, so `count(a@x.com) == 1`.
  - The upload itself then 500s (`FileNotFoundError`, because its folder vanished).
  - The **new registrant of the email can query the deleted user's document**.
- **Fix:** take a per-user lock across `api_upload` and `delete_account`. Or, after `ingest_file`, re-check that the user row still exists and `delete_doc` if it doesn't. Better still, tag chunks with an immutable `user_id` (the users PK, or a random uid), not the email. Then a re-registered email is a different owner by construction. This also fixes REG-B22.

## REG-B13 (P2): A failure while retiring the old version leaves both versions live
- **Test:** `test_failure_while_retiring_old_version_does_not_leave_both_versions`
- **Repro:** the new version is upserted, and then the `_drop_other_versions` delete fails (a Qdrant blip).
- **Expected:** one version live, as the "atomic re-upload" docstring promises.
- **Actual:** `ingest_file` raises, so the upload returns 503 ("Qdrant isn't reachable") and the old file stays on disk. But **old and new chunks are both live** (count 2), and the student gets contradictory citations from the version they think failed.
- **Fix:** in `ingest_file`, wrap `_drop_other_versions` in `try/except`. On failure, `_drop_version(user, doc, ver)` and re-raise, so the old version stays the only one. Alternatively, keep a `live_ver` per doc and filter retrieval on it, so a pointer swap is the commit.

## REG-B14 (P2): `build()` wipes the bucket before rebuilding
- **Test:** `test_build_that_fails_midway_keeps_the_previous_bucket`
- **Repro:** seed the shared bucket with 2 docs. Rebuild with the embedder failing on doc 2.
- **Expected:** "only that bucket is replaced". A failed rebuild leaves the previous bucket, like uploads do.
- **Actual:** `delete_bucket(owner)` runs first (`build_index.py:105`). After the crash, 1 of 2 chunks remains, so the bucket is half-gone. With `--owner a@x.com` it is a student's uploads.
- **Fix:** embed and upsert everything under a new `ver` first. Only then `_delete(Filter(must=[_user_cond(owner)], must_not=[ver==new]))`. On an exception, drop the new `ver` and re-raise.

## REG-B15 (P2): `_strip_marks` deletes real feedback
- **Test:** `test_strip_marks_keeps_legitimate_feedback_lines`
- **Repro:** run `_strip_marks` on these lines:
  - "Correct: You found that 17/5 gives q = 3 and r = 2. [Number Theory · p1]"
  - "Suggestion: Mark the 2 conditions on r explicitly, e.g. for 100/10."
- **Expected (SPEC):** feedback under Correct / Missing / Incorrect / Suggestion, with no marks. The headings themselves must survive.
- **Actual:** **both lines are deleted whole.**
  - `\b\d+/\s*(5|10|…)\b` matches the division example "17/5" and "100/10".
  - `\b(?:score|marks?)\b[^\n]*\d` matches the verb "Mark … 2".
  - The student loses "Correct" and "Suggestion" entirely. This is exactly the Division-Algorithm material the app is demoed on.
- **Fix:** strip only *mark-shaped* fragments, never whole heading lines:
  - Drop a line only if it *starts* with `(score|marks?|grade|rating|result)\s*[:=-]`, allowing leading `**`/`#`/`-`.
  - Inside the other lines, remove only the matched fragment when it is a standalone score: "8/10" followed by end/space/punctuation and preceded by "score|got|give|earn|award|mark(s)" within a few words.
  - Never touch a line that starts with one of the four headings unless the fragment is that specific shape.

## REG-B16 (P2): `_strip_marks` misses common grade formats
- **Test:** `test_strip_marks_catches_common_mark_formats`
- **Repro:** feedback containing "**Grade:** B+" or "Overall: 85% correct.".
- **Expected (SPEC):** **no marks**, and the module docstring calls this "a promise".
- **Actual:**
  - Both survive. The line-start alternative `^\s*(?:score|…)` sits inside `^.*(?:…)`, and it fails on markdown bold (`**Grade:**`).
  - Percentages are matched only after "score|marks".
  - Letter grades are never matched.
- **Fix:** allow `^[\s*#>_-]*` before the keyword. Add `\b\d{1,3}\s*%` (when the line has no other heading content), and add `\bgrade\b[^\n]*\b[A-F][+-]?\b`. Write the cases above, plus REG-B15's, as a table-driven unit test.

## REG-B17 (P2): Exam treats any bracket as a citation
- **Test:** `test_exam_any_bracket_is_not_a_citation`
- **Repro:** the model returns three items: one whose answer ends "r lies in [0, b-1].", one tagged "[Physics · p9]" (the student has no Physics), and one correctly tagged.
- **Expected (SPEC "each answer cited"):** only the third survives.
- **Actual:** all three are kept. `_TAG = r"\[[^\]\n]+\]"` accepts any bracket, so an uncited answer with an interval, or a hallucinated source, passes as "cited".
- **Fix:** accept only the tags that were in the context: `allowed = {f"[{h['source']} · {h['loc']}]" for h in hits}`, and keep an item only if `any(t in it["a"] for t in allowed)`. To tolerate variants, normalise whitespace and `**` first, and also accept `[A · p1; A · p2]` by splitting on `;`. `test_exam_keeps_legit_citation_variants` already covers the variants and must stay green. It passes today, and it guards the case the brief asked about: no, a legitimate answer with bold or next-line tags is *not* dropped.

## REG-B18 (P2): "summarise module 2" summarises everything
- **Test:** `test_summarise_module_2_covers_module_2_not_everything`
- **Repro:** index "Module 1" and "Module 2", then ask "summarise module 2".
- **Expected:** an overview of Module 2.
- **Actual:** `_broad_topic` drops "module" (a scope word) and "2" (a digit), so the topic is empty. The whole material is summarised, with Module 1 citations mixed in. "summarise chapter 3" behaves the same way.
- **Fix:** before stripping, try `_match_document` on the raw question words *including* scope words and digits ("module 2" ⊆ the words of "Module 2"). Only fall back to the stripped topic if no document matches.

## REG-B19 (P2): The teach rescue matches new topics by substring
- **Test:** `test_teach_new_topic_that_is_a_substring_of_a_context_word_refuses`
- **Repro:** after a Division Algorithm turn, ask "And what about an ion?".
- **Expected (SPEC):** a new off-material topic gets the exact refusal.
- **Actual:** `answer.py:362`, `all(w in ctx_low for w in new)`, is a substring test. "ion" is found inside "divis**ion**", the rescue runs, and the tutor answers about the Division Algorithm (deflection). Short topics hit this often: "ion" in "division", "port" in "support", "tree" in "street", "ram" in "program".
- **Fix:** `ctx_words = set(_words(c2))`, then `all(w in ctx_words for w in new)`. Optionally add light stemming (strip a trailing s/es).

## REG-B20 (P3): A successful login doesn't reset the failure count
- **Test:** `test_successful_login_resets_the_failure_count`
- **Actual:** 9 typos, then a success, then 1 typo, then the right password gets **429**. A student who mistypes often gets locked out despite signing in fine in between.
- **Fix:** `with _fail_lock: _failures.pop(email, None)` on a successful login.

## REG-B21 (P3): The failure table grows without bound
- **Test:** `test_failure_table_forgets_stale_entries`
- **Actual:** `_too_many_failures` prunes only the key being checked, so 2,000 stale keys stay after another login. A script posting random emails grows memory forever.
- **Fix:** on every call (or every N calls), drop the keys whose newest timestamp is older than `FAIL_WINDOW`. Or use a bounded `OrderedDict` or LRU.

## REG-B22 (P3): A login racing account deletion leaves a session for the next owner
- **Test:** `test_login_racing_account_deletion_leaves_no_session_for_the_next_owner`
- **Repro:** device 2's login has verified the password while device 1 deletes the account. `_new_session(email)` then inserts a session for an email with no user. Someone re-registers that email.
- **Actual:** device 2's token authenticates as the **newcomer**. `current_user` joins on email, so the token works.
- **Fix:** bind sessions to the immutable `users.id`, not the email (`sessions.user_id`, `JOIN users u ON u.id = s.user_id`). Then a re-registered email is a different id. Also do the password check and the insert in one transaction.

## REG-B23 (P3): Bidi and invisible characters survive in file names
- **Test:** `test_bidi_and_invisible_characters_are_stripped_from_names`
- **Actual:**
  - `"lab‮fdp.md"` is stored and listed. It renders as "labdm.pdf", an extension spoof in the material list.
  - `"notes​.txt"` is listed next to a visually identical "notes.txt".
- **Fix:** in `_safe_name`, also remove `[​-‏‪-‮⁦-⁩﻿]` (and NFC-normalise the name).

## REG-B24 (P3): A name shared by two documents covers only one
- **Test:** `test_summarise_a_name_shared_by_two_documents_covers_both`
- **Actual:** "summarise my graphs notes" with "Graphs 1" and "Graphs 2" keeps only the shortest/first label, so half of what was asked for is silently left out.
- **Fix:** have `_match_document` return *all* labels whose words ⊇ the topic. Make `retrieve` accept `sources: list[str]` (`MatchAny`).

## REG-B25 (P3): The web scrub misses entity-encoded notes tags
- **Test:** `test_scrub_removes_entity_encoded_notes_tags`
- **Actual:** `&#91;Number Theory · p1&#93;` survives `_scrub_web_tags`. marked renders it to "[Number Theory · p1]" in a text node, and `gildInto` makes it a **notes** citation pill on a web answer. Web pages are untrusted, so a prompt-injected page can cause this.
- **Fix:** `text = html.unescape(text)` before the scrub. Also ask the front-end to gild only after the decision is known, never gilding non-web tags on `answer_web`.

## REG-B26 (P3): Unguarded endpoints fail as text/plain
- **Test:** `test_unguarded_endpoints_also_fail_as_json`
- **Actual:** with `sqlite3.OperationalError('database is locked')`, `/api/login` and `/api/me` return `500 Internal Server Error`, text/plain. The UI shows the generic status text instead of a message (SPEC Ops: "a clear JSON error, not a crash").
- **Fix:** add a global `@app.exception_handler(Exception)` that returns `_err(500, …)` (and `_err(503, …)` for connection errors). Or apply `@guarded` to signup, login, me, logout and change_password.

## REG-B27 (P3): Health ignores the model
- **Test:** `test_health_reports_llm_down_when_the_model_is_not_pulled`
- **Actual:** `ollama.list()` succeeding is enough for `llm: true`. If llama3.1:8b isn't pulled, the pill says Active while every question 503s.
- **Fix:** `llm = any(m.get("model", m.get("name","")).startswith(LLM_MODEL) for m in ollama.list()["models"])`.

## REG-B28 (P3): Remove during a re-upload gives inconsistent state
- **Test:** `test_delete_during_reupload_leaves_a_consistent_state`
- **Repro:** re-upload `notes.txt`. While it indexes, remove `notes.txt` in another tab.
- **Actual:** the delete removes all chunks (including the new version) and the file. Then the upload's `os.replace` puts the new file back. The response says `indexed: true`, but the list shows **not indexed**.
- **Fix:** use the same per-(user, doc) lock as REG-B8 for `api_material_delete`. Or re-check `user_docs` before replying `indexed: true`.

---

## Verified working (passing tests in this file)
- **Sessions:**
  - Signup and login return distinct, working tokens.
  - Logout revokes only that token, and a second logout is a clean 401.
  - Expired sessions get a 401 and are purged on the next login.
  - Change-password keeps the current device, signs out the account's other devices (never another user's), and the old password stops working.
  - Account delete kills every session. Re-registering the email revives none of them and inherits no files or chunks.
  - The stored `token_hash` is not usable as a token.
- **Malformed auth:** 11 `Authorization` variants all get a clean JSON 401 on `/api/me`, `/api/material` and `/api/logout`: empty, `Bearer` with no token, Basic, `Token`, `Bearerabc`, a two-part value, 20 KB, SQL quote, and non-ASCII.
- **Email case:** `"  Asha@X.com "`, `asha@x.COM` and `ASHA@x.com` are one identity. The duplicate gets a 409, there is one upload folder, and the chunks are tagged with the lower-cased email.
- **Lockout:** it triggers after 10 failures, refuses even the right password from the same client (defended above), and is keyed case-insensitively.
- **Upload limits:** the size cap returns a JSON 413 with "MB", keeps the old file and chunks, and leaves no `.tmp-*`. A file exactly at the limit is accepted.
- **Failed re-uploads:**
  - A re-upload that fails to index (empty text) keeps the old file and the old chunks, which still answer.
  - A re-upload with Qdrant down gets a 503 with the Qdrant message and keeps everything, with no temp files.
- **Listing:** the material list hides `.tmp-*`, dotfiles and sub-directories.
- **File names:**
  - Malayalam and Devanagari names round-trip (upload, list, remove).
  - `.tmp-x.txt` is stored as `tmp-x.txt` and cannot collide with temp files.
  - A trailing dot and a 300-character ASCII name are handled.
  - Control characters are stripped.
  - Names that differ only in case are independent documents.
  - "....", "/", `..\..\` and blanks get a 400.
- **Store:**
  - `replace_doc` swaps versions, and a mid-write failure leaves the old version alone.
  - `user=None` touches only the shared bucket. `delete_source(None, …)`, `delete_bucket(None)`, `delete_user(None|"")` never reach a student.
  - `delete_doc` matches the exact file name (`notes.txt` vs `notes.md`). `delete_source` matches the label.
- **Engine:**
  - "summarise my introduction pptx" covers only "Introduction" when that document exists, and refuses when it doesn't.
  - The web scrub keeps `[web · …]`, intervals and `arr[i]`, and removes notes tags.
  - Exam keeps citation variants (bold tags, a tag on the next line, a multi-tag bracket).
  - Exam `n = MAX_N + 1` gets a 422, and `n = MAX_N` yields exactly MAX_N items.
- **Errors:**
  - LLM down gives a 503 with the Ollama message on answer, exam and grade.
  - An unexpected engine exception gives a JSON 500 without internals.
  - Account delete with Qdrant down gives a 503 and keeps the account and files.
  - `/api/material` with Qdrant down gives a 503.
  - Validation: a bad *value* (empty, whitespace, over-long question) gets a 400. A missing field, wrong type, bad enum, or `system` role in history gets a 422. No model call is made in any of these cases.
  - `/api/health` is public and returns only `{ok, qdrant, llm}`.
