from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import unittest
from unittest import mock

from crewhall import brand, settings
from crewhall.client import Client
from crewhall.gateway import ALLOWED_OPS, AgentGateway

ALIAS = "prod"


def _gateway(host_of=None):
    calls: list[dict] = []

    def dispatch(req):
        calls.append(req)
        return {"ok": True, "echo": req.get("op")}

    hosts = {"remote-1": ALIAS, "other-1": "staging", "local-1": None}
    return AgentGateway("/tmp/unused.sock", ALIAS, dispatch,
                        host_of or (lambda ref: hosts[ref])), calls


class GatewayPolicyTests(unittest.TestCase):
    def test_allowed_ops_forward_with_sanitised_request(self):
        gw, calls = _gateway()
        res = gw.handle_request({"op": "team_send", "sender": "remote-1", "token": "t",
                                 "recipient": "x", "body": "hi", "_actor": "local"})
        self.assertTrue(res["ok"])
        self.assertEqual(calls[0]["_actor"], f"ssh:{ALIAS}")  # not client-controlled
        self.assertEqual(calls[0]["body"], "hi")

    def test_every_other_op_is_refused_before_the_daemon(self):
        gw, calls = _gateway()
        for op in ("create", "agent_create", "agent_write", "agent_key", "kill", "terminate",
                   "shutdown", "settings_set", "settings_get", "reset_apply", "team_up",
                   "team_members", "request_list", "agent_list", "message_send",
                   "bundle_import", "clean_apply", "fs_complete", "agent_hook", "nonsense"):
            res = gw.handle_request({"op": op, "sender": "remote-1", "agent": "remote-1",
                                     "target": "remote-1", "token": "t"})
            self.assertFalse(res["ok"], op)
        self.assertEqual(calls, [])

    def test_token_and_actor_are_mandatory(self):
        gw, calls = _gateway()
        for req in ({"op": "agent_identity", "target": "remote-1"},
                    {"op": "agent_identity", "target": "remote-1", "token": ""},
                    {"op": "agent_identity", "target": "remote-1", "token": 5},
                    {"op": "agent_identity", "token": "t"},
                    {"op": "request_cancel", "request_id": "r1", "token": "t"},  # operator cancel
                    "not a dict", None):
            self.assertFalse(gw.handle_request(req)["ok"], req)
        self.assertEqual(calls, [])

    def test_only_agents_of_this_host_may_act(self):
        gw, calls = _gateway()
        for actor in ("local-1", "other-1", "ghost"):
            res = gw.handle_request({"op": "agent_identity", "target": actor, "token": "t"})
            self.assertFalse(res["ok"], actor)
        self.assertEqual(calls, [])

    def test_ping_reveals_nothing(self):
        gw, calls = _gateway()
        self.assertEqual(gw.handle_request({"op": "ping"}), {"ok": True})
        self.assertEqual(calls, [])

    def test_allowed_ops_are_exactly_the_token_authenticated_agent_ops(self):
        self.assertEqual(set(ALLOWED_OPS), {"agent_identity", "team_send", "request_create",
                                            "request_reply", "request_cancel"})


class GatewaySocketTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ati-gw-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "gw.sock")
        self.calls: list[dict] = []
        self.gw = AgentGateway(self.path, ALIAS,
                               lambda r: self.calls.append(r) or {"ok": True},
                               lambda ref: ALIAS)
        self.gw.start()
        self.addCleanup(self.gw.stop)

    def _send(self, raw: bytes) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(self.path)
            s.sendall(raw)
            data = b""
            while b"\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.split(b"\n")[0])

    def test_socket_is_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_roundtrip_bad_json_and_oversize(self):
        ok = self._send(b'{"op":"agent_identity","target":"a","token":"t"}\n')
        self.assertTrue(ok["ok"])
        self.assertFalse(self._send(b"{nope\n")["ok"])
        self.assertIn("too large", self._send(b"x" * (300 * 1024))["error"])
        self.assertEqual(len(self.calls), 1)

    def test_stop_removes_the_socket(self):
        self.gw.stop()
        self.assertFalse(os.path.exists(self.path))


class TunnelSettingsAndClientTests(unittest.TestCase):
    def test_tunnel_is_closed_by_default_and_must_be_boolean(self):
        base = {"ssh": "user@example"}
        self.assertFalse(settings._validate_host("h", base)["tunnel"])
        self.assertTrue(settings._validate_host("h", {**base, "tunnel": True})["tunnel"])
        for bad in ("yes", 1, None, [], "true"):
            with self.assertRaises(settings.SettingsError):
                settings._validate_host("h", {**base, "tunnel": bad})

    def test_gateway_env_disables_daemon_autostart(self):
        with mock.patch.dict(os.environ, {f"{brand.ENV_PREFIX}_GATEWAY": "1"}):
            self.assertFalse(Client(socket_path="/nonexistent").autostart)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(f"{brand.ENV_PREFIX}_GATEWAY", None)
            os.environ.pop(f"{brand.LEGACY_ENV_PREFIX}_GATEWAY", None)
            self.assertTrue(Client(socket_path="/nonexistent").autostart)


if __name__ == "__main__":
    unittest.main()
