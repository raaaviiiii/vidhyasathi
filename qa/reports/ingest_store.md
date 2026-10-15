# QA report — Ingestion & Storage

Scope: `src/ingest.py`, `src/chunk.py`, `src/build_index.py`, `src/store.py`, `src/ocr.py` (failure paths only), plus the upload, list and delete endpoints in `app.py` where they decide what happens to ingested data.
Tests: `tests/test_ingest_store.py` (62 tests). Real Qdrant server; fake embedder and OCR.
Run: `QDRANT_URL=http://localhost:6333 python -m pytest tests/test_ingest_store.py -q`
Result: **33 passed, 29 failed.** Every failure is a bug listed below; there are no xfails or skips.

Severity key: P0 = security / data loss / crash on a core flow · P1 = feature broken vs spec · P2 = wrong, but there's a workaround · P3 = polish.

---

## ING-1 · P0 · A crafted upload filename can write anywhere and then delete the file it overwrote (this wiped users.db)
- **Failing tests:** `test_upload_traversal_name_cannot_overwrite_or_delete_app_files`, `test_upload_filename_cannot_escape_user_folder`
- **Repro:** POST `/api/upload` with `filename="../../../users.db"`, any bytes, any user.
  `dest = _user_dir(uid) / file.filename` resolves to `<BASE>/users.db`, so the upload overwrites it. `ingest_file` then returns "unsupported" because `.db` is not a supported type, and the orphan clean-up `dest.unlink()` **deletes users.db**. Every account is gone. The same path writes `.txt`/`.md` files outside the user folder and keeps them; a name like `../../../app.py` overwrites the file and then deletes it.
- **Expected:** SPEC, Accounts & isolation: "must not be able to read, query or delete another student's material or account by tampering with requests (… public tunnel link)".
- **Actual:** an anonymous user can overwrite or delete arbitrary files under the app root.
- **Fix:** in `app.py::api_upload`, use `name = Path(file.filename or "").name`. Reject an empty name, `.`/`..`, or a name starting with `.`. Then check `dest.resolve().parent == _user_dir(uid).resolve()` before writing. `api_material_delete` already does the `.name` guard; apply the same rule to uploads.

## ING-2 · P0 · `delete_source(None, src)` deletes every student's chunks for that document name
- **Failing tests:** `test_delete_source_without_user_does_not_touch_other_students`, `test_upload_with_blank_user_does_not_delete_other_students_material`
- **Repro:** student A uploads `notes.txt`. Then anyone uploads `notes.txt` with `user=""`. `_uid("")` is None, `_source_filter(None, "notes")` drops the user condition, and A's chunks are deleted. A's material list now shows `notes.txt` as **not indexed**.
- **Expected:** SPEC: another student must never be able to "delete another student's material … by tampering with requests"; "Two different files must never overwrite each other's chunks."
- **Fix:** `src/store.py::_source_filter`. When `user` is None, match points whose `user` is null (`IsNullCondition(is_null=PayloadField(key="user"))`) instead of matching everyone. Also, `app.py::api_upload` should reject a blank user rather than treat it as "shared".

## ING-3 · P0 · Two files with the same stem (notes.pdf and notes.docx) share one source, so the second upload deletes the first
- **Failing tests:** `test_same_stem_different_extension_both_kept`, `test_remove_one_same_stem_file_keeps_the_other`
- **Repro:** `ingest_file(notes.pdf)` gives 3 chunks. `ingest_file(notes.docx)` gives 3 chunks, but `count()` is still 3: `source_label` gives "notes" for both files, so `delete_source(user, "notes")` removes the PDF's chunks. Removing `notes.pdf` in the UI also deletes `notes.txt`'s chunks, and `notes.txt` is then listed as *not indexed*.
- **Expected:** SPEC, My material: "Two different files must never overwrite each other's chunks"; ✕ "deletes the file AND its chunks" (only its own).
- **Fix:** key stored chunks on the full filename, not the stem. Add a `file` payload field (`path.name`) in `build_index.ingest_file` and `store.upsert_chunks`, and have `delete_source`/`_source_filter` and `api_material`'s `indexed` check use it. Keep `source_label` for the display and citation tag only (or include the extension when two files share a stem).

## ING-4 · P0 · Point ids come from a lossy slug, so different documents overwrite each other (every Malayalam or Hindi filename becomes "doc")
- **Failing tests:** `test_names_that_slugify_alike_do_not_collide[Notes 1.txt-notes-1.txt]`, `[Notes.txt-notes.txt]`, `[C++ basics.txt-C basics.txt]`, `test_non_ascii_filenames_do_not_collide`
- **Repro:** ingest `Notes 1.txt` and then `notes-1.txt` for the same user. Both slugify to `notes-1`, so the chunk id `notes-1_1_c0` and the point id `uuid5(user:chunk_id)` are the same, and the second file silently replaces the first one's vector, text and source. `ഗണിതം.txt` and `भौतिकी.txt` both slugify to `doc`, so every non-ASCII-named file from a user overwrites the previous one. This matters for students in Kerala.
- **Expected:** SPEC: "Two different files must never overwrite each other's chunks."
- **Fix:** `src/chunk.py::chunk_records`. Build ids from an injective key, for example `f"{sha1(file_name).hexdigest()[:12]}_{order}_c{j}"`, or pass the full filename into `_point_id` in `src/store.py` (`uuid5(NS, f"{user}:{file_name}:{order}:{j}")`). Don't build ids from `_slug`.

## ING-5 · P0 · `build()` wipes every student's uploads
- **Failing test:** `test_build_does_not_wipe_students_uploads`
- **Repro:** a student has indexed material. The operator runs `python -m src.build_index` to re-seed `data/docs`. `reset_collection()` drops the whole collection, so every student's chunks are gone. The upload files stay on disk, and the UI shows them all as "not indexed".
- **Expected:** SPEC: "Uploaded material persists across server restarts"; per-user isolation.
- **Fix:** `src/build_index.py::build`. Replace `reset_collection()` with `delete_user(owner)` when an owner is given. When there is no owner, delete only points whose `user` is null (the same null filter as ING-2). Never drop the collection from the seeding path.

## ING-6 · P1 · OCR failure (no API key or no network) makes the whole PDF fail, and image uploads crash
- **Failing tests:** `test_pdf_with_scanned_page_degrades_when_ocr_fails`, `test_image_when_ocr_fails_returns_error_not_exception`, `test_upload_image_when_ocr_fails_leaves_no_orphan`
- **Repro:** monkeypatch `src.ocr.ocr_image` to raise, then ingest a 2-page PDF (page 1 digital, page 2 scanned). The `RuntimeError` from page 2 escapes `_load_pdf`, so page 1's text is not indexed either. Image uploads raise too; through the API the PNG is left on disk as "not indexed", and the toast shows the raw exception text.
- **Expected:** SPEC: scanned pages "go through OCR" (graceful degradation as briefed); "the student is told plainly whether it was indexed"; "Errors are shown in plain language … never a misleading message".
- **Fix:** in `src/ingest.py::_load_pdf`, wrap the per-page `ocr_image` call in try/except, skip the failed page, and collect a warning such as `ocr_failed_pages`. In `_load_image`, catch the error and return `[]`, or raise a typed `OCRUnavailable`. `ingest_file` should then return `{"ok": False, "error": "OCR is unavailable (check ANTHROPIC_API_KEY / network)"}`, or `ok: True` with a `warnings` list for a partial PDF.

## ING-7 · P1 · Corrupt files raise out of `ingest_file`; the upload leaves an orphan file and shows a raw library error
- **Failing tests:** `test_ingest_corrupt_pdf_returns_error_not_exception`, `test_ingest_corrupt_docx_returns_error_not_exception`, `test_upload_corrupt_file_leaves_no_orphan`
- **Repro:** `ingest_file(broken.pdf)` raises `pymupdf.FileDataError`; a corrupt `.docx` raises `BadZipFile`. In `api_upload`, the `except Exception` branch does not unlink `dest`, so `broken.pdf` stays in "My material" as not indexed. The user sees "Failed to open file '/…/data/uploads/<hash>/broken.pdf' as type pdf", which also leaks the server path.
- **Expected:** SPEC: plain-language errors; "told plainly whether it was indexed"; no orphan (the code's own comment in `api_upload` intends this).
- **Fix:** in `src/build_index.py::ingest_file`, wrap `load_document` in try/except and return `{"ok": False, "error": "couldn't read this file — it may be corrupt or password-protected"}`. In `app.py::api_upload`, also call `dest.unlink(missing_ok=True)` in the `except` branch, and don't echo `str(e)`.

## ING-8 · P1 · Re-uploading is not atomic: old chunks are deleted before the new ones are stored
- **Failing test:** `test_failed_reupload_keeps_previous_version`
- **Repro:** index `notes.txt`, then re-upload it while `upsert_chunks` fails (Qdrant hiccup or timeout). `delete_source` has already run, so the student ends up with neither version (count = 0).
- **Expected:** SPEC: "Re-uploading the same file replaces it." A failed replace must not lose the material.
- **Fix:** in `src/build_index.py::ingest_file`, upsert the new points first and then delete only stale points: `delete_source` with an extra `must_not: HasIdCondition(new_ids)`. Alternatively, stamp a version or `ingested_at` field and delete the older ones. Deterministic ids (once ING-4 is fixed) make the upsert-then-prune approach safe.

## ING-9 · P1 · `count()` and `retrieve()` crash with a 404 when the collection doesn't exist yet (fresh install or a new deploy)
- **Failing tests:** `test_count_when_collection_missing_is_zero`, `test_retrieve_when_collection_missing_is_empty`
- **Repro:** start from an empty Qdrant and call `count("a@x.com")` or `retrieve("what is gcd", user=...)`. Both raise `UnexpectedResponse 404 "Collection … doesn't exist"`. `answer._retrieve` calls `count` first, so a student asking a question before anyone has uploaded gets a crash instead of an honest refusal. (`user_sources`, `delete_source` and `delete_user` already guard this case and pass.)
- **Expected:** SPEC, Thesis: refuse honestly; Ops: "clear JSON error, not a crash".
- **Fix:** in `src/store.py::count`, return 0 when `not client.collection_exists(...)`. In `src/retrieve.py::retrieve`, return `[]` when `total == 0`; it currently uses `limit = top_k` and queries anyway. A cleaner option is an `ensure_collection()` in `store.py` that runs once at app startup.

## ING-10 · P1 · Large documents can't be indexed: a single upsert goes over Qdrant's 32 MB request limit
- **Failing test:** `test_large_textbook_ingests`
- **Repro:** a ~950-paragraph text file gives ~5,000 chunks with dense 384-dim vectors. `upsert_chunks` sends one request of ~45 MB and Qdrant rejects it with `400 "JSON payload … larger than allowed (limit: 33554432 bytes)"`. Probes showed 3,436 chunks still went through and 5,000 failed, so the limit is roughly 3.5k–4.5k chunks, about one full semester textbook PDF. The delete has already run (ING-8), so a re-upload of a large book also loses the old copy.
- **Expected:** SPEC: upload PDF/DOCX/… and be told plainly whether it was indexed. A full textbook is a core input.
- **Fix:** in `src/store.py::upsert_chunks`, send the points in batches of about 256 (`for i in range(0, n, 256): client.upsert(..., points=points[i:i+256], wait=True)`), or use `client.upload_points(..., batch_size=256)`.

## ING-11 · P2 · Concurrent first uploads on a fresh collection race on `create_collection` (409 Conflict)
- **Failing test:** `test_concurrent_first_uploads_on_fresh_collection`
- **Repro:** 8 threads call `ingest_file` while the collection doesn't exist. Each one sees `collection_exists == False` and calls `create_collection`; the losers get `409 Conflict` and their uploads fail. In the app, the user sees the raw error and the file is orphaned (ING-7).
- **Expected:** SPEC, Ops / Qdrant server mode: the reason for moving to server mode was that concurrent writes must not be lost.
- **Fix:** in `src/store.py::upsert_chunks`, wrap `create_collection` in try/except and ignore 409 "already exists", or create the collection once at app startup. *Once the collection exists*, concurrency works: no lost writes and no duplicates (see the verified list below).

## ING-12 · P2 · Text files saved as UTF-16 (Windows Notepad "Unicode") come out garbled, and Latin-1 accents are dropped
- **Failing test:** `test_txt_utf16_with_bom_decodes`
- **Repro:** a UTF-16 LE file with a BOM is indexed as `"T h e E u c l i d e a n …"`, which is useless for retrieval. A Latin-1 `"Café théorème"` becomes `"Caf thorme"` because `errors="ignore"` drops the accented characters. The same `errors="ignore"` decoding is used in `_load_csv`.
- **Fix:** in `src/ingest.py::_load_text` (and `_load_csv`), read the bytes, check for a BOM (`utf-8-sig`, `utf-16`), then try `utf-8` and fall back to `cp1252`/`latin-1`, rather than ignoring errors.

## ING-13 · P2 · A UTF-8 BOM in a CSV (from Excel's "CSV UTF-8" export) ends up in the first header
- **Failing test:** `test_csv_with_utf8_bom_header_is_clean`
- **Actual:** `'﻿Term: gcd; Meaning: …'` on every row. The first column's header becomes a different token, which hurts retrieval and makes citations look broken.
- **Fix:** in `src/ingest.py::_load_csv`, use `encoding="utf-8-sig"`.

## ING-14 · P2 · Ragged CSV rows silently lose data
- **Failing test:** `test_csv_ragged_rows_keep_extra_cells`
- **Repro:** `prime,two divisors,EXTRA-NOTE-KEPT` under a 2-column header. `zip(header, row)` truncates the row, so `EXTRA-NOTE-KEPT` is never indexed.
- **Fix:** in `src/ingest.py::_load_csv` (and `_load_xlsx`), use `itertools.zip_longest(header, row, fillvalue="")` and label extra cells `col{n}`.

## ING-15 · P2 · PPTX text inside grouped shapes is not indexed
- **Failing test:** `test_pptx_text_inside_grouped_shapes_is_captured`
- **Repro:** a slide whose only text box is inside a group shape gives zero records. Grouped content is common in lecture decks (diagrams, SmartArt-like layouts). `_load_pptx` only looks at top-level `slide.shapes`.
- **Fix:** in `src/ingest.py::_load_pptx`, walk shapes recursively (`if sh.shape_type == MSO_SHAPE_TYPE.GROUP: recurse into sh.shapes`). Consider adding speaker notes too (`slide.notes_slide.notes_text_frame`).

## ING-16 · P3 · The chunker emits a redundant final chunk that is wholly contained in the previous one
- **Failing test:** `test_chunk_no_redundant_tail_chunk`
- **Repro:** `chunk_text("y"*1450, 800, 150)` gives chunk lengths `[800, 800, 150]`. The 150-character tail is a substring of chunk 2. The same happens for word text whose length lands just past a window boundary. This wastes a retrieval slot and can double-cite the same passage.
- **Fix:** in `src/chunk.py::chunk_text`, `break` once `end >= len(text)` instead of stepping back by `overlap` and looping again.

## ING-17 · P3 · `chunk_records` emits empty or whitespace-only chunks
- **Failing test:** `test_chunk_records_never_emits_empty_chunks`
- **Repro:** a record with `text=""` or `"   "` produces a chunk with that text, because `chunk_text` returns `[text]` when the text is short. The built-in loaders strip such records first, but the `index_text` path and any future loader don't.
- **Fix:** in `src/chunk.py::chunk_text`, return `[]` when `not text.strip()`, or skip blank pieces in `chunk_records`.

## ING-18 · P3 · `upsert_chunks([])` raises 400 "Empty update request"
- **Failing test:** `test_upsert_empty_list_is_noop`
- **Note:** `ingest_file` currently guards against this, but `build()` with documents that yield no text would hit it after `reset_collection()`.
- **Fix:** in `src/store.py::upsert_chunks`, `if not chunks: return`.

## ING-19 · P3 (flag, no test, API not called) · OCR model ids in `src/config.py::OCR_TIERS`
- `claude-haiku-4-5-20251001`, `claude-sonnet-5` and `claude-opus-4-8` are all listed as **Active** on Anthropic's model-deprecations page (checked 2026-10-14), so none of them is invalid today.
- **Risk:** Haiku 4.5's tentative retirement date is "not sooner than **2026-10-15**", tomorrow. Tier 0 is the first call for every sharp scan, so once that model is retired, every OCR call fails. Combined with ING-6, every scanned PDF and image upload would then fail outright.
- **Fix:** make the tiers configurable from env, catch `anthropic.NotFoundError` in `src/ocr.py::ocr_image` and escalate to the next tier, and re-check ids against the Models API. Also, `OCR_EST_COST` prices are stated estimates and should be re-tuned.

---

## Verified working (passing tests)
- **Chunker:** short text gives one chunk; text of exactly `CHUNK_SIZE` gives one chunk; text with no spaces terminates with bounded chunk sizes; every word is covered and no chunk exceeds `CHUNK_SIZE`; consecutive chunks overlap correctly; chunk ids within a document are unique; `source`, `loc` and `ocr` are preserved per chunk; Malayalam text is chunked safely.
- **PDF:** per-page `p1..pN` locators; an upper-case `.PDF` extension is accepted; a scanned PDF goes through OCR exactly once and is flagged `ocr=True`.
- **PNG:** goes through OCR, flagged `ocr`, locator `image`.
- **DOCX:** headings become locators ("Number Theory"); tables are captured as `table` records.
- **PPTX:** one record per slide, and tables on a slide are captured.
- **XLSX:** all sheets are captured with `"<sheet> row N"` locators; numeric 0 is kept; order is unique across sheets.
- **CSV:** basic header/value rows. **TXT/MD:** loaders work.
- **`source_label`:** normalisation (underscores and runs of spaces become one space).
- **Empty or whitespace files, and unsupported types:** a clear `ok: False` error and no orphan via the API.
- **`ingest_file`:** the result shape and chunk count match `count()`; the payload has `chunk_id, user, source, loc, order, text, ocr`.
- **Re-upload:** re-uploading the same file for the same user replaces it with identical deterministic ids and no duplicates; re-uploading a shorter version drops stale chunks.
- **Isolation:** the same file for two users has disjoint point ids and separate counts; `delete_user` removes only that user; `delete_user("")` and `delete_user(None)` are no-ops; `delete_source(user, src)` removes only that user's source.
- **Missing collection:** `user_sources`, `delete_source`, `delete_user` and `/api/material` work before any collection exists.
- **Scale:** `user_sources` paginates correctly past 256 points (300 sources).
- **Concurrency (collection already exists):** 12 threads ingesting for two users while another thread counts: no errors, no lost writes, and the exact per-user counts and sources. 6 threads uploading the same file at once: no duplicates.
- **`build(owner)`:** indexes pdf + docx + csv under the owner.
