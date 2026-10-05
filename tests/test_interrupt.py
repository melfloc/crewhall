from __future__ import annotations

import unittest

from agent_terminal import ClaudeCodeHarness, OpenCodeHarness

from .support import FakeSession
from .test_harness import READY_SCREEN as OC_READY, WORKING_SCREEN as OC_WORKING
from .test_harness_claude import CLAUDE_READY, CLAUDE_WORKING


class Interrupt(unittest.TestCase):
    def test_claude_sends_exactly_one_esc_while_working(self):
        # Two ESCs on Claude open the Rewind selector, so exactly one.
        session = FakeSession(screen=CLAUDE_WORKING, name="c")
        self.assertTrue(ClaudeCodeHarness(session, name="c").interrupt())
        self.assertEqual(session.keys, ["ESC"])

    def test_opencode_sends_esc_twice_while_working(self):
        session = FakeSession(screen=OC_WORKING, name="o")
        self.assertTrue(OpenCodeHarness(session, name="o").interrupt())
        self.assertEqual(session.keys, ["ESC", "ESC"])

    def test_nothing_is_sent_when_idle(self):
        for cls, screen in ((ClaudeCodeHarness, CLAUDE_READY), (OpenCodeHarness, OC_READY)):
            session = FakeSession(screen=screen, name="x")
            self.assertFalse(cls(session, name="x").interrupt())
            self.assertEqual(session.keys, [])


if __name__ == "__main__":
    unittest.main()
