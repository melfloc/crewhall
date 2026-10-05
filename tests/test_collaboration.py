from __future__ import annotations

import os
import tempfile
import threading
import unittest

from crewhall import Controller, Status
from crewhall.control_files import (
    BEGIN,
    END,
    ensure_managed_section,
)
from crewhall.messaging import Messaging, MessagingError

from .support import FakeHarness


class ControlFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-cf-")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, name="CLAUDE.md"):
        with open(os.path.join(self.tmp, name), encoding="utf-8") as fh:
            return fh.read()

    def test_creates_when_missing(self):
        result = ensure_managed_section(self.tmp)
        self.assertTrue(result.created)
        text = self._read()
        self.assertIn(BEGIN, text)
        self.assertIn(END, text)

    def test_preserves_existing_content(self):
        original = "# Existing Project Rules\n\nRegla A\nRegla B\n"
        with open(os.path.join(self.tmp, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(original)
        ensure_managed_section(self.tmp)
        text = self._read()
        self.assertTrue(text.startswith(original))
        self.assertIn(BEGIN, text)

    def test_idempotent(self):
        ensure_managed_section(self.tmp)
        first = self._read()
        result = ensure_managed_section(self.tmp)
        self.assertTrue(result.unchanged)
        self.assertEqual(self._read(), first)

    def test_updates_managed_section_without_touching_user_content(self):
        original = "user line 1\nuser line 2\n"
        with open(os.path.join(self.tmp, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(original)
        ensure_managed_section(self.tmp)
        # Tamper only inside the managed block.
        text = self._read()
        header = next(
            line for line in text.splitlines()
            if line.startswith("## ") and line != BEGIN
        )
        tampered = text.replace(header, "## TAMPERED")
        with open(os.path.join(self.tmp, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write(tampered)
        result = ensure_managed_section(self.tmp)
        self.assertTrue(result.updated)
        final = self._read()
        self.assertIn(header, final)
        self.assertTrue(final.startswith(original))
        self.assertEqual(final.count(BEGIN), 1)
        self.assertEqual(final.count(END), 1)

    def test_multiple_directories_independent(self):
        other = tempfile.mkdtemp(prefix="at-cf2-")
        self.addCleanup(lambda: __import__("shutil").rmtree(other, ignore_errors=True))
        ensure_managed_section(self.tmp)
        ensure_managed_section(other)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "CLAUDE.md")))
        self.assertTrue(os.path.isfile(os.path.join(other, "CLAUDE.md")))

    def test_does_not_touch_non_existent_dir(self):
        self.assertIsNone(ensure_managed_section("/no/such/dir/xyz"))

    def test_section_not_duplicated_after_many_runs(self):
        for _ in range(10):
            ensure_managed_section(self.tmp)
        text = self._read()
        self.assertEqual(text.count(BEGIN), 1)
        self.assertEqual(text.count(END), 1)


class CollaborationBase(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)

    def _agent(self, name):
        h = FakeHarness(name)
        self.controller.register_agent(h)
        return h

    def _token(self, harness):
        return self.controller._agent_token(harness.agent_id)


class Identity(CollaborationBase):
    def test_identity_and_teams(self):
        a = self._agent("auditor")
        self._agent("extractor")
        self._agent("outsider")
        self.controller.create_team("fiscal", ["auditor", "extractor"])
        self.controller.create_team("legal", ["outsider"])
        info = self.controller.agent_identity(a.agent_id, self._token(a))
        self.assertEqual(info["teams"], ["fiscal"])
        names = {m["name"] for m in info["teammates"]}
        self.assertEqual(names, {"extractor"})
        self.assertNotIn("outsider", names)

    def test_multiple_teams_union(self):
        a = self._agent("a")
        self._agent("b")
        self._agent("c")
        self.controller.create_team("fiscal", ["a", "b"])
        self.controller.create_team("auditoria", ["a", "c"])
        info = self.controller.agent_identity(a.agent_id, self._token(a))
        self.assertEqual(set(info["teams"]), {"fiscal", "auditoria"})
        self.assertEqual({m["name"] for m in info["teammates"]}, {"b", "c"})

    def test_ungrouped_has_no_teammates(self):
        a = self._agent("a")
        info = self.controller.agent_identity(a.agent_id, self._token(a))
        self.assertEqual(info["teams"], [])
        self.assertEqual(info["teammates"], [])

    def test_invalid_token_rejected(self):
        a = self._agent("a")
        with self.assertRaises(MessagingError):
            self.controller.agent_identity(a.agent_id, "wrong-token")


class Authorization(CollaborationBase):
    def setUp(self):
        super().setUp()
        self.a = self._agent("a")
        self.b = self._agent("b")
        self.c = self._agent("c")
        self.d = self._agent("d")
        self.controller.create_team("fiscal", ["a", "b"])
        self.controller.create_team("dev", ["c"])

    def test_shared_team_allowed(self):
        delivery = self.controller.send_message_as(
            "a", self._token(self.a), "b", "hola"
        )
        self.assertTrue(delivery.delivered)
        self.assertEqual(self.b.session.writes, ["[from: a] hola"])

    def test_no_shared_team_denied(self):
        with self.assertRaises(MessagingError) as ctx:
            self.controller.send_message_as("a", self._token(self.a), "c", "nope")
        self.assertIn("shared Team", str(ctx.exception))
        self.assertEqual(self.c.session.writes, [])

    def test_ungrouped_cannot_send_or_receive(self):
        with self.assertRaises(MessagingError):
            self.controller.send_message_as("d", self._token(self.d), "a", "hi")
        with self.assertRaises(MessagingError):
            self.controller.send_message_as("a", self._token(self.a), "d", "hi")

    def test_multi_team_intersection(self):
        self.controller.create_team("auditoria", ["a", "c"])
        delivery = self.controller.send_message_as("a", self._token(self.a), "c", "ok")
        self.assertTrue(delivery.delivered)

    def test_multiteam_b_c_denied(self):
        # Put a in both fiscal and auditoria so it can talk to b and c, but b/c
        # still share no Team with each other.
        self.controller.create_team("auditoria", ["a", "c"])
        with self.assertRaises(MessagingError):
            self.controller.send_message_as("b", self._token(self.b), "c", "x")

    def test_unknown_recipient(self):
        with self.assertRaises(MessagingError):
            self.controller.send_message_as("a", self._token(self.a), "ghost", "x")

    def test_invalid_sender_token(self):
        with self.assertRaises(MessagingError):
            self.controller.send_message_as("a", "bad", "b", "x")

    def test_sender_derived_from_token_not_body(self):
        # Sender identity comes from the token; the CLI cannot spoof it.
        delivery = self.controller.send_message_as("a", self._token(self.a), "b", "msg")
        self.assertEqual(delivery.message.sender, self.a.agent_id)
        self.assertEqual(delivery.message.recipient, self.b.agent_id)


class Activation(CollaborationBase):
    def test_revive_called_when_recipient_stopped(self):
        a = self._agent("a")
        b = self._agent("b")
        revives: list[str] = []

        def revive(harness):
            revives.append(harness.agent_id)
            harness.session.status = Status.RUNNING

        messaging = Messaging(self.controller.agents, revive=revive)
        b.session.status = Status.EXITED
        delivery = messaging.send(a.agent_id, b.agent_id, "wake up")
        self.assertTrue(delivery.delivered)
        self.assertEqual(revives, [b.agent_id])
        self.assertEqual(b.session.writes, ["wake up"])

    def test_no_revive_when_alive(self):
        a = self._agent("a")
        b = self._agent("b")
        revives: list[str] = []
        messaging = Messaging(
            self.controller.agents, revive=lambda h: revives.append(h.agent_id)
        )
        messaging.send(a.agent_id, b.agent_id, "hi")
        self.assertEqual(revives, [])

    def test_revive_failure_reports_failed_delivery(self):
        a = self._agent("a")
        b = self._agent("b")

        def revive(_harness):
            raise RuntimeError("cannot start")

        messaging = Messaging(self.controller.agents, revive=revive)
        b.session.status = Status.EXITED
        delivery = messaging.send(a.agent_id, b.agent_id, "hi")
        self.assertFalse(delivery.delivered)
        self.assertIsNotNone(delivery.error)


class BusyRecipientDelivery(CollaborationBase):
    """A reply to an agent that is momentarily WORKING must not be lost."""

    def test_send_waits_for_busy_recipient(self):
        from crewhall.harness import AgentState
        from crewhall.messaging import Messaging
        from tests.support import FakeHarness

        class Flaky(FakeHarness):
            def __init__(self, name):
                super().__init__(name)
                self._calls = 0

            def state(self):
                self._calls += 1
                if self._calls <= 2:
                    return AgentState.WORKING
                return AgentState.READY

        a = FakeHarness("a")
        b = Flaky("b")
        registry = self.controller.agents
        self.controller.register_agent(a)
        registry.add(b)
        messaging = Messaging(registry)
        delivery = messaging.send(a.agent_id, b.agent_id, "reply", wait=5.0)
        self.assertTrue(delivery.delivered, delivery.error)
        self.assertIn("reply", b.session.writes)

    def test_send_fails_when_recipient_never_usable(self):
        from crewhall.harness import AgentState
        from crewhall.messaging import Messaging
        from tests.support import FakeHarness

        class AlwaysWorking(FakeHarness):
            def state(self):
                return AgentState.WORKING

        a = FakeHarness("a")
        b = AlwaysWorking("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents)
        delivery = messaging.send(
            a.agent_id, b.agent_id, "x", wait=0.5, queue_if_busy=False
        )
        self.assertFalse(delivery.delivered)
        self.assertIsNotNone(delivery.error)

    def test_not_ready_inject_is_retried_not_reported_delivered(self):
        # The recipient looked usable but could not safely take input (e.g. an
        # overlay). Harness.send raises HarnessNotReady: that must NOT count as
        # delivered, and must be retried (bounded) instead of failing for good.
        import time as _t

        from crewhall.harness.base import HarnessNotReady
        from crewhall.messaging import Messaging
        from tests.support import FakeHarness

        class Flaky(FakeHarness):
            attempts = 0

            def send(self, prompt, timeout=30.0):
                Flaky.attempts += 1
                if Flaky.attempts == 1:
                    raise HarnessNotReady(self.agent_id, "overlay open")
                super().send(prompt, timeout=timeout)

        a = FakeHarness("a")
        b = Flaky("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents)
        delivery = messaging.send(a.agent_id, b.agent_id, "THE_REPLY", wait=1.0)
        self.assertFalse(delivery.delivered)
        self.assertTrue(delivery.queued)
        self.assertNotIn("THE_REPLY", b.session.writes)
        deadline = _t.monotonic() + 8
        while not delivery.delivered and _t.monotonic() < deadline:
            _t.sleep(0.1)
        self.assertTrue(delivery.delivered, delivery.error)
        self.assertEqual(b.session.writes, ["THE_REPLY"])

    def test_busy_recipient_is_deferred_then_delivered(self):
        import time as _t

        from crewhall.harness import AgentState
        from crewhall.messaging import Messaging
        from tests.support import FakeHarness

        class LateReady(FakeHarness):
            def __init__(self, name):
                super().__init__(name)
                self._t0 = _t.monotonic()

            def state(self):
                if _t.monotonic() - self._t0 < 1.5:
                    return AgentState.WORKING
                return AgentState.READY

        a = FakeHarness("a")
        b = LateReady("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents)
        delivery = messaging.send(a.agent_id, b.agent_id, "deferred-ok", wait=0.3)
        self.assertTrue(delivery.queued, "busy recipient should be queued")
        # The background worker delivers once the recipient becomes usable.
        deadline = _t.monotonic() + 6
        while _t.monotonic() < deadline and "deferred-ok" not in b.session.writes:
            _t.sleep(0.2)
        self.assertIn("deferred-ok", b.session.writes)


class Concurrency(CollaborationBase):
    def test_two_senders_same_recipient(self):
        a = self._agent("a")
        b = self._agent("b")
        c = self._agent("c")
        self.controller.create_team("fiscal", ["a", "b", "c"])
        errors: list[Exception] = []

        def send(sender, body):
            try:
                self.controller.send_message_as(sender, self._token(
                    {"a": a, "c": c}[sender]), "b", body)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=send, args=("a", "from-a")),
            threading.Thread(target=send, args=("c", "from-c")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(b.session.writes), ["[from: a] from-a", "[from: c] from-c"])


if __name__ == "__main__":
    unittest.main()


class DeliveryStatusAndDedup(CollaborationBase):
    def test_status_progression_and_ack(self):
        import time as _t

        from crewhall.harness import AgentState
        from crewhall.messaging import Messaging
        from tests.support import FakeHarness

        class Reacting(FakeHarness):
            working = False

            def send(self, prompt, timeout=30.0):
                super().send(prompt, timeout=timeout)
                Reacting.working = True

            def _detect(self, text):
                if Reacting.working:
                    return AgentState.WORKING, "working"
                return AgentState.READY, "ready"

        a, b = FakeHarness("a"), Reacting("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=3)
        delivery = messaging.send(a.agent_id, b.agent_id, "hi", wait=1.0)
        self.assertTrue(delivery.delivered)
        deadline = _t.monotonic() + 3
        while delivery.status != "acknowledged" and _t.monotonic() < deadline:
            _t.sleep(0.05)
        self.assertEqual(delivery.status, "acknowledged")
        self.assertEqual(delivery.to_dict()["status"], "acknowledged")

    def test_unacknowledged_stays_injected(self):
        from crewhall.messaging import Messaging
        from tests.support import FakeHarness

        a, b = FakeHarness("a"), FakeHarness("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=0)
        delivery = messaging.send(a.agent_id, b.agent_id, "hi", wait=1.0)
        self.assertEqual(delivery.status, "injected")

    def test_identical_pending_message_is_not_duplicated(self):
        import time as _t

        from crewhall.harness import AgentState
        from crewhall.messaging import Messaging
        from tests.support import FakeHarness

        class Busy(FakeHarness):
            busy = True

            def _detect(self, text):
                return (AgentState.WORKING, "w") if Busy.busy else (AgentState.READY, "r")

        a, b = FakeHarness("a"), Busy("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        messaging = Messaging(self.controller.agents, ack_timeout=0)
        d1 = messaging.send(a.agent_id, b.agent_id, "same", wait=0.2)
        d2 = messaging.send(a.agent_id, b.agent_id, "same", wait=0.2)
        self.assertIs(d1, d2)
        self.assertTrue(d1.queued)
        Busy.busy = False
        deadline = _t.monotonic() + 8
        while not d1.delivered and _t.monotonic() < deadline:
            _t.sleep(0.1)
        self.assertTrue(d1.delivered)
        self.assertEqual(b.session.writes, ["same"])
        # Once delivered, the same text is a new, legitimate message.
        d3 = messaging.send(a.agent_id, b.agent_id, "same", wait=1.0)
        self.assertIsNot(d3, d1)


class AgentSendDoesNotBlockTheSender(CollaborationBase):
    def test_busy_recipient_returns_quickly_as_queued_when_an_agent_sends(self):
        import time as _t

        from crewhall.harness import AgentState
        from tests.support import FakeHarness

        class Busy(FakeHarness):
            def _detect(self, text):
                return AgentState.WORKING, "busy"

        a, b = FakeHarness("a"), Busy("b")
        self.controller.register_agent(a)
        self.controller.agents.add(b)
        self.controller.create_team("t", [a.agent_id, b.agent_id])
        token = self.controller._agent_token(a.agent_id)
        t0 = _t.monotonic()
        delivery = self.controller.send_message_as(a.agent_id, token, b.agent_id, "hola")
        self.assertLess(_t.monotonic() - t0, 12)          # was up to 30 s of a blocked agent turn
        self.assertTrue(delivery.queued)
