"""Soak-test invariants (layer 4).

``scripts/soak.py`` runs for a couple of hours: hundreds of messages with random
human drafts, several daemon restarts and one agent killed mid-turn. At the end
it feeds what it observed to :func:`check_invariants`, which returns the list of
violations (empty means the soak held). The checks are pure so the suite can
exercise them with synthetic data.

Invariants:

* no message stays in the deferred queue longer than the TTL;
* no message is delivered twice;
* messages to one recipient arrive in the order they were sent;
* a human draft is never destroyed;
* memory, threads and the log stay within their bounds.
"""
from __future__ import annotations

from typing import Any


def _queue_violations(queue: list[dict[str, Any]], now: float,
                      queue_ttl: float) -> list[str]:
    out = []
    for item in queue:
        try:
            enqueued = float(item.get("enqueued_at") or 0.0)
        except (TypeError, ValueError):
            continue
        age = now - enqueued
        if age > queue_ttl:
            out.append(
                f"message to {item.get('recipient')} stuck in the queue for {age:.0f}s "
                f"(> {queue_ttl:.0f}s)")
    return out


def _duplicate_violations(delivered: list[dict[str, Any]]) -> list[str]:
    seen: set[tuple[Any, Any]] = set()
    out = []
    for item in delivered:
        key = (item.get("recipient"), item.get("body"))
        if key in seen:
            out.append(f"message delivered twice to {key[0]}: {str(key[1])[:40]!r}")
        seen.add(key)
    return out


def _order_violations(sent: list[dict[str, Any]],
                      delivered: list[dict[str, Any]]) -> list[str]:
    order: dict[Any, dict[Any, int]] = {}
    for index, item in enumerate(sent):
        order.setdefault(item.get("recipient"), {})[item.get("body")] = index
    last: dict[Any, int] = {}
    out = []
    for item in delivered:
        recipient = item.get("recipient")
        position = order.get(recipient, {}).get(item.get("body"))
        if position is None:
            continue
        if recipient in last and position < last[recipient]:
            out.append(f"out-of-order delivery to {recipient}: {str(item.get('body'))[:40]!r}")
        last[recipient] = max(position, last.get(recipient, position))
    return out


def _draft_violations(drafts: list[dict[str, Any]]) -> list[str]:
    out = []
    for draft in drafts:
        if draft.get("expected") != draft.get("actual"):
            out.append(
                f"draft in {draft.get('agent')} was altered: "
                f"{draft.get('expected')!r} -> {draft.get('actual')!r}")
    return out


def _limit_violations(limits: dict[str, Any]) -> list[str]:
    out = []
    for name, value_key, max_key in (
        ("memory", "memory_kb", "memory_max_kb"),
        ("threads", "threads", "threads_max"),
        ("log", "log_bytes", "log_max_bytes"),
    ):
        value = limits.get(value_key)
        maximum = limits.get(max_key)
        if value is None or maximum is None:
            continue
        if value > maximum:
            out.append(f"{name} is not bounded: {value} > {maximum}")
    return out


def check_invariants(
    *,
    sent: list[dict[str, Any]] | None = None,
    delivered: list[dict[str, Any]] | None = None,
    queue: list[dict[str, Any]] | None = None,
    drafts: list[dict[str, Any]] | None = None,
    limits: dict[str, Any] | None = None,
    now: float | None = None,
    queue_ttl: float = 3600.0,
) -> list[str]:
    """Return the invariant violations observed by a soak (empty = healthy)."""
    import time

    now = time.time() if now is None else now
    violations: list[str] = []
    violations += _queue_violations(list(queue or []), now, queue_ttl)
    violations += _duplicate_violations(list(delivered or []))
    violations += _order_violations(list(sent or []), list(delivered or []))
    violations += _draft_violations(list(drafts or []))
    violations += _limit_violations(dict(limits or {}))
    return violations
