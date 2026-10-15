// Composer / send / modes / errors.
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, closeAll, resp, deferred, USER } from './harness.mjs';

afterEach(closeAll);

test('quick mode: send posts question, shows user echo + answer, hides hero', async () => {
  const env = boot({ storage: USER });
  await env.ask('  What is a prime?  ');
  const c = env.callsTo('/api/answer');
  assert.equal(c.length, 1);
  assert.equal(c[0].method, 'POST');
  assert.deepEqual({ ...c[0].body }, { question: 'What is a prime?', mode: 'quick', web: false, history: [], user: 'asha@x.com' });
  assert.ok(!env.visible('#hero'));
  assert.equal(env.$('#transcript .msg.user .bubble').textContent, 'What is a prime?');
  assert.ok(env.lastBot().querySelector('.bubble').textContent.includes('Fine'));
  assert.equal(env.$('#input').value, '');
  assert.equal(env.$('#loader'), null);
});

test('empty / whitespace input does not send', async () => {
  const env = boot({ storage: USER });
  await env.ask('   ');
  assert.equal(env.calls.length, 0);
});

test('Enter sends, Shift+Enter does not', async () => {
  const env = boot({ storage: USER });
  const inp = env.$('#input');
  inp.value = 'line one';
  inp.dispatchEvent(new env.window.KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true }));
  await env.settle();
  assert.equal(env.callsTo('/api/answer').length, 0);
  inp.dispatchEvent(new env.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await env.settle();
  assert.equal(env.callsTo('/api/answer').length, 1);
});

test('Enter while an IME composition is active (Malayalam/Hindi input) does not send', async () => {
  const env = boot({ storage: USER });
  const inp = env.$('#input');
  inp.value = 'പ്രൈം';
  inp.dispatchEvent(new env.window.KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true }));
  await env.settle();
  assert.equal(env.callsTo('/api/answer').length, 0, 'Enter that commits an IME candidate must not submit');
});

test('double-send while busy is blocked', async () => {
  const slow = deferred();
  const env = boot({ storage: USER, routes: { '/api/answer': slow } });
  env.$('#input').value = 'first'; env.window.send();
  env.$('#input').value = 'second'; env.window.send();
  await env.settle();
  assert.equal(env.callsTo('/api/answer').length, 1);
  slow.resolve({ answer: 'ok', decision: 'answer', top_score: 0.8, sources: [] });
  await env.settle();
  env.$('#input').value = 'third'; env.window.send(); await env.settle();
  assert.equal(env.callsTo('/api/answer').length, 2, 'busy flag released after the answer');
});

test('teach mode: history grows and is sent on the follow-up', async () => {
  let n = 0;
  const env = boot({ storage: USER, routes: { '/api/answer': () => ({ answer: 'A' + (++n), decision: 'answer', top_score: 0.8, sources: [] }) } });
  env.window.setModeByName('teach');
  await env.ask('teach me primes');
  await env.ask('why?');
  const c = env.callsTo('/api/answer');
  assert.equal(c[0].body.mode, 'teach');
  assert.deepEqual(c[0].body.history, []);
  assert.deepEqual(c[1].body.history, [{ role: 'user', content: 'teach me primes' }, { role: 'assistant', content: 'A1' }]);
});

test('quick mode never sends history even after a teach exchange', async () => {
  const env = boot({ storage: USER });
  env.window.setModeByName('teach');
  await env.ask('one');
  env.window.setModeByName('quick');
  await env.ask('two');
  assert.deepEqual(env.callsTo('/api/answer')[1].body.history, []);
});

test('exam mode: posts /api/exam with topic + n=5 and renders numbered Q/A', async () => {
  const env = boot({ storage: USER, routes: { '/api/exam': { grounded: true, topic: 'primes', raw: '', items: [{ q: 'Define prime', a: 'A number... [Notes · p1]' }, { q: 'Is 1 prime?', a: 'No [Notes · p2]' }] } } });
  env.window.setModeByName('exam');
  await env.ask('primes');
  const c = env.callsTo('/api/exam');
  assert.equal(c.length, 1);
  assert.equal(c[0].body.topic, 'primes');
  assert.equal(c[0].body.n, 5);
  assert.equal(env.callsTo('/api/answer').length, 0);
  const qs = env.$$('#transcript .examq .q').map(e => e.textContent);
  assert.deepEqual(qs, ['Q1. Define prime', 'Q2. Is 1 prime?']);
  assert.match(env.lastBot().querySelector('.who').textContent, /PRACTICE SET · primes/);
  assert.equal(env.lastBot().querySelectorAll('.cite').length, 2);
});

test('exam mode: off-material topic shows the refusal', async () => {
  const env = boot({ storage: USER, routes: { '/api/exam': { grounded: false, topic: 'quantum', items: [], raw: '' } } });
  env.window.setModeByName('exam');
  await env.ask('quantum');
  assert.ok(env.lastBot().classList.contains('refuse'));
  assert.match(env.lastBot().textContent, /don't have that in the material/);
});

test('backend 500 is reported in plain language, not "Is app.py running?"', async () => {
  const env = boot({ storage: USER, routes: { '/api/answer': resp(500, 'Internal Server Error') } });
  await env.ask('q');
  const txt = env.lastBot().textContent;
  assert.ok(env.lastBot().classList.contains('refuse'));
  assert.doesNotMatch(txt, /Is app\.py running/i, 'the server answered — it IS running; message is misleading (SPEC: UI errors never misleading)');
});

test('backend JSON error (e.g. model down) is surfaced to the student', async () => {
  const env = boot({ storage: USER, routes: { '/api/answer': resp(503, { error: "The model isn't running (Ollama unreachable)." }) } });
  await env.ask('q');
  assert.match(env.lastBot().textContent, /model isn't running/, 'SPEC Ops: clear JSON error; UI: "the model isn\'t running"');
});

test('network failure shows a plain-language message and releases busy', async () => {
  const env = boot({ storage: USER, routes: { '/api/answer': () => { throw new TypeError('Failed to fetch'); } } });
  await env.ask('q');
  assert.doesNotMatch(env.lastBot().textContent, /^\s*VIDHYĀSATHI\s*Failed to fetch\s*$/, 'raw browser exception text is not plain language');
  assert.equal(env.window.eval('busy'), false);
});

test('New chat while an answer is in flight: stale answer does not land in the new chat', async () => {
  const slow = deferred();
  const env = boot({ storage: USER, routes: { '/api/answer': slow } });
  env.$('#input').value = 'old question'; env.window.send(); await env.settle();
  env.window.newChat();
  slow.resolve({ answer: 'STALE ANSWER', decision: 'answer', top_score: 0.8, sources: [] });
  await env.settle();
  assert.ok(!env.$('#transcript').textContent.includes('STALE ANSWER'), 'answer to the previous chat leaked into the new chat');
});

test('user question echo is shown as typed text, including & entities', async () => {
  const env = boot({ storage: USER });
  await env.ask('is &lt;b&gt; a tag? <script>x</script>');
  const b = env.$('#transcript .msg.user .bubble');
  assert.equal(b.querySelectorAll('*').length, 0);
  assert.equal(b.textContent, 'is &lt;b&gt; a tag? <script>x</script>');
});
