"""TOTP codes: unit tests + a real enroll/login over HTTP."""
from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from crewhall.web import totp

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TotpUnitTest(unittest.TestCase):
    def test_secret_and_code(self):
        secret = totp.new_secret()
        self.assertTrue(secret)
        base64.b32decode(secret + "=" * (-len(secret) % 8))
        code = totp.code_at(secret)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_verify_accepts_current_and_skew_rejects_wrong(self):
        secret = totp.new_secret()
        now = time.time()
        self.assertIsNotNone(totp.verify(secret, totp.code_at(secret, now), 0))
        # previous step is accepted (clock drift)
        self.assertIsNotNone(totp.verify(secret, totp.code_at(secret, now - totp.PERIOD), 0))
        self.assertIsNone(totp.verify(secret, "000000" if totp.code_at(secret, now) != "000000" else "111111", 0))

    def test_replay_is_rejected(self):
        secret = totp.new_secret()
        step = totp.verify(secret, totp.code_at(secret), 0)
        self.assertIsNotNone(step)
        self.assertIsNone(totp.verify(secret, totp.code_at(secret), step))  # same step

    def test_otpauth_uri(self):
        uri = totp.otpauth_uri("ABCDEF", "phone")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=ABCDEF", uri)
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class TotpEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ati-totp-", dir="/tmp")
        cls.run_dir = os.path.join(cls.root, "run")
        os.makedirs(cls.run_dir, mode=0o700)
        cls.sock = os.path.join(cls.root, "d.sock")
        cls.port = _free_port()
        cls.env = {
            **os.environ,
            "XDG_CONFIG_HOME": os.path.join(cls.root, "config"),
            "XDG_STATE_HOME": os.path.join(cls.root, "state"),
            "XDG_RUNTIME_DIR": cls.run_dir,
            "CREWHALL_TMUX_SOCKET": f"at_totp_{os.getpid()}",
            "CREWHALL_HOOKS": "0", "CREWHALL_CONVERSATIONS": "0",
            "PYTHONPATH": REPO,
        }
        from crewhall.web import auth

        cls._saved = {k: os.environ.get(k) for k in
                      ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")}
        os.environ.update({k: cls.env[k] for k in cls._saved})
        cls.master = auth.generate_token(rotate=True)
        cls.daemon = subprocess.Popen(
            [sys.executable, "-P", "-m", "crewhall.daemon", "--foreground", "--socket", cls.sock],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert _wait(lambda: os.path.exists(cls.sock)), "daemon did not start"
        cls.web = subprocess.Popen(
            [sys.executable, "-P", "-m", "crewhall", "web", "--port", str(cls.port),
             "--socket", cls.sock, "--require-auth"],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert _wait(lambda: _port_open(cls.port)), "web did not start"

    @classmethod
    def tearDownClass(cls):
        for proc in (cls.web, cls.daemon):
            try:
                proc.terminate(); proc.wait(timeout=10)
            except Exception:
                try: proc.kill()
                except Exception: pass
        subprocess.run(["tmux", "-L", cls.env["CREWHALL_TMUX_SOCKET"], "kill-server"],
                       capture_output=True)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.root, ignore_errors=True)

    def _post(self, path, body, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json", "Origin": f"http://localhost:{self.port}"}
        if cookie:
            headers["Cookie"] = cookie
        conn.request("POST", path, json.dumps(body), headers)
        resp = conn.getresponse()
        raw = resp.read()
        sc = resp.getheader("Set-Cookie")
        conn.close()
        try:
            data = json.loads(raw.decode() or "{}")
        except ValueError:
            data = {}
        return resp.status, data, sc

    def test_enroll_and_login_with_a_code(self):
        status, _data, sc = self._post("/login", {"token": self.master})
        self.assertEqual(status, 200)
        cookie = sc.split(";", 1)[0]

        status, data, _ = self._post("/api/totp/begin", {"label": "test"}, cookie=cookie)
        self.assertEqual(status, 200, data)
        secret, ceremony = data["secret"], data["ceremony"]
        self.assertIn("secret=", data["uri"])

        code = totp.code_at(secret)
        status, data, _ = self._post("/api/totp/confirm",
                                     {"ceremony": ceremony, "code": code}, cookie=cookie)
        self.assertEqual(status, 200, data)

        # Login with the same step's code (confirm does not consume it).
        status, data, sc = self._post("/api/totp/login", {"code": code})
        self.assertEqual(status, 200, data)
        session = sc.split(";", 1)[0]
        status, data, _ = self._post("/api/op", {"op": "meta_info"}, cookie=session)
        self.assertEqual(status, 200)

        # Replaying the same code is rejected.
        status, _data, _ = self._post("/api/totp/login", {"code": code})
        self.assertEqual(status, 401)

    def test_login_page_offers_a_code_field(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/login")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        self.assertIn('id="c"', body)
        self.assertIn("code", body.lower())

    def test_wrong_code_is_rejected(self):
        status, _data, _ = self._post("/api/totp/login", {"code": "000000"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
