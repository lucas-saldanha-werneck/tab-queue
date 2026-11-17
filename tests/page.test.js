// Page logic tests: run the page script against a tiny fake DOM (node --test tests/)
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function boot(items, extra) {
  const html = fs.readFileSync(path.join(__dirname, '..', 'server', 'page.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1].replace('__TQ_CONFIG__', '{}');
  const els = {}, timers = [], calls = [];
  const el = (id) => els[id] || (els[id] = { id, style: {}, hidden: false, textContent: '', innerHTML: '', value: '', listeners: {},
    addEventListener(t, f) { this.listeners[t] = f; }, classList: { add() {}, remove() {} } });
  const served = items.slice();
  const ctx = {
    document: { getElementById: el, title: '', hidden: false, addEventListener() {} },
    location: { search: '', pathname: '/' }, history: { replaceState() {} },
    URLSearchParams, Set, Math, JSON, Date, Promise, console,
    setInterval() {}, clearTimeout() {},
    setTimeout(f, ms) { timers.push({ f, ms }); return timers.length; },
    fetch: async (u, o) => {
      calls.push(u);
      const body = o && o.body ? JSON.parse(o.body) : {};
      let j = {};
      if (u === 'api/items') j = Object.assign({ items: served.slice(), pending: [], counts: {}, undo_seconds: 10, mac_seen_ago: 1 }, extra);
      else if (u === 'api/done') j = { ok: true };
      else if (u === 'api/undo') j = { result: 'kept' };
      return { ok: true, json: async () => j };
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(script, ctx);
  const clickDone = async (url) => {
    const li = { dataset: { url }, classList: { add() {}, remove() {} } };
    const btn = { closest: (s) => (s === 'li' ? li : btn) };
    await els.list.listeners.click({ target: { closest: (s) => (s === '.done' ? btn : null) } });
  };
  const flush = () => new Promise((r) => setImmediate(r));
  return { els, timers, calls, clickDone, flush, served };
}
const item = (u) => ({ url: u, open_url: u, title: 't ' + u, kind: 'page', site: 'x.example' });

test('Done on B after Done on A does not bring A back', async () => {
  const p = boot([item('https://a.example/'), item('https://b.example/')]);
  await p.flush();
  assert.match(p.els.list.innerHTML, /a\.example/);
  await p.clickDone('https://a.example/');
  p.timers.splice(0).forEach((t) => t.f()); // the 250 ms render
  assert.doesNotMatch(p.els.list.innerHTML, /a\.example/);
  await p.clickDone('https://b.example/');
  p.timers.splice(0).forEach((t) => t.f());
  assert.doesNotMatch(p.els.list.innerHTML, /a\.example/);
  assert.doesNotMatch(p.els.list.innerHTML, /b\.example/);
  assert.deepStrictEqual(p.calls.filter((c) => c === 'api/done').length, 2);
});

test('the Undo toast uses the time the server gives, never more than its window', async () => {
  const p = boot([item('https://a.example/')], { undo_seconds: 1, undo_toast_ms: 500 });
  await p.flush();
  await p.clickDone('https://a.example/');
  const toastTimer = p.timers.filter((t) => t.ms !== 250).pop();
  assert.ok(toastTimer.ms <= 1000, 'toast ' + toastTimer.ms + ' ms');
});
