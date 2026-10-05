from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
import uuid

from crewhall import Controller
from crewhall.control_files import BEGIN, END
from crewhall.messaging import MessagingError

RUN = (
    os.environ.get("AT_RUN_CLAUDE") == "1"
    and os.environ.get("AT_RUN_OPENCODE") == "1"
)
HAVE = shutil.which("claude") is not None and shutil.which("opencode") is not None
BACKEND = "tmux" if shutil.which("tmux") else "pty"


def _wait(predicate, timeout: float = 60.0, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_CLAUDE=1 and AT_RUN_OPENCODE=1")
class CollaborationReal(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)

    def tearDown(self) -> None:
        self.controller.shutdown()

    def test_control_file_preservation_real(self) -> None:
        tmp = tempfile.mkdtemp(prefix="at-cf-real-")
        try:
            original = "# Project Rules\n\nRegla A\nRegla B\n"
            path = os.path.join(tmp, "CLAUDE.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
            self.controller._ensure_control_file(tmp)
            with open(path, encoding="utf-8") as fh:
                first = fh.read()
            self.assertTrue(first.startswith(original))
            self.assertIn(BEGIN, first)
            self.controller._ensure_control_file(tmp)  # idempotent
            with open(path, encoding="utf-8") as fh:
                second = fh.read()
            self.assertEqual(first, second)
            self.assertEqual(second.count(BEGIN), 1)
            self.assertEqual(second.count(END), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_message_after_revive_is_clean(self) -> None:
        """Fase 9.1: a message injected after reviving an EXITED agent must
        arrive as a clean input, not concatenated with residual TUI text."""
        cwd = os.getcwd()
        opus = self.controller.create_agent("claude", name="opus", backend=BACKEND, cwd=cwd)
        sonnet = self.controller.create_agent("claude", name="sonnet", backend=BACKEND, cwd=cwd)
        self.controller.create_team("fiscal", ["opus", "sonnet"])
        _wait(lambda: opus.state().value in ("ready", "waiting_input"), 60)
        _wait(lambda: sonnet.state().value in ("ready", "waiting_input"), 60)

        # Leave residual text in Sonnet's input, then end its process.
        sonnet.session.write("/model sonnet")
        time.sleep(0.7)
        sonnet.session.terminate()
        _wait(lambda: sonnet.state().value in ("exited", "error"), 30)
        assert sonnet.state().value in ("exited", "error")

        token = self.controller._agent_token(opus.agent_id)
        body = "Hola sonnet, soy opus - " + uuid.uuid4().hex[:6]
        for _ in range(2):
            delivery = self.controller.send_message_as("opus", token, "sonnet", body)
            self.assertTrue(delivery.delivered, delivery.error)
            agent = self.controller.get_agent("sonnet")
            self.assertTrue(_wait(lambda agent=agent, body=body: body in agent.capture(), 60), "not delivered")
            for line in agent.capture().splitlines():
                if body[-6:] in line:
                    self.assertNotIn("/model", line)
                    self.assertNotIn("sonnetHola", line)
            body = "Hola sonnet, soy opus - " + uuid.uuid4().hex[:6]
            time.sleep(1)

    def test_team_activation_and_isolation(self) -> None:
        cwd = os.getcwd()
        auditor = self.controller.create_agent("claude", name="auditor", backend=BACKEND, cwd=cwd)
        extractor = self.controller.create_agent("opencode", name="extractor", backend=BACKEND, cwd=cwd)
        abogado = self.controller.create_agent("opencode", name="abogado", backend=BACKEND, cwd=cwd)
        self.controller.create_team("fiscal", ["auditor", "extractor"])
        self.controller.create_team("legal", ["abogado"])

        self.assertTrue(auditor.start(timeout=40).usable)
        extractor_id = extractor.agent_id
        token_auditor = self.controller._agent_token(auditor.agent_id)

        # Stop extractor completely (process ends, agent stays registered).
        _wait(lambda: extractor.state().value in ("ready", "waiting_input"), 40)
        extractor.session.send_key("CTRL_D")
        self.assertTrue(
            _wait(lambda: extractor.state().value in ("exited", "error"), 30),
            "extractor did not exit",
        )

        body = "WAKE_" + uuid.uuid4().hex[:6].upper()
        delivery = self.controller.send_message_as(
            "auditor", token_auditor, "extractor", body
        )
        self.assertTrue(delivery.delivered, delivery.error)
        # Same logical agent (same id) reactivated, not a new agent.
        self.assertEqual(self.controller.get_agent("extractor").agent_id, extractor_id)
        self.assertTrue(
            _wait(lambda: body in self.controller.get_agent("extractor").capture(), 30),
            "message not delivered after activation",
        )

        # Team isolation: auditor (fiscal) cannot reach abogado (legal).
        denied = "DENY_" + uuid.uuid4().hex[:6].upper()
        with self.assertRaises(MessagingError):
            self.controller.send_message_as("auditor", token_auditor, "abogado", denied)
        self.assertNotIn(denied, abogado.capture())

        # Reverse direction inside the shared Team.
        reply = "REPLY_" + uuid.uuid4().hex[:6].upper()
        token_extractor = self.controller._agent_token(extractor_id)
        back = self.controller.send_message_as("extractor", token_extractor, "auditor", reply)
        self.assertTrue(back.delivered, back.error)


if __name__ == "__main__":
    unittest.main()
