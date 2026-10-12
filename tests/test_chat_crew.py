from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import chat as conversations
from crewhall.controller import Controller
from crewhall.messaging import MessagingError
from tests.support import FakeHarness


class ChatCrew(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-crew-", dir="/tmp")
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
        self.lead.session.spec.cwd = self.workspace
        self.worker.session.spec.cwd = self.workspace
        self.controller.register_agent(self.lead)
        self.controller.register_agent(self.worker)
        self.controller.teams.create(
            "crew1", [self.lead.agent_id, self.worker.agent_id],
            workspace=self.workspace)
        self.lead_token = self.controller._agent_token(self.lead.agent_id)
        self.worker_token = self.controller._agent_token(self.worker.agent_id)
        self.conv = conversations.create("Report chat", team="crew1")

    def test_conversation_remembers_its_crew(self) -> None:
        self.assertEqual(conversations.get(self.conv["id"])["team"], "crew1")
        again = conversations.set_team(self.conv["id"], None)
        self.assertIsNone(again["team"])
        conversations.set_team(self.conv["id"], "crew1")

    def test_worker_publishes_artifact_with_producer(self) -> None:
        src = os.path.join(self.workspace, "informe.md")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("# informe\n")
        result = self.controller.publish_artifact(
            self.worker.agent_id, self.worker_token, self.conv["id"], src)
        self.assertEqual(result["producer"], self.worker.agent_id)
        info = conversations.info(self.conv["id"])
        self.assertEqual(len(info["outputs"]), 1)
        self.assertEqual(info["outputs"][0]["producer"], self.worker.agent_id)

    def test_publish_rejects_an_outsider(self) -> None:
        outsider = FakeHarness("outsider")
        self.controller.register_agent(outsider)
        token = self.controller._agent_token(outsider.agent_id)
        src = os.path.join(self.workspace, "x.md")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("x")
        with self.assertRaises(MessagingError):
            self.controller.publish_artifact(outsider.agent_id, token, self.conv["id"], src)

    def test_publish_rejects_a_source_outside_the_roots(self) -> None:
        outside = os.path.join(self.root, "secret.txt")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("secret")
        with self.assertRaises(conversations.ConversationError):
            self.controller.publish_artifact(
                self.worker.agent_id, self.worker_token, self.conv["id"], outside)

    def test_activity_reports_crew_messages_and_requests(self) -> None:
        messaging = self.controller.messaging
        messaging.send(self.lead.agent_id, self.worker.agent_id, "do the report",
                       idempotency_key="h1", wait=0.1, conversation_id=self.conv["id"])
        activity = self.controller.conversation_activity(self.conv["id"])
        self.assertEqual(activity["team"], "crew1")
        self.assertIn(self.worker.agent_id, activity["members"])
        bodies = [m["body"] for m in activity["messages"]]
        self.assertIn("do the report", bodies)

    def test_activity_tolerates_a_system_sender(self) -> None:
        # An OnlyOffice comment arrives as system:office (not an agent): the
        # activity strip and message history must include it, not crash.
        self.controller.messaging.send_system(
            self.lead.agent_id, "[OnlyOffice chat] hola", origin="office",
            conversation_id=self.conv["id"])
        activity = self.controller.conversation_activity(self.conv["id"])
        senders = [m["sender"] for m in activity["messages"]]
        self.assertIn("system:office", senders)
        history = self.controller.messaging.history()
        self.assertTrue(any(m["sender"] == "system:office" and m["sender_name"] == "office"
                            for m in history))

    # set_conversation_team keeps the stored crew and the lead's membership in sync.
    def test_attaching_a_crew_joins_the_existing_lead(self) -> None:
        self.controller.teams.create("crew2", [self.worker.agent_id])
        conv = conversations.create("Solo chat", agent_id=self.lead.agent_id)
        meta = self.controller.set_conversation_team(conv["id"], "crew2")
        crew2 = self.controller.teams.get("crew2")
        self.assertEqual(meta["team"], crew2.team_id)
        self.assertIn(self.lead.agent_id, crew2.agent_ids)
        self.assertTrue(self.controller.agent_in_conversation(
            self.worker.agent_id, self.worker_token, conv["id"]))
        # Detaching removes the lead from that crew again.
        self.controller.set_conversation_team(conv["id"], None)
        self.assertNotIn(self.lead.agent_id, self.controller.teams.get("crew2").agent_ids)

    def test_unknown_team_is_refused(self) -> None:
        conv = conversations.create("Solo chat", agent_id=self.lead.agent_id)
        with self.assertRaises(KeyError):
            self.controller.set_conversation_team(conv["id"], "nope")

    def test_cli_chat_publish_uses_the_agent_identity(self) -> None:
        import argparse

        from crewhall import cli

        src = os.path.join(self.workspace, "informe.md")
        with open(src, "w") as fh:
            fh.write("Informe de prueba\n")
        calls = []

        class _Client:
            def call(self, op, **params):
                calls.append((op, params))
                return {"artifact": {"relpath": "informe.md"}}

        env = {"CREWHALL_AGENT_ID": "worker-1", "CREWHALL_TOKEN": "tok",
               "CREWHALL_CONVERSATION": self.conv["id"]}
        args = argparse.Namespace(path=src, conversation=None, name=None, socket=None)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli, "_client", lambda _a: _Client()):
            self.assertEqual(cli.cmd_chat_publish(args), 0)
        op, params = calls[0]
        self.assertEqual(op, "conversation_publish")
        self.assertEqual((params["sender"], params["token"], params["id"]),
                         ("worker-1", "tok", self.conv["id"]))
        self.assertEqual(params["source"], src)

    def test_publish_and_crew_changes_are_audited(self) -> None:
        from crewhall import audit

        self.assertIn("conversation_publish", audit.AUDITED)
        self.assertIn("conversation_set_team", audit.AUDITED)
        summary = audit.summarize("conversation_publish", {
            "id": "c-1", "sender": "w", "token": "SECRET", "source": "/x"})
        self.assertIn("sender=w", summary)
        self.assertNotIn("SECRET", summary)


if __name__ == "__main__":
    unittest.main()
