// Regression + adversarial round on the rewritten UI script (sanitizer, sessions,
// chats-as-data, in-flight requests, errors, keyboard/a11y, phone layout).
// Every test states SPEC behaviour; a failing test is a bug (see qa/reports/regression_frontend.md).
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, closeAll, injected, resp, deferred, USER, AUTH, authOf, HTML } from './harness.mjs';

afterEach(closeAll);

/* ------------------------------ helpers ------------------------------ */
const PH = '⁣';   // the invisible separator renderMd uses around math placeholders
const ASHA = USER;
const BALA_LOGIN = { ok: true, name: 'Bala', email: 'bala@x.com', token: 'tok-bala' };
const ans = (answer, extra = {}) => ({ answer, decision: 'answer', top_score: 0.8, caveat: '', offer_help: false, web_used: false, web_sources: [], sources: [], ...extra });

function md(env, text) { const d = env.document.createElement('div'); env.window.renderMd(d, text); return d; }
function render(env, r) { env.window.reveal(); env.window.renderAnswer(ans('x', r)); return env.lastBot(); }
function key(env, target, k) {
  if (typeof target === 'string') target = env.$(target);
  const ev = new env.window.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true });
  target.dispatchEvent(ev); return ev;
}
function escape(env) { return key(env, env.document.body, 'Escape'); }
async function signIn(env, email = 'bala@x.com') {
  if (env.ev('authMode') !== 'login') env.window.toggleAuth();
  env.$('#anEmail').value = email; env.$('#anPass').value = 'pw';
  await env.window.doAuth(); await env.settle();
}
const chats = env => JSON.parse(env.ls('vs_chats') || '[]');
const dupIds = env => { const seen = {}; for (const e of env.$$('[id]')) seen[e.id] = (seen[e.id] || 0) + 1; return Object.keys(seen).filter(k => seen[k] > 1); };
/** Elements/attributes a sanitized bubble must never contain. */
function unsafe(root) {
  const bad = injected(root);
  for (const e of root.querySelectorAll('*')) {
    const tag = e.tagName.toLowerCase();
    if (['svg', 'math', 'img', 'style', 'link', 'meta', 'base', 'form', 'button', 'input', 'details', 'noscript', 'template', 'textarea'].includes(tag)) bad.push('<' + tag + '>');
    for (const a of e.attributes) {
      if (['style', 'id', 'name', 'srcdoc', 'formaction', 'xlink:href'].includes(a.name.toLowerCase())) bad.push(`${tag}[${a.name}]`);
      if (a.name === 'href' && !/^https?:/i.test(a.value)) bad.push(`${tag}[href=${a.value}]`);
    }
  }
  return bad;
}

/* =====================================================================
 * 1. Sanitizer / renderMd / gildInto / math placeholders (real marked.js)
 * ===================================================================== */

const MXSS = [
  '<a href="java&#x09;script:alert(1)">tab</a>',
  '<a href="&#106;avascript:alert(1)">ent</a>',
  '[ent](&#106;avascript:alert(1))',
  '[ws]( javascript:alert(1) )',
  '<a href="JaVaScRiPt:alert(1)">case</a>',
  '<a href="data:text/html,<script>alert(1)</script>">data</a>',
  '[d](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)',
  '<a href="vbscript:msgbox(1)">vb</a>',
  '<javascript:alert(1)>',
  '[x][1]\n\n[1]: javascript:alert(1)',
  '[t](https://a.example "t\\" onmouseover=alert(1) x=\\"")',
  '<details open ontoggle=alert(1)><summary>s</summary>x</details>',
  '<svg><a xlink:href="javascript:alert(1)"><text x="1" y="1">t</text></a></svg>',
  '<svg><style><img src=x onerror=alert(1)></style></svg>',
  '<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>',
  '<math><mi xlink:href="javascript:alert(1)">m</mi></math>',
  '<noscript><p title="</noscript><img src=x onerror=alert(1)>"></noscript>',
  '<table><tr><td><img src=x onerror=alert(1)></td></tr></table>',
  '<div><p>unclosed <b><i><img src=x onerror=alert(1)',
  '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
  '<form><button formaction="javascript:alert(1)">go</button></form>',
  '<a href="https://ok.example" onclick="alert(1)" style="position:fixed;inset:0">ok</a>',
  '<base href="javascript:alert(1)//"><a href="x">rel</a>',
  '<meta http-equiv="refresh" content="0;url=javascript:alert(1)">',
  '<a id="input" name="send">clobber</a><form id="file"></form>',
  '<p>\n\n<img src=x onerror=alert(1)>\n\n</p>',
  '<!-- --><img src=x onerror=alert(1)><!-- -->',
  '<<img src=x onerror=alert(1)>script>alert(1)<</img>/script>',
  '<template><img src=x onerror=alert(1)></template>',
  '[Notes · <img src=x onerror=alert(1)>]',
];

test('sanitizer: mXSS / scheme / attribute payloads through real marked.js never yield executable or styled markup', () => {
  const env = boot({ storage: ASHA });
  const fails = [];
  for (const p of MXSS) { const bad = unsafe(md(env, p)); if (bad.length) fails.push(`${JSON.stringify(p)} -> ${bad.join(', ')}`); }
  assert.deepEqual(fails, []);
  assert.deepEqual(env.errors, []);
});

test('sanitizer: $…$ math containing </span><img onerror> and KaTeX \\href{javascript:} stay inert text', () => {
  const env = boot({ storage: ASHA });
  const d = md(env, 'A $</span><img src=x onerror=alert(1)>$ and $\\href{javascript:alert(1)}{click}$ and $$<svg onload=alert(1)>$$ [Notes · p1]');
  assert.deepEqual(unsafe(d), []);
  assert.ok(d.textContent.includes('$\\href{javascript:alert(1)}{click}$'), 'math text altered');
  assert.equal(d.querySelectorAll('.cite').length, 1);
});

test('KaTeX is invoked without trust (\\href{javascript:}, \\htmlClass stay disabled) on live answers and restored chats', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/answer': ans('$\\href{javascript:alert(1)}{x}$ [Notes · p1]') } });
  const seen = [];
  env.window.renderMathInElement = (node, opts) => seen.push(opts || {});
  await env.ask('q');
  env.window.newChat();
  env.window.openRecent(); env.click(env.$('.recent .r'));
  assert.ok(seen.length >= 2, 'typeset not run on live answer and on restore');
  for (const o of seen) { assert.notEqual(o.trust, true); assert.ok(typeof o.trust !== 'function', 'trust callback enabled'); }
});

test('sanitizer: offline (marked missing) the same payloads are escaped text and citations still gild', () => {
  const env = boot({ storage: ASHA, marked: false });
  const d = md(env, MXSS.filter(p => !p.startsWith('[Notes')).join('\n') + '\n[Notes · p1]');
  assert.deepEqual(unsafe(d), []);
  assert.equal(d.querySelectorAll('.cite').length, 1);
});

// REG-F1
test('REG-F1: model/web HTML cannot borrow the app\'s CSS classes (fake notes-citation pill on a web answer, full-screen fake overlay)', () => {
  const env = boot({ storage: ASHA });
  // A web page (prompt injection) writes a notes-style pill WITHOUT brackets, so the backend's
  // [..·..] scrub for web answers never sees it, plus a page-covering fake "session expired" panel.
  const d = render(env, { decision: 'answer_web', web_used: true,
    answer: 'Photosynthesis… [web · evil.example] <span class="cite">Biology Notes · p4</span>\n\n' +
            '<div class="overlay show"><div class="modal"><h2>Session expired</h2><a class="enter" href="https://evil.example/login">Sign in again</a></div></div>',
    web_sources: [{ source: 'web · evil.example', url: 'https://evil.example/' }] });
  const bubble = d.querySelector('.bubble');
  const notesPills = [...bubble.querySelectorAll('.cite')].filter(c => !c.classList.contains('web'));
  assert.deepEqual(notesPills.map(c => c.textContent), [], 'a web answer shows a notes-style gilded citation (SPEC: web answers carry only [web · domain] tags)');
  assert.equal(bubble.querySelectorAll('.overlay, .modal, .enter, .hidden, .toast, .acctmenu').length, 0,
    'untrusted HTML kept app layout classes (overlay.show = position:fixed full-screen layer over the app)');
});

// REG-F2
test('REG-F2: a model link with its own rel/target still gets target=_blank + rel=noopener noreferrer', () => {
  const env = boot({ storage: ASHA });
  const d = md(env, '<a href="https://evil.example/" rel="nofollow">a</a> <a href="https://evil.example/" target="_self">b</a> <a target="_top" href="https://evil.example/">c</a>');
  const out = [...d.querySelectorAll('a')].map(a => `${a.getAttribute('target')}|${a.getAttribute('rel')}`);
  assert.deepEqual(out, Array(3).fill('_blank|noopener noreferrer'),
    'sanitizeInto sets rel/target while handling href, then strips them when the loop reaches the original rel/target attribute');
});

// REG-F3
test('REG-F3: a "$" amount before a citation does not swallow the citation as math', () => {
  const env = boot({ storage: ASHA });
  const d = md(env, 'The fee is $5 per term [Fees · p2] and the late fine is $10 [Fees · p3].');
  assert.deepEqual([...d.querySelectorAll('.cite')].map(c => c.textContent), ['Fees · p2', 'Fees · p3'],
    'MATH_RE treats "$5 … $" as inline math, so the citation inside is never gilded (and KaTeX would typeset the sentence)');
});

// REG-F4
test('REG-F4: model text that contains the U+2063 placeholder markers is shown as written (not replaced / deleted)', () => {
  const env = boot({ storage: ASHA });
  const d = md(env, `Step ${PH}M0${PH} first, then $x^2$; also ${PH}M7${PH} here.`);
  const t = d.textContent;
  assert.equal((t.match(/\$x\^2\$/g) || []).length, 1, `smuggled placeholder was replaced with someone else's math: ${JSON.stringify(t)}`);
  assert.ok(t.includes('M7'), `text silently deleted: ${JSON.stringify(t)}`);
});

test('gilding: [Notes · p1] and [web · site] gild; [0, b-1], arr[i], code, pre, links and math are untouched', () => {
  const env = boot({ storage: ASHA });
  const d = md(env, 'Range [0, b-1] and arr[i] [Notes · p1] [web · en.wikipedia.org]\n\n```\n[Code · p1]\n```\n\n`[Inline · p2]` [Link · x](https://a.example) $[Math · p3]$ $$[Disp · p4]$$');
  const cites = [...d.querySelectorAll('.cite')];
  assert.deepEqual(cites.map(c => c.textContent), ['Notes · p1', '🌐 web · en.wikipedia.org']);
  assert.ok(cites[1].classList.contains('web') && !cites[0].classList.contains('web'));
  assert.equal(d.querySelector('pre code').textContent, '[Code · p1]\n');
  assert.equal(d.querySelector('p code').textContent, '[Inline · p2]');
  assert.equal(d.querySelector('a').textContent, 'Link · x');
  assert.ok(d.textContent.includes('[0, b-1]') && d.textContent.includes('$[Math · p3]$') && d.textContent.includes('$$[Disp · p4]$$'));
});

/* =====================================================================
 * 2. Sessions
 * ===================================================================== */

test('boot: token → home; nothing stored → plain login (no message, no requests)', async () => {
  const a = boot({ storage: ASHA }); await a.settle();
  assert.ok(a.visible('#app') && !a.visible('#login'));
  const b = boot(); await b.settle();
  assert.ok(b.visible('#login'));
  assert.equal(b.$('#authErr').style.display, 'none');
  assert.equal(a.calls.length + b.calls.length, 0);
});

test('boot: legacy storage (vs_user/vs_email, no vs_token) → forced sign-in with a message; old chats and identity cleared', async () => {
  const env = boot({ storage: { vs_user: 'Old', vs_email: 'old@x.com', vs_chats: JSON.stringify([{ title: 'old', time: 1, html: '<p>x</p>' }]) } });
  await env.settle();
  assert.ok(env.visible('#login') && !env.visible('#app'));
  assert.match(env.$('#authErr').textContent, /sign in again/i);
  assert.equal(env.$('#authBtnText').textContent, 'Sign in');
  assert.equal(env.ls('vs_user'), null); assert.equal(env.ls('vs_email'), null); assert.equal(env.ls('vs_chats'), null);
  assert.equal(env.calls.length, 0);
  assert.deepEqual(env.errors, []);
});

test('401 on a call → login with the server message; open chat saved first (owned); one call, no loop; busy released; no error bubble', async () => {
  let expired = false;
  const env = boot({ storage: ASHA, routes: { '/api/answer': () => expired ? resp(401, { ok: false, error: 'Your session has expired. Please sign in again.' }) : ans('A1') } });
  await env.ask('first question');
  expired = true;
  await env.ask('second');
  assert.ok(env.visible('#login') && !env.visible('#app'));
  assert.equal(env.$('#authErr').textContent, 'Your session has expired. Please sign in again.');
  assert.equal(env.callsTo('/api/answer').length, 2);
  assert.equal(env.ev('busy'), false);
  assert.equal(env.$('#loader'), null);
  const c = chats(env);
  assert.equal(c.length, 1); assert.equal(c[0].owner, 'asha@x.com'); assert.equal(c[0].title, 'first question');
  assert.equal(env.ls('vs_token'), null);
  assert.ok(!/session has expired/.test(env.$('#transcript').textContent));
});

// REG-F5
test('REG-F5: after a 401, signing in as a DIFFERENT account does not show, save or send the previous account\'s chat', async () => {
  let expired = false;
  const env = boot({ storage: ASHA, routes: {
    // (the mock answers per token: Bala's own reply must not carry Asha's marker text)
    '/api/answer': (b, call) => expired ? resp(401, { ok: false, error: 'Your session has expired. Please sign in again.' })
      : ans(authOf(call) === 'Bearer tok-bala' ? 'bala answer' : 'ASHA-PRIVATE-ANSWER'),
    '/api/login': BALA_LOGIN } });
  env.window.setModeByName('teach');
  await env.ask('asha private question');
  expired = true;
  await env.ask('follow-up');                  // 401 → login screen
  await signIn(env, 'bala@x.com');            // Bala signs in on the same tab
  assert.ok(env.visible('#app'));
  assert.doesNotMatch(env.$('#transcript').textContent, /ASHA-PRIVATE-ANSWER|asha private question/, "Bala sees Asha's open chat");
  expired = false;
  env.window.setModeByName('teach');
  await env.ask('bala question');
  const hist = env.callsTo('/api/answer').at(-1).body.history;
  assert.deepEqual(hist, [], "Asha's teach history was sent to the server under Bala's token");
  env.window.newChat();
  const leaked = chats(env).filter(c => JSON.stringify(c).includes('ASHA-PRIVATE-ANSWER'));
  assert.deepEqual(leaked.map(c => c.owner).filter(o => o !== 'asha@x.com'), [], "Asha's conversation saved under Bala's account");
  assert.deepEqual(leaked, [], 'signing in as a different account must clear the previous account\'s chats (doAuth guard is dead: sessionExpired already removed vs_email)');
});

// REG-F6
test('REG-F6: a 401 while a panel is open closes the panel (it otherwise sits above the login screen)', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/material': resp(401, { ok: false, error: 'Please sign in.' }) } });
  env.window.openMaterial(); await env.settle();
  assert.ok(env.visible('#login'));
  assert.ok(!env.$('#overlay').classList.contains('show'), '#overlay (z-index 40) stays open over #login (z-index 20)');
  assert.ok(!env.document.body.classList.contains('nav-open'));
});

test('sign-out calls /api/logout with the bearer token and never re-saves the open chat', async () => {
  const env = boot({ storage: ASHA });
  await env.ask('secret question');
  env.window.signOut();
  env.window.dispatchEvent(new env.window.Event('beforeunload'));
  env.window.saveCurrentChat();
  const c = env.callsTo('/api/logout');
  assert.equal(c.length, 1); assert.equal(authOf(c[0]), AUTH); assert.equal(c[0].method, 'POST');
  assert.equal(env.ls('vs_chats'), null); assert.equal(env.ls('vs_token'), null);
});

// REG-F7
test('REG-F7: the /api/logout request survives the immediate page reload (keepalive or awaited)', async () => {
  const slow = deferred();
  const env = boot({ storage: ASHA, routes: { '/api/logout': slow } });
  const ret = env.window.signOut();
  const call = env.callsTo('/api/logout')[0];
  assert.ok(call, 'no logout request');
  let awaited = false;
  if (ret && typeof ret.then === 'function') { let done = false; ret.then(() => { done = true; }); await env.settle(); awaited = !done; }
  slow.resolve({ ok: true }); await env.settle();
  assert.ok(call.init.keepalive === true || awaited,
    'signOut fires fetch() then location.reload() synchronously; the navigation aborts the request, so the server session is never revoked');
});

test('two accounts\' chats in vs_chats never cross (owner field): list, restore, save', async () => {
  const bala = { id: 111, owner: 'bala@x.com', title: 'bala chat', time: 2, log: [{ t: 'user', text: 'BALA SECRET' }], history: [] };
  const asha = { id: 222, owner: 'asha@x.com', title: 'asha chat', time: 1, log: [{ t: 'user', text: 'asha text' }], history: [] };
  const env = boot({ storage: { ...ASHA, vs_chats: JSON.stringify([bala, asha]) } });
  env.window.openRecent();
  assert.deepEqual(env.$$('.recent .r').map(r => r.firstChild.nodeValue), ['asha chat']);
  env.window.restoreChat(111);
  assert.doesNotMatch(env.$('#transcript').textContent, /BALA SECRET/);
  await env.ask('more');
  env.window.newChat();
  const after = chats(env);
  assert.deepEqual(after.find(c => c.id === 111), bala, "Bala's stored chat changed");
  assert.ok(after.filter(c => c.owner === 'asha@x.com').every(c => !JSON.stringify(c).includes('BALA')));
  env.window.clearChats();
  assert.deepEqual(chats(env), [bala], "Clear all removed another account's chats");
});

test('403 (wrong current password) keeps the student signed in and shows the message', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/account/password': resp(403, { ok: false, error: 'Current password is wrong.' }) } });
  env.window.openSettings();
  env.$('#pwOld').value = 'bad'; env.$('#pwNew').value = 'newpass1';
  await env.window.doChangePassword();
  assert.equal(env.$('#pwMsg').textContent, 'Current password is wrong.');
  assert.ok(env.visible('#app') && !env.visible('#login'));
  assert.equal(env.ls('vs_token'), 'tok-asha');
});

/* =====================================================================
 * 3. Recent chats as data
 * ===================================================================== */

test('restore → continue chatting → New chat updates the same entry (no duplicate) with the follow-up', async () => {
  const env = boot({ storage: ASHA });
  await env.ask('first chat');
  env.window.newChat();
  env.window.openRecent(); env.click(env.$('.recent .r'));
  await env.ask('follow-up question');
  env.window.newChat();
  const c = chats(env);
  assert.equal(c.length, 1);
  assert.equal(c[0].title, 'first chat');
  assert.deepEqual(c[0].log.filter(e => e.t === 'user').map(e => e.text), ['first chat', 'follow-up question']);
});

// REG-F8
test('REG-F8: re-opening the SAME recent chat after continuing it keeps the new messages', async () => {
  const env = boot({ storage: ASHA });
  await env.ask('first chat');
  env.window.newChat();
  env.window.openRecent(); env.click(env.$('.recent .r'));
  await env.ask('follow-up question');
  env.window.openRecent(); env.click(env.$('.recent .r'));      // same chat again from Recent
  assert.match(env.$('#transcript').textContent, /follow-up question/, 'restoreChat skips saving when the id matches and replays the stale copy');
  env.window.newChat();
  assert.ok(JSON.stringify(chats(env)).includes('follow-up question'), 'follow-up permanently lost');
});

test('corrupted vs_chats JSON → empty Recent, New chat still works, no errors', async () => {
  const env = boot({ storage: { ...ASHA, vs_chats: '{not json' } });
  env.window.openRecent();
  assert.match(env.$('#modal').textContent, /No past chats/);
  await env.ask('hello');
  env.window.newChat();
  assert.equal(chats(env).length, 1);
  assert.deepEqual(env.errors, []);
});

// REG-F9
test('REG-F9: malformed entries in vs_chats ([null], log with null / missing r) do not break New chat or restore', async () => {
  const env = boot({ storage: { ...ASHA, vs_chats: JSON.stringify([null, { id: 5, owner: 'asha@x.com', title: 'broken', time: 1,
    log: [{ t: 'user', text: 'hi' }, null, { t: 'answer' }, { t: 'exam' }, { t: 'answer', r: { answer: 'ok', sources: [null] } }], history: [] }]) } });
  await env.ask('hello');
  let threw = null;
  try { env.window.newChat(); } catch (e) { threw = e.message; }
  assert.equal(threw, null, 'saveCurrentChat dereferences c.id on a null entry → New chat throws');
  assert.ok(env.visible('#hero'), 'New chat did not reset');
  env.window.openRecent();
  const row = env.$$('.recent .r').find(r => /broken/.test(r.textContent));
  env.click(row);
  assert.ok(!env.$('#overlay').classList.contains('show'), 'restore threw half-way; the Recent panel stayed open');
  assert.match(env.$('#transcript').textContent, /hi/);
  assert.deepEqual(env.errors, []);
});

test('legacy {html} chats and crafted log entries are sanitized on restore', async () => {
  const crafted = { id: 9, owner: 'asha@x.com', title: 'x', time: 1, history: [], log: [
    { t: 'user', text: '<img src=x onerror=alert(1)>', who: '<img src=x onerror=alert(2)>' },
    { t: 'answer', r: { answer: '<img src=x onerror=alert(3)> [x](javascript:alert(4))', decision: 'answer', caveat: '<img src=x onerror=alert(5)>',
      sources: [{ source: '<script>alert(6)</script>', loc: '<img src=x onerror=alert(7)>' }], web_used: true,
      web_sources: [{ url: 'javascript:alert(8)', source: '<img src=x onerror=alert(9)>' }, { url: 'https://e.example/" onmouseover="alert(10)' }] } },
    { t: 'exam', r: { grounded: true, topic: '<img src=x onerror=alert(11)>', items: [{ q: '<svg onload=alert(12)>', a: '<img src=x onerror=alert(13)>' }] } },
    { t: 'grade', r: { grounded: true, feedback: '<details open ontoggle=alert(14)>x</details>' } },
    { t: 'error', msg: '<img src=x onerror=alert(15)>' },
    { t: '<img>', msg: '<img src=x onerror=alert(16)>' },
  ] };
  const legacy = { title: 'old', time: 7, history: [], html: '<div class="msg user"><div class="bubble">q</div></div><img src=x onerror=alert(17)><a href="javascript:alert(18)">j</a><script>alert(19)</script><svg onload=alert(20)></svg>' };
  const env = boot({ storage: { ...ASHA, vs_chats: JSON.stringify([crafted, legacy]) } });
  env.window.restoreChat(9);
  assert.deepEqual(injected(env.$('#transcript')), []);
  assert.equal(env.$$('#transcript img, #transcript svg, #transcript details, #transcript script').length, 0);
  for (const b of env.$$('#transcript .bubble')) assert.deepEqual(unsafe(b).filter(x => x !== 'b[style]'), []); // b[style] = the app's own grade header
  env.window.restoreChat(7);
  assert.deepEqual(injected(env.$('#transcript')), []);
  assert.equal(env.$$('#transcript img, #transcript svg').length, 0);
  assert.deepEqual(env.errors, []);
});

/* =====================================================================
 * 4. In-flight requests
 * ===================================================================== */

test('restore / New chat while an exam or grade is pending: nothing lands in the wrong chat; busy released', async () => {
  const ex = deferred(), gr = deferred();
  const env = boot({ storage: ASHA, prompts: ['Q?', 'my answer'], routes: { '/api/exam': ex, '/api/grade': gr } });
  await env.ask('saved chat'); env.window.newChat();
  env.window.setModeByName('exam');
  env.$('#input').value = 'primes'; env.window.send(); await env.settle();
  env.window.openRecent(); env.click(env.$('.recent .r'));
  ex.resolve({ grounded: true, topic: 'primes', items: [{ q: 'STALE-EXAM', a: 'a' }] }); await env.settle();
  assert.doesNotMatch(env.$('#transcript').textContent, /STALE-EXAM/);
  assert.equal(env.ev('busy'), false);
  env.window.openGrade(); await env.settle();
  env.window.newChat();
  gr.resolve({ grounded: true, feedback: 'STALE-GRADE' }); await env.settle();
  assert.doesNotMatch(env.$('#transcript').textContent, /STALE-GRADE/);
  assert.ok(env.visible('#hero'));
});

test('sign-out while an answer is pending: the late answer is not saved', async () => {
  const slow = deferred();
  const env = boot({ storage: ASHA, routes: { '/api/answer': slow } });
  env.$('#input').value = 'pending q'; env.window.send(); await env.settle();
  env.window.signOut();
  slow.resolve(ans('LATE')); await env.settle();
  env.window.dispatchEvent(new env.window.Event('beforeunload'));
  assert.equal(env.ls('vs_chats'), null);
});

// REG-F10
test('REG-F10: switching to Teach while a Quick answer is in flight does not put that exchange into teach history', async () => {
  const slow = deferred();
  const env = boot({ storage: ASHA, routes: { '/api/answer': slow } });
  env.$('#input').value = 'quick question'; env.window.send(); await env.settle();
  env.click('.chip[data-mode=teach]');
  slow.resolve(ans('quick answer')); await env.settle();
  // (compare as JSON: an array created inside the page is from another JS realm)
  assert.equal(env.ev('JSON.stringify(history)'), '[]',
    'send() checks the CURRENT mode after the await, not the mode the question was asked in');
});

// REG-F11
test('REG-F11: Grade my answer while an answer is pending is blocked (or queued) — never two #loader elements', async () => {
  const slow = deferred(), gr = deferred();
  const env = boot({ storage: ASHA, prompts: ['Q?', 'A'], routes: { '/api/answer': slow, '/api/grade': gr } });
  env.$('#input').value = 'q'; env.window.send(); await env.settle();
  env.window.openGrade(); await env.settle();
  assert.deepEqual(dupIds(env), [], 'openGrade ignores busy → a second id="loader"');
  slow.resolve(ans('a')); gr.resolve({ grounded: true, feedback: 'f' }); await env.settle();
});

/* =====================================================================
 * 5. Errors and the status pill
 * ===================================================================== */

test('{ok:false,error} on HTTP 200 is shown as an error (answer, exam, delete), not as an answer', async () => {
  const env = boot({ storage: ASHA, routes: {
    '/api/answer': { ok: false, error: 'Question is too long.' },
    '/api/exam': { ok: false, error: 'Topic is empty.' },
    '/api/material/delete': { ok: false, error: 'Locked.' } } });
  await env.ask('q');
  assert.ok(env.lastBot().classList.contains('refuse'));
  assert.equal(env.lastBot().querySelector('.bubble').textContent, 'Question is too long.');
  assert.equal(env.lastBot().querySelector('.meta'), null);
  env.window.setModeByName('exam'); await env.ask('t');
  assert.equal(env.lastBot().querySelector('.bubble').textContent, 'Topic is empty.');
  await env.window.delMaterial('a.txt');
  assert.match(env.$('#toast').textContent, /Locked\./);
});

test('413 / 429 / 503 / network-failure wording, and the status pill text + state', async () => {
  let n = 0;
  const seq = [resp(503, 'Service Unavailable'), resp(429, '<html>'), 'throw', ans('ok')];
  const env = boot({ storage: ASHA, routes: {
    '/api/answer': () => { const s = seq[n++]; if (s === 'throw') throw new TypeError('Failed to fetch'); return s; },
    '/api/upload': resp(413, '<html>Request Entity Too Large</html>') } });
  const pill = () => [env.$('#statusText').textContent, env.$('#statusDot').className];
  await env.ask('a');
  assert.match(env.lastBot().textContent, /isn't running/);
  assert.deepEqual(pill(), ['Service offline', 'dot warn']);
  await env.ask('b');
  assert.match(env.lastBot().textContent, /Too many attempts/);
  await env.ask('c');
  assert.match(env.lastBot().textContent, /not reachable/);
  assert.deepEqual(pill(), ['Unreachable', 'dot down']);
  await env.ask('d');
  assert.deepEqual(pill(), ['Active', 'dot']);
  await env.window.doUpload(new env.window.File(['x'], 'big.pdf')); await env.settle();
  assert.match(env.$('#toast').textContent, /too large/i);
  assert.equal(env.$('.pill').getAttribute('aria-live'), 'polite');
});

// REG-F12
test('REG-F12: a 200 response that is not JSON is reported as an error, not as "I don\'t have that in the material"', async () => {
  const env = boot({ storage: ASHA, prompts: ['Q?', 'A'], routes: { '/api/exam': '<html>tunnel interstitial</html>', '/api/grade': '<html>tunnel interstitial</html>' } });
  env.window.setModeByName('exam');
  await env.ask('primes');
  assert.doesNotMatch(env.lastBot().textContent, /don't have that in the material/, 'garbage response rendered as an honest refusal (misleading)');
  env.window.openGrade(); await env.settle();
  assert.doesNotMatch(env.lastBot().textContent, /don't have that in the material/);
});

/* =====================================================================
 * 6. Keyboard / a11y
 * ===================================================================== */

test('nav items, Settings footer and the account chip activate with Enter and Space (once)', async () => {
  const env = boot({ storage: ASHA });
  const titles = { 1: 'Recent chats', 2: 'My material', 4: 'Trust & citations' };
  for (const [i, t] of Object.entries(titles)) for (const k of ['Enter', ' ']) {
    env.window.closeModal();
    const ev = key(env, env.navLinks()[i], k); await env.settle();
    assert.ok(ev.defaultPrevented, 'page would scroll / follow href="#"');
    assert.equal(env.$('#modal h2').textContent, t); assert.equal(env.activeNav(), +i);
  }
  env.window.closeModal();
  key(env, '.footer', ' ');
  assert.equal(env.$('#modal h2').textContent, 'Settings');
  assert.ok(env.$('.footer').classList.contains('on'));
  env.window.closeModal();
  key(env, '#acctChip', 'Enter');
  assert.ok(env.$('#acctMenu').classList.contains('show'));
  assert.equal(env.$('#acctChip').getAttribute('aria-expanded'), 'true');
  key(env, '#acctChip', ' ');
  assert.equal(env.$('#acctChip').getAttribute('aria-expanded'), 'false');
});

// REG-F13
test('REG-F13: account-menu items work from the keyboard (Enter on "Settings" is not swallowed by the chip)', () => {
  const env = boot({ storage: ASHA });
  key(env, '#acctChip', 'Enter');
  const item = [...env.$$('#acctMenu button')].find(b => b.textContent === 'Settings');
  item.focus();
  const ev = key(env, item, 'Enter');
  assert.ok(!ev.defaultPrevented, "chip's keydown handler (keyActivate) catches the bubbled Enter, preventDefaults the button's activation and toggles the menu shut");
});

// REG-F14
test('REG-F14: every role=button control responds to Enter/Space (starters, "Sign in" toggle, modal ✕)', async () => {
  const env = boot({ storage: ASHA, prompts: [null] });
  const dead = [];
  key(env, env.$$('#starters .starter')[0], 'Enter');
  if (env.$('#input').value !== 'Explain a concept from my notes') dead.push('starter "Explain a concept"');
  key(env, env.$$('#starters .starter')[3], ' ');
  if (env.ev('mode') !== 'teach') dead.push('starter "Teach me step by step"');
  env.window.openTrust();
  key(env, '#modal .x', 'Enter');
  if (env.$('#overlay').classList.contains('show')) dead.push('modal ✕');
  const login = boot();
  key(login, '#authToggle b', 'Enter');
  if (login.$('#authBtnText').textContent !== 'Sign in') dead.push('"Sign in" toggle on the login card (keyboard users cannot reach sign-in)');
  assert.deepEqual(dead, []);
});

// REG-F15
test('REG-F15: web-fallback switch exposes its state correctly (role=switch → aria-checked) and stays in sync with the Settings default', () => {
  const env = boot({ storage: { ...ASHA, vs_web: '1' } });
  const wt = env.$('#webToggle');
  const state = () => wt.getAttribute('aria-checked') ?? wt.getAttribute('aria-pressed');
  assert.ok(wt.classList.contains('on'));
  assert.equal(state(), 'true', 'boot with web default ON: switch reads as off to a screen reader');
  env.window.openSettings(); env.click('#webDefBtn');           // default → Off
  assert.ok(!wt.classList.contains('on'));
  assert.equal(state(), 'false');
  key(env, wt, ' ');
  assert.equal(state(), 'true');
  assert.ok(wt.hasAttribute('aria-checked'), 'role=switch uses aria-checked; aria-pressed is not a supported state of a switch');
});

// REG-F16
test('REG-F16: aria-expanded tracks the account menu and the phone menu button (incl. Escape)', () => {
  const env = boot({ storage: ASHA });
  env.click('#acctChip');
  escape(env);
  assert.ok(!env.$('#acctMenu').classList.contains('show'));
  assert.equal(env.$('#acctChip').getAttribute('aria-expanded'), 'false', 'Escape closes the menu but leaves aria-expanded="true"');
  const mb = env.$('.menubtn');
  env.click(mb);
  assert.equal(mb.getAttribute('aria-expanded'), 'true', 'phone menu button has no expanded state');
  escape(env);
  assert.equal(mb.getAttribute('aria-expanded'), 'false');
});

test('Escape closes the panel, the phone sidebar and the account menu; highlight returns to the base nav item', () => {
  const env = boot({ storage: ASHA });
  env.window.openPractice(); env.window.openTrust();
  env.document.body.classList.add('nav-open'); env.click('#acctChip');
  escape(env);
  assert.ok(!env.$('#overlay').classList.contains('show'));
  assert.ok(!env.document.body.classList.contains('nav-open'));
  assert.ok(!env.$('#acctMenu').classList.contains('show'));
  assert.equal(env.activeNav(), 3);
});

// REG-F17
test('REG-F17: dialog focus — opening a panel moves focus into it; re-rendering Settings does not drop focus to <body>', () => {
  const env = boot({ storage: ASHA });
  const link = env.navLinks()[4]; link.focus();
  key(env, link, 'Enter');
  assert.ok(env.$('#modal').contains(env.document.activeElement), `focus stayed on ${env.document.activeElement.textContent.trim()} behind an aria-modal dialog`);
  escape(env);
  assert.equal(env.document.activeElement, link, 'focus not returned to the opener');
  env.window.openSettings();
  const teach = env.$$('#modal button').find(b => b.textContent === 'Teach'); teach.focus(); env.click(teach);
  assert.notEqual(env.document.activeElement, env.document.body, 'setDefaultMode re-renders the modal and focus falls to <body>');
});

/* =====================================================================
 * 7. Phone layout
 * ===================================================================== */

test('phone: menu button toggles body.nav-open, scrim closes it, opening a panel / Practice / New chat closes it', async () => {
  const env = boot({ storage: ASHA });
  const open = () => env.document.body.classList.contains('nav-open');
  env.click('.menubtn'); assert.ok(open());
  env.click('.scrim'); assert.ok(!open());
  for (const act of [() => env.click(env.navLinks()[2]), () => env.click(env.navLinks()[3]), () => env.click('.newchat'), () => env.click('.footer')]) {
    env.click('.menubtn'); assert.ok(open());
    act(); await env.settle();
    assert.ok(!open());
  }
});

test('phone (static CSS): sidebar off-canvas + scrim only when nav-open, menu button shown only on phones', () => {
  const css = [...HTML.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n');
  const phone = (css.match(/@media \(max-width: 760px\)\{([\s\S]*?)\n\}/) || [])[1] || '';
  assert.match(css, /\.menubtn\{display:none/);
  assert.match(css, /\.scrim\{display:none\}/);
  assert.match(phone, /\.menubtn\{display:flex\}/);
  assert.match(phone, /\.sidebar\{[^}]*position:fixed[^}]*transform:translateX\(-102%\)/);
  assert.match(phone, /body\.nav-open \.sidebar\{transform:none\}/);
  assert.match(phone, /body\.nav-open \.scrim\{display:block/);
  const z = s => +(new RegExp(s + '\\{[^}]*z-index:(\\d+)').exec(phone) || [])[1];
  assert.ok(z('\\.sidebar') > z('body\\.nav-open \\.scrim'), 'scrim above the sidebar would swallow taps');
});

/* =====================================================================
 * 8. Static
 * ===================================================================== */

test('static: no duplicate ids and every inline on* handler resolves across login toggle, panels and menu', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/material': { docs: [{ name: 'a.txt', size: 10, indexed: true }] } } });
  const names = new Set();
  const collect = html => { for (const m of html.matchAll(/\son\w+="([^"]*)"/g)) for (const f of m[1].matchAll(/(?:^|[;{(\s!&|])([a-zA-Z_$][\w$]*)\s*\(/g)) names.add(f[1]); };
  collect(HTML);
  for (const open of ['openSettings', 'openMaterial', 'openRecent', 'openTrust']) { env.window[open](); await env.settle(); assert.deepEqual(dupIds(env), [], open); collect(env.$('#modal').innerHTML); }
  env.window.toggleAuth(); collect(env.$('#authToggle').innerHTML); env.window.toggleAuth(); collect(env.$('#authToggle').innerHTML);
  const builtin = new Set(['if', 'getElementById', 'click', 'focus', 'preventDefault', 'stopPropagation']);
  assert.deepEqual([...names].filter(n => !builtin.has(n) && typeof env.window[n] !== 'function'), []);
});

test('static: no console errors on load for legacy storage, corrupted chats, or a hostile stored name', async () => {
  const envs = [
    boot({ storage: { vs_user: 'x' } }),
    boot({ storage: { vs_token: 'only-token' } }),
    boot({ storage: { ...ASHA, vs_chats: '[[[', vs_mode: '<img>', vs_web: 'yes' } }),
    boot({ storage: { ...ASHA, vs_user: '  ' } }),
  ];
  for (const e of envs) await e.settle();
  assert.deepEqual(envs.flatMap(e => e.errors), []);
  assert.equal(envs[2].ev('mode'), 'quick');
});
