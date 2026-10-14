from __future__ import annotations

import unittest

from crewhall import AgentState
from crewhall.controller import Controller
from crewhall.daemon import Server
from crewhall.messaging import MessagingError
from tests.support import FakeHarness


class WorkingHarness(FakeHarness):
    def _detect(self, text: str) -> tuple[AgentState, str]:
        return AgentState.WORKING, "fake harness is working"


class StatusBase(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)

    def _agent(self, name, cls=FakeHarness):
        h = cls(name)
        self.controller.register_agent(h)
        return h


class AgentStatus(StatusBase):
    def test_ready_agent_is_deliverable(self) -> None:
        a = self._agent("a")
        self._agent("b")
        self.controller.create_team("crew", ["a", "b"])
        st = self.controller.agent_status(a.agent_id)
        self.assertEqual(st["state"], "ready")
        self.assertTrue(st["deliverable_now"])
        self.assertEqual(st["blocked_by"], [])
        self.assertFalse(st["human_draft"])
        self.assertEqual(st["queue"]["count"], 0)
        self.assertIn("activity", st)
        self.assertIn("processes", st)

    def test_working_agent_is_blocked(self) -> None:
        a = self._agent("a", WorkingHarness)
        st = self.controller.agent_status(a.agent_id)
        self.assertEqual(st["state"], "working")
        self.assertFalse(st["deliverable_now"])
        self.assertIn("working", st["blocked_by"])

    def test_human_draft_is_reported_without_its_text(self) -> None:
        a = self._agent("a")
        a.input_is_clean = lambda: False  # type: ignore[method-assign]
        st = self.controller.agent_status(a.agent_id)
        self.assertTrue(st["human_draft"])
        self.assertIn("human_draft", st["blocked_by"])
        self.assertNotIn("SECRET-DRAFT", str(st))

    def test_pending_interaction_blocks(self) -> None:
        a = self._agent("a")
        self.controller.interactions.open(
            a.agent_id, "permission", f"{a.agent_id}:1",
            {"title": "run", "choices": ["once", "reject"]},
        )
        st = self.controller.agent_status(a.agent_id)
        self.assertIn("permission_pending", st["blocked_by"])
        self.assertEqual(len(st["interactions"]), 1)

    def test_unsupported_cli_version_blocks(self) -> None:
        a = self._agent("a")
        a.version_blocked = True
        self.controller._agent_versions[a.agent_id] = {
            "kind": "opencode", "version": "2.0.0", "supported": False,
            "reason": "outside the validated range (last verified 1.18.34)",
            "blocked": True,
        }
        st = self.controller.agent_status(a.agent_id)
        self.assertIn("unsupported_cli_version", st["blocked_by"])
        self.assertFalse(st["deliverable_now"])
        self.assertEqual(st["cli"]["version"], "2.0.0")
        self.assertIn("reason", st["cli"])


class StatusAuthorization(StatusBase):
    def setUp(self) -> None:
        super().setUp()
        self.lead = self._agent("lead")
        self.worker = self._agent("worker")
        self.outsider = self._agent("outsider")
        self.controller.create_team("crew", ["lead", "worker"], leads=["lead"])
        self.controller.create_team("other", ["outsider"], leads=["outsider"])

    def test_teammate_can_see_status(self) -> None:
        self.controller._authorize_team(self.worker, self.lead)  # shared team ok

    def test_outsider_cannot_see_status(self) -> None:
        with self.assertRaises(MessagingError):
            self.controller._authorize_team(self.outsider, self.worker)

    def test_only_lead_may_request_output(self) -> None:
        self.assertTrue(self.controller._lead_of(self.lead, self.worker))
        self.assertFalse(self.controller._lead_of(self.worker, self.lead))


class StatusGuardOrigin(unittest.TestCase):
    def setUp(self) -> None:
        import os
        import shutil
        import tempfile
        from unittest import mock

        self.root = tempfile.mkdtemp(prefix="ati-status-", dir="/tmp")
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

    def test_agent_form_needs_no_control_token(self) -> None:
        self.assertIsNone(self.server._guard_origin(
            {"op": "agent_status", "sender": "a", "token": "t", "target": "b"}, None))

    def test_operator_form_requires_control_token(self) -> None:
        self.assertIsNotNone(self.server._guard_origin(
            {"op": "agent_status", "target": "b"}, None))
        self.assertIsNone(self.server._guard_origin(
            {"op": "agent_status", "target": "b",
             "_control_token": self.server._control_token}, None))


if __name__ == "__main__":
    unittest.main()
