from __future__ import annotations

import os
import tempfile
import time
import unittest

from crewhall.messaging import Messaging
from crewhall.persistence import HistoryStore
from tests.support import FakeHarness


class _Registry:
    def __init__(self, agents: dict) -> None:
        self._agents = agents

    def resolve(self, target: str):
        if target in self._agents:
            return self._agents[target]
        raise KeyError(target)

    def all(self):
        return list(self._agents.values())


def _two_agents() -> tuple[_Registry, FakeHarness, FakeHarness]:
    a = FakeHarness(name="a")
    b = FakeHarness(name="b")
    return _Registry({a.agent_id: a, b.agent_id: b}), a, b


class MessageHistoryPersistence(unittest.TestCase):
    """Fase A2: the message history survives a daemon restart."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="at-hist-")
        self.path = os.path.join(self.dir, "messages.jsonl")

    def test_history_survives_a_restart(self) -> None:
        registry, a, b = _two_agents()
        first = Messaging(registry, history_store=HistoryStore(self.path, max_entries=100))
        delivery = first.send(a.agent_id, b.agent_id, "hello across restart", wait=0.0)
        self.assertTrue(delivery.delivered)

        # A brand-new Messaging object is what a restarted daemon builds.
        second = Messaging(registry, history_store=HistoryStore(self.path, max_entries=100))
        self.assertEqual(second.history(), [])
        self.assertGreaterEqual(second.load_history(), 1)
        bodies = [row["body"] for row in second.history()]
        self.assertIn("hello across restart", bodies)

    def test_no_store_keeps_history_in_memory_only(self) -> None:
        registry, a, b = _two_agents()
        messaging = Messaging(registry)
        messaging.send(a.agent_id, b.agent_id, "ephemeral", wait=0.0)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(messaging.load_history(), 0)

    def test_rotation_keeps_only_the_newest(self) -> None:
        store = HistoryStore(self.path, max_entries=10, max_age_days=None)
        for i in range(100):
            store.append({
                "message_id": f"m{i}", "timestamp": float(i),
                "sender": "a", "recipient": "b", "body": str(i),
            })
        records = store.load()
        self.assertEqual(len(records), 10)
        self.assertEqual(records[-1]["message_id"], "m99")

        with open(self.path, encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        self.assertEqual(len(lines), 10)

    def test_old_entries_are_pruned_by_age(self) -> None:
        store = HistoryStore(self.path, max_entries=1000, max_age_days=1)
        now = time.time()
        store.append({"message_id": "old", "timestamp": now - 5 * 86400,
                      "sender": "a", "recipient": "b", "body": "old"})
        store.append({"message_id": "new", "timestamp": now,
                      "sender": "a", "recipient": "b", "body": "new"})
        ids = [row["message_id"] for row in store.load()]
        self.assertEqual(ids, ["new"])


if __name__ == "__main__":
    unittest.main()
