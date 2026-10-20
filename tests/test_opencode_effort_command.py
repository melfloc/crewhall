"""``crewhall agent effort``: change a live OpenCode 2 agent's reasoning effort."""
from __future__ import annotations

import unittest
from unittest import mock

from crewhall import Controller, OpenCodeHarness, notices
from crewhall.harness import HarnessError
from crewhall.opencode_link import OpenCodeLink

from .fake_opencode_v2 import PASSWORD, FakeOpenCodeV2, start_server
from .support import FakeSession

SID = "ses_mine"
CATALOG = [{
    "id": "deepseek-v4.1-flash", "modelID": "deepseek-v4.1-flash",
    "providerID": "opencode-go",
    "variants": [{"id": "low"}, {"id": "high"}, {"id": "max"}],
}]


class EffortCommand(unittest.TestCase):
    def setUp(self):
        notices.clear()
        self.addCleanup(notices.clear)
        FakeOpenCodeV2.reset()
        FakeOpenCodeV2.models = list(CATALOG)
        FakeOpenCodeV2.sessions[SID] = {
            "id": SID, "agent": "build",
            "model": {"providerID": "opencode-go", "id": "deepseek-v4.1-flash",
                      "variant": "max"},
            "time": {"created": 1, "updated": 0}, "location": {"directory": "/tmp"},
        }
        self.server, self.port = start_server()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.link = OpenCodeLink(self.port, PASSWORD, api="v2")
        self.controller = Controller(adopt=False)
        self.controller.track_conversations = True
        self.harness = OpenCodeHarness(FakeSession(screen="", name="e1"), name="e1")
        self.harness.conversation_id = SID
        self.harness.link = self.link
        self.controller.register_agent(self.harness)

    def _posted(self):
        return [body for method, path, body in FakeOpenCodeV2.requests
                if method == "POST" and path.endswith(f"/{SID}/model")]

    def test_without_a_value_it_reports_the_current_effort_and_the_options(self):
        info = self.controller.set_agent_effort("e1")
        self.assertEqual(info["effort"], "max")
        self.assertEqual(info["available"], ["low", "high", "max"])
        self.assertEqual(self._posted(), [])

    def test_it_changes_the_variant_and_confirms_it_by_reading_back(self):
        info = self.controller.set_agent_effort("e1", "high")
        self.assertEqual(self._posted()[-1]["model"]["variant"], "high")
        self.assertEqual(info["effort"], "high")

    def test_an_effort_the_model_does_not_offer_is_refused(self):
        with self.assertRaises(HarnessError) as ctx:
            self.controller.set_agent_effort("e1", "ultra")
        self.assertIn("low, high, max", str(ctx.exception))
        self.assertEqual(self._posted(), [])

    def test_default_is_always_allowed_and_is_not_raised_back_to_max(self):
        self.controller.set_agent_effort("e1", "default")
        self.assertEqual(self._posted()[-1]["model"]["variant"], "default")
        FakeOpenCodeV2.requests.clear()
        # The standing policy (default_variant=max) must leave an explicit choice alone.
        self.controller._ensure_default_variant(self.link, SID, agent_id=self.harness.agent_id)
        self.assertEqual(self._posted(), [])

    def test_the_choice_is_pinned_in_the_persisted_session_config(self):
        self.controller.set_agent_effort("e1", "low")
        self.assertTrue(self.controller._v2_config[self.harness.agent_id]["effort_pinned"])

    def test_an_agent_without_an_api_session_gets_a_clear_error(self):
        self.harness.link = None
        with self.assertRaises(HarnessError):
            self.controller.set_agent_effort("e1", "high")


class EffortWiring(unittest.TestCase):
    def test_the_op_is_exposed_to_leads_like_interrupt_and_restart(self):
        from crewhall import daemon
        self.assertIn("agent_effort", daemon._AGENT_AUTH_OPS)

    def test_the_cli_parses_the_command(self):
        from crewhall.cli import build_parser
        args = build_parser().parse_args(["agent", "effort", "Ejecutor_1", "max"])
        self.assertEqual((args.target, args.effort), ("Ejecutor_1", "max"))
        args = build_parser().parse_args(["agent", "effort", "Ejecutor_1"])
        self.assertIsNone(args.effort)


if __name__ == "__main__":
    unittest.main()
