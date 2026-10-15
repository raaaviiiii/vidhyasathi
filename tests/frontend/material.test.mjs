// My material modal: list, upload, delete, file-name injection.
import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { boot, closeAll, injected, USER, AUTH, authOf } from './harness.mjs';

afterEach(closeAll);

test('material list shows only the returned docs, indexed / not indexed flags, sizes', async () => {
  const env = boot({ storage: USER, routes: { '/api/material': { docs: [{ name: 'Number Theory.pdf', size: 20480, indexed: true }, { name: 'scan.png', size: 1024, indexed: false }] } } });
  env.window.openMaterial(); await env.settle();
  const docs = env.$$('#doclist .doc');
  assert.equal(docs.length, 2);
  assert.equal(docs[0].querySelector('.nm').textContent, 'Number Theory.pdf');
  assert.match(docs[1].querySelector('.nm').textContent, /not indexed/);
  assert.equal(docs[0].querySelector('.sz').textContent, '20 KB');
  assert.ok(docs[0].querySelector('button.docdel'));
});

test('material list: empty state message', async () => {
  const env = boot({ storage: USER });
  env.window.openMaterial(); await env.settle();
  assert.match(env.$('#doclist').textContent, /Nothing yet/);
});

test('material list: server failure replaces the "loading…" placeholder with an error', async () => {
  const env = boot({ storage: USER, routes: { '/api/material': () => { throw new TypeError('Failed to fetch'); } } });
  env.window.openMaterial(); await env.settle();
  assert.doesNotMatch(env.$('#doclist').textContent, /loading/, 'list stuck on "loading…" forever when the request fails');
});

test('upload sends file + user in FormData and reports indexed + chunk count', async () => {
  const env = boot({ storage: USER, routes: { '/api/upload': { ok: true, indexed: true, name: 'n.txt', chunks: 7 } } });
  env.window.openMaterial(); await env.settle();
  const f = new env.window.File(['hello'], 'n.txt', { type: 'text/plain' });
  await env.window.doUpload(f); await env.settle();
  const c = env.callsTo('/api/upload')[0];
  assert.equal(c.method, 'POST');
  const fd = c.init.body;
  assert.ok(fd instanceof env.window.FormData);
  assert.equal(authOf(c), AUTH, 'upload sent without the session token');
  assert.equal(fd.get('file').name, 'n.txt');
  assert.match(env.$('#toast').textContent, /Indexed n\.txt ✓ \(7 chunks\)/);
  assert.ok(env.callsTo('/api/material').length >= 2, 'list refreshed after upload');
});

test('upload not indexed: student is told plainly with the reason', async () => {
  const env = boot({ storage: USER, routes: { '/api/upload': { ok: true, indexed: false, name: 'e.pdf', error: 'no extractable text' } } });
  env.window.openMaterial(); await env.settle();
  await env.window.doUpload(new env.window.File(['x'], 'e.pdf')); await env.settle();
  assert.match(env.$('#toast').textContent, /e\.pdf.*no extractable text/);
});

test('drag-and-drop on the drop zone uploads the file', async () => {
  const env = boot({ storage: USER });
  env.window.openMaterial(); await env.settle();
  const drop = env.$('#drop');
  const ev = new env.window.Event('drop', { bubbles: true, cancelable: true });
  ev.dataTransfer = { files: [new env.window.File(['x'], 'd.txt')] };
  drop.dispatchEvent(ev); await env.settle();
  assert.equal(env.callsTo('/api/upload').length, 1);
});

test('delete (✕) asks to confirm, posts name + user, toasts and refreshes', async () => {
  const env = boot({ storage: USER, routes: { '/api/material': { docs: [{ name: 'a.txt', size: 10, indexed: true }] } } });
  env.window.openMaterial(); await env.settle();
  env.click(env.$('#doclist .docdel')); await env.settle();
  assert.equal(env.confirms.length, 1);
  const c = env.callsTo('/api/material/delete')[0];
  assert.deepEqual({ ...c.body }, { name: 'a.txt' });
  assert.equal(authOf(c), AUTH);
  assert.match(env.$('#toast').textContent, /Removed a\.txt/);
});

test('delete cancelled at confirm sends nothing', async () => {
  const env = boot({ storage: USER, confirm: false });
  await env.window.delMaterial('a.txt');
  assert.equal(env.callsTo('/api/material/delete').length, 0);
});

for (const name of [
  "it's notes.pdf",
  'x");alert(1);//.txt',
  "x');window.__pwned=1;//.txt",
  "x\\');window.__pwned=1;//.txt",
  'a&amp;b.txt',
  'back`tick`.txt',
  '<script>alert(1)</script>.txt',
]) {
  test(`file name ${JSON.stringify(name)}: ✕ deletes exactly that file, no injected code`, async () => {
    const env = boot({ storage: USER, routes: { '/api/material': { docs: [{ name, size: 10, indexed: true }] } } });
    env.window.__pwned = 0;
    env.window.openMaterial(); await env.settle();
    const nm = env.$('#doclist .doc .nm');
    assert.equal(nm.firstChild.textContent, name, 'file name shown verbatim');
    assert.equal(nm.querySelectorAll('script,img').length, 0);
    let threw = null;
    try { env.click(env.$('#doclist .docdel')); } catch (e) { threw = e; }
    await env.settle();
    assert.equal(env.window.__pwned, 0, 'file name executed as JS from the onclick attribute');
    assert.equal(threw, null);
    const c = env.callsTo('/api/material/delete');
    assert.equal(c.length, 1, '✕ did nothing (broken onclick attribute)');
    assert.equal(c[0].body.name, name, 'delete request named the wrong file');
  });
}

test('file name with a double quote cannot add attributes/handlers to the ✕ button', async () => {
  const name = 'x" onmouseover="window.__pwned=1" data-x=".txt';
  const env = boot({ storage: USER, routes: { '/api/material': { docs: [{ name, size: 10, indexed: true }] } } });
  env.window.openMaterial(); await env.settle();
  const btn = env.$('#doclist .docdel');
  const attrs = [...btn.attributes].map(a => a.name);
  const allowed = new Set(['class', 'onclick', 'title', 'type', 'aria-label']);
  assert.deepEqual(attrs.filter(a => !allowed.has(a)), [], `file name injected attributes into the button: ${attrs}`);
  assert.ok(!btn.hasAttribute('onmouseover') && !btn.hasAttribute('data-x'));
});

test('upload rejected by the server (HTTP 500/413, non-JSON) is not reported as "is app.py running?"', async () => {
  const env = boot({ storage: USER, routes: { '/api/upload': { __resp: true, status: 413, json: 'Request Entity Too Large' } } });
  env.window.openMaterial(); await env.settle();
  await env.window.doUpload(new env.window.File(['x'], 'big.pdf')); await env.settle();
  assert.doesNotMatch(env.$('#toast').textContent, /is app\.py running/i, 'server answered; message is misleading');
});

test('delete of an already-removed file (404 "Not found.") is not reported as "is app.py running?"', async () => {
  const env = boot({ storage: USER, routes: { '/api/material/delete': { __resp: true, status: 404, json: { ok: false, error: 'Not found.' } } } });
  await env.window.delMaterial('gone.txt'); await env.settle();
  assert.doesNotMatch(env.$('#toast').textContent, /is app\.py running/i);
});
