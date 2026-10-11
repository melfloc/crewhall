from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from crewhall.mcp import server


class FakeSession:
    def __init__(self, responses=None, rate=True, identity=("agent-1", "tok")) -> None:
        self.responses = responses or {}
        self._rate = rate
        self._identity = identity
        self.calls: list[tuple[str, dict]] = []

    def rate_ok(self) -> bool:
        return self._rate

    def identity(self):
        if self._identity is None:
            raise server.McpError(-32001, "not inside an agent")
        return self._identity

    def call(self, op, **params):
        self.calls.append((op, params))
        return self.responses.get(op, {})


class CallTool(unittest.TestCase):
    def _tool(self, name, arguments=None, **kw):
        session = FakeSession(**kw)
        return session, server.call_tool(session, name, arguments)

    def test_whoami(self) -> None:
        _, out = self._tool("whoami", {},
                            responses={"agent_identity": {"agent_id": "a", "name": "n", "teams": ["t"]}})
        self.assertFalse(out["isError"])

    def test_list_teammates(self) -> None:
        _, out = self._tool("list_teammates", {}, responses={"agent_identity": {"teammates": []}})
        self.assertIn("content", out)

    def test_send_message_ok_and_invalid(self) -> None:
        _, out = self._tool("send_message", {"to": "b", "message": "hi"},
                            responses={"team_send": {"delivery": {"ok": True}}})
        self.assertFalse(out["isError"])
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(), "send_message", {"to": "", "message": "hi"})
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(), "send_message", {"to": "b", "message": ""})

    def test_handoff(self) -> None:
        _, out = self._tool("handoff", {"to": "b", "message": "take over"},
                            responses={"team_send": {"delivery": {"ok": True}}})
        self.assertIn("handoff", out["content"][0]["text"])

    def test_request_ok_and_timeout_invalid(self) -> None:
        _, out = self._tool("request", {"to": "b", "task": "do"},
                            responses={"request_create": {"request": {"request_id": "r", "state": "open"}}})
        self.assertFalse(out["isError"])
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(), "request", {"to": "b", "task": "do", "timeout": -1})

    def test_reply(self) -> None:
        _, out = self._tool("reply", {"request_id": "r", "message": "done"},
                            responses={"request_reply": {"request": {"request_id": "r", "state": "replied"}}})
        self.assertFalse(out["isError"])
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(), "reply", {"request_id": "", "message": "x"})

    def test_new_session(self) -> None:
        _, out = self._tool("new_session", {},
                            responses={"team_new_session": {"conversation_id": "c", "previous": None,
                                                            "confirmed": True}})
        self.assertIn("conversation_id", out["content"][0]["text"])

    def test_publish_artifact(self) -> None:
        _, out = self._tool("publish_artifact", {"conversation": "c", "path": "/x"},
                            responses={"conversation_publish": {"artifact": {"name": "x"}}})
        self.assertFalse(out["isError"])
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(), "publish_artifact", {"conversation": "", "path": "/x"})

    def test_unknown_tool(self) -> None:
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(), "nope", {})

    def test_rate_limited(self) -> None:
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(rate=False), "whoami", {})

    def test_arguments_must_be_object(self) -> None:
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(), "whoami", "not a dict")

    def test_unexpected_arguments(self) -> None:
        with self.assertRaises(server.McpError):
            server.call_tool(FakeSession(), "whoami", {"extra": 1})


class Handle(unittest.TestCase):
    def test_invalid_request(self) -> None:
        self.assertEqual(server.handle(FakeSession(), "x")["error"]["code"], -32600)

    def test_initialize_ping_tools(self) -> None:
        self.assertIn("result", server.handle(FakeSession(), {"id": 1, "method": "initialize"}))
        self.assertEqual(server.handle(FakeSession(), {"id": 2, "method": "ping"})["result"], {})
        self.assertIn("tools", server.handle(FakeSession(), {"id": 3, "method": "tools/list"})["result"])

    def test_notification(self) -> None:
        self.assertIsNone(server.handle(FakeSession(), {"method": "initialized"}))

    def test_tools_call_bad_params(self) -> None:
        resp = server.handle(FakeSession(), {"id": 1, "method": "tools/call", "params": "x"})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_tools_call_success_and_errors(self) -> None:
        resp = server.handle(FakeSession(responses={"agent_identity": {}}),
                             {"id": 1, "method": "tools/call",
                              "params": {"name": "whoami", "arguments": {}}})
        self.assertIn("result", resp)
        # identity error -> jsonrpc error
        resp = server.handle(FakeSession(identity=None),
                             {"id": 2, "method": "tools/call",
                              "params": {"name": "whoami", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32001)
        # a validation McpError -> isError text result
        resp = server.handle(FakeSession(),
                             {"id": 3, "method": "tools/call",
                              "params": {"name": "send_message", "arguments": {"to": "", "message": ""}}})
        self.assertTrue(resp["result"]["isError"])

    def test_method_missing_and_unknown(self) -> None:
        self.assertEqual(server.handle(FakeSession(), {"id": 1})["error"]["code"], -32600)
        self.assertEqual(server.handle(FakeSession(), {"id": 1, "method": "x"})["error"]["code"], -32601)


class Main(unittest.TestCase):
    def test_main_loop(self) -> None:
        lines = "\n".join([
            "{bad json",
            json.dumps({"id": 1, "method": "initialize"}),
            "",
            json.dumps({"method": "initialized"}),
        ])
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(lines)), \
                contextlib.redirect_stdout(out):
            rc = server.main([])
        self.assertEqual(rc, 0)
        self.assertIn("parse error", out.getvalue())


if __name__ == "__main__":
    unittest.main()
