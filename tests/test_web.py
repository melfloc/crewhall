from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
import urllib.request

from crewhall.web import ws
from crewhall.web.server import WebServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WebSocketCodec(unittest.TestCase):
    def test_accept_key_rfc_example(self):
        # RFC 6455 example
        self.assertEqual(
            ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_encode_decode_roundtrip(self):
        frame = ws.encode_text('{"hello":"world"}')
        # server->client frames are unmasked; decode expects masked, so build a
        # masked client frame here.
        payload = b"ping"
        mask = b"\x01\x02\x03\x04"
        masked = bytearray([0x81, 0x80 | len(payload)]) + mask
        masked += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        opcode, flen, out = ws.parse_frame(bytes(masked))
        self.assertEqual(opcode, 1)
        self.assertEqual(out, b"ping")
        self.assertTrue(frame.startswith(b"\x81"))


class WebSocketEvents(unittest.TestCase):
    """Regression: after a 101 handshake the channel must actually stream
    events (state + output) and never die on a non-blocking send."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_rt = os.environ.get("XDG_RUNTIME_DIR")
        cls._prev_cfg = os.environ.get("XDG_CONFIG_HOME")
        cls._prev_state = os.environ.get("XDG_STATE_HOME")
        cls.tmp = tempfile.mkdtemp(prefix="at-wsev-")
        os.environ["XDG_RUNTIME_DIR"] = cls.tmp
        os.environ["XDG_CONFIG_HOME"] = cls.tmp
        os.environ["XDG_STATE_HOME"] = cls.tmp
        from crewhall.web import auth

        cls.token = auth.generate_token()
        cls.port = _free_port()
        cls.server = WebServer(host="127.0.0.1", port=cls.port)
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
            from crewhall.client import Client

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

    def _ws(self):
        from crewhall.web import auth

        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: " + key + "\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Cookie: at_session={auth.issue_session()}\r\n\r\n"
        )
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        return s

    def _recv(self, s, timeout=4.0):
        s.settimeout(timeout)
        try:
            h = s.recv(2)
        except TimeoutError:
            return None
        if len(h) < 2:
            return None
        ln = h[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", s.recv(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", s.recv(8))[0]
        data = b""
        while len(data) < ln:
            data += s.recv(ln - len(data))
        return json.loads(data.decode("utf-8"))

    def test_state_frame_streams_and_includes_messages(self):
        s = self._ws()
        try:
            deadline = time.monotonic() + 5
            got = None
            while time.monotonic() < deadline:
                m = self._recv(s)
                if m and m.get("type") == "state":
                    got = m
                    break
            self.assertIsNotNone(got, "no state frame received")
            self.assertIn("agents", got)
            self.assertIn("teams", got)
            self.assertIn("messages", got)
        finally:
            s.close()

    @unittest.skipUnless(shutil.which("opencode"), "needs the opencode CLI")
    def test_output_event_after_input(self):
        # Create an agent via the control plane, send input through the same
        # API the web uses, and expect an output frame over the WebSocket.
        import shutil as _shutil

        from crewhall.client import Client

        backend = "tmux" if _shutil.which("tmux") else "pty"
        client = Client()
        client.call(
            "agent_create", kind="opencode", name="wsprobe",
            backend=backend, cwd=self.tmp,
        )
        s = self._ws()
        try:
            self._recv(s)  # initial state
            # Wait for the agent to become usable before injecting input.
            deadline = time.monotonic() + 90
            ready = False
            while time.monotonic() < deadline:
                state = client.call("agent_state", target="wsprobe")["agent"]["state"]
                if state in ("ready", "waiting_input"):
                    ready = True
                    break
                time.sleep(0.5)
            self.assertTrue(ready, "agent not ready")
            client.call(
                "agent_write", target="wsprobe", text="Reply with WS_REGRESSION_OK"
            )
            client.call("agent_key", target="wsprobe", key="ENTER")
            # The regression contract: after input, the WebSocket streams an
            # output frame (transcript) reflecting the agent's turn.
            deadline = time.monotonic() + 120
            seen = False
            while time.monotonic() < deadline:
                m = self._recv(s, timeout=3)
                if m and m.get("type") == "output" and "WS_REGRESSION_OK" in m.get("output", ""):
                    seen = True
                    break
            self.assertTrue(seen, "output after input not streamed")
        finally:
            s.close()
            try:
                client.call("agent_stop", target="wsprobe")
            except Exception:
                pass


class WebServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_runtime = os.environ.get("XDG_RUNTIME_DIR")
        cls._prev_state = os.environ.get("XDG_STATE_HOME")
        cls.tmp = tempfile.mkdtemp(prefix="at-web-")
        os.environ["XDG_RUNTIME_DIR"] = cls.tmp
        os.environ["XDG_STATE_HOME"] = cls.tmp
        cls.port = _free_port()
        cls.server = WebServer(host="127.0.0.1", port=cls.port)
        cls.server.start()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server.stop()
        except Exception:
            pass
        try:
            from crewhall.client import Client

            Client(autostart=False).call("shutdown")
        except Exception:
            pass
        if cls._prev_runtime is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = cls._prev_runtime
        if cls._prev_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = cls._prev_state
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path: str) -> tuple[int, bytes]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.read()

    def _post(self, op: str, **params) -> dict:
        data = json.dumps({"op": op, **params}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/op", data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read())

    def _raw_get(self, raw_path: str) -> tuple[int, str, bytes]:
        """Send a GET without letting urllib normalize the path (needed for ../)."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(
                f"GET {raw_path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
            )
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1]) if lines and lines[0] else 0
        ctype = next(
            (ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("content-type:")), ""
        )
        return status, ctype, body

    def test_index_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"CREWHALL", body)

    def test_static_assets_are_served_with_their_types(self):
        status, body = self._get("/static/app.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--bg", body)  # a design token, not an error page
        status, body = self._get("/static/js/core.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function el(", body)
        status, ctype, _ = self._raw_get("/static/js/core.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", ctype)

    def test_service_worker_is_served_with_push_handlers(self):
        status, body = self._get("/sw.js")
        self.assertEqual(status, 200)
        self.assertIn(b'addEventListener("push"', body)
        self.assertIn(b"notificationclick", body)
        status, ctype, _ = self._raw_get("/sw.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", ctype)

    def test_static_rejects_path_traversal(self):
        # Anything that escapes the static directory must be a flat 404, never a
        # read of a server file or the daemon's own sources.
        for raw in (
            "/static/../server.py",
            "/static/../../server.py",
            "/static/../web/server.py",
            "/static/%2e%2e/server.py",
            "/static/..%2fserver.py",
        ):
            status, _ctype, body = self._raw_get(raw)
            self.assertEqual(status, 404, raw)
            self.assertNotIn(b"BaseHTTPRequestHandler", body, raw)
        # A directory or an unknown extension is not a served asset either.
        self.assertEqual(self._raw_get("/static/")[0], 404)
        self.assertEqual(self._raw_get("/static/nope.txt")[0], 404)

    def test_meta_and_state(self):
        meta = self._post("meta_info")
        self.assertTrue(meta["ok"])
        self.assertIn("harnesses", meta)
        status, body = self._get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertIn("agents", state)
        self.assertIn("teams", state)

    def test_team_create_and_list(self):
        r = self._post("team_create", name="webt", agent_ids=[])
        self.assertTrue(r["ok"], r)
        r2 = self._post("team_list")
        self.assertTrue(any(t["name"] == "webt" for t in r2["teams"]))

    def test_interaction_ops_reach_the_daemon(self):
        r = self._post("interaction_list")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["interactions"], [])
        r = self._post("interaction_respond", id="nope:per_1", answer={"reply": "once"})
        self.assertFalse(r["ok"])
        self.assertIn("no such interaction", r["error"])

    def test_bundle_export_and_list(self):
        r = self._post("bundle_export")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["name"].endswith(".tar.gz"))
        listing = self._post("bundle_list")
        self.assertTrue(listing["ok"])
        self.assertTrue(any(b["name"] == r["name"] for b in listing["bundles"]))

    def test_bundle_import_previews_and_applies_team_definitions(self):
        from crewhall import bundle, paths

        dest_dir = os.path.join(paths.state_dir(), "bundles")
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        name = "bundle-teamtest.tar.gz"
        self.addCleanup(lambda: os.path.exists(os.path.join(dest_dir, name)) and os.unlink(os.path.join(dest_dir, name)))
        bundle.export_bundle(os.path.join(dest_dir, name), live_teams=[
            {"name": "ghost-team", "workspace": None,
             "agents": [{"name": "ghost", "kind": "opencode", "cwd": "/nonexistent-dir-xyz"}]}])
        r = self._post("bundle_import", name=name, dry_run=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["teams_plan"][0]["team"], "ghost-team")
        self.assertFalse(r["teams_plan"][0]["agents"][0]["cwd_ok"])
        r = self._post("bundle_import", name=name, dry_run=False, apply_teams=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["teams_applied"]["created"], [])
        self.assertEqual(r["teams_applied"]["skipped"][0]["agent"], "ghost")
        self.assertFalse(any(t["name"] == "ghost-team" for t in self._post("team_list")["teams"]))

    def _raw_req(self, method: str, path: str, body: bytes = b"") -> tuple[int, dict, bytes]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body or None, method=method,
                                     headers={"Content-Type": "application/gzip"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_bundle_download_upload_delete_roundtrip(self):
        exported = self._post("bundle_export")
        name = exported["name"]
        self.addCleanup(lambda: self._post("bundle_delete", name=name))
        status, headers, data = self._raw_req("GET", "/api/bundle/" + name)
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(data[:2] == b"\x1f\x8b")  # gzip
        status, _, body = self._raw_req("POST", "/api/bundle?name=my%20setup.tar.gz", data)
        up = json.loads(body)
        self.assertEqual(status, 200, up)
        self.assertRegex(up["name"], r"^uploaded-\d{8}-\d{6}-my-setup\.tar\.gz$")
        listing = {b["name"]: b for b in self._post("bundle_list")["bundles"]}
        self.assertTrue(listing[up["name"]]["valid"])
        r = self._post("bundle_delete", names=[up["name"], "../../etc/passwd", "nope.tar.gz"])
        self.assertEqual(r["deleted"], [up["name"]])
        self.assertEqual(len(r["failed"]), 2)

    def test_bundle_upload_rejects_garbage_and_download_rejects_traversal(self):
        status, _, body = self._raw_req("POST", "/api/bundle?name=x.tar.gz", b"this is not a bundle")
        self.assertEqual(status, 400)
        self.assertIn("not a valid bundle", json.loads(body)["error"])
        self.assertEqual(self._raw_req("POST", "/api/bundle?name=x.tar.gz")[0], 400)  # empty
        for raw in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "x.txt", "nope.tar.gz"):
            self.assertEqual(self._raw_req("GET", "/api/bundle/" + raw)[0], 404, raw)

    def test_bundle_list_flags_empty_bundles(self):
        name = self._post("bundle_export")["name"]  # no config, no teams: an empty bundle
        self.addCleanup(lambda: self._post("bundle_delete", name=name))
        entry = next(b for b in self._post("bundle_list")["bundles"] if b["name"] == name)
        self.assertTrue(entry["valid"])
        if not entry["files"]:
            self.assertTrue(entry["empty"])

    def test_settings_roundtrip_validation_and_provider_check(self):
        self.addCleanup(lambda: self._post("settings_reset"))
        r = self._post("settings_get")
        self.assertTrue(r["ok"], r)
        self.assertEqual([p["kind"] for p in r["providers"]], ["claude", "codex", "opencode"])
        no_confirm = self._post("settings_set", changes={"providers.claude.env": "X=1"})
        self.assertFalse(no_confirm["ok"]); self.assertIn("confirmation", no_confirm["error"])
        r = self._post("settings_set", changes={"security.session_ttl_hours": 6,
                                                "providers.claude.env": "ANTHROPIC_API_KEY=sk-secret"},
                       confirm="CONFIRM")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("sk-secret", json.dumps(r))                      # never echoed back
        bad = self._post("settings_set", changes={"security.session_ttl_hours": 0})
        self.assertFalse(bad["ok"]); self.assertIn("between 1 and 720", bad["error"])
        none_left = self._post("settings_set", changes={"providers.claude.enabled": False,
                                                       "providers.codex.enabled": False,
                                                       "providers.opencode.enabled": False})
        self.assertFalse(none_left["ok"]); self.assertIn("at least one provider", none_left["error"])
        chk = self._post("provider_check", kind="claude", command="definitely-not-a-binary-xyz")
        self.assertFalse(chk["providers"][0]["ok"])

    def test_token_rotation_returns_a_new_token_once_and_old_one_stops_working(self):
        from crewhall.web import auth

        old = auth.generate_token(rotate=True)
        self.addCleanup(lambda: auth.revoke_token())
        r = self._post("web_token_rotate")
        self.assertTrue(r["ok"], r)
        self.assertNotEqual(r["token"], old)
        self.assertTrue(auth.verify_token(r["token"]))
        self.assertFalse(auth.verify_token(old))
        self.assertTrue(r["fingerprint"].startswith("sha256:"))
        self.assertNotIn(r["token"], json.dumps(self._post("web_token_status")))

    def test_the_web_ui_cannot_turn_itself_off(self):
        r = self._post("frontend_set", mode="off")
        self.assertFalse(r["ok"]); self.assertIn("lock you out", r["error"])

    def test_fs_complete_lists_directories_of_the_daemon_host(self):
        import tempfile

        d = tempfile.mkdtemp(prefix="at-fsw-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.addCleanup(lambda: self._post("settings_reset"))
        self._post("settings_set", changes={"security.fs_roots": [d]})
        os.makedirs(os.path.join(d, "proj-a")); os.makedirs(os.path.join(d, "proj-b")); open(os.path.join(d, "file"), "w").close()
        r = self._post("fs_complete", prefix=d + "/proj")
        self.assertTrue(r["ok"], r)
        self.assertEqual([e["name"] for e in r["entries"]], ["proj-a", "proj-b"])
        # Outside every allowed root: nothing is listed (no escape).
        outside = self._post("fs_complete", prefix="/etc/passw")
        self.assertEqual(outside["entries"], [])
        sym = os.path.join(d, "escape")
        os.symlink("/etc", sym)
        escaped = self._post("fs_complete", prefix=sym + "/passw")
        self.assertEqual(escaped["entries"], [])

    def test_reset_plan_and_the_typed_confirmation_for_a_full_reset(self):
        plan = self._post("reset_plan", level="full")
        self.assertTrue(plan["ok"], plan); self.assertTrue(plan["needs_confirm"])
        self.assertFalse(self._post("reset_plan", level="nuke")["ok"])
        no_word = self._post("reset_apply", level="full")
        self.assertFalse(no_word["ok"]); self.assertIn("RESET", no_word["error"])
        wrong = self._post("reset_apply", level="full", confirm="reset")
        self.assertFalse(wrong["ok"])
        clean = self._post("reset_apply", level="clean")
        self.assertTrue(clean["ok"], clean); self.assertFalse(clean["restart"])

    def test_bundle_import_rejects_traversal(self):
        r = self._post("bundle_import", name="../../etc/passwd")
        self.assertFalse(r["ok"])
        self.assertIn("invalid bundle name", r["error"])

    def test_session_admin_ops(self):
        sessions = self._post("web_sessions")
        self.assertTrue(sessions["ok"])
        self.assertIsInstance(sessions["sessions"], list)
        status = self._post("web_token_status")
        self.assertTrue(status["ok"])
        self.assertIn("token_exists", status)
        self.assertIn("fingerprint", status)

    def test_update_status_op_reports_versions(self):
        r = self._post("update_status")
        self.assertTrue(r["ok"], r)
        self.assertIn("version", r)
        self.assertIn("restart_pending", r)

    def test_forbidden_operation(self):
        r = self._post("shutdown")
        self.assertFalse(r["ok"])
        self.assertIn("not allowed", r["error"])

    def test_websocket_handshake(self):
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            req = (
                "GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: " + key + "\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(req.encode())
            data = sock.recv(4096).decode("latin-1")
            self.assertIn("101", data)
            self.assertIn("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", data)
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
