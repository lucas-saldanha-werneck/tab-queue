// tab-queue robot: pure logic, no Chrome and no network. Loaded by robot.js (JXA, through eval)
// and by the Node tests (require). Keep it plain ES5-compatible JavaScript.
var TQCore = (function () {
  function dec(v) {
    try { return decodeURIComponent(v.replace(/\+/g, ' ')); } catch (e) { return v; }
  }
  // value of ?k=... or &k=... up to the next & or #
  function param(u, k) {
    var m = new RegExp('[?&]' + k + '=([^&#]*)').exec(String(u));
    return m ? dec(m[1]) : '';
  }

  // The suspender's own url=/uri= is the first one in the string (the real address comes after it, so any
  // url= inside the real address is further right). Encoded value: up to the next & or #. Value that starts
  // with a scheme (not encoded): the real address itself, to the end of the string.
  function target(u) {
    u = String(u);
    var m = /[?&](?:url|uri)=/.exec(u);
    if (!m) return null;
    var rest = u.slice(m.index + m[0].length);
    var raw = /^[a-z][a-z0-9+.\-]*:\/\//i.test(rest) ? rest : rest.split(/[&#]/)[0];
    return { value: /^[a-z][a-z0-9+.\-]*:\/\//i.test(rest) ? raw : dec(raw), head: u.slice(0, m.index + 1) };
  }

  // Tabs frozen by suspender extensions (e.g. Tab Suspender) show chrome-extension://<id>/park.html?...&url=<real>.
  // The queue works with the real address.
  function realTab(url, title) {
    url = String(url || ''); title = String(title || '');
    if (url.indexOf('chrome-extension://') === 0 && /\/(park|suspended)\.html/.test(url)) {
      var t = target(url);
      if (t && t.value) return { url: t.value, title: param(t.head, 'title') || title, suspended: true };
    }
    return { url: url, title: title, suspended: false };
  }

  // ps -o etime= gives [[dd-]hh:]mm:ss
  function parseEtime(et) {
    et = String(et || '').trim();
    if (!et) return null;
    var d = 0, hms = et;
    if (et.indexOf('-') > 0) { d = +et.split('-')[0]; hms = et.split('-')[1]; }
    var p = hms.split(':').map(Number);
    while (p.length < 3) p.unshift(0);
    if (p.some(isNaN) || isNaN(d)) return null;
    return d * 86400 + p[0] * 3600 + p[1] * 60 + p[2];
  }

  function matchesAny(url, patterns) {
    for (var i = 0; i < (patterns || []).length; i++) {
      try { if (new RegExp(patterns[i]).test(url)) return true; } catch (e) { /* bad pattern: ignore */ }
    }
    return false;
  }

  // wins: [{id, mode, urls:[...]}]. want: a window id (number/string) or "largest".
  // state: {window_id, urls} from the last run. Returns {id, reason} or null.
  function pickWindow(wins, want, state) {
    var normal = (wins || []).filter(function (w) { return (w.mode || 'normal') === 'normal'; });
    if (!normal.length) return null;
    function byId(id) {
      for (var i = 0; i < normal.length; i++) if (String(normal[i].id) === String(id)) return normal[i];
      return null;
    }
    if (want !== undefined && want !== null && want !== '' && want !== 'largest') {
      var w = byId(want);
      if (w) return { id: w.id, reason: 'config' };
    }
    state = state || {};
    if (state.window_id !== undefined && state.window_id !== null) {
      var s = byId(state.window_id);
      if (s) return { id: s.id, reason: 'last' };
    }
    // the id changed (Chrome restarted): find the window that holds most of the tabs we saw last time
    var known = {};
    (state.urls || []).forEach(function (u) { known[u] = 1; });
    var best = null, bestHits = 0;
    if (state.urls && state.urls.length) {
      normal.forEach(function (w) {
        var hits = 0;
        (w.urls || []).forEach(function (u) { if (known[u]) hits++; });
        if (hits > bestHits) { best = w; bestHits = hits; }
      });
      if (best && bestHits >= Math.max(1, Math.min(3, Math.ceil(state.urls.length / 3)))) {
        return { id: best.id, reason: 'same-tabs' };
      }
    }
    var big = normal[0];
    normal.forEach(function (w) { if ((w.urls || []).length > (big.urls || []).length) big = w; });
    return { id: big.id, reason: 'largest' };
  }

  // Close only tabs whose id AND real address still match what the server asked for.
  // tabs: [{id, url}] of the queue window, url already real.
  function planClose(action, tabs) {
    var want = {};
    (action.tab_ids || []).forEach(function (id) { want[String(id)] = 1; });
    var ok = [], moved = [];
    (tabs || []).forEach(function (t) {
      if (!want[String(t.id)]) return;
      if (t.url === action.url) ok.push(t.id); else moved.push(t.id);
    });
    var result = ok.length ? 'closed' : moved.length ? 'changed' : 'gone';
    return { close: ok, result: result };
  }

  // Tabs in other windows that match routing patterns and were not there in the last run.
  // others: [{id, url, window_id}], seen: {tabId: 1} from the last run (null on the first run: move nothing).
  function planRoute(others, seen, patterns) {
    if (!seen) return [];
    return (others || []).filter(function (t) { return !seen[String(t.id)] && matchesAny(t.url, patterns); });
  }

  function sameLink(a, b) {
    function norm(u) { return String(u || '').replace(/^http:\/\//, 'https://').replace(/\/+$/, ''); }
    return norm(a) === norm(b);
  }

  function findOpen(url, tabs) {
    for (var i = 0; i < (tabs || []).length; i++) if (sameLink(tabs[i].url, url)) return tabs[i];
    return null;
  }

  // After "open -g", find the new tab: ids not seen before; prefer one with the same link.
  function newTab(beforeIds, afterTabs, url) {
    var had = {};
    (beforeIds || []).forEach(function (id) { had[String(id)] = 1; });
    var fresh = (afterTabs || []).filter(function (t) { return !had[String(t.id)]; });
    if (!fresh.length) return null;
    return findOpen(url, fresh) || (fresh.length === 1 ? fresh[0] : null);
  }

  // newWindows: [{id, tabs:[{id,url}]}], only windows that appeared during our own make-tab call.
  // Ours to close only when the single tab is our link or about:blank; anything else belongs to someone else.
  function strayWindows(newWindows, url) {
    return (newWindows || []).filter(function (w) {
      var tabs = w.tabs || [];
      return tabs.length === 1 && (tabs[0].url === 'about:blank' || sameLink(tabs[0].url, url));
    });
  }

  // After "open -g": a tab with this link in another window, whose id did not exist before the open.
  function pickAdopt(wins, queueId, url, hadIds) {
    var had = {};
    (hadIds || []).forEach(function (id) { had[String(id)] = 1; });
    for (var i = 0; i < (wins || []).length; i++) {
      if (String(wins[i].id) === String(queueId)) continue;
      var tabs = wins[i].tabs || [];
      for (var k = tabs.length - 1; k >= 0; k--) {
        if (!had[String(tabs[k].id)] && sameLink(tabs[k].url, url)) return { window_id: wins[i].id, tab_id: tabs[k].id };
      }
    }
    return null;
  }

  // Tab and window ids restart with Chrome, so what we saw last run says nothing about this run:
  // then routing only records. startedAt: epoch seconds when this Chrome process started (null if unknown).
  function restarted(state, pick, startedAt) {
    state = state || {};
    if (!state.other_seen || state.window_id === undefined || state.window_id === null) return true;
    if (!pick || (pick.reason !== 'config' && pick.reason !== 'last')) return true;
    if (startedAt !== null && startedAt !== undefined && state.chrome_started !== undefined &&
        Math.abs(startedAt - state.chrome_started) > 120) return true;
    return false;
  }

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function stamp(d) {
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
      pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  return { param: param, realTab: realTab, parseEtime: parseEtime, matchesAny: matchesAny, pickWindow: pickWindow,
    planClose: planClose, planRoute: planRoute, sameLink: sameLink, findOpen: findOpen, newTab: newTab, strayWindows: strayWindows, pickAdopt: pickAdopt, restarted: restarted, stamp: stamp };
})();
if (typeof module !== 'undefined') module.exports = TQCore;
