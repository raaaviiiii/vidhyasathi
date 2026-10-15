# Phase 2 backend: adversarial QA report (round 3)

**Scope:** the phase-2 backend.
- Ask for Help: `app.py` section "ask for help", `src/doubts.py`, and `src/build_index.py` (`replace_document`, `index_records`).
- The evaluation harness: `src/eval.py`.

I judged it against `qa/SPEC.md` and the phase-2 intent from the brief. I edited no product code and no other test file.

**Tests:** `tests/test_phase2_backend.py` has 37 tests: **13 pass, 24 fail**.
- 3 bugs are parametrised over 3 cases each.
- Every failing test maps to one bug below.
- I ran the file 3 times and got the same result each time.

**Run:** `cd /home/claude/vidhyasathi && QDRANT_URL=http://localhost:6333 python3 -m pytest tests/test_phase2_backend.py -q --timeout=120 -p no:cacheprovider`

**Whole suite:** 370 tests: 345 pass, 25 fail.
- 24 failures are the ones in this file.
- The other failure is `tests/test_frontend.py::test_frontend_suite`. It wraps the node suite, and the frontend tester's new `tests/frontend/phase2.test.mjs` (added 19:47, tests P2F-1 to P2F-14) fails there. That suite was green in my baseline run of 333/333 before the file appeared.
- All earlier backend suites still pass, including `test_doubts.py`, `test_eval.py`, `test_api_security.py` and `test_regression_backend.py`. Nothing regressed.

**Severity:**
- P0 = security, privacy or data loss.
- P1 = a phase-2 promise broken.
- P2 = wrong, but the trigger is narrow or a workaround exists.
- P3 = hardening or polish.

| ID | Sev | Title |
|---|---|---|
| P2B-1 | P0 | Deleting the newest account frees its user id. The next sign-up inherits that helper's answered doubts: it is shown as their helper and, once promoted, can read and rewrite them. |
| P2B-2 | P1 | Two answers for the same student at the same moment wipe ALL of that student's helper answers from their knowledge base |
| P2B-3 | P1 | A helper answer that lands while the student deletes their account is indexed after the wipe. The next owner of the email inherits it. |
| P2B-4 | P1 | The eval sweep replays the gate on a different `top_score` than the live app uses, so its threshold recommendations are wrong for every small knowledge base |
| P2B-5 | P1 | The sweep recommends the LEAST honest setting when no setting reaches 95% refusals |
| P2B-6 | P2 | A student can rate the same answer again and again. The intent says once. |
| P2B-7 | P2 | The 20-open-doubts cap is a check-then-insert race: simultaneous requests all get through |
| P2B-8 | P2 | A helper sees the student's full name when it uses a tab, a no-break space or an ideographic space |
| P2B-9 | P2 | Off-material negatives are never checked against the student's notes, so a biology student's honest answers count as dishonest |
| P2B-10 | P2 | Chunk ids collide across `notes.txt` and `notes.md`, so the eval scores a hit on the wrong document as a perfect retrieval |
| P2B-11 | P2 | Stale set items (the expected passage was deleted) are scored as retrieval misses and drag MRR and Recall down |
| P2B-12 | P3 | A helper answer can plant notes-style citation tags (`[Number Theory · p1]`) inside the student's KB |
| P2B-13 | P3 | Load-spreading has no floor: a busy maths helper's doubts go to the chemistry helper |
| P2B-14 | P3 | Editing an answer resets `answered_at`, which inflates `avg_hours_to_answer` |
| P2B-15 | P3 | A doubt id ≥ 2^63 on answer, feedback or close returns a 500 instead of a 404 |
| P2B-16 | P3 | `eval run`: a malformed `.jsonl` line or a missing set file crashes with a raw traceback |
| P2B-17 | P3 | Vector-only Recall@5 is never computed. The report prints "—". |
| P2B-18 | P3 | `make` on a student whose chunks are all under 120 characters says "No indexed material… Upload notes first" |
| P2B-19 | P3 | The report claims "refuses 0% of off-material" when there are no negatives, and prints "median None s" for an empty set |

---

## P2B-1 (P0): a new account inherits a deleted helper's doubts through user-id reuse

- **Test:** `test_a_new_account_does_not_inherit_a_deleted_helpers_answered_doubts`
- **Repro:**
  1. Helper Latif, the newest account (highest `users.id`), answers student Asha's doubt.
  2. Latif deletes his account. `delete_account` clears `helper_id` only on *open* doubts, so answered doubts keep `helper_id` and `answered_by` = Latif's id.
  3. Mallory signs up. `users` is `id INTEGER PRIMARY KEY` without AUTOINCREMENT, so SQLite reuses the freed max id and Mallory gets Latif's id.
- **Expected:** a deleted helper's identity never passes to someone else. A new account sees nothing.
- **Actual:**
  - Asha's doubt now shows `helper_name: "Mallory Stranger"`.
  - Once Mallory is promoted to helper, her inbox lists Asha's doubt (question, note, attempt and the student's first name).
  - `POST /api/doubts/{id}/answer` passes the `helper_id == u.id` check. Mallory can rewrite the answer, and that rewrite is re-indexed into Asha's notes.
  - The same reuse hits `usage.user_id` and `doubts.student_id` whenever the newest student deletes their account.
- **Fix:**
  - Create the table with `id INTEGER PRIMARY KEY AUTOINCREMENT`. Existing DBs need a one-off migration: create a new table, copy the rows across, rename it.
  - In `delete_account`, also run `UPDATE doubts SET helper_id=NULL WHERE helper_id=?` and `UPDATE doubts SET answered_by=NULL WHERE answered_by=?` for every status. Show "a helper (account removed)".
  - In `answer_doubt`, require `d["status"]=='open' or d["answered_by"]==u.id` for non-admin edits.

## P2B-2 (P1): concurrent answers erase all of a student's helper answers

- **Test:** `test_concurrent_answers_for_one_student_keep_every_helper_answer_indexed`
- **Repro:** two helpers answer two different doubts of the same student at once. Each request calls `index_helper_answers` and then `replace_document`, which writes a new version and then deletes "every other version" of `__helper_answers__`. Each request deletes the other's version.
- **Expected:** both answers are in the student's KB, cited "Helper answers · doubt N".
- **Actual:** the student's KB has **zero** helper-answer chunks. Both requests return `ok`, and the DB says both are answered. The feedback loop silently loses everything until the next answer for that student.
  - The same mechanism hit uploads in REG-B8, which was fixed there with `_doc_lock`. The helper-answers path doesn't take that lock.
- **Fix:** in `answer_doubt`, wrap the DB read of `answered` and the `index_helper_answers` call in `with _doc_lock(d["student_email"], "__helper_answers__"):`. That serialises rebuilds per student, and each rebuild reads the latest answered set inside the lock.

## P2B-3 (P1): a helper answer resurrects chunks for a deleted account

- **Test:** `test_answer_racing_account_deletion_leaves_nothing_for_the_next_owner_of_the_email`
- **Repro:**
  1. The answer UPDATE commits.
  2. The student's account delete runs: chunks are wiped and the doubts deleted.
  3. `index_helper_answers(student_email, answered)` then runs with the list read before the delete, and writes it under `stu@x.com`.
  4. A new person signs up with `stu@x.com` and asks the same question.
- **Expected:** no data survives an account deletion (SPEC: delete account removes chunks). REG-B12 established the same rule for uploads.
- **Actual:** the new account has the old student's `__helper_answers__` chunk in its KB.
- **Fix:**
  - After `index_helper_answers`, re-check that the student row still exists (`_account_exists(d["student_id"])`). If it doesn't, `delete_doc(email, HELPER_DOC)`.
  - Also take the per-student lock from P2B-2 in `delete_account`.
  - The long-term fix (REG-B12) is to tag chunks with an immutable user id, not the email.

## P2B-4 (P1): the eval sweep replays the gate on the wrong score

- **Test:** `test_eval_scores_the_gate_on_the_same_top_score_the_app_uses`
- **Repro:**
  1. A student has 7 chunks, which is under `SMALL_KB_MAX=30`, so it counts as a small KB like most real students.
  2. The best-cosine chunk is short, and the re-ranker prefers longer ones.
  3. The live app (`_retrieve`, small-KB branch) gates on `max(cosine over all chunks)` = 0.707.
  4. `run_set` uses `max(score over ranked[:TOP_K])` after the re-rank = 0.416.
- **Expected:** the sweep "replays the gate at other cut-offs", so each row must carry the score the live gate sees. Otherwise the recommended HELP/STRONG values don't describe the app.
- **Actual:** the two scores disagree. The sweep's answered and refused rates, and the recommended thresholds, are computed on scores the app never uses.
  - `_check` likewise sees only the top-5 re-ranked hits, while the app's check sees the whole small KB.
- **Fix:** call `src.answer._retrieve(q, user=user)` and use its `(hits, top_score)` for `row["top_score"]` and `_check`. Keep `retrieve` and `rerank` only for the MRR comparison. Then the gate and the harness can't drift apart.

## P2B-5 (P1): the sweep recommends the least honest setting when none reaches 95%

- **Test:** `test_sweep_never_recommends_a_less_honest_setting_when_none_reaches_95_percent`
- **Repro:** `ok = [g for g in grid if g["refused_off_material"] >= 0.95] or grid`. When nothing qualifies, the fallback maximises *answered rate* over the whole grid.
- **Expected:** honesty first. The brief requires that the sweep keep ≥95% honest refusals, and the thesis says a wrong-but-confident answer is the worst outcome. Failing that, recommend the most honest setting and say the target wasn't reachable.
- **Actual:** the recommended setting refuses 0% of off-material questions, although 50% was available.
- **Fix:**
  - When nothing qualifies, set `best_ref = max(g["refused_off_material"] for g in grid)` and choose among the settings with that rate.
  - Add `"honesty_target_met": False` to the sweep and print it in `report_md`.

## P2B-6 (P2): a student can rate the same answer repeatedly

- **Test:** `test_student_can_rate_an_answered_doubt_only_once`
- **Repro:** rate `helpful: true`, then `helpful: false` on the same answered doubt.
- **Expected:** the intent says "rate an answered doubt once". The second rating gets a 400 or 409, and the first rating stands.
- **Actual:** 200, and the rating is overwritten. `helpful_rate` in the owner's stats can be flipped at will.
  - It can be argued that changing your mind should be allowed. If so, update the intent. As written, it says once.
- **Fix:** in `doubt_feedback`, run `UPDATE doubts SET helpful=? WHERE id=? AND helpful IS NULL`. If `rowcount == 0`, return `_err(409, "You've already rated this answer.")`.

## P2B-7 (P2): the 20-open-doubt cap races

- **Test:** `test_open_doubt_cap_holds_under_concurrent_requests`
- **Repro:** with the cap set to 2, send 6 simultaneous `POST /api/doubts`. The count runs in one connection, then `route_doubt` embeds (slow), then the insert runs in a second connection.
- **Expected:** at most 2 open doubts.
- **Actual:** 6 open doubts. A script can flood the helpers' inboxes past the cap.
- **Fix:** do the count and the insert in one transaction: `c.execute("BEGIN IMMEDIATE")`, re-count, then insert. Route first, outside the transaction. Alternatively, run `INSERT … SELECT … WHERE (SELECT COUNT(*) …) < ?` and check `rowcount`.

## P2B-8 (P2): helpers can see the student's surname

- **Test:** `test_helpers_see_only_the_first_name_whatever_whitespace_the_name_uses[...]`, with 3 cases.
- **Repro:** sign up with the name `"Asha Kumar"` (a no-break space, common when pasted from a document or typed on some phone keyboards), `"Asha\tKumar"` or `"Asha　Kumar"`, then ask a doubt.
- **Expected:** a helper sees the student's first name only: "Asha".
- **Actual:** the helper sees `"Asha Kumar"`. `_first` uses `.split(" ")`, which splits only on ASCII spaces.
- **Fix:** `parts = (name or "").split(); return parts[0] if parts else "A student"`. With no argument, `str.split()` splits on all Unicode whitespace. Also normalise whitespace in `SignupReq.name`.

## P2B-9 (P2): "off-material" negatives may be in the student's material

- **Test:** `test_make_set_does_not_use_questions_the_students_notes_cover_as_off_material`
- **Repro:** a student with biology notes on photosynthesis and mitochondria runs `make_set(negatives=20)`. The fixed `OFF_MATERIAL` list includes "Explain how photosynthesis works in plants." and "What is the function of mitochondria in a cell?".
- **Expected:** negatives are questions the student's material does NOT cover.
- **Actual:** both are answered correctly, which is right, but they are scored as **failed honest refusals**. That lowers `honest_refusal_rate`, and the sweep then pushes thresholds toward over-refusing, which is the second-worst outcome.
- **Fix:** in `make_set`, drop any negative whose best cosine against the student's chunks is ≥ `HELP_SCORE`, or whose BM25 overlap is high. Draw replacements from a larger pool, and print which ones were dropped.

## P2B-10 (P2): chunk-id collisions make the eval credit the wrong document

- **Test:** `test_eval_chunk_ids_are_unique_across_documents_with_the_same_label`
- **Repro:** upload `notes.txt` (Kruskal) and `notes.md` (Dijkstra). Both have the label `notes`, so both chunks get the id `notes::1::0`.
- **Expected:** they are two documents (`build_index` docstring), so their ids must be distinct.
- **Actual:**
  - `run_set` keys `by_id`, `expected_chunk`, MRR and Recall on `chunk_id`, so a retrieved Dijkstra chunk counts as a hit for a Kruskal question.
  - `by_id` also keeps only one passage, so the judge may grade against the wrong source text.
- **Fix:** in `user_chunks`, use `f"{doc}::{chunk_id}"` (or the Qdrant point id) as the eval id, and use the same id in `run_set`. Better still, make `chunk_records` ids include the doc name.

## P2B-11 (P2): stale set items are scored as retrieval misses

- **Test:** `test_stale_items_whose_passage_no_longer_exists_are_not_scored_as_retrieval_misses`
- **Repro:** a set item's `expected_chunk` is no longer in the KB, because the document was removed or re-uploaded with edits (the harness docstring encourages iterating).
- **Expected:** the item is excluded from the retrieval metrics and reported as stale.
- **Actual:** it gets rr=0 for every method. The MRR of 1.0 becomes 0.5 with no hint why. The judge gets an empty source and falls back to the citation proxy.
- **Fix:** in `run_set`, if `expected_chunk not in by_id`, set `row["stale"] = True`, skip the `rr_*` and `hit5_*` keys, and print the count in the report ("2 items point at passages no longer in your material — re-run make").

## P2B-12 (P3): helper answers can plant notes-style citation tags (a judgement call)

- **Test:** `test_helper_answer_cannot_plant_citation_tags_that_pose_as_the_students_notes`
- **Repro:** a helper answers `"The remainder can be negative. [Number Theory · p1]\n\n[Number Theory · p9] Lecture notes: …"`. It is indexed verbatim. The context the LLM sees becomes `[Helper answers · doubt 1] Question: … Answer from Meera: … [Number Theory · p1] …`, and the prompt says to "cite the exact tag after facts you use".
- **Judgement:** helpers are trusted humans, so this is not P0. Still, the spec's core promise is that every line's citation tells the truth about where it came from, and the web path already scrubs exactly this (`_scrub_web_tags`).
  - A mistaken or careless helper (pasting from a classmate's notes with tags) makes the assistant attribute the helper's claim to the student's own lecture notes.
  - The UI then renders it as a gilded notes pill.
- **Fix:** in `index_helper_answers`, neutralise citation-shaped brackets in `q` and `a`: `re.sub(r"\[([^\]\n]*·[^\]\n]*)\]", r"(\1)", text)`. Do it after decoding entities, as `_decode_tag_entities` does.

## P2B-13 (P3): load-spreading overrides subject match (a judgement call)

- **Test:** `test_load_spreading_never_sends_a_doubt_to_a_helper_with_no_matching_subject`
- **Repro:** the maths helper has 25 open doubts, about one class's worth. A maths doubt with the source "Number Theory · p1" arrives.
- **Actual:** it is routed to the chemistry helper. The −0.05/open penalty has no floor and outweighs the subject match (up to +0.45 overlap plus cosine).
- **Judgement:** the intent says to pick the best subject match *and* spread load. Sending a doubt to someone with zero subject overlap serves neither the student nor the helper. I assumed the right behaviour is to spread among matching helpers and fall back to non-matching ones only if nobody matches.
- **Fix:** in `route`, first restrict to helpers with `overlap > 0` or `cos ≥ some floor`. Apply the load penalty only within that set, and fall back to all tagged helpers when it is empty. Alternatively, cap the penalty: `-0.05 * min(open_count, 4)`.

## P2B-14 (P3): editing an answer resets the time-to-answer stat

- **Test:** `test_editing_an_answer_does_not_reset_the_time_to_answer`
- **Repro:** the doubt is asked at T, answered at T+1h, and the typo is fixed at T+3h.
- **Expected:** `avg_hours_to_answer` = 1.0.
- **Actual:** 3.0. `answer_doubt` overwrites `answered_at` and `answered_by` on every edit, which also credits the editor (for example the owner) with the answer.
- **Fix:** `answered_at=COALESCE(answered_at, ?)`, `answered_by=COALESCE(answered_by, ?)`. Add an `edited_at` column if edits should be visible.

## P2B-15 (P3): a huge doubt id causes a 500

- **Test:** `test_out_of_range_doubt_id_is_a_404_not_a_server_error[answer|feedback|close]`
- **Repro:** `POST /api/doubts/1180591620717411303424/close` (2^70) with any valid token.
- **Expected:** 404 JSON.
- **Actual:** 500 "Something went wrong". sqlite raises `OverflowError: Python int too large to convert to SQLite INTEGER`, which is logged as an unexpected error. Negative, zero, non-numeric and SQL-ish ids are handled fine.
- **Fix:** type the path parameter as `doubt_id: int = Path(..., ge=1, le=2**63 - 1)`, or return 404 in `_load_doubt` when the id is not in `1..2**63-1`.

## P2B-16 (P3): `eval run` crashes on a hand-edited or missing set

- **Tests:** `test_cli_run_reports_a_malformed_set_line_clearly` and `test_cli_run_with_a_missing_set_file_says_how_to_make_one`
- **Repro:**
  - A `.jsonl` line with a trailing comma raises a raw `JSONDecodeError` with no line number. The docstring explicitly tells the student to edit this file by hand.
  - `run` before `make` raises a raw `FileNotFoundError`.
- **Fix:** parse the file line by line. On error, `raise SystemExit(f"{path}:{lineno}: not valid JSON ({e.msg})")`. If the path doesn't exist, `raise SystemExit(f"No test set at {path}. Create one first: python -m src.eval make --user {user}")`. Also validate that each item has a non-empty `question`.

## P2B-17 (P3): vector-only Recall@5 is missing

- **Test:** `test_eval_reports_recall_at_5_for_vector_only_too`
- **Expected:** the intent lists MRR@10 and Recall@5 for pipeline, vector-only and BM25.
- **Actual:** there is no `hit5_vector` or `recall5_vector_only`, and the report table prints "—" in that cell.
- **Fix:** in `run_set`, add `hit5_vector=hit_at(vec_ids, target, 5)`, add `"recall5_vector_only"` to `summarise`, and render it in `report_md`.

## P2B-18 (P3): a misleading "no material" message for short notes

- **Test:** `test_make_set_with_only_short_chunks_does_not_claim_there_is_no_material`
- **Repro:** the student's material is only flashcard-length lines, each chunk under 120 characters.
- **Actual:** `SystemExit("No indexed material for … Upload notes in the app first.")`, but they have uploaded notes.
- **Fix:** distinguish the two cases: "Your N passages are all very short (< 120 characters), so there is nothing to write questions from." Or fall back to merging neighbouring short chunks.

## P2B-19 (P3): the report misreports unmeasured metrics

- **Test:** `test_report_never_prints_none_or_a_fake_0_percent_for_unmeasured_metrics`
- **Repro:**
  - A set with only in-material items: `sweep` coerces `_mean([])` (None) to `0.0`. The report says "Recommended … refuses **0%** of off-material" and "Current … refuses 0%", which are claims about measurements that never happened. `ok = [... >= 0.95] or grid` then falls back to the whole grid as well.
  - An empty set prints "Latency: median None s, 90th percentile None s".
- **Fix:**
  - Keep `None` in `sweep`. Treat None refusal rates as not eligible, and recommend nothing when there are no negatives ("add off-material questions to tune thresholds").
  - In `report_md`, format latency with a helper that prints "—" for None.

---

## Verified working (passing tests in this file)

- **IDOR matrix** (`test_idor_matrix_on_every_doubt_route`). The asking student, another student and the wrong helper all get a 403 on `answer`. Everyone except the asker gets a 404 (no existence oracle) on `feedback` and `close`, and that includes the owner and the assigned helper. Ids -1, 0 and 999999 give 404. Ids `abc`, `1.5` and `1;DROP TABLE doubts` give a 404 or 422 as JSON. No token or a bad token gives 401. Nothing changed state.
- **Demoted and deleted helpers** (`test_demoted_and_deleted_helpers_lose_access`). A demoted helper gets a 403 on answer and `is_helper` false. A deleted helper's token gets a 401. Both helpers' open doubts return to the owner's pool, and the owner can answer them.
- **Answer after the student deleted their account** returns a 404, and no chunks are written. The race case is P2B-3.
- **Helper who is also a student.** Their own doubt is never routed to them, never appears in their inbox, and they can't answer it (403). If they are the only helper, the doubt is unassigned and goes to the owner.
- **Owner who is also a helper.** Routing to the owner works, and the inbox shows it. `/api/me` reports role=helper, is_admin and is_helper. The owner can't answer their own doubt.
- **`/api/me` role flags** are correct for owner, helper and student.
- **Answer editing** re-indexes into one version with no duplicates and keeps one chunk per doubt. The latest text replaces the old. The source is "Helper answers" and the loc is "doubt N". The answer is never listed in `/api/material`, can't be deleted as a file (404), is invisible to other students, and is cited on the next `/api/answer`.
- **Long and Malayalam answers.** An 8000-character Malayalam answer is accepted and chunked into several chunks with unique ids. 8001 characters gives a 400.
- **Privacy.** No doubts payload shows a student's email or surname to a helper or the owner. Students never see helper emails, other students' doubts or other students' names. The `usage` table has exactly `id, user_id, ts, decision`, with no question text. The whitespace exception is P2B-8.
- **Admin endpoints with malformed bodies** (missing or null email, non-boolean `helper`, null expertise, list body, over-long fields, invalid JSON) all return a 400 or 422 JSON error. The email is normalised (`"  MATHS@X.COM "`).
- **Routing edge cases.** Helpers with empty or whitespace-only subjects are skipped while tagged helpers exist. When only untagged helpers exist, the least-loaded one is used. Ties spread load (already covered in `test_doubts.py`).
- **Withdraw and stats.** Withdrawing twice gives a 400, and rating a withdrawn doubt gives a 400. Stats have 8 weeks. A usage row 1 minute past the 7-day boundary lands in the previous week, and rows older than 8 weeks are excluded. Withdrawn doubts count in neither `open` nor `answered`.
- **Eval edge inputs.** A KB with only helper answers gives a clean `SystemExit` from `make` and `run`. A one-tiny-chunk KB runs. Zero positives give `None` metrics, not a ZeroDivisionError. The report renders. The sweep with all-equal scores returns a recommendation.

## Notes on judgement calls

- **P2B-12 and P2B-13** depend on interpretation and are rated P3. P2B-12 assumes helper text should not carry citation tags, by analogy with the web-tag scrub. P2B-13 assumes load-spreading should not override a zero subject match.
- **P2B-6:** I read "rate … once" literally.
- **P2B-1 is P0** because it gives a stranger read and write access to another student's doubts and notes. It only needs the newest account to be deleted, which is a normal event in a small class deployment.
