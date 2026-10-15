# Front-end QA report — `index.html`

**Scope:** single-file UI (`index.html`, inline `<script>`), judged against `qa/SPEC.md`.
**Harness:** JSDOM 29 + **real marked.js 12.0.2** injected before the inline script (the CDN copy cannot load offline), a no-op `renderMathInElement`, a mocked `fetch` that records every call, and stubbed `confirm` and `prompt`. One test also runs with marked absent, to simulate the CDN failing.

**Run:**
`cd /home/claude/vidhyasathi && NODE_PATH=/tmp/node_modules node --test tests/frontend/`
(or `pytest tests/test_frontend.py`, which wraps the same run).

**Result:** 104 tests: **59 pass, 45 fail**. Every failure maps to a bug below.

Files:
- `tests/frontend/harness.mjs`: boots the page, mocks fetch, and provides the `injected()` XSS detector
- `tests/frontend/{auth,chat,render,material,nav}.test.mjs`: the tests
- `tests/frontend/index.js`: loader, so the directory form of `node --test` works on Node 22
- `tests/test_frontend.py`: pytest wrapper that prepends `/tmp/node_modules` to NODE_PATH, because NODE_PATH is already set globally in this environment

How the XSS detector works: `injected(el)` reports any `<script>`, `<iframe>`, `<object>` or `<embed>` element, any `on*=` attribute, and any `href`/`src` value starting with `javascript:` inside the rendered message.

---

## Bugs

### UI-1 — P0 — Model output is inserted as live HTML (stored XSS through answers, practice sets and grading)
- **Failing tests:**
  - `XSS: model answer with <img onerror> / <script> does not execute (real marked.js)`
  - `XSS: model answer with a javascript: markdown link is not clickable script`
  - `XSS: model answer HTML is escaped when marked.js CDN fails to load (offline)`
  - `XSS: HTML hidden inside a [citation] tag is not injected`
  - `XSS: payload wrapped in $…$ math delimiters is not injected (math path bypasses markdown)`
  - `XSS: exam topic, question and answer text cannot inject`
  - `XSS: grade feedback from the model cannot inject`
- **Repro:** `/api/answer` returns `answer: 'Hi <img src=x onerror="…">'`. The `<img onerror>` reaches the DOM.
  - The same happens for `[x](javascript:alert(1))`, which renders as `<a href="javascript:…">`.
  - It also happens when marked failed to load: `mdGild` then returns the raw text.
  - Text inside `$…$` is stashed before marked runs and put back unescaped.
  - The `[ … ]` citation regex puts `inner` back without escaping it.
  - `renderExam` (each item's `a`) and the grade feedback both go through `mdGild` and have the same hole.
- **Why P0:** the model's text comes from the student's material and, with web fallback on, from arbitrary web pages. A page or a PDF can therefore inject script into the student's session (indirect prompt injection leading to XSS). Through UI-7 that script is also stored in `vs_chats` and runs again when the chat is restored.
- **Expected (SPEC, UI):** "Untrusted text (model output, web pages, file names, user input) must never execute as HTML/JS in the page."
- **Actual:** the injected markup executes.
- **Fix in `mdGild()`:**
  1. HTML-escape `& < > " '` in the text **before** `marked.parse`. Alternatively, sanitise marked's output with DOMPurify (pin the version), or use a marked `renderer.html` that returns its input escaped.
  2. Escape each stashed math string when restoring it.
  3. Escape `inner` in the citation replace.
  4. If marked is missing, fall back to `gild()`, which escapes, not to the raw text.
  5. Allow only `http:`/`https:`/`mailto:` link hrefs.
  6. Since `renderExam` and `openGrade` reuse `mdGild`, this one fix covers them too.

### UI-2 — P0 — Web source URLs are interpolated raw into `href` (javascript: URLs and attribute breakout)
- **Failing tests:**
  - `XSS: web_sources url "javascript:alert(1)" is not rendered as a live link`
  - `XSS: web_sources url with a quote cannot break out into an attribute`
  - `web disclosure links open with rel="noopener"` (P3 part)
- **Repro:** `web_sources:[{url:'javascript:alert(1)'}]` renders a clickable `javascript:` link. `url:'https://e.com/" onmouseover="alert(1)'` adds an `onmouseover` handler to the `<a>`. Search-result URLs are attacker-controlled.
- **Expected:** SPEC, UI ("web pages … must never execute").
- **Fix in `renderAnswer()`:**
  - Build the links with `document.createElement('a')` and set `a.href = url` only after `new URL(url).protocol` is `http:` or `https:`.
  - Set the link text with `textContent`.
  - Add `rel="noopener noreferrer"`.

### UI-3 — P0 — A file name breaks out of `onclick="delMaterial('…')"` in My material
- **Failing tests:**
  - `file name "x\\');window.__pwned=1;//.txt": ✕ deletes exactly that file, no injected code` (the code executes)
  - `file name "x\\");alert(1);//.txt": ✕ deletes …` (the ✕ button does nothing)
  - `file name with a double quote cannot add attributes/handlers to the ✕ button`
- **Repro:** the file name is passed through `esc()`, which only handles `<>&`, and then through `.replace(/'/g,"\\'")`.
  - A name containing `\'` becomes `\\'`. The backslash escapes itself, the quote closes the string, and the rest runs as JS. This is confirmed: `window.__pwned` became 1.
  - A name containing `"` ends the attribute. It can add new handlers, or it breaks the onclick, so the ✕ silently fails and the file can never be removed.
- **Expected:** SPEC, UI ("file names … must never execute"). SPEC, My material: "a remove (✕) button that deletes the file".
- **Fix in `listMaterial()`:**
  - Drop the inline `onclick`. Create the button in the DOM, store the name with `btn.dataset.name = d.name`, and call `btn.addEventListener('click', () => delMaterial(d.name))`.
  - If string HTML must stay, add `"` → `&quot;` and `'` → `&#39;` to `esc()` and pass an index into the docs array, not the name.

### UI-4 — P0 — Source names (taken from file names), the caveat and error text are rendered unescaped
- **Failing tests:**
  - `XSS: caveat and sources fields are rendered as text`
  - `XSS: error message text (from server) is rendered as text`
- **Repro:** `sources:[{source:'<img src=x onerror=…>',loc:'p1'}]` executes from the sources line.
  - The backend's `source_label()` takes the source name from the uploaded file's stem, so a file named `<img src=x onerror=…>.txt` fires on **every answer that cites it**. In the shared bucket (UI-5) that reaches other users.
  - `renderError(msg)` and the `caveat` also use `innerHTML`.
- **Expected:** SPEC, UI.
- **Fix:**
  - In `renderAnswer()`, wrap `s.source`, `s.loc` and `r.caveat` in `esc()`, with `esc` extended to escape quotes.
  - In `renderError()`, set the bubble with `textContent`.

### UI-5 — P0 — A user saved before the per-user update is silently put in the shared bucket
- **Failing test:** `a user saved before the per-user update (vs_user but no vs_email) is not silently put in the shared bucket`
- **Repro:** localStorage holds `vs_user` but no `vs_email`. On reload the boot code calls `applyUser(_u,'')` and `enter()`, and `USER_EMAIL` stays `''`.
  - Every `/api/answer`, `/api/exam`, `/api/grade`, `/api/upload` and `/api/material` request then sends `user:""`. The backend's `_uid()` maps that to `None`, which is the **shared bucket**.
  - Every pre-update student lands in that one bucket. They all see and query each other's uploads, and their own uploads go there too.
  - Change password and Delete account also send `email:""` and fail with "Current password is wrong."
- **Expected (SPEC, Accounts & isolation):** "Each signed-in student sees and queries only their own uploads."
- **Fix (boot block at the end of the script):**
  - `if(_u && !_e){ localStorage.removeItem('vs_user'); }`, so the user is sent back to sign in. Only enter the app when both values are present.
  - Also make `post()` refuse to send when `USER_EMAIL` is empty.
  - Backend: reject an empty `user` rather than mapping it to the shared bucket.

### UI-6 — P1 — Recent chats are not per-user, and Sign out writes them back on unload
- **Failing tests:**
  - `Recent chats are per-user: after A signs out, B on the same browser cannot see A's chat`
  - `Sign out does not let the beforeunload handler re-save the open chat`
- **Repro:**
  1. User A has a chat open and clicks Sign out.
  2. `signOut()` removes `vs_chats` and then calls `location.reload()`.
  3. The reload fires `beforeunload`, and `saveCurrentChat()` writes A's open chat back into `vs_chats`.
  4. B signs in on the same browser and opens Recent chats. A's question and the answer drawn from A's material are listed.
  - Separately, `vs_chats` is a single key for the whole browser, never keyed by user.
- **Expected (SPEC, Accounts & isolation):** another user's material must never appear. Sign out should end the session cleanly. This matters on shared lab PCs.
- **Fix:**
  - Key the storage per user: `'vs_chats:'+USER_EMAIL` in `saveCurrentChat`, `openRecent`, `restoreChat` and the Settings "Clear all".
  - In `signOut()`, set a `signingOut` flag (or `removeEventListener('beforeunload', saveCurrentChat)`) before `reload()`.

### UI-7 — P1 — Recent chat titles and restored transcripts are injected as HTML (stored XSS)
- **Failing test:** `chat titles in Recent are rendered as text (no HTML injection)`
- **Repro:** ask `what does <img src=x onerror=…> do?`, click New chat, then open Recent chats.
  - `saveCurrentChat()` takes the title from `textContent`, which decodes the escaped `<`.
  - `openRecent()` puts `c.title` into `innerHTML`, so the handler executes.
  - `restoreChat()` also restores `c.html` as raw HTML, which replays anything UI-1 let through.
  - Combined with UI-6, this executes in another user's session.
- **Expected:** SPEC, UI ("user input … must never execute").
- **Fix:**
  - In `openRecent()`, wrap the title in `esc()`.
  - Store the structured messages, not `innerHTML`, and re-render them on restore. At minimum, sanitise the HTML in `restoreChat`.

### UI-8 — P1 — Misleading or raw error messages ("Is app.py running?" when the server answered)
- **Failing tests:**
  - `backend 500 is reported in plain language, not "Is app.py running?"`
  - `backend JSON error (e.g. model down) is surfaced to the student`
  - `network failure shows a plain-language message and releases busy`
  - `upload rejected by the server (HTTP 500/413, non-JSON) is not reported as "is app.py running?"`
  - `delete of an already-removed file (404 "Not found.") is not reported as "is app.py running?"`
- **Repro:**
  - `post()` throws `Backend error (500). Is app.py running?` for **every** non-2xx response, including 503 with `{error:"The model isn't running"}`. The server's `error` body is discarded.
  - When fetch rejects, the student sees the raw browser text "Failed to fetch".
  - `delMaterial` on a 404 shows "Remove failed — is app.py running?".
  - `doUpload` on a 413 or a non-JSON 500 shows "Upload failed — is app.py running?".
- **Expected (SPEC, UI):** "Errors are shown in plain language (e.g. 'the model isn't running'), never a misleading message." SPEC, Ops: requests fail "with a clear JSON error".
- **Fix:**
  - In `post()`, do `if(!res.ok){let j={};try{j=await res.json()}catch{}; throw new Error(j.error||j.detail||friendly(res.status))}`. `friendly` would map, for example, 5xx to "The server hit an error — try again" and 503 to "The model isn't running".
  - Map a `TypeError` from fetch to "Can't reach the Vidhyāsathi server."
  - Apply the same handling in `doUpload()` and `delMaterial()`. Use `r.error` for the 404 case.

### UI-9 — P1 — No phone-width layout
- **Failing tests:**
  - `static: phone width — a mobile media query collapses/hides the 272px sidebar`
  - `static: login card fits a 360px phone (no fixed width > viewport)`
- **Repro:** the stylesheet has no `@media` rule.
  - `.sidebar` is fixed at `flex:0 0 272px`, the `.topbar` and `.stage` use 40px side padding, and `body` has `overflow:hidden`.
  - On a 360px phone, the chat column is about 88px wide and cannot be scrolled sideways.
  - `.card` (login) is `width:388px`, wider than the screen.
- **Expected (SPEC, UI):** "Works on a phone-width screen."
- **Fix (CSS):**
  - Add `@media (max-width:700px){ .sidebar{position:fixed;left:-280px;transition:left .2s;z-index:30} .sidebar.open{left:0} .topbar,.stage{padding-left:14px;padding-right:14px} .hero h1{font-size:34px} }` and a hamburger button that toggles `.sidebar.open`.
  - Change `.card` to `width:388px;max-width:calc(100vw - 32px)`.

### UI-10 — P2 — The active nav highlight does not follow what is shown
- **Failing tests:**
  - `nav "Recent chats" opens its panel AND highlights that nav item`
  - `nav "My material" …`
  - `nav "Trust & citations" …`
  - `nav "Practice sets" switches to exam mode, shows chat, and highlights Practice sets`
  - `Settings (sidebar footer) is highlighted/reflected when open`
- **Repro:** click any of these items. "Current chat" stays highlighted, and `setActiveNav` is only ever called with 0.
- **Expected (SPEC, UI):** "The active nav item reflects what's shown."
- **Fix:**
  - In `attachNav()`, call `setActiveNav(i)` in each handler: 1 in `openRecent`, 2 in `openMaterial`, 4 in `openTrust`, 3 for Practice sets. For Settings, call `setActiveNav(-1)` and add `.footer.on`.
  - In `closeModal()`, return to 0, or to 3 while exam mode is active.

### UI-11 — P2 — A refusal says "Not fully sure — you can ask a human helper."
- **Failing test:** `refusal offer_help line does not say "Not fully sure" (nothing was answered)`
- **Repro:** the backend sets `offer_help` for both `answer_uncertain` and `refuse`, and `renderAnswer()` always prints "Not fully sure …". Under a confident refusal ("I don't have that in the material.") that wording undercuts the honest refusal.
- **Expected:** the refusal is a confident statement that the material does not cover the question (SPEC, Thesis). "Not fully sure" fits the tentative band only.
- **Fix in `renderAnswer()`:** for `r.decision==='refuse'`, print "This isn't in your material — a teacher or classmate may be able to help." Keep "Not fully sure…" for `answer_uncertain`.

### UI-12 — P2 — An answer still in flight lands in the next chat after New chat
- **Failing test:** `New chat while an answer is in flight: stale answer does not land in the new chat`
- **Repro:** send a question, then click New chat before the reply arrives.
  - The old reply is appended to the fresh transcript, and the hero is still visible.
  - In Teach mode, the old Q/A is pushed into the **new** chat's `history`, which misdirects the next follow-up.
- **Fix in `send()`:** keep a chat-generation counter (`let chatGen=0`) and increment it in `newChat()` and `restoreChat()`. In `send()`, capture `const g=chatGen`, and after the `await` discard the result if `g!==chatGen`. Alternatively, abort with an `AbortController`.

### UI-13 — P2 — Citation and markdown post-processing corrupts code, links and math
- **Failing tests:**
  - `a citation tag is not mangled into a pill inside inline code / markdown links`
  - `math with < or [ ] survives rendering (e.g. $a<b$, interval $[0,1]$)`
- **Repro:**
  - In `` `arr[i]` ``, the `[i]` is turned into a citation pill.
  - `$x \in [0,1]$` turns `[0,1]` into a pill and breaks the KaTeX source.
  - `$a<b$` is parsed by marked as an HTML tag, and the inequality is deleted. The student sees "If $a0,1$ then done".
  - These are equations the OCR caveat is about.
- **Expected:** SPEC, UI: "Answers render markdown + KaTeX math, gilded `[source]` citation pills."
- **Fix in `mdGild()`:**
  - Escape `<`, `>` and `&` inside the stashed math.
  - Apply the citation replace only to text nodes outside `code`, `pre`, `a` and math placeholders. Alternatively, convert the tags to placeholders **before** marked, matching only `[Source · loc]` with the `·`, and restore them afterwards.

### UI-14 — P2 — Restoring a recent chat and then pressing New chat saves a duplicate
- **Failing test:** `restoring a recent chat then New chat does not duplicate it in Recent`
- **Repro:** restore chat X, then click New chat. `saveCurrentChat()` adds X a second time with `unshift`, and this repeats every time.
- **Fix:** have `restoreChat()` remember the restored index or id. `saveCurrentChat()` should then replace that entry instead of adding a new one, and should not save at all when nothing changed.

### UI-15 — P2 — Clicking a composer mode chip overwrites the saved default mode
- **Failing test:** `Composer chip clicks are per-session and do NOT overwrite the Settings default mode`
- **Repro:** set the Settings default mode to Quick, then click "Exam & Practice" once. `setMode()` and `setModeByName()` write `vs_mode`, so the next load starts in Exam. The starters and the Practice-sets nav do the same, and `toggleWeb()` does the same for the web default.
- **Expected (SPEC, Settings):** "default mode, web-fallback default" are settings, not the last chip clicked.
- **Fix:** remove `localStorage.setItem` from `setMode()`, `setModeByName()` and `toggleWeb()`. Persist only in `setDefaultMode()` and `toggleWebDefault()`.

### UI-16 — P2 — The material list stays on "loading…" forever when the request fails
- **Failing test:** `material list: server failure replaces the "loading…" placeholder with an error`
- **Fix in `listMaterial()`:** in the empty `catch(e){}` block, write "Couldn't load your material — check the server" into `#doclist`. Also check `res.ok`.

### UI-17 — P2 — Pressing Enter to confirm an IME candidate sends the message (Malayalam/Hindi input)
- **Failing test:** `Enter while an IME composition is active (Malayalam/Hindi input) does not send`
- **Fix in the `#input` keydown handler:** add `&&!event.isComposing&&event.keyCode!==229`.

### UI-18 — P3 — The echo of the user's question decodes entities
- **Failing test:** `user question echo is shown as typed text, including & entities`
- **Repro:** typing `&lt;b&gt;` is echoed as `<b>`. `addUser()` only escapes `<`, not `&`. `openGrade()` has the same problem.
- **Fix:** build the echo bubble with `textContent`, or use a full `esc()`.

### UI-19 — P3 — An unknown `decision` value renders an empty badge pill
- **Failing test:** `unknown decision value does not render an empty badge`
- **Fix in `renderAnswer()`:** skip the badge when `dm[r.decision]` is undefined.

### UI-20 — P3 — Accessibility of key controls
- **Failing tests:**
  - `nav items are keyboard-reachable (href or tabindex + role)`
  - `account chip is a keyboard-operable control (role=button / aria-expanded)`
  - `static (P3): key controls have accessible names (send button, textarea, auth inputs, delete ✕)`
  - `static: email input is type=email and has autocomplete hints`
- **Repro:**
  - The nav items are `<a>` with no `href` or `tabindex`, and the Settings footer and the account chip are plain `div`s, so the keyboard cannot reach them.
  - The `.send` button is an icon with no label, and `#input` has no label.
  - The login `<label>`s are not tied to their inputs with `for`.
  - `#anEmail` is not `type=email`, and the password field has no `autocomplete`.
- **Fix:**
  - Make the nav items, the footer and the chip `<button>` elements, or give them `role="button"` and `tabindex="0"` plus Enter/Space handlers. Add `aria-haspopup` and `aria-expanded` to the chip.
  - Add `aria-label="Send"`, `aria-label="Ask from your notes"` and `for=` on the labels.
  - Set `type="email" autocomplete="email"`, and `autocomplete="current-password"` (or `"new-password"` in sign-up mode).
  - `.docdel` already has `title="Remove"`, so it passes this check.

---

## Verified working (passing tests)
- **Login/signup:**
  - A fresh load shows the login screen with no console errors.
  - Empty fields show an error without sending a request.
  - A 409 duplicate email and a 401 wrong password show the server's message.
  - An unreachable server shows a plain message.
  - Enter submits the form.
  - Sign-in mode hides the Name field.
  - Success goes home, greets by first name (chip and avatar letter included), and stores `vs_user` and `vs_email`.
  - A reload with both values saved goes straight to home, with Current chat active.
- **Identity on requests:**
  - `user` is sent on answer, exam, grade and material-delete requests, and on the material list as a query parameter.
  - Uploads send `user` in the FormData.
  - Change password and Delete account send `email`.
- **Composer and modes:**
  - Quick mode sends the correct body: question trimmed, `history:[]`, `web` taken from the toggle.
  - Blank input is not sent. Shift+Enter does not send; Enter does.
  - A second send while busy is blocked, and the busy flag is released afterwards.
  - Teach mode builds up history and sends it on follow-ups; Quick mode never sends it.
  - Exam mode posts `{topic, n:5}` and renders a numbered practice set with pills, or the refusal for an off-material topic.
- **Rendering of the four decisions:**
  - `answer`: grounded badge, score with two decimals, citation pills, no refuse style.
  - `answer_uncertain`: tentative badge and the ask-a-human line.
  - `refuse`: refuse style, "not in material" badge, exact refusal text.
  - `answer_web`: web style, 🌐 web pill, disclosure with a trimmed link.
  - The caveat shows a single ↳ marker.
  - The sources line removes duplicates and caps at 5.
  - The score tag and sources line are left out when there is no data.
- **Grade my answer:** the "not a grade" header, cited feedback, the off-material refusal, and cancelling either prompt (nothing is sent) all work.
- **My material:**
  - The list shows indexed and not-indexed flags and sizes, and the empty state.
  - An upload toasts "Indexed … ✓ (N chunks)", or "Saved … — reason" when it isn't indexed, and refreshes the list.
  - Drag-and-drop uploads.
  - Delete asks for confirmation; cancelling sends nothing; confirming sends `{name,user}` and shows a toast.
  - File names containing `'`, `` ` ``, `&amp;` or `<script>` display verbatim and delete correctly.
- **User name:** a name or email containing HTML shows as text in the greeting, chip, account menu and Settings.
- **Escaping:** the exam topic, question and raw fallback are escaped. The question echo cannot inject.
- **Chats:**
  - New chat saves to Recent, resets to the hero and clears the teach history.
  - Restore brings back the transcript and history and closes the modal.
  - Settings "Clear all" empties Recent.
  - Sign out removes `vs_user`, `vs_email` and `vs_chats` and the next load shows login (but see UI-6 for what `beforeunload` writes back).
- **Account chip:**
  - It opens and closes on click and shows the email.
  - It offers Settings, My material and Sign out, and each item opens the right panel and closes the menu.
  - Clicking outside closes it; clicking inside does not.
- **Settings:**
  - Shows the name and email.
  - The default mode persists and applies on reload.
  - The web default persists, syncs the composer toggle, applies on reload and is sent with requests.
  - Change password needs both fields, sends the correct body, and shows the server error or the success message.
  - Delete account needs the password and a confirmation, shows the server error, and signs out on success.
- **Starters:** Explain a concept, Make a practice set (exam mode), Teach me step by step (teach mode) and Grade my answer (prompts) work. Exactly one mode chip is on at a time.
- **Static checks:**
  - No duplicate ids, including with modals open.
  - Every function named in an inline handler exists, including those in the Settings modal and the account menu.
  - No console or script errors on load, logged in or out.

## Notes for other testers (outside the front end, not filed here)
- `GET /api/users` (app.py) returns every user's name, email and sign-up date with no authentication, over a public tunnel. This is for the backend/security tester.
- The backend trusts the client-supplied `user`/`email` field on every route. A tampered request can read or delete another user's material (SPEC, Accounts & isolation). No front-end fix can address this.
