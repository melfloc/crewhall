"""Browser test for the xterm terminal page against a real daemon + web server."""
from __future__ import annotations

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

try:  # pragma: no cover - optional dependency
    from playwright.sync_api import sync_playwright
except Exception:  # noqa: BLE001
    sync_playwright = None

CHROMIUM = (shutil.which("chromium") or shutil.which("chromium-browser")
            or shutil.which("google-chrome"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


@unittest.skipUnless(sync_playwright and CHROMIUM, "needs playwright + chromium")
class TerminalPageBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ati-termweb-", dir="/tmp")
        cls.run_dir = os.path.join(cls.root, "run")
        os.makedirs(cls.run_dir, mode=0o700)
        cls.sock = os.path.join(cls.root, "d.sock")
        cls.tmux_socket = f"at_termweb_{os.getpid()}"
        cls.port = _free_port()
        cls.env = {
            **os.environ,
            "XDG_CONFIG_HOME": os.path.join(cls.root, "config"),
            "XDG_STATE_HOME": os.path.join(cls.root, "state"),
            "XDG_RUNTIME_DIR": cls.run_dir,
            "CREWHALL_TMUX_SOCKET": cls.tmux_socket,
            "CREWHALL_HOOKS": "0",
            "CREWHALL_CONVERSATIONS": "0",
            "PYTHONPATH": REPO,
        }
        cls.daemon = subprocess.Popen(
            [sys.executable, "-P", "-m", "crewhall.daemon", "--foreground", "--socket", cls.sock],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        assert _wait(lambda: os.path.exists(cls.sock)), "daemon did not start"
        # Keep the isolated XDG for the whole class so token files are shared
        # with the daemon/web subprocesses (restored in tearDownClass).
        cls._saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME",
                                                     "XDG_RUNTIME_DIR")}
        os.environ.update({"XDG_CONFIG_HOME": cls.env["XDG_CONFIG_HOME"],
                           "XDG_STATE_HOME": cls.env["XDG_STATE_HOME"],
                           "XDG_RUNTIME_DIR": cls.env["XDG_RUNTIME_DIR"]})
        from crewhall import settings
        from crewhall.web import auth

        settings.patch({"terminals.enabled": True}, confirm=True)
        cls.master = auth.generate_token(rotate=True)
        cls.web = subprocess.Popen(
            [sys.executable, "-P", "-m", "crewhall", "web", "--port", str(cls.port),
             "--socket", cls.sock, "--require-auth"],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        assert _wait(lambda: _port_open(cls.port)), "web did not start"

    # -- HTTP helpers ------------------------------------------------------
    def _post(self, path, body, cookie=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        conn.request("POST", path, json.dumps(body).encode(), headers)
        resp = conn.getresponse()
        raw = resp.read()
        sc = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, (json.loads(raw.decode() or "{}") if raw else {}), sc

    def _origin(self):
        return f"http://127.0.0.1:{self.port}"

    def _login(self):
        status, _data, sc = self._post("/login", {"token": self.master}, origin=self._origin())
        assert status == 200
        return sc.split(";", 1)[0]

    def _unlock_write(self, cookie):
        from crewhall.web import terminal_tokens

        token, _ = terminal_tokens.issue("browser", scope="write", hosts=["*"], ttl=3600)
        self._post("/api/terminal-unlock", {"token": token}, cookie=cookie, origin=self._origin())

    def _ticket(self, cookie, term_id, mode):
        _s, data, _ = self._post("/api/terminal-ticket", {"id": term_id, "mode": mode},
                                 cookie=cookie, origin=self._origin())
        return data.get("ticket", "")

    @classmethod
    def tearDownClass(cls):
        for proc in (cls.web, cls.daemon):
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        subprocess.run(["tmux", "-L", cls.tmux_socket, "kill-server"], capture_output=True)
        for k, v in getattr(cls, "_saved", {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        from crewhall.client import Client

        client = Client(socket_path=self.sock, autostart=False)
        self.client = client
        self.term = client.call("terminal_create", cwd="/tmp")["terminal"]

    def tearDown(self):
        try:
            self.client.call("terminal_close", id=self.term["session_id"])
        except Exception:
            pass

    def test_type_echo_and_claim(self):
        cookie = self._login()
        self._unlock_write(cookie)
        term_id = self.term["session_id"]
        ticket_a = self._ticket(cookie, term_id, "write")
        ticket_b = self._ticket(cookie, term_id, "read")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            try:
                context = browser.new_context(viewport={"width": 1200, "height": 700})
                context.add_cookies([{"name": "at_session",
                                      "value": cookie.split("=", 1)[1],
                                      "url": f"http://127.0.0.1:{self.port}"}])
                a = context.new_page()
                a.goto(f"http://127.0.0.1:{self.port}/terminal.html?id={term_id}&ticket={ticket_a}")
                a.wait_for_selector(".xterm", timeout=10000)
                a.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('keyboard')",
                    timeout=10000)
                a.click("#xterm")
                a.keyboard.type("echo hola")
                a.keyboard.press("Enter")
                a.wait_for_function(
                    "() => document.querySelector('.xterm-rows').textContent.includes('hola')",
                    timeout=10000)
                # A second connection is read-only and can claim the keyboard.
                b = context.new_page()
                b.goto(f"http://127.0.0.1:{self.port}/terminal.html?id={term_id}&ticket={ticket_b}")
                b.wait_for_selector(".xterm", timeout=10000)
                b.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('read-only')",
                    timeout=10000)
                b.click("#term-claim")
                b.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('keyboard')",
                    timeout=10000)
            finally:
                browser.close()


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
