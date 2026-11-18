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

    def load_cli(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("tq_cli", os.path.join(ROOT, "bin", "tab-queue"))
        spec = importlib.util.spec_from_loader("tq_cli", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def test_local_link_follows_port_and_listen_of_the_last_install(self):
        mod = self.load_cli()
        self.cli("install", "--port", "9100")
        cfg = load(os.path.join(self.tmp, "cfg", "config.json"))
        self.assertNotIn("public_url", cfg)  # nothing derived is frozen into the config
        self.assertEqual(mod.page_link(cfg, {"key": "K"}), "http://127.0.0.1:9100/K/")
        self.cli("install", "--port", "9200", "--listen", "0.0.0.0")
        cfg = load(os.path.join(self.tmp, "cfg", "config.json"))
        self.assertNotIn("public_url", cfg)
        link = mod.page_link(cfg, {"key": "K"})
        self.assertRegex(link, r"^http://[^/:]+:9200/K/$")
        self.assertNotIn("127.0.0.1", link)
        self.assertNotIn("0.0.0.0", link)
        self.cli("install")  # install again with no options: settings of the last install stay
        cfg = load(os.path.join(self.tmp, "cfg", "config.json"))
        self.assertEqual((cfg["port"], cfg["listen"]), (9200, "0.0.0.0"))
        self.cli("install", "--public-url", "https://q.example.com/")
        cfg = load(os.path.join(self.tmp, "cfg", "config.json"))
        self.assertEqual(mod.page_link(cfg, {"key": "K"}), "https://q.example.com/K/")
        self.assertEqual(mod.page_link(cfg, {"key": "K", "key_in_link": False}), "https://q.example.com/")

    def test_launchctl_load_waits_for_the_job_to_leave(self):
        from unittest import mock
        mod = self.load_cli()
        calls, state = [], {"loaded": 3, "boot_fail": 1}

        def fake_run(cmd, **kw):
            calls.append(cmd[1])
            r = mock.Mock(returncode=0, stdout="", stderr="")
            if cmd[1] == "print":
                if state["loaded"] > 0:
                    state["loaded"] -= 1
                else:
                    r.returncode = 113
            elif cmd[1] == "bootstrap":
                if state["loaded"] > 0:
                    self.fail("bootstrap while the old job was still loaded")
                if state["boot_fail"]:
                    state["boot_fail"] -= 1
                    r.returncode, r.stderr = 5, "Bootstrap failed: 5: Input/output error"
            return r
        with mock.patch.object(mod, "NO_LAUNCHCTL", False), mock.patch.object(mod.subprocess, "run", fake_run), \
                mock.patch.object(mod.time, "sleep"):
            mod.launchctl_load("local.x", "/tmp/x.plist")
        self.assertEqual(calls[0], "bootout")
        self.assertEqual(calls.count("bootstrap"), 2)  # one failure, then it works

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

    def win(self, wid, *tabs, **kw):
        return dict({"id": wid, "mode": "normal", "active": 1,
                     "tabs": [{"id": wid * 10 + k, "url": u, "title": u} for k, u in enumerate(tabs)]}, **kw)

    def test_foreign_new_window_during_open_is_left_alone(self):
        # another automation opens windows in this Chrome: only a blank window made by OUR make may be closed
        self.fake([self.win(1, YT, "https://b.example/")], foreign_window="https://x.com/")
        self.robot()
        self.phone("api/add", {"url": PAGE})
        self.robot()
        self.assertEqual(self.tabs(1), [YT, "https://b.example/", PAGE])
        self.assertEqual([w["id"] for w in self.chrome()["windows"]], [1, 9601])
        self.assertIn("not ours", self.log())

    def test_window_that_existed_before_the_open_is_never_closed(self):
        self.fake([self.win(1, YT, "https://b.example/", "https://c.example/"), self.win(2, "about:blank"), self.win(3, PAGE + "2")])
        self.robot()
        self.phone("api/add", {"url": PAGE})
        self.robot()
        self.assertEqual(self.tabs(1)[-1], PAGE)
        self.assertEqual(self.tabs(2), ["about:blank"])
        self.assertEqual(self.tabs(3), [PAGE + "2"])

    def test_restart_does_not_move_lavish_tabs_of_other_windows(self):
        self.fake([self.win(1, YT, "https://b.example/", "https://c.example/"), self.win(2, LAV + "old")], age=3600)
        self.robot()
        # Chrome restarted: every window and tab has a new id, the same pages are open
        self.fake([self.win(7, YT, "https://b.example/", "https://c.example/"), self.win(8, LAV + "old")], age=200)
        self.robot()
        self.assertEqual(self.tabs(8), [LAV + "old"])
        self.assertEqual(self.tabs(7), [YT, "https://b.example/", "https://c.example/"])
        self.assertIn("moving nothing", self.log())
        # the run after that is normal again: a lavish page that opens now does move
        d = self.chrome()
        d["windows"][1]["tabs"].append({"id": 999, "url": LAV + "new", "title": "n"})
        dump(d, self.fake_path)
        self.robot()
        self.assertEqual(self.tabs(7)[-1], LAV + "new")
        self.assertEqual(self.tabs(8), [LAV + "old"])

    def set_open_method(self, m):
        cfgp = os.path.join(self.tmp, "cfg", "config.json")
        cfg = load(cfgp)
        cfg["open_method"] = m
        dump(cfg, cfgp)

    def test_open_g_does_not_close_a_tab_the_user_already_had(self):
        self.set_open_method("open-g")
        mine = self.win(2, PAGE)
        self.fake([mine, self.win(1, YT, "https://b.example/", "https://c.example/")], open_g_noop=True)
        self.robot()
        self.phone("api/add", {"url": PAGE})
        self.robot()
        self.assertEqual(self.tabs(2), [PAGE])  # the user's own tab is still there
        self.assertNotIn(PAGE, self.tabs(1))
        self.assertIn("NOT FOUND", self.log())

    def test_open_g_adopts_the_tab_it_made(self):
        self.set_open_method("open-g")
        self.fake([self.win(2, "https://keep.example/"), self.win(1, YT, "https://b.example/", "https://c.example/")])
        self.robot()
        self.phone("api/add", {"url": PAGE})
        self.robot()
        self.assertEqual(self.tabs(1)[-1], PAGE)
        self.assertEqual(self.tabs(2), ["https://keep.example/"])

    def test_open_reads_all_windows_only_twice(self):
        self.fake([self.win(1, YT, "https://b.example/"), self.win(2, "https://o.example/")])
        self.robot()
        self.phone("api/add", {"url": PAGE})
        d = self.chrome()
        d["reads_all"] = d["reads_q"] = 0
        dump(d, self.fake_path)
        self.robot()
        d = self.chrome()
        self.assertEqual(self.tabs(1)[-1], PAGE)
        self.assertLessEqual(d["reads_all"], 2, d)  # choosing the window + looking at the other windows
        self.assertGreaterEqual(d["reads_q"], 1)

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
