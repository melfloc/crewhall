from __future__ import annotations

import json
import os
import time
import unittest

from crewhall.mcp import server


class _Identity(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {k: os.environ.get(k)
                      for k in ("CREWHALL_AGENT_ID", "CREWHALL_TOKEN")}
        os.environ.update({"CREWHALL_AGENT_ID": "sess_me",
                           "CREWHALL_TOKEN": "tok-secret"})

    def tearDown(self) -> None:
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Protocol(_Identity):
    def test_initialize_advertises_tools(self):
        response = server.handle(server.Session(), {"jsonrpc": "2.0", "id": 1,
                                                    "method": "initialize", "params": {}})
        self.assertEqual(response["result"]["serverInfo"]["name"], "crewhall")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_tools_list_is_minimal(self):
        response = server.handle(server.Session(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in response["result"]["tools"]}
        self.assertEqual(names, {"whoami", "list_teammates", "send_message", "new_session",
                                  "request", "reply", "handoff"})
        # No tool can read screens/files/state of others or run commands.
        for tool in response["result"]["tools"]:
            self.assertNotIn("command", json.dumps(tool["inputSchema"]).lower())

    def test_unknown_method_is_an_error(self):
        response = server.handle(server.Session(), {"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_notification_has_no_response(self):
        self.assertIsNone(server.handle(server.Session(), {"method": "notifications/initialized"}))


class Identity(_Identity):
    def test_missing_identity_refuses_tool_calls(self):
        import os

        os.environ.pop("CREWHALL_TOKEN", None)
        session = server.Session()
        response = server.handle(session, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "whoami", "arguments": {}}})
        self.assertEqual(response["error"]["code"], -32001)

    def test_no_from_argument_so_impersonation_is_impossible(self):
        session = server.Session()
        with self.assertRaises(server.McpError):
            server.call_tool(session, "send_message",
                             {"to": "bob", "message": "hi", "from": "alice"})

    def test_send_message_uses_the_env_identity(self):
        session = server.Session()
        seen = {}

        def fake_call(op, **params):
            seen.update(op=op, **params)
            return {"delivery": {"state": "injected"}}

        session.call = fake_call  # type: ignore[assignment]
        server.call_tool(session, "send_message", {"to": "bob", "message": "hi"})
        self.assertEqual(seen["op"], "team_send")
        self.assertEqual(seen["sender"], "sess_me")
        self.assertEqual(seen["token"], "tok-secret")
        self.assertNotIn("from", seen)

    def test_outside_team_error_is_reported_not_raised(self):
        session = server.Session()

        def fake_call(op, **params):
            raise server.McpError(-32002, "not in the same team")

        session.call = fake_call  # type: ignore[assignment]
        response = server.handle(session, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "send_message",
                                                      "arguments": {"to": "x", "message": "y"}}})
        self.assertTrue(response["result"]["isError"])

    def test_arguments_are_validated(self):
        session = server.Session()
        for args in ({"to": "bob"}, {"message": "hi"}, {"to": "", "message": "hi"},
                     {"to": "bob", "message": ""}, {"to": "bob", "message": "x" * 9000}):
            with self.assertRaises(server.McpError):
                server.call_tool(session, "send_message", args)

    def test_rate_limit(self):
        session = server.Session()
        session._calls = [time.time()] * server.RATE_MAX
        with self.assertRaises(server.McpError):
            server.call_tool(session, "whoami", {})


class ControllerWiring(_Identity):
    def _controller(self):
        from crewhall import Controller
        from crewhall import settings

        self.addCleanup(lambda: settings.reset("providers.claude"))
        self.addCleanup(lambda: settings.reset("providers.codex"))
        return Controller(adopt=False)

    def test_claude_gets_a_private_config_file_without_secrets(self):
        import os
        import stat

        from crewhall import settings
        from crewhall.harness import get_harness

        c = self._controller()
        settings.patch({"providers.claude.mcp": True})
        args, path = c._mcp_for("claude", get_harness("claude"))
        self.assertEqual(args[0], "--mcp-config")
        self.assertTrue(path and os.path.isfile(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertNotIn("tok-secret", body)
        self.assertIn("crewhall.mcp", body)
        c._mcp_files["sess_me"] = path
        c._mcp_cleanup("sess_me")
        self.assertFalse(os.path.exists(path))

    def test_disabled_by_default_and_codex_uses_flags(self):
        from crewhall import settings
        from crewhall.harness import get_harness

        c = self._controller()
        args, path = c._mcp_for("claude", get_harness("claude"))
        self.assertEqual((args, path), ([], None))
        settings.patch({"providers.codex.mcp": True})
        args, path = c._mcp_for("codex", get_harness("codex"))
        self.assertIsNone(path)
        self.assertIn("-c", args)
        self.assertTrue(any("mcp_servers.crewhall" in a for a in args))

    def test_opencode_has_no_verified_mcp_wiring(self):
        from crewhall.harness import get_harness

        self.assertFalse(get_harness("opencode").mcp_supported)


if __name__ == "__main__":
    unittest.main()
