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

from .support import HardTimeoutMixin

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
class TerminalPageBrowser(HardTimeoutMixin, unittest.TestCase):
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

    def _open_page(self, page, term_id, ticket):
        """Open /terminal.html with a test-only handle to the xterm/ws objects."""
        script = os.path.join(REPO, "crewhall", "web", "static", "js", "terminal_page.js")

        def serve(route):
            with open(script, encoding="utf-8") as fh:
                body = fh.read()
            body = body.replace("const term = new Terminal(",
                                "const term = window.__term = new Terminal(")
            body = body.replace("ws = new WebSocket(url);",
                                "ws = window.__termWs = new WebSocket(url);")
            route.fulfill(status=200, content_type="text/javascript", body=body)

        page.route("**/static/js/terminal_page.js", serve)
        page.goto(f"http://127.0.0.1:{self.port}/terminal.html?id={term_id}&ticket={ticket}")
        page.wait_for_selector(".xterm", timeout=10000)

    @staticmethod
    def _grid(page):
        return page.evaluate("() => ({cols: window.__term.cols, rows: window.__term.rows})")

    def _tmux_size(self, term_id):
        for _ in range(3):
            proc = subprocess.run(
                ["tmux", "-L", self.tmux_socket, "display", "-p", "-t", term_id,
                 "#{window_width}x#{window_height}"],
                capture_output=True, text=True)
            if proc.returncode == 0:
                return proc.stdout.strip()
            time.sleep(0.1)
        return ""

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
        self._install_test_timeout()
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

    def test_read_client_never_resizes_and_claim_applies_its_own_size(self):
        cookie = self._login()
        self._unlock_write(cookie)
        term_id = self.term["session_id"]
        ticket_a = self._ticket(cookie, term_id, "write")
        ticket_b = self._ticket(cookie, term_id, "read")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            try:
                def context(viewport):
                    ctx = browser.new_context(viewport=viewport)
                    ctx.add_cookies([{"name": "at_session",
                                      "value": cookie.split("=", 1)[1],
                                      "url": f"http://127.0.0.1:{self.port}"}])
                    return ctx

                desktop = context({"width": 1200, "height": 700})
                a = desktop.new_page()
                self._open_page(a, term_id, ticket_a)
                a.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('keyboard')",
                    timeout=10000)
                a_grid = self._grid(a)
                a_size = f"{a_grid['cols']}x{a_grid['rows']}"
                self.assertTrue(_wait(lambda: self._tmux_size(term_id) == a_size),
                                f"writer did not apply its size: {self._tmux_size(term_id)} != {a_size}")

                mobile = context({"width": 390, "height": 844})
                b = mobile.new_page()
                self._open_page(b, term_id, ticket_b)
                b.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('read-only')",
                    timeout=10000)
                b_grid = self._grid(b)
                b_size = f"{b_grid['cols']}x{b_grid['rows']}"
                self.assertNotEqual(b_size, a_size)
                # A read client fits its own grid but must not resize tmux.
                self.assertEqual(self._tmux_size(term_id), a_size)
                b.set_viewport_size({"width": 500, "height": 700})
                b.wait_for_timeout(300)
                self.assertEqual(self._tmux_size(term_id), a_size)
                # Even a raw 0x03 from a read client must be ignored by the server.
                b.evaluate("window.__termWs.send(new Uint8Array([0x03, 0, 60, 0, 20]))")
                b.wait_for_timeout(300)
                self.assertEqual(self._tmux_size(term_id), a_size)
                b_grid = self._grid(b)
                b_size = f"{b_grid['cols']}x{b_grid['rows']}"

                b.click("#term-claim")
                b.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('keyboard')",
                    timeout=10000)
                self.assertTrue(
                    _wait(lambda: self._tmux_size(term_id) == b_size),
                    f"claiming the keyboard did not apply the reader's size: "
                    f"{self._tmux_size(term_id)} != {b_size}")
                desktop.close()
                mobile.close()
            finally:
                browser.close()

    def test_reconnect_gets_a_fresh_ticket_and_restores_its_size(self):
        cookie = self._login()
        self._unlock_write(cookie)
        term_id = self.term["session_id"]
        ticket = self._ticket(cookie, term_id, "write")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
            try:
                context = browser.new_context(viewport={"width": 1200, "height": 700})
                context.add_cookies([{"name": "at_session",
                                      "value": cookie.split("=", 1)[1],
                                      "url": f"http://127.0.0.1:{self.port}"}])
                page = context.new_page()
                self._open_page(page, term_id, ticket)
                page.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('keyboard')",
                    timeout=10000)
                first = self._grid(page)

                # Drop the socket and resize the window while it reconnects.
                page.evaluate("window.__termWs.close()")
                page.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('reconnecting')",
                    timeout=5000)
                page.set_viewport_size({"width": 800, "height": 500})
                page.wait_for_function(
                    "() => document.getElementById('term-conn-state').textContent.includes('keyboard')",
                    timeout=15000)
                second = self._grid(page)
                self.assertNotEqual((first["cols"], first["rows"]),
                                    (second["cols"], second["rows"]))
                expected = f"{second['cols']}x{second['rows']}"
                self.assertTrue(_wait(lambda: self._tmux_size(term_id) == expected),
                                f"reconnect did not restore its size: "
                                f"{self._tmux_size(term_id)} != {expected}")
                context.close()
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
