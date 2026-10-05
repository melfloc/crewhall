from __future__ import annotations

import os
import shutil
import time
import unittest

from agent_terminal import Controller

RUN = (
    os.environ.get("AT_RUN_CLAUDE") == "1"
    and os.environ.get("AT_RUN_OPENCODE") == "1"
)
HAVE = shutil.which("claude") is not None and shutil.which("opencode") is not None
BACKEND = "tmux" if shutil.which("tmux") else "pty"


def wait_for_text(harness, needle: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in harness.capture():
            return True
        time.sleep(0.3)
    return False


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_CLAUDE=1 and AT_RUN_OPENCODE=1")
class MessagingReal(unittest.TestCase):
    def test_opencode_and_claude_exchange_messages(self) -> None:
        controller = Controller(adopt=False)
        a = controller.create_agent("opencode", name="oc", backend=BACKEND, cwd=os.getcwd())
        b = controller.create_agent("claude", name="cc", backend=BACKEND, cwd=os.getcwd())
        try:
            a.start(timeout=40)
            b.start(timeout=40)

            from_oc = "MESSAGE_FROM_OPENCODE_123"
            d1 = controller.send_message("oc", "cc", from_oc)
            self.assertTrue(d1.delivered, d1.error)
            self.assertTrue(wait_for_text(b, from_oc), "message did not reach Claude's input")

            from_cc = "MESSAGE_FROM_CLAUDE_456"
            d2 = controller.send_message("cc", "oc", from_cc)
            self.assertTrue(d2.delivered, d2.error)
            self.assertTrue(wait_for_text(a, from_cc), "message did not reach OpenCode's input")

            cap_a, cap_b = a.capture(), b.capture()
            self.assertIn(from_cc, cap_a)
            self.assertIn(from_oc, cap_b)
            self.assertNotIn(from_oc, cap_a)
            self.assertNotIn(from_cc, cap_b)

            history = controller.message_history()
            self.assertEqual(len(history), 2)
            self.assertTrue(all(m["delivered"] for m in history))
            self.assertEqual(history[0]["sender_name"], "oc")
            self.assertEqual(history[0]["recipient_name"], "cc")
        finally:
            a.stop()
            b.stop()
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()
