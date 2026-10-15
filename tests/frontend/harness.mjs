// Shared JSDOM harness for the Vidhyasathi single-file UI (index.html).
// - Loads index.html with runScripts:"dangerously" at http://localhost:8000/
// - Injects REAL marked.js (from /tmp/node_modules via NODE_PATH) unless {marked:false}
//   (simulates the CDN failing to load offline), and a no-op renderMathInElement.
// - Mocks window.fetch: records every call and answers from a route table.
// - Stubs confirm/prompt/alert; captures console errors + jsdom errors.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { JSDOM, VirtualConsole } = require('jsdom');

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(HERE, '..', '..');
export const HTML = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const MARKED_SRC = fs.readFileSync(
  path.join(path.dirname(require.resolve('marked')), 'marked.umd.js'), 'utf8');

// A signed-in session: the server identifies the student from the bearer token,
// never from a client-supplied email.
export const USER = { vs_user: 'Asha Kumar', vs_email: 'asha@x.com', vs_token: 'tok-asha' };
export const AUTH = 'Bearer tok-asha';
/** The Authorization header a recorded fetch call carried. */
export function authOf(call) { const hd = (call && call.init && call.init.headers) || {}; return hd.Authorization || hd.authorization; }

/** Build an HTTP-ish response object for route handlers. */
export function resp(status, json) { return { __resp: true, status, json }; }
/** Make a route handler that never resolves until you call .resolve(json). */
export function deferred() {
  let res; const p = new Promise(r => { res = r; });
  const h = () => p; h.resolve = (json, status = 200) => res(resp(status, json)); return h;
}

export const DEFAULT_ROUTES = {
  '/api/answer': { answer: 'Fine [Notes · p1]', decision: 'answer', top_score: 0.71, caveat: '',
    offer_help: false, web_used: false, web_sources: [], sources: [{ source: 'Notes', loc: 'p1', score: 0.71, ocr: false }] },
  '/api/exam': { grounded: true, topic: 't', raw: '', items: [{ q: 'Q?', a: 'A [Notes · p1]' }] },
  '/api/grade': { grounded: true, feedback: '**Correct:** ok [Notes · p1]' },
  '/api/material': { docs: [] },
  '/api/material/delete': { ok: true },
  '/api/upload': { ok: true, indexed: true, name: 'f.txt', chunks: 3 },
  '/api/signup': { ok: true, name: 'Asha Kumar', email: 'asha@x.com', token: 'tok-asha' },
  '/api/login': { ok: true, name: 'Asha Kumar', email: 'asha@x.com', token: 'tok-asha' },
  '/api/logout': { ok: true },
  '/api/account/password': { ok: true },
  '/api/account/delete': { ok: true },
  '/api/doubts': { ok: true, mine: [], inbox: [], role: 'student', is_helper: false, is_admin: false },
};

const open = new Set();
export function closeAll() { for (const w of open) { try { w.close(); } catch {} } open.clear(); }

export function boot({ storage = {}, routes = {}, marked = true, confirm = true, prompts = [] } = {}) {
  const calls = [];
  const errors = [];
  const confirms = [];
  const vc = new VirtualConsole();
  vc.on('error', (...a) => errors.push(a.map(String).join(' ')));
  vc.on('jsdomError', e => { if (!/Not implemented: navigation/.test(String(e && e.message))) errors.push('jsdomError: ' + (e && (e.stack || e.message))); });
  const allRoutes = { ...DEFAULT_ROUTES, ...routes };
  const promptQueue = [...prompts];

  const dom = new JSDOM(HTML, {
    url: 'http://localhost:8000/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(window) {
      for (const [k, v] of Object.entries(storage)) window.localStorage.setItem(k, v);
      if (marked) window.eval(MARKED_SRC);
      window.renderMathInElement = () => {};
      window.confirm = (m) => { confirms.push(m); return typeof confirm === 'function' ? confirm(m) : confirm; };
      window.prompt = () => (promptQueue.length ? promptQueue.shift() : null);
      window.alert = () => {};
      window.fetch = async (url, init = {}) => {
        const u = new URL(String(url), 'http://localhost:8000/');
        let body = init.body;
        if (typeof body === 'string') { try { body = JSON.parse(body); } catch {} }
        const call = { url: String(url), path: u.pathname, query: Object.fromEntries(u.searchParams), method: init.method || 'GET', body, init };
        calls.push(call);
        let h = allRoutes[u.pathname];
        if (h === undefined) h = resp(404, { detail: 'Not Found' });
        let out = typeof h === 'function' ? await h(body, call) : h;
        if (!(out && out.__resp)) out = resp(200, out);
        return {
          ok: out.status >= 200 && out.status < 300,
          status: out.status,
          json: async () => { if (typeof out.json === 'string') throw new SyntaxError('Unexpected token'); return JSON.parse(JSON.stringify(out.json)); },
          text: async () => (typeof out.json === 'string' ? out.json : JSON.stringify(out.json)),
        };
      };
    },
  });
  const window = dom.window;
  open.add(window);
  const document = window.document;
  const env = {
    dom, window, document, calls, errors, confirms,
    $: s => document.querySelector(s),
    $$: s => [...document.querySelectorAll(s)],
    ev: code => window.eval(code),
    ls: k => window.localStorage.getItem(k),
    storageSnapshot() { const o = {}; for (let i = 0; i < window.localStorage.length; i++) { const k = window.localStorage.key(i); o[k] = window.localStorage.getItem(k); } return o; },
    callsTo: p => calls.filter(c => c.path === p),
    visible(el) { if (typeof el === 'string') el = document.querySelector(el); return !!el && !el.closest('.hidden'); },
    async settle(n = 8) { for (let i = 0; i < n; i++) await new Promise(r => setImmediate(r)); },
    async ask(text) { const i = document.getElementById('input'); i.value = text; window.send(); await env.settle(); },
    navLinks: () => [...document.querySelectorAll('.nav a')],
    activeNav() { return env.navLinks().findIndex(a => a.classList.contains('on')); },
    lastBot: () => { const b = document.querySelectorAll('#transcript .msg.bot'); return b[b.length - 1]; },
    click(el) { if (typeof el === 'string') el = document.querySelector(el); el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })); },
    /** Reload: new page over a copy of this page's localStorage. */
    reload(extra = {}) { return boot({ storage: env.storageSnapshot(), ...extra }); },
  };
  return env;
}

/** Any element under root carrying executable markup (on* handler, script, javascript: URL). */
export function injected(root) {
  const bad = [];
  for (const el of root.querySelectorAll('*')) {
    const tag = el.tagName.toLowerCase();
    if (['script', 'iframe', 'object', 'embed'].includes(tag)) bad.push('<' + tag + '>');
    for (const a of el.attributes) {
      if (/^on/i.test(a.name)) bad.push(`${tag}[${a.name}]`);
      if (/^(href|src|action|formaction)$/i.test(a.name) && /^\s*javascript:/i.test(a.value)) bad.push(`${tag}[${a.name}=javascript:]`);
    }
  }
  return bad;
}
