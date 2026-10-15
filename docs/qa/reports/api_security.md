# QA Report — HTTP API, AUTH & SECURITY

App: Vidhyāsathi (FastAPI `app.py`). Shared over a public Cloudflare tunnel, so every
endpoint is treated as internet-facing. Judged against `qa/SPEC.md`.

Test file: `tests/test_api_security.py`
Run: `cd /home/claude/vidhyasathi && QDRANT_URL=http://localhost:6333 python -m pytest tests/test_api_security.py -q`
Result at time of writing: **33 failed, 24 passed** (each failure = one confirmed defect below).

Severity key: P0 = security / data loss / crash on a core flow · P1 = feature broken vs
spec · P2 = wrong with a workaround · P3 = polish.

---

## API-1 — No authentication: identity is a client-supplied field (full cross-user takeover)
- Severity: **P0**
- Failing tests:
  - `test_cannot_list_another_users_material_by_tampering`
  - `test_cannot_query_another_users_material_by_tampering`
  - `test_cannot_exam_on_another_users_material_by_tampering`
  - `test_cannot_grade_against_another_users_material_by_tampering`
  - `test_cannot_delete_another_users_material_by_tampering`
  - `test_core_endpoints_reject_unauthenticated[answer|exam|grade|material/delete]`
  - `test_material_list_requires_auth`, `test_upload_requires_auth`
- Repro: sign up as `mallory@x.com`, then `GET /api/material?user=victim@x.com`, or
  `POST /api/answer {"question": "...", "user": "victim@x.com"}`, or
  `POST /api/material/delete {"name": "important.txt", "user": "victim@x.com"}`.
- Expected (SPEC "Accounts & isolation"): "Each signed-in student sees and queries only
  their own uploads… A student must not be able to read, query or delete another
  student's material or account by tampering with requests." Login should mint a session
  token; the server should derive the user from it and ignore any client `user`.
- Actual: `/api/answer`, `/api/exam`, `/api/grade`, `/api/upload`, `/api/material`,
  `/api/material/delete` all take the user straight from the request body/query
  (`_uid(r.user)` / `user=` param). There is no token, cookie, or session anywhere
  (`/api/login` returns only `{ok,name,email}`). Anyone on the tunnel can read, query,
  generate practice sets from, and **delete** any other student's material by typing
  their email. This is the headline bug and the root cause of API-2.
- Suggested fix: `app.py` — on `login()` issue a signed token (e.g.
  `secrets.token_urlsafe` stored in a `sessions` table / `itsdangerous` signed token);
  add a FastAPI dependency `current_user(authorization: str = Header(...))` that resolves
  the token to an email and 401s when absent/invalid. Change `api_answer`, `api_exam`,
  `api_grade`, `api_upload`, `api_material`, `api_material_delete` to take the user from
  that dependency and **drop** the `user` field from `AnswerReq`/`ExamReq`/`GradeReq`/
  `DeleteMaterialReq` and the `?user=` query param.

## API-2 — `GET /api/users` is unauthenticated and leaks every tester's name + email
- Severity: **P0** (PII disclosure to the whole internet)
- Failing tests: `test_users_endpoint_not_public`, `test_users_endpoint_does_not_leak_emails_unauthenticated`
- Repro: `GET /api/users` with no auth → `{"count": N, "users": [{name, email, joined}, …]}`.
- Expected: the spec defines no "list all users" feature; a public roster of every
  registered name + email on a tunnelled URL is a privacy breach. Endpoint must require
  auth (and arguably not exist / not return others' emails at all).
- Actual: `list_users()` has no auth check and dumps the full table.
- Suggested fix: `app.py` `list_users()` — remove the endpoint, or gate it behind the
  `current_user` dependency (API-1) and return only the caller's own record.

## API-3 — Upload path traversal: `file.filename` is used unsanitised
- Severity: **P0** (arbitrary file write outside the user's folder)
- Failing tests: `test_upload_rejects_parent_traversal_filename`,
  `test_upload_rejects_absolute_path_filename`
- Repro: `POST /api/upload` with filename `../../evil.txt` writes to
  `data/evil.txt` (two levels above the per-user uploads dir); an absolute path
  `/tmp/…/pwned_abs.txt` is honoured verbatim. Confirmed by walking the tmp tree in the
  test: the file lands outside `data/uploads/<hash>/`.
- Expected (SPEC "My material"): the upload is saved "into the user's own folder".
- Actual: `api_upload` does `dest = _user_dir(uid) / file.filename`. `Path(base) /
  "../../evil.txt"` escapes the base; `Path(base) / "/abs/path"` discards the base
  entirely. (Note: `test_upload_rejects_traversal_into_app_code` currently passes only
  because `app.py` sits outside the redirected tmp `BASE`; the write still escapes the
  uploads tree, so treat API-3 as confirmed by the other two tests.)
- Suggested fix: `app.py` `api_upload` — sanitise before use:
  `safe = Path(file.filename or "").name` (as `api_material_delete` already does), reject
  empty/`.`/`..`, then `dest = _user_dir(uid) / safe`; verify
  `dest.resolve().is_relative_to(_user_dir(uid).resolve())`.

## API-4 — Endpoints crash with a raw 500 when the LLM is down
- Severity: **P0** (core flows crash; SPEC "Ops" calls this out explicitly)
- Failing tests: `test_answer_llm_down_returns_clean_json_error`,
  `test_exam_llm_down_returns_clean_json_error`, `test_grade_llm_down_returns_clean_json_error`
- Repro: set `fakes.llm.fail = ConnectionError("ollama down")`, then call
  `/api/answer`, `/api/exam`, `/api/grade` on grounded material → the `ConnectionError`
  propagates out of `ask_llm`/`chat_llm` and FastAPI returns an unhandled 500.
- Expected (SPEC "Ops": "When Ollama or Qdrant is down, requests fail with a clear JSON
  error, not a crash"; SPEC "UI": "Errors are shown in plain language (e.g. 'the model
  isn't running')").
- Actual: no try/except around the model calls in `answer()` / `generate()` / `grade()`
  or their API wrappers; the exception becomes a bare 500 with a stack trace.
- Suggested fix: `app.py` — wrap `api_answer`/`api_exam`/`api_grade` bodies in
  try/except and, on `ConnectionError`/Ollama errors, return
  `JSONResponse({"ok": False, "error": "the model isn't running"}, status_code=503)`;
  or add an exception handler mapping model/connection errors to a clean 503 JSON.

## API-5 — Qdrant outage returns a raw 500 instead of a clean JSON error
- Severity: **P0** (core flow crash; SPEC "Ops")
- Failing test: `test_qdrant_down_returns_clean_json_error`
- Repro: point `src.store._client` at a dead port, then `POST /api/answer` →
  `qdrant_client … ResponseHandlingException: [Errno 111] Connection refused` escapes as
  an unhandled 500.
- Expected: same as API-4 — a clear JSON error, not a crash.
- Actual: `count(user)` / `retrieve()` raise the Qdrant client exception with no handler.
- Suggested fix: same handler as API-4, also catching
  `qdrant_client.http.exceptions` / connection errors → 503 JSON
  "the knowledge base is unavailable".

## API-6 — Blank user is a shared bucket + the app crashes on a blank query to an empty collection
- Severity: **P0** (cross-user data mixing by design + crash)
- Failing tests: `test_blank_user_is_not_a_shared_readable_bucket`, `test_blank_user_query_is_rejected`
- Repro:
  - Upload with `user=""` then `GET /api/material?user=""` returns the file — any other
    blank-user caller (e.g. an unauthenticated visitor) sees and can delete it.
  - `POST /api/answer {"question": "...", "user": ""}` against a collection that doesn't
    exist yet raises `AttributeError: 'str' object has no attribute 'get'` at
    `src/answer.py:204` (an unhandled 500) — reproducible any time a blank/anon request
    hits before any shared upload.
- Expected (SPEC "Accounts & isolation"): material is per-student and private; there is
  no anonymous shared space in the product. A blank/unauthenticated identity must be
  rejected (401), not silently pooled.
- Actual: `_uid("")` → `None`; `None` means "no user filter" → a global shared bucket,
  and the blank-history path also trips a latent crash. Once auth (API-1) lands this
  disappears, but it is called out separately because it is independently reproducible
  today and causes both a leak and a crash.
- Suggested fix: with the API-1 dependency, an empty/missing identity 401s before any
  store access, which closes both halves.

## API-7 — No input validation on question / topic / n / history (crash + waste)
- Severity: **P1** (spec's validation expectations unmet; one path crashes)
- Failing tests: `test_answer_empty_question_rejected`, `test_answer_whitespace_question_rejected`,
  `test_answer_oversized_question_rejected`, `test_answer_malformed_history_items_rejected`,
  `test_exam_n_zero_rejected`, `test_exam_n_negative_rejected`, `test_exam_n_absurdly_large_bounded`
- Repro: `POST /api/answer {"question": ""}` is processed; a 250 KB question is
  processed; `/api/exam {"n": -5}` / `{"n": 100000}` / `{"n": 0}` are accepted and fed
  to the model; `/api/answer {"mode":"teach","history":["not a dict", 42]}` crashes
  because `answer()` does `m.get("role")` on items it assumes are dicts
  (raw 500: `AttributeError: 'str' object has no attribute 'get'`).
- Expected (SPEC "UI"/robustness): untrusted user input is validated; a hostile/empty
  input gets a 400/422, not a model call or a crash. On a public tunnel an unbounded
  question/`n` is a trivial resource-exhaustion vector.
- Actual: `AnswerReq`/`ExamReq` have no constraints; `n` is an unbounded int; `history`
  is a bare `list` with no per-item shape; empty/whitespace questions are not checked.
- Suggested fix: `app.py` — add pydantic constraints:
  `question: constr(strip_whitespace=True, min_length=1, max_length=4000)`,
  `topic` likewise, `n: conint(ge=1, le=50)`, and a typed `history` item model
  (`role: Literal["user","assistant"]`, `content: str`) or validate/skip malformed
  items in `_window_query`/`answer` before `.get`.

## API-8 — Signup accepts non-emails, 1-char passwords, and unbounded names
- Severity: **P1**
- Failing tests: `test_signup_rejects_non_email`, `test_signup_rejects_short_password`,
  `test_signup_rejects_overlong_name`
- Repro: `POST /api/signup {"email":"not-an-email", …}` → 200 ok; `{"password":"a"}` →
  200 ok; `{"name":"Z"*100000}` → 200 ok.
- Expected: real account system (SPEC "Accounts & isolation"). `/api/account/password`
  already enforces `len(new) >= 4`; signup enforces nothing, so the password policy is
  trivially bypassed at account creation. Email identity should be a valid email since
  it is the account key and (today) the retrieval scope.
- Actual: `signup()` checks only that the three fields are non-empty.
- Suggested fix: `app.py` `SignupReq` — `email: EmailStr`,
  `password: constr(min_length=4)`, `name: constr(strip_whitespace=True, min_length=1,
  max_length=100)`.

## API-9 — Unindexable upload reports `ok: true` (misleading success)
- Severity: **P2** (user is told it "saved" when nothing is usable; has a workaround —
  the `indexed:false` flag — but the top-level `ok` contradicts it)
- Failing tests: `test_upload_unsupported_type_reports_failure`, `test_upload_empty_file_reports_failure`
- Repro: `POST /api/upload` with `malware.exe` (unsupported) or `empty.txt`
  (no extractable text) → `{"ok": true, "name": "...", "indexed": false, "error": "..."}`.
- Expected (SPEC "My material": "After upload the student is told plainly whether it was
  indexed"). A failure should be `ok: false` so the client and any API consumer can
  treat it as a failure; `ok:true + indexed:false` is a contradictory signal.
- Actual: `api_upload` hard-codes `"ok": True` on both the "indexing failed" and the
  exception branch. (Orphan-file cleanup itself works — `test_upload_failure_leaves_no_orphan_file`
  passes — so this is purely the misleading status.)
- Suggested fix: `app.py` `api_upload` — return `"ok": res.get("ok", False)` /
  `"ok": False` on the failure and exception branches.

## Notes (not filed as failing tests)
- **CORS `allow_origins="*"`** (`test_cors_wildcard_without_credentials` passes): with
  `allow_credentials` unset (default False), browsers block reading cross-origin
  responses, and identity is currently a request field, so the *browser* CORS risk is
  **LOW today**. It becomes **HIGH** the moment API-1's token/cookie auth lands if `*`
  is kept with credentials — the test guards against that pairing. Recommend pinning
  `allow_origins` to the tunnel origin when auth is added.
- **Login user-enumeration / timing**: `/api/login` returns the same message for unknown
  email vs wrong password (good), but `_hash` is skipped entirely when the email is
  unknown, so a timing side-channel can distinguish them. **P3** — note only.
- **Rate limiting**: none on `/api/login`, `/api/signup`, `/api/answer`, `/api/upload`.
  On a public tunnel this allows brute-force and resource exhaustion. **P3 / ops** —
  note only; recommend a simple per-IP limiter (e.g. slowapi) once auth lands.

---

## What I verified WORKS (24 passing tests)
- Signup → login roundtrip; duplicate email → 409 (`test_signup_then_login_roundtrip`,
  `test_signup_duplicate_email_conflict`).
- Passwords are salted + PBKDF2-hashed, never stored in plaintext
  (`test_password_not_stored_in_plaintext`); wrong login/old-password/account-delete
  password all 401 (`test_login_wrong_password_rejected`,
  `test_change_password_wrong_old_rejected`, `test_account_delete_wrong_password_rejected`).
- Change password then login with the new one works and the old one stops working
  (`test_change_password_then_login_with_new`).
- A user's own material list shows only their own files when each caller uses their own
  identity (`test_material_list_shows_only_own_files`); user B asking about user A's
  topic with B's own identity correctly refuses (`test_answer_never_mixes_two_users_material`).
- Normal uploads land in the per-user folder (`test_uploaded_file_lands_in_user_folder`);
  re-upload replaces rather than duplicating (`test_reupload_same_name_replaces_not_duplicates`);
  two different files coexist (`test_two_different_files_coexist`).
- Upload failure cleans up the orphan file (`test_upload_failure_leaves_no_orphan_file`);
  a corrupt PDF is handled without a 500 (`test_upload_corrupt_pdf_reports_failure`).
- Deleting your own material removes both the file and its chunks (subsequent query
  refuses) (`test_delete_own_material_removes_file_and_chunks`); missing name → 404
  (`test_delete_missing_material_is_404`); **`api_material_delete` already strips path
  components** via `Path(r.name).name`, so delete-traversal is safe
  (`test_delete_with_path_components_does_not_escape`).
- Account delete removes the row, files and chunks, and the email can re-register
  cleanly with no inherited material (`test_account_delete_removes_account_and_allows_resignup`,
  `test_account_delete_purges_material_chunks`).
- `/` serves index.html (`test_index_served`); crafted HTML filenames keep no path
  separators server-side (`test_material_name_with_html_is_not_executable_markup`).
- Missing required `question` field → 422 (`test_answer_missing_question_field_is_422`).
