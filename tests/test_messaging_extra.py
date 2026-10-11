from __future__ import annotations

import time
import unittest
from collections import deque

from crewhall.harness import AgentState
from crewhall.messaging import (
    Delivery,
    Message,
    Messaging,
    MessagingError,
    delivery_from_dict,
    new_message_id,
)
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


class CtlHarness(FakeHarness):
    """A harness whose state / input cleanliness the test controls."""

    def __init__(self, name=None, state=AgentState.READY) -> None:
        super().__init__(name=name)
        self._state = state
        self._draft = ""
        self._raise_state = False

    def _detect(self, text):
        if self._raise_state:
            raise RuntimeError("state boom")
        return self._state, "controlled"

    def input_line(self):
        return self._draft

    def input_is_clean(self):
        if self._raise_state:
            raise RuntimeError("clean boom")
        return not self._draft.strip()

    def set(self, state: AgentState, draft: str = "") -> None:
        self._state, self._draft = state, draft


def _pair():
    a = CtlHarness(name="a")
    b = CtlHarness(name="b")
    return _Registry({a.agent_id: a, b.agent_id: b}), a, b


def _delivery(sender: str, recipient: str, body: str = "hi", **kw) -> Delivery:
    return Delivery(
        message=Message(message_id=new_message_id(), sender=sender, recipient=recipient,
                        body=body, timestamp=time.time()),
        delivered=False, **kw,
    )


class DeliveryStatus(unittest.TestCase):
    def test_failed_status(self) -> None:
        d = _delivery("a", "b")
        self.assertEqual(d.status, "failed")

    def test_queued_status(self) -> None:
        d = _delivery("a", "b", queued=True)
        self.assertEqual(d.status, "queued")

    def test_delivery_from_dict_roundtrip(self) -> None:
        d = _delivery("a", "b")
        d.delivered = True
        d.delivered_at = 1.0
        restored = delivery_from_dict(d.to_dict())
        self.assertIsNotNone(restored)
        self.assertEqual(restored.message.body, "hi")

    def test_delivery_from_dict_rejects_bad(self) -> None:
        self.assertIsNone(delivery_from_dict({"nope": 1}))


class QueueReason(unittest.TestCase):
    def test_stopped(self) -> None:
        reg, a, b = _pair()
        b.set(AgentState.EXITED)
        m = Messaging(reg)
        self.assertEqual(m._queue_reason(b), "stopped")

    def test_state_exception_is_busy(self) -> None:
        reg, a, b = _pair()
        b._raise_state = True
        m = Messaging(reg)
        self.assertEqual(m._queue_reason(b), "busy")

    def test_draft_exception_is_busy(self) -> None:
        reg, a, b = _pair()
        b._raise_state = True
        m = Messaging(reg)
        # state() raises -> "busy" (covers the first guard); force the second by
        # making state work but input_is_clean raise.
        b._raise_state = False
        b.input_is_clean = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
        self.assertEqual(m._queue_reason(b), "busy")

    def test_resolve_unknown_raises(self) -> None:
        reg, a, b = _pair()
        m = Messaging(reg)
        with self.assertRaises(MessagingError):
            m._resolve("ghost", "recipient")


class Drain(unittest.TestCase):
    def _messaging(self, reg) -> Messaging:
        m = Messaging(reg)
        m._ensure_worker = lambda agent_id: None  # type: ignore[method-assign]
        return m

    def test_ttl_expired_is_dropped(self) -> None:
        reg, a, b = _pair()
        m = self._messaging(reg)
        d = _delivery(a.agent_id, b.agent_id, queued=True, text="hi")
        d.expires_at = time.time() - 1
        m._queues[b.agent_id] = deque([d])
        m._drain(b.agent_id)
        self.assertEqual(d.status, "failed")
        self.assertNotIn(b.agent_id, m._queues)

    def test_unknown_recipient_is_dropped(self) -> None:
        reg, a, b = _pair()
        m = self._messaging(reg)
        d = _delivery(a.agent_id, "ghost", queued=True, text="hi")
        m._queues["ghost"] = deque([d])
        m._drain("ghost")
        self.assertEqual(d.status, "failed")

    def test_terminal_recipient_without_revive_is_dropped(self) -> None:
        reg, a, b = _pair()
        b.set(AgentState.EXITED)
        m = self._messaging(reg)
        d = _delivery(a.agent_id, b.agent_id, queued=True, text="hi")
        m._queues[b.agent_id] = deque([d])
        m._drain(b.agent_id)
        self.assertEqual(d.status, "failed")

    def test_revive_reactivates_then_delivers(self) -> None:
        reg, a, b = _pair()
        b.set(AgentState.EXITED)

        def revive(harness):
            harness.set(AgentState.READY)

        m = Messaging(reg, revive=revive)
        m._ensure_worker = lambda agent_id: None  # type: ignore[method-assign]
        d = _delivery(a.agent_id, b.agent_id, queued=True, text="revived")
        m._queues[b.agent_id] = deque([d])
        m._drain(b.agent_id)
        self.assertTrue(d.delivered)
        self.assertIn("revived", b.session.writes)

    def test_injection_failure_is_dropped(self) -> None:
        reg, a, b = _pair()
        m = self._messaging(reg)

        def boom(body, timeout=30.0):
            raise RuntimeError("nope")

        b.send = boom  # type: ignore[method-assign]
        d = _delivery(a.agent_id, b.agent_id, queued=True, text="hi")
        m._queues[b.agent_id] = deque([d])
        m._drain(b.agent_id)
        self.assertEqual(d.status, "failed")
        self.assertIn("nope", d.error or "")


class WatchAck(unittest.TestCase):
    def test_acknowledged_when_working(self) -> None:
        reg, a, b = _pair()
        b.set(AgentState.WORKING)
        m = Messaging(reg, ack_timeout=1.0)
        d = _delivery(a.agent_id, b.agent_id)
        d.delivered = True
        m._watch_ack(b, d)
        deadline = time.monotonic() + 2
        while d.acknowledged_at is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(d.acknowledged_at)

    def test_zero_timeout_does_nothing(self) -> None:
        reg, a, b = _pair()
        m = Messaging(reg, ack_timeout=0)
        d = _delivery(a.agent_id, b.agent_id)
        m._watch_ack(b, d)  # returns immediately
        self.assertIsNone(d.acknowledged_at)


class Durability(unittest.TestCase):
    def test_snapshot_and_load(self) -> None:
        reg, a, b = _pair()
        m = Messaging(reg)
        m._ensure_worker = lambda agent_id: None  # type: ignore[method-assign]
        d = _delivery(a.agent_id, b.agent_id, queued=True, text="x")
        d.expires_at = time.time() + 60
        m._queues[b.agent_id] = deque([d])
        snap = m.snapshot()
        self.assertEqual(len(snap), 1)

        m2 = Messaging(reg)
        m2._ensure_worker = lambda agent_id: None  # type: ignore[method-assign]
        self.assertEqual(m2.load(snap), 1)

    def test_load_skips_bad_and_expired(self) -> None:
        reg, a, b = _pair()
        m = Messaging(reg)
        m._ensure_worker = lambda agent_id: None  # type: ignore[method-assign]
        items = [
            {"bad": True},
            {"recipient": b.agent_id, "sender": a.agent_id, "body": "old",
             "expires_at": time.time() - 10},
        ]
        self.assertEqual(m.load(items), 0)

    def test_persist_swallows_store_error(self) -> None:
        reg, a, b = _pair()

        class BadStore:
            def save(self, items):
                raise OSError("disk full")

        m = Messaging(reg, store=BadStore())
        m._persist()  # must not raise

    def test_log_swallows_session_error(self) -> None:
        reg, a, b = _pair()

        def boom(type_, data):
            raise RuntimeError("log sink")

        b.session._log = boom  # type: ignore[method-assign]
        Messaging._log(b, "x", {})  # must not raise

    def test_record_prunes_keys(self) -> None:
        reg, a, b = _pair()
        m = Messaging(reg, history_limit=2)
        for i in range(5):
            m._record(_delivery(a.agent_id, b.agent_id, f"m{i}"), dedup_key=f"k{i}")
        self.assertLessEqual(len(m._keys), 2)

    def test_load_history_bad_store(self) -> None:
        reg, a, b = _pair()

        class BadHistory:
            def load(self):
                raise OSError("nope")

        m = Messaging(reg, history_store=BadHistory())
        self.assertEqual(m.load_history(), 0)

    def test_load_history_skips_bad_rows(self) -> None:
        reg, a, b = _pair()

        class Store:
            def load(self):
                return [{"nope": 1}, {"message_id": "m", "sender": "a", "recipient": "b",
                                      "body": "ok", "timestamp": 1.0}]

        m = Messaging(reg, history_store=Store())
        self.assertEqual(m.load_history(), 1)

    def test_close_sets_stop(self) -> None:
        reg, a, b = _pair()
        m = Messaging(reg)
        m.close()
        self.assertTrue(m._stop.is_set())


if __name__ == "__main__":
    unittest.main()
