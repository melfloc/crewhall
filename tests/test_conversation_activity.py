from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import chat as conversations
from crewhall.controller import Controller
from tests.support import FakeHarness


class ActivityPerConversation(unittest.TestCase):
    """Fase E1: two chats on the same Team each see only their own strip."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-e1-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.controller = Controller(adopt=False, persist=False)
        self.workspace = os.path.join(self.root, "work")
        os.makedirs(self.workspace)
        self.lead = FakeHarness("lead")
        self.worker = FakeHarness("worker")
        self.controller.register_agent(self.lead)
        self.controller.register_agent(self.worker)
        self.controller.teams.create(
            "crew1", [self.lead.agent_id, self.worker.agent_id], workspace=self.workspace)
        self.token = self.controller._agent_token(self.lead.agent_id)
        self.conv_a = conversations.create("Chat A", team="crew1")["id"]
        self.conv_b = conversations.create("Chat B", team="crew1")["id"]

    def _send(self, body: str, conversation_id: str) -> None:
        self.controller.send_message_as(
            self.lead.agent_id, self.token, self.worker.agent_id, body,
            conversation_id=conversation_id)

    def test_each_chat_sees_only_its_own(self) -> None:
        self._send("for A", self.conv_a)
        self._send("for B", self.conv_b)

        activity_a = self.controller.conversation_activity(self.conv_a)
        activity_b = self.controller.conversation_activity(self.conv_b)
        self.assertEqual([m["body"] for m in activity_a["messages"]], ["for A"])
        self.assertEqual([m["body"] for m in activity_b["messages"]], ["for B"])

    def test_see_all_team_widens_the_strip(self) -> None:
        self._send("for A", self.conv_a)
        self._send("for B", self.conv_b)
        activity = self.controller.conversation_activity(self.conv_a, all_team=True)
        self.assertEqual(sorted(m["body"] for m in activity["messages"]), ["for A", "for B"])


if __name__ == "__main__":
    unittest.main()
