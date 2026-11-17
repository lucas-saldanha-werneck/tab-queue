#!/usr/bin/env python3
"""tab-queue server: keeps the list of tabs of one Chrome window and the requests to close or open tabs.

The Mac robot posts the tabs of the queue window to /api/robot/sync (header X-Token) and gets back the
actions to run (close a tab, open a link). The phone page lives at /<key>/ and its API at /<key>/api/...
Without the right key every path answers 404.

Standard library only (Python 3.9+), state in one SQLite file. Settings come from environment variables
and, optionally, a JSON file (TQ_CONFIG). See README.md.
"""
import contextlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

VERSION = "0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "lang": "en",
    "title": "",
    "undo_seconds": 10,
    # Pages that count as "lavish" notes (local lavish-axi review pages by default).
    "lavish_match": [r"^https?://(127\.0\.0\.1|localhost):4387/session/"],
    # Rewrite links before the phone opens them, e.g. a local address to its public one.
    "rewrites": [],
    "order": "newest",  # or "window" (same order as the tabs on the Mac)
    "oembed": True,  # fetch the channel name of YouTube videos from youtube.com/oembed
    "max_items": 2000,
    # False: the page also opens at "/" with no key. Only when a login in front of the server
    # (Cloudflare Access, Tailscale, a VPN...) already keeps strangers out.
    "key_in_link": True,
}

VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")


def load_settings(env=None):
    env = os.environ if env is None else env
    s = dict(DEFAULTS)
    path = env.get("TQ_CONFIG")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            s.update(json.load(f))
    if env.get("TQ_LANG"):
        s["lang"] = env["TQ_LANG"]
    if env.get("TQ_UNDO_SECONDS"):
        s["undo_seconds"] = int(env["TQ_UNDO_SECONDS"])
    if env.get("TQ_REWRITES"):
        s["rewrites"] = json.loads(env["TQ_REWRITES"])
    if env.get("TQ_LAVISH_MATCH"):
        s["lavish_match"] = json.loads(env["TQ_LAVISH_MATCH"])
    if env.get("TQ_OEMBED") in ("0", "false", "no"):
        s["oembed"] = False
    if env.get("TQ_KEY_IN_LINK") in ("0", "false", "no"):
        s["key_in_link"] = False
    return s


# ---------- pure helpers ----------

def youtube_id(url):
    p = urlparse(url)
    host = p.netloc.lower().split(":")[0]
    if host == "youtu.be" or host.endswith(".youtu.be"):
        v = p.path.strip("/").split("/")[0]
        return v or None
    if not (host == "youtube.com" or host.endswith(".youtube.com")):
        return None
    if p.path == "/watch":
        v = parse_qs(p.query).get("v", [""])[0]
        return v or None
    m = re.match(r"^/(shorts|live|embed)/([\w-]{6,})", p.path)
    return m.group(2) if m else None


def kind_of(url, settings):
    for pat in settings.get("lavish_match") or []:
        if re.search(pat, url):
            return "lavish"
    if youtube_id(url):
        return "video"
    host = urlparse(url).netloc.lower().split(":")[0]
    if host.endswith("vimeo.com") and re.match(r"^/\d+", urlparse(url).path):
        return "video"
    return "page"


def rewrite(url, settings):
    for r in settings.get("rewrites") or []:
        src, dst = r.get("from", ""), r.get("to", "")
        if src and url.startswith(src):
            return dst + url[len(src):]
    return url


def clean_title(title, url):
    t = (title or "").strip()
    for suffix in (" - YouTube",):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    t = re.sub(r"^\(\d+\)\s+", "", t)  # "(3) Title" unread counters
    return t or url


def valid_url(url):
    if not isinstance(url, str) or len(url) > 4000:
        return False
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.netloc)


def site_of(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def oembed(url):
    try:
        q = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
        with urllib.request.urlopen(q, timeout=6) as r:
            d = json.load(r)
        return d.get("author_name")
    except Exception:
        return None


# ---------- store ----------

def undo_toast_ms(undo_seconds):
    u = float(undo_seconds) * 1000
    return int(max(u / 2, u - 2000))


class Store:
    """All state. Every method takes the lock; SQLite is opened per call (safe across threads)."""

    def __init__(self, path, settings, clock=time.time, fetch_channel=oembed):
        self.path = path
        self.settings = settings
        self.clock = clock
        self.fetch_channel = fetch_channel
        self.lock = threading.RLock()
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS items(
                url TEXT PRIMARY KEY, title TEXT, kind TEXT, channel TEXT,
                tab_ids TEXT DEFAULT '[]', position INTEGER DEFAULT 0,
                first_seen REAL, last_seen REAL, on_mac INTEGER DEFAULT 1,
                done_at REAL, closed_at REAL, close_sent_at REAL);
            CREATE TABLE IF NOT EXISTS opens(
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, reason TEXT,
                created REAL, sent_at REAL, finished_at REAL, result TEXT);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            """)

    @contextlib.contextmanager
    def db(self):
        """One connection per use: commit on success, roll back on error, always close."""
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    # meta
    def get_meta(self, k, default=None):
        with self.lock, self.db() as c:
            r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return r["v"] if r else default

    def set_meta(self, k, v):
        with self.lock, self.db() as c:
            c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    # robot side
    def sync(self, tabs, results=(), window_id=None, robot_version=None):
        """tabs = every tab of the queue window now: [{id, url, title}] (url already resolved).
        Returns the actions the robot must run."""
        now = self.clock()
        undo = float(self.settings.get("undo_seconds", 10))
        present = {}
        for pos, t in enumerate(tabs or []):
            url = t.get("url") or ""
            if not valid_url(url):
                continue
            e = present.setdefault(url, {"title": t.get("title") or "", "ids": [], "pos": pos})
            if t.get("id") is not None:
                e["ids"].append(int(t["id"]))
        new_videos = []
        with self.lock, self.db() as c:
            self._apply_results(c, results or [], now)
            rows = {r["url"]: r for r in c.execute("SELECT * FROM items")}
            for url, e in present.items():
                r = rows.get(url)
                title = clean_title(e["title"], url)
                if r is None:
                    kind = kind_of(url, self.settings)
                    c.execute("INSERT INTO items(url,title,kind,tab_ids,position,first_seen,last_seen,on_mac)"
                              " VALUES(?,?,?,?,?,?,?,1)", (url, title, kind, json.dumps(e["ids"]), e["pos"], now, now))
                    if kind == "video":
                        new_videos.append(url)
                elif r["on_mac"] and not r["closed_at"]:
                    c.execute("UPDATE items SET title=?, tab_ids=?, position=?, last_seen=? WHERE url=?",
                              (title, json.dumps(e["ids"]), e["pos"], now, url))
                else:
                    # back on the Mac (reopened by hand, by Undo, or a link sent from the phone): back in the queue
                    c.execute("UPDATE items SET title=?, tab_ids=?, position=?, last_seen=?, on_mac=1, done_at=NULL,"
                              " closed_at=NULL, close_sent_at=NULL, first_seen=? WHERE url=?",
                              (title, json.dumps(e["ids"]), e["pos"], now, now, url))
            for url, r in rows.items():
                if url not in present and r["on_mac"]:
                    # closed on the Mac (by the robot after Done, or by hand): leaves the list
                    c.execute("UPDATE items SET on_mac=0, tab_ids='[]', closed_at=COALESCE(closed_at,?) WHERE url=?",
                              (now, url))
            # an open request whose link is now in the window is finished
            for o in c.execute("SELECT id,url FROM opens WHERE finished_at IS NULL").fetchall():
                if o["url"] in present:
                    c.execute("UPDATE opens SET finished_at=?, result=COALESCE(result,'seen') WHERE id=?", (now, o["id"]))
            actions = []
            for r in c.execute("SELECT url, tab_ids FROM items WHERE done_at IS NOT NULL AND on_mac=1 AND done_at<=?",
                               (now - undo,)):
                ids = json.loads(r["tab_ids"] or "[]")
                if ids:
                    actions.append({"op": "close", "url": r["url"], "tab_ids": ids})
                    c.execute("UPDATE items SET close_sent_at=? WHERE url=?", (now, r["url"]))
            # open requests: send once; send again only if the robot never answered within 10 min
            for o in c.execute("SELECT id,url FROM opens WHERE finished_at IS NULL AND (sent_at IS NULL OR sent_at<?)",
                               (now - 600,)).fetchall():
                actions.append({"op": "open", "id": o["id"], "url": o["url"]})
                c.execute("UPDATE opens SET sent_at=? WHERE id=?", (now, o["id"]))
            # the robot can wait and sync again soon when a Done is about to become due
            due = [r["done_at"] + undo - now for r in c.execute(
                "SELECT done_at FROM items WHERE done_at IS NOT NULL AND on_mac=1 AND done_at>?", (now - undo,))]
            c.execute("DELETE FROM items WHERE url IN (SELECT url FROM items WHERE on_mac=0 ORDER BY last_seen DESC"
                      " LIMIT -1 OFFSET ?)", (int(self.settings.get("max_items", 2000)),))
            c.execute("DELETE FROM opens WHERE finished_at IS NOT NULL AND finished_at<?", (now - 7 * 86400,))
        self.set_meta("last_sync", now)
        if window_id is not None:
            self.set_meta("window_id", window_id)
        if robot_version:
            self.set_meta("robot_version", robot_version)
        if new_videos and self.settings.get("oembed"):
            threading.Thread(target=self._fill_channels, args=(new_videos,), daemon=True).start()
        out = {"actions": actions, "undo_seconds": undo}
        if due:
            out["again_in"] = max(1, round(min(due) + 0.5))
        return out

    def _apply_results(self, c, results, now):
        for res in results:
            if res.get("op") == "open" and res.get("id") is not None:
                c.execute("UPDATE opens SET finished_at=?, result=? WHERE id=? AND finished_at IS NULL",
                          (now, str(res.get("result", ""))[:200], int(res["id"])))
            elif res.get("op") == "close" and res.get("url") and res.get("result") != "closed":
                # not closed (tab changed or gone): let the next sync send it again with fresh ids
                c.execute("UPDATE items SET close_sent_at=NULL WHERE url=?", (res["url"],))

    def _fill_channels(self, urls):
        for url in urls:
            ch = self.fetch_channel(url)
            if ch:
                with self.lock, self.db() as c:
                    c.execute("UPDATE items SET channel=? WHERE url=?", (ch, url))

    # phone side
    def items(self):
        now = self.clock()
        order = "position ASC" if self.settings.get("order") == "window" else "first_seen DESC, position DESC"
        with self.lock, self.db() as c:
            rows = c.execute("SELECT * FROM items WHERE on_mac=1 AND done_at IS NULL ORDER BY " + order).fetchall()
            pend = c.execute("SELECT id,url,created,sent_at,result FROM opens WHERE finished_at IS NULL"
                             " ORDER BY created DESC").fetchall()
        out = []
        for r in rows:
            vid = youtube_id(r["url"]) if r["kind"] == "video" else None
            out.append({
                "url": r["url"], "open_url": rewrite(r["url"], self.settings), "title": r["title"],
                "kind": r["kind"], "site": site_of(r["url"]), "channel": r["channel"],
                "thumb": ("https://i.ytimg.com/vi/%s/mqdefault.jpg" % vid) if vid else None,
                "added": r["first_seen"],
            })
        pending = [{"id": p["id"], "url": p["url"], "open_url": rewrite(p["url"], self.settings),
                    "kind": kind_of(p["url"], self.settings), "site": site_of(p["url"]),
                    "sent": bool(p["sent_at"])} for p in pend]
        counts = {"all": len(out)}
        for k in ("video", "lavish", "page"):
            counts[k] = sum(1 for i in out if i["kind"] == k)
        last = self.get_meta("last_sync")
        return {"items": out, "pending": pending, "counts": counts,
                "mac_seen_ago": (now - float(last)) if last else None,
                "undo_seconds": self.settings.get("undo_seconds", 10),
                # how long the page shows its Undo button: always inside the server's Undo window
                "undo_toast_ms": undo_toast_ms(self.settings.get("undo_seconds", 10))}

    def done(self, url):
        with self.lock, self.db() as c:
            n = c.execute("UPDATE items SET done_at=? WHERE url=? AND on_mac=1 AND done_at IS NULL",
                          (self.clock(), url)).rowcount
        return n > 0

    def undo(self, url):
        """Done by mistake. If no close was sent to the robot yet: cancel. Otherwise the tab is gone or about to
        be (the robot may not have reported yet): ask the robot to open it again."""
        with self.lock, self.db() as c:
            return self._undo(c, url)

    def _undo(self, c, url):
        r = c.execute("SELECT * FROM items WHERE url=?", (url,)).fetchone()
        if r is None or r["done_at"] is None:
            return "nothing"
        if r["on_mac"] and r["close_sent_at"] is None:
            c.execute("UPDATE items SET done_at=NULL WHERE url=?", (url,))
            return "kept"
        c.execute("UPDATE items SET done_at=NULL, close_sent_at=NULL WHERE url=?", (url,))
        self._queue_open(c, url, "undo")
        return "reopening"

    def _queue_open(self, c, url, reason):
        if not c.execute("SELECT 1 FROM opens WHERE url=? AND finished_at IS NULL", (url,)).fetchone():
            c.execute("INSERT INTO opens(url,reason,created) VALUES(?,?,?)", (url, reason, self.clock()))

    def add(self, url, reason="phone"):
        if not valid_url(url):
            return None
        with self.lock, self.db() as c:
            r = c.execute("SELECT on_mac, done_at FROM items WHERE url=?", (url,)).fetchone()
            if r is not None and r["on_mac"] and r["done_at"] is not None:
                # marked Done but still open: cancel the Done (like an Undo) instead of queueing an open
                return "already" if self._undo(c, url) == "kept" else "queued"
            if r is not None and r["on_mac"]:
                return "already"
            if c.execute("SELECT 1 FROM opens WHERE url=? AND finished_at IS NULL", (url,)).fetchone():
                return "already"
            self._queue_open(c, url, reason)
        return "queued"


# ---------- secrets ----------

def load_key(data_dir, env):
    """The secret part of the page link. Kept in <data>/key so the rotate command survives restarts."""
    path = os.path.join(data_dir, "key")
    if os.path.exists(path):
        with open(path) as f:
            k = f.read().strip()
        if k:
            return k
    k = env.get("TQ_KEY") or secrets.token_urlsafe(18)
    save_key(data_dir, k)
    return k


def save_key(data_dir, k):
    path = os.path.join(data_dir, "key")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(k + "\n")
    os.replace(tmp, path)


# ---------- HTTP ----------

def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tab-queue"
        sys_version = ""

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            if "text" in ctype or "json" in ctype:
                ctype += "; charset=utf-8"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 4_000_000:
                return {}
            try:
                d = json.loads(self.rfile.read(n))
                return d if isinstance(d, dict) else {}
            except ValueError:
                return {}

        def robot_ok(self):
            return hmac.compare_digest(self.headers.get("X-Token", ""), app.token)

        def split_key(self):
            """'/<key>/rest' -> 'rest' when the key matches, else None.
            With key_in_link off, '/rest' -> 'rest' too (the keyed link keeps working)."""
            path = urlparse(self.path).path
            parts = path.split("/", 2)
            if len(parts) >= 2 and parts[1] and hmac.compare_digest(parts[1], app.key):
                return parts[2] if len(parts) > 2 else None
            if not app.open_root:
                return None
            return path[1:]

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                return self.send(200, {"ok": True, "version": VERSION})
            if path == "/api/robot/info":
                if not self.robot_ok():
                    return self.send(403, {"error": "token"})
                last = app.store.get_meta("last_sync")
                return self.send(200, {"key": app.key, "key_in_link": not app.open_root, "version": VERSION, "last_sync": float(last) if last else None,
                                       "items": app.store.items()["counts"]["all"]})
            parts = path.split("/")
            if len(parts) == 2 and parts[1] and hmac.compare_digest(parts[1], app.key):
                self.send_response(301)
                self.send_header("Location", "/%s/" % app.key)
                self.end_headers()
                return
            rest = self.split_key()
            if rest is None:
                return self.send(404, {"error": "not found"})
            if rest == "":
                return self.send(200, app.page(), "text/html")
            if rest == "api/items":
                return self.send(200, app.store.items())
            if rest == "manifest.json":
                name = app.settings.get("title") or "Tab queue"
                return self.send(200, {"name": name, "short_name": name[:12], "start_url": app.base(),
                                       "display": "standalone", "background_color": "#121315",
                                       "theme_color": "#121315"}, "application/manifest+json")
            return self.send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path.startswith("/api/robot/"):
                if not self.robot_ok():
                    return self.send(403, {"error": "token"})
                if path == "/api/robot/sync":
                    b = self.body()
                    return self.send(200, app.store.sync(b.get("tabs") or [], b.get("results") or [],
                                                         b.get("window_id"), b.get("version")))
                if path == "/api/robot/rotate-key":
                    app.rotate_key()
                    return self.send(200, {"key": app.key})
                return self.send(404, {"error": "not found"})
            rest = self.split_key()
            if rest is None:
                return self.send(404, {"error": "not found"})
            b = self.body()
            url = b.get("url") or ""
            if rest == "api/done":
                return self.send(200, {"ok": app.store.done(url)})
            if rest == "api/undo":
                return self.send(200, {"ok": True, "result": app.store.undo(url)})
            if rest == "api/add":
                r = app.store.add(url.strip())
                return self.send(200 if r else 400, {"ok": bool(r), "result": r})
            return self.send(404, {"error": "not found"})

    return Handler


class App:
    def __init__(self, data_dir, token, settings, store=None):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.token = token
        self.settings = settings
        self.key = load_key(data_dir, os.environ)
        self.open_root = not settings.get("key_in_link", True)
        self.store = store or Store(os.path.join(data_dir, "queue.db"), settings)
        with open(os.path.join(HERE, "page.html"), encoding="utf-8") as f:
            self._page = f.read()

    def base(self):
        return "/" if self.open_root else "/%s/" % self.key

    def rotate_key(self):
        self.key = secrets.token_urlsafe(18)
        save_key(self.data_dir, self.key)

    def page(self):
        cfg = {"lang": self.settings.get("lang", "en"), "title": self.settings.get("title", "")}
        lang = "pt-BR" if cfg["lang"].startswith("pt") else "en"
        return (self._page.replace("__TQ_CONFIG__", json.dumps(cfg).replace("<", "\\u003c"))
                .replace('lang="en"', 'lang="%s"' % lang, 1))


def main(argv=None):
    env = os.environ
    token = env.get("TQ_TOKEN", "")
    if len(token) < 16:
        sys.exit("TQ_TOKEN is missing or shorter than 16 characters (the robot's password).")
    data = env.get("TQ_DATA", os.path.join(HERE, "data"))
    app = App(data, token, load_settings(env))
    host, port = env.get("TQ_HOST", "127.0.0.1"), int(env.get("TQ_PORT", "8765"))
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    print("tab-queue %s listening on %s:%d (data in %s)" % (VERSION, host, port, data), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
