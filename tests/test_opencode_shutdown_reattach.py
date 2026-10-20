"""Shutdown without killing agents, and reattaching their OpenCode links.

``daemon stop`` keeps tmux-backed TUIs alive, but the private ``serve`` belongs
to the control plane and is stopped. The next daemon must recover the link for
the surviving agent (adopting a live server or starting one on the persisted
port), and a failed reattach must never be silent.
"""
from __future__ import annotations

import unittest
from unittest import mock

from crewhall import Controller, OpenCodeHarness, notices
from crewhall.opencode_link import OpenCodeLink, free_port

from .fake_opencode_v2 import PASSWORD, FakeOpenCodeV2, start_server
from .support import FakeSession

SID = "ses_mine"


class _Proc:
    pid = 4242

    def poll(self):
        return None


class ShutdownAndReattach(unittest.TestCase):
    def setUp(self):
        notices.clear()
        self.addCleanup(notices.clear)
        patcher = mock.patch("crewhall.watch.notify")
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeOpenCodeV2.reset()

    def _survivor(self, controller, name="sobrevive"):
        controller.track_conversations = True
        harness = OpenCodeHarness(FakeSession(screen="", name=name), name=name)
        harness.session.backend.name = "tmux"  # survives `daemon stop`
        harness.conversation_id = SID
        controller.register_agent(harness)
        return harness

    def test_shutdown_stops_the_event_followers_before_killing_the_serve(self):
        # A follower still running when its serve dies would try to apply the
        # model/agent to a closed port and raise a false "could not apply" notice.
        import threading
        controller = Controller(adopt=False)
        harness = self._survivor(controller)
        stop = threading.Event()
        controller._bridges[harness.agent_id] = stop
        controller._opencode_servers[harness.agent_id] = _Proc()
        seen = []
        with mock.patch.object(
                controller, "_kill_opencode_server",
                lambda proc: seen.append(stop.is_set())):
            controller.shutdown(kill_agents=False)
        self.assertEqual(seen, [True])

    def test_shutdown_without_killing_agents_still_stops_private_serves(self):
        server, port = start_server()
        self.addCleanup(server.server_close)
        controller = Controller(adopt=False)
        harness = self._survivor(controller)
        harness.link = OpenCodeLink(port, PASSWORD, api="v2")
        controller._opencode_links[harness.agent_id] = {
            "port": port, "password": PASSWORD, "api": "v2"}
        proc = _Proc()
        controller._opencode_servers[harness.agent_id] = proc
        killed = []

        def kill(server_proc):
            killed.append(server_proc)
            server.shutdown()

        with mock.patch.object(controller, "_kill_opencode_server", side_effect=kill):
            controller.shutdown(kill_agents=False)

        # Documented behaviour: the TUI survives, the private `serve` does not.
        self.assertEqual(killed, [proc])
        self.assertFalse(harness.session.closed)
        self.assertNotIn(harness.agent_id, controller._opencode_servers)
        with self.assertRaises(Exception):
            OpenCodeLink(port, PASSWORD, api="v2").get_json("/api/info", timeout=0.2)

    def test_the_next_daemon_recovers_the_link_and_reads_the_history(self):
        first, port = start_server()
        FakeOpenCodeV2.sessions[SID] = {
            "id": SID, "time": {"created": 1000},
            "location": {"directory": "/tmp/work"},
        }
        controller = Controller(adopt=False)
        harness = self._survivor(controller)
        harness.link = OpenCodeLink(port, PASSWORD, api="v2")
        controller._opencode_servers[harness.agent_id] = _Proc()
        controller._opencode_links[harness.agent_id] = {
            "port": port, "password": PASSWORD, "api": "v2"}

        def kill(_proc):
            first.shutdown()
            first.server_close()

        with mock.patch.object(controller, "_kill_opencode_server", side_effect=kill):
            controller.shutdown(kill_agents=False)

        # A fresh daemon restores the agent and its persisted link info.
        restarted = Controller(adopt=False)
        adopted = self._survivor(restarted)
        FakeOpenCodeV2.sessions[SID] = {
            "id": SID, "agent": "build",
            "model": {"providerID": "p", "id": "m"},
            "time": {"created": 1000, "updated": 0},
            "location": {"directory": "/tmp/work"},
        }
        FakeOpenCodeV2.messages[SID] = [
            {"id": "m1", "type": "user", "text": "hola", "time": {"created": 1000}},
        ]
        started = []

        def start_server_again(kind, cwd, requested_port, password, identity=None):
            self.assertEqual(requested_port, port)  # the persisted port
            server, _actual = start_server(port)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            started.append(requested_port)
            return _Proc()

        with mock.patch.object(restarted, "_start_opencode_server",
                               side_effect=start_server_again), \
                mock.patch.object(restarted, "_attach_link",
                                  side_effect=lambda h, link: setattr(h, "link", link)):
            restarted._reattach_opencode_link(
                adopted, {"port": port, "password": PASSWORD, "api": "v2"},
                "/tmp/work")

        self.assertEqual(started, [port])
        self.assertIsNotNone(adopted.link)
        self.assertEqual(adopted.link.port, port)
        self.assertEqual(adopted.history()["total"], 1)

    def test_a_dead_v1_link_is_not_attached_silently(self):
        controller = Controller(adopt=False)
        harness = self._survivor(controller)
        port = free_port()  # nobody is listening

        with mock.patch.object(controller, "_attach_link",
                               side_effect=lambda h, link: setattr(h, "link", link)):
            controller._reattach_opencode_link(
                harness, {"port": port, "password": PASSWORD, "api": "v1"}, "/tmp/work")

        self.assertIsNone(harness.link)
        self.assertTrue(getattr(harness, "legacy", False))
        self.assertTrue(any("reattach" in n["title"].lower() for n in notices.pending()))

    def test_a_dead_v2_serve_reports_the_failed_reattach(self):
        controller = Controller(adopt=False)
        harness = self._survivor(controller)
        port = free_port()

        with mock.patch.object(controller, "_attach_link",
                               side_effect=lambda h, link: setattr(h, "link", link)), \
                mock.patch.object(controller, "_start_opencode_server", return_value=None):
            controller._reattach_opencode_link(
                harness, {"port": port, "password": PASSWORD, "api": "v2"}, "/tmp/work")

        self.assertIsNone(harness.link)
        self.assertTrue(any("reattach" in n["title"].lower() for n in notices.pending()))


if __name__ == "__main__":
    unittest.main()
