// tab-queue robot (JXA). Run by launchd every 30 s:  /usr/bin/osascript -l JavaScript robot.js [mode]
//   mode: (none) one pass  |  -v  one pass, verbose log  |  send-front  move the front tab to the queue window first
// Running inside Apple's /usr/bin/osascript keeps the "control Google Chrome" permission stable across updates.
// Reads the tabs of the queue window, sends them to the server, runs the actions it gets back
// (close a tab after Done, open a link sent from the phone, reopen after Undo). It never opens a Chrome window.
// Config: ~/.config/tab-queue/config.json (or $TQ_CONFIG_FILE). Log: ~/.local/state/tab-queue/robot.log
ObjC.import('Foundation');

var ROBOT_VERSION = '0.1.0';

function run(argv) {
  argv = argv || [];
  var app = Application.currentApplication();
  app.includeStandardAdditions = true;
  var env = $.NSProcessInfo.processInfo.environment;
  function getenv(k) { var v = env.objectForKey(k); return v.isNil() ? '' : ObjC.unwrap(v); }
  var HOME = $.NSHomeDirectory().js;
  var HERE = scriptDir();
  var STATE_DIR = getenv('TQ_STATE_DIR') || HOME + '/.local/state/tab-queue';
  var verbose = argv.indexOf('-v') >= 0;

  function sh(c) { return app.doShellScript(c); }
  function q(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }
  function readFile(p) {
    var s = $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null);
    return s.isNil() ? null : s.js;
  }
  function writeFile(p, text) {
    $(text).writeToFileAtomicallyEncodingError(p, true, $.NSUTF8StringEncoding, null);
  }
  function scriptDir() {
    var args = ObjC.deepUnwrap($.NSProcessInfo.processInfo.arguments);
    for (var i = 0; i < args.length; i++) if (/robot\.js$/.test(args[i])) return args[i].replace(/\/[^\/]*$/, '') || '.';
    return '.';
  }

  eval(readFile(HERE + '/core.js'));
  var C = TQCore;

  sh('mkdir -p ' + q(STATE_DIR));
  var LOG = STATE_DIR + '/robot.log';
  function log(m) {
    var line = C.stamp(new Date()) + ' ' + m + '\n';
    var fm = $.NSFileManager.defaultManager;
    if (!fm.fileExistsAtPath(LOG)) fm.createFileAtPathContentsAttributes(LOG, $(), $());
    var h = $.NSFileHandle.fileHandleForWritingAtPath(LOG);
    h.seekToEndOfFile; h.writeData($(line).dataUsingEncoding($.NSUTF8StringEncoding)); h.closeFile;
  }

  try {
    var cfgPath = getenv('TQ_CONFIG_FILE') || HOME + '/.config/tab-queue/config.json';
    var raw = readFile(cfgPath);
    if (raw === null) { log('no config at ' + cfgPath + ' (run: tab-queue install)'); return; }
    var cfg = JSON.parse(raw);
    var routes = cfg.route_to_queue || [];
    var statePath = STATE_DIR + '/state.json';
    var state = {};
    try { state = JSON.parse(readFile(statePath) || '{}'); } catch (e) { state = {}; }

    var chrome = getenv('TQ_FAKE_CHROME') ? fakeChrome(getenv('TQ_FAKE_CHROME')) : realChrome(cfg.chrome_app || 'Google Chrome');
    if (!chrome.running()) { if (verbose) log('Chrome is not running'); return; }
    var age = chrome.ageSeconds();
    if (age !== null && age < (cfg.min_chrome_age || 90)) { log('Chrome started ' + age + ' s ago, waiting'); return; }

    var wins = chrome.windows();
    var pick = C.pickWindow(wins.map(function (w) {
      return { id: w.id, mode: w.mode, urls: w.tabs.map(function (t) { return C.realTab(t.url, t.title).url; }) };
    }), cfg.window, state);
    if (!pick) { log('no normal Chrome window'); return; }
    var Q = pick.id;
    if (String(Q) !== String(state.window_id)) log('queue window ' + Q + ' (' + pick.reason + ')');

    function queueTabs() {
      var w = chrome.windows().filter(function (x) { return String(x.id) === String(Q); })[0];
      return (w ? w.tabs : []).map(function (t) { var r = C.realTab(t.url, t.title); return { id: t.id, url: r.url, title: r.title }; });
    }

    // Open a link as a new tab at the end of the queue window, without taking the screen. Returns {result, tab_id}.
    function openInQueue(url) {
      var winsBefore = chrome.windows();
      if (!winsBefore.some(function (w) { return String(w.id) === String(Q); })) {
        log('open ' + url + ': queue window ' + Q + ' is gone, not opening anything');
        return { result: 'no-queue-window' };
      }
      var before = queueTabs();
      var had = C.findOpen(url, before);
      if (had) return { result: 'already-open', tab_id: had.id };
      var method = cfg.open_method || 'applescript';
      var front0 = chrome.frontApp();
      var t0 = Date.now();
      try {
        if (method === 'applescript') chrome.openTab(Q, url);
        else if (method === 'open-g') chrome.openBackground(url);
        else return { result: 'unsupported open_method ' + method };
      } catch (e) {
        log('open ' + url + ' failed: ' + String(e).slice(0, 150));
        return { result: 'error' };
      }
      var tab = null;
      for (var i = 0; i < 20 && !tab; i++) {
        if (i) delay(0.25);
        tab = C.newTab(before.map(function (t) { return t.id; }), queueTabs(), url);
        if (!tab && method === 'open-g') tab = chrome.adoptNewTab(url, Q);
      }
      // a "make new tab" can leave an empty New Tab behind: close it if it is one we just made
      var ids = {}; before.forEach(function (t) { ids[String(t.id)] = 1; });
      queueTabs().forEach(function (t) {
        if (!ids[String(t.id)] && (!tab || String(t.id) !== String(tab.id)) && /^chrome:\/\/newtab\/?$/.test(t.url)) {
          chrome.closeTab(Q, t.id); log('closed stray New Tab ' + t.id);
        }
      });
      // never leave a new Chrome window behind: close one that appeared and holds only blank pages or this link
      C.strayWindows(winsBefore, chrome.windows(), url).forEach(function (w) {
        log('a new Chrome window ' + w.id + ' appeared after the open; closing its ' + w.tabs.length + ' blank tab(s)');
        w.tabs.forEach(function (t) { chrome.closeTab(w.id, t.id); });
      });
      var front1 = chrome.frontApp();
      log('open ' + url + ' -> ' + (tab ? 'tab ' + tab.id : 'NOT FOUND') + ' in ' + ((Date.now() - t0) / 1000).toFixed(1) +
          ' s; front app before=' + front0 + ' after=' + front1 + (front0 === front1 ? ' (kept)' : ' (CHANGED)'));
      return tab ? { result: 'opened', tab_id: tab.id } : { result: 'not-found' };
    }

    // Chrome's AppleScript "move tab" loses the page (it leaves an empty tab), so a move is: open the same
    // link in the queue window, check it is there, then close the original (id and address checked).
    function moveToQueue(t) {
      var r = openInQueue(t.url);
      if (r.result !== 'opened' && r.result !== 'already-open') { log('move failed for ' + t.url + ': ' + r.result); return false; }
      var w = chrome.windows().filter(function (x) { return String(x.id) === String(t.window_id); })[0];
      var still = w && w.tabs.filter(function (x) { return String(x.id) === String(t.id); })[0];
      if (still && C.realTab(still.url, still.title).url === t.url) {
        chrome.closeTab(t.window_id, t.id);
        log('moved ' + t.url + ' from window ' + t.window_id + ' to the queue');
      }
      return true;
    }

    function otherTabs() {
      var out = [];
      chrome.windows().forEach(function (w) {
        if (String(w.id) === String(Q) || (w.mode || 'normal') !== 'normal') return;
        w.tabs.forEach(function (t) { out.push({ id: t.id, url: C.realTab(t.url, t.title).url, window_id: w.id }); });
      });
      return out;
    }

    if (argv.indexOf('send-front') >= 0) {
      var front = chrome.windows()[0];
      if (!front || String(front.id) === String(Q)) log('send-front: the front window is already the queue window');
      else {
        var at = front.tabs[(front.active || 1) - 1];
        if (at) moveToQueue({ id: at.id, url: C.realTab(at.url, at.title).url, window_id: front.id });
      }
    }

    // pages matching route_to_queue that opened in another window since the last run go to the queue window
    var others = otherTabs();
    var routed = C.planRoute(others, state.other_seen || null, routes);
    routed.forEach(moveToQueue);
    if (routed.length) others = otherTabs();
    var seen = {}; others.forEach(function (t) { seen[String(t.id)] = 1; });

    var server = String(cfg.server || '').replace(/\/+$/, '');
    function post(path, body) {
      var tmp = sh('mktemp -d -t tabqueue');
      try {
        writeFile(tmp + '/body.json', JSON.stringify(body));
        writeFile(tmp + '/headers', 'X-Token: ' + cfg.token + '\nContent-Type: application/json\n');
        var out = sh('curl -sS -m 20 -A tab-queue-robot/' + ROBOT_VERSION + ' -H @' + q(tmp + '/headers') +
          ' --data-binary @' + q(tmp + '/body.json') + ' ' + q(server + path) + ' 2>&1 || true');
        try { return JSON.parse(out); } catch (e) { throw new Error('server did not answer JSON: ' + String(out).slice(0, 120)); }
      } finally { sh('rm -rf ' + q(tmp)); }
    }

    var results = [], tabs = queueTabs(), started = Date.now(), resp;
    for (var round = 0; round < 6; round++) {
      resp = post('/api/robot/sync', { tabs: tabs, results: results, window_id: Q, version: ROBOT_VERSION });
      if (resp.error) { log('server refused: ' + resp.error); break; }
      results = [];
      (resp.actions || []).forEach(function (a) {
        if (a.op === 'close') {
          var plan = C.planClose(a, queueTabs());
          plan.close.forEach(function (id) { chrome.closeTab(Q, id); });
          log('close ' + a.url + ' tabs ' + JSON.stringify(a.tab_ids) + ' -> ' + plan.result);
          results.push({ op: 'close', url: a.url, result: plan.result });
        } else if (a.op === 'open') {
          var r = openInQueue(a.url);
          results.push({ op: 'open', id: a.id, url: a.url, result: r.result });
        }
      });
      tabs = queueTabs();
      if (results.length) continue; // report the results right away
      // a Done is about to pass its Undo time: wait for it instead of waiting for the next launchd run
      var waitFor = resp.again_in;
      if (waitFor && (Date.now() - started) / 1000 + waitFor <= (cfg.max_wait || 25)) { delay(waitFor); tabs = queueTabs(); continue; }
      break;
    }

    var urls = tabs.map(function (t) { return t.url; }).slice(0, 300);
    writeFile(statePath, JSON.stringify({ window_id: Q, urls: urls, other_seen: seen, at: C.stamp(new Date()) }));
    if (verbose) log('sync: ' + tabs.length + ' tabs in window ' + Q);
  } catch (e) {
    log('ERROR ' + String(e).slice(0, 300));
    throw e;
  }

  // ---------- Chrome adapters ----------

  function realChrome(name) {
    var c = Application(name);
    function asStr(s) { return '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'; }
    return {
      running: function () { return c.running(); },
      ageSeconds: function () {
        var et = sh('p=$(pgrep -x ' + q(name) + ' | head -1); [ -n "$p" ] && ps -o etime= -p $p || true');
        return C.parseEtime(et);
      },
      windows: function () {
        var ids = c.windows.id(), modes = c.windows.mode(), out = [];
        for (var i = 0; i < ids.length; i++) {
          var w = c.windows.byId(ids[i]);
          var tids = w.tabs.id(), urls = w.tabs.url(), titles = w.tabs.title(), tabs = [];
          for (var k = 0; k < tids.length; k++) tabs.push({ id: +tids[k], url: urls[k], title: titles[k] });
          out.push({ id: +ids[i], mode: modes[i], active: w.activeTabIndex(), tabs: tabs });
        }
        return out;
      },
      closeTab: function (wid, tid) { c.windows.byId(+wid).tabs.byId(+tid).close(); },
      // one osascript process: remember the front app, make the tab, give the screen back at once
      // (measured: Chrome in front for under 0.6 s; a separate call left it in front for seconds)
      openTab: function (wid, url) {
        var src = 'set p to ""\n' +
          'try\ntell application "System Events" to set p to name of first process whose frontmost is true\nend try\n' +
          'tell application ' + asStr(name) + '\n' +
          'if not (exists window id ' + (+wid) + ') then error "queue window missing"\n' +
          'make new tab at end of tabs of window id ' + (+wid) + ' with properties {URL:' + asStr(url) + '}\n' +
          'end tell\n' +
          'if p is not "" and p is not ' + asStr(name) + ' then\ntry\ntell application p to activate\nend try\nend if';
        sh('/usr/bin/osascript -e ' + q(src));
      },
      openBackground: function (url) { sh('open -g -a ' + q(name) + ' ' + q(url)); },
      // after "open -g" the tab lands in Chrome's last active window: bring it to the queue window
      adoptNewTab: function (url, wid) {
        var ws = this.windows();
        for (var i = 0; i < ws.length; i++) {
          if (String(ws[i].id) === String(wid)) continue;
          for (var k = ws[i].tabs.length - 1; k >= 0; k--) {
            var t = ws[i].tabs[k];
            if (C.sameLink(t.url, url)) { this.openTab(wid, url); this.closeTab(ws[i].id, t.id); return null; }
          }
        }
        return null;
      },
      frontApp: function () {
        ObjC.import('AppKit');
        var a = $.NSWorkspace.sharedWorkspace.frontmostApplication;
        return a.isNil() ? '?' : a.localizedName.js;
      }
    };
  }

  // A JSON file standing in for Chrome, for automated tests (TQ_FAKE_CHROME=/path/fake.json).
  function fakeChrome(path) {
    function load() { return JSON.parse(readFile(path)); }
    function save(d) { writeFile(path, JSON.stringify(d, null, 1)); }
    return {
      running: function () { return load().running !== false; },
      ageSeconds: function () { var a = load().age; return a === undefined ? 3600 : a; },
      windows: function () { return load().windows; },
      closeTab: function (wid, tid) {
        var d = load();
        d.windows.forEach(function (w) {
          if (String(w.id) === String(wid)) w.tabs = w.tabs.filter(function (t) { return String(t.id) !== String(tid); });
        });
        d.windows = d.windows.filter(function (w) { return w.tabs.length; }); // last tab closed = window closed
        (d.log = d.log || []).push('close ' + tid);
        save(d);
      },
      openTab: function (wid, url) {
        var d = load();
        if (!d.windows.some(function (w) { return String(w.id) === String(wid); })) throw new Error('queue window missing');
        d.next_id = (d.next_id || 9000) + 1;
        if (d.make_creates_window) d.windows.push({ id: d.next_id + 500, mode: 'normal', active: 1, tabs: [{ id: d.next_id + 501, url: 'about:blank', title: '' }] });
        d.windows.forEach(function (w) { if (String(w.id) === String(wid)) w.tabs.push({ id: d.next_id, url: url, title: url }); });
        (d.log = d.log || []).push('open ' + url);
        save(d);
      },
      openBackground: function (url) { this.openTab(load().windows[0].id, url); },
      adoptNewTab: function () { return null; },
      frontApp: function () { return load().front || 'Finder'; }
    };
  }
}
