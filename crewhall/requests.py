"""Request/response between agents: an ``assign`` with a correlated answer.

A request is a message that expects exactly one reply from the original
recipient. It builds on ``Messaging`` (delivery/ack) but never infers
completion from terminal quietness: the answer is another message, matched by
``request_id``. State lives in bounded, reconstructible memory (no secrets).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

PENDING = "pending"
ACCEPTED = "accepted"
REPLIED = "replied"
EXPIRED = "expired"
CANCELLED = "cancelled"
FAILED = "failed"
OPEN_STATES = (PENDING, ACCEPTED)


class RequestError(RuntimeError):
    pass


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]


@dataclass
class Request:
    request_id: str
    sender: str
    recipient: str
    task: str
    created_at: float
    deadline: float
    state: str = PENDING
    reply: str | None = None
    replied_at: float | None = None
    replied_by: str | None = None
    accepted_at: float | None = None
    depth: int = 1
    history: list[str] = field(default_factory=list)  # chain of agent ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "sender": self.sender, "recipient": self.recipient,
            "task": self.task, "created_at": self.created_at, "deadline": self.deadline,
            "state": self.state, "reply": self.reply, "replied_at": self.replied_at,
            "replied_by": self.replied_by, "accepted_at": self.accepted_at, "depth": self.depth,
        }


class RequestBoard:
    """Bounded registry of open and recently answered requests."""

    def __init__(self, *, max_open_per_agent: int = 2, max_depth: int = 4,
                 default_timeout: float = 300.0, max_body: int = 8000,
                 retention: int = 500) -> None:
        self.max_open_per_agent = max_open_per_agent
        self.max_depth = max_depth
        self.default_timeout = default_timeout
        self.max_body = max_body
        self._retention = retention
        self._lock = threading.RLock()
        self._items: dict[str, Request] = {}
        self._order: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def create(self, sender: str, recipient: str, task: str, *,
               timeout: float | None = None, parent: str | None = None) -> Request:
        task = (task or "").strip()
        if not task:
            raise RequestError("the request task is empty")
        if len(task) > self.max_body:
            raise RequestError(f"request body exceeds {self.max_body} characters")
        if sender == recipient:
            raise RequestError("cannot request from yourself")
        now = time.time()
        self.sweep()
        with self._lock:
            open_for_sender = [r for r in self._items.values()
                               if r.sender == sender and r.state in OPEN_STATES]
            if len(open_for_sender) >= self.max_open_per_agent:
                raise RequestError(
                    f"{sender} already has {len(open_for_sender)} open requests "
                    f"(max {self.max_open_per_agent})"
                )
            depth = 1
            history = [sender, recipient]
            if parent:
                parent_req = self._items.get(parent)
                if parent_req is not None:
                    depth = parent_req.depth + 1
                    history = [*parent_req.history, recipient]
            if depth > self.max_depth:
                raise RequestError(f"request chain too deep (max {self.max_depth})")
            # Anti-loop: the same pair must not be waiting on each other.
            for req in self._items.values():
                if (req.state in OPEN_STATES and req.sender == recipient
                        and req.recipient == sender):
                    raise RequestError("a request is already pending in the opposite direction")
            duration = float(timeout) if timeout else self.default_timeout
            req = Request(request_id=new_request_id(), sender=sender, recipient=recipient,
                          task=task, created_at=now, deadline=now + duration,
                          depth=depth, history=history)
            self._items[req.request_id] = req
            self._order.append(req.request_id)
            self._trim()
            return req

    def accept(self, request_id: str) -> None:
        with self._lock:
            req = self._items.get(request_id)
            if req is not None and req.state == PENDING:
                req.state = ACCEPTED
                req.accepted_at = time.time()

    def reply(self, request_id: str, by: str, body: str) -> Request:
        body = (body or "").strip()
        if not body:
            raise RequestError("the reply is empty")
        if len(body) > self.max_body:
            raise RequestError(f"reply body exceeds {self.max_body} characters")
        with self._lock:
            req = self._items.get(request_id)
            if req is None:
                raise RequestError(f"unknown request {request_id!r}")
            if req.state not in OPEN_STATES:
                raise RequestError(f"request already {req.state}")
            if by != req.recipient:
                raise RequestError("only the original recipient can reply to this request")
            req.state = REPLIED
            req.reply = body
            req.replied_at = time.time()
            req.replied_by = by
            return req

    def cancel(self, request_id: str, by: str) -> Request:
        with self._lock:
            req = self._items.get(request_id)
            if req is None:
                raise RequestError(f"unknown request {request_id!r}")
            if req.state not in OPEN_STATES:
                raise RequestError(f"request already {req.state}")
            if by not in (req.sender, req.recipient):
                raise RequestError("only the sender or recipient can cancel this request")
            req.state = CANCELLED
            req.replied_at = time.time()
            return req

    def cancel_operator(self, request_id: str) -> Request:
        """Cancel from the control plane (authenticated Web UI / local CLI)."""
        with self._lock:
            req = self._items.get(request_id)
            if req is None:
                raise RequestError(f"unknown request {request_id!r}")
            if req.state not in OPEN_STATES:
                raise RequestError(f"request already {req.state}")
            req.state = CANCELLED
            req.replied_at = time.time()
            req.replied_by = "operator"
            return req

    def fail(self, request_id: str, error: str = "failed") -> None:
        with self._lock:
            req = self._items.get(request_id)
            if req is not None and req.state in OPEN_STATES:
                req.state = FAILED
                req.reply = (req.reply or "") + f"[{error}]"

    def sweep(self) -> list[str]:
        """Mark open requests past their deadline as expired; returns their ids."""
        now = time.time()
        expired: list[str] = []
        with self._lock:
            for req in self._items.values():
                if req.state in OPEN_STATES and req.deadline <= now:
                    req.state = EXPIRED
                    expired.append(req.request_id)
        return expired

    # -- queries -----------------------------------------------------------
    def get(self, request_id: str) -> Request | None:
        self.sweep()
        with self._lock:
            return self._items.get(request_id)

    def list(self, *, open_only: bool = False, agent: str | None = None,
             team_ids: set[str] | None = None) -> list[dict[str, Any]]:
        self.sweep()
        with self._lock:
            items = list(self._items.values())
        if open_only:
            items = [r for r in items if r.state in OPEN_STATES]
        if agent:
            items = [r for r in items if agent in (r.sender, r.recipient)]
        if team_ids is not None:
            items = [r for r in items if r.sender in team_ids and r.recipient in team_ids]
        items.sort(key=lambda r: r.created_at)
        return [r.to_dict() for r in items]

    # -- persistence (bounded) --------------------------------------------
    def snapshot(self) -> list[dict[str, Any]]:
        self.sweep()
        with self._lock:
            return [r.to_dict() for r in self._items.values()]

    def load(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._items.clear()
            self._order.clear()
            for row in rows or []:
                try:
                    req = Request(
                        request_id=str(row["request_id"]), sender=str(row["sender"]),
                        recipient=str(row["recipient"]), task=str(row["task"]),
                        created_at=float(row["created_at"]), deadline=float(row["deadline"]),
                        state=str(row.get("state", PENDING)), reply=row.get("reply"),
                        replied_at=row.get("replied_at"), replied_by=row.get("replied_by"),
                        accepted_at=row.get("accepted_at"), depth=int(row.get("depth", 1)),
                        history=list(row.get("history") or []),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                self._items[req.request_id] = req
                self._order.append(req.request_id)
        self.sweep()

    def _trim(self) -> None:
        # Keep the newest ``retention`` requests; drop closed ones first.
        while len(self._order) > self._retention:
            oldest = self._order[0]
            req = self._items.get(oldest)
            if req is not None and req.state in OPEN_STATES:
                break
            self._order.pop(0)
            self._items.pop(oldest, None)
