from __future__ import annotations

import os
import tempfile
import unittest

from agent_terminal import Controller, TeamError
from agent_terminal.team import validate_workspace

from .support import FakeHarness


class WorkspaceValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-ws-")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_absolute_dir(self):
        self.assertEqual(validate_workspace(self.tmp), os.path.abspath(self.tmp))

    def test_missing_dir_rejected(self):
        with self.assertRaises(TeamError):
            validate_workspace(os.path.join(self.tmp, "nope"))

    def test_empty_rejected(self):
        with self.assertRaises(TeamError):
            validate_workspace("")

    def test_relative_rejected(self):
        with self.assertRaises(TeamError):
            validate_workspace("relative/dir")


class TeamWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)
        self.tmp = tempfile.mkdtemp(prefix="at-ws-")
        self.other = tempfile.mkdtemp(prefix="at-ws2-")

    def tearDown(self) -> None:
        import shutil

        for p in (self.tmp, self.other):
            shutil.rmtree(p, ignore_errors=True)

    def _agent(self, name):
        h = FakeHarness(name)
        self.controller.register_agent(h)
        return h

    def test_team_stores_workspace(self):
        team = self.controller.create_team("fiscal", [], workspace=self.tmp)
        self.assertEqual(team.workspace, os.path.abspath(self.tmp))
        self.assertEqual(self.controller.team_info(team.team_id)["workspace"], os.path.abspath(self.tmp))

    def test_invalid_workspace_rejected(self):
        with self.assertRaises(TeamError):
            self.controller.create_team("bad", [], workspace="/nope/missing/xyz")

    def test_set_workspace(self):
        team = self.controller.create_team("fiscal", [])
        updated = self.controller.set_team_workspace(team.team_id, self.tmp)
        self.assertEqual(updated.workspace, os.path.abspath(self.tmp))
        cleared = self.controller.set_team_workspace(team.team_id, None)
        self.assertIsNone(cleared.workspace)

    def test_cwd_precedence_explicit_over_workspace(self):
        self.controller.create_team("fiscal", [], workspace=self.tmp)
        self.assertEqual(
            self.controller.resolve_cwd(self.other, "fiscal"), os.path.abspath(self.other)
        )

    def test_cwd_precedence_team_workspace(self):
        self.controller.create_team("fiscal", [], workspace=self.tmp)
        self.assertEqual(
            self.controller.resolve_cwd(None, "fiscal"), os.path.abspath(self.tmp)
        )

    def test_cwd_precedence_default(self):
        team = self.controller.create_team("fiscal", [])
        self.assertIsNone(team.workspace)
        self.assertEqual(self.controller.resolve_cwd(None, "fiscal"), os.getcwd())

    def test_agent_inherits_team_workspace(self):
        self.controller.create_team("fiscal", [], workspace=self.tmp)
        agent = self.controller.create_agent("opencode", name="a", team="fiscal")
        self.assertEqual(agent.session.spec.cwd, os.path.abspath(self.tmp))
        # membership applied
        team = self.controller.get_team("fiscal")
        self.assertIn(agent.agent_id, team.agent_ids)

    def test_agent_explicit_cwd_not_overwritten(self):
        self.controller.create_team("fiscal", [], workspace=self.tmp)
        agent = self.controller.create_agent(
            "opencode", name="a", team="fiscal", cwd=self.other
        )
        self.assertEqual(agent.session.spec.cwd, os.path.abspath(self.other))

    def test_control_file_created_in_agent_cwd_is_the_one_that_agent_reads(self):
        self.controller.create_team("fiscal", [], workspace=self.tmp)
        self.controller.create_agent("opencode", name="a", team="fiscal")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "AGENTS.md")))     # OpenCode reads AGENTS.md
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "CLAUDE.md")))

    def test_claude_agent_gets_claude_md(self):
        from unittest import mock

        from agent_terminal import ClaudeCodeHarness

        self.controller.create_team("fiscal", [], workspace=self.tmp)
        with mock.patch.object(ClaudeCodeHarness, "command", classmethod(lambda cls: ["sleep", "30"])):
            self.controller.create_agent("claude", name="c", team="fiscal", backend="pty")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "CLAUDE.md")))


class WorkspaceReal(unittest.TestCase):
    """Opt-in: workspace inheritance, daemon persistence and UI reconnect."""

    def test_workspace_and_daemon_persistence_real(self) -> None:
        import signal
        import time

        if os.environ.get("AT_RUN_REAL_WORKSPACE") != "1":
            self.skipTest("set AT_RUN_REAL_WORKSPACE=1 to run")
        from agent_terminal import paths
        from agent_terminal.client import Client, ensure_daemon

        cwd = os.path.expanduser("~/.cache/at-coop-demo")
        os.makedirs(cwd, exist_ok=True)
        state = paths.state_path()
        try:
            os.unlink(state)
        except OSError:
            pass

        def wait(pred, t=90):
            d = time.monotonic() + t
            while time.monotonic() < d:
                if pred():
                    return True
                time.sleep(0.5)
            return pred()

        import uuid as _uuid

        team_name = "wsreal-" + _uuid.uuid4().hex[:6]
        agent_name = "opus-" + _uuid.uuid4().hex[:6]
        client = Client()
        client.call("team_create", name=team_name, agent_ids=[], workspace=cwd)
        agent = client.call(
            "agent_create", kind="claude", name=agent_name, backend="tmux",
            cwd=None, team=team_name,
        )["agent"]
        self.assertEqual(agent["cwd"], cwd)
        wait(lambda: client.call("agent_list")["agents"][0]["state"] in ("ready", "waiting_input"), 80)
        aid = agent["agent_id"]

        # Restart the daemon; logical state must survive, same agent_id.
        with open(paths.pid_path()) as fh:
            pid = int(fh.read())
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.6)
        ensure_daemon()
        time.sleep(1)
        agents = client.call("agent_list")["agents"]
        teams = client.call("team_list")["teams"]
        mine_team = next(t for t in teams if t["name"] == team_name)
        self.assertEqual(mine_team["workspace"], cwd)
        mine = [a for a in agents if a["agent_id"] == aid]
        self.assertEqual(len(mine), 1, "agent identity not preserved across restart")
        # Do not shut down the daemon: other tests may share it. It is cleaned
        # up by the test runner's daemon lifecycle handling.


if __name__ == "__main__":
    unittest.main()
