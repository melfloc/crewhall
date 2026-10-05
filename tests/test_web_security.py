from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.request

from agent_terminal.web import auth, tailscale
from agent_terminal.web.server import SecurityPolicy, WebServer, resolve_host
from agent_terminal.web.ws import accept_key


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AuthUnit(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("XDG_CONFIG_HOME")
        self.cfg = tempfile.mkdtemp(prefix="at-auth-")
        os.environ["XDG_CONFIG_HOME"] = self.cfg

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._prev
        shutil.rmtree(self.cfg, ignore_errors=True)

    def test_generate_load_and_perms(self):
        token = auth.generate_token()
        self.assertGreaterEqual(len(token), 32)
        self.assertTrue(auth.token_exists())
        self.assertEqual(auth.load_token(), token)
        self.assertTrue(auth.file_permissions_ok())

    def test_generate_twice_fails_without_rotate(self):
        auth.generate_token()
        with self.assertRaises(FileExistsError):
            auth.generate_token()
        t2 = auth.generate_token(rotate=True)
        self.assertEqual(auth.load_token(), t2)

    def test_verify_token(self):
        token = auth.generate_token()
        self.assertTrue(auth.verify_token(token))
        self.assertFalse(auth.verify_token("nope"))
        self.assertFalse(auth.verify_token(None))

    def test_revoke(self):
        auth.generate_token()
        self.assertTrue(auth.revoke_token())
        self.assertFalse(auth.token_exists())
        self.assertFalse(auth.verify_token("x"))

    def test_fingerprint_is_not_token(self):
        token = auth.generate_token()
        fp = auth.token_fingerprint()
        self.assertTrue(fp.startswith("sha256:"))
        self.assertNotIn(token, fp)

    def test_session_roundtrip(self):
        auth.generate_token()
        session = auth.issue_session(ttl=60)
        self.assertTrue(auth.verify_session(session))
        self.assertFalse(auth.verify_session("bad.sig"))
        # A session signed with a different token's key must not verify.
        auth.generate_token(rotate=True)
        self.assertFalse(auth.verify_session(session))

    def test_authenticate_bearer_and_cookie(self):
        token = auth.generate_token()
        self.assertTrue(auth.authenticate({"Authorization": f"Bearer {token}"}))
        self.assertFalse(auth.authenticate({"Authorization": "Bearer x"}))
        session = auth.issue_session()
        self.assertTrue(auth.authenticate({"Cookie": f"at_session={session}"}))
        self.assertFalse(auth.authenticate({}))

    def test_authorize_boundary(self):
        self.assertTrue(auth.authorize("web"))


class SecurityPolicyUnit(unittest.TestCase):
    def test_localhost_hosts_allowed(self):
        p = SecurityPolicy()
        for h in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            self.assertTrue(p.host_allowed(h))

    def test_unknown_host_rejected(self):
        p = SecurityPolicy()
        self.assertFalse(p.host_allowed("evil.example:80"))

    def test_allow_list(self):
        p = SecurityPolicy(allowed_hosts=["100.89.1.2", "host.tail.ts.net"])
        self.assertTrue(p.host_allowed("100.89.1.2:8765"))
        self.assertTrue(p.host_allowed("host.tail.ts.net"))
        self.assertFalse(p.host_allowed("other:80"))

    def test_origin_policy(self):
        p = SecurityPolicy(allowed_hosts=["100.89.1.2"])
        self.assertTrue(p.origin_allowed("http://100.89.1.2:8765"))
        self.assertFalse(p.origin_allowed("http://evil.example"))
        self.assertFalse(p.origin_allowed("ftp://100.89.1.2"))


class TailscaleDetection(unittest.TestCase):
    def test_available_is_bool(self):
        self.assertIsInstance(tailscale.available(), bool)

    def test_local_ipv4_none_or_str(self):
        ip = tailscale.local_ipv4()
        self.assertTrue(ip is None or isinstance(ip, str))

    def test_status_shape_or_unavailable(self):
        try:
            data = tailscale.status()
            self.assertIn("Self", data)
        except tailscale.TailscaleUnavailable:
            self.skipTest("tailscale not available/configured")

    def test_resolve_host_tailscale_without_binary(self):
        from argparse import Namespace

        args = Namespace(tailscale=True, host="127.0.0.1", allow_host=None)
        if not tailscale.available() or not tailscale.local_ipv4():
            with self.assertRaises(SystemExit):
                resolve_host(args)
        else:
            host, allowed = resolve_host(args)
            self.assertEqual(host, tailscale.local_ipv4())
            self.assertTrue(allowed)


class SecureWebServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_rt = os.environ.get("XDG_RUNTIME_DIR")
        cls._prev_cfg = os.environ.get("XDG_CONFIG_HOME")
        cls._prev_state = os.environ.get("XDG_STATE_HOME")
        cls.tmp = tempfile.mkdtemp(prefix="at-wsec-")
        os.environ["XDG_RUNTIME_DIR"] = cls.tmp
        os.environ["XDG_CONFIG_HOME"] = cls.tmp
        os.environ["XDG_STATE_HOME"] = cls.tmp
        cls.token = auth.generate_token()
        cls.port = _free_port()
        cls.server = WebServer(
            host="127.0.0.1", port=cls.port,
            policy=SecurityPolicy(require_auth=True, allowed_hosts=["127.0.0.1", "localhost"]),
        )
        cls.server.start()
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server.stop()
        except Exception:
            pass
        try:
            from agent_terminal.client import Client

            Client(autostart=False).call("shutdown")
        except Exception:
            pass
        for var, prev in (
            ("XDG_RUNTIME_DIR", cls._prev_rt),
            ("XDG_CONFIG_HOME", cls._prev_cfg),
            ("XDG_STATE_HOME", cls._prev_state),
        ):
            if prev is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prev
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, op, headers=None, body=None):
        data = json.dumps(body or {"op": op}).encode()
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/op", data=data, headers=h
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _raw(self, request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(request)
            sock.settimeout(5)
            return sock.recv(4096)

    def test_unknown_get_path_gets_404_instead_of_hanging(self):
        auth_hdr = f"Authorization: Bearer {self.token}"
        reply = self._raw(
            f"GET /nope HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth_hdr}\r\n\r\n".encode()
        )
        self.assertTrue(reply.startswith(b"HTTP/1.1 404"), reply[:60])

    def test_oversized_body_is_rejected_without_reading_it(self):
        auth_hdr = f"Authorization: Bearer {self.token}"
        reply = self._raw(
            (f"POST /api/op HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth_hdr}\r\n"
             "Content-Type: application/json\r\n"
             "Content-Length: 999999999\r\n\r\n").encode()
        )
        self.assertTrue(reply.startswith(b"HTTP/1.1 413"), reply[:60])

    def test_bad_content_length_is_400(self):
        auth_hdr = f"Authorization: Bearer {self.token}"
        reply = self._raw(
            (f"POST /api/op HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth_hdr}\r\n"
             "Content-Length: abc\r\n\r\n").encode()
        )
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply[:60])

    def test_requires_auth(self):
        status, _ = self._post("meta_info")
        self.assertEqual(status, 401)

    def test_bad_token_rejected(self):
        status, _ = self._post("meta_info", {"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_good_token_allowed(self):
        status, body = self._post("meta_info", {"Authorization": f"Bearer {self.token}"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_login_sets_session_cookie(self):
        data = json.dumps({"token": self.token}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/login", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            cookie = r.headers.get("Set-Cookie")
        self.assertIn("at_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_session_cookie_authenticates(self):
        session = auth.issue_session()
        status, body = self._post("meta_info", {"Cookie": f"at_session={session}"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_bad_origin_rejected(self):
        status, _ = self._post(
            "meta_info",
            {"Authorization": f"Bearer {self.token}", "Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)

    def test_index_public_login_served(self):
        for path in ("/", "/login"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                self.assertEqual(r.status, 200)

    def test_websocket_requires_auth(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(
                b"GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            data = sock.recv(4096).decode("latin-1")
            self.assertIn("401", data.splitlines()[0])
        finally:
            sock.close()

    def test_websocket_authed_handshake(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(
                (
                    "GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                    "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    f"Authorization: Bearer {self.token}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            data = sock.recv(4096).decode("latin-1")
            self.assertIn("101", data.splitlines()[0])
            self.assertIn(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), data)
        finally:
            sock.close()

    def test_security_headers_on_every_response(self):
        for path in ("/", "/login", "/static/app.css", "/static/js/core.js"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                h = {k.lower(): v for k, v in r.headers.items()}
            csp = h.get("content-security-policy", "")
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotIn("unsafe-eval", csp)
            self.assertEqual(h.get("x-content-type-options"), "nosniff")
            self.assertEqual(h.get("x-frame-options"), "DENY")
            self.assertEqual(h.get("referrer-policy"), "no-referrer")
            self.assertTrue(h.get("cross-origin-opener-policy"))
        data = json.dumps({"op": "meta_info"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/op", data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.headers.get("Cache-Control"), "no-store")

    def test_login_is_rate_limited_after_repeated_failures(self):
        auth.reset_login_limits()
        self.addCleanup(auth.reset_login_limits)

        def attempt(token):
            data = json.dumps({"token": token}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/login", data=data,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as exc:
                return exc.code

        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            self.assertEqual(attempt("wrong-token"), 401)
        # The next attempt is refused even with the right token (temporary lockout).
        self.assertEqual(attempt(self.token), 429)
        auth.reset_login_limits()
        self.assertEqual(attempt(self.token), 200)

    def test_token_not_in_state_store(self):
        from agent_terminal import paths

        text = ""
        try:
            text = open(paths.state_path(), encoding="utf-8").read()
        except OSError:
            pass
        self.assertNotIn(self.token, text)


if __name__ == "__main__":
    unittest.main()


class WebFocusAndHistoryOps(unittest.TestCase):
    def test_history_op_is_exposed_and_ws_cadence_is_tiered(self):
        from agent_terminal.web.server import ALLOWED_OPS, Handler

        self.assertIn("agent_history", ALLOWED_OPS)
        self.assertLessEqual(Handler.WS_TICK, Handler.WS_SNAPSHOT_EVERY)
        self.assertLess(Handler.WS_TICK, Handler.WS_OTHERS_EVERY)


class QuietClientErrors(unittest.TestCase):
    def test_a_client_that_vanishes_is_one_log_line_not_a_traceback(self):
        import io
        from contextlib import redirect_stderr

        from agent_terminal.web.server import _TrackingHTTPServer

        server = _TrackingHTTPServer.__new__(_TrackingHTTPServer)
        err = io.StringIO()
        with self.assertLogs("crewhall.web", level="INFO") as logs, redirect_stderr(err):
            try:
                raise BrokenPipeError(32, "Broken pipe")
            except BrokenPipeError:
                server.handle_error(None, ("203.0.113.9", 4242))
        self.assertEqual(err.getvalue(), "")                      # nothing on stderr (the daemon log)
        self.assertIn("203.0.113.9", logs.output[0])
