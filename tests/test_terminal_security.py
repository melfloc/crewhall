"""Negative security tests for web terminals (Phase 3).

Each test asserts the *effect* (no execution / rejected / dropped), not just an
HTTP status code. A real isolated daemon + web server (auth required) is used.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from crewhall.web import ws as wsmod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = "/tmp/ati-termsec-pwn"


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


class WSClient:
    """A tiny masked-frame WebSocket client for the tests."""

    def __init__(self, port: int, path: str, *, origin: str | None, cookie: str | None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket",
            "Connection: Upgrade", f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if origin is not None:
            lines.append(f"Origin: {origin}")
        if cookie:
            lines.append(f"Cookie: {cookie}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.status = self._read_status()

    def _read_status(self) -> int:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        self._buf = bytearray(data.split(b"\r\n\r\n", 1)[1]) if b"\r\n\r\n" in data else bytearray()
        try:
            return int(data.split(b" ", 2)[1])
        except (IndexError, ValueError):
            return 0

    def send(self, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x82])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _parse_buffered(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        opcode = buf[0] & 0x0F
        length = buf[1] & 0x7F
        idx = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = struct.unpack(">H", buf[2:4])[0]
            idx = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = struct.unpack(">Q", buf[2:10])[0]
            idx = 10
        if len(buf) < idx + length:
            return None
        payload = bytes(buf[idx:idx + length])
        del self._buf[: idx + length]
        return wsmod.Frame(fin=True, opcode=opcode, masked=False, payload=payload,
                           frame_len=idx + length)

    def recv(self, timeout: float = 3.0):
        """Return a Frame, None on EOF, and raise TimeoutError on no data."""
        frame = self._parse_buffered()
        if frame is not None:
            return frame
        self.sock.settimeout(timeout)
        try:
            chunk = self.sock.recv(65536)
        except TimeoutError:
            raise
        except OSError:
            return None
        if not chunk:
            return None
        self._buf += chunk
        return self._parse_buffered()

    def recv_until(self, predicate, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = self.recv(timeout=0.5)
            except TimeoutError:
                continue
            if frame is not None and predicate(frame):
                return frame
        return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ati-termsec-", dir="/tmp")
        cls.run_dir = os.path.join(cls.root, "run")
        os.makedirs(cls.run_dir, mode=0o700)
        cls.sock = os.path.join(cls.root, "d.sock")
        cls.tmux_socket = f"at_termsec_{os.getpid()}"
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
        cls._saved = {k: os.environ.get(k) for k in cls.env
                      if k in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
                               "CREWHALL_TMUX_SOCKET")}
        os.environ.update({k: cls.env[k] for k in cls._saved})
        from crewhall import settings
        from crewhall.web import auth

        settings.patch({"terminals.enabled": True}, confirm=True)
        cls.master = auth.generate_token(rotate=True)
        cls.daemon_log = open(os.path.join(cls.root, "daemon.log"), "wb")
        cls.daemon = subprocess.Popen(
            [sys.executable, "-P", "-m", "crewhall.daemon", "--foreground", "--socket", cls.sock],
            env=cls.env, stdout=cls.daemon_log, stderr=cls.daemon_log,
        )
        assert _wait(lambda: os.path.exists(cls.sock)), "daemon did not start"
        cls.web = subprocess.Popen(
            [sys.executable, "-P", "-m", "crewhall", "web", "--port", str(cls.port),
             "--socket", cls.sock, "--require-auth", "--allow-host", "example.com"],
            env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        assert _wait(lambda: _port_open(cls.port)), "web did not start"

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
        try:
            cls.daemon_log.close()
        except Exception:
            pass
        subprocess.run(["tmux", "-L", cls.tmux_socket, "kill-server"], capture_output=True)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.root, ignore_errors=True)

    # -- HTTP helpers ------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None,
                 cookie: str | None = None, origin: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, json.dumps(body or {}).encode(), headers)
        resp = conn.getresponse()
        raw = resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        conn.close()
        try:
            data = json.loads(raw.decode() or "{}")
        except ValueError:
            data = {}
        return resp.status, data, set_cookie

    def _cookie(self) -> str:
        status, _data, sc = self._request("POST", "/login", {"token": self.master},
                                          origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200)
        return sc.split(";", 1)[0]

    def _unlock(self, cookie: str, token: str):
        return self._request("POST", "/api/terminal-unlock", {"token": token}, cookie=cookie,
                             origin=f"http://127.0.0.1:{self.port}")

    def _ticket(self, cookie: str, terminal_id: str, mode: str = "write",
                origin: str | None = None):
        return self._request("POST", "/api/terminal-ticket", {"id": terminal_id, "mode": mode},
                             cookie=cookie,
                             origin=origin or f"http://127.0.0.1:{self.port}")

    def _op(self, cookie: str, op: str, **params):
        return self._request("POST", "/api/op", {"op": op, **params}, cookie=cookie,
                             origin=f"http://127.0.0.1:{self.port}")

    def _issue(self, scope="write", hosts=None, ttl=3600):
        from crewhall.web import terminal_tokens

        token, rec = terminal_tokens.issue("test", scope=scope, hosts=hosts, ttl=ttl)
        return token, rec

    def _new_terminal(self, cookie, **params):
        status, data, _ = self._op(cookie, "terminal_create", **params)
        self.assertEqual(status, 200, data)
        return data["terminal"]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class TerminalSecurity(_Harness):
    def tearDown(self):
        if os.path.exists(MARK):
            os.unlink(MARK)

    def test_master_session_without_unlock_is_forbidden(self):
        cookie = self._cookie()
        for op, params in (
            ("terminal_list", {}),
            ("terminal_info", {"id": "term_deadbeef"}),
            ("terminal_create", {}),
            ("terminal_write", {"id": "term_deadbeef", "text": "x"}),
            ("terminal_key", {"id": "term_deadbeef", "key": "ENTER"}),
            ("terminal_capture", {"id": "term_deadbeef"}),
            ("terminal_resize", {"id": "term_deadbeef", "cols": 80, "rows": 24}),
            ("terminal_close", {"id": "term_deadbeef"}),
        ):
            status, _, _ = self._op(cookie, op, **params)
            self.assertEqual(status, 403, op)
        status, _, _ = self._ticket(cookie, "term_deadbeef")
        self.assertEqual(status, 403)

    def test_read_scope_cannot_write(self):
        token, _ = self._issue(scope="read")
        cookie = self._cookie()
        self.assertEqual(self._unlock(cookie, token)[0], 200)
        status, _, _ = self._op(cookie, "terminal_create")
        self.assertEqual(status, 403)
        status, _, _ = self._ticket(cookie, "term_deadbeef", mode="write")
        self.assertEqual(status, 403)

    def test_write_token_creates_and_lists(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        self.assertEqual(term["kind"], "terminal")
        self.assertTrue(term["owner"].startswith("tt_"))
        status, data, _ = self._op(cookie, "terminal_list")
        self.assertEqual(status, 200)
        self.assertTrue(any(t["session_id"] == term["session_id"] for t in data["terminals"]))
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_host_restricted_token_cannot_open_local(self):
        token, _ = self._issue(scope="write", hosts=["prod1"])
        cookie = self._cookie()
        self._unlock(cookie, token)
        status, _, _ = self._op(cookie, "terminal_create", cwd="/tmp")
        self.assertEqual(status, 403)

    def test_client_underscore_fields_are_discarded(self):
        token, rec = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        status, data, _ = self._request(
            "POST", "/api/op",
            {"op": "terminal_create", "cwd": "/tmp", "_actor": "pwned", "_foo": "bar"},
            cookie=cookie, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200, data)
        from crewhall import audit

        for entry in audit.recent(limit=20):
            if entry.get("op") == "terminal_create":
                self.assertNotEqual(entry.get("actor"), "pwned")
        self._op(cookie, "terminal_close", id=data["terminal"]["session_id"])

    def test_injection_values_never_execute(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        payload = f"$(touch {MARK})"
        for params in (
            {"shell": f"bash; touch {MARK}"},
            {"host": "-oProxyCommand=touch"},
            {"cwd": payload},
            {"title": payload},
            {"cwd": "../etc"},
            {"title": "with\nnewline"},
        ):
            status, _, _ = self._op(cookie, "terminal_create", **params)
            self.assertNotEqual(status, 200, params)
        # Malicious ids are rejected before touching tmux.
        for op, extra in (("terminal_write", {"text": "x"}),
                          ("terminal_key", {"key": "ENTER"}),
                          ("terminal_close", {})):
            status, _, _ = self._op(cookie, op, id=payload, **extra)
            self.assertNotEqual(status, 200, op)
        self.assertFalse(os.path.exists(MARK))
        # `command` is literal text sent to the terminal's own shell (by design,
        # not interpreted by the daemon): a benign command is accepted as-is.
        term = self._new_terminal(cookie, cwd="/tmp", command="echo LITERAL")
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_ws_missing_origin_rejected(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        status, data, _ = self._ticket(cookie, term["session_id"])
        ws = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={data['ticket']}",
                      origin=None, cookie=cookie)
        self.assertNotEqual(ws.status, 101)
        ws.close()
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_ws_foreign_origin_rejected(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        status, data, _ = self._ticket(cookie, term["session_id"])
        ws = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={data['ticket']}",
                      origin="http://evil.example", cookie=cookie)
        self.assertNotEqual(ws.status, 101)
        ws.close()
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_ticket_single_use_and_binding(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        status, data, _ = self._ticket(cookie, term["session_id"])
        ticket = data["ticket"]
        ok = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={ticket}",
                      origin=f"http://127.0.0.1:{self.port}", cookie=cookie)
        self.assertEqual(ok.status, 101)
        ok.close()
        # Reused ticket is rejected.
        reused = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={ticket}",
                          origin=f"http://127.0.0.1:{self.port}", cookie=cookie)
        self.assertNotEqual(reused.status, 101)
        reused.close()
        # A ticket bound to another terminal is rejected.
        status, data2, _ = self._ticket(cookie, term["session_id"])
        other = WSClient(self.port, f"/ws/terminal/term_deadbeef?ticket={data2['ticket']}",
                         origin=f"http://127.0.0.1:{self.port}", cookie=cookie)
        self.assertNotEqual(other.status, 101)
        other.close()
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_oversize_message_closes_1009(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        status, data, _ = self._ticket(cookie, term["session_id"])
        ws = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={data['ticket']}",
                      origin=f"http://127.0.0.1:{self.port}", cookie=cookie)
        self.assertEqual(ws.status, 101)
        from crewhall import settings

        big = settings.get("terminals.max_message_bytes") + 10
        ws.send(bytes([0x01]) + b"x" * big)
        frame = ws.recv_until(lambda f: f.opcode == wsmod.OP_CLOSE, timeout=5)
        self.assertIsNotNone(frame)
        code = struct.unpack(">H", frame.payload[:2])[0] if len(frame.payload) >= 2 else 0
        self.assertEqual(code, wsmod.CLOSE_TOO_BIG)
        ws.close()
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_readonly_ignores_input(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp", readonly=True)
        status, _, _ = self._op(cookie, "terminal_write", id=term["session_id"], text="x")
        self.assertNotEqual(status, 200)
        status, _, _ = self._op(cookie, "terminal_key", id=term["session_id"], key="ENTER")
        self.assertNotEqual(status, 200)
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_disabled_feature_is_404(self):
        from crewhall import settings

        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        settings.patch({"terminals.enabled": False})
        try:
            for op in ("terminal_list", "terminal_create"):
                status, _, _ = self._op(cookie, op)
                self.assertEqual(status, 404, op)
            status, _, _ = self._ticket(cookie, "term_deadbeef")
            self.assertEqual(status, 404)
        finally:
            settings.patch({"terminals.enabled": True}, confirm=True)

    def test_audit_is_metadata_only_and_files_private(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        secret = "S3CR3T_" + base64.b64encode(os.urandom(6)).decode()
        self._op(cookie, "terminal_write", id=term["session_id"], text=secret, enter=True)
        time.sleep(0.5)
        from crewhall import audit
        from crewhall.web import terminal_tokens

        audit_path = audit.audit_path()
        with open(audit_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        self.assertNotIn(secret, content)
        self.assertTrue(audit.file_permissions_ok())
        store = terminal_tokens.store_path()
        self.assertTrue(terminal_tokens.file_permissions_ok())
        with open(store, encoding="utf-8") as fh:
            stored = fh.read()
        self.assertNotIn(token, stored)  # never the token in clear
        # The daemon log must not contain the secret either.
        with open(os.path.join(self.root, "daemon.log"), "rb") as fh:
            log = fh.read().decode("utf-8", "replace")
        self.assertNotIn(secret, log)
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_token_is_not_stored_in_clear(self):
        from crewhall.web import terminal_tokens

        token, rec = self._issue(scope="write")
        with open(terminal_tokens.store_path(), encoding="utf-8") as fh:
            stored = fh.read()
        self.assertNotIn(token, stored)
        self.assertIn("hash", stored)

    def test_ticket_origin_binding(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        status, data, _ = self._ticket(cookie, term["session_id"])
        # example.com is an allowed origin, but the ticket was bound to 127.0.0.1.
        ws = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={data['ticket']}",
                      origin="http://example.com", cookie=cookie)
        self.assertNotEqual(ws.status, 101)
        ws.close()
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_idle_timeout_closes_connection_not_terminal(self):
        from crewhall import settings

        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        settings.patch({"terminals.idle_timeout": 30})
        try:
            _s, data, _ = self._ticket(cookie, term["session_id"], mode="read")
            ws = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={data['ticket']}",
                          origin=f"http://127.0.0.1:{self.port}", cookie=cookie)
            self.assertEqual(ws.status, 101)
            deadline = time.monotonic() + 45
            closed = False
            while time.monotonic() < deadline:
                try:
                    frame = ws.recv(timeout=5.0)
                except TimeoutError:
                    continue
                if frame is None:
                    closed = True
                    break
            self.assertTrue(closed, "idle connection should be closed")
            ws.close()
            # The terminal itself is still there.
            status, info, _ = self._op(cookie, "terminal_info", id=term["session_id"])
            self.assertEqual(status, 200)
        finally:
            settings.patch({"terminals.idle_timeout": 900})
            self._op(cookie, "terminal_close", id=term["session_id"])

    def test_max_per_token(self):
        from crewhall import settings

        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        settings.patch({"terminals.max_per_token": 1})
        try:
            self._new_terminal(cookie, cwd="/tmp")
            status, _, _ = self._op(cookie, "terminal_create", cwd="/tmp")
            self.assertNotEqual(status, 200)
        finally:
            settings.patch({"terminals.max_per_token": 4})
            for t in self._op(cookie, "terminal_list")[1].get("terminals", []):
                self._op(cookie, "terminal_close", id=t["session_id"])

    def test_max_clients_per_session(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        opened = []
        statuses = []
        for _ in range(5):
            _s, data, _ = self._ticket(cookie, term["session_id"], mode="read")
            ws = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={data['ticket']}",
                          origin=f"http://127.0.0.1:{self.port}", cookie=cookie)
            statuses.append(ws.status)
            opened.append(ws)
        self.assertEqual(statuses.count(101), 4)
        self.assertNotIn(101, statuses[4:])
        for ws in opened:
            ws.close()
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_readonly_ws_input_ignored(self):
        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp", readonly=True)
        _s, data, _ = self._ticket(cookie, term["session_id"], mode="read")
        ws = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={data['ticket']}",
                      origin=f"http://127.0.0.1:{self.port}", cookie=cookie)
        self.assertEqual(ws.status, 101)
        ws.send(bytes([0x01]) + b"echo READONLY_$((6*7))\n")
        time.sleep(0.6)
        _s, cap, _ = self._op(cookie, "terminal_capture", id=term["session_id"])
        self.assertNotIn("READONLY_42", cap.get("output", ""))
        ws.close()
        self._op(cookie, "terminal_close", id=term["session_id"])

    def test_terminal_responses_have_security_headers(self):
        status, _data, _ = self._request("GET", "/terminal.html")
        # _request only returns json; use a raw connection for headers.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/terminal.html")
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        resp.read()
        conn.close()
        self.assertIn("content-security-policy", headers)
        self.assertEqual(headers.get("x-frame-options", "").upper(), "DENY")

    def test_disabled_ws_is_404(self):
        from crewhall import settings

        settings.patch({"terminals.enabled": False})
        try:
            ws = WSClient(self.port, "/ws/terminal/term_deadbeef",
                          origin=f"http://127.0.0.1:{self.port}", cookie=self._cookie())
            self.assertEqual(ws.status, 404)
            ws.close()
        finally:
            settings.patch({"terminals.enabled": True}, confirm=True)

    def test_slow_ws_client_does_not_freeze_daemon(self):
        from crewhall.client import Client

        token, _ = self._issue(scope="write")
        cookie = self._cookie()
        self._unlock(cookie, token)
        term = self._new_terminal(cookie, cwd="/tmp")
        _s, data, _ = self._ticket(cookie, term["session_id"], mode="write")
        ws = WSClient(self.port, f"/ws/terminal/{term['session_id']}?ticket={data['ticket']}",
                      origin=f"http://127.0.0.1:{self.port}", cookie=cookie)
        self.assertEqual(ws.status, 101)
        # Flood the terminal while never reading from the WebSocket.
        self._op(cookie, "terminal_write", id=term["session_id"],
                 text="yes | head -c 30000000\n", enter=True)
        client = Client(socket_path=self.sock, autostart=False)
        t0 = time.monotonic()
        client.call("ping")
        self.assertLess(time.monotonic() - t0, 1.0)
        ws.close()
        self._op(cookie, "terminal_close", id=term["session_id"])


if __name__ == "__main__":
    unittest.main()
