"""OpenCode v2: crewhall applies --model/--agent through the server API.

A real v2 TUI rejects both flags, so crewhall creates the session and switches
the model/agent via the API before the agent is usable, re-applies them on
restart, and never leaves a silently ignored flag or an unapplied setting: it
records a notice (modal + desktop), a summary field and ``agent status``.
"""
from __future__ import annotations

import base64
import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from crewhall import Controller, notices
from crewhall.commands.agents import _print_status
from crewhall.opencode_link import OpenCodeLink

from .support import FakeHarness

PASSWORD = "s3cret"
DEFAULT_PROVIDER = "prov"


class FakeV2Server(BaseHTTPRequestHandler):
    """A tiny OpenCode v2 server: session create + model/agent switch."""

    sessions: dict[str, dict] = {}
    requests: list[tuple[str, str, dict]] = []
    fail_agents: set[str] = set()
    default_model: dict | None = {"providerID": DEFAULT_PROVIDER, "id": "fallback-model"}
    catalog: list[dict] = []

    def log_message(self, *a):  # noqa: ANN002
        pass

    def _authed(self) -> bool:
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def _json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802
        if not self._authed():
            return
        path = self.path.split("?")[0]
        if path == "/api/info":
            self._json({"version": "2.0.28", "pid": 1})
        elif path == "/api/model":
            self._json({"data": type(self).catalog})
        elif path == "/api/model/default":
            self._json({"data": type(self).default_model})
        elif path.startswith("/api/session/"):
            sid = path.rsplit("/", 1)[1]
            session = type(self).sessions.get(sid)
            if session is None:
                self.send_response(404)
                self.end_headers()
            else:
                self._json({"data": session})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if not self._authed():
            return
        path = self.path.split("?")[0]
        body = self._body()
        type(self).requests.append(("POST", path, body))
        if path == "/api/session":
            sid = f"ses_created{len(type(self).sessions) + 1}"
            session: dict = {"id": sid}
            if body.get("model"):
                session["model"] = dict(body["model"])
            elif type(self).default_model:
                session["model"] = dict(type(self).default_model)
            type(self).sessions[sid] = session
            self._json({"data": type(self).sessions[sid]})
        elif path.endswith("/model"):
            sid = path.split("/")[3]
            session = type(self).sessions.setdefault(sid, {"id": sid})
            session["model"] = dict(body.get("model") or {})
            self.send_response(204)
            self.end_headers()
        elif path.endswith("/agent"):
            sid = path.split("/")[3]
            agent = str(body.get("agent") or "")
            if agent in type(self).fail_agents:
                self.send_response(404)
                self.end_headers()
            else:
                type(self).sessions.setdefault(sid, {"id": sid})["agent"] = agent
                self.send_response(204)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


class V2ModelBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeV2Server)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeV2Server.sessions = {}
        FakeV2Server.requests = []
        FakeV2Server.fail_agents = {"bogus"}
        FakeV2Server.default_model = {"providerID": DEFAULT_PROVIDER, "id": "fallback-model"}
        FakeV2Server.catalog = []
        self.controller = Controller(adopt=False)
        self.link = OpenCodeLink(self.port, PASSWORD, api="v2")
        notices.clear()
        self.addCleanup(notices.clear)
        patcher = mock.patch("crewhall.watch.notify")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _configure(self, args, session_id=None, agent="a1"):
        return self.controller._configure_v2_session(
            agent, "opencode", self.link, args, session_id=session_id)


class V2AppliesModelAndAgent(V2ModelBase):
    def test_creates_session_then_switches_model_and_agent(self):
        report = self._configure(["--model", "acme/fast", "--agent", "builder"])
        self.assertEqual(report["model"], "acme/fast")
        self.assertTrue(report["model_applied"])
        self.assertTrue(report["agent_applied"])
        self.assertTrue(report["session_id"])
        paths = [path for _m, path, _b in FakeV2Server.requests]
        self.assertIn("/api/session", paths)
        # The model rides the create call (the TUI reads it from there); the
        # agent is switched on the created session.
        create_body = next(b for _m, p, b in FakeV2Server.requests if p == "/api/session")
        self.assertEqual(create_body["model"], {"providerID": "acme", "id": "fast"})
        self.assertIn(f"/api/session/{report['session_id']}/agent", paths)
        stored = FakeV2Server.sessions[report["session_id"]]
        self.assertEqual(stored["model"], {"providerID": "acme", "id": "fast"})
        self.assertEqual(stored["agent"], "builder")
        self.assertEqual(report["running_model"], "acme/fast")
        self.assertEqual(report["ignored_flags"], [])

    def test_run_without_flags_creates_a_plain_session(self):
        report = self._configure([])
        self.assertIsNone(report["model"])
        self.assertTrue(report["session_id"])
        self.assertEqual(report["ignored_flags"], [])

    def test_resume_switches_in_place(self):
        FakeV2Server.sessions["ses_old"] = {
            "id": "ses_old", "model": {"providerID": "x", "id": "old"},
        }
        report = self._configure(["--model", "acme/new"], session_id="ses_old")
        self.assertEqual(report["session_id"], "ses_old")
        paths = [path for _m, path, _b in FakeV2Server.requests]
        self.assertNotIn("/api/session", paths)  # reused, not recreated
        self.assertIn("/api/session/ses_old/model", paths)
        self.assertEqual(report["running_model"], "acme/new")

    def test_model_alias_is_canonicalised_via_the_catalog(self):
        FakeV2Server.catalog = [
            {"id": "fast", "modelID": "fast", "providerID": "acme-go", "canonical": "acme-go"},
        ]
        report = self._configure(["--model", "acme/fast"])
        self.assertTrue(report["model_applied"])
        stored = FakeV2Server.sessions[report["session_id"]]
        self.assertEqual(stored["model"], {"providerID": "acme-go", "id": "fast"})

    def test_default_model_from_provider_settings(self):
        with mock.patch("crewhall.services.agents.settings.provider",
                        return_value={"default_model": "acme/from-config"}):
            report = self._configure([])
        self.assertEqual(report["model"], "acme/from-config")
        self.assertTrue(report["model_applied"])


class V2NeverSilent(V2ModelBase):
    def test_unresolvable_model_records_a_notice_and_report(self):
        FakeV2Server.default_model = None  # nothing to draw a provider from
        report = self._configure(["--model", "bare-name"])
        self.assertFalse(report["model_applied"])
        self.assertIn("bare-name", report["error"])
        self.assertEqual(report["ignored_flags"], ["--model"])
        titles = [n["title"] for n in notices.pending()]
        self.assertTrue(any("could not apply" in t for t in titles), titles)
        self.assertTrue(any("ignored some flags" in t for t in titles), titles)

    def test_failed_agent_is_listed_as_ignored(self):
        report = self._configure(["--model", "acme/fast", "--agent", "bogus"])
        self.assertTrue(report["model_applied"])  # model still applied
        self.assertFalse(report["agent_applied"])
        self.assertEqual(report["ignored_flags"], ["--agent"])
        bodies = " ".join(n["body"] for n in notices.pending())
        self.assertIn("bogus", bodies)

    def test_agent_summary_exposes_the_config(self):
        h = FakeHarness("v2sum")
        self.controller.register_agent(h)
        self.controller._v2_config[h.agent_id] = {
            "model": "acme/fast", "running_model": None, "model_applied": False,
            "agent": None, "agent_applied": None, "session_id": "ses_x",
            "ignored_flags": ["--model"], "error": "boom",
        }
        with mock.patch.object(Controller, "_activity",
                               return_value={"model": "acme/other", "label": "idle"}):
            summary = self.controller.agent_summary(h)
        self.assertEqual(summary["session_config"]["model"], "acme/fast")
        self.assertEqual(summary["session_config"]["running_model"], "acme/other")

    def test_status_line_reads_requested_and_running(self):
        status = {
            "name": "v2", "agent_id": "a1", "kind": "opencode", "state": "ready",
            "alive": True, "backend": "tmux", "host": None, "activity": {},
            "interactions": [], "queue": {}, "deliverable_now": True, "blocked_by": [],
            "processes": {"available": True, "shells": [], "others": []},
            "subagents": [],
            "session_config": {
                "model": "acme/wanted", "running_model": "acme/actual",
                "model_applied": False, "agent": "builder", "agent_applied": True,
                "ignored_flags": ["--model"],
            },
        }
        out = io.StringIO()
        with redirect_stdout(out):
            _print_status(status)
        text = out.getvalue()
        self.assertIn("model requested acme/wanted, running acme/actual", text)
        self.assertIn("agent: builder (applied)", text)
        self.assertIn("ignored in v2: --model", text)


class V2Wiring(unittest.TestCase):
    def setUp(self):
        self.controller = Controller(adopt=False)

    def test_launch_command_drops_flags_and_uses_the_session(self):
        command = self.controller._launch_command(
            "opencode", ["--model", "acme/fast", "--agent", "b"], None, 4242,
            server_url="http://127.0.0.1:4242", resume="ses_created1",
        )
        joined = " ".join(command)
        self.assertNotIn("--model", joined)
        self.assertNotIn("--agent", joined)
        self.assertIn("--server http://127.0.0.1:4242", joined)
        self.assertIn("--session ses_created1", joined)

    def test_adopting_a_session_applies_pending_config(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeV2Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        FakeV2Server.sessions = {}
        FakeV2Server.requests = []
        FakeV2Server.fail_agents = set()
        FakeV2Server.default_model = {"providerID": DEFAULT_PROVIDER, "id": "fallback-model"}
        FakeV2Server.catalog = []
        notices.clear()
        self.addCleanup(notices.clear)
        patcher = mock.patch("crewhall.watch.notify")
        patcher.start()
        self.addCleanup(patcher.stop)

        h = FakeHarness("pending")
        h.link = OpenCodeLink(server.server_address[1], PASSWORD, api="v2")
        self.controller.register_agent(h)
        self.controller._agent_args[h.agent_id] = ["--model", "acme/fast"]
        self.controller._v2_config[h.agent_id] = {}
        self.assertTrue(self.controller._adopt_conversation(h, "ses_adopted1"))
        paths = [path for _m, path, _b in FakeV2Server.requests]
        self.assertIn("/api/session/ses_adopted1/model", paths)


if __name__ == "__main__":
    unittest.main()
