// Login / signup / identity tests for index.html.
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, closeAll, resp, USER, AUTH, authOf } from './harness.mjs';

afterEach(closeAll);

function fill(env, { name, email, pass }) {
  if (name !== undefined) env.$('#anName').value = name;
  if (email !== undefined) env.$('#anEmail').value = email;
  if (pass !== undefined) env.$('#anPass').value = pass;
}

test('fresh load shows login, not the app, with no console errors', async () => {
  const env = boot();
  await env.settle();
  assert.ok(env.visible('#login'));
  assert.ok(!env.visible('#app'));
  assert.deepEqual(env.errors, []);
});

test('signup: empty fields show an error and make no request', async () => {
  const env = boot();
  fill(env, { name: '', email: 'a@x.com', pass: 'pw' });
  await env.window.doAuth();
  assert.equal(env.$('#authErr').style.display, 'block');
  assert.match(env.$('#authErr').textContent, /fill everything/i);
  assert.equal(env.calls.length, 0);
});

test('signup: server error (409 duplicate email) is shown on the card', async () => {
  const env = boot({ routes: { '/api/signup': resp(409, { ok: false, error: 'An account with that email already exists.' }) } });
  fill(env, { name: 'Asha', email: 'a@x.com', pass: 'pw' });
  await env.window.doAuth();
  assert.equal(env.$('#authErr').textContent, 'An account with that email already exists.');
  assert.ok(env.visible('#login'));
});

test('signup success: goes home, greets by first name, stores name + email', async () => {
  const env = boot({ routes: { '/api/signup': { ok: true, name: 'Asha Kumar', email: 'asha@x.com' } } });
  fill(env, { name: 'Asha Kumar', email: 'Asha@X.com ', pass: 'pw' });
  await env.window.doAuth();
  assert.ok(!env.visible('#login'));
  assert.ok(env.visible('#app'));
  assert.match(env.$('.hero h1').textContent, /Asha$/);
  assert.equal(env.$('.acct .nm').textContent, 'Asha');
  assert.equal(env.$('.acct .av').firstChild.nodeValue, 'A');
  assert.equal(env.ls('vs_user'), 'Asha Kumar');
  assert.equal(env.ls('vs_email'), 'asha@x.com');
  assert.deepEqual(env.callsTo('/api/signup')[0].body, { name: 'Asha Kumar', email: 'Asha@X.com', password: 'pw' });
});

test('sign-in mode: name field hidden, wrong password error shown, Enter submits', async () => {
  const env = boot({ routes: { '/api/login': resp(401, { ok: false, error: 'Wrong email or password.' }) } });
  env.window.toggleAuth();
  assert.equal(env.$('#authName').style.display, 'none');
  assert.equal(env.$('#authBtnText').textContent, 'Sign in');
  fill(env, { email: 'a@x.com', pass: 'bad' });
  env.$('#anPass').dispatchEvent(new env.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await env.settle();
  assert.equal(env.callsTo('/api/login').length, 1);
  assert.equal(env.$('#authErr').textContent, 'Wrong email or password.');
});

test('login with unreachable server shows a plain error', async () => {
  const env = boot({ routes: { '/api/login': () => { throw new TypeError('Failed to fetch'); } } });
  env.window.toggleAuth();
  fill(env, { email: 'a@x.com', pass: 'pw' });
  await env.window.doAuth();
  assert.match(env.$('#authErr').textContent, /not reachable/i);
});

test('reload with a saved user goes straight to home with greeting', async () => {
  const env = boot({ storage: USER });
  await env.settle();
  assert.ok(!env.visible('#login'));
  assert.ok(env.visible('#app'));
  assert.match(env.$('.hero h1').textContent, /Asha$/);
  assert.equal(env.activeNav(), 0);
  assert.deepEqual(env.errors, []);
});

test('user name with HTML is shown as text in greeting, chip, menu and settings', async () => {
  const evil = '<img src=x onerror=alert(1)>';
  const env = boot({ storage: { vs_user: evil, vs_email: '"><img src=x onerror=alert(2)>@x.com', vs_token: 'tok' } });
  await env.settle();
  assert.equal(env.$$('.hero h1 img, .acct img, #acctMenu img').length, 0);
  env.window.openSettings();
  assert.equal(env.$$('#modal img').length, 0);
  assert.ok(env.$('#modal').textContent.includes(evil));
});

test('every request carries the signed-in identity (answer, exam, grade, material, delete)', async () => {
  const env = boot({ storage: USER, prompts: ['What is X?', 'X is Y'] });
  await env.ask('q1');
  env.window.setModeByName('exam');
  await env.ask('topic');
  env.window.openGrade(); await env.settle();
  env.window.openMaterial(); await env.settle();
  await env.window.delMaterial('a.txt'); await env.settle();
  for (const p of ['/api/answer', '/api/exam', '/api/grade', '/api/material', '/api/material/delete'])
    assert.equal(authOf(env.callsTo(p)[0]), AUTH, `${p} sent without the session token`);
});

test('a user saved before the per-user update (vs_user but no vs_email) is not silently put in the shared bucket', async () => {
  // Pre-update sessions only stored vs_user. On reload the UI must either send
  // them back to sign in, or send a real identity — never user:"" (shared bucket).
  const env = boot({ storage: { vs_user: 'Old User' } });
  await env.settle();
  if (env.visible('#login')) return; // acceptable: forced re-login
  await env.ask('what is a prime?');
  assert.ok(authOf(env.callsTo('/api/answer')[0]), 'request sent with no identity at all');
});
