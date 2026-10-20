"""Privacy leftovers from the v2 handoff: prompt bodies and OpenCode id shapes.

Tmux errors used to embed the whole prompt in their message (and therefore in
``daemon.log``); they must carry only length/hash. The controller and the link
must also agree on which ``ses_…`` ids are valid.
"""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from crewhall import Controller, OpenCodeHarness
from crewhall.backends.tmux import TmuxBackend
from crewhall.controller import Controller as ControllerClass
from crewhall.daemon import Server
from crewhall.opencode_link import _SESSION, _V2_ID

from .support import FakeSession

SECRET = "TOP-SECRET-PROMPT: do not log this body"


class TmuxPromptRedaction(unittest.TestCase):
    def _backend(self, stderr: str):
        backend = TmuxBackend(socket="crewhall-test")
        backend.pane_id = "%1"
        backend._run = lambda *args, **kwargs: subprocess.CompletedProcess(
            ["tmux", *args], 1, "", stderr)
        return backend

    def test_send_keys_failure_reports_length_and_hash_only(self):
        backend = self._backend(f"tmux send-keys failed: {SECRET}")
        with self.assertRaises(RuntimeError) as caught:
            backend.write(SECRET)
        message = str(caught.exception)
        self.assertNotIn(SECRET, message)
        self.assertIn(str(len(SECRET)), message)
        self.assertIn("sha256:", message)

    def test_paste_failure_reports_length_and_hash_only(self):
        text = SECRET + "\nsecond line"
        backend = self._backend(f"tmux paste-buffer failed: {text}")
        with self.assertRaises(RuntimeError) as caught:
            backend.write(text)
        message = str(caught.exception)
        self.assertNotIn(text, message)
        self.assertNotIn(SECRET, message)
        self.assertIn(str(len(text)), message)
        self.assertIn("sha256:", message)

    def test_daemon_log_and_error_response_carry_no_prompt_body(self):
        backend = self._backend(f"tmux send-keys failed: {SECRET}")
        controller = Controller(adopt=False)
        harness = OpenCodeHarness(FakeSession(screen="", name="priv"), name="priv")
        harness.session.write = backend.write
        controller.register_agent(harness)
        server = object.__new__(Server)
        server.controller = controller
        with self.assertLogs("crewhall.daemon", level="ERROR") as logs:
            response = server._dispatch(
                {"op": "agent_write", "target": harness.agent_id, "text": SECRET},
                "agent_write")
        self.assertFalse(response["ok"])
        self.assertNotIn(SECRET, response["error"])
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("sha256:", response["error"])


class ConversationIdAlignment(unittest.TestCase):
    def test_controller_and_link_accept_the_same_opencode_ids(self):
        patterns = [
            ControllerClass._CONVERSATION_ID["opencode"],
            _SESSION,
            _V2_ID,
        ]
        for sid in ("ses_abc123", "ses_ab-c_1"):
            for pattern in patterns:
                self.assertTrue(pattern.match(sid), (sid, pattern.pattern))
        for bad in ("foo", "", " ses_x", "ses_ab c"):
            for pattern in patterns:
                self.assertFalse(pattern.match(bad), (bad, pattern.pattern))


if __name__ == "__main__":
    unittest.main()
