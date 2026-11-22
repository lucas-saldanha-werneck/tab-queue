// Robot logic tests (Node's built-in runner): node --test tests/
const test = require('node:test');
const assert = require('node:assert');
const C = require('../robot/core.js');

test('realTab resolves suspended tabs', () => {
  const u = 'chrome-extension://fiabciakcmgepblmdkmemdbbkilneeeh/park.html?tabId=1&sessionId=2&title=Hello%20there&url=https%3A%2F%2Fexample.org%2Fa%3Fb%3D1&icon=x';
  assert.deepStrictEqual(C.realTab(u, 'ignored'), { url: 'https://example.org/a?b=1', title: 'Hello there', suspended: true });
  assert.deepStrictEqual(C.realTab('https://x.example/', 'X'), { url: 'https://x.example/', title: 'X', suspended: false });
  // a park page without url stays as it is
  assert.strictEqual(C.realTab('chrome-extension://abc/park.html?title=a', 't').url, 'chrome-extension://abc/park.html?title=a');
});

test('parseEtime', () => {
  assert.strictEqual(C.parseEtime('05:03'), 303);
  assert.strictEqual(C.parseEtime('01:00:00'), 3600);
  assert.strictEqual(C.parseEtime('2-00:00:10'), 172810);
  assert.strictEqual(C.parseEtime(''), null);
});

test('pickWindow: config id, last id, same tabs, largest; never incognito', () => {
  const wins = [
    { id: 1, mode: 'normal', urls: ['a', 'b'] },
    { id: 2, mode: 'normal', urls: ['c', 'd', 'e', 'f'] },
    { id: 3, mode: 'incognito', urls: ['1', '2', '3', '4', '5', '6'] },
  ];
  assert.deepStrictEqual(C.pickWindow(wins, 1, {}), { id: 1, reason: 'config' });
  assert.deepStrictEqual(C.pickWindow(wins, '1', {}), { id: 1, reason: 'config' });
  assert.deepStrictEqual(C.pickWindow(wins, 'largest', {}), { id: 2, reason: 'largest' });
  assert.deepStrictEqual(C.pickWindow(wins, 'largest', { window_id: 1 }), { id: 1, reason: 'last' });
  // configured id gone (Chrome restarted): the window with the tabs we saw last time
  assert.deepStrictEqual(C.pickWindow(wins, 999, { window_id: 998, urls: ['a', 'b', 'zz'] }), { id: 1, reason: 'same-tabs' });
  assert.deepStrictEqual(C.pickWindow(wins, 3, {}), { id: 2, reason: 'largest' });
  assert.strictEqual(C.pickWindow([{ id: 3, mode: 'incognito', urls: [] }], 'largest', {}), null);
});

test('planClose checks id AND address', () => {
  const tabs = [{ id: 1, url: 'https://a/' }, { id: 2, url: 'https://b/' }, { id: 3, url: 'https://a/' }];
  assert.deepStrictEqual(C.planClose({ url: 'https://a/', tab_ids: [1, 3] }, tabs), { close: [1, 3], result: 'closed' });
  assert.deepStrictEqual(C.planClose({ url: 'https://a/', tab_ids: ['1'] }, tabs), { close: [1], result: 'closed' });
  // the tab now shows another page: do not close it
  assert.deepStrictEqual(C.planClose({ url: 'https://a/', tab_ids: [2] }, tabs), { close: [], result: 'changed' });
  assert.deepStrictEqual(C.planClose({ url: 'https://a/', tab_ids: [42] }, tabs), { close: [], result: 'gone' });
});

test('planRoute moves only new matching tabs, nothing on the first run', () => {
  const pats = ['^http://127\\.0\\.0\\.1:4387/session/'];
  const others = [
    { id: 10, url: 'http://127.0.0.1:4387/session/abc', window_id: 2 },
    { id: 11, url: 'http://127.0.0.1:4387/session/def', window_id: 2 },
    { id: 12, url: 'https://example.org/', window_id: 2 },
  ];
  assert.deepStrictEqual(C.planRoute(others, null, pats), []);
  assert.deepStrictEqual(C.planRoute(others, { 10: 1 }, pats).map((t) => t.id), [11]);
  assert.deepStrictEqual(C.planRoute(others, {}, ['[bad']), []);
});

test('planRoute never routes a suspended tab', () => {
  const pats = ['^http://127\\.0\\.0\\.1:4387/session/'];
  const others = [
    { id: 11, url: 'http://127.0.0.1:4387/session/a', window_id: 2, suspended: true },
    { id: 12, url: 'http://127.0.0.1:4387/session/b', window_id: 2, suspended: false },
  ];
  assert.deepStrictEqual(C.planRoute(others, { 1: 1 }, pats).map((t) => t.id), [12]);
});

test('planRoute moves nothing from a window with a burst of new tabs (restore)', () => {
  const pats = ['^http://127\\.0\\.0\\.1:4387/session/'];
  const restored = [];
  for (let i = 0; i < C.RESTORE_BURST + 1; i++) restored.push({ id: 100 + i, url: 'http://127.0.0.1:4387/session/r' + i, window_id: 5 });
  const other = { id: 200, url: 'http://127.0.0.1:4387/session/n', window_id: 6 };
  const seen = { 1: 1 };
  assert.deepStrictEqual(C.restoredWindows(restored.concat(other), seen), [{ window_id: 5, count: C.RESTORE_BURST + 1 }]);
  // the burst window is skipped, a single new tab in another window still routes
  assert.deepStrictEqual(C.planRoute(restored.concat(other), seen, pats).map((t) => t.id), [200]);
  // exactly RESTORE_BURST new tabs is still normal
  assert.deepStrictEqual(C.planRoute(restored.slice(0, C.RESTORE_BURST), seen, pats).length, C.RESTORE_BURST);
  assert.deepStrictEqual(C.restoredWindows(restored, null), []);
});

test('newTab finds the tab that appeared', () => {
  const after = [{ id: 1, url: 'https://a/' }, { id: 7, url: 'https://new.example/x' }];
  assert.strictEqual(C.newTab([1], after, 'https://new.example/x').id, 7);
  assert.strictEqual(C.newTab([1], after, 'http://new.example/x/').id, 7); // redirect to https, trailing slash
  assert.strictEqual(C.newTab([1, 7], after, 'https://new.example/x'), null);
  assert.strictEqual(C.newTab([1], after.concat([{ id: 8, url: 'https://other/' }]), 'https://zzz/'), null);
});

test('stamp has seconds', () => {
  assert.match(C.stamp(new Date(2026, 10, 17, 9, 5, 7)), /^2026-11-17 09:05:07$/);
});

test('strayWindows: only a window made by our own call, with one tab: our link or about:blank', () => {
  const mine = 'https://new.example/x';
  const w = (id, ...urls) => ({ id, tabs: urls.map((u, k) => ({ id: id * 10 + k, url: u })) });
  assert.deepStrictEqual(C.strayWindows([w(2, 'about:blank'), w(3, mine)], mine).map((x) => x.id), [2, 3]);
  // someone else's automation opened these: never touched
  assert.deepStrictEqual(C.strayWindows([w(4, 'https://x.com/')], mine), []);
  assert.deepStrictEqual(C.strayWindows([w(5, 'about:blank', 'https://jobs.lever.co/a')], mine), []);
  assert.deepStrictEqual(C.strayWindows([w(6, 'about:blank', 'about:blank')], mine), []);
  assert.deepStrictEqual(C.strayWindows([w(7)], mine), []);
});

test('param and realTab: Tab Suspender and unencoded uri= at the end', () => {
  const real = 'https://example.org/a?x=1&y=2#frag';
  // encoded url= (Tab Suspender): stops at the next &
  const enc = 'chrome-extension://x/park.html?title=T%20t&url=' + encodeURIComponent(real) + '&icon=z';
  assert.deepStrictEqual(C.realTab(enc, 'z'), { url: real, title: 'T t', suspended: true });
  // url= last, encoded, real address contains an encoded url= too
  const inner = 'https://example.org/go?url=' + encodeURIComponent('https://other.example/') + '&k=v';
  const enc2 = 'chrome-extension://x/park.html?title=A&url=' + encodeURIComponent(inner);
  assert.strictEqual(C.realTab(enc2, '').url, inner);
  // uri= not encoded, at the end: everything to the end of the string is the address (& and # included)
  const raw = 'chrome-extension://x/suspended.html?title=T&uri=' + real;
  assert.strictEqual(C.realTab(raw, '').url, real);
  // ...even when the real address has its own url= (which must not win over the suspender's uri=)
  const tricky = 'https://example.org/p?a=1&url=https://decoy.example/&b=2';
  assert.strictEqual(C.realTab('chrome-extension://x/park.html?uri=' + tricky, '').url, tricky);
  assert.strictEqual(C.realTab('chrome-extension://x/park.html?uri=' + tricky, 'zz').title, 'zz');
  // unencoded url= at the end too
  assert.strictEqual(C.realTab('chrome-extension://x/park.html?tabId=3&url=' + real, '').url, real);
  // param itself: first match with a real boundary, to & or #
  assert.strictEqual(C.param('a?b=1&c=two%20words#x', 'c'), 'two words');
  assert.strictEqual(C.param('a?xb=1', 'b'), '');
});

test('restarted: new Chrome run means ids mean nothing, so route nothing and just record', () => {
  const st = { window_id: 5, other_seen: { 1: 1 }, chrome_started: 1000 };
  assert.strictEqual(C.restarted(st, { id: 5, reason: 'last' }, 1010), false);
  assert.strictEqual(C.restarted(st, { id: 5, reason: 'last' }, 1000 + 3600), true); // started later: a restart
  assert.strictEqual(C.restarted(st, { id: 9, reason: 'same-tabs' }, 1005), true);
  assert.strictEqual(C.restarted(st, { id: 9, reason: 'largest' }, 1005), true);
  assert.strictEqual(C.restarted({}, { id: 9, reason: 'largest' }, 1005), true); // empty state
  assert.strictEqual(C.restarted({ window_id: 5, other_seen: {} }, { id: 5, reason: 'last' }, null), false); // age unknown: trust ids
  assert.strictEqual(C.restarted({ window_id: 5, other_seen: { 1: 1 } }, { id: 5, reason: 'config' }, 1005), false);
});

test('pickAdopt: only a tab whose id did not exist before the open', () => {
  const wins = [
    { id: 1, tabs: [{ id: 11, url: 'https://a/' }] },
    { id: 2, tabs: [{ id: 21, url: 'https://new.example/x' }, { id: 22, url: 'https://new.example/x' }] },
  ];
  assert.strictEqual(C.pickAdopt(wins, 1, 'https://new.example/x', [11, 21, 22]), null); // all were already there
  assert.deepStrictEqual(C.pickAdopt(wins, 1, 'https://new.example/x', [11, 21]), { window_id: 2, tab_id: 22 });
  assert.strictEqual(C.pickAdopt(wins, 2, 'https://new.example/x', [11]), null); // the queue window itself is skipped
});
