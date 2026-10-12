from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from crewhall import chat as conversations
from crewhall.controller import Controller
from crewhall.daemon import Server
from crewhall.harness import AgentState
from crewhall.messaging import Messaging
from crewhall.persistence import PendingQueueStore
from tests.support import FakeHarness


class _Busy(FakeHarness):
    # Per instance: a class-level flag flipped by one test leaked into the next
    # one in the same process and made the recipient look idle.
    busy = True

    def _detect(self, text):
        return (AgentState.WORKING, "w") if self.busy else (AgentState.READY, "r")


class _DraftHarness(FakeHarness):
    """A recipient whose input holds a human draft: send refuses (retryable)."""

    def __init__(self, name="b"):
        super().__init__(name)
        self.draft = "human is typing this"
        self.send_attempts = 0

    def _detect(self, text):
        return AgentState.READY, "ready"

    def input_line(self) -> str:
        return self.draft

    def _input_is_clean(self) -> bool:
        return not self.draft.strip()

    def send(self, prompt, timeout=30.0):
        # Mirrors Harness.send: refuse to overwrite a foreign draft.
        from crewhall.harness.base import HarnessNotReady

        self.send_attempts += 1
        if self.draft.strip():
            raise HarnessNotReady(self.agent_id, "input line not clean")
        self.session.write(prompt)
        self.session.send_enter()


class QueueOrdering(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False, persist=False)

    def test_three_queued_messages_arrive_in_order(self) -> None:
        a, b = FakeHarness("a"), _Busy("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=0)
        deliveries = [
            messaging.send(a.agent_id, b.agent_id, f"m{i}", wait=0.1)
            for i in range(3)
        ]
        self.assertTrue(all(d.queued for d in deliveries))
        b.busy = False
        deadline = time.monotonic() + 8
        while not deliveries[-1].delivered and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(all(d.delivered for d in deliveries))
        self.assertEqual(b.session.writes, ["m0", "m1", "m2"])
        messaging.close()

    def test_human_draft_queues_and_stays_intact(self) -> None:
        a, b = FakeHarness("a"), _DraftHarness("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=0)
        delivery = messaging.send(a.agent_id, b.agent_id, "queued body", wait=0.2)
        self.assertTrue(delivery.queued, delivery.to_dict())
        self.assertFalse(delivery.delivered)
        # Waiting behind a draft is not an error (the CLI printed one before).
        self.assertIsNone(delivery.to_dict()["error"])
        self.assertEqual(delivery.status, "queued")
        self.assertEqual(b.draft, "human is typing this")
        self.assertEqual(b.session.writes, [])  # nothing typed over the draft
        # The human finishes: the queue then delivers untouched.
        b.draft = ""
        deadline = time.monotonic() + 5
        while not delivery.delivered and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(delivery.delivered)
        self.assertEqual(b.session.writes, ["queued body"])
        messaging.close()


class QueueDurability(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-pq-", dir="/tmp")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.store_path = os.path.join(self.root, "pending.json")

    def test_queued_message_survives_a_restart(self) -> None:
        controller = Controller(adopt=False, persist=False)
        a, b = FakeHarness("a"), _Busy("b")
        controller.register_agent(a)
        controller.agents.add(b)
        store = PendingQueueStore(self.store_path)
        m1 = Messaging(controller.agents, ack_timeout=0, store=store)
        d = m1.send(a.agent_id, b.agent_id, "survive me", wait=0.1, queue_ttl=600)
        self.assertTrue(d.queued)
        m1.close()

        items = store.load()
        self.assertEqual([i["body"] for i in items], ["survive me"])

        # "Restart": a fresh registry + messaging with the same recipient ready.
        controller2 = Controller(adopt=False, persist=False)
        a2, b2 = FakeHarness("a"), FakeHarness("b")
        a2.session.session_id = a.agent_id  # identities persist across a restart
        b2.session.session_id = b.agent_id
        controller2.register_agent(a2)
        controller2.agents.add(b2)
        m2 = Messaging(controller2.agents, ack_timeout=0,
                       store=PendingQueueStore(self.store_path))
        resumed = m2.load(items)
        self.assertEqual(resumed, 1)
        deadline = time.monotonic() + 5
        while not b2.session.writes and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("survive me", b2.session.writes)
        m2.close()

    def test_expired_pending_is_dropped(self) -> None:
        controller = Controller(adopt=False, persist=False)
        a, b = FakeHarness("a"), FakeHarness("b")
        controller.register_agent(a)
        controller.agents.add(b)
        store = PendingQueueStore(self.store_path)
        messaging = Messaging(controller.agents, ack_timeout=0, store=store)
        resumed = messaging.load([{
            "message_id": "msg_x", "sender": a.agent_id, "recipient": b.agent_id,
            "body": "stale", "text": "stale", "timestamp": time.time() - 100,
            "expires_at": time.time() - 1,
        }])
        self.assertEqual(resumed, 0)
        self.assertEqual(b.session.writes, [])
        messaging.close()


class SystemMessages(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False, persist=False)

    def test_system_message_to_busy_recipient_is_queued_then_delivered(self) -> None:
        b = _Busy("b")
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=0)
        delivery = messaging.send_system(
            b.agent_id, "[OnlyOffice comment] cambia esto", origin="office")
        self.assertTrue(delivery.queued, delivery.to_dict())
        self.assertEqual(delivery.message.sender, "system:office")
        b.busy = False
        deadline = time.monotonic() + 5
        while not delivery.delivered and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(delivery.delivered)
        self.assertEqual(b.session.writes, ["[OnlyOffice comment] cambia esto"])
        messaging.close()

    def test_system_message_respects_a_human_draft(self) -> None:
        b = _DraftHarness("b")
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=0)
        delivery = messaging.send_system(b.agent_id, "system note", origin="office")
        self.assertTrue(delivery.queued)
        self.assertEqual(b.draft, "human is typing this")
        self.assertEqual(b.session.writes, [])
        messaging.close()

    def test_system_idempotency_key_dedups(self) -> None:
        b = _Busy("b")
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=0)
        d1 = messaging.send_system(b.agent_id, "same", origin="office",
                                   idempotency_key="office:c-1:abc")
        d2 = messaging.send_system(b.agent_id, "same", origin="office",
                                   idempotency_key="office:c-1:abc")
        self.assertIs(d1, d2)
        messaging.close()

    def test_system_message_survives_snapshot_and_load(self) -> None:
        b = _Busy("b")
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=0)
        messaging.send_system(b.agent_id, "persist me", origin="office",
                              queue_ttl=600)
        items = messaging.snapshot()
        self.assertEqual(items[0]["sender"], "system:office")
        messaging.close()

        controller2 = Controller(adopt=False, persist=False)
        b2 = FakeHarness("b")
        b2.session.session_id = b.agent_id
        controller2.agents.add(b2)
        m2 = Messaging(controller2.agents, ack_timeout=0)
        self.assertEqual(m2.load(items), 1)
        deadline = time.monotonic() + 5
        while not b2.session.writes and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(b2.session.writes, ["persist me"])
        m2.close()


class ForwardCollabQueues(unittest.TestCase):
    """A busy recipient must not make an OnlyOffice comment vanish (P1)."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-fwd-", dir="/tmp")
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
        self.agent = _Busy("lead")
        self.controller.agents.add(self.agent)
        self.server = Server(self.controller, os.path.join(self.root, "r", "d.sock"))
        self.conv = conversations.create("chat", agent_id=self.agent.agent_id)

    def test_forward_collab_on_busy_agent_queues_not_drops(self) -> None:
        self.server._forward_collab(self.conv["id"], "doc.txt", "you", "comment",
                                    'sobre "p2": cambia esto')
        history = self.controller.messaging.history()
        self.assertEqual(len(history), 1, history)
        self.assertTrue(history[0]["queued"])
        self.assertEqual(history[0]["sender"], "system:office")
        self.assertIn("cambia esto", history[0]["body"])
        self.assertEqual(self.agent.session.writes, [])  # not injected while busy

    def test_forward_collab_two_identical_texts_are_two_messages(self) -> None:
        # "sí" twice in the document chat is two answers, not a duplicate: the
        # co-editor already forwards each message/comment id exactly once.
        args = (self.conv["id"], "doc.txt", "you", "chat", "sí")
        self.server._forward_collab(*args)
        self.server._forward_collab(*args)
        self.assertEqual(len(self.controller.messaging.history()), 2)

    def test_reopened_document_does_not_reforward_old_comments(self) -> None:
        from crewhall.office import collab as office_collab

        forwarded = []

        class _Editor:
            def chat_messages(self):
                return []

            def comments(self):
                return [{"Id": "c1", "Data": {"UserName": "you", "Text": "revisa"}}]

        def make_registry():
            reg = office_collab.CollabRegistry()
            reg.set_forward(lambda *a: forwarded.append(a))
            reg._editors[reg.key(self.conv["id"], "doc.docx")] = _Editor()
            return reg

        make_registry().poll_forward()
        self.assertEqual(len(forwarded), 1)
        # A new registry (reopen / daemon restart) lists the same comment again.
        make_registry().poll_forward()
        self.assertEqual(len(forwarded), 1)

    def test_forward_collab_delivers_when_agent_becomes_ready(self) -> None:
        self.server._forward_collab(self.conv["id"], "doc.txt", "you", "chat", "hola")
        self.agent.busy = False
        deadline = time.monotonic() + 5
        while not self.agent.session.writes and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.agent.session.writes), 1)
        self.assertIn("[OnlyOffice chat", self.agent.session.writes[0])
        self.assertIn("hola", self.agent.session.writes[0])


if __name__ == "__main__":
    unittest.main()
