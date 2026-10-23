"""Adopted OpenCode v1 agents: backfill, runtime version and the legacy fallback.

An agent that survives a daemon restart (or predates v2) has no persisted link.
Its TUI still owns a v1 server on ``--port``; crewhall must read the process'
argv/environ, adopt that server, recover the newest conversation of its cwd and
report the version of *that* process instead of the configured v2 binary.
"""
from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from crewhall import Controller, OpenCodeHarness, notices
from crewhall.harness import versions
from crewhall.opencode_link import free_port

from .support import FakeSession

PASSWORD = "v1-test-password"


class FakeOpenCodeV1(BaseHTTPRequestHandler):
    sessions: list[dict] = []

    def log_message(self, *args):  # noqa: D401 - silence the test log
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/session":
            self._json(type(self).sessions)
        else:
            # No /api/info: this is a v1 server.
            self.send_response(404)
            self.end_headers()


def start_v1_server() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenCodeV1)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class AdoptedV1Case(unittest.TestCase):
    def setUp(self):
        self.server, self.port = start_v1_server()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        FakeOpenCodeV1.sessions = []
        self.cwd = tempfile.mkdtemp(prefix="oc-v1-")
        self.addCleanup(shutil.rmtree, self.cwd, True)
        self.controller = Controller(adopt=False)
        self.controller.track_conversations = True
        self.harness = OpenCodeHarness(FakeSession(screen="", name="viejo"), name="viejo")
        self.harness.session.pid = 4242
        self.controller.register_agent(self.harness)
        self.child = 987654
        notices.clear()
        self.addCleanup(notices.clear)
        patcher = mock.patch("crewhall.watch.notify")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _backfill(self, port: int | None = None) -> None:
        port = port or self.port
        cmdline = f"/opt/opencode/1.18.31/bin/opencode\0--port\0{port}\0".encode()
        environ = f"OPENCODE_SERVER_PASSWORD={PASSWORD}\0".encode()
        real_open = open

        def fake_open(path, *args, **kwargs):
            if path == f"/proc/{self.child}/cmdline":
                return io.BytesIO(cmdline)
            if path == f"/proc/{self.child}/environ":
                return io.BytesIO(environ)
            return real_open(path, *args, **kwargs)

        def attach(harness, link):
            harness.link = link

        with mock.patch("builtins.open", side_effect=fake_open), \
                mock.patch.object(self.controller, "_proc_descendants",
                                  return_value=[self.child]), \
                mock.patch.object(self.controller, "_attach_link", side_effect=attach), \
                mock.patch.object(versions, "probe_version", return_value="1.18.31"):
            self.controller._backfill_opencode_legacy(self.harness, self.cwd)

    def _configured_v2(self) -> None:
        report = versions.VersionReport(
            "opencode", ("opencode",), "2.0.29", (2, 0, 29), True, "2.0.29",
            "within the validated range")
        with mock.patch.object(self.controller, "_resolve_provider_command",
                              return_value=(["opencode"], report, False)), \
                mock.patch.object(versions, "evaluate", return_value=report):
            self.controller._attach_version(self.harness, "opencode", None)


class Backfill(AdoptedV1Case):
    def test_adopted_v1_recovers_link_api_and_newest_conversation(self):
        FakeOpenCodeV1.sessions = [
            {"id": "ses_old", "directory": self.cwd,
             "time": {"created": 1000}},
            {"id": "ses_new", "directory": self.cwd,
             "time": {"created": 5000}},
        ]
        self._backfill()
        self.assertIsNotNone(self.harness.link)
        self.assertEqual(self.harness.link.api, "v1")
        self.assertEqual(self.harness.link.port, self.port)
        self.assertEqual(self.harness.link.password, PASSWORD)
        self.assertEqual(self.harness.conversation_id, "ses_new")

    def test_runtime_version_comes_from_the_adopted_process(self):
        FakeOpenCodeV1.sessions = [
            {"id": "ses_new", "directory": self.cwd,
             "time": {"created": 5000}},
        ]
        self._configured_v2()
        self._backfill()
        with mock.patch.object(self.controller, "_activity", return_value={}):
            summary = self.controller.agent_summary(self.harness)
        self.assertEqual(summary["runtime_version"], "opencode v1.18.31")
        self.assertNotEqual(summary["runtime_version"], "opencode v2.0.29")

    def test_dead_v1_server_leaves_the_agent_legacy_with_a_notice(self):
        dead = free_port()
        self._backfill(port=dead)
        self.assertTrue(getattr(self.harness, "legacy", False))
        self.assertIsNone(self.harness.link)
        self.assertTrue(any("legacy" in n["title"].lower() for n in notices.pending()))


if __name__ == "__main__":
    unittest.main()
