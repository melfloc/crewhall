from __future__ import annotations

import base64
import json
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_terminal import Controller, OpenCodeHarness
from agent_terminal.harness import AgentState
from agent_terminal.opencode_link import OpenCodeLink, free_port, map_messages

from .support import FakeSession

PASSWORD = "s3cret"
SID = "ses_abc123XYZ"
MESSAGES = [
    {"info": {"role": "user", "time": {"created": 1791014874824}},
     "parts": [{"type": "text", "text": "hola"}]},
    {"info": {"role": "assistant", "time": {"created": 1791014874854}},
     "parts": [{"type": "step-start"}, {"type": "reasoning", "text": "pensando"},
               {"type": "text", "text": "## Hecho\n- uno"},
               {"type": "tool", "tool": "bash",
                "state": {"input": {"command": "ls"}, "output": "a\nb"}},
               {"type": "step-finish"}]},
]
SESSIONS = [
    {"id": "ses_late", "directory": "/tmp", "time": {"created": 5000}},
    {"id": "ses_first", "directory": "/tmp", "time": {"created": 3000}},
    {"id": "ses_other_dir", "directory": "/var", "time": {"created": 3100}},
    {"id": "ses_before_start", "directory": "/tmp", "time": {"created": 100}},
]
EVENTS = [
    {"type": "server.connected", "properties": {}},
    {"type": "session.created", "properties": {"sessionID": SID, "info": {"id": SID}}},
    {"type": "session.status", "properties": {"sessionID": SID, "status": {"type": "busy"}}},
    {"type": "session.idle", "properties": {"sessionID": SID}},
]


class FakeOpenCode(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _authed(self):
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_GET(self):  # noqa: N802
        if not self._authed():
            return
        if self.path == f"/session/{SID}/message":
            body = json.dumps(MESSAGES).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/session":
            body = json.dumps(SESSIONS).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/event":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for ev in EVENTS:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            self.wfile.flush()
        else:
            self.send_response(404)
            self.end_headers()


class OpenCodeLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenCode)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_map_messages_builds_structured_blocks(self):
        entries = map_messages(MESSAGES)
        self.assertEqual([e["role"] for e in entries], ["user", "assistant"])
        kinds = [b["type"] for b in entries[1]["blocks"]]
        self.assertEqual(kinds, ["text", "tool_use", "tool_result"])  # reasoning dropped
        self.assertEqual(entries[1]["blocks"][1]["name"], "bash")
        self.assertTrue(entries[0]["at"].startswith("2026-"))

    def test_history_requires_password_and_valid_session(self):
        good = OpenCodeLink(self.port, PASSWORD)
        self.assertEqual(good.history(SID)["total"], 2)
        self.assertFalse(OpenCodeLink(self.port, "wrong").history(SID)["available"])
        self.assertFalse(good.history("../etc/passwd")["available"])
        self.assertFalse(good.history("ses_nope")["available"])  # 404 -> unavailable

    def test_events_drive_session_id_and_turn_signals(self):
        c = Controller(adopt=False)
        h = OpenCodeHarness(FakeSession(screen="", name="o"), name="o")
        c.register_agent(h)
        link = OpenCodeLink(self.port, PASSWORD)
        for event in link.events(lambda: False):
            c._on_opencode_event(h, event)
        self.assertEqual(h.conversation_id, SID)
        self.assertGreater(h._stop_at, 0)
        self.assertEqual(c._hook_events[h.agent_id]["event"], "stop")

    def test_harness_history_goes_through_the_link(self):
        h = OpenCodeHarness(FakeSession(screen="", name="o"), name="o")
        self.assertFalse(h.history()["available"])
        h.link, h.conversation_id = OpenCodeLink(self.port, PASSWORD), SID
        self.assertEqual(h.history()["messages"][1]["blocks"][0]["text"], "## Hecho\n- uno")

    def test_stop_event_completes_the_turn(self):
        screen = '  Build · m\n  Ask anything… "x"\n  ctrl+p commands'
        h = OpenCodeHarness(FakeSession(screen=screen, name="o"), name="o")
        h.send("hola")
        self.assertEqual(h.state(), AgentState.UNKNOWN)
        h.on_hook("stop")
        self.assertEqual(h.state(), AgentState.WAITING_INPUT)


class SessionDiscovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OpenCodeLinkTest.setUpClass.__func__(OpenCodeLinkTest)
        cls.port = OpenCodeLinkTest.port

    @classmethod
    def tearDownClass(cls):
        OpenCodeLinkTest.tearDownClass.__func__(OpenCodeLinkTest)

    def test_picks_earliest_session_in_dir_after_start_not_claimed(self):
        link = OpenCodeLink(self.port, PASSWORD)
        self.assertEqual(link.discover("/tmp", 3000, set()), "ses_first")
        self.assertEqual(link.discover("/tmp", 3000, {"ses_first"}), "ses_late")
        self.assertIsNone(link.discover("/tmp", 8000, set()))
        self.assertIsNone(link.discover("/nowhere", 0, set()))
        self.assertIsNone(OpenCodeLink(self.port, "bad").discover("/tmp", 0, set()))

    def test_history_resolves_a_missed_creation_event(self):
        h = OpenCodeHarness(FakeSession(screen="", name="o"), name="o")
        h.link = OpenCodeLink(self.port, PASSWORD)
        h.conversation_resolver = lambda: SID
        self.assertEqual(h.history()["total"], 2)
        self.assertEqual(h.conversation_id, SID)


class OpenCodeLaunchWiring(unittest.TestCase):
    def test_port_and_password_and_user_overrides(self):
        c = Controller(adopt=False)
        c.track_conversations = True
        link = c._new_opencode_link("opencode", [])
        self.assertTrue(link.password and link.port)
        self.assertEqual(
            c._launch_command("opencode", ["--agent", "x"], None, link.port),
            ["opencode", "--port", str(link.port), "--agent", "x"],
        )
        self.assertIsNone(c._new_opencode_link("opencode", ["--port", "1234"]))
        self.assertIsNone(c._new_opencode_link("claude", []))
        c.track_conversations = False
        self.assertIsNone(c._new_opencode_link("opencode", []))

    def test_free_port_is_usable(self):
        self.assertGreater(free_port(), 1023)


if __name__ == "__main__":
    unittest.main()


class BusyDedup(unittest.TestCase):
    def test_repeated_busy_counts_once_until_idle(self):
        c = Controller(adopt=False)
        h = OpenCodeHarness(FakeSession(screen="", name="o"), name="o")
        c.register_agent(h)
        busy = {"type": "session.status", "properties": {"sessionID": SID, "status": {"type": "busy"}}}
        calls = []
        h.on_hook = lambda event, at=None: calls.append(event)
        for _ in range(4):
            c._on_opencode_event(h, busy)
        c._on_opencode_event(h, {"type": "session.idle", "properties": {"sessionID": SID}})
        c._on_opencode_event(h, busy)
        self.assertEqual(calls, ["prompt_submit", "stop", "prompt_submit"])


class NewSession(unittest.TestCase):
    """``/new`` in OpenCode starts another root session in the same process."""

    def setUp(self):
        self.c = Controller(adopt=False)
        self.h = OpenCodeHarness(FakeSession(screen="", name="o"), name="o")
        self.c.register_agent(self.h)
        self.h.conversation_id = SID

    def created(self, sid, parent=None):
        info = {"id": sid, **({"parentID": parent} if parent else {})}
        return {"type": "session.created", "properties": {"sessionID": sid, "info": info}}

    def test_new_root_session_replaces_the_conversation(self):
        self.c._on_opencode_event(self.h, self.created("ses_new1"))
        self.assertEqual(self.h.conversation_id, "ses_new1")

    def test_subagent_session_is_ignored_including_its_turn_events(self):
        calls = []
        self.h.on_hook = lambda event, at=None: calls.append(event)
        self.c._on_opencode_event(self.h, self.created("ses_child", parent=SID))
        self.c._on_opencode_event(self.h, {"type": "session.idle",
                                           "properties": {"sessionID": "ses_child"}})
        self.assertEqual(self.h.conversation_id, SID)
        self.assertEqual(calls, [])

    def test_activity_in_an_unknown_root_session_is_followed(self):
        self.h.link = mock.Mock()
        self.h.link.get_json.return_value = {"id": "ses_old2"}
        busy = {"type": "session.status",
                "properties": {"sessionID": "ses_old2", "status": {"type": "busy"}}}
        self.c._on_opencode_event(self.h, busy)
        self.assertEqual(self.h.conversation_id, "ses_old2")
        self.h.link.get_json.assert_called_once_with("/session/ses_old2")
