// Phase 2 — Ask for Help UI: the button, My doubts, Helper inbox, owner tools.
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, closeAll, injected, USER, AUTH, authOf } from './harness.mjs';

afterEach(closeAll);

const tentative = { answer: 'Maybe it is X [Notes · p1]', decision: 'answer_uncertain', top_score: 0.41, caveat: '',
  offer_help: true, web_used: false, web_sources: [], sources: [{ source: 'Notes', loc: 'p1', score: 0.41, ocr: false }] };
const refused = { answer: "I don't have that in the material.", decision: 'refuse', top_score: 0.1, caveat: '',
  offer_help: true, web_used: false, web_sources: [], sources: [] };
const grounded = { answer: 'It is X [Notes · p1]', decision: 'answer', top_score: 0.8, caveat: '', offer_help: false,
  web_used: false, web_sources: [], sources: [{ source: 'Notes', loc: 'p1', score: 0.8, ocr: false }] };

const EVIL = '<img src=x onerror="window.__pwned=1">';
const doubt = (o = {}) => ({ id: 1, question: 'Why is r < b?', attempt: 'Maybe [Notes · p1]', decision: 'answer_uncertain',
  note: '', status: 'open', created: 1700000000, answer: null, answered_at: null, helpful: null, sources: ['Notes · p1'],
  helper_name: 'Meera Teacher', mine: true, ...o });

test('"Ask a helper" appears on tentative and refused answers, never on grounded ones', async () => {
  for (const [r, shown] of [[tentative, true], [refused, true], [grounded, false]]) {
    const env = boot({ storage: USER, routes: { '/api/answer': r } });
    await env.ask('why is r less than b?');
    assert.equal(!!env.lastBot().querySelector('.askhelp'), shown, r.decision);
  }
});

test('sending a doubt posts the question, the bot attempt, decision and sources, with the token', async () => {
  const env = boot({ storage: USER, routes: { '/api/answer': tentative,
    '/api/doubts': (b, call) => call.method === 'POST' ? { ok: true, routed_to: 'Meera Teacher', doubt: doubt() }
      : { ok: true, mine: [], inbox: [], role: 'student', is_helper: false, is_admin: false } } });
  await env.ask('why is r less than b?');
  env.click(env.lastBot().querySelector('.askhelp')); await env.settle();
  assert.equal(env.$('#modal h2').textContent, 'Ask a helper');
  assert.equal(env.$('#hq').value, 'why is r less than b?');
  env.$('#hn').value = 'I missed the lecture';
  env.click('#hsend'); await env.settle();
  const c = env.calls.filter(x => x.path === '/api/doubts' && x.method === 'POST')[0];
  assert.deepEqual({ ...c.body, sources: [...c.body.sources] }, { question: 'why is r less than b?', attempt: tentative.answer,
    decision: 'answer_uncertain', sources: ['Notes · p1'], note: 'I missed the lecture' });
  assert.equal(authOf(c), AUTH);
  assert.ok(!env.$('#overlay').classList.contains('show'));
  assert.match(env.$('#toast').textContent, /Sent only to Meera Teacher/);
  assert.match(env.lastBot().querySelector('.askhelp').textContent, /Sent to Meera Teacher/);
  assert.ok(env.lastBot().querySelector('.askhelp').disabled);
});

test('an empty question is not sent; a server error is shown in the dialog', async () => {
  const env = boot({ storage: USER, routes: { '/api/answer': refused,
    '/api/doubts': (b, call) => call.method === 'POST' ? { __resp: true, status: 429, json: { ok: false, error: 'You already have 20 open doubts.' } }
      : { ok: true, mine: [], inbox: [] } } });
  await env.ask('q');
  env.click(env.lastBot().querySelector('.askhelp')); await env.settle();
  env.$('#hq').value = '  ';
  env.click('#hsend'); await env.settle();
  assert.equal(env.calls.filter(x => x.path === '/api/doubts' && x.method === 'POST').length, 0);
  env.$('#hq').value = 'real question';
  env.click('#hsend'); await env.settle();
  assert.equal(env.$('#hmsg').textContent, 'You already have 20 open doubts.');
});

test('My doubts lists status, renders the helper answer safely, and offers feedback once', async () => {
  const answered = doubt({ id: 2, status: 'answered', answer: `Because **r** can't reach b ${EVIL}`, answered_at: 1700003600 });
  const env = boot({ storage: USER, routes: { '/api/doubts': { ok: true, mine: [answered, doubt({ id: 3 })], inbox: [] },
    '/api/doubts/2/feedback': { ok: true } } });
  await env.settle();
  assert.equal(env.$('#doubtsBadge').textContent, '1', 'new answer not badged');
  env.click('#navDoubts'); await env.settle();
  const cards = env.$$('#dlist .doubt');
  assert.equal(cards.length, 2);
  assert.deepEqual(injected(env.$('#dlist')), []);
  assert.equal(window_pwned(env), undefined);
  assert.match(cards[0].querySelector('.da').innerHTML, /<strong>r<\/strong>/);
  assert.match(cards[0].querySelector('.st').textContent, /answered/);
  assert.match(cards[1].querySelector('.st').textContent, /waiting/);
  assert.ok(env.$('#doubtsBadge').classList.contains('hidden'), 'badge not cleared after viewing');
  const yes = [...cards[0].querySelectorAll('button')].find(b => b.textContent === 'Yes');
  env.click(yes); await env.settle();
  assert.deepEqual(env.callsTo('/api/doubts/2/feedback')[0].body, { helpful: true });
  assert.match(cards[0].textContent, /Thanks for the feedback/);
  assert.equal(env.activeNav(), 5);
});

function window_pwned(env) { return env.window.__pwned; }

test('withdrawing an open doubt asks to confirm and posts to /close', async () => {
  const env = boot({ storage: USER, routes: { '/api/doubts': { ok: true, mine: [doubt({ id: 7 })], inbox: [] },
    '/api/doubts/7/close': { ok: true } } });
  env.click('#navDoubts'); await env.settle();
  const w = [...env.$$('#dlist button')].find(b => b.textContent === 'Withdraw');
  env.click(w); await env.settle();
  assert.equal(env.confirms.length, 1);
  assert.equal(env.callsTo('/api/doubts/7/close').length, 1);
});

test('Helper inbox is hidden for students and shown (with a count) for helpers', async () => {
  const s = boot({ storage: USER }); await s.settle();
  assert.ok(s.$('#navInbox').classList.contains('hidden'));
  const h = boot({ storage: USER, routes: { '/api/doubts': { ok: true, role: 'helper', is_helper: true, is_admin: false,
    mine: [], inbox: [doubt({ id: 4, mine: false, student_name: 'Asha' }), doubt({ id: 5, mine: false, status: 'answered', answer: 'x' })] } } });
  await h.settle();
  assert.ok(!h.$('#navInbox').classList.contains('hidden'));
  assert.equal(h.$('#inboxBadge').textContent, '1');
});

test('a helper answers from the inbox; untrusted fields render as text', async () => {
  const item = doubt({ id: 4, mine: false, student_name: EVIL, question: `What is ${EVIL}?`, note: EVIL, attempt: `x ${EVIL}` });
  const env = boot({ storage: USER, routes: { '/api/doubts': { ok: true, role: 'helper', is_helper: true, mine: [], inbox: [item] },
    '/api/doubts/4/answer': { ok: true } } });
  await env.settle();
  env.click('#navInbox'); await env.settle();
  assert.deepEqual(injected(env.$('#ilist')), []);
  assert.match(env.$('#ilist .dq').textContent, /<img src=x/);
  const card = env.$('#ilist .doubt');
  card.querySelector('textarea').value = 'Because r < b by definition.';
  env.click([...card.querySelectorAll('button')].find(b => b.textContent === 'Send answer')); await env.settle();
  assert.deepEqual(env.callsTo('/api/doubts/4/answer')[0].body, { answer: 'Because r < b by definition.' });
  assert.match(card.textContent, /added to their material/);
  assert.equal(env.activeNav(), 6);
});

test('Settings: helpers see "My helper subjects"; the owner sees helper management and stats', async () => {
  const helper = boot({ storage: USER, routes: { '/api/doubts': { ok: true, role: 'helper', is_helper: true, is_admin: false, mine: [], inbox: [] },
    '/api/me/expertise': { ok: true } } });
  helper.window.openSettings(); await helper.settle();
  assert.match(helper.$('#helperSettings').textContent, /My helper subjects/);
  assert.doesNotMatch(helper.$('#helperSettings').textContent, /owner/);
  const inp = helper.$('#helperSettings input'); inp.value = 'graphs, trees';
  helper.click([...helper.$$('#helperSettings button')].find(b => b.textContent === 'Save subjects')); await helper.settle();
  assert.deepEqual(helper.callsTo('/api/me/expertise')[0].body, { expertise: 'graphs, trees' });

  const owner = boot({ storage: USER, routes: {
    '/api/doubts': { ok: true, role: 'student', is_helper: true, is_admin: true, mine: [], inbox: [] },
    '/api/admin/helpers': (b, call) => call.method === 'POST' ? { ok: true }
      : { ok: true, helpers: [{ name: 'Meera', email: 'm@x.com', expertise: 'number theory', open: 2, answered: 5 }] },
    '/api/admin/stats': { ok: true, doubts: 7, answered: 5, open: 2, avg_hours_to_answer: 3.5, helpful_rate: 0.8,
      weeks: [{ questions: 10, doubts: 4, escalation_rate: 0.4 }, { questions: 20, doubts: 2, escalation_rate: 0.1 }] } } });
  owner.window.openSettings(); await owner.settle(20);
  const box = owner.$('#helperSettings').textContent;
  assert.match(box, /Helpers & escalations/);
  assert.match(box, /Meera · m@x.com — number theory/);
  assert.match(box, /7 doubts · 5 answered · 2 open/);
  assert.match(box, /40% → 10%/);
  const [em, sub] = owner.$$('#helperSettings input');
  em.value = 'new@x.com'; sub.value = 'chemistry';
  owner.click([...owner.$$('#helperSettings button')].find(b => b.textContent === 'Make helper')); await owner.settle();
  assert.deepEqual(owner.calls.filter(c => c.path === '/api/admin/helpers' && c.method === 'POST')[0].body,
    { email: 'new@x.com', helper: true, expertise: 'chemistry' });
});

test('no doubts requests when signed out', async () => {
  const env = boot(); await env.settle();
  assert.equal(env.callsTo('/api/doubts').length, 0);
});

test('the ask box shows who the doubt goes to before sending, and lets the student pick', async () => {
  const route = { ok: true, suggested: { id: 4, name: 'Meera Teacher', subjects: 'number theory', open: 0 },
    helpers: [{ id: 4, name: 'Meera Teacher', subjects: 'number theory', open: 0 },
              { id: 9, name: '<b>Charles</b>', subjects: 'chemistry', open: 1 }], fallback: null };
  const env = boot({ storage: USER, routes: { '/api/answer': tentative, '/api/doubts/route': route,
    '/api/doubts': (b, call) => call.method === 'POST' ? { ok: true, routed_to: 'Charles', doubt: doubt() }
      : { ok: true, mine: [], inbox: [], role: 'student', is_helper: false, is_admin: false } } });
  await env.ask('why is r less than b?');
  env.click(env.lastBot().querySelector('.askhelp')); await env.settle();
  const sel = env.$('#hto');
  assert.ok(sel, 'a recipient picker is shown');
  assert.match(sel.options[0].textContent, /Meera Teacher — number theory\s+\(best match\)/);
  assert.equal(sel.querySelector('b'), null, 'names are text, never HTML');
  assert.match(env.$('#hroute').textContent, /Only this person will see your question/);
  assert.equal(env.$('#hsend').textContent, 'Send to Meera Teacher');
  sel.value = '9'; sel.dispatchEvent(new env.window.Event('change')); await env.settle();
  assert.equal(env.$('#hsend').textContent, 'Send to <b>Charles</b>');
  env.click('#hsend'); await env.settle();
  const c = env.calls.filter(x => x.path === '/api/doubts' && x.method === 'POST')[0];
  assert.equal(c.body.helper_id, 9);
  assert.match(env.$('#toast').textContent, /Sent only to Charles/);
});

test('with no helpers set up, the box says the app owner gets it', async () => {
  const env = boot({ storage: USER, routes: { '/api/answer': tentative,
    '/api/doubts/route': { ok: true, suggested: null, helpers: [], fallback: 'the app owner' } } });
  await env.ask('why is r less than b?');
  env.click(env.lastBot().querySelector('.askhelp')); await env.settle();
  assert.match(env.$('#hroute').textContent, /goes to the app owner/);
  assert.equal(env.$('#hsend').textContent, 'Send to the app owner');
});
