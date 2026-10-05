from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from crewhall import paths
from crewhall.frontends import FrontendError, FrontendManager
from crewhall.web import auth
from crewhall.web.server import ALLOWED_OPS

TS_IP = "127.0.0.2"  # a loopback alias stands in for the Tailscale address


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(host, port, path="/", headers=None, body=None, timeout=5):
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return None  # refused / unreachable


class FrontendBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="at-front-")
        cls._env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": cls.tmp, "XDG_CONFIG_HOME": cls.tmp, "XDG_STATE_HOME": cls.tmp})
        cls._env.start()

    @classmethod
    def tearDownClass(cls):
        try:
            from crewhall.client import Client

            Client(autostart=False).call("shutdown")
        except Exception:  # noqa: BLE001
            pass
        cls._env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        auth.revoke_token()  # deterministic: each test starts without a token
        self.port = free_port()
        self.state_file = os.path.join(self.tmp, f"frontends-{self.port}.json")
        self.mgr = FrontendManager(paths.socket_path(), state_file=self.state_file)
        self.mgr.port = self.port
        self.addCleanup(self.mgr.stop_all)
        ts = mock.patch.multiple("crewhall.frontends.tailscale", available=lambda: True,
                                 local_ipv4=lambda: TS_IP, dns_name=lambda: None)
        ts.start()
        self.addCleanup(ts.stop)


class Modes(FrontendBase):
    def test_off_local_tailscale_both_switch_without_restarting_anything(self):
        p = self.port
        self.assertEqual(self.mgr.status()["mode"], "off")
        self.assertIsNone(http("127.0.0.1", p))

        self.mgr.set_mode("local")
        self.assertEqual(http("127.0.0.1", p), 200)
        self.assertIsNone(http(TS_IP, p))                      # not reachable on the tailnet address

        out = self.mgr.set_mode("tailscale")
        self.assertIsNone(http("127.0.0.1", p), "local must be closed in tailscale mode")
        self.assertEqual(http(TS_IP, p), 200)                  # login page is served...
        self.assertEqual(http(TS_IP, p, "/api/op", {"Content-Type": "application/json"}, b'{"op":"ping"}'), 401)
        self.assertTrue(out["token_created"])                  # ...and the API requires the token
        self.assertTrue(auth.token_exists())

        self.mgr.set_mode("both")
        self.assertEqual(http("127.0.0.1", p), 200)
        self.assertEqual(http(TS_IP, p), 200)

        self.mgr.set_mode("off")
        self.assertIsNone(http("127.0.0.1", p))
        self.assertIsNone(http(TS_IP, p))
        self.assertEqual(self.mgr.status()["mode"], "off")

    def test_tailscale_mode_rejects_foreign_host_headers_and_accepts_the_token(self):
        self.mgr.set_mode("tailscale")
        token = auth.load_token()
        hdr = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        self.assertEqual(http(TS_IP, self.port, "/api/op", hdr, b'{"op":"ping"}'), 200)
        self.assertEqual(http(TS_IP, self.port, "/api/op", {**hdr, "Host": "evil.example"}, b'{"op":"ping"}'), 400)

    def test_disabling_drops_connections_that_are_already_open(self):
        self.mgr.set_mode("tailscale")
        conn = socket.create_connection((TS_IP, self.port), timeout=5)
        conn.sendall(f"GET / HTTP/1.1\r\nHost: {TS_IP}:{self.port}\r\nConnection: keep-alive\r\n\r\n".encode())
        self.assertTrue(conn.recv(4096).startswith(b"HTTP/1.1 200"))
        self.mgr.set_mode("local")                             # switch away from tailscale
        conn.settimeout(5)
        eof = False
        try:
            conn.sendall(f"GET / HTTP/1.1\r\nHost: {TS_IP}:{self.port}\r\n\r\n".encode())  # a 2nd request
        except OSError:
            eof = True                                         # already closed: fine
        seen = b""
        while not eof:
            try:
                chunk = conn.recv(65536)                       # first response's leftovers, then EOF
            except TimeoutError:
                self.fail("connection still open after the interface was disabled")
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
        self.assertNotIn(b"HTTP/1.1 200", seen, "a second request must not be served after disable")
        conn.close()

    def test_local_does_not_serve_the_internal_control_ops_to_web_clients(self):
        # Stopping the daemon stays a server-side action. (Switching the Web UI mode became a Settings
        # action in 0.47.0; the web layer refuses "off" so a browser cannot cut its own connection.)
        self.assertNotIn("shutdown", ALLOWED_OPS)

    def test_settings_panel_may_read_and_switch_the_web_ui_mode(self):
        for op in ("frontend_status", "frontend_set"):
            self.assertIn(op, ALLOWED_OPS)


class Failures(FrontendBase):
    def test_unknown_mode_and_bad_port_are_rejected(self):
        with self.assertRaises(FrontendError):
            self.mgr.set_mode("everywhere")
        with self.assertRaises(FrontendError):
            self.mgr.set_mode("local", port=70000)

    def test_tailscale_unavailable_fails_cleanly_and_changes_nothing(self):
        self.mgr.set_mode("local")
        with mock.patch("crewhall.frontends.tailscale.available", lambda: False):
            with self.assertRaises(FrontendError) as ctx:
                self.mgr.set_mode("tailscale")
        self.assertIn("Tailscale", str(ctx.exception))
        # local was disabled-first by design of "switch", but nothing half-started remains
        self.assertIsNone(http(TS_IP, self.port))

    def test_port_in_use_reports_a_clear_error_and_persists_reality(self):
        with socket.socket() as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            blocker.bind(("127.0.0.1", self.port))
            blocker.listen(1)
            with self.assertRaises(FrontendError) as ctx:
                self.mgr.set_mode("local")
        self.assertIn("cannot listen", str(ctx.exception))
        self.assertEqual(self.mgr.status()["mode"], "off")
        self.assertEqual(json.load(open(self.state_file))["mode"], "off")


class Persistence(FrontendBase):
    def test_mode_and_port_survive_a_restart(self):
        self.mgr.set_mode("local")
        data = json.load(open(self.state_file))
        self.assertEqual(data, {"mode": "local", "port": self.port})
        self.assertEqual(os.stat(self.state_file).st_mode & 0o777, 0o600)
        self.mgr.stop_all()                                    # "daemon restarts"
        self.assertIsNone(http("127.0.0.1", self.port))
        fresh = FrontendManager(paths.socket_path(), state_file=self.state_file)
        self.addCleanup(fresh.stop_all)
        fresh.restore()
        self.assertEqual(fresh.status()["mode"], "local")
        self.assertEqual(http("127.0.0.1", self.port), 200)

    def test_restore_never_raises_and_defaults_to_off(self):
        fresh = FrontendManager(paths.socket_path(), state_file=os.path.join(self.tmp, "nope.json"))
        fresh.restore()
        self.assertEqual(fresh.status()["mode"], "off")
        open(self.state_file, "w").write("{broken json")
        broken = FrontendManager(paths.socket_path(), state_file=self.state_file)
        broken.restore()
        self.assertEqual(broken.status()["mode"], "off")
        json.dump({"mode": "tailscale", "port": self.port}, open(self.state_file, "w"))
        with mock.patch("crewhall.frontends.tailscale.available", lambda: False), \
                mock.patch("crewhall.frontends.RESTORE_ATTEMPTS", 1):
            bad = FrontendManager(paths.socket_path(), state_file=self.state_file)
            bad.restore()                                       # tailnet down at boot: daemon must still come up
        self.assertEqual(bad.status()["mode"], "off")
        self.assertIn("Tailscale", bad.status()["frontends"]["tailscale"]["error"])


if __name__ == "__main__":
    unittest.main()


class SavedModeIsIntentNotStatus(FrontendBase):
    def test_a_failed_restore_never_rewrites_the_saved_mode_and_retries_until_it_works(self):
        # Regression: boot before Tailscale is up (or a busy port) used to overwrite the saved mode with "off".
        json.dump({"mode": "tailscale", "port": self.port}, open(self.state_file, "w"))
        available = {"up": False}
        fresh = FrontendManager(paths.socket_path(), state_file=self.state_file)
        self.addCleanup(fresh.stop_all)
        with mock.patch("crewhall.frontends.RESTORE_RETRY_EVERY", 0.2), \
                mock.patch("crewhall.frontends.tailscale.available", lambda: available["up"]):
            fresh.begin_restore()
            import threading
            threading.Thread(target=fresh.restore, daemon=True).start()
            st = fresh.status()                                         # answered once the 1st attempt failed
            self.assertEqual(st["mode"], "off")
            self.assertIn("Tailscale", st["frontends"]["tailscale"]["error"])
            self.assertEqual(json.load(open(self.state_file))["mode"], "tailscale")   # intent preserved
            available["up"] = True                                      # the tailnet comes up later…
            import time
            deadline = time.monotonic() + 8
            while fresh.status()["mode"] != "tailscale" and time.monotonic() < deadline:
                time.sleep(0.1)
        self.assertEqual(fresh.status()["mode"], "tailscale")
        self.assertEqual(http(TS_IP, self.port), 200)
        self.assertEqual(json.load(open(self.state_file))["mode"], "tailscale")

    def test_shutting_down_cancels_the_retry_loop(self):
        json.dump({"mode": "tailscale", "port": self.port}, open(self.state_file, "w"))
        fresh = FrontendManager(paths.socket_path(), state_file=self.state_file)
        with mock.patch("crewhall.frontends.RESTORE_RETRY_EVERY", 30), \
                mock.patch("crewhall.frontends.tailscale.available", lambda: False):
            import threading
            t = threading.Thread(target=fresh.restore, daemon=True)
            t.start()
            fresh.stop_all()
            t.join(timeout=5)
        self.assertFalse(t.is_alive())

    def test_the_test_suite_cannot_touch_the_real_user_state(self):
        self.assertTrue(os.environ.get("AT_TEST_ISOLATED"))
        self.assertNotEqual(os.path.dirname(paths.state_dir()), os.path.expanduser("~/.local/state"))
