// Sidebar nav, recent chats, account menu, settings, starters, static checks.
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, closeAll, injected, HTML, USER } from './harness.mjs';

afterEach(closeAll);

const NAV = { current: 0, recent: 1, material: 2, practice: 3, trust: 4 };

/* ------------------------------ nav ------------------------------ */

test('sidebar has the spec nav items in order + New chat + Settings', () => {
  const env = boot({ storage: USER });
  assert.deepEqual(env.navLinks().map(a => a.textContent.trim()), ['Current chat', 'Recent chats', 'My material', 'Practice sets', 'Trust & citations']);
  assert.match(env.$('.newchat').textContent, /New chat/);
  assert.match(env.$('.footer').textContent, /Settings/);
});

for (const [label, idx, modalTitle] of [['Recent chats', NAV.recent, 'Recent chats'], ['My material', NAV.material, 'My material'], ['Trust & citations', NAV.trust, 'Trust & citations']]) {
  test(`nav "${label}" opens its panel AND highlights that nav item`, async () => {
    const env = boot({ storage: USER });
    env.click(env.navLinks()[idx]); await env.settle();
    assert.ok(env.$('#overlay').classList.contains('show'));
    assert.equal(env.$('#modal h2').textContent, modalTitle);
    assert.equal(env.activeNav(), idx, `"${label}" is shown but "Current chat" stays highlighted (SPEC: active nav reflects what's shown)`);
  });
}

test('closing a panel returns the highlight to Current chat', async () => {
  const env = boot({ storage: USER });
  env.click(env.navLinks()[NAV.trust]);
  env.window.closeModal();
  assert.equal(env.activeNav(), NAV.current);
});

test('nav "Practice sets" switches to exam mode, shows chat, and highlights Practice sets', async () => {
  const env = boot({ storage: USER });
  env.click(env.navLinks()[NAV.practice]); await env.settle();
  assert.equal(env.window.eval('mode'), 'exam');
  assert.ok(env.$('.chip[data-mode=exam]').classList.contains('on'));
  assert.match(env.$('#toast').textContent, /Exam mode/);
  assert.equal(env.activeNav(), NAV.practice, 'Practice sets clicked but Current chat highlighted');
});

test('Settings (sidebar footer) is highlighted/reflected when open', async () => {
  const env = boot({ storage: USER });
  env.click('.footer');
  assert.equal(env.$('#modal h2').textContent, 'Settings');
  assert.equal(env.activeNav(), -1, 'a sidebar item other than Settings still shows as active while Settings is open');
});

test('nav items are keyboard-reachable (href or tabindex + role)', () => {
  const env = boot({ storage: USER });
  for (const a of [...env.navLinks(), env.$('.footer'), env.$('#acctChip')]) {
    const focusable = a.hasAttribute('href') || a.hasAttribute('tabindex') || a.tagName === 'BUTTON';
    assert.ok(focusable, `${a.textContent.trim().slice(0, 20)} is not keyboard focusable`);
  }
});

/* ------------------------------ chats ------------------------------ */

test('New chat saves the current chat to Recent and resets to hero; teach history cleared', async () => {
  const env = boot({ storage: USER });
  env.window.setModeByName('teach');
  await env.ask('teach me primes');
  env.click('.newchat');
  assert.ok(env.visible('#hero'));
  assert.equal(env.$('#transcript').innerHTML, '');
  assert.equal(env.window.eval('JSON.stringify(history)'), '[]');
  const chats = JSON.parse(env.ls('vs_chats'));
  assert.equal(chats.length, 1);
  assert.equal(chats[0].title, 'teach me primes');
  assert.equal(chats[0].history.length, 2);
});

test('restoring a recent chat brings back its transcript and teach history', async () => {
  const env = boot({ storage: USER });
  env.window.setModeByName('teach');
  await env.ask('teach me primes');
  env.window.newChat();
  env.window.openRecent();
  const rows = env.$$('.recent .r');
  assert.equal(rows.length, 1);
  assert.match(rows[0].textContent, /teach me primes/);
  env.click(rows[0]);
  assert.ok(!env.$('#overlay').classList.contains('show'));
  assert.match(env.$('#transcript').textContent, /teach me primes/);
  assert.equal(env.window.eval('history').length, 2);
  assert.equal(env.activeNav(), 0);
});

test('restoring a recent chat then New chat does not duplicate it in Recent', async () => {
  const env = boot({ storage: USER });
  await env.ask('first chat');
  env.window.newChat();
  env.window.openRecent(); env.click(env.$$('.recent .r')[0]);
  env.window.newChat();
  const titles = JSON.parse(env.ls('vs_chats')).map(c => c.title);
  assert.deepEqual(titles, ['first chat'], 'restored chat saved again as a duplicate');
});

test('chat titles in Recent are rendered as text (no HTML injection)', async () => {
  const env = boot({ storage: { ...USER, vs_chats: JSON.stringify([{ title: '<img src=x onerror=alert(1)>', time: 1, html: '', history: [] }]) } });
  env.window.openRecent();
  assert.equal(env.$$('.recent img').length, 0, 'chat title became live HTML');
  assert.match(env.$('.recent .r').textContent, /<img src=x onerror=alert\(1\)>/);
});

test('Recent chats are per-user: after A signs out, B on the same browser cannot see A\'s chat', async () => {
  const a = boot({ storage: USER });
  await a.ask('my private question');          // chat open (not yet saved)
  a.window.signOut();                           // removes vs_chats, then location.reload()
  a.window.dispatchEvent(new a.window.Event('beforeunload')); // what the reload fires in a real browser
  const b = a.reload({ routes: { '/api/login': { ok: true, name: 'Bala', email: 'bala@x.com', token: 'tok-bala' } } });
  assert.ok(b.visible('#login'));
  b.window.toggleAuth();
  b.$('#anEmail').value = 'bala@x.com'; b.$('#anPass').value = 'pw';
  await b.window.doAuth();
  b.window.openRecent();
  assert.doesNotMatch(b.$('#modal').textContent, /my private question/, "B sees A's chat (vs_chats is one browser-wide key, re-saved on unload)");
});

test('Sign out clears name, email and recent chats, and returns to login', async () => {
  const env = boot({ storage: USER });
  await env.ask('something');
  env.window.newChat();
  let reloaded = false;
  try { env.window.location.reload = () => { reloaded = true; }; } catch {}
  env.window.signOut();
  assert.equal(env.ls('vs_user'), null);
  assert.equal(env.ls('vs_email'), null);
  assert.equal(env.ls('vs_chats'), null);
  const next = env.reload();
  await next.settle();
  assert.ok(next.visible('#login'));
});

test('Sign out does not let the beforeunload handler re-save the open chat', async () => {
  const env = boot({ storage: USER });
  await env.ask('secret');            // chat open, not yet saved
  env.window.signOut();               // clears vs_chats then reloads → beforeunload fires
  env.window.dispatchEvent(new env.window.Event('beforeunload'));
  assert.equal(env.ls('vs_chats'), null, "signed-out user's chat was written back to localStorage on unload");
});

test('Settings "Clear all" recent chats empties the list', async () => {
  const env = boot({ storage: { ...USER, vs_chats: JSON.stringify([{ title: 'a', time: 1, html: '<p>x</p>', history: [] }]) } });
  env.window.openSettings();
  const clear = env.$$('#modal button').find(b => b.textContent === 'Clear all');
  env.click(clear);
  assert.equal(env.ls('vs_chats'), null);
  env.window.openRecent();
  assert.match(env.$('#modal').textContent, /No past chats/);
});

/* ------------------------------ account chip ------------------------------ */

test('account chip menu: opens on click, shows email + Settings / My material / Sign out', () => {
  const env = boot({ storage: USER });
  const m = env.$('#acctMenu');
  assert.ok(!m.classList.contains('show'));
  env.click('#acctChip');
  assert.ok(m.classList.contains('show'));
  assert.equal(m.querySelector('.who').textContent, 'asha@x.com');
  assert.deepEqual([...m.querySelectorAll('button')].map(b => b.textContent), ['Settings', 'My material', 'Sign out']);
  env.click('#acctChip');
  assert.ok(!m.classList.contains('show'), 'second click toggles closed');
});

test('account chip menu: outside click closes; clicks inside the menu do not', () => {
  const env = boot({ storage: USER });
  const m = env.$('#acctMenu');
  env.click('#acctChip');
  env.click(m.querySelector('.who'));
  assert.ok(m.classList.contains('show'));
  env.click('#stage');
  assert.ok(!m.classList.contains('show'));
});

test('account chip menu items open Settings / My material and close the menu', async () => {
  const env = boot({ storage: USER });
  const m = env.$('#acctMenu');
  env.click('#acctChip');
  env.click(m.querySelectorAll('button')[0]);
  assert.equal(env.$('#modal h2').textContent, 'Settings');
  assert.ok(!m.classList.contains('show'));
  env.click('#acctChip');
  env.click(m.querySelectorAll('button')[1]); await env.settle();
  assert.equal(env.$('#modal h2').textContent, 'My material');
  assert.ok(!m.classList.contains('show'));
});

test('account chip is a keyboard-operable control (role=button / aria-expanded)', () => {
  const env = boot({ storage: USER });
  const chip = env.$('#acctChip');
  assert.ok(chip.tagName === 'BUTTON' || chip.getAttribute('role') === 'button', 'account chip is a bare div');
  assert.ok(chip.hasAttribute('aria-expanded') || chip.hasAttribute('aria-haspopup'));
});

/* ------------------------------ settings ------------------------------ */

test('Settings shows profile name + email', () => {
  const env = boot({ storage: USER });
  env.window.openSettings();
  const v = env.$('#modal .trow .v').textContent;
  assert.match(v, /Asha Kumar/);
  assert.match(v, /asha@x\.com/);
});

test('Settings default mode persists and applies on next load', async () => {
  const env = boot({ storage: USER });
  env.window.openSettings();
  env.click(env.$$('#modal button').find(b => b.textContent === 'Teach'));
  assert.equal(env.ls('vs_mode'), 'teach');
  assert.equal(env.window.eval('mode'), 'teach');
  assert.ok(env.$$('#modal button').find(b => b.textContent === 'Teach').classList.contains('on'));
  const next = env.reload();
  assert.equal(next.window.eval('mode'), 'teach');
  assert.ok(next.$('.chip[data-mode=teach]').classList.contains('on'));
  assert.ok(!next.$('.chip[data-mode=quick]').classList.contains('on'));
});

test('Settings web-fallback default persists, syncs the composer toggle, applies on load and is sent', async () => {
  const env = boot({ storage: USER });
  env.window.openSettings();
  env.click('#webDefBtn');
  assert.equal(env.ls('vs_web'), '1');
  assert.ok(env.$('#webToggle').classList.contains('on'));
  assert.equal(env.$('#webDefBtn').textContent, 'On');
  const next = env.reload();
  assert.ok(next.$('#webToggle').classList.contains('on'));
  await next.ask('q');
  assert.equal(next.callsTo('/api/answer')[0].body.web, true);
});

test('Composer chip clicks are per-session and do NOT overwrite the Settings default mode', async () => {
  const env = boot({ storage: { ...USER, vs_mode: 'quick' } });
  env.click('.chip[data-mode=exam]');
  assert.equal(env.ls('vs_mode'), 'quick', 'clicking a composer mode chip silently changed the saved default mode');
});

test('Change password: both fields required; posts email/old/new; shows server error', async () => {
  const env = boot({ storage: USER, routes: { '/api/account/password': { ok: false, error: 'Current password is wrong.' } } });
  env.window.openSettings();
  await env.window.doChangePassword();
  assert.equal(env.$('#pwMsg').textContent, 'Fill both fields.');
  assert.equal(env.callsTo('/api/account/password').length, 0);
  env.$('#pwOld').value = 'old'; env.$('#pwNew').value = 'newpass';
  await env.window.doChangePassword();
  assert.deepEqual(env.callsTo('/api/account/password')[0].body, { old_password: 'old', new_password: 'newpass' });
  assert.equal(env.$('#pwMsg').textContent, 'Current password is wrong.');
});

test('Change password success shows confirmation', async () => {
  const env = boot({ storage: USER });
  env.window.openSettings();
  env.$('#pwOld').value = 'old'; env.$('#pwNew').value = 'newpass';
  await env.window.doChangePassword();
  assert.match(env.$('#pwMsg').textContent, /Password updated/);
});

test('Delete account: requires password + confirm; posts email/password; error shown; success signs out', async () => {
  const env = boot({ storage: USER, routes: { '/api/account/delete': (b) => b.password === 'right' ? { ok: true } : { __resp: true, status: 403, json: { ok: false, error: 'Wrong password.' } } } });
  env.window.openSettings();
  await env.window.doDeleteAccount();
  assert.match(env.$('#delMsg').textContent, /Enter your password/);
  env.$('#delPw').value = 'bad';
  await env.window.doDeleteAccount();
  assert.equal(env.confirms.length, 1);
  assert.deepEqual(env.callsTo('/api/account/delete')[0].body, { password: 'bad' });
  assert.equal(env.$('#delMsg').textContent, 'Wrong password.');
  env.$('#delPw').value = 'right';
  await env.window.doDeleteAccount();
  assert.equal(env.ls('vs_email'), null);
  assert.equal(env.ls('vs_user'), null);
});

/* ------------------------------ starters ------------------------------ */

test('starter chips: Explain fills input; Practice set → exam mode; Teach → teach mode; Grade → prompts', async () => {
  const env = boot({ storage: USER, prompts: [null] });
  const s = env.$$('#starters .starter');
  assert.equal(s.length, 4);
  env.click(s[0]);
  assert.equal(env.$('#input').value, 'Explain a concept from my notes');
  env.click(s[1]);
  assert.equal(env.window.eval('mode'), 'exam');
  assert.equal(env.$('#input').value, '');
  env.click(s[3]);
  assert.equal(env.window.eval('mode'), 'teach');
  env.click(s[2]); await env.settle();
  assert.equal(env.calls.length, 0);
});

test('mode chips set the mode and exactly one chip is on', () => {
  const env = boot({ storage: USER });
  for (const m of ['teach', 'exam', 'quick']) {
    env.click(`.chip[data-mode=${m}]`);
    assert.equal(env.window.eval('mode'), m);
    assert.deepEqual(env.$$('.chip.on').map(c => c.dataset.mode), [m]);
  }
});

/* ------------------------------ static checks ------------------------------ */

test('static: no duplicate element ids (incl. after modals open)', async () => {
  const env = boot({ storage: USER });
  const dupes = () => { const seen = {}; for (const el of env.$$('[id]')) seen[el.id] = (seen[el.id] || 0) + 1; return Object.keys(seen).filter(k => seen[k] > 1); };
  assert.deepEqual(dupes(), []);
  env.window.openSettings(); env.window.openMaterial(); await env.settle();
  assert.deepEqual(dupes(), []);
});

test('static: every function called from an inline on* handler exists', async () => {
  const env = boot({ storage: USER });
  env.window.openSettings();
  const html = HTML + env.$('#modal').innerHTML + env.$('#acctMenu').innerHTML;
  const names = new Set();
  for (const m of html.matchAll(/\son\w+="([^"]*)"/g))
    for (const f of m[1].matchAll(/(?:^|[;{(\s!&|])([a-zA-Z_$][\w$]*)\s*\(/g)) names.add(f[1]);
  const builtin = new Set(['if', 'getElementById', 'click', 'focus', 'remove', 'removeItem', 'toast', 'setItem', 'stopPropagation', 'preventDefault']);
  const missing = [...names].filter(n => !builtin.has(n) && typeof env.window[n] !== 'function');
  assert.deepEqual(missing, []);
});

test('static: no console/script errors on load (logged out and logged in)', async () => {
  const a = boot(); const b = boot({ storage: USER });
  await a.settle(); await b.settle();
  assert.deepEqual([...a.errors, ...b.errors], []);
});

test('static (P3): key controls have accessible names (send button, textarea, auth inputs, delete ✕)', async () => {
  const env = boot({ storage: USER, routes: { '/api/material': { docs: [{ name: 'a.txt', size: 1, indexed: true }] } } });
  const named = el => !!(el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('aria-labelledby') ||
    (el.id && env.$(`label[for="${el.id}"]`)) || el.closest('label') || (el.tagName === 'BUTTON' && el.textContent.trim().length > 1));
  const missing = [];
  for (const sel of ['.send', '#input', '#anName', '#anEmail', '#anPass']) if (!named(env.$(sel))) missing.push(sel);
  env.window.openMaterial(); await env.settle();
  if (!named(env.$('.docdel'))) missing.push('.docdel');
  assert.deepEqual(missing, [], 'controls without an accessible name');
});

test('static: email input is type=email and has autocomplete hints', () => {
  const env = boot();
  assert.equal(env.$('#anEmail').getAttribute('type'), 'email');
  assert.ok(env.$('#anPass').getAttribute('autocomplete'));
});

test('static: phone width — a mobile media query collapses/hides the 272px sidebar', () => {
  const css = [...HTML.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n');
  const mq = [...css.matchAll(/@media[^{]*\((?:max-width)\s*:\s*(\d+)px\)\s*\{([\s\S]*?\})\s*\}/g)];
  const phone = mq.filter(m => +m[1] >= 480 && +m[1] <= 900);
  assert.ok(phone.length > 0, 'no @media (max-width: …) rule at all — 272px fixed sidebar + 388px login card on a 360px phone (SPEC: works on a phone-width screen)');
  assert.ok(phone.some(m => /\.sidebar/.test(m[2])), 'mobile media query does not touch .sidebar');
});

test('static: login card fits a 360px phone (no fixed width > viewport)', () => {
  const css = [...HTML.matchAll(/<style>([\s\S]*?)<\/style>/g)].map(m => m[1]).join('\n');
  const card = css.match(/\.card\{([^}]*)\}/)[1];
  const fixed = /(?:^|;)width:(\d+)px/.exec(card);
  assert.ok(!fixed || +fixed[1] <= 340 || /max-width/.test(card), `.card has width:${fixed && fixed[1]}px with no max-width`);
});
