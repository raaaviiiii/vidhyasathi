# Front-end regression + adversarial report: `index.html` (round 2)

**Scope:** the rewritten UI script, after the round-1 fixes in commit 97a2c7b, judged against `qa/SPEC.md`. The areas covered are:
- the sanitizer, `renderMd`, gilding and math placeholders
- token sessions and 401 handling
- chats stored as data
- in-flight requests
- error wording
- keyboard and a11y
- phone layout

**Harness:** the existing `tests/frontend/harness.mjs`, which runs JSDOM 29 with the real marked.js 12.0.2 and a mocked fetch.

**New tests:** `tests/frontend/regression.test.mjs` has 41 tests.

**Run:** `cd /home/claude/vidhyasathi && NODE_PATH=/tmp/node_modules node --test tests/frontend/`

**Result:** 145 tests: **128 pass, 17 fail**.
- All 104 tests from round 1 still pass.
- Of the 41 new tests, 24 pass and 17 fail. Each failure is one bug below (REG-F1 to REG-F17), and each failing test has the bug's ID in its name.

| ID | Sev | Title |
|---|---|---|
| REG-F1 | P1 | Untrusted HTML keeps the app's CSS classes: a fake notes citation on a web answer, and a full-screen fake overlay |
| REG-F5 | P1 | After a 401, signing in as a different account shows, continues and saves the previous account's chat |
| REG-F14 | P1 | Several role=button controls ignore the keyboard, including the "Sign in" toggle, so keyboard users cannot reach sign-in |
| REG-F3 | P2 | A "$" amount before a citation is parsed as math, so the citation pill disappears |
| REG-F6 | P2 | A 401 while a panel is open leaves the panel on top of the login screen |
| REG-F7 | P2 | The `/api/logout` request is aborted by the immediate `location.reload()` |
| REG-F8 | P2 | Re-opening the same recent chat throws away the messages added since it was restored |
| REG-F9 | P2 | A null or malformed entry in `vs_chats` makes New chat throw, and restore half-renders |
| REG-F10 | P2 | Switching to Teach while a Quick answer is in flight puts that exchange into teach history |
| REG-F12 | P2 | A non-JSON 200 response is shown as an honest "not in the material" refusal |
| REG-F13 | P2 | Account-menu items cannot be activated with the keyboard |
| REG-F2 | P3 | A model link's own `rel`/`target` attributes strip the safe `target=_blank rel=noopener` |
| REG-F4 | P3 | Model text containing the U+2063 placeholder markers is replaced or deleted |
| REG-F11 | P3 | "Grade my answer" ignores `busy`, which creates two `#loader` elements |
| REG-F15 | P3 | The web-fallback switch's ARIA state is wrong on boot and out of sync with Settings (and uses aria-pressed instead of aria-checked) |
| REG-F16 | P3 | `aria-expanded` goes stale on Escape, and the phone menu button has none |
| REG-F17 | P3 | Dialog focus is not moved into the panel or back to the opener, and is dropped to `<body>` when Settings re-renders |

---

## Bugs

### REG-F1 — P1 — Untrusted HTML keeps the app's CSS classes
- **Failing test:** `REG-F1: model/web HTML cannot borrow the app's CSS classes …`
- **Repro:** with web fallback on, a web page injects this into the answer:
  - `<span class="cite">Biology Notes · p4</span>`
  - `<div class="overlay show"><div class="modal"><h2>Session expired</h2><a class="enter" href="https://evil.example/login">Sign in again</a></div></div>`

  `sanitizeInto` keeps any `class` that matches `/^[\w\- ]*$/`, so both elements keep their classes.
- **Expected:**
  - SPEC: web answers carry only `[web · domain]` tags and never a notes-style tag. The backend's `_scrub_web_tags` only removes *bracketed* tags, so an HTML pill gets past it.
  - SPEC: untrusted text must never act as HTML in the page.
- **Actual:**
  - A gold notes-style "Biology Notes · p4" pill appears on a web answer, so web content is laundered as the student's own notes. This attacks the thesis directly.
  - `.overlay.show` is `position:fixed; inset:0; z-index:40`, so the model can paint a full-screen fake "session expired / sign in" panel that links to a phishing site.
  - The same applies to `hidden`, `toast show`, `acctmenu show`, `enter`, `tag ok` (a fake "grounded" badge) and so on.
- **Fix in `sanitizeInto`:** drop the `class` attribute from untrusted markup altogether. The only class marked emits is `language-*` on `<code>`, so allow just that: `/^language-[\w-]+$/` on `CODE`. Gilding adds `.cite` afterwards, so pills still work.

### REG-F5 — P1 — After a 401, a different account inherits the previous account's chat
- **Failing test:** `REG-F5: after a 401, signing in as a DIFFERENT account …`
- **Repro:**
  1. Asha is in a Teach chat when her token expires.
  2. The next request returns 401, `sessionExpired()` runs and the login screen shows.
  3. Bala signs in on the same tab.
- **Actual:**
  - Asha's transcript is still on screen.
  - `chatLog`, `history` and `chatId` are still in memory, so Bala's next Teach question **sends Asha's teach history to the server under Bala's token**.
  - On New chat, `saveCurrentChat()` replaces Asha's stored entry (same `chatId`) with one marked `owner:'bala@x.com'`, which moves her conversation into Bala's Recent list.
  - The guard meant to prevent this in `doAuth` is dead code: `if(r.email && localStorage.getItem('vs_email') && …)`. `sessionExpired → clearSession()` has already removed `vs_email`, so the condition is never true.
- **Expected:**
  - SPEC: a student sees only their own data.
  - The round-1 fix list says signing in as a different account clears the previous account's chats.
- **Fix:**
  1. In `sessionExpired()`, remember the old owner (`lastOwner=USER_EMAIL`) before calling `clearSession()`.
  2. In `doAuth()`, compare `r.email` against `lastOwner` or the stored `vs_email`, not against the already-cleared key. On a mismatch, remove `vs_chats` (or that owner's entries) **and** reset the in-memory chat: `chatGen++; chatLog=[]; history=[]; chatId=null; resetTranscript();`.
  3. Simplest safe variant: always reset the in-memory chat in `doAuth()`. It was already saved by `sessionExpired()`, so nothing is lost.

### REG-F14 — P1 — role=button controls that ignore Enter/Space
- **Failing test:** `REG-F14: every role=button control responds to Enter/Space …`
- **Repro:** focus each of these and press Enter or Space. Nothing happens.
  - the four `#starters .starter` items
  - the `#authToggle b` "Sign in" / "Create an account" link
  - every modal `✕` (`.x`)
- **Expected:** SPEC (UI); round-1 fix list (keyboard activation). A control with role=button must respond to Enter and Space. The login card opens in *sign-up* mode, so a returning keyboard-only student cannot reach the sign-in form at all. That is why this is P1. Escape covers the ✕.
- **Fix:**
  - Starters: call `keyActivate(el, () => el.click())` for each `.starter` in `attachNav()`.
  - Auth toggle: in `toggleAuth()`, render the `<b>` as a real `<button type="button">`, or attach `keyActivate` after each `innerHTML` swap.
  - Modal ✕: in `openModal()`, add `keyActivate(modal.querySelector('.x'), closeModal)`, or make `.x` a `<button>`.

### REG-F3 — P2 — "$5 … $10" is treated as inline math and swallows citations
- **Failing test:** `REG-F3: a "$" amount before a citation …`
- **Repro:** the answer is `The fee is $5 per term [Fees · p2] and the late fine is $10 [Fees · p3].`
- **Actual:**
  - `MATH_RE` (`/\$[^\$\n]+?\$/`) matches `$5 per term [Fees · p2] and the late fine is $`. The text is stashed as math and never gilded, so the first citation shows as raw text.
  - With KaTeX loaded, the whole span is then typeset as an equation.
  - Money amounts are common in commerce and economics notes.
- **Expected:** SPEC: answers show gilded `[source]` citation pills.
- **Fix:** in `MATH_RE`, use the auto-render rules: an opening `$` must not be followed by whitespace or a digit-then-space, and a closing `$` must not be preceded by whitespace. For example `/\$\$[\s\S]+?\$\$|\$(?=\S)(?!\d+(?:[.,]\d+)?\s)[^$\n]*?[^\s\\]\$/`. Also never let a placeholder span contain a `[…·…]` tag.

### REG-F6 — P2 — A 401 while a panel is open leaves the panel above the login screen
- **Failing test:** `REG-F6: a 401 while a panel is open closes the panel`
- **Repro:** open My material. `/api/material` returns 401.
- **Actual:** `sessionExpired()` shows `#login` (z-index 20), but `#overlay.show` (z-index 40) is still open on top of it. The student sees a "loading…" or "Couldn't load your material" panel over the sign-in form. The same happens from Settings (change password) and from the phone sidebar.
- **Fix:** in `sessionExpired()` (or `showLogin()`), call `document.getElementById('overlay').classList.remove('show'); closeSidebar();` and close the account menu.

### REG-F7 — P2 — The logout request is aborted by the reload
- **Failing test:** `REG-F7: the /api/logout request survives the immediate page reload`
- **Repro:** Sign out. `signOut()` calls `fetch('/api/logout')` and then immediately calls `location.reload()`.
- **Actual:**
  - Browsers cancel in-flight fetches when the document unloads, so the server session usually survives. The token stays valid server-side until it expires.
  - That matters on a shared or lab machine, or if the token was copied.
- **Expected:** "sign-out calls /api/logout", effectively.
- **Fix:** in `signOut()`, pass `keepalive:true` to the fetch, or `await` it (with a short timeout) before reloading.
- **Judgement call:** JSDOM does not navigate, so the test accepts either `keepalive:true` or an awaited promise as proof.

### REG-F8 — P2 — Re-opening the same recent chat discards the new messages
- **Failing test:** `REG-F8: re-opening the SAME recent chat after continuing it keeps the new messages`
- **Repro:**
  1. Restore chat X from Recent.
  2. Ask a follow-up.
  3. Open Recent and click X again (for example, to "refresh" it).
- **Actual:** `restoreChat()` only saves when `chatId !== id`. It is the same id here, so nothing is saved, and the stale stored copy is replayed. The follow-up is lost for good.
- **Fix:** in `restoreChat()`, always call `saveCurrentChat()` first (`if(chatLog.length) saveCurrentChat();`), then re-read the chat from storage.

### REG-F9 — P2 — A malformed `vs_chats` entry breaks New chat and restore
- **Failing test:** `REG-F9: malformed entries in vs_chats …`
- **Repro:**
  - `vs_chats = '[null, …]'` is valid JSON, so `loadChats()` accepts it. `saveCurrentChat()` then runs `.filter(c => (c.id ?? c.time) !== chatId)` and throws `Cannot read properties of null (reading 'id')`. New chat throws before it resets anything, and keeps doing so until storage is cleared.
  - A log entry of `null`, or `{t:'answer'}` with no `r`, throws inside `pushEntry`/`answerNode`. Restore stops halfway with the Recent panel still open.
  - Round 1 only guarded against a JSON parse error.
- **Fix:**
  - `loadChats()`: `.filter(c => c && typeof c === 'object')`.
  - `restoreChat()`: skip non-object entries, and render entries without `r` through `errorNode`.
  - `answerNode`, `examNode`, `gradeNode`: default `r = r || {}`, and filter `sources` and `items` for objects.
  - Wrap each `pushEntry` in try/catch so that one bad entry cannot abort the replay.

### REG-F10 — P2 — Teach history picks up a Quick exchange
- **Failing test:** `REG-F10: switching to Teach while a Quick answer is in flight …`
- **Repro:** ask in Quick mode. While it is pending, click the Teach chip.
- **Actual:** after `await post()`, `send()` checks the global `mode` (now `teach`) and pushes the Quick Q/A into `history`. The next Teach turn sends that history, which goes against the SPEC intent of "multi-turn tutor with memory" for Teach turns only.
- **Fix:** in `send()`, take `const m = mode` before the await, and use `m` both for the request and for the `history.push` condition.

### REG-F12 — P2 — A non-JSON 200 is rendered as an honest refusal
- **Failing test:** `REG-F12: a 200 response that is not JSON …`
- **Repro:** `/api/exam` or `/api/grade` returns 200 with an HTML body, for example a tunnel or captive-portal interstitial, or a proxy page.
- **Actual:**
  - `api()` swallows the JSON error and returns `{}`, so `examNode` and `gradeNode` show "I don't have that in the material — …".
  - `/api/answer` similarly shows an empty "answer" bubble.
- **Expected:** SPEC: errors are shown in plain language, never a misleading message. SPEC: a refusal on a covered topic is the second-worst outcome.
- **Fix:** in `api()`, if `res.ok` but `data === null`, throw `new ApiError(res.status, 'The server sent an unexpected reply. Please try again.')` and set the status pill to degraded.

### REG-F13 — P2 — Account-menu items are dead to the keyboard
- **Failing test:** `REG-F13: account-menu items work from the keyboard …`
- **Repro:** focus the chip, press Enter (the menu opens), Tab to "Sign out" or "Settings", press Enter.
- **Actual:**
  - The menu is a child of `#acctChip`, so the keydown bubbles to the chip's `keyActivate` handler. That handler calls `preventDefault()`, which cancels the button's own activation, and then toggles the menu shut.
  - The keyboard can never run Sign out from the menu.
  - `#acctMenu`'s `onclick` stops click propagation but not keydown.
- **Fix:** do either of these:
  - In `attachNav()`: `keyActivate(chip, e => { if (e.target === chip) toggleAcct(e); })`.
  - In `buildAcctMenu()`: add `m.addEventListener('keydown', e => e.stopPropagation())`. Better still, move the menu out of the chip element.

### REG-F2 — P3 — A model link's own rel/target cancels the safe defaults
- **Failing test:** `REG-F2: a model link with its own rel/target …`
- **Repro:**
  - `<a href="https://e.example" rel="x">` comes out as `target=_blank` with no `rel`.
  - `<a target="_self" href=…>` comes out as `rel=noopener noreferrer` with no target, so clicking the link navigates the app away and loses the open chat.
- **Cause:** `sanitizeInto` sets `target` and `rel` while it is handling `href`. Then the same loop reaches the original `rel`/`target` attribute and removes it.
- **Fix:** in `sanitizeInto`, strip every attribute first, then set `href`, `target` and `rel` after the loop.

### REG-F4 — P3 — U+2063 placeholder smuggling
- **Failing test:** `REG-F4: model text that contains the U+2063 placeholder markers …`
- **Repro:** the answer contains the literal `⁣M0⁣` (U+2063 M 0 U+2063) and also `$x^2$`.
- **Actual:**
  - The literal is replaced with the real `$x^2$`, which duplicates the math.
  - A `⁣M7⁣` with no matching index is silently deleted.
  - It is not executable, since math is restored as text, but model or web content can rewrite what the student reads.
- **Fix:** in `renderMd`, before the math pass, strip U+2063 from the input (`text = text.replace(/⁣/g, '')`), or use a per-render random nonce in the placeholder.

### REG-F11 — P3 — "Grade my answer" ignores `busy`
- **Failing test:** `REG-F11: Grade my answer while an answer is pending …`
- **Repro:** send a question. While it is pending, click the "Grade my answer" starter.
- **Actual:**
  - A second `id="loader"` is created.
  - `killLoader()` removes only the first, so a spinner can be left behind.
  - Grade and answer replies can land in either order.
- **Fix:** in `openGrade()`, `if (busy) return;` and set or release `busy` the same way `send()` does.

### REG-F15 — P3 — Web-fallback switch ARIA state
- **Failing test:** `REG-F15: web-fallback switch exposes its state correctly …`
- **Repro:** with `vs_web='1'`, the toggle is visually on but has `aria-pressed="false"`. `applyPrefs()` and `toggleWebDefault()` toggle the class without updating ARIA.
- **Expected:** the round-1 fix list says ARIA is kept in sync with the Settings default.
- **Fix:**
  - Add `setWeb(on)` that sets the class **and** `aria-checked`, and use it in `toggleWeb`, `applyPrefs` and `toggleWebDefault`.
  - `role="switch"` takes `aria-checked`, not `aria-pressed`.
- **Judgement call:** the test accepts either attribute for the sync check, but also requires `aria-checked` as the correct attribute.

### REG-F16 — P3 — Stale `aria-expanded`
- **Failing test:** `REG-F16: aria-expanded tracks the account menu and the phone menu button …`
- **Actual:**
  - The Escape handler in `attachNav()` hides `#acctMenu` but leaves `aria-expanded="true"` on the chip.
  - `.menubtn` has no `aria-expanded` or `aria-controls` at all.
- **Fix:**
  - In the Escape handler, also call `chip.setAttribute('aria-expanded','false')`.
  - Have `toggleSidebar()` and `closeSidebar()` set `aria-expanded` on `.menubtn`, and add `aria-controls` pointing at the sidebar's id.

### REG-F17 — P3 — Dialog focus management
- **Failing test:** `REG-F17: dialog focus — …`
- **Actual:**
  - `openModal()` leaves focus on the nav link behind an `aria-modal` dialog.
  - `closeModal()` does not return focus to the opener.
  - `setDefaultMode()` and `toggleWebDefault()` re-render the whole modal through `openSettings()`, so the focused button is destroyed and focus drops to `<body>`.
- **Fix:**
  - In `openModal()`: save `document.activeElement`, then focus the dialog (`modal.tabIndex = -1; modal.focus()`) or its first control.
  - In `closeModal()`: restore focus to the saved element.
  - In `setDefaultMode()` and `toggleWebDefault()`: update the buttons in place instead of re-rendering, or refocus the equivalent button after re-rendering.

---

## Verified working (passing tests in `regression.test.mjs`)
- **Sanitizer:** 30 mXSS, scheme and attribute payloads through real marked produce no script, no `on*` attribute, no non-http(s) href, and no svg, math, img, style, base, meta, form, details or template. The payloads include:
  - `java&#x09;script:`, `&#106;avascript:`, `data:`, `vbscript:`, mixed case
  - autolinks, reference-style links, link-title breakout
  - `<details ontoggle>`, `<svg><a xlink:href>`, `<math><mtext><mglyph><style>`, `<noscript>` breakout
  - unclosed tags, comments, `<base>`, `<meta refresh>`, and DOM-clobbering ids and names
- The same holds offline, when marked is missing.
- `$…$` containing `</span><img onerror>` and `\href{javascript:}` stays inert text.
- `renderMathInElement` is always called with `trust:false`, both live and on restore.
- **Gilding:** `[Notes · p1]` and `[web · site]` gild, and the web pill is marked web. `[0, b-1]`, `arr[i]`, code, pre, link text, inline math and display math are left untouched.
- **Boot:**
  - With a token, the app opens with no calls.
  - With nothing stored, login shows with no message.
  - Legacy storage forces sign-in in "Sign in" mode with a message, and clears the old identity and chats.
- **401 handling:**
  - The login screen shows the server's message.
  - The open chat is saved first, with the right owner.
  - There is exactly one call, so no loop. `busy` is released, there is no loader, and there is no error bubble.
- **Sign-out:** sends `/api/logout` with the bearer token, and never re-saves the chat (beforeunload, or a late answer arriving after sign-out).
- **Owner isolation in `vs_chats`:**
  - Recent lists only your own chats.
  - `restoreChat(otherId)` is a no-op, and saving never touches another owner's entry.
  - Clear all keeps other owners' entries.
- **403 on change password:** shows the message and keeps the student signed in, with the token intact.
- **Recent chats:**
  - restore, continue, then New chat updates one entry with the full log
  - an invalid-JSON `vs_chats` is tolerated
  - crafted log entries (answer, exam, grade, error, unknown type, hostile `who`, sources and web_sources) and legacy `{html}` chats are sanitized on restore
- **In-flight requests:** an exam pending across a restore, and a grade pending across New chat, do not land in the wrong chat, and `busy` is released.
- **Errors:**
  - `{ok:false,error}` on a 200 is shown as an error for answer, exam and delete.
  - Plain-language wording for 503, 429, network failure and 413.
  - The status pill moves through Service offline (warn), Unreachable (down) and back to Active, and has `aria-live`.
- **Keyboard:**
  - Nav items, the Settings footer and the account chip activate with Enter and Space, with `preventDefault`.
  - `aria-expanded` on the chip toggles.
  - Escape closes the panel, the sidebar and the menu, and restores the base nav highlight.
- **Phone layout:**
  - The menu button toggles `body.nav-open` and the scrim closes it.
  - Nav panels, Practice, New chat and Settings close the sidebar.
  - The CSS (checked statically) puts the sidebar off-canvas at ≤760px, shows the scrim only when the sidebar is open, and stacks the sidebar above the scrim.
- **Static checks:**
  - No duplicate ids across all panels.
  - Every inline handler resolves, including both states of the login toggle.
  - No console errors on load with legacy, token-only, corrupted or hostile storage, and an unknown `vs_mode` falls back to quick.
