// renderAnswer / renderExam / grade rendering + XSS.
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, closeAll, injected, USER } from './harness.mjs';

afterEach(closeAll);

const base = { answer: 'x', decision: 'answer', top_score: 0.5, caveat: '', offer_help: false, web_used: false, web_sources: [], sources: [] };
function render(env, r) { env.window.reveal(); env.window.renderAnswer({ ...base, ...r }); return env.lastBot(); }

test('decision "answer": grounded badge, score, citation pills, markdown rendered', () => {
  const env = boot({ storage: USER });
  const d = render(env, { answer: '**Prime**: has two divisors [Number Theory · p1]', top_score: 0.734,
    sources: [{ source: 'Number Theory', loc: 'p1', score: 0.73, ocr: false }] });
  const tags = [...d.querySelectorAll('.meta .tag')].map(t => t.textContent);
  assert.deepEqual(tags, ['grounded', 'score 0.73']);
  assert.ok(d.querySelector('.tag.ok'));
  assert.equal(d.querySelector('.bubble strong').textContent, 'Prime');
  const cite = d.querySelector('.bubble .cite');
  assert.equal(cite.textContent, 'Number Theory · p1');
  assert.ok(!cite.classList.contains('web'));
  assert.equal(d.querySelector('.srcline').textContent, 'sources: Number Theory · p1');
  assert.ok(!d.classList.contains('refuse'));
});

test('decision "answer_uncertain": tentative badge + ask-a-human line', () => {
  const env = boot({ storage: USER });
  const d = render(env, { decision: 'answer_uncertain', offer_help: true, top_score: 0.4 });
  assert.equal(d.querySelector('.meta .tag.warn').textContent, 'tentative');
  assert.match(d.textContent, /ask a human/i);
});

test('decision "refuse": refuse styling, "not in material" badge, exact refusal text', () => {
  const env = boot({ storage: USER });
  const d = render(env, { decision: 'refuse', answer: "I don't have that in the material.", offer_help: true, top_score: 0.12 });
  assert.ok(d.classList.contains('refuse'));
  assert.equal(d.querySelector('.meta .tag.no').textContent, 'not in material');
  assert.equal(d.querySelector('.bubble').textContent.trim(), "I don't have that in the material.");
});

test('refusal offer_help line does not say "Not fully sure" (nothing was answered)', () => {
  const env = boot({ storage: USER });
  const d = render(env, { decision: 'refuse', answer: "I don't have that in the material.", offer_help: true });
  const notes = [...d.querySelectorAll('.note')].map(n => n.textContent).join(' ');
  assert.doesNotMatch(notes, /not fully sure/i, 'a refusal is certain it lacks the material; "Not fully sure" contradicts the honest refusal');
});

test('decision "answer_web": web badge, 🌐 disclosure with domain links, web pill style', () => {
  const env = boot({ storage: USER });
  const d = render(env, { decision: 'answer_web', web_used: true, answer: 'Photosynthesis... [web · en.wikipedia.org]',
    web_sources: [{ source: 'web · en.wikipedia.org', url: 'https://en.wikipedia.org/wiki/Photosynthesis' }] });
  assert.ok(d.classList.contains('web'));
  assert.equal(d.querySelector('.meta .tag').textContent, 'from the web');
  assert.ok(d.querySelector('.bubble .cite.web'));
  assert.match(d.querySelector('.bubble .cite.web').textContent, /^🌐 web · en\.wikipedia\.org$/);
  const a = d.querySelector('.webdisc a');
  assert.equal(a.getAttribute('href'), 'https://en.wikipedia.org/wiki/Photosynthesis');
  assert.equal(a.textContent, 'en.wikipedia.org/wiki/Photosynthesis');
  assert.match(d.querySelector('.webdisc').textContent, /^🌐 Not in your notes/);
});

test('web disclosure links open with rel="noopener" (target=_blank tabnabbing)', () => {
  const env = boot({ storage: USER });
  const d = render(env, { decision: 'answer_web', web_used: true, web_sources: [{ source: 'web · a.com', url: 'https://a.com/x' }] });
  const a = d.querySelector('.webdisc a');
  assert.equal(a.getAttribute('target'), '_blank');
  assert.match(a.getAttribute('rel') || '', /noopener/);
});

test('caveat is shown once with a single ↳ marker', () => {
  const env = boot({ storage: USER });
  const d = render(env, { caveat: '↳ This draws on a handwritten/scanned source — worth double-checking the equation.' });
  const n = d.querySelector('.note');
  assert.equal((n.textContent.match(/↳/g) || []).length, 1);
  assert.match(n.textContent, /double-checking the equation/);
});

test('sources line dedupes repeated source·loc and caps at 5', () => {
  const env = boot({ storage: USER });
  const s = (source, loc) => ({ source, loc, score: 0.5, ocr: false });
  const d = render(env, { sources: [s('A', 'p1'), s('A', 'p1'), s('A', 'p2'), s('B', 'p1'), s('C', 'p1'), s('D', 'p1'), s('E', 'p1'), s('A', 'p1')] });
  assert.equal(d.querySelector('.srcline').textContent, 'sources: A · p1;  A · p2;  B · p1;  C · p1;  D · p1');
});

test('no score tag when top_score missing; no srcline when no sources', () => {
  const env = boot({ storage: USER });
  const d = render(env, { top_score: undefined, sources: [] });
  assert.equal(d.querySelectorAll('.meta .tag').length, 1);
  assert.equal(d.querySelector('.srcline'), null);
});

test('unknown decision value does not render an empty badge', () => {
  const env = boot({ storage: USER });
  const d = render(env, { decision: 'weird' });
  for (const t of d.querySelectorAll('.meta .tag')) assert.notEqual(t.textContent.trim(), '', 'empty decision badge pill');
});

test('a citation tag is not mangled into a pill inside inline code / markdown links', () => {
  const env = boot({ storage: USER });
  const d = render(env, { answer: 'Use `arr[i]` to index. See [the docs](https://example.org). [Notes · p3]' });
  assert.equal(d.querySelector('.bubble code').textContent, 'arr[i]', 'mdGild turned code "arr[i]" into a citation pill');
  assert.equal(d.querySelectorAll('.bubble .cite').length, 1);
});

test('math with < or [ ] survives rendering (e.g. $a<b$, interval $[0,1]$)', () => {
  const env = boot({ storage: USER });
  const d = render(env, { answer: 'If $a<b$ and $x \\in [0,1]$ then done [Notes · p2]' });
  const txt = d.querySelector('.bubble').textContent;
  assert.ok(txt.includes('$a<b$'), `inequality inside math was parsed as an HTML tag: ${JSON.stringify(txt)}`);
  assert.ok(txt.includes('[0,1]'), `interval inside math became a citation pill: ${JSON.stringify(txt)}`);
  assert.equal(d.querySelectorAll('.bubble .cite').length, 1);
});

/* ------------------------------ XSS ------------------------------ */

test('XSS: model answer with <img onerror> / <script> does not execute (real marked.js)', () => {
  const env = boot({ storage: USER });
  env.window.__pwned = 0;
  const d = render(env, { answer: 'Hi <img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script> [Notes · p1]' });
  assert.deepEqual(injected(d), [], 'model output became live HTML (SPEC: untrusted text must never execute)');
});

test('XSS: model answer with a javascript: markdown link is not clickable script', () => {
  const env = boot({ storage: USER });
  const d = render(env, { answer: '[click me](javascript:alert(document.cookie))' });
  assert.deepEqual(injected(d), []);
});

test('XSS: model answer HTML is escaped when marked.js CDN fails to load (offline)', () => {
  const env = boot({ storage: USER, marked: false });
  const d = render(env, { answer: '<img src=x onerror=alert(1)> [Notes · p1]' });
  assert.deepEqual(injected(d), []);
});

test('XSS: HTML hidden inside a [citation] tag is not injected', () => {
  const env = boot({ storage: USER });
  const d = render(env, { answer: 'x [<img src=x onerror=alert(1)>]' });
  assert.deepEqual(injected(d), []);
});

test('XSS: payload wrapped in $…$ math delimiters is not injected (math path bypasses markdown)', () => {
  const env = boot({ storage: USER });
  const d = render(env, { answer: 'Formula: $<img src=x onerror=alert(1)>$ [Notes · p1]' });
  assert.deepEqual(injected(d), []);
});

test('XSS: web_sources url "javascript:alert(1)" is not rendered as a live link', () => {
  const env = boot({ storage: USER });
  const d = render(env, { decision: 'answer_web', web_used: true, web_sources: [{ source: 'web · evil', url: 'javascript:alert(1)' }] });
  assert.deepEqual(injected(d), []);
});

test('XSS: web_sources url with a quote cannot break out into an attribute', () => {
  const env = boot({ storage: USER });
  const d = render(env, { decision: 'answer_web', web_used: true, web_sources: [{ source: 'web · e.com', url: 'https://e.com/" onmouseover="alert(1)' }] });
  assert.deepEqual(injected(d), []);
});

test('XSS: caveat and sources fields are rendered as text', () => {
  const env = boot({ storage: USER });
  const d = render(env, { caveat: '<img src=x onerror=alert(1)>', sources: [{ source: '<img src=x onerror=alert(2)>', loc: 'p1' }] });
  assert.deepEqual(injected(d), []);
});

test('XSS: exam topic, question and answer text cannot inject', () => {
  const env = boot({ storage: USER });
  env.window.reveal();
  env.window.renderExam({ grounded: true, topic: '<img src=x onerror=alert(1)>', raw: '',
    items: [{ q: '<img src=x onerror=alert(2)>', a: '<img src=x onerror=alert(3)> [N · p1]' }] });
  assert.deepEqual(injected(env.lastBot()), []);
});

test('XSS: exam raw fallback (no parsed items) cannot inject', () => {
  const env = boot({ storage: USER });
  env.window.reveal();
  env.window.renderExam({ grounded: true, topic: 't', items: [], raw: '"><svg onload=alert(1)>' });
  assert.deepEqual(injected(env.lastBot()), []);
});

test('XSS: grade feedback from the model cannot inject', async () => {
  const env = boot({ storage: USER, prompts: ['Q', 'A'], routes: { '/api/grade': { grounded: true, feedback: '**Correct:** <img src=x onerror=alert(1)>' } } });
  env.window.openGrade(); await env.settle();
  assert.deepEqual(injected(env.lastBot()), []);
});

test('XSS: error message text (from server) is rendered as text', () => {
  const env = boot({ storage: USER });
  env.window.reveal();
  env.window.renderError('<img src=x onerror=alert(1)>');
  assert.deepEqual(injected(env.lastBot()), []);
});

test('grade flow: grounded feedback shows "not a grade" header + cited feedback; off-material refuses', async () => {
  const env = boot({ storage: USER, prompts: ['What is a prime?', 'two divisors', 'Quantum?', 'idk'],
    routes: { '/api/grade': (b) => b.question === 'Quantum?' ? { grounded: false, feedback: '' } : { grounded: true, feedback: '**Correct:** yes [Notes · p1]\n**Missing:** none' } } });
  env.window.openGrade(); await env.settle();
  const g = env.callsTo('/api/grade')[0].body;
  assert.equal(g.question, 'What is a prime?');
  assert.equal(g.student_answer, 'two divisors');
  assert.match(env.$('#transcript .msg.user .who').textContent, /answer to "What is a prime\?"/);
  assert.match(env.lastBot().textContent, /not a grade/);
  assert.ok(env.lastBot().querySelector('.cite'));
  env.window.openGrade(); await env.settle();
  assert.ok(env.lastBot().classList.contains('refuse'));
  assert.match(env.lastBot().textContent, /can't grade/);
});

test('grade flow: cancelling either prompt sends nothing', async () => {
  const env = boot({ storage: USER, prompts: [null] });
  env.window.openGrade(); await env.settle();
  const env2 = boot({ storage: USER, prompts: ['Q', ''] });
  env2.window.openGrade(); await env2.settle();
  assert.equal(env.calls.filter(c => c.path !== '/api/doubts').length + env2.calls.filter(c => c.path !== '/api/doubts').length, 0);
});
