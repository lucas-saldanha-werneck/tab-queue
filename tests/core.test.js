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

test('strayWindows: only new windows with blank pages or the opened link', () => {
  const before = [{ id: 1, tabs: [{ id: 11, url: 'https://a/' }] }];
  const after = before.concat([
    { id: 2, tabs: [{ id: 21, url: 'about:blank' }] },
    { id: 3, tabs: [{ id: 31, url: 'chrome://newtab/' }, { id: 32, url: 'https://new.example/x' }] },
    { id: 4, tabs: [{ id: 41, url: 'https://someone-elses.example/' }] },
  ]);
  assert.deepStrictEqual(C.strayWindows(before, after, 'https://new.example/x').map((w) => w.id), [2, 3]);
  assert.deepStrictEqual(C.strayWindows(after, after, 'https://new.example/x'), []);
});
