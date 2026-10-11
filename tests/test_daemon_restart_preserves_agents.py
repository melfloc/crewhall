from __future__ import annotations

import argparse
import shutil
import unittest
from unittest import mock

from crewhall.backends import tmux as tmux_backend
from crewhall.controller import Controller
from crewhall.types import SessionSpec

sleep_forever = ["/bin/sh", "-c", "sleep 300"]


def _tmux_sessions() -> set[str]:
    return {s["session_id"] for s in tmux_backend.existing_sessions()}


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, op: str, **kwargs):
        self.calls.append((op, kwargs))
        return {}


class DaemonStopCli(unittest.TestCase):
    """The CLI decides the two semantics of `daemon stop`."""

    def _run(self, kill_agents: bool) -> dict:
        from crewhall import cli

        client = _FakeClient()
        args = argparse.Namespace(action="stop", kill_agents=kill_agents)
        with mock.patch.object(cli, "_client", return_value=client):
            rc = cli.cmd_daemon(args)
        self.assertEqual(rc, 0)
        op, kwargs = client.calls[0]
        self.assertEqual(op, "shutdown")
        return kwargs

    def test_stop_keeps_agents_by_default(self) -> None:
        self.assertFalse(self._run(kill_agents=False).get("kill_agents"))

    def test_stop_kill_agents_stops_them(self) -> None:
        self.assertTrue(self._run(kill_agents=True).get("kill_agents"))


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class DaemonRestartPreservesAgents(unittest.TestCase):
    """Fase A1: `daemon stop` must not kill tmux-backed agents."""

    def test_stop_without_kill_keeps_agents_and_readopts_them(self) -> None:
        first = Controller(adopt=False)
        session = first.create(SessionSpec(command=sleep_forever), backend_name="tmux")
        sid = session.session_id
        self.assertTrue(session.status.alive)

        # The new default for `daemon stop`: control plane goes down, agents stay.
        first.shutdown(kill_agents=False)
        self.assertIn(sid, _tmux_sessions())

        # A fresh daemon adopts the surviving session under the same id.
        second = Controller(adopt=True)
        adopted = second.get(sid)
        self.assertEqual(adopted.session_id, sid)
        self.assertTrue(adopted.status.alive)

        second.shutdown(kill_agents=True)
        self.assertNotIn(sid, _tmux_sessions())

    def test_stop_with_kill_agents_stops_them(self) -> None:
        controller = Controller(adopt=False)
        session = controller.create(SessionSpec(command=sleep_forever), backend_name="tmux")
        sid = session.session_id
        self.assertIn(sid, _tmux_sessions())

        controller.shutdown(kill_agents=True)
        self.assertNotIn(sid, _tmux_sessions())


if __name__ == "__main__":
    unittest.main()
