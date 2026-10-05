from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

from .harness import AgentState, Harness
from .harness.base import HarnessNotReady


class MessagingError(RuntimeError):
    pass


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class Message:
    """An immutable, transport-agnostic message intent between two agents.

    ``sender`` and ``recipient`` are public agent identities (``agent_id`` from
    the AgentRegistry), never tmux panes, pids or sessions.
    """

    message_id: str
    sender: str
    recipient: str
    body: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "body": self.body,
            "timestamp": self.timestamp,
        }


@dataclass
class Delivery:
    """Outcome of attempting to deliver a Message to the recipient harness."""

    message: Message
    delivered: bool
    delivered_at: float | None = None
    error: str | None = None
    sender_name: str | None = None
    recipient_name: str | None = None
    queued: bool = False
    # Last inject failed because the recipient was momentarily not ready
    # (retryable); internal, not part of to_dict().
    retryable: bool = False
    # Set when the recipient visibly started working after the injection.
    acknowledged_at: float | None = None

    @property
    def status(self) -> str:
        """failed | queued | injected | acknowledged (``delivered`` = injected)."""
        if self.delivered:
            return "acknowledged" if self.acknowledged_at else "injected"
        if self.queued:
            return "queued"
        return "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message.message_id,
            "sender": self.message.sender,
            "sender_name": self.sender_name,
            "recipient": self.message.recipient,
            "recipient_name": self.recipient_name,
            "body": self.message.body,
            "timestamp": self.message.timestamp,
            "delivered": self.delivered,
            "delivered_at": self.delivered_at,
            "queued": self.queued,
            "status": self.status,
            "acknowledged_at": self.acknowledged_at,
            "error": self.error,
        }


class Messaging:
    """Minimal agent-to-agent messaging over the Harness contract.

    Delivery is synchronous and means: the recipient ``Harness.send()`` accepted
    the body and injected it into its input (``InteractiveSession.write`` +
    ``send_enter``). It does NOT mean the agent finished processing the message,
    and it does NOT mean the model answered. If that stronger guarantee is ever
    needed, it belongs to a later layer (Inbox/Teams), not here.

    This layer knows only agent identity and the ``Harness.send`` method. It has
    no knowledge of OpenCode, Claude Code, tmux or PTYs.
    """

    def __init__(
        self,
        registry: Any,
        *,
        history_limit: int = 1000,
        revive: Any = None,
        ack_timeout: float = 15.0,
    ) -> None:
        self._registry = registry
        self._ack_timeout = ack_timeout
        # (sender, recipient, body) -> Delivery still waiting in the retry queue.
        self._pending: dict[tuple[str, str, str], Delivery] = {}
        self._history: deque[Delivery] = deque(maxlen=history_limit)
        self._lock = threading.RLock()
        self._send_locks: dict[str, threading.Lock] = {}
        # Optional callable(harness) that restarts a stopped agent on demand.
        self._revive = revive

    def send(
        self,
        sender: str,
        recipient: str,
        body: str,
        *,
        authorize: Any = None,
        wait: float = 30.0,
        queue_if_busy: bool = True,
        queue_ttl: float = 1800.0,
        prefix_sender: bool = False,
    ) -> Delivery:
        """Deliver a message to the recipient's own terminal.

        If the recipient is momentarily WORKING (e.g. an orchestrator blocked in
        its own turn waiting for this reply), a synchronous wait would deadlock
        the sender. Instead, after a short grace ``wait`` we hand the message to
        a deferred worker that keeps trying until the recipient is usable again
        (bounded by ``queue_ttl``); the returned Delivery is marked
        ``queued=True``. This is a bounded retry, not a persistent queue.
        """
        sender_harness = self._resolve(sender, "sender")
        recipient_harness = self._resolve(recipient, "recipient")
        if sender_harness.agent_id == recipient_harness.agent_id:
            raise MessagingError(
                f"cannot send a message from an agent to itself "
                f"({sender_harness.agent_id})"
            )
        if authorize is not None:
            authorize(sender_harness, recipient_harness)

        key = (sender_harness.agent_id, recipient_harness.agent_id, body)
        with self._lock:
            pending = self._pending.get(key)
        if pending is not None and pending.queued and not pending.delivered:
            # Same message still waiting for a busy recipient: do not duplicate.
            return pending

        message = Message(
            message_id=new_message_id(),
            sender=sender_harness.agent_id,
            recipient=recipient_harness.agent_id,
            body=body,
            timestamp=time.time(),
        )
        delivery = Delivery(
            message=message,
            delivered=False,
            sender_name=sender_harness.name,
            recipient_name=recipient_harness.name,
        )
        # What is typed into the recipient's terminal. Messages carry no sender
        # on their own, so agent-to-agent sends are attributed explicitly.
        text = (
            f"[from: {sender_harness.name or sender_harness.agent_id}] {body}"
            if prefix_sender
            else body
        )
        self._log(
            sender_harness,
            "message_sent",
            {"message_id": message.message_id, "recipient": message.recipient,
             "size": len(body)},
        )

        with self._lock_for(recipient_harness.agent_id):
            if self._revive is not None and not recipient_harness.session.status.alive:
                self._log(
                    recipient_harness,
                    "agent_started_by_message",
                    {"message_id": message.message_id, "sender": message.sender},
                )
                try:
                    self._revive(recipient_harness)
                except Exception as exc:  # noqa: BLE001
                    delivery.error = f"{type(exc).__name__}: {exc}"
                recipient_harness = self._registry.resolve(recipient_harness.agent_id)

            if delivery.error is None:
                # Short synchronous attempt so READY recipients deliver now.
                if self._await_usable(recipient_harness, wait, poll=0.25):
                    if (
                        not self._try_inject(recipient_harness, delivery, text)
                        and delivery.retryable
                        and queue_if_busy
                        and not self._terminal(recipient_harness)
                    ):
                        # Usable a moment ago but not safe to type into now:
                        # hand over to the bounded retry worker.
                        delivery.queued = True
                        self._record(delivery)
                        self._defer(
                            recipient_harness.agent_id, delivery, text, queue_ttl,
                            key,
                        )
                        return delivery
                elif queue_if_busy and not self._terminal(recipient_harness):
                    delivery.queued = True
                    self._record(delivery)
                    self._defer(recipient_harness.agent_id, delivery, text, queue_ttl, key)
                    return delivery
                else:
                    delivery.error = "recipient not usable"
                    self._log(
                        recipient_harness, "message_failed",
                        {"message_id": message.message_id,
                         "sender": message.sender, "error": delivery.error},
                    )

        self._record(delivery)
        return delivery

    # -- delivery helpers --------------------------------------------------
    def _try_inject(self, harness: Harness, delivery: Delivery, body: str) -> bool:
        try:
            harness.send(body)
        except Exception as exc:  # noqa: BLE001
            delivery.error = f"{type(exc).__name__}: {exc}"
            delivery.retryable = isinstance(exc, HarnessNotReady)
            self._log(
                harness, "message_failed",
                {"message_id": delivery.message.message_id,
                 "sender": delivery.message.sender, "error": delivery.error},
            )
            return False
        delivery.delivered = True
        delivery.queued = False
        delivery.retryable = False
        delivery.error = None
        delivery.delivered_at = time.time()
        self._log(
            harness, "message_delivered",
            {"message_id": delivery.message.message_id,
             "sender": delivery.message.sender},
        )
        self._watch_ack(harness, delivery)
        return True

    def _watch_ack(self, harness: Harness, delivery: Delivery) -> None:
        """Mark the delivery acknowledged once the recipient starts working.

        Best effort and bounded: it only upgrades ``injected`` to
        ``acknowledged``; delivery semantics are unchanged.
        """
        if self._ack_timeout <= 0:
            return

        def watch() -> None:
            deadline = time.monotonic() + self._ack_timeout
            while time.monotonic() < deadline:
                try:
                    if harness.state() == AgentState.WORKING:
                        delivery.acknowledged_at = time.time()
                        self._log(
                            harness, "message_acknowledged",
                            {"message_id": delivery.message.message_id},
                        )
                        return
                except Exception:  # noqa: BLE001
                    return
                time.sleep(0.25)

        threading.Thread(target=watch, name="msg-ack", daemon=True).start()

    def _terminal(self, harness: Harness) -> bool:
        return harness.state() in (AgentState.EXITED, AgentState.ERROR)

    def _defer(
        self,
        agent_id: str,
        delivery: Delivery,
        body: str,
        ttl: float,
        key: tuple[str, str, str] | None = None,
    ) -> None:
        if key is not None:
            with self._lock:
                self._pending[key] = delivery

        def forget() -> None:
            if key is not None:
                with self._lock:
                    if self._pending.get(key) is delivery:
                        del self._pending[key]

        def worker() -> None:
            try:
                run()
            finally:
                forget()

        def run() -> None:
            deadline = time.monotonic() + ttl
            while time.monotonic() < deadline:
                time.sleep(1.0)
                try:
                    harness = self._registry.resolve(agent_id)
                except KeyError:
                    return  # agent removed
                if self._terminal(harness):
                    return
                if not harness.state().usable:
                    continue
                with self._lock_for(agent_id):
                    if self._try_inject(harness, delivery, body):
                        self._log(harness, "message_delivered_deferred",
                                  {"message_id": delivery.message.message_id})
                        return
        threading.Thread(target=worker, name=f"msg-defer-{agent_id}", daemon=True).start()

    def history(
        self,
        *,
        agent: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._history)
        if agent is not None:
            items = [
                d
                for d in items
                if agent
                in (
                    d.message.sender,
                    d.message.recipient,
                    d.sender_name,
                    d.recipient_name,
                )
            ]
        if limit is not None:
            items = items[-limit:]
        return [d.to_dict() for d in items]

    def _await_usable(self, harness: Harness, timeout: float, poll: float = 0.3) -> bool:
        """Wait (bounded) until the recipient can accept input.

        Returns True as soon as the agent is READY/WAITING_INPUT; False on a
        terminal state or timeout. This is a short synchronous grace period
        before deferring to the background retry worker.
        """
        if timeout <= 0:
            return harness.state().usable
        deadline = time.monotonic() + timeout
        while True:
            state = harness.state()
            if state.usable:
                return True
            if state in (AgentState.EXITED, AgentState.ERROR):
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    def _resolve(self, target: str, role: str) -> Harness:
        try:
            return self._registry.resolve(target)
        except KeyError as exc:
            raise MessagingError(f"unknown {role} {target!r}") from exc

    def _record(self, delivery: Delivery) -> None:
        with self._lock:
            self._history.append(delivery)

    def _lock_for(self, agent_id: str) -> threading.Lock:
        with self._lock:
            lock = self._send_locks.get(agent_id)
            if lock is None:
                lock = threading.Lock()
                self._send_locks[agent_id] = lock
            return lock

    @staticmethod
    def _log(harness: Harness, type_: str, data: dict[str, Any]) -> None:
        session = getattr(harness, "session", None)
        logger = getattr(session, "_log", None)
        if callable(logger):
            try:
                logger(type_, data)
            except Exception:
                pass
