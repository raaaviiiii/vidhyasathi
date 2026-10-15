// Phase 2 (Ask for Help) — adversarial round + regression pass over the rest of the UI.
// Every test states intended behaviour (qa/SPEC.md + the phase-2 UI intent); a failing
// test is a bug, named by its ID in qa/reports/phase2_frontend.md (P2F-n).
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, closeAll, injected, resp, deferred, USER, AUTH, authOf, HTML } from './harness.mjs';

afterEach(closeAll);

/* ------------------------------ helpers ------------------------------ */
const ASHA = USER;
const BALA_LOGIN = { ok: true, name: 'Bala Raman', email: 'bala@x.com', token: 'tok-bala' };
const EVIL = '<img src=x onerror="window.__pwned=1">';
const tentative = { answer: 'Maybe it is X [Notes · p1]', decision: 'answer_uncertain', top_score: 0.41, caveat: '',
  offer_help: true, web_used: false, web_sources: [], sources: [{ source: 'Notes', loc: 'p1', score: 0.41, ocr: false }] };
const refused = { answer: "I don't have that in the material.", decision: 'refuse', top_score: 0.1, caveat: '',
  offer_help: true, web_used: false, web_sources: [], sources: [] };
const web = { answer: 'From the web [web · example.org]', decision: 'answer_web', top_score: 0.2, caveat: '', offer_help: false,
  web_used: true, web_sources: [{ source: 'example.org', url: 'https://example.org/a' }], sources: [] };
const ok = (answer) => ({ answer, decision: 'answer', top_score: 0.8, caveat: '', offer_help: false, web_used: false, web_sources: [], sources: [] });
const student = (o = {}) => ({ ok: true, role: 'student', is_helper: false, is_admin: false, mine: [], inbox: [], ...o });
const helperP = (o = {}) => ({ ok: true, role: 'helper', is_helper: true, is_admin: false, mine: [], inbox: [], ...o });
const ownerP = (o = {}) => ({ ok: true, role: 'student', is_helper: true, is_admin: true, mine: [], inbox: [], ...o });
const doubt = (o = {}) => ({ id: 1, question: 'Why is r < b?', attempt: 'Maybe [Notes · p1]', decision: 'answer_uncertain',
  note: '', status: 'open', created: 1700000000, answer: null, answered_at: null, helpful: null, sources: ['Notes · p1'],
  helper_name: 'Meera Teacher', mine: true, ...o });
/** /api/doubts handler: GET → payload, POST → sent-ok (optionally deferred). */
const doubtsRoute = (get, post = { ok: true, routed_to: 'Meera Teacher' }) =>
  (b, call) => call.method === 'POST' ? (typeof post === 'function' ? post(b, call) : post) : (typeof get === 'function' ? get(b, call) : get);
const posts = (env, p = '/api/doubts') => env.calls.filter(c => c.path === p && c.method === 'POST');

function key(env, target, k, extra = {}) {
  if (typeof target === 'string') target = env.$(target);
  const ev = new env.window.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...extra });
  target.dispatchEvent(ev); return ev;
}
async function signIn(env, email = 'bala@x.com') {
  if (env.ev('authMode') !== 'login') env.window.toggleAuth();
  env.$('#anEmail').value = email; env.$('#anPass').value = 'pw';
  await env.window.doAuth(); await env.settle();
}
const dupIds = env => { const seen = {}; for (const e of env.$$('[id]')) seen[e.id] = (seen[e.id] || 0) + 1; return Object.keys(seen).filter(k => seen[k] > 1); };
const btnText = (root, t) => [...root.querySelectorAll('button')].find(b => b.textContent === t);
async function openAsk(env) { env.click(env.lastBot().querySelector('.askhelp')); await env.settle(); }
const overlayOpen = env => env.$('#overlay').classList.contains('show');

/* =====================================================================
 * Bugs
 * ===================================================================== */

// P2F-1
test('P2F-1: a second 401 (two requests in flight when the session ends) must not wipe who was signed in — the next account never sees the previous chat', async () => {
  // Student path: an answer is pending, the student opens "My doubts"; both come back 401.
  let expired = false;
  const slow = deferred();
  const env = boot({ storage: ASHA, routes: {
    '/api/answer': (b, call) => authOf(call) === 'Bearer tok-bala' ? ok('bala answer') : (expired ? slow() : ok('ASHA-PRIVATE-ANSWER')),
    '/api/doubts': (b, call) => authOf(call) === 'Bearer tok-bala' ? student() : (expired ? resp(401, { ok: false, error: 'Your session has expired. Please sign in again.' }) : student()),
    '/api/login': BALA_LOGIN } });
  await env.settle();
  await env.ask('asha private question');
  expired = true;
  await env.ask('asha follow-up');             // pending
  env.click('#navDoubts'); await env.settle(); // 401 #1 → login
  assert.ok(env.visible('#login'));
  slow.resolve({ ok: false, error: 'Please sign in.' }, 401); await env.settle();   // 401 #2 (no token any more)
  await signIn(env, 'bala@x.com');
  assert.doesNotMatch(env.$('#transcript').textContent, /ASHA-PRIVATE-ANSWER|asha private question/,
    "Bala sees Asha's chat: the 2nd sessionExpired() set lastAccount = '' so doAuth's account-switch reset is skipped");

  // Owner path: opening Settings with an expired token — renderHelperSettings carries on after
  // the failed refresh with the stale admin ROLE and fires /api/admin/* without a token → 2nd 401.
  let expired2 = false;
  const own = boot({ storage: ASHA, routes: {
    '/api/answer': ok('OWNER-PRIVATE-ANSWER'),
    '/api/doubts': (b, call) => authOf(call) === 'Bearer tok-bala' ? student() : (expired2 ? resp(401, { ok: false, error: 'expired' }) : ownerP()),
    '/api/admin/helpers': (b, call) => authOf(call) === AUTH && !expired2 ? { ok: true, helpers: [] } : resp(401, { ok: false, error: 'expired' }),
    '/api/admin/stats': (b, call) => authOf(call) === AUTH && !expired2 ? { ok: true, weeks: [], doubts: 0, answered: 0, open: 0 } : resp(401, { ok: false, error: 'expired' }),
    '/api/login': BALA_LOGIN } });
  await own.settle();
  await own.ask('owner private question');
  expired2 = true;
  own.window.openSettings(); await own.settle(20);
  assert.ok(own.visible('#login'));
  await signIn(own, 'bala@x.com');
  assert.doesNotMatch(own.$('#transcript').textContent, /OWNER-PRIVATE-ANSWER|owner private question/, "Bala sees the owner's chat");
});

// P2F-2
test('P2F-2: double-clicking "Send to a helper" (or re-opening the dialog while the first send is pending) creates ONE doubt', async () => {
  const slow = deferred();
  const env = boot({ storage: ASHA, routes: { '/api/answer': tentative, '/api/doubts': doubtsRoute(student(), slow) } });
  await env.ask('why is r less than b?');
  await openAsk(env);
  env.click('#hsend'); env.click('#hsend'); await env.settle();
  assert.equal(posts(env).length, 1, 'double click on Send posted the doubt twice');
  // Escape, open the dialog again from the same answer and send again while #1 is pending
  key(env, env.document.body, 'Escape');
  const b = env.lastBot().querySelector('.askhelp');
  if (!b.disabled) { env.click(b); await env.settle(); if (env.$('#hsend')) { env.click('#hsend'); await env.settle(); } }
  assert.equal(posts(env).length, 1, 'the Ask button stays live while the first doubt is in flight');
});

// P2F-3
test('P2F-3: a doubt already sent from an answer is remembered when that chat is restored from Recent (no second doubt)', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/answer': tentative, '/api/doubts': doubtsRoute(student()) } });
  await env.ask('why is r less than b?');
  await openAsk(env); env.click('#hsend'); await env.settle();
  assert.ok(env.lastBot().querySelector('.askhelp').disabled);
  env.window.newChat();
  env.window.openRecent(); env.click(env.$$('.recent .r')[0]); await env.settle();
  const b = env.lastBot().querySelector('.askhelp');
  assert.ok(b, 'restored answer lost its button');
  assert.ok(b.disabled && /Sent to Meera Teacher/.test(b.textContent),
    `restored button reads "${b.textContent}" and is live — the student can send the same doubt again`);
  // same across a page reload (chat saved by beforeunload)
  const again = env.reload({ routes: { '/api/doubts': doubtsRoute(student()) } });
  again.window.openRecent(); again.click(again.$$('.recent .r')[0]); await again.settle();
  assert.ok(again.lastBot().querySelector('.askhelp').disabled, 'after reload + restore the button is live again');
});

// P2F-4
test('P2F-4: after a 401, the next account does not inherit the previous account\'s role, inbox nav, badges or owner tools', async () => {
  let expired = false;
  const env = boot({ storage: ASHA, routes: {
    '/api/answer': (b, call) => expired && authOf(call) === AUTH ? resp(401, { ok: false, error: 'Session expired.' }) : ok('x'),
    '/api/doubts': (b, call) => authOf(call) === 'Bearer tok-bala' ? resp(503, { ok: false, error: 'Qdrant is down' })
      : ownerP({ inbox: [doubt({ id: 4, mine: false, student_name: 'Kiran' }), doubt({ id: 5, mine: false, student_name: 'Devi' })],
        mine: [doubt({ id: 9, status: 'answered', answer: 'y' })] }),
    '/api/admin/helpers': { ok: true, helpers: [] }, '/api/admin/stats': { ok: true, weeks: [], doubts: 0, answered: 0, open: 0 },
    '/api/login': BALA_LOGIN } });
  await env.settle();
  assert.equal(env.$('#inboxBadge').textContent, '2');
  expired = true;
  await env.ask('q');                       // 401 → login
  await signIn(env, 'bala@x.com');          // Bala (a student); his /api/doubts fails once
  assert.ok(env.$('#navInbox').classList.contains('hidden'), "Bala (student) is shown Asha's Helper inbox nav");
  assert.ok(env.$('#inboxBadge').closest('.hidden') && env.$('#doubtsBadge').classList.contains('hidden'), "Asha's badge counts shown to Bala");
  env.window.openSettings(); await env.settle(20);
  assert.doesNotMatch(env.$('#helperSettings').textContent, /Helpers & escalations/, 'owner tools rendered for a student from the stale ROLE');
});

// P2F-5
test('P2F-5: a doubt that finishes sending after the dialog was dismissed neither closes the panel opened since nor fails silently', async () => {
  const slow = deferred();
  const env = boot({ storage: ASHA, routes: { '/api/answer': refused, '/api/doubts': doubtsRoute(student(), slow) } });
  await env.ask('q');
  await openAsk(env); env.click('#hsend'); await env.settle();
  key(env, env.document.body, 'Escape');
  env.window.openMaterial(); await env.settle();
  assert.equal(env.$('#modal h2').textContent, 'My material');
  slow.resolve({ ok: true, routed_to: 'Meera Teacher' }); await env.settle();
  assert.ok(overlayOpen(env) && env.$('#modal h2').textContent === 'My material', 'the late success called closeModal() and shut "My material"');
  // ...and a late FAILURE is written into the detached dialog: the student never learns it wasn't sent
  const slow2 = deferred();
  const f = boot({ storage: ASHA, routes: { '/api/answer': refused, '/api/doubts': doubtsRoute(student(), slow2) } });
  await f.ask('q');
  await openAsk(f); f.click('#hsend'); await f.settle();
  key(f, f.document.body, 'Escape');
  slow2.resolve({ ok: false, error: 'You already have 20 open doubts.' }, 429); await f.settle();
  assert.match(f.$('#toast').textContent + (overlayOpen(f) && f.$('#hmsg') ? f.$('#hmsg').textContent : ''), /20 open doubts/, 'send failed silently after the dialog was dismissed');
});

// P2F-6
test('P2F-6: the owner\'s ✕ (remove helper) asks to confirm before demoting a teacher and unassigning their open doubts', async () => {
  const env = boot({ storage: ASHA, confirm: false, routes: { '/api/doubts': ownerP(),
    '/api/admin/helpers': (b, call) => call.method === 'POST' ? { ok: true } : { ok: true, helpers: [{ name: 'Meera', email: 'm@x.com', expertise: 'graphs', open: 3, answered: 9 }] },
    '/api/admin/stats': { ok: true, weeks: [], doubts: 0, answered: 0, open: 0 } } });
  env.window.openSettings(); await env.settle(20);
  env.click(env.$('#helperSettings .docdel')); await env.settle();
  assert.equal(env.confirms.length, 1, 'no confirmation (material ✕ asks; this one is more destructive)');
  assert.equal(posts(env, '/api/admin/helpers').length, 0, 'helper removed although the owner cancelled');
});

// P2F-7
test('P2F-7: "Was this helpful?" can only be answered once — fast clicks post once, not Yes AND Not really', async () => {
  const slow = deferred();
  const env = boot({ storage: ASHA, routes: { '/api/doubts': student({ mine: [doubt({ id: 2, status: 'answered', answer: 'because' })] }),
    '/api/doubts/2/feedback': slow } });
  env.click('#navDoubts'); await env.settle();
  const card = env.$('#dlist .doubt');
  env.click(btnText(card, 'Yes')); env.click(btnText(card, 'Not really')); await env.settle();
  assert.equal(env.callsTo('/api/doubts/2/feedback').length, 1, 'both answers were posted; UI then says "Thanks"');
});

// P2F-8
test('P2F-8: focus is not dropped after sending a doubt or rating an answer (keyboard users keep their place)', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/answer': tentative, '/api/doubts': doubtsRoute(student()) } });
  await env.ask('q');
  const b = env.lastBot().querySelector('.askhelp'); b.focus();
  env.click(b); await env.settle();
  env.click('#hsend'); await env.settle();
  const a = env.document.activeElement;
  assert.ok(a !== env.document.body && !env.$('#modal').contains(a) && !a.disabled,
    `after Send focus is on ${a === env.document.body ? '<body>' : a.outerHTML.slice(0, 60)} (the opener was disabled before focus was returned to it)`);

  const env2 = boot({ storage: ASHA, routes: { '/api/doubts': student({ mine: [doubt({ id: 2, status: 'answered', answer: 'x' })] }), '/api/doubts/2/feedback': { ok: true } } });
  env2.click('#navDoubts'); await env2.settle();
  const yes = btnText(env2.$('#dlist'), 'Yes'); yes.focus(); env2.click(yes); await env2.settle();
  assert.notEqual(env2.document.activeElement, env2.document.body, 'rating replaces the focused button and focus falls to <body>');
});

// P2F-9
test('P2F-9: the "Ask a helper" dialog does not mark "My doubts" as the current page', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/answer': tentative } });
  await env.ask('q');
  await openAsk(env);
  assert.notEqual(env.activeNav(), 5, 'My doubts is highlighted (aria-current=page) while the Ask dialog is shown over the chat');
});

// P2F-10
test('P2F-10: a withdrawn doubt is not described as "waiting for a helper"', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/doubts': student({ mine: [doubt({ id: 3, status: 'closed', helper_name: null })] }) } });
  env.click('#navDoubts'); await env.settle();
  const card = env.$('#dlist .doubt');
  assert.match(card.querySelector('.st').textContent, /withdrawn/);
  assert.doesNotMatch(card.textContent, /waiting for a helper/, 'chip says "withdrawn", meta line says "→ waiting for a helper"');
});

// P2F-11
test('P2F-11: "My helper subjects" — saving the (always empty) field does not silently wipe the helper\'s subjects', async () => {
  const env = boot({ storage: ASHA, confirm: false, routes: { '/api/doubts': helperP(), '/api/me/expertise': { ok: true } } });
  env.window.openSettings(); await env.settle();
  const inp = env.$('#helperSettings input');
  assert.equal(inp.value, '', '(precondition: the field is never prefilled — the UI has no way to know current subjects)');
  env.click(btnText(env.$('#helperSettings'), 'Save subjects')); await env.settle();
  const sent = env.callsTo('/api/me/expertise').map(c => c.body.expertise);
  assert.ok(!sent.includes(''), 'empty subjects posted without warning → helper stops being matched to doubts');
});

// P2F-12
test('P2F-12: a long attempt is truncated on a character boundary (no lone surrogate → server 422 "not a valid string")', async () => {
  const long = { ...tentative, answer: 'a'.repeat(7999) + '😀 tail' };
  const env = boot({ storage: ASHA, routes: { '/api/answer': long, '/api/doubts': doubtsRoute(student()) } });
  await env.ask('q');
  await openAsk(env); env.click('#hsend'); await env.settle();
  const att = posts(env)[0].body.attempt;
  assert.ok(!/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(att), 'attempt ends in half an emoji');
});

// P2F-13
test('P2F-13 (static CSS): with seven nav items the sidebar can scroll, so Settings is reachable on short screens', () => {
  const css = [...HTML.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n');
  const rules = [...css.matchAll(/(?:^|\})\s*([^{}@]*\.sidebar[^{}]*)\{([^}]*)\}/g)].filter(m => /(^|,)\s*(body\.nav-open\s+)?\.sidebar\s*(,|$)/.test(m[1]));
  assert.ok(rules.length, 'no .sidebar rule found');
  assert.ok(rules.some(m => /overflow(-y)?:\s*(auto|scroll)/.test(m[2])),
    '.sidebar has no overflow-y; ~545px of content (7 nav items + New chat + Settings) is clipped on a 390px-tall landscape phone or with the keyboard up, hiding Settings/Sign out');
});

// P2F-14
test('P2F-14: after a helper sends an answer, the card reflects it ("answered", "Update answer")', async () => {
  let answered = false;
  const env = boot({ storage: ASHA, routes: {
    '/api/doubts': () => helperP({ inbox: [doubt({ id: 4, mine: false, student_name: 'Kiran', ...(answered ? { status: 'answered', answer: 'Because.' } : {}) })] }),
    '/api/doubts/4/answer': () => { answered = true; return { ok: true }; } } });
  await env.settle();
  env.click('#navInbox'); await env.settle();
  const card = env.$('#ilist .doubt');
  card.querySelector('textarea').value = 'Because.';
  env.click(btnText(card, 'Send answer')); await env.settle();
  assert.equal(env.$('#inboxBadge').classList.contains('hidden'), true);
  const live = env.$('#ilist .doubt');
  assert.ok(!btnText(live, 'Send answer') && btnText(live, 'Update answer'), 'button still says "Send answer"');
  assert.match(live.querySelector('.st').textContent, /answered/, 'chip still says "waiting" although the badge already dropped');
});

/* =====================================================================
 * Verified behaviour (should pass)
 * ===================================================================== */

test('XSS: every new untrusted field (question, attempt, note, answer, names, sources, routed_to, admin rows, stats) stays inert', async () => {
  const md = `**ok** [x](javascript:alert(1)) <div class="overlay show"><a class="enter" href="https://evil.example">Sign in</a></div> ${EVIL} $\\href{javascript:alert(2)}{x}$`;
  // My doubts
  const s = boot({ storage: ASHA, routes: { '/api/doubts': student({ mine: [
    doubt({ id: 2, status: 'answered', question: EVIL, helper_name: EVIL, answer: md }),
    doubt({ id: 3, status: 'open', question: '<svg onload=alert(1)>', helper_name: '<b>x</b>' })] }) } });
  s.click('#navDoubts'); await s.settle();
  assert.deepEqual(injected(s.$('#dlist')), []);
  assert.equal(s.$('#dlist .overlay, #dlist .enter, #dlist .show'), null, 'helper markdown borrowed app classes');
  assert.ok(![...s.$$('#dlist a')].some(a => /javascript:/i.test(a.getAttribute('href') || '')));
  assert.match(s.$('#dlist .dq').textContent, /<img src=x/);
  // Inbox
  const h = boot({ storage: ASHA, routes: { '/api/doubts': helperP({ inbox: [doubt({ id: 4, mine: false, student_name: EVIL,
    question: EVIL, note: EVIL, attempt: md, sources: [EVIL, '<script>alert(1)</script>'] })] }) } });
  await h.settle(); h.click('#navInbox'); await h.settle();
  assert.deepEqual(injected(h.$('#ilist')), []);
  assert.equal(h.$('#ilist .overlay, #ilist .enter'), null);
  assert.match(h.$('#ilist details .dm').textContent, /<script>/);
  // Ask dialog: routed_to and a hostile question
  const a = boot({ storage: ASHA, routes: { '/api/answer': tentative, '/api/doubts': doubtsRoute(student(), { ok: true, routed_to: EVIL }) } });
  await a.ask(EVIL);
  await openAsk(a);
  assert.equal(a.$('#hq').value, EVIL);
  assert.equal(a.$('#modal img, #modal svg'), null, 'prefilled question parsed as HTML');
  a.click('#hsend'); await a.settle();
  assert.deepEqual(injected(a.$('#transcript')), []); assert.deepEqual(injected(a.$('#toast')), []);
  assert.match(a.lastBot().querySelector('.askhelp').textContent, /<img src=x/);
  // Owner list + stats with string numbers
  const o = boot({ storage: ASHA, routes: { '/api/doubts': ownerP(),
    '/api/admin/helpers': { ok: true, helpers: [{ name: EVIL, email: '"><img src=x onerror=alert(1)>', expertise: EVIL, open: EVIL, answered: '<b>9</b>' }] },
    '/api/admin/stats': { ok: true, doubts: EVIL, answered: '<i>5</i>', open: '2', avg_hours_to_answer: EVIL, helpful_rate: '0.5',
      weeks: [{ questions: '3', doubts: 1, escalation_rate: '0.33' }] } } });
  o.window.openSettings(); await o.settle(20);
  assert.deepEqual(injected(o.$('#helperSettings')), []);
  assert.match(o.$('#helperSettings').textContent, /<img src=x/);
  for (const e of [s, h, a, o]) { assert.equal(e.window.__pwned, undefined); assert.deepEqual(e.errors, []); }
});

test('button rules: tentative + refused show it, grounded and web (offer_help false) never; restored unsent chat button still sends', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/answer': web, '/api/doubts': doubtsRoute(student()) } });
  await env.ask('web q');
  assert.equal(env.lastBot().querySelector('.askhelp'), null);
  const r = boot({ storage: ASHA, routes: { '/api/answer': refused, '/api/doubts': doubtsRoute(student()) } });
  await r.ask('restore me');
  r.window.newChat();
  r.window.openRecent(); r.click(r.$$('.recent .r')[0]); await r.settle();
  await openAsk(r);
  assert.equal(r.$('#hq').value, 'restore me');
  r.click('#hsend'); await r.settle();
  assert.equal(posts(r).length, 1);
  assert.equal(authOf(posts(r)[0]), AUTH);
  assert.equal(posts(r)[0].body.decision, 'refuse');
  assert.ok(r.lastBot().querySelector('.askhelp').disabled);
});

test('401 while the ask dialog / My doubts / inbox is open → login, panel closed, no error bubble; same account can continue', async () => {
  let expired = false;
  const env = boot({ storage: ASHA, routes: { '/api/answer': tentative,
    '/api/doubts': doubtsRoute(() => expired ? resp(401, { ok: false, error: 'Session expired.' }) : student(),
      () => expired ? resp(401, { ok: false, error: 'Session expired.' }) : { ok: true, routed_to: 'Meera' }) } });
  await env.ask('q');
  await openAsk(env);
  expired = true;
  env.click('#hsend'); await env.settle();
  assert.ok(env.visible('#login') && !overlayOpen(env));
  assert.equal(env.$('#authErr').textContent, 'Session expired.');
  assert.ok(!/Session expired/.test(env.$('#transcript').textContent));
  expired = false;
  await signIn(env, 'asha@x.com');
  assert.match(env.$('#transcript').textContent, /Maybe it is X/);
  await openAsk(env); env.click('#hsend'); await env.settle();
  assert.ok(env.lastBot().querySelector('.askhelp').disabled);
  expired = true;
  env.click('#navDoubts'); await env.settle();
  assert.ok(env.visible('#login') && !overlayOpen(env));
});

test('badge: answered-not-viewed per account; viewing clears it for that account only; survives reload; B\'s badge independent', async () => {
  const mineA = [doubt({ id: 2, status: 'answered', answer: 'a' }), doubt({ id: 3, status: 'answered', answer: 'b' }), doubt({ id: 4 })];
  const a = boot({ storage: ASHA, routes: { '/api/doubts': student({ mine: mineA }) } });
  await a.settle();
  assert.equal(a.$('#doubtsBadge').textContent, '2');
  a.click('#navDoubts'); await a.settle();
  assert.ok(a.$('#doubtsBadge').classList.contains('hidden'));
  const a2 = a.reload({ routes: { '/api/doubts': student({ mine: [...mineA, doubt({ id: 5, status: 'answered', answer: 'c' })] }) } });
  await a2.settle();
  assert.equal(a2.$('#doubtsBadge').textContent, '1', 'only the new answer is badged after reload');
  // sign out → Bala signs in on the same browser; his doubt ids collide with Asha's
  const storage = a2.storageSnapshot(); for (const k of ['vs_user', 'vs_email', 'vs_token']) delete storage[k];
  const b = boot({ storage, routes: { '/api/login': BALA_LOGIN,
    '/api/doubts': (x, call) => authOf(call) === 'Bearer tok-bala' ? student({ mine: [doubt({ id: 2, status: 'answered', answer: 'z' })] }) : student() } });
  await signIn(b, 'bala@x.com');
  assert.equal(b.$('#doubtsBadge').textContent, '1', "Asha's seen-set suppressed Bala's badge");
});

test('Helper inbox: hidden for students even if the server sends inbox items; role changes mid-session show/hide it', async () => {
  let payload = student({ inbox: [doubt({ id: 4, mine: false })] });
  const env = boot({ storage: ASHA, routes: { '/api/doubts': () => payload } });
  await env.settle();
  assert.ok(env.$('#navInbox').classList.contains('hidden'));
  payload = helperP({ inbox: [doubt({ id: 4, mine: false })] });   // promoted by the owner
  env.window.openSettings(); await env.settle();
  assert.ok(!env.$('#navInbox').classList.contains('hidden'));
  assert.match(env.$('#helperSettings').textContent, /My helper subjects/);
  env.window.closeModal();
  payload = student();                                              // demoted again
  env.click('#navDoubts'); await env.settle();
  assert.ok(env.$('#navInbox').classList.contains('hidden'));
});

test('keyboard + nav: Enter on My doubts / Helper inbox opens them, focus moves in, Escape returns focus and highlight', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/doubts': helperP() } });
  await env.settle();
  for (const [id, title, idx] of [['#navDoubts', 'My doubts', 5], ['#navInbox', 'Helper inbox', 6]]) {
    const link = env.$(id); link.focus();
    const ev = key(env, link, 'Enter'); await env.settle();
    assert.ok(ev.defaultPrevented);
    assert.equal(env.$('#modal h2').textContent, title);
    assert.equal(env.activeNav(), idx);
    assert.equal(env.$(id).getAttribute('aria-current'), 'page');
    assert.ok(env.$('#modal').contains(env.document.activeElement));
    key(env, env.document.body, 'Escape');
    assert.equal(env.document.activeElement, link);
    assert.equal(env.activeNav(), 0);
  }
});

test('phone: tapping My doubts / Helper inbox in the open sidebar closes it', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/doubts': helperP() } });
  await env.settle();
  for (const id of ['#navDoubts', '#navInbox']) {
    env.click('.menubtn'); assert.ok(env.document.body.classList.contains('nav-open'));
    env.click(id); await env.settle();
    assert.ok(!env.document.body.classList.contains('nav-open'));
  }
});

test('withdraw: cancelling the confirm posts nothing; confirming re-renders the list', async () => {
  let closed = false;
  const env = boot({ storage: ASHA, confirm: () => closed, routes: {
    '/api/doubts': () => student({ mine: [doubt({ id: 7, status: closed ? 'closed' : 'open' })] }), '/api/doubts/7/close': { ok: true } } });
  env.click('#navDoubts'); await env.settle();
  env.click(btnText(env.$('#dlist'), 'Withdraw')); await env.settle();
  assert.equal(env.callsTo('/api/doubts/7/close').length, 0);
  closed = true;
  env.click(btnText(env.$('#dlist'), 'Withdraw')); await env.settle();
  assert.equal(env.callsTo('/api/doubts/7/close').length, 1);
  assert.match(env.$('#dlist .st').textContent, /withdrawn/);
});

test('every doubts request carries the bearer token; none before sign-in; feedback / answer / admin calls too', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/doubts': ownerP({ mine: [doubt({ id: 2, status: 'answered', answer: 'a' })], inbox: [doubt({ id: 4, mine: false })] }),
    '/api/doubts/2/feedback': { ok: true }, '/api/doubts/4/answer': { ok: true },
    '/api/admin/helpers': { ok: true, helpers: [] }, '/api/admin/stats': { ok: true, weeks: [], doubts: 0, answered: 0, open: 0 } } });
  await env.settle();
  env.click('#navDoubts'); await env.settle(); env.click(btnText(env.$('#dlist'), 'Yes')); await env.settle();
  env.click('#navInbox'); await env.settle(); env.$('#ilist textarea').value = 'ans'; env.click(btnText(env.$('#ilist'), 'Send answer')); await env.settle();
  env.window.openSettings(); await env.settle(20);
  const doubtCalls = env.calls.filter(c => /^\/api\/(doubts|admin|me)/.test(c.path));
  assert.ok(doubtCalls.length >= 6);
  assert.deepEqual(doubtCalls.filter(c => authOf(c) !== AUTH).map(c => c.path), []);
});

test('static: no duplicate ids, inline handlers resolve, no console errors across ask dialog, My doubts, inbox and owner Settings', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/answer': tentative,
    '/api/doubts': ownerP({ mine: [doubt({ id: 2, status: 'answered', answer: 'a' }), doubt({ id: 3 }), doubt({ id: 6, status: 'closed' })],
      inbox: [doubt({ id: 4, mine: false }), doubt({ id: 5, mine: false, status: 'answered', answer: 'x' })] }),
    '/api/admin/helpers': { ok: true, helpers: [{ name: 'M', email: 'm@x.com', expertise: '', open: 0, answered: 0 }] },
    '/api/admin/stats': { ok: true, weeks: [{ questions: 0, doubts: 1, escalation_rate: null }], doubts: 1, answered: 0, open: 1, avg_hours_to_answer: null, helpful_rate: null } } });
  await env.ask('q');
  const names = new Set();
  const collect = h => { for (const m of h.matchAll(/\son\w+="([^"]*)"/g)) for (const f of m[1].matchAll(/(?:^|[;{(\s!&|])([a-zA-Z_$][\w$]*)\s*\(/g)) names.add(f[1]); };
  for (const open of [() => openAsk(env), () => env.window.openDoubts(), () => env.window.openInbox(), () => env.window.openSettings()]) {
    await open(); await env.settle(20);
    assert.deepEqual(dupIds(env), []); collect(env.$('#modal').innerHTML);
  }
  assert.match(env.$('#helperSettings').textContent, /1 doubts · 0 answered · 1 open — escalation rate by week: –/);
  const builtin = new Set(['if', 'getElementById', 'click', 'focus', 'preventDefault', 'stopPropagation']);
  assert.deepEqual([...names].filter(n => !builtin.has(n) && typeof env.window[n] !== 'function'), []);
  assert.deepEqual(env.errors, []);
});

test('regression pass with phase 2 present: ask → material → settings → recent restore → new chat, all still behave', async () => {
  const env = boot({ storage: ASHA, routes: { '/api/answer': ok('X is Y [Notes · p1]'),
    '/api/material': { docs: [{ name: 'notes.pdf', size: 2048, indexed: true }] }, '/api/doubts': student() } });
  await env.settle();
  await env.ask('what is X?');
  assert.equal(env.lastBot().querySelector('.cite').textContent, 'Notes · p1');
  assert.equal(env.lastBot().querySelector('.askhelp'), null);
  env.click(env.navLinks()[2]); await env.settle();
  assert.match(env.$('#doclist').textContent, /notes\.pdf/); assert.equal(env.activeNav(), 2);
  env.window.closeModal();
  env.window.openSettings(); await env.settle();
  assert.match(env.$('#modal').textContent, /asha@x\.com/);
  assert.equal(env.$('#helperSettings').textContent, '', 'a plain student sees no helper/owner sections');
  env.window.closeModal();
  env.window.newChat();
  assert.ok(env.visible('#hero'));
  env.click(env.navLinks()[1]); env.click(env.$$('.recent .r')[0]); await env.settle();
  assert.match(env.$('#transcript').textContent, /what is X\?/);
  assert.equal(env.activeNav(), 0);
  assert.deepEqual(env.errors, []);
});
