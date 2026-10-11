from __future__ import annotations

import contextlib
import io
import unittest

from crewhall.cli import _print_delivery
from crewhall.harness import AgentState
from crewhall.messaging import Messaging
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


class WorkingHarness(FakeHarness):
    def _detect(self, text):
        return AgentState.WORKING, "a turn is running"


class StartingHarness(FakeHarness):
    def _detect(self, text):
        return AgentState.STARTING, "still starting"


class DraftHarness(FakeHarness):
    def _detect(self, text):
        return AgentState.READY, "ready"

    def input_line(self):
        return "a human draft"


def _queue_reason_for(recipient: FakeHarness) -> str:
    sender = FakeHarness(name="sender")
    registry = _Registry({sender.agent_id: sender, recipient.agent_id: recipient})
    messaging = Messaging(registry)
    delivery = messaging.send(sender.agent_id, recipient.agent_id, "ping", wait=0.0)
    assert delivery.queued, "expected the message to queue"
    return delivery.queue_reason or ""


class QueueReason(unittest.TestCase):
    """Fase A3: report *why* a message was queued, not always "busy"."""

    def test_busy_recipient(self) -> None:
        self.assertEqual(_queue_reason_for(WorkingHarness(name="r")), "busy")

    def test_starting_recipient(self) -> None:
        self.assertEqual(_queue_reason_for(StartingHarness(name="r")), "starting")

    def test_human_draft(self) -> None:
        self.assertEqual(_queue_reason_for(DraftHarness(name="r")), "human draft")

    def test_cli_reports_the_reason(self) -> None:
        cases = {
            "busy": "recipient busy",
            "human draft": "human draft",
            "starting": "still starting",
        }
        for reason, expected in cases.items():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                _print_delivery({
                    "message_id": "m1", "sender": "a", "recipient": "b",
                    "body": "hi", "queued": True, "delivered": False,
                    "queue_reason": reason,
                })
            self.assertIn(expected, out.getvalue(), msg=reason)


if __name__ == "__main__":
    unittest.main()
