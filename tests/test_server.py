"""Server tests: the store logic with a fake clock, and the HTTP API on a real local port."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import app  # noqa: E402

YT = "https://www.youtube.com/watch?v=abcdefghijk"
PAGE = "https://example.org/article"
LAV = "http://127.0.0.1:4387/session/0123abcd"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def tabs(*pairs):
    return [{"id": i, "url": u, "title": "T " + u} for i, u in pairs]


class HelperTests(unittest.TestCase):
    def test_youtube_id(self):
        self.assertEqual(app.youtube_id(YT), "abcdefghijk")
        self.assertEqual(app.youtube_id("https://youtu.be/abcdefghijk?t=3"), "abcdefghijk")
        self.assertEqual(app.youtube_id("https://m.youtube.com/shorts/abcdefghijk"), "abcdefghijk")
        self.assertIsNone(app.youtube_id("https://www.youtube.com/@channel"))
        self.assertIsNone(app.youtube_id("https://notyoutube.com/watch?v=abcdefghijk"))

    def test_kind(self):
        s = app.load_settings({})
        self.assertEqual(app.kind_of(YT, s), "video")
        self.assertEqual(app.kind_of(LAV, s), "lavish")
        self.assertEqual(app.kind_of(PAGE, s), "page")
        self.assertEqual(app.kind_of("https://vimeo.com/12345", s), "video")

    def test_rewrite_and_title(self):
        s = {"rewrites": [{"from": "http://127.0.0.1:4387/", "to": "https://lavish.example.com/"}]}
        self.assertEqual(app.rewrite(LAV, s), "https://lavish.example.com/session/0123abcd")
        self.assertEqual(app.rewrite(PAGE, s), PAGE)
        self.assertEqual(app.clean_title("(3) Hello - YouTube", YT), "Hello")
        self.assertEqual(app.clean_title("", PAGE), PAGE)

    def test_valid_url(self):
        self.assertTrue(app.valid_url(PAGE))
        self.assertFalse(app.valid_url("javascript:alert(1)"))
        self.assertFalse(app.valid_url("chrome://newtab/"))
        self.assertFalse(app.valid_url(None))

    def test_settings_from_env_and_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w") as f:
                json.dump({"lang": "pt", "undo_seconds": 4}, f)
            s = app.load_settings({"TQ_CONFIG": p, "TQ_UNDO_SECONDS": "7", "TQ_OEMBED": "0"})
            self.assertEqual(s["lang"], "pt")
            self.assertEqual(s["undo_seconds"], 7)
            self.assertFalse(s["oembed"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.settings = app.load_settings({"TQ_OEMBED": "0"})
        self.s = app.Store(os.path.join(self.dir.name, "q.db"), self.settings, clock=self.clock)

    def tearDown(self):
        self.dir.cleanup()

    def urls(self):
        return [i["url"] for i in self.s.items()["items"]]

    def test_sync_lists_tabs_and_counts(self):
        self.s.sync(tabs((1, YT), (2, PAGE), (3, LAV), (4, "chrome://newtab/")))
        d = self.s.items()
        self.assertEqual(d["counts"], {"all": 3, "video": 1, "lavish": 1, "page": 1})
        v = [i for i in d["items"] if i["kind"] == "video"][0]
        self.assertEqual(v["thumb"], "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg")

    def test_tab_closed_by_hand_leaves_and_reopened_comes_back(self):
        self.s.sync(tabs((1, YT), (2, PAGE)))
        self.s.sync(tabs((1, YT)))
        self.assertEqual(self.urls(), [YT])
        self.s.sync(tabs((1, YT), (5, PAGE)))
        self.assertIn(PAGE, self.urls())

    def test_done_waits_for_undo_time_then_closes(self):
        self.s.sync(tabs((1, YT), (2, PAGE)))
        self.assertTrue(self.s.done(PAGE))
        self.assertNotIn(PAGE, self.urls())  # hidden from the list right away
        r = self.s.sync(tabs((1, YT), (2, PAGE)))
        self.assertEqual(r["actions"], [])  # still inside the Undo time
        self.assertEqual(r["again_in"], 10)
        self.clock.t += 10
        r = self.s.sync(tabs((1, YT), (2, PAGE)))
        self.assertEqual(r["actions"], [{"op": "close", "url": PAGE, "tab_ids": [2]}])
        r = self.s.sync(tabs((1, YT)), results=[{"op": "close", "url": PAGE, "result": "closed"}])
        self.assertEqual(r["actions"], [])
        self.assertNotIn(PAGE, self.urls())

    def test_undo_before_close_keeps_tab(self):
        self.s.sync(tabs((2, PAGE)))
        self.s.done(PAGE)
        self.assertEqual(self.s.undo(PAGE), "kept")
        self.clock.t += 60
        self.assertEqual(self.s.sync(tabs((2, PAGE)))["actions"], [])
        self.assertEqual(self.urls(), [PAGE])

    def test_undo_after_close_reopens(self):
        self.s.sync(tabs((2, PAGE)))
        self.s.done(PAGE)
        self.clock.t += 11
        self.s.sync(tabs((2, PAGE)))
        self.s.sync(tabs())  # robot closed it
        self.assertEqual(self.s.undo(PAGE), "reopening")
        r = self.s.sync(tabs())
        self.assertEqual(len(r["actions"]), 1)
        a = r["actions"][0]
        self.assertEqual((a["op"], a["url"]), ("open", PAGE))
        self.assertEqual(self.s.sync(tabs())["actions"], [])  # sent once
        self.s.sync(tabs((7, PAGE)), results=[{"op": "open", "id": a["id"], "result": "opened"}])
        self.assertEqual(self.urls(), [PAGE])
        self.assertEqual(self.s.items()["pending"], [])

    def test_close_uses_fresh_tab_ids_after_restart(self):
        self.s.sync(tabs((2, PAGE)))
        self.s.done(PAGE)
        self.clock.t += 11
        r = self.s.sync(tabs((2, PAGE)))
        self.assertEqual(r["actions"][0]["tab_ids"], [2])
        # Chrome restarted: same page, new id. The robot reported "gone"; the next sync sends the new id.
        r = self.s.sync(tabs((99, PAGE)), results=[{"op": "close", "url": PAGE, "result": "gone"}])
        self.assertEqual(r["actions"], [{"op": "close", "url": PAGE, "tab_ids": [99]}])

    def test_duplicate_tabs_close_together(self):
        self.s.sync(tabs((2, PAGE), (3, PAGE)))
        self.assertEqual(self.s.items()["counts"]["all"], 1)
        self.s.done(PAGE)
        self.clock.t += 11
        self.assertEqual(self.s.sync(tabs((2, PAGE), (3, PAGE)))["actions"][0]["tab_ids"], [2, 3])

    def test_add_link(self):
        self.assertEqual(self.s.add("ftp://x"), None)
        self.assertEqual(self.s.add(PAGE), "queued")
        self.assertEqual(self.s.add(PAGE), "already")
        self.assertEqual(len(self.s.items()["pending"]), 1)
        r = self.s.sync(tabs())
        self.assertEqual(r["actions"][0]["op"], "open")
        self.s.sync(tabs((4, PAGE)))  # it showed up in the window
        self.assertEqual(self.s.items()["pending"], [])
        self.assertEqual(self.s.add(PAGE), "already")

    def test_open_sent_again_only_after_ten_minutes(self):
        self.s.add(PAGE)
        self.assertEqual(len(self.s.sync(tabs())["actions"]), 1)
        self.clock.t += 300
        self.assertEqual(self.s.sync(tabs())["actions"], [])
        self.clock.t += 301
        self.assertEqual(len(self.s.sync(tabs())["actions"]), 1)

    def test_done_on_unknown_is_false(self):
        self.assertFalse(self.s.done("https://nope.example/"))
        self.assertEqual(self.s.undo("https://nope.example/"), "nothing")

    def test_window_order(self):
        self.settings["order"] = "window"
        self.s.sync(tabs((1, YT), (2, PAGE)))
        self.assertEqual(self.urls(), [YT, PAGE])

    def test_channel_filled_in_background(self):
        self.settings["oembed"] = True
        done = threading.Event()

        def fake(url):
            done.set()
            return "Some Channel"
        self.s.fetch_channel = fake
        self.s.sync(tabs((1, YT)))
        done.wait(2)
        for _ in range(50):
            if self.s.items()["items"][0]["channel"]:
                break
            threading.Event().wait(0.02)
        self.assertEqual(self.s.items()["items"][0]["channel"], "Some Channel")


class HttpTests(unittest.TestCase):
    TOKEN = "t" * 24

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ.pop("TQ_KEY", None)
        self.app = app.App(self.dir.name, self.TOKEN, app.load_settings({"TQ_OEMBED": "0", "TQ_LANG": "pt"}))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()

    def req(self, method, path, body=None, token=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Token"] = token
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw.decode())
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_health_and_key_protection(self):
        self.assertEqual(self.req("GET", "/health")[0], 200)
        self.assertEqual(self.req("GET", "/")[0], 404)
        self.assertEqual(self.req("GET", "/wrongkey/")[0], 404)
        self.assertEqual(self.req("GET", "/wrongkey/api/items")[0], 404)
        code, page = self.req("GET", "/%s/" % self.app.key)
        self.assertEqual(code, 200)
        self.assertIn('lang="pt-BR"', page)
        self.assertIn('"lang": "pt"', page)
        self.assertNotIn("__TQ_CONFIG__", page)
        self.assertEqual(self.req("GET", "/%s/manifest.json" % self.app.key)[0], 200)

    def test_robot_needs_token(self):
        self.assertEqual(self.req("POST", "/api/robot/sync", {"tabs": []})[0], 403)
        self.assertEqual(self.req("POST", "/api/robot/sync", {"tabs": []}, token="wrong")[0], 403)
        self.assertEqual(self.req("GET", "/api/robot/info")[0], 403)

    def test_full_flow(self):
        k = self.app.key
        code, r = self.req("POST", "/api/robot/sync", {"tabs": tabs((1, YT), (2, PAGE)), "window_id": 5}, token=self.TOKEN)
        self.assertEqual((code, r["actions"]), (200, []))
        code, d = self.req("GET", "/%s/api/items" % k)
        self.assertEqual(d["counts"]["all"], 2)
        self.assertLess(d["mac_seen_ago"], 5)
        self.assertEqual(self.req("POST", "/%s/api/done" % k, {"url": PAGE})[1], {"ok": True})
        self.assertEqual(self.req("POST", "/%s/api/undo" % k, {"url": PAGE})[1]["result"], "kept")
        self.assertEqual(self.req("POST", "/%s/api/add" % k, {"url": "not a link"})[0], 400)
        self.assertEqual(self.req("POST", "/%s/api/add" % k, {"url": "https://new.example/"})[1]["result"], "queued")
        code, r = self.req("POST", "/api/robot/sync", {"tabs": tabs((1, YT), (2, PAGE))}, token=self.TOKEN)
        self.assertEqual([a["op"] for a in r["actions"]], ["open"])
        code, info = self.req("GET", "/api/robot/info", token=self.TOKEN)
        self.assertEqual((info["key"], info["items"]), (k, 2))

    def test_rotate_key(self):
        old = self.app.key
        code, r = self.req("POST", "/api/robot/rotate-key", {}, token=self.TOKEN)
        self.assertEqual(code, 200)
        self.assertNotEqual(r["key"], old)
        self.assertEqual(self.req("GET", "/%s/" % old)[0], 404)
        self.assertEqual(self.req("GET", "/%s/" % r["key"])[0], 200)
        # the new key survives a restart
        again = app.App(self.dir.name, self.TOKEN, app.load_settings({}))
        self.assertEqual(again.key, r["key"])
        st = os.stat(os.path.join(self.dir.name, "key")).st_mode & 0o777
        self.assertEqual(st, 0o600)

    def test_bad_json_is_ignored(self):
        r = urllib.request.Request(self.base + "/api/robot/sync", data=b"{nope", method="POST",
                                   headers={"X-Token": self.TOKEN, "Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            self.assertEqual(json.load(resp)["actions"], [])


class GenericCodeTests(unittest.TestCase):
    """The code is public and generic: no personal domain, window id or name in it."""

    def test_no_personal_values(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # extra words (your domain, window id, e-mail) can come from TQ_PRIVATE_WORDS="a,b,c"
        bad = ["saldanha"] + [w.strip().lower() for w in os.environ.get("TQ_PRIVATE_WORDS", "").split(",") if w.strip()]
        for d in ("bin", "robot", "server"):
            for dirpath, _, files in os.walk(os.path.join(root, d)):
                for name in files:
                    if name.endswith(".pyc") or "data" in dirpath:
                        continue
                    with open(os.path.join(dirpath, name), errors="replace") as f:
                        text = f.read().lower()
                    for b in bad:
                        self.assertNotIn(b, text, "%s in %s" % (b, name))


class MainTests(unittest.TestCase):
    def test_refuses_short_token(self):
        old = os.environ.get("TQ_TOKEN")
        os.environ["TQ_TOKEN"] = "short"
        try:
            with self.assertRaises(SystemExit):
                app.main()
        finally:
            if old is None:
                os.environ.pop("TQ_TOKEN", None)
            else:
                os.environ["TQ_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()
