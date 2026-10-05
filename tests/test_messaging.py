from __future__ import annotations

import threading
import unittest

from agent_terminal import (
    ClaudeCodeHarness,
    Controller,
    Delivery,
    Message,
    MessagingError,
    OpenCodeHarness,
)
from agent_terminal.messaging import new_message_id

from .support import FakeHarness, FakeSession

OC_READY = '  Build \u00b7 model\n  Ask anything\u2026 "x"\n  ctrl+p commands'
CLAUDE_READY = "\n".join(
    [
        " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
        '\u276f\u00a0Try "write a test"',
        "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents",
    ]
)


def opencode(name: str) -> OpenCodeHarness:
    return OpenCodeHarness(FakeSession(screen=OC_READY, name=name), name=name)


def claude(name: str) -> ClaudeCodeHarness:
    return ClaudeCodeHarness(FakeSession(screen=CLAUDE_READY, name=name), name=name)


class MessageModel(unittest.TestCase):
    def test_fields_and_dict(self) -> None:
        msg = Message(
            message_id="msg_abc",
            sender="sess_a",
            recipient="sess_b",
            body="hola",
            timestamp=1234.5,
        )
        as_dict = msg.to_dict()
        self.assertEqual(as_dict["message_id"], "msg_abc")
        self.assertEqual(as_dict["sender"], "sess_a")
        self.assertEqual(as_dict["recipient"], "sess_b")
        self.assertEqual(as_dict["body"], "hola")
        self.assertEqual(as_dict["timestamp"], 1234.5)

    def test_ids_are_unique_and_prefixed(self) -> None:
        ids = {new_message_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(i.startswith("msg_") for i in ids))


class MessagingUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)
        self.a = FakeHarness("a")
        self.b = FakeHarness("b")
        self.controller.register_agent(self.a)
        self.controller.register_agent(self.b)
        self.messaging = self.controller.messaging

    def test_send_a_to_b_delivered(self) -> None:
        delivery = self.messaging.send("a", "b", "hello from a")
        self.assertIsInstance(delivery, Delivery)
        self.assertTrue(delivery.delivered)
        self.assertIsNotNone(delivery.delivered_at)
        self.assertIsNone(delivery.error)
        self.assertEqual(delivery.message.sender, self.a.agent_id)
        self.assertEqual(delivery.message.recipient, self.b.agent_id)
        self.assertEqual(delivery.message.body, "hello from a")
        self.assertEqual(delivery.sender_name, "a")
        self.assertEqual(delivery.recipient_name, "b")

    def test_body_reaches_recipient_input(self) -> None:
        self.messaging.send("a", "b", "hello from a")
        self.assertEqual(self.b.session.writes, ["hello from a"])
        self.assertIn("ENTER", self.b.session.keys)

    def test_sender_is_not_touched(self) -> None:
        self.messaging.send("a", "b", "hello")
        self.assertEqual(self.a.session.writes, [])
        self.assertEqual(self.a.session.keys, [])

    def test_unknown_sender(self) -> None:
        with self.assertRaises(MessagingError) as ctx:
            self.messaging.send("ghost", "b", "hi")
        self.assertIn("sender", str(ctx.exception))

    def test_unknown_recipient(self) -> None:
        with self.assertRaises(MessagingError) as ctx:
            self.messaging.send("a", "ghost", "hi")
        self.assertIn("recipient", str(ctx.exception))

    def test_self_message_rejected(self) -> None:
        with self.assertRaises(MessagingError) as ctx:
            self.messaging.send("a", "a", "hi")
        self.assertIn("itself", str(ctx.exception))

    def test_two_consecutive_messages_ordered_in_history(self) -> None:
        self.messaging.send("a", "b", "one")
        self.messaging.send("a", "b", "two")
        history = self.messaging.history()
        self.assertEqual([m["body"] for m in history], ["one", "two"])
        self.assertEqual(self.b.session.writes, ["one", "two"])

    def test_history_filter_by_agent(self) -> None:
        self.messaging.send("a", "b", "from a")
        self.messaging.send("b", "a", "from b")
        for_a = self.messaging.history(agent="a")
        self.assertEqual({m["body"] for m in for_a}, {"from a", "from b"})
        only_b_to_a = [
            m for m in self.messaging.history(agent="a") if m["sender_name"] == "b"
        ]
        self.assertEqual([m["body"] for m in only_b_to_a], ["from b"])

    def test_isolation_message_does_not_reach_other_agent(self) -> None:
        c = FakeHarness("c")
        self.controller.register_agent(c)
        self.messaging.send("a", "b", "only for b")
        self.assertEqual(self.b.session.writes, ["only for b"])
        self.assertEqual(c.session.writes, [])

    def test_body_with_multiline_is_preserved(self) -> None:
        body = "line one\nline two"
        self.messaging.send("a", "b", body)
        self.assertEqual(self.b.session.writes, [body])

    def test_traceability_events_recorded(self) -> None:
        self.messaging.send("a", "b", "trace me")
        sent = [e for e in self.a.session.events() if e["type"] == "message_sent"]
        delivered = [e for e in self.b.session.events() if e["type"] == "message_delivered"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(sent[0]["data"]["recipient"], self.b.agent_id)

    def test_heterogeneous_real_harness_classes(self) -> None:
        oc = opencode("oc")
        cc = claude("cc")
        self.controller.register_agent(oc)
        self.controller.register_agent(cc)
        d1 = self.messaging.send("oc", "cc", "opencode to claude")
        d2 = self.messaging.send("cc", "oc", "claude to opencode")
        self.assertTrue(d1.delivered, d1.error)
        self.assertTrue(d2.delivered, d2.error)
        self.assertEqual(cc.session.writes, ["opencode to claude"])
        self.assertEqual(oc.session.writes, ["claude to opencode"])

    def test_concurrent_bidirectional(self) -> None:
        results: list[Delivery] = []
        lock = threading.Lock()

        def sender(s: str, r: str, body: str) -> None:
            delivery = self.messaging.send(s, r, body)
            with lock:
                results.append(delivery)

        threads = [
            threading.Thread(target=sender, args=("a", "b", "a2b-1")),
            threading.Thread(target=sender, args=("b", "a", "b2a-1")),
            threading.Thread(target=sender, args=("a", "b", "a2b-2")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(d.delivered for d in results))
        self.assertEqual(sorted(self.b.session.writes), ["a2b-1", "a2b-2"])
        self.assertEqual(self.a.session.writes, ["b2a-1"])
        self.assertEqual(len(self.messaging.history()), 3)


if __name__ == "__main__":
    unittest.main()
