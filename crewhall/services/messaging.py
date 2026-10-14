"""Controller services: messaging (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import collections
import time
from typing import Any
from .. import settings
from ..harness import Harness, HarnessError
from ..messaging import Delivery, MessagingError
from ..controller import AGENT_SEND_WAIT


class ServicesMessaging:

    def new_session(self, harness: Harness, timeout: float = 20.0) -> dict[str, Any]:
        """Make the agent's TUI start a clean conversation (/clear, /new).

        Waits up to ``timeout`` for the agent to be idle, then refuses
        (HarnessError): nothing is queued.
        """
        self._assert_operable(harness)
        commands = harness.new_session_commands
        if not commands:
            raise HarnessError(harness.agent_id, f"{harness.kind} agents have no new-session command")
        previous = harness.conversation_id
        harness.send(commands[0], timeout=timeout)
        deadline = time.monotonic() + timeout
        confirmed = False
        while time.monotonic() < deadline:
            if harness.uses_hooks:  # the SessionStart hook reports the new id
                confirmed = harness.conversation_id not in (None, previous)
            else:
                on_home = getattr(harness, "on_home_screen", None)
                confirmed = bool(on_home and on_home())
            if confirmed:
                break
            time.sleep(0.2)
        return {
            "agent": self.agent_summary(harness), "command": commands[0],
            "previous": previous, "conversation_id": harness.conversation_id,
            "confirmed": confirmed,
        }

    def new_session_as(
        self, sender: str, token: str | None, target: str, timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Agent-to-agent: clear a teammate's conversation (Team boundary enforced)."""
        sender_harness = self.agents.resolve(sender)
        self._check_token(sender_harness.agent_id, token)
        recipient = self.agents.resolve(target)
        self._authorize_team(sender_harness, recipient)
        if recipient.agent_id == sender_harness.agent_id:
            raise MessagingError("an agent cannot clear its own session this way")
        result = self.new_session(recipient, timeout=timeout)
        logger = getattr(recipient.session, "_log", None)
        if callable(logger):
            logger("new_session", {"by": sender_harness.agent_id})
        return result

    def send_message_as(
        self, sender: str, token: str | None, recipient: str, body: str,
        idempotency_key: str | None = None, message_id: str | None = None,
        conversation_id: str | None = None,
    ) -> Delivery:
        """Agent-to-agent send: identity from token + Team boundary enforced."""
        sender_harness = self.agents.resolve(sender)
        self._check_token(sender_harness.agent_id, token)
        self._check_send_limits(sender_harness.agent_id, body)
        return self.messaging.send(
            sender_harness.agent_id, recipient, body,
            authorize=self._authorize_team, prefix_sender=True,
            idempotency_key=idempotency_key, message_id=message_id,
            conversation_id=conversation_id,
            # Agents are told not to wait for answers: when the recipient is busy, hand over to the
            # bounded retry queue quickly instead of blocking the sender's turn for up to 30 s.
            wait=AGENT_SEND_WAIT,
        )

    def _check_send_limits(self, agent_id: str, body: str) -> None:
        """Anti-loop guards for agent-to-agent sends (D2): a size cap and a
        per-agent rate limit. Both are generous and configurable."""
        max_chars = int(settings.get("messaging.max_body_chars"))
        if len(body) > max_chars:
            raise MessagingError(f"message too large ({len(body)} > {max_chars} characters)")
        limit = int(settings.get("messaging.rate_per_minute"))
        if limit <= 0:
            return
        now = time.time()
        with self._locks_guard:
            times = self._send_times.setdefault(agent_id, collections.deque())
            while times and now - times[0] > 60.0:
                times.popleft()
            if len(times) >= limit:
                raise MessagingError("rate limit: too many messages; slow down")
            times.append(now)

    def send_message(self, sender: str, recipient: str, body: str,
                     idempotency_key: str | None = None,
                     message_id: str | None = None,
                     conversation_id: str | None = None) -> Delivery:
        return self.messaging.send(
            sender, recipient, body, prefix_sender=True,
            idempotency_key=idempotency_key, message_id=message_id,
            conversation_id=conversation_id,
            # Control plane (CLI/Web): don't block for 30 s before queueing; a
            # short grace is enough to deliver to a READY recipient, then the
            # queue takes over and reports the real reason (Fase A3).
            wait=AGENT_SEND_WAIT,
        )
