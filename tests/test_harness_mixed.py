from __future__ import annotations

import os
import shutil
import unittest

from agent_terminal import AgentState, Controller

RUN = (
    os.environ.get("AT_RUN_CLAUDE") == "1"
    and os.environ.get("AT_RUN_OPENCODE") == "1"
)
HAVE = shutil.which("claude") is not None and shutil.which("opencode") is not None
BACKEND = "tmux" if shutil.which("tmux") else "pty"


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_CLAUDE=1 and AT_RUN_OPENCODE=1")
class MixedRealAgents(unittest.TestCase):
    def test_opencode_and_claude_coexist_independently(self) -> None:
        controller = Controller(adopt=False)
        a = controller.create_agent("opencode", name="A", backend=BACKEND, cwd=os.getcwd())
        b = controller.create_agent("claude", name="B", backend=BACKEND, cwd=os.getcwd())
        try:
            self.assertTrue(a.start(timeout=40).usable)
            self.assertTrue(b.start(timeout=40).usable)
            self.assertEqual(a.info().kind, "opencode")
            self.assertEqual(b.info().kind, "claude")
            self.assertEqual(a.info().backend, b.info().backend)

            import uuid as _uuid

            token_a = "ALPHA_" + _uuid.uuid4().hex[:6].upper()
            token_b = "BRAVO_" + _uuid.uuid4().hex[:6].upper()
            a.send(f"Reply with exactly the single token: {token_a}")
            b.send(f"Reply with exactly the single token: {token_b}")

            a.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)
            b.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)

            cap_a = a.capture()
            cap_b = b.capture()
            self.assertIn(token_a, cap_a)
            self.assertNotIn(token_b, cap_a)
            self.assertIn(token_b, cap_b)
            self.assertNotIn(token_a, cap_b)
            self.assertNotEqual(a.agent_id, b.agent_id)
            self.assertNotEqual(a.session.session_id, b.session.session_id)

            kinds = {agent["kind"] for agent in controller.list_agents()}
            self.assertEqual(kinds, {"opencode", "claude"})
        finally:
            a.stop()
            b.stop()
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()
