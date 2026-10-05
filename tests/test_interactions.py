"""Permission prompts and questions raised by an agent's TUI, answered from crewhall."""
from __future__ import annotations

import base64
import os
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_terminal import Controller, OpenCodeHarness
from agent_terminal import interactions as ix
from agent_terminal.opencode_link import OpenCodeLink

from .support import FakeSession

PASSWORD = "s3cret"
SID = "ses_abc123XYZ"
PERM = {"id": "per_1A", "sessionID": SID, "permission": "bash", "patterns": ["rm -rf build"],
        "metadata": {"command": "rm -rf build"}, "always": ["rm *"]}
QUESTION = {"id": "que_1B", "sessionID": SID, "questions": [
    {"question": "¿Qué color?", "header": "Color",
     "options": [{"label": "Rojo", "description": "cálido"}, {"label": "Azul", "description": "frío"}]},
    {"question": "¿Qué extras?", "header": "Extras", "multiple": True, "custom": False,
     "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]},
]}


class FakeServer(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a):
        pass

    def _reply(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self._reply({}, 401)
            return False
        return True

    def do_GET(self):  # noqa: N802
        if not self._authed():
            return
        if self.path == "/permission":
            return self._reply(self.state["perms"])
        if self.path == "/question":
            return self._reply(self.state["questions"])
        self._reply({}, 404)

    def do_POST(self):  # noqa: N802
        if not self._authed():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        parts = self.path.strip("/").split("/")  # permission|question, id, reply|reject
        pool = self.state["perms" if parts[0] == "permission" else "questions"]
        if not any(r["id"] == parts[1] for r in pool):
            return self._reply({"name": "NotFound"}, 404)
        pool[:] = [r for r in pool if r["id"] != parts[1]]
        self.state["posts"].append((self.path, body))
        self._reply(True)


class OpenCodeInteractions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeServer.state.clear()
        FakeServer.state.update(perms=[dict(PERM)], questions=[json.loads(json.dumps(QUESTION))], posts=[])
        self.c = Controller(adopt=False)
        self.h = OpenCodeHarness(FakeSession(screen="", name="o"), name="o")
        self.c.register_agent(self.h)
        self.h.link = OpenCodeLink(self.server.server_address[1], PASSWORD)
        self.h.conversation_id = SID

    def asked(self):
        self.c._on_opencode_event(self.h, {"type": "permission.asked", "properties": PERM})
        self.c._on_opencode_event(self.h, {"type": "question.asked", "properties": QUESTION})
        return {i["kind"]: i for i in self.c.list_interactions(self.h.agent_id)}

    def test_events_open_neutral_requests_visible_in_the_agent_summary(self):
        items = self.asked()
        self.assertEqual(items["permission"]["title"], "bash")
        self.assertIn("rm -rf build", items["permission"]["detail"])
        self.assertEqual(items["question"]["questions"][1]["multiple"], True)
        self.assertEqual(len(self.c.agent_summary(self.h)["interactions"]), 2)
        self.asked()  # the same request again is not duplicated
        self.assertEqual(len(self.c.list_interactions()), 2)

    def test_answering_goes_through_the_opencode_api(self):
        items = self.asked()
        self.c.respond_interaction(items["permission"]["id"], {"reply": "once"}, by="web")
        self.c.respond_interaction(items["question"]["id"], {"answers": [["Azul"], ["A", "B"]]})
        self.assertEqual(FakeServer.state["posts"], [
            ("/permission/per_1A/reply", {"reply": "once"}),
            ("/question/que_1B/reply", {"answers": [["Azul"], ["A", "B"]]}),
        ])
        self.assertEqual(self.c.list_interactions(), [])

    def test_deny_with_a_note_and_dismissing_a_question(self):
        items = self.asked()
        self.c.respond_interaction(items["permission"]["id"], {"reply": "reject", "message": "no"})
        self.c.respond_interaction(items["question"]["id"], {"reject": True})
        self.assertEqual(FakeServer.state["posts"], [
            ("/permission/per_1A/reply", {"reply": "reject", "message": "no"}),
            ("/question/que_1B/reject", {}),
        ])

    def test_invalid_answers_are_refused_before_reaching_the_agent(self):
        items = self.asked()
        q, p = items["question"]["id"], items["permission"]["id"]
        for bad in ({"answers": [["Azul"]]},                    # one answer missing
                    {"answers": [["Azul", "Rojo"], ["A"]]},     # two for a single-choice question
                    {"answers": [["Azul"], ["Z"]]},             # custom not allowed there
                    {"answers": [["Azul"], []]}):
            with self.assertRaises(ix.InteractionError):
                self.c.respond_interaction(q, bad)
        with self.assertRaises(ix.InteractionError):
            self.c.respond_interaction(p, {"reply": "yes please"})
        self.assertEqual(FakeServer.state["posts"], [])
        # Custom text is accepted where the CLI allows it.
        self.c.respond_interaction(q, {"answers": [["Verde"], ["B"]]})

    def test_answered_in_the_tui_closes_the_request_here(self):
        items = self.asked()
        self.c._on_opencode_event(self.h, {"type": "permission.replied", "properties":
                                           {"sessionID": SID, "requestID": "per_1A", "reply": "always"}})
        self.c._on_opencode_event(self.h, {"type": "question.rejected", "properties":
                                           {"sessionID": SID, "requestID": "que_1B"}})
        self.assertEqual(self.c.list_interactions(), [])
        with self.assertRaises(ix.InteractionError):
            self.c.respond_interaction(items["permission"]["id"], {"reply": "once"})

    def test_reconnect_syncs_pending_requests_and_drops_stale_ones(self):
        self.c._on_opencode_event(self.h, {"type": "server.connected", "properties": {}})
        self.assertEqual({i["kind"] for i in self.c.list_interactions()}, {"permission", "question"})
        FakeServer.state["perms"].clear()
        self.c._on_opencode_event(self.h, {"type": "server.connected", "properties": {}})
        self.assertEqual([i["kind"] for i in self.c.list_interactions()], ["question"])

    def test_request_gone_on_the_agent_side_is_reported_and_dropped(self):
        items = self.asked()
        FakeServer.state["perms"].clear()  # answered in the TUI, event not seen
        with self.assertRaises(ix.InteractionError):
            self.c.respond_interaction(items["permission"]["id"], {"reply": "once"})
        self.assertEqual([i["kind"] for i in self.c.list_interactions()], ["question"])

    def test_removing_the_agent_forgets_its_requests(self):
        self.asked()
        self.c.remove_agent(self.h.agent_id, force=True)
        self.assertEqual(self.c.list_interactions(), [])


class Board(unittest.TestCase):
    def test_closed_requests_are_pruned_after_a_while(self):
        board = ix.InteractionBoard()
        item, created = board.open("a", "permission", "a:per_1", {"choices": ["once"]})
        self.assertTrue(created)
        board.close(item.interaction_id, answer={"reply": "once"}, by="web")
        self.assertEqual(board.pending(), [])
        item.closed_at -= ix.KEEP_CLOSED + 1
        board.pending()
        with self.assertRaises(ix.InteractionError):
            board.get("a:per_1")

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ix.InteractionError):
            ix.InteractionBoard().open("a", "elicitation", "x", {})


if __name__ == "__main__":
    unittest.main()


# -- Claude Code: PermissionRequest hook ----------------------------------------
import io  # noqa: E402
import time  # noqa: E402
from unittest import mock  # noqa: E402

from agent_terminal import ClaudeCodeHarness, cli, hooks  # noqa: E402
from agent_terminal.messaging import MessagingError  # noqa: E402

ASK = {"tool_name": "AskUserQuestion", "tool_use_id": "toolu_01ABC",
       "tool_input": {"questions": [
           {"question": "Which color?" + " long" * 600, "header": "Color", "multiSelect": False,
            "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cold"}]},
           {"question": "Extras?", "header": "Extras", "multiSelect": True,
            "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}]}}
BASH = {"tool_name": "Bash", "tool_use_id": "toolu_02DEF",
        "tool_input": {"command": "rm -rf build", "description": "clean"},
        "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash"}],
                                    "behavior": "allow", "destination": "session"}]}


class ClaudePermissions(unittest.TestCase):
    def setUp(self):
        self.c = Controller(adopt=False)
        self.h = ClaudeCodeHarness(FakeSession(screen="", name="c"), name="c")
        self.c.register_agent(self.h)
        self.token = self.c._agent_token(self.h.agent_id)

    def ask(self, payload, wait=5.0):
        """Run the (blocking) hook request in a thread; return (thread, result box)."""
        box = {}
        t = threading.Thread(target=lambda: box.update(self.c.claude_permission_request(
            self.h.agent_id, self.token, payload, wait)), daemon=True)
        t.start()
        deadline = time.monotonic() + 3
        while not self.c.list_interactions() and time.monotonic() < deadline and t.is_alive():
            time.sleep(0.02)
        return t, box

    def answer(self, payload, answer):
        t, box = self.ask(payload)
        item = self.c.list_interactions(self.h.agent_id)[0]
        self.c.respond_interaction(item["id"], answer, by="web")
        t.join(3)
        self.assertFalse(t.is_alive())
        return item, box["decision"]

    def test_bash_permission_allow_once_always_and_deny(self):
        item, decision = self.answer(BASH, {"reply": "once"})
        self.assertEqual(item["title"], "Bash")
        self.assertIn("rm -rf build", item["detail"])
        self.assertEqual(item["choices"], ["once", "always", "reject"])
        self.assertTrue(item["terminal"])
        self.assertEqual(decision, {"behavior": "allow"})
        _, decision = self.answer({**BASH, "tool_use_id": "toolu_03"}, {"reply": "always"})
        self.assertEqual(decision["updatedPermissions"], BASH["permission_suggestions"])
        _, decision = self.answer({**BASH, "tool_use_id": "toolu_04"}, {"reply": "reject", "message": "no"})
        self.assertEqual(decision, {"behavior": "deny", "message": "no"})

    def test_always_is_only_offered_when_claude_suggests_rules(self):
        payload = {k: v for k, v in BASH.items() if k != "permission_suggestions"}
        item, _ = self.answer(payload, {"reply": "once"})
        self.assertEqual(item["choices"], ["once", "reject"])
        t, _ = self.ask({**payload, "tool_use_id": "toolu_05"})
        pending = self.c.list_interactions()[0]
        with self.assertRaises(ix.InteractionError):
            self.c.respond_interaction(pending["id"], {"reply": "always"})
        self.c.respond_interaction(pending["id"], {"terminal": True})
        t.join(3)

    def test_ask_user_question_answers_keyed_by_the_exact_question(self):
        item, decision = self.answer(ASK, {"answers": [["Blue"], ["A", "Something else"]]})
        self.assertTrue(item["questions"][0]["custom"])  # Claude always offers "Other"
        self.assertEqual(decision["behavior"], "allow")
        updated = decision["updatedInput"]
        self.assertEqual(updated["questions"], ASK["tool_input"]["questions"])
        self.assertEqual(updated["answers"], {ASK["tool_input"]["questions"][0]["question"]: "Blue",
                                              "Extras?": "A, Something else"})
        _, decision = self.answer({**ASK, "tool_use_id": "toolu_06"}, {"reject": True})
        self.assertEqual(decision["behavior"], "deny")

    def test_terminal_handoff_and_timeout_let_claude_show_its_dialog(self):
        _, decision = self.answer(BASH, {"terminal": True})
        self.assertIsNone(decision)
        start = time.monotonic()
        out = self.c.claude_permission_request(self.h.agent_id, self.token,
                                               {**BASH, "tool_use_id": "toolu_07"}, 0.3)
        self.assertIsNone(out["decision"])
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.c.list_interactions(), [])  # the card goes away with it

    def test_disabled_unusable_or_unauthorized_requests_never_block(self):
        self.assertIsNone(self.c.claude_permission_request(self.h.agent_id, self.token, BASH, 0)["decision"])
        self.assertIsNone(self.c.claude_permission_request(
            self.h.agent_id, self.token, {"tool_name": "Bash"}, 5)["decision"])
        with self.assertRaises(MessagingError):
            self.c.claude_permission_request(self.h.agent_id, "wrong", BASH, 5)
        self.assertEqual(self.c.list_interactions(), [])


class ClaudeHookWiring(unittest.TestCase):
    def test_settings_declare_a_permission_hook_whose_stdout_reaches_claude(self):
        entry = hooks.claude_settings()["hooks"]["PermissionRequest"][0]["hooks"][0]
        self.assertIn("agent hook permission_request", entry["command"])
        self.assertNotIn(">/dev/null 2>&1", entry["command"])
        self.assertTrue(entry["command"].endswith("|| true"))
        self.assertEqual(entry["timeout"], hooks.PERMISSION_HOOK_TIMEOUT)

    def run_hook(self, response=None, error=None):
        client = mock.Mock()
        client.call.side_effect = error
        client.call.return_value = response
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CREWHALL_AGENT_ID": "a1", "CREWHALL_TOKEN": "t"}), \
                mock.patch.object(cli, "_client", lambda args: client), \
                mock.patch.object(cli, "_hook_payload", lambda: dict(BASH)), \
                mock.patch("sys.stdout", out):
            code = cli.cmd_agent_hook(mock.Mock(event="permission_request"))
        return code, out.getvalue(), client

    def test_cli_prints_claudes_decision(self):
        code, out, client = self.run_hook({"decision": {"behavior": "allow"}})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}})
        self.assertEqual(client.call.call_args.kwargs["payload"], BASH)

    def test_no_decision_or_any_failure_prints_nothing(self):
        self.assertEqual(self.run_hook({"decision": None})[1], "")
        code, out, _ = self.run_hook(error=RuntimeError("daemon down"))
        self.assertEqual((code, out), (0, ""))


class WaitOnlyWhileSomeoneWatches(unittest.TestCase):
    def call(self, seen_ago, env=None):
        from agent_terminal import daemon

        fake = mock.Mock()
        fake._viewer_seen = time.monotonic() - seen_ago
        fake.controller.claude_permission_request.return_value = {"decision": None}
        with mock.patch.dict(os.environ, env or {}, clear=False):
            os.environ.pop("CREWHALL_PERMISSION_WAIT", None) if env is None else None
            daemon.Server._op_agent_permission_request(fake, {"agent": "a", "payload": BASH})
        return fake.controller.claude_permission_request.call_args.kwargs["wait"]

    def test_no_web_viewer_means_no_wait(self):
        from agent_terminal import daemon

        self.assertEqual(self.call(seen_ago=60), 0.0)
        self.assertEqual(self.call(seen_ago=1), daemon.PERMISSION_WAIT)
        self.assertEqual(self.call(seen_ago=1, env={"CREWHALL_PERMISSION_WAIT": "0"}), 0.0)
        self.assertEqual(self.call(seen_ago=1, env={"CREWHALL_PERMISSION_WAIT": "99999"}),
                         daemon.PERMISSION_WAIT_MAX)
