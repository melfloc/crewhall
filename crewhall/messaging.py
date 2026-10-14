from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast

from .harness import AgentState, Harness
from .harness.base import HarnessNotReady
import contextlib

log = logging.getLogger("crewhall.messaging")


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
    # The chat conversation this message belongs to (None for plain team traffic).
    conversation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "body": self.body,
            "timestamp": self.timestamp,
            "conversation_id": self.conversation_id,
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
    #-- queue bookkeeping (internal, never serialized by to_dict) ---------
    # Text actually typed (already carries the ``[from: …]`` prefix or not).
    text: str = ""
    # Absolute wall-clock expiry of the queued item (None: no TTL).
    expires_at: float | None = None
    # Whether the queue worker already tried to revive the recipient once.
    revive_tried: bool = False
    # Why the message is queued: busy | human draft | starting | stopped.
    queue_reason: str | None = None
    # Insertion marker for history-ordered key pruning (internal).
    _seq: int = field(default=0, compare=False)

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
            "queue_reason": self.queue_reason,
            "conversation_id": self.message.conversation_id,
            "error": self.error,
        }


def delivery_from_dict(record: dict[str, Any]) -> Delivery | None:
    """Rebuild a Delivery from ``Delivery.to_dict()`` (history persistence)."""
    try:
        message = Message(
            message_id=str(record["message_id"]),
            sender=str(record["sender"]),
            recipient=str(record["recipient"]),
            body=str(record.get("body") or ""),
            timestamp=float(record.get("timestamp") or 0.0),
            conversation_id=record.get("conversation_id"),
        )
    except (KeyError, TypeError, ValueError):
        return None
    delivery = Delivery(
        message=message,
        delivered=bool(record.get("delivered")),
        sender_name=record.get("sender_name"),
        recipient_name=record.get("recipient_name"),
        queued=bool(record.get("queued")),
    )
    delivery.delivered_at = record.get("delivered_at")
    delivery.error = record.get("error")
    delivery.acknowledged_at = record.get("acknowledged_at")
    delivery.queue_reason = record.get("queue_reason")
    return delivery


class Messaging:
    """Agent-to-agent messaging with an ordered, durable per-recipient queue.

    Delivery is synchronous and means: the recipient ``Harness.send()`` accepted
    the body and injected it into its input (``InteractiveSession.write`` +
    ``send_enter``). It does NOT mean the agent finished processing the message,
    and it does NOT mean the model answered. If that stronger guarantee is ever
    needed, it belongs to a later layer (Inbox/Teams), not here.

    When a recipient is momentarily WORKING (or its input holds a human draft),
    messages are handed to a **FIFO queue with one worker per recipient**, so
    several messages to the same agent arrive in order (never interleaved or
    reordered). Queued entries are persisted (with their remaining TTL) and
    resumed on the next daemon start; a message carries at most one retry worker,
    not one thread per attempt.

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
        store: Any = None,
        history_store: Any = None,
        workers: Any = None,
    ) -> None:
        self._registry = registry
        self._ack_timeout = ack_timeout
        self._store = store
        self._history_store = history_store
        self._workers_reg = workers
        self._queues: dict[str, deque[Delivery]] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._keys: dict[str, Delivery] = {}
        self._history: deque[Delivery] = deque(maxlen=history_limit)
        self._lock = threading.RLock()
        self._send_locks: dict[str, threading.Lock] = {}
        self._stop = threading.Event()
        self._persist_lock = threading.Lock()
        self._seq = 0
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
        message_id: str | None = None,
        idempotency_key: str | None = None,
        conversation_id: str | None = None,
    ) -> Delivery:
        """Deliver a message to the recipient's own terminal.

        If the recipient is momentarily WORKING (e.g. an orchestrator blocked in
        its own turn waiting for this reply), a synchronous wait would deadlock
        the sender. Instead, after a short grace ``wait`` we append the message to
        the recipient's FIFO queue and return a Delivery marked ``queued=True``.

        Two calls with the same ``idempotency_key`` (or the same caller-supplied
        ``message_id``) return the very same Delivery; two identical bodies with
        no such key are two legitimate messages and are never merged.
        """
        sender_harness = self._resolve(sender, "sender")
        # What is typed into the recipient's terminal. Messages carry no sender
        # on their own, so agent-to-agent sends are attributed explicitly.
        text = (
            f"[from: {sender_harness.name or sender_harness.agent_id}] {body}"
            if prefix_sender
            else body
        )
        return self._deliver(
            sender_id=sender_harness.agent_id,
            sender_name=sender_harness.name,
            sender_harness=sender_harness,
            recipient=recipient,
            body=body,
            text=text,
            authorize=authorize,
            wait=wait,
            queue_if_busy=queue_if_busy,
            queue_ttl=queue_ttl,
            message_id=message_id,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
        )

    def send_system(
        self,
        recipient: str,
        body: str,
        *,
        origin: str,
        queue_ttl: float = 1800.0,
        idempotency_key: str | None = None,
        wait: float = 2.0,
        conversation_id: str | None = None,
    ) -> Delivery:
        """Deliver a message with no agent sender (e.g. an OnlyOffice comment).

        Same FIFO queue, persistence and acknowledgement as ``send`` but there is
        no sender to authenticate: the body is attributed to ``system:<origin>``.
        A short synchronous grace (``wait``) keeps a READY recipient instant; a
        WORKING one, or one holding a human draft, is queued and retried instead
        of losing the message.
        """
        return self._deliver(
            sender_id=f"system:{origin}",
            sender_name=origin,
            sender_harness=None,
            recipient=recipient,
            body=body,
            text=body,
            authorize=None,
            wait=wait,
            queue_if_busy=True,
            queue_ttl=queue_ttl,
            message_id=None,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
        )

    def _deliver(
        self,
        *,
        sender_id: str,
        sender_name: str | None,
        sender_harness: Harness | None,
        recipient: str,
        body: str,
        text: str,
        authorize: Any,
        wait: float,
        queue_if_busy: bool,
        queue_ttl: float,
        message_id: str | None,
        idempotency_key: str | None,
        conversation_id: str | None = None,
    ) -> Delivery:
        """Shared delivery path for ``send`` (agent) and ``send_system``."""
        recipient_harness = self._resolve(recipient, "recipient")
        if sender_harness is not None and sender_harness.agent_id == recipient_harness.agent_id:
            raise MessagingError(
                f"cannot send a message from an agent to itself "
                f"({sender_harness.agent_id})"
            )
        if authorize is not None:
            authorize(sender_harness, recipient_harness)

        dedup_key = idempotency_key or message_id
        if dedup_key:
            with self._lock:
                existing = self._keys.get(dedup_key)
            if existing is not None:
                return existing

        message = Message(
            message_id=message_id or new_message_id(),
            sender=sender_id,
            recipient=recipient_harness.agent_id,
            body=body,
            timestamp=time.time(),
            conversation_id=conversation_id,
        )
        delivery = Delivery(
            message=message,
            delivered=False,
            sender_name=sender_name,
            recipient_name=recipient_harness.name,
        )
        delivery.text = text
        if sender_harness is not None:
            self._log(
                sender_harness,
                "message_sent",
                {"message_id": message.message_id, "recipient": message.recipient,
                 "size": len(body)},
            )

        with self._lock_for(recipient_harness.agent_id):
            # Once anything is queued for this recipient, new messages line up
            # behind it: FIFO across both the synchronous and deferred paths.
            if len(self._queue_of(recipient_harness.agent_id)) > 0:
                return self._enqueue(
                    recipient_harness, delivery, queue_ttl, dedup_key)

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
                    if self._try_inject(recipient_harness, delivery, delivery.text):
                        self._record(delivery, dedup_key)
                        return delivery
                    if (
                        delivery.retryable
                        and queue_if_busy
                        and not self._terminal(recipient_harness)
                    ):
                        # Usable a moment ago but not safe to type into now
                        # (e.g. a human started drafting): queue and retry.
                        return self._enqueue(
                            recipient_harness, delivery, queue_ttl, dedup_key)
                elif queue_if_busy and not self._terminal(recipient_harness):
                    return self._enqueue(
                        recipient_harness, delivery, queue_ttl, dedup_key)
                else:
                    delivery.error = "recipient not usable"
                    self._log(
                        recipient_harness, "message_failed",
                        {"message_id": message.message_id,
                         "sender": message.sender, "error": delivery.error},
                    )

        self._record(delivery, dedup_key)
        return delivery

    # -- ordered queue -----------------------------------------------------
    def _queue_of(self, agent_id: str) -> deque[Delivery]:
        with self._lock:
            return self._queues.get(agent_id, deque())

    def queue_depth(self) -> dict[str, int]:
        """Pending items per recipient (health/metrics)."""
        with self._lock:
            return {agent_id: len(q) for agent_id, q in self._queues.items() if q}

    def queue_info(self, agent_id: str) -> dict[str, Any]:
        """How many messages are waiting for ``agent_id`` and how old the oldest is."""
        with self._lock:
            items = list(self._queues.get(agent_id) or [])
        if not items:
            return {"count": 0, "oldest_age": None, "oldest_since": None}
        oldest = min(d.message.timestamp for d in items)
        return {
            "count": len(items),
            "oldest_age": round(max(0.0, time.time() - oldest), 1),
            "oldest_since": oldest,
        }

    def _enqueue(
        self,
        recipient: Harness,
        delivery: Delivery,
        ttl: float,
        dedup_key: str | None,
    ) -> Delivery:
        delivery.queued = True
        # A retryable first attempt (busy, human draft) is not an error: the
        # message is waiting, so do not report the transient failure.
        delivery.error = None
        delivery.retryable = False
        # Report the real cause so the control plane can say "human draft"
        # instead of blaming a busy agent (Fase A3).
        delivery.queue_reason = self._queue_reason(recipient)
        delivery.expires_at = time.time() + ttl if ttl and ttl > 0 else None
        agent_id = recipient.agent_id
        with self._lock:
            self._queues.setdefault(agent_id, deque()).append(delivery)
        self._record(delivery, dedup_key)
        self._persist()
        self._ensure_worker(agent_id)
        return delivery

    def _ensure_worker(self, agent_id: str) -> None:
        if self._workers_reg is not None:
            self._workers_reg.spawn(
                f"msg-queue-{agent_id}", self._drain, args=(agent_id,)
            )
            return
        with self._lock:
            worker = self._workers.get(agent_id)
            if worker is not None and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._drain, args=(agent_id,),
                name=f"msg-queue-{agent_id}", daemon=True,
            )
            self._workers[agent_id] = worker
        worker.start()

    def _drain(self, agent_id: str) -> None:
        """Deliver this recipient's queue strictly in order.

        One worker per recipient: it only ever looks at the head item, so order
        is preserved even under concurrent sends.
        """
        while not self._stop.is_set():
            with self._lock:
                queue = self._queues.get(agent_id)
                if not queue:
                    self._queues.pop(agent_id, None)
                    self._workers.pop(agent_id, None)
                    return
                delivery = queue[0]

            if delivery.expires_at and time.time() > delivery.expires_at:
                self._drop(agent_id, delivery, error="queue ttl expired")
                continue

            try:
                harness = self._registry.resolve(agent_id)
            except KeyError:
                self._drop(agent_id, delivery, error="recipient removed")
                continue

            if self._terminal(harness):
                if self._revive is not None and not delivery.revive_tried:
                    delivery.revive_tried = True
                    try:
                        self._revive(harness)
                    except Exception:  # noqa: BLE001
                        log.debug("ignored exception in a boundary guard", exc_info=True)
                    with contextlib.suppress(KeyError):
                        harness = self._registry.resolve(agent_id)
                if self._terminal(harness):
                    self._drop(agent_id, delivery, error="recipient not usable")
                    continue

            if not harness.state().usable:
                self._stop.wait(0.5)
                continue

            with self._lock_for(agent_id):
                injected = self._try_inject(harness, delivery, delivery.text)
            if injected:
                self._log(harness, "message_delivered_deferred",
                          {"message_id": delivery.message.message_id})
                self._drop(agent_id, delivery)
                continue
            if delivery.retryable:
                self._stop.wait(0.5)
                continue
            self._drop(agent_id, delivery, error=delivery.error or "injection failed")

    def _drop(self, agent_id: str, delivery: Delivery, error: str | None = None) -> None:
        with self._lock:
            queue = self._queues.get(agent_id)
            if queue and queue[0] is delivery:
                queue.popleft()
            if not queue:
                self._queues.pop(agent_id, None)
                self._workers.pop(agent_id, None)
            if error is not None:
                # Given up: it is no longer queued, it failed (status "failed").
                delivery.queued = False
                if delivery.error is None:
                    delivery.error = error
        if error is not None:
            try:
                harness = self._registry.resolve(agent_id)
            except KeyError:
                harness = None
            if harness is not None:
                self._log(harness, "message_failed",
                          {"message_id": delivery.message.message_id,
                           "sender": delivery.message.sender, "error": delivery.error})
        self._persist()

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

        if self._workers_reg is not None:
            self._workers_reg.spawn(
                f"msg-ack-{delivery.message.message_id}", watch
            )
        else:
            threading.Thread(target=watch, name="msg-ack", daemon=True).start()

    def _terminal(self, harness: Harness) -> bool:
        return harness.state() in (AgentState.EXITED, AgentState.ERROR)

    def _queue_reason(self, harness: Harness) -> str:
        """Best-effort explanation of why a message had to be queued."""
        try:
            state = harness.state()
        except Exception:  # noqa: BLE001 - never let the diagnosis hide the queueing
            return "busy"
        if state == AgentState.WORKING:
            return "busy"
        if state == AgentState.STARTING:
            return "starting"
        if state in (AgentState.EXITED, AgentState.ERROR):
            return "stopped"
        # Usable state but the input holds text we did not type: a human draft.
        try:
            if not harness.input_is_clean():
                return "human draft"
        except Exception:  # noqa: BLE001
            return "busy"
        return "busy"

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

    def load_history(self) -> int:
        """Restore the message history persisted across daemon restarts."""
        if self._history_store is None:
            return 0
        try:
            records = self._history_store.load()
        except Exception:  # noqa: BLE001 - a bad history must not stop startup
            return 0
        loaded = 0
        with self._lock:
            for record in records:
                delivery = delivery_from_dict(record)
                if delivery is None:
                    continue
                self._seq += 1
                delivery._seq = self._seq
                self._history.append(delivery)
                loaded += 1
        return loaded

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
            return cast(Harness, self._registry.resolve(target))
        except KeyError as exc:
            raise MessagingError(f"unknown {role} {target!r}") from exc

    def _record(self, delivery: Delivery, dedup_key: str | None = None) -> None:
        with self._lock:
            self._seq += 1
            delivery._seq = self._seq
            self._history.append(delivery)
            if dedup_key:
                self._keys[dedup_key] = delivery
                while len(self._keys) > (self._history.maxlen or 1000):
                    self._keys.pop(next(iter(self._keys)))
        if self._history_store is not None:
            try:
                self._history_store.append(delivery.to_dict())
            except Exception:  # noqa: BLE001 - history must never break delivery
                log.debug("ignored exception in a boundary guard", exc_info=True)

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
            except Exception:  # noqa: BLE001 - a broken log sink must not break delivery
                log.debug("session log callback failed for %s", type_, exc_info=True)

    # -- durability --------------------------------------------------------
    def snapshot(self) -> list[dict[str, Any]]:
        """Queue contents, in order, for persistence across a daemon restart."""
        with self._lock:
            items = []
            for queue in self._queues.values():
                for delivery in queue:
                    items.append({
                        "message_id": delivery.message.message_id,
                        "sender": delivery.message.sender,
                        "recipient": delivery.message.recipient,
                        "sender_name": delivery.sender_name,
                        "recipient_name": delivery.recipient_name,
                        "body": delivery.message.body,
                        "text": delivery.text,
                        "timestamp": delivery.message.timestamp,
                        "expires_at": delivery.expires_at,
                        "queue_reason": delivery.queue_reason,
                        "conversation_id": delivery.message.conversation_id,
                    })
        return items

    def _persist(self) -> None:
        if self._store is None:
            return
        # Snapshot and write under one lock: two workers persisting concurrently
        # must not let an older snapshot land last (it would resurrect, and
        # re-deliver after a restart, a message that was already injected).
        with self._persist_lock:
            try:
                self._store.save(self.snapshot())
            except Exception:  # noqa: BLE001 - persistence must never break delivery
                log.debug("ignored exception in a boundary guard", exc_info=True)

    def load(self, items: list[dict[str, Any]], now: float | None = None) -> int:
        """Resume persisted pending messages (drops those already expired)."""
        now = time.time() if now is None else now
        resumed = 0
        for item in items or []:
            try:
                recipient = str(item["recipient"])
                sender = str(item["sender"])
                body = str(item["body"])
                message_id = str(item.get("message_id") or new_message_id())
                expires_at = item.get("expires_at")
            except (KeyError, TypeError):
                continue
            if expires_at is not None and float(expires_at) <= now:
                continue
            delivery = Delivery(
                message=Message(
                    message_id=message_id, sender=sender, recipient=recipient,
                    body=body, timestamp=float(item.get("timestamp") or now),
                    conversation_id=item.get("conversation_id"),
                ),
                delivered=False,
                queued=True,
                sender_name=item.get("sender_name"),
                recipient_name=item.get("recipient_name"),
                text=str(item.get("text") or body),
                expires_at=float(expires_at) if expires_at is not None else None,
                queue_reason=item.get("queue_reason"),
            )
            with self._lock:
                self._queues.setdefault(recipient, deque()).append(delivery)
            self._record(delivery)
            self._ensure_worker(recipient)
            resumed += 1
        if resumed:
            self._persist()
        return resumed

    def close(self) -> None:
        self._stop.set()
