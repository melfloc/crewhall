from __future__ import annotations

import os
import shutil
import unittest
import uuid

from crewhall import AgentState, Controller

RUN = os.environ.get("AT_RUN_OPENCODE") == "1"
HAVE = shutil.which("opencode") is not None
BACKEND = "tmux" if shutil.which("tmux") else "pty"


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_OPENCODE=1 with opencode installed")
class OpenCodeHarnessReal(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)

    def tearDown(self) -> None:
        self.controller.shutdown()

    def _create(self, name: str):
        return self.controller.create_agent(
            "opencode", name=name, backend=BACKEND, cwd=os.getcwd()
        )

    def test_single_agent_semantic_control(self) -> None:
        agent = self._create("solo")
        self.assertEqual(agent.state(), AgentState.STARTING)
        self.assertEqual(agent.start(timeout=40), AgentState.READY)

        token = "HARNESS_" + uuid.uuid4().hex[:6].upper()
        agent.send(f"Reply with exactly the single token: {token}")

        agent.wait_for_state(AgentState.WORKING, timeout=15)
        final = agent.wait_for_state(
            (AgentState.WAITING_INPUT, AgentState.READY), timeout=90
        )
        self.assertIn(final, (AgentState.WAITING_INPUT, AgentState.READY))
        self.assertIn(token, agent.capture())
        self.assertTrue(agent.is_waiting())

    def test_two_agents_are_independent(self) -> None:
        a = self._create("a")
        b = self._create("b")
        a.start(timeout=40)
        b.start(timeout=40)

        token_a = "TOKA_" + uuid.uuid4().hex[:6].upper()
        token_b = "TOKB_" + uuid.uuid4().hex[:6].upper()
        a.send(f"Reply with exactly the single token: {token_a}")
        b.send(f"Reply with exactly the single token: {token_b}")

        a.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)
        b.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)

        cap_a = a.capture()
        cap_b = b.capture()
        self.assertIn(token_a, cap_a)
        self.assertIn(token_b, cap_b)
        self.assertNotIn(token_b, cap_a)
        self.assertNotIn(token_a, cap_b)


if __name__ == "__main__":
    unittest.main()
