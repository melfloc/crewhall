"""User-facing notices: things crewhall handled on its own.

The first case is a provider CLI moving outside the range its adapter was
verified against: crewhall switches to a compatible binary automatically and
records a notice explaining what happened, which the Web UI shows in a modal
(and ``crewhall doctor`` lists). The store is a small JSON file (0600) in the
state directory, deduplicated by a stable ``key`` so each change is shown once
and stays dismissed until the user reads it.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import threading
import time
from typing import Any

from . import paths

FILENAME = "notices.json"
MAX = 50
LEVELS = ("info", "warn", "err")
_LOCK = threading.RLock()


def path() -> str:
    return os.path.join(paths.state_dir(), FILENAME)


def _load() -> list[dict[str, Any]]:
    try:
        with open(path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [n for n in data if isinstance(n, dict)] if isinstance(data, list) else []


def _write(items: list[dict[str, Any]]) -> None:
    directory = os.path.dirname(path())
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(items[-MAX:], fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path())
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def add(*, key: str, title: str, body: str, level: str = "warn",
        kind: str | None = None) -> dict[str, Any] | None:
    """Record a notice once per ``key``; return it (None if already known)."""
    with _LOCK:
        items = _load()
        if any(n.get("key") == key for n in items):
            return None
        notice = {
            "id": "note_" + secrets.token_hex(6),
            "key": key,
            "title": title,
            "body": body,
            "level": level if level in LEVELS else "warn",
            "kind": kind,
            "created_at": time.time(),
            "acked": False,
        }
        items.append(notice)
        try:
            _write(items)
        except OSError:
            return None
        return notice


def list_all() -> list[dict[str, Any]]:
    """Every notice, newest first."""
    with _LOCK:
        return list(reversed(_load()))


def pending() -> list[dict[str, Any]]:
    return [n for n in list_all() if not n.get("acked")]


def ack(notice_id: str | None = None) -> list[dict[str, Any]]:
    """Mark one notice (or every pending one) as read; return the remaining list."""
    with _LOCK:
        items = _load()
        changed = False
        for notice in items:
            if notice.get("acked"):
                continue
            if notice_id is None or notice.get("id") == notice_id:
                notice["acked"] = True
                changed = True
        if changed:
            with contextlib.suppress(OSError):
                _write(items)
        return list(reversed(items))


def clear() -> None:
    """Drop every notice (used by tests and reset)."""
    with _LOCK, contextlib.suppress(OSError):
        os.unlink(path())
