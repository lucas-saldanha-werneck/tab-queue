"""End-to-end with a fake Chrome: the real JXA robot (osascript, macOS only) and the CLI against a real local server.
The fake Chrome is a JSON file (TQ_FAKE_CHROME); nothing here touches your Chrome."""
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import app  # noqa: E402

HAS_OSA = sys.platform == "darwin" and shutil.which("osascript")
YT = "https://www.youtube.com/watch?v=abcdefghijk"
PAGE = "https://example.org/a"
LAV = "http://127.0.0.1:4387/session/abc"


def read(path):
    with open(path) as f:
        return f.read()


def load(path):
    return json.loads(read(path))


def dump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.token = secrets.token_hex(16)
        settings = app.load_settings({"TQ_OEMBED": "0"})
        settings["undo_seconds"] = 1
        self.app = app.App(os.path.join(self.tmp, "data"), self.token, settings)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.env = dict(os.environ, TQ_CONFIG_DIR=os.path.join(self.tmp, "cfg"), TQ_SHARE_DIR=os.path.join(self.tmp, "share"),
                        TQ_STATE_DIR=os.path.join(self.tmp, "state"), TQ_AGENTS_DIR=os.path.join(self.tmp, "agents"),
                        TQ_NO_LAUNCHCTL="1", HOME=self.tmp)
        self.env.pop("TQ_TOKEN", None)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, ok=True):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "tab-queue")] + list(args), env=self.env,
                           capture_output=True, text=True, timeout=60)
        if ok and r.returncode:
            self.fail("tab-queue %s failed (%d): %s %s" % (args, r.returncode, r.stdout, r.stderr))
        return r

    def phone(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("%s/%s/%s" % (self.base, self.app.key, path), data=data,
                                     method="POST" if body is not None else "GET",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)

    def install_remote(self, *extra):
        tf = os.path.join(self.tmp, "token")
        with open(tf, "w") as f:
            f.write(self.token)
        return self.cli("install", "--server", self.base, "--token-file", tf, *extra)


class CliTests(Base):
    def test_install_remote_writes_private_config_and_robot_agent(self):
        out = self.install_remote("--window", "123", "--interval", "20").stdout
        self.assertIn("remote server", out)
        cfgp = os.path.join(self.tmp, "cfg", "config.json")
        self.assertEqual(os.stat(cfgp).st_mode & 0o777, 0o600)
        cfg = load(cfgp)
        self.assertEqual((cfg["window"], cfg["interval"], cfg["local"]), (123, 20, False))
        self.assertEqual(cfg["open_method"], "applescript")
        robot = read(os.path.join(self.tmp, "agents", "local.tab-queue.robot.plist"))
        self.assertIn("<integer>20</integer>", robot)
        self.assertNotIn(self.token, robot)  # the token stays in the private config file
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "agents", "local.tab-queue.server.plist")))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "share", "app", "robot", "robot.js")))
        if shutil.which("plutil"):
            subprocess.run(["plutil", "-lint", os.path.join(self.tmp, "agents", "local.tab-queue.robot.plist")], check=True,
                           capture_output=True)

    def test_install_local_server(self):
        self.cli("install", "--lang", "pt", "--rewrite", "http://127.0.0.1:4387/=https://lavish.example.com/")
        cfg = load(os.path.join(self.tmp, "cfg", "config.json"))
        self.assertTrue(cfg["local"])
        self.assertEqual(cfg["server"], "http://127.0.0.1:8765")
        sp = os.path.join(self.tmp, "agents", "local.tab-queue.server.plist")
        self.assertEqual(os.stat(sp).st_mode & 0o777, 0o600)
        scfg = load(os.path.join(self.tmp, "cfg", "server.json"))
        self.assertEqual(scfg["lang"], "pt")
        self.assertEqual(scfg["rewrites"], [{"from": "http://127.0.0.1:4387/", "to": "https://lavish.example.com/"}])
        # running install again keeps the token
        self.cli("install")
        self.assertEqual(load(os.path.join(self.tmp, "cfg", "config.json"))["token"], cfg["token"])

    def test_remote_without_token_refuses(self):
        r = self.cli("install", "--server", self.base, ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("token", r.stderr)

    def test_link_rotate_and_status(self):
        self.install_remote("--public-url", "https://queue.example.com")
        link = self.cli("link").stdout.strip()
        self.assertEqual(link, "https://queue.example.com/%s/" % self.app.key)
        old = self.app.key
        out = self.cli("rotate-key").stdout
        self.assertNotEqual(self.app.key, old)
        self.assertIn(self.app.key, out)
        self.assertEqual(read(os.path.join(self.tmp, "cfg", "link.txt")).strip(),
                         "https://queue.example.com/%s/" % self.app.key)
        r = self.cli("status", ok=False)  # robot never synced -> exit 1 with a warning
        self.assertEqual(r.returncode, 1)
        self.assertIn("server: up", r.stdout)
        self.assertNotIn(self.token, r.stdout)


@unittest.skipUnless(HAS_OSA, "needs macOS osascript")
class RobotTests(Base):
    def fake(self, windows, **kw):
        d = dict(kw, windows=windows)
        with open(self.fake_path, "w") as f:
            json.dump(d, f)

    def chrome(self):
        with open(self.fake_path) as f:
            return json.load(f)

    def setUp(self):
        super().setUp()
        self.fake_path = os.path.join(self.tmp, "fake.json")
        self.install_remote()
        self.env["TQ_FAKE_CHROME"] = self.fake_path
        self.env["TQ_CONFIG_FILE"] = os.path.join(self.tmp, "cfg", "config.json")

    def robot(self, *args):
        r = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", os.path.join(ROOT, "robot", "robot.js")] + list(args),
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr + self.log())
        return r

    def log(self):
        p = os.path.join(self.tmp, "state", "robot.log")
        return read(p) if os.path.exists(p) else ""

    def tabs(self, wid):
        return [t["url"] for w in self.chrome()["windows"] if w["id"] == wid for t in w["tabs"]]

    def test_list_done_close_undo_reopen(self):
        park = "chrome-extension://x/park.html?title=Hi&url=" + urllib.request.quote(PAGE, safe="")
        self.fake([{"id": 1, "mode": "normal", "active": 1, "tabs": [{"id": 11, "url": YT, "title": "V - YouTube"},
                                                                     {"id": 12, "url": park, "title": "x"}]},
                   {"id": 2, "mode": "normal", "active": 1, "tabs": [{"id": 21, "url": "https://o.example/", "title": "o"}]}])
        self.robot("-v")
        d = self.phone("api/items")
        self.assertEqual(sorted(i["url"] for i in d["items"]), sorted([YT, PAGE]))
        self.assertEqual([i["title"] for i in d["items"] if i["url"] == PAGE], ["Hi"])
        self.assertTrue(self.phone("api/done", {"url": PAGE})["ok"])
        self.robot()  # waits out the 1 s Undo time inside the same run, then closes
        self.assertEqual(self.tabs(1), [YT])
        self.assertIn("close %s tabs [12] -> closed" % PAGE, self.log())
        self.assertEqual(self.phone("api/undo", {"url": PAGE})["result"], "reopening")
        self.robot()
        self.assertEqual(self.tabs(1), [YT, PAGE])
        self.assertEqual(self.tabs(2), ["https://o.example/"])  # other windows untouched
        self.assertIn(PAGE, [i["url"] for i in self.phone("api/items")["items"]])

    def test_link_from_phone_and_lavish_routing(self):
        self.fake([{"id": 1, "mode": "normal", "active": 1, "tabs": [{"id": 11, "url": YT, "title": "V"}]},
                   {"id": 2, "mode": "normal", "active": 1, "tabs": [{"id": 21, "url": LAV + "old", "title": "l"}]}])
        self.robot()  # first run: remembers the lavish tab already open, moves nothing
        self.assertEqual(self.tabs(2), [LAV + "old"])
        self.phone("api/add", {"url": PAGE})
        d = self.chrome()
        d["windows"][1]["tabs"].append({"id": 22, "url": LAV, "title": "new lavish"})
        dump(d, self.fake_path)
        self.robot()
        self.assertEqual(self.tabs(1), [YT, LAV, PAGE])
        self.assertEqual(self.tabs(2), [LAV + "old"])
        self.assertEqual(self.phone("api/items")["pending"], [])
        self.assertIn("(kept)", self.log())

    def test_wrong_address_is_not_closed(self):
        self.fake([{"id": 1, "mode": "normal", "active": 1, "tabs": [{"id": 12, "url": PAGE, "title": "p"}]}])
        self.robot()
        self.phone("api/done", {"url": PAGE})
        d = self.chrome()
        d["windows"][0]["tabs"][0]["url"] = "https://navigated.example/"  # same tab, other page now
        dump(d, self.fake_path)
        self.app.store.settings["undo_seconds"] = 0
        self.robot()
        self.assertEqual(self.tabs(1), ["https://navigated.example/"])

    def test_send_front(self):
        self.fake([{"id": 2, "mode": "normal", "active": 2, "tabs": [{"id": 21, "url": "https://keep.example/", "title": "k"},
                                                                     {"id": 22, "url": PAGE, "title": "p"}]},
                   {"id": 1, "mode": "normal", "active": 1, "tabs": [{"id": 11, "url": YT, "title": "V"},
                                                                     {"id": 13, "url": "https://b.example/", "title": "b"}]}])
        cfgp = os.path.join(self.tmp, "cfg", "config.json")
        cfg = load(cfgp)
        cfg["window"] = 1
        dump(cfg, cfgp)
        self.robot("send-front")
        self.assertEqual(self.tabs(1), [YT, "https://b.example/", PAGE])
        self.assertEqual(self.tabs(2), ["https://keep.example/"])

    def test_new_window_after_open_is_closed(self):
        # if Chrome ever answers an open with a new window, the robot closes it (rule: never leave a new window)
        self.fake([{"id": 1, "mode": "normal", "active": 1, "tabs": [{"id": 11, "url": YT, "title": "V"}]}],
                  make_creates_window=True)
        self.robot()
        self.phone("api/add", {"url": PAGE})
        self.robot()
        self.assertEqual([w["id"] for w in self.chrome()["windows"]], [1])
        self.assertEqual(self.tabs(1), [YT, PAGE])
        self.assertIn("a new Chrome window", self.log())

    def test_open_code_never_makes_windows(self):
        src = read(os.path.join(ROOT, "robot", "robot.js"))
        real = src[src.index("function realChrome"):src.index("function fakeChrome")]  # the part that talks to Chrome
        self.assertIn("if not (exists window id", real)
        for bad in ("make new window", "open location", "windows.push", ".Window("):
            self.assertFalse(bad in real, "robot.js real Chrome code contains %r" % bad)

    def test_chrome_just_started_does_nothing(self):
        self.fake([{"id": 1, "mode": "normal", "active": 1, "tabs": [{"id": 11, "url": YT, "title": "V"}]}], age=10)
        self.robot()
        self.assertEqual(self.phone("api/items")["items"], [])
        self.assertIn("waiting", self.log())


if __name__ == "__main__":
    unittest.main()
