// tab-queue robot (JXA). Run by launchd every 30 s:  /usr/bin/osascript -l JavaScript robot.js [mode]
//   mode: (none) one pass  |  -v  one pass, verbose log  |  send-front  move the front tab to the queue window first
// Running inside Apple's /usr/bin/osascript keeps the "control Google Chrome" permission stable across updates.
// Reads the tabs of the queue window, sends them to the server, runs the actions it gets back
// (close a tab after Done, open a link sent from the phone, reopen after Undo). It never opens a Chrome window.
// Config: ~/.config/tab-queue/config.json (or $TQ_CONFIG_FILE). Log: ~/.local/state/tab-queue/robot.log
ObjC.import('Foundation');

var ROBOT_VERSION = '0.2.1';

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
    var startedAt = age === null ? null : Date.now() / 1000 - age; // when this Chrome process started
    if (age !== null && age < (cfg.min_chrome_age || 90)) { log('Chrome started ' + age + ' s ago, waiting'); return; }

    var wins = chrome.windows();
    var pick = C.pickWindow(wins.map(function (w) {
      return { id: w.id, mode: w.mode, urls: w.tabs.map(function (t) { return C.realTab(t.url, t.title).url; }) };
    }), cfg.window, state);
    if (!pick) { log('no normal Chrome window'); return; }
    var Q = pick.id;
    if (String(Q) !== String(state.window_id)) log('queue window ' + Q + ' (' + pick.reason + ')');

    // tabs of the queue window only (one batch read of that window), url already real; null if the window is gone
    function readQueue() {
      var tabs = chrome.windowTabs(Q);
      if (!tabs) return null;
      return tabs.map(function (t) { var r = C.realTab(t.url, t.title); return { id: t.id, url: r.url, title: r.title }; });
    }
    function queueTabs() { return readQueue() || []; }

    // Open a link as a new tab at the end of the queue window, without taking the screen. Returns {result, tab_id}.
    function openInQueue(url) {
      var before = readQueue();
      if (before === null) {
        log('open ' + url + ': queue window ' + Q + ' is gone, not opening anything');
        return { result: 'no-queue-window' };
      }
      var had = C.findOpen(url, before);
      if (had) return { result: 'already-open', tab_id: had.id };
      var method = cfg.open_method || 'applescript';
      var front0 = chrome.frontApp();
      var t0 = Date.now();
      var newWindowIds = [];
      var hadIds = null; // open-g: every tab id that exists now, so only a tab that appears later gets adopted
      if (method === 'open-g') {
        hadIds = [];
        chrome.windows().forEach(function (w) { w.tabs.forEach(function (t) { hadIds.push(t.id); }); });
      }
      try {
        if (method === 'applescript') newWindowIds = chrome.openTab(Q, url);
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
        if (!tab && method === 'open-g') chrome.adoptNewTab(url, Q, hadIds);
      }
      // a "make new tab" can leave an empty New Tab behind: close it if it is one we just made
      var ids = {}; before.forEach(function (t) { ids[String(t.id)] = 1; });
      queueTabs().forEach(function (t) {
        if (!ids[String(t.id)] && (!tab || String(t.id) !== String(tab.id)) && /^chrome:\/\/newtab\/?$/.test(t.url)) {
          chrome.closeTab(Q, t.id); log('closed stray New Tab ' + t.id);
        }
      });
      // a window that appeared DURING our own make-tab call (the call reports which) and holds only our link or
      // about:blank is ours: close it. Any other new window belongs to someone else (other automations open
      // windows too): leave it and only write it in the log.
      var born = (newWindowIds || []).map(function (id) { return { id: id, tabs: chrome.windowTabs(id) || [] }; });
      var mine = C.strayWindows(born, url);
      born.forEach(function (w) {
        if (mine.indexOf(w) >= 0) {
          log('a new Chrome window ' + w.id + ' appeared during the open and holds only our page; closing it');
          w.tabs.forEach(function (t) { chrome.closeTab(w.id, t.id); });
        } else log('a new Chrome window ' + w.id + ' appeared during the open but is not ours (' + w.tabs.length + ' tab(s)); left alone');
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
      var wt = chrome.windowTabs(t.window_id);
      var still = wt && wt.filter(function (x) { return String(x.id) === String(t.id); })[0];
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
        w.tabs.forEach(function (t) { var rt = C.realTab(t.url, t.title);
        out.push({ id: t.id, url: rt.url, window_id: w.id, suspended: rt.suspended }); });
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
    // (ids change when Chrome restarts, so on the first run after a restart it only records what is open)
    var routed = [];
    if (C.restarted(state, pick, startedAt)) { if (state.other_seen) log('Chrome restarted or the queue window changed: recording the other windows, moving nothing'); }
    else {
      C.restoredWindows(others, state.other_seen).forEach(function (w) {
        log('window ' + w.window_id + ' got ' + w.count + ' new tabs at once (more than ' + C.RESTORE_BURST + '): looks like a restore, recording them and moving nothing from it');
      });
      routed = C.planRoute(others, state.other_seen, routes);
    }
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
    writeFile(statePath, JSON.stringify({ window_id: Q, urls: urls, other_seen: seen,
      chrome_started: startedAt === null ? state.chrome_started : Math.round(startedAt), at: C.stamp(new Date()) }));
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
      windowIds: function () { return c.windows.id().map(Number); },
      // id, url and title of every tab of one window, 3 batch reads; null if the window does not exist
      windowTabs: function (wid) {
        try {
          var w = c.windows.byId(+wid), tids = w.tabs.id(), urls = w.tabs.url(), titles = w.tabs.title(), tabs = [];
          for (var k = 0; k < tids.length; k++) tabs.push({ id: +tids[k], url: urls[k], title: titles[k] });
          return tabs;
        } catch (e) { return null; }
      },
      closeTab: function (wid, tid) { c.windows.byId(+wid).tabs.byId(+tid).close(); },
      // one osascript process: remember the front app, make the tab, give the screen back at once
      // (measured: Chrome in front for under 0.6 s; a separate call left it in front for seconds).
      // Returns the ids of windows that appeared between the lines right before and right after the make.
      openTab: function (wid, url) {
        var src = 'set p to ""\nset idsBefore to {}\nset idsAfter to {}\n' +
          'try\ntell application "System Events" to set p to name of first process whose frontmost is true\nend try\n' +
          'tell application ' + asStr(name) + '\n' +
          'if not (exists window id ' + (+wid) + ') then error "queue window missing"\n' +
          'set idsBefore to id of every window\n' +
          'make new tab at end of tabs of window id ' + (+wid) + ' with properties {URL:' + asStr(url) + '}\n' +
          'set idsAfter to id of every window\n' +
          'end tell\n' +
          'if p is not "" and p is not ' + asStr(name) + ' then\ntry\ntell application p to activate\nend try\nend if\n' +
          'set born to ""\nrepeat with i in idsAfter\nif idsBefore does not contain (contents of i) then set born to born & (contents of i) & ","\nend repeat\nreturn born';
        var out = sh('/usr/bin/osascript -e ' + q(src));
        return String(out).split(',').filter(function (x) { return /^\d+$/.test(x.trim()); }).map(Number);
      },
      openBackground: function (url) { sh('open -g -a ' + q(name) + ' ' + q(url)); },
      // after "open -g" the tab lands in Chrome's last active window: bring it to the queue window.
      // Only a tab whose id did not exist before the open (hadIds) is ours; one the user already had stays.
      adoptNewTab: function (url, wid, hadIds) {
        var f = C.pickAdopt(this.windows(), wid, url, hadIds);
        if (f) { this.openTab(wid, url); this.closeTab(f.window_id, f.tab_id); }
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
    function count(k) { var d = load(); d[k] = (d[k] || 0) + 1; save(d); }
    return {
      running: function () { return load().running !== false; },
      ageSeconds: function () { var a = load().age; return a === undefined ? 3600 : a; },
      windows: function () { count('reads_all'); return load().windows; },
      windowIds: function () { return load().windows.map(function (w) { return w.id; }); },
      windowTabs: function (wid) {
        count('reads_q');
        var w = load().windows.filter(function (x) { return String(x.id) === String(wid); })[0];
        return w ? w.tabs : null;
      },
      closeTab: function (wid, tid) {
        var d = load();
        d.windows.forEach(function (w) {
          if (String(w.id) === String(wid)) w.tabs = w.tabs.filter(function (t) { return String(t.id) !== String(tid); });
        });
        d.windows = d.windows.filter(function (w) { return w.tabs.length; }); // last tab closed = window closed
        (d.log = d.log || []).push('close ' + tid);
        save(d);
      },
      // returns the ids of the windows that appeared during the call, like the real one
      openTab: function (wid, url) {
        var d = load(), born = [];
        if (!d.windows.some(function (w) { return String(w.id) === String(wid); })) throw new Error('queue window missing');
        d.next_id = (d.next_id || 9000) + 1;
        if (d.make_creates_window) { born.push(d.next_id + 500); d.windows.push({ id: d.next_id + 500, mode: 'normal', active: 1, tabs: [{ id: d.next_id + 501, url: 'about:blank', title: '' }] }); }
        if (d.foreign_window) { born.push(d.next_id + 600); d.windows.push({ id: d.next_id + 600, mode: 'normal', active: 1, tabs: [{ id: d.next_id + 601, url: d.foreign_window, title: '' }] }); }
        d.windows.forEach(function (w) { if (String(w.id) === String(wid)) w.tabs.push({ id: d.next_id, url: url, title: url }); });
        (d.log = d.log || []).push('open ' + url);
        save(d);
        return born;
      },
      // open -g: the tab lands in the first (front) window, unless the fake says Chrome did nothing
      openBackground: function (url) {
        if (load().open_g_noop) return;
        var d = load();
        d.next_id = (d.next_id || 9000) + 1;
        d.windows[0].tabs.push({ id: d.next_id, url: url, title: url });
        save(d);
      },
      adoptNewTab: function (url, wid, hadIds) {
        var f = C.pickAdopt(this.windows(), wid, url, hadIds);
        if (f) { this.openTab(wid, url); this.closeTab(f.window_id, f.tab_id); }
      },
      frontApp: function () { return load().front || 'Finder'; }
    };
  }
}
