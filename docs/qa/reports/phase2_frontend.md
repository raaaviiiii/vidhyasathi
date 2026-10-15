# Front-end adversarial report: Phase 2 "Ask for Help" (`index.html`)

**Scope:**
- The new Phase-2 UI: the "Ask a helper" button and dialog, My doubts, Helper inbox, helper/owner Settings sections, and badges and role data from `GET /api/doubts`.
- A regression pass over chat, material, Settings and Recent chats with Phase 2 present.

The UI is judged against `qa/SPEC.md` and the Phase-2 UI intent.

**Harness:** `tests/frontend/harness.mjs`, which runs JSDOM with the real marked.js and a mocked fetch.

**New tests:** `tests/frontend/phase2.test.mjs` has 25 tests.
- 14 tests are bug probes, named `P2F-n`.
- 11 tests cover behaviour that was verified to work.

**Run:** `cd /home/claude/vidhyasathi && NODE_PATH=/tmp/node_modules node --test tests/frontend/`

**Result:** 179 tests: **165 pass, 14 fail**.
- All 154 pre-existing tests still pass.
- Of the 25 new tests, 11 pass and 14 fail. Each failure is one bug below.

| ID | Sev | Title |
|---|---|---|
| P2F-1 | P1 | A second 401 erases `lastAccount`, so the next account sees the previous student's chat (REG-F5 is back). The owner's Settings triggers it every time. |
| P2F-2 | P2 | A double-click on "Send to a helper", or re-asking while a send is pending, creates duplicate doubts |
| P2F-4 | P2 | After a 401, the next account inherits the previous role, Helper inbox, badges and owner tools |
| P2F-5 | P2 | A send that finishes after the dialog was dismissed closes whatever panel is open now; a late failure is silent |
| P2F-3 | P3 | Restoring a chat from Recent forgets that its doubt was sent, so the button is live again |
| P2F-6 | P3 | The owner's ✕ (remove helper) has no confirmation |
| P2F-7 | P3 | "Was this helpful?" can post both Yes and Not really |
| P2F-8 | P3 | Focus is lost after sending a doubt (the opener is disabled first) and after rating an answer |
| P2F-9 | P3 | The Ask dialog highlights "My doubts" as the current page |
| P2F-10 | P3 | A withdrawn doubt also reads "→ waiting for a helper" |
| P2F-11 | P3 | "My helper subjects" is never prefilled, so saving overwrites or wipes the helper's subjects blindly |
| P2F-12 | P3 | The attempt is cut at 8000 UTF-16 units, so half an emoji gives a server 422 and the doubt cannot be sent |
| P2F-13 | P3 | With 7 nav items the sidebar can't scroll, so Settings and Sign out are clipped on short screens |
| P2F-14 | P3 | After a helper sends an answer, the inbox card still says "waiting" and "Send answer" |

---

## Bugs

### P2F-1 — P1 — A second 401 erases who was signed in, so the next account sees the previous chat
- **Failing test:** `P2F-1: a second 401 …`
- **Repro, student path:**
  1. Asha asks a question. Her session expires.
  2. She asks again, so the answer is pending.
  3. She clicks My doubts. `/api/doubts` returns 401, `sessionExpired()` runs, and `lastAccount = 'asha@x.com'`.
  4. The pending answer then returns 401 as well. `sessionExpired()` runs again, now with `USER_EMAIL === ''`, so `lastAccount = ''`.
  5. Bala signs in. `doAuth`'s guard `if(lastAccount&&lastAccount!==who)` is false, so his screen shows Asha's whole transcript. His teach history and his next save then carry her conversation (the same leak as REG-F5).
- **Repro, owner path (no race needed):**
  1. The owner's token has expired and the owner opens Settings.
  2. `renderHelperSettings` awaits `refreshDoubts()`. That call gets a 401 and returns `null` (it swallows the error).
  3. The function carries on with the stale `ROLE.is_admin === true` and calls `load()`.
  4. `load()` fires `GET /api/admin/helpers` with **no** token, because the session was already cleared. That gets a second 401 and a second `sessionExpired()`.
  5. Whoever signs in next sees the owner's chat.
- **Why Phase 2 makes this likely:** it adds background requests that overlap with user actions: `refreshDoubts()` after sending, after answering, at boot and when Settings opens.
- **Expected:** the account-switch reset still runs. Never send an authenticated request with no token.
- **Actual:** the previous student's chat is shown to, continued by and saved under the next account.
- **Fix:**
  - In `sessionExpired`, use `if(USER_EMAIL)lastAccount=USER_EMAIL;`. Better still, return early when there is no session (`if(!USER_TOKEN)return;`).
  - In `api()`, when `auth` is requested and `!USER_TOKEN`, throw `ApiError(401, …)` without calling `fetch`.
  - In `renderHelperSettings`, `return` when `await refreshDoubts()` is `null`.

### P2F-2 — P2 — Double Send creates duplicate doubts
- **Failing test:** `P2F-2: double-clicking "Send to a helper" …`
- **Repro:**
  - Click `#hsend` twice. Two `POST /api/doubts` requests go out.
  - Or: click Send, press Escape, and click "Ask a helper" again. The button is still enabled while the first request is pending, so Send posts again.
- **Expected:** one doubt. The helper gets one item. The 20-open-doubt limit and the escalation stats are not inflated.
- **Actual:** two doubts.
- **Fix:** in `openAskHelper`'s Send handler:
  - Set `hsend.disabled = true` (and `btn.disabled = true`) before the `await`.
  - Re-enable both only in the `catch`.
  - Also have `openAskHelper` refuse to open while `btn.dataset.sending` is set.

### P2F-4 — P2 — After a 401, the next account inherits the previous role, inbox and badges
- **Failing test:** `P2F-4: after a 401, the next account does not inherit …`
- **Repro:**
  1. The owner or a helper (2 open inbox items, 1 new answer) gets a 401.
  2. A student, Bala, signs in on the same tab. His first `/api/doubts` call fails (503 or a network error).
- **Actual:**
  - "Helper inbox" stays visible with the previous user's count "2", and the My doubts badge stays too.
  - Settings renders "Helpers & escalations (owner)" for Bala from the stale `ROLE`.
  - Even when Bala's refresh succeeds, the stale state is shown until it resolves.
- **Fix:**
  - In `sessionExpired`, and in `doAuth` before `applyUser`, reset `ROLE={role:'student',is_helper:false,is_admin:false}` and `doubtsCache={mine:[],inbox:[]}`, then call `updateBadges()`.
  - In `renderHelperSettings`, render nothing if `refreshDoubts()` failed.

### P2F-5 — P2 — A late send result acts on the wrong panel
- **Failing test:** `P2F-5: a doubt that finishes sending after the dialog was dismissed …`
- **Repro 1:**
  1. Click Send. While it is pending, press Escape and open My material.
  2. The send succeeds. The handler calls `closeModal()` unconditionally, so My material shuts under the student.
- **Repro 2:**
  1. Click Send, then press Escape.
  2. The send fails (for example, 429 "You already have 20 open doubts").
  3. The error is written into `#hmsg`, which is now hidden. The student is never told.
- **Fix:** in `openAskHelper`, keep a reference to the dialog node (`const dlg=document.getElementById('hsend')`).
  - On success, call `closeModal()` only if `document.getElementById('hsend')===dlg` and the overlay is shown.
  - On failure, if the dialog is gone, `toast("Couldn't send your doubt: "+e.message)`.

### P2F-3 — P3 — Restoring a chat from Recent forgets that its doubt was sent
- **Failing test:** `P2F-3: a doubt already sent …`
- **Repro:**
  1. Send a doubt from a tentative answer. The button shows "✓ Sent to Meera Teacher" and is disabled.
  2. Click New chat, then restore that chat from Recent (or reload and restore).
  3. The button reads "🙋 Ask a helper" and is enabled again.
- **Judgement:** the intent leaves the restored state to the tester. The sensible behaviour is to keep the sent state, because a live button invites a duplicate doubt to the same teacher. The state is only on the DOM button, never in `chatLog`.
- **Fix:** on success, set `r.sent_to = res.routed_to || 'a helper'`. `r` is the same object stored in the `chatLog` entry. Then call `saveCurrentChat()` if the chat is still the current one. In `answerNode`, render the button disabled with "✓ Sent to …" when `r.sent_to` is set.

### P2F-6 — P3 — Removing a helper has no confirmation
- **Failing test:** `P2F-6: the owner's ✕ (remove helper) asks to confirm …`
- **Repro:** in the owner's Settings, click ✕ on a helper row. The request posts immediately.
- **Why it matters:** this demotes the teacher, erases their subjects (the server sets `expertise=''`) and sends their open doubts back to the pool. The material ✕, which is less destructive, does ask first.
- **Fix:** in `renderHelperSettings`, start the ✕ handler with `if(!confirm('Remove '+x.name+' as a helper? Their open doubts go back to the unassigned pool.'))return;`.

### P2F-7 — P3 — "Was this helpful?" can be answered twice
- **Failing test:** `P2F-7: …`
- **Repro:** click Yes, then Not really, before the first request returns. Both `POST /feedback` requests are sent. The server keeps the last one, and the UI says "Thanks".
- **Fix:** in `openDoubts`, disable both feedback buttons inside the click handler before the `await`, and re-enable them in the `catch`.

### P2F-8 — P3 — Focus is lost after Send and after rating an answer
- **Failing test:** `P2F-8: focus is not dropped …`
- **Repro 1:**
  1. Focus "Ask a helper", press Enter, then Send.
  2. The handler disables `btn` *before* `closeModal()`. A disabled button cannot take focus, so `modalOpener.focus()` fails.
  3. Focus stays on ✕ inside the now-hidden overlay; in a real browser it falls to `<body>`.
- **Repro 2:** in My doubts, pressing Yes replaces the focused button with text, so focus falls to `<body>`.
- **Fix:**
  - In `openAskHelper`, call `closeModal()` first, then disable the button and move focus to the answer bubble or the next focusable control. Alternatively, keep the button focusable with `aria-disabled="true"` instead of `disabled`.
  - In feedback, set `tabindex=-1` on the "Thanks" span and focus it.

### P2F-9 — P3 — The Ask dialog highlights "My doubts"
- **Failing test:** `P2F-9: …`
- **Repro:** open the Ask dialog over the chat. `openModal(…, 5)` marks My doubts `.on` with `aria-current="page"`.
- **Fix:** in `openAskHelper`, call `openModal(…, -1)` (or `baseNav`).

### P2F-10 — P3 — A withdrawn doubt says "waiting for a helper"
- **Failing test:** `P2F-10: …`
- **Repro:** a doubt with `status:'closed'` and `helper_name:null` shows the chip "withdrawn" next to "→ waiting for a helper".
- **Fix:** in `openDoubts`, show the meta text only for open doubts: `d.helper_name?('→ '+d.helper_name):(d.status==='open'?'→ waiting for a helper':'')`.

### P2F-11 — P3 — "My helper subjects" overwrites or wipes subjects blindly
- **Failing test:** `P2F-11: …`
- **Repro:** a helper opens Settings. The field is always empty, because neither `/api/doubts` nor `/api/me` returns `expertise`.
  - Saving it as-is posts `expertise:''`. The helper becomes "untagged" and stops being matched to doubts.
  - Typing one new subject replaces all the old ones without the helper seeing them.
- **Fix:**
  - Backend: return `expertise` in `GET /api/doubts` (or `/api/me`).
  - In `renderHelperSettings`, prefill `inp.value`.
  - Also refuse, or confirm, an empty save.

### P2F-12 — P3 — Truncating the attempt can split an emoji, so the send fails
- **Failing test:** `P2F-12: …`
- **Repro:**
  1. A tentative answer longer than 8000 UTF-16 units has an emoji or other non-BMP character at the cut.
  2. `String(r.answer).slice(0,8000)` leaves a lone surrogate.
  3. Pydantic rejects it: "Input should be a valid string, unable to parse raw data as a unicode string". This was checked against the real `DoubtReq`.
  4. The student cannot send the doubt at all.
- **Fix:** in `openAskHelper`, cut by code points (`Array.from(s).slice(0,8000).join('')`), or drop a trailing high surrogate after slicing.

### P2F-13 — P3 — The sidebar is clipped on short screens
- **Failing test:** `P2F-13 (static CSS): …`
- **Repro:** the sidebar is about 545px tall: brand, New chat, 7 nav items, Settings footer, gaps and padding. `.sidebar` is a fixed-height flex column with no `overflow`.
  - On a 390px-tall landscape phone, or a desktop window with the on-screen keyboard up, the footer (Settings, which holds Sign out) sits below the fold and cannot be scrolled to.
  - The problem existed before but was marginal. The two new items add about 80px.
- **Fix:** add `overflow-y:auto` to `.sidebar`, in both the base rule and the phone rule.

### P2F-14 — P3 — The inbox card goes stale after an answer is sent
- **Failing test:** `P2F-14: …`
- **Repro:**
  1. A helper answers an open doubt. `refreshDoubts()` runs, and the badge drops to 0.
  2. The card still shows the "waiting" chip and the "Send answer" button.
- **Fix:** after a successful post in `openInbox`'s send handler, set `d.status='answered'; d.answer=a;` and update the chip (`c.querySelector('.st')`) and `send.textContent='Update answer'`. Alternatively, re-render the list.

---

## Verified working (passing tests in `phase2.test.mjs`)

- **XSS.** All of these stay inert text:
  - question, attempt and helper answer as markdown, including `javascript:` links, `class="overlay show"` and KaTeX `\href`
  - note, helper_name, student_name and the sources array
  - `routed_to` (button label and toast) and the prefilled `#hq`
  - the owner list's name, email and expertise
  - stats fields sent as strings or HTML

  No `on*` handlers or `javascript:` URLs appear, no app classes are borrowed, and there are no console errors.
- **Button rules.**
  - Tentative and refused answers show the button. Grounded answers and web answers (`offer_help:false`) never do.
  - A restored chat whose doubt was never sent still opens the dialog prefilled. It sends `decision:'refuse'` with the token and then disables the button.
- **401 handling.** A 401 from Send in the Ask dialog, from My doubts or from the inbox goes to the login screen. The panel closes, no error bubble appears, and the server message is shown. Signing back in as the same account restores the chat.
- **Badges.**
  - The badge counts answered doubts that haven't been viewed, per account (`vs_seen_answers:<email>`).
  - Viewing clears it, and that persists across a reload. Only answers that arrive later are badged again.
  - A different account with colliding doubt ids gets its own badge after a normal sign-out and sign-in.
- **Roles.**
  - The inbox nav stays hidden for students even when the server sends inbox items.
  - Promotion and demotion mid-session show and hide it on the next refresh (opening Settings or My doubts).
  - A student sees no helper or owner sections in Settings.
- **Keyboard and nav.**
  - Enter on My doubts or Helper inbox opens the panel; the default action is prevented.
  - The highlight and `aria-current` sit on items 5 and 6. Focus moves into the dialog.
  - Escape returns focus to the nav link and the highlight to Current chat.
- **Phone.** Tapping either new nav item closes the off-canvas sidebar.
- **Withdraw.** Cancelling the confirm posts nothing. Confirming posts `/close` and re-renders the card as "withdrawn".
- **Auth.** Every Phase-2 request carries `Authorization: Bearer <token>`: doubts GET and POST, feedback, answer, `/api/admin/helpers`, `/api/admin/stats` and `/api/me/expertise`. No doubts request is made while signed out (existing test).
- **Static checks** across the Ask dialog, My doubts (answered, open and withdrawn), the inbox (open and answered) and the owner's Settings: no duplicate ids, every inline handler resolves, no console errors, and the "–" placeholder appears for a null escalation rate.
- **Regression pass with Phase 2 present.**
  - A grounded answer shows its citation pill and no Ask button.
  - My material lists files and highlights its nav item.
  - Settings shows the profile.
  - New chat goes back to the hero screen, and restoring from Recent brings the transcript back and highlights Current chat.

## Judgement calls
- **Duplicate-doubt bugs (P2F-2, P2F-3).** The backend has no deduplication, so I treat the UI as the only guard. Restore keeping the "sent" state is my reading of "judge sensibly".
- **IME and Escape.** I dropped a test for an Escape keydown fired with `isComposing:true`, which would close the inbox and lose a drafted answer. Browsers are inconsistent about whether that keydown reaches the page, so it isn't reported as a bug. Still worth adding `if(e.isComposing)return;` to the Escape branch in `attachNav`.
- **Initial focus.** The Ask dialog's first focus goes to ✕ rather than the question textarea. That is consistent with every other panel (`openModal` picks the first focusable control), so it isn't counted. Focusing `#hq`, or Send, would be friendlier.
- **P2F-13 is static.** JSDOM has no layout, so this test checks the CSS rule. The height estimate is computed from the declared sizes.
