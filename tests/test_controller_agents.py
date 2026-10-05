from __future__ import annotations

import unittest

from crewhall import Controller, OpenCodeHarness
from crewhall.controller import AgentNotFound

from .support import FakeSession

SCREEN = '  Build · model\n  Ask anything… "x"\n  ctrl+p commands'


class ControllerAgents(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)

    def _register(self, name: str) -> OpenCodeHarness:
        harness = OpenCodeHarness(FakeSession(screen=SCREEN, name=name), name=name)
        self.controller.register_agent(harness)
        return harness

    def test_register_and_list(self) -> None:
        self._register("alpha")
        self._register("beta")
        agents = self.controller.list_agents()
        self.assertEqual({a["name"] for a in agents}, {"alpha", "beta"})
        self.assertEqual({a["kind"] for a in agents}, {"opencode"})

    def test_resolve_by_name_and_id(self) -> None:
        alpha = self._register("alpha")
        self.assertIs(self.controller.get_agent("alpha"), alpha)
        self.assertIs(self.controller.get_agent(alpha.agent_id), alpha)

    def test_resolve_unknown_raises(self) -> None:
        with self.assertRaises(AgentNotFound):
            self.controller.get_agent("ghost")

    def test_resolve_ambiguous_prefix_raises(self) -> None:
        self._register("alpha")
        self._register("beta")
        with self.assertRaises(AgentNotFound):
            self.controller.get_agent("sess_fake")

    def test_remove_stops_and_unregisters(self) -> None:
        alpha = self._register("alpha")
        info = self.controller.remove_agent("alpha")
        self.assertEqual(info.name, "alpha")
        self.assertTrue(alpha.session.closed)
        self.assertEqual(self.controller.list_agents(), [])


if __name__ == "__main__":
    unittest.main()
