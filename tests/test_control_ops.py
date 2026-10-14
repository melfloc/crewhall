from __future__ import annotations

import unittest

from crewhall.controller import Controller
from crewhall.daemon import Server
from crewhall.messaging import MessagingError
from crewhall.processes import ProcessError
from tests.support import FakeHarness


class ControlBase(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)

    def _agent(self, name):
        h = FakeHarness(name)
        self.controller.register_agent(h)
        return h


class Containment(ControlBase):
    def test_interrupt_reports_and_logs_by(self) -> None:
        a = self._agent("a")
        calls = []
        a.interrupt = lambda: (calls.append("x"), True)[1]
        out = self.controller.interrupt_agent(a.agent_id, by="lead")
        self.assertTrue(out["interrupted"])
        self.assertEqual(calls, ["x"])
        events = a.session.events()
        self.assertTrue(any(e["type"] == "interrupt" and e["data"].get("by") == "lead"
                            for e in events))

    def test_signal_requires_pid_or_all_shells(self) -> None:
        a = self._agent("a")
        with self.assertRaises(ProcessError):
            self.controller.signal_agent(a.agent_id)

    def test_signal_all_shells_signals_each_shell(self) -> None:
        a = self._agent("a")
        self.controller.list_processes = lambda target: {
            "shells": [{"pid": 11}, {"pid": 22}], "others": []}
        seen = []
        self.controller.signal_process = (
            lambda target, pid, sig, by="user": seen.append((pid, sig, by)) or {"pid": pid})
        out = self.controller.signal_agent(a.agent_id, "KILL", all_shells=True, by="lead")
        self.assertEqual(out["count"], 2)
        self.assertEqual(sorted(p for p, _, _ in seen), [11, 22])
        self.assertTrue(all(s == "KILL" and by == "lead" for _, s, by in seen))

    def test_restart_brake_after_limit(self) -> None:
        agent_id = "worker"
        for _ in range(3):
            self.controller._check_restart_brake(agent_id)
            self.controller._record_restart(agent_id)
        with self.assertRaises(MessagingError) as ctx:
            self.controller._check_restart_brake(agent_id)
        self.assertIn("brake", str(ctx.exception))

    def test_agent_cannot_restart_itself(self) -> None:
        a = self._agent("a")
        with self.assertRaises(MessagingError):
            self.controller.force_restart_agent(a.agent_id, by=a.agent_id)


class ControlGuard(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import shutil
        import tempfile
        from unittest import mock

        self.root = tempfile.mkdtemp(prefix="ati-control-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.server = Server(Controller(adopt=False, persist=False),
                             os.path.join(self.root, "r", "d.sock"))

    def test_agent_forms_skip_control_token(self) -> None:
        for op in ("agent_interrupt", "agent_signal", "agent_restart"):
            self.assertIsNone(self.server._guard_origin(
                {"op": op, "sender": "a", "token": "t", "target": "b"}, None), op)

    def test_operator_forms_require_control_token(self) -> None:
        for op in ("agent_interrupt", "agent_signal", "agent_restart"):
            self.assertIsNotNone(self.server._guard_origin({"op": op, "target": "b"}, None), op)
            self.assertIsNone(self.server._guard_origin(
                {"op": op, "target": "b", "_control_token": self.server._control_token}, None), op)


class LaunchResume(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)

    def test_claude_resume_uses_resume_flag(self) -> None:
        cmd = self.controller._launch_command("claude", [], resume="abc-123")
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc-123")
        self.assertNotIn("--session-id", cmd)

    def test_opencode_resume_uses_session_flag(self) -> None:
        cmd = self.controller._launch_command("opencode", [], resume="ses_abc")
        self.assertIn("--session", cmd)
        self.assertEqual(cmd[cmd.index("--session") + 1], "ses_abc")

    def test_codex_resume_uses_subcommand(self) -> None:
        cmd = self.controller._launch_command("codex", [], resume="uuid-1")
        self.assertEqual(cmd[:3], ["codex", "resume", "uuid-1"])

    def test_no_resume_is_a_normal_launch(self) -> None:
        cmd = self.controller._launch_command("claude", [], conversation_id="new-id")
        self.assertIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)


if __name__ == "__main__":
    unittest.main()
