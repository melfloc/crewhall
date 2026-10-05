"""Read-only reader for Codex's rollout JSONL (``~/.codex/sessions/…``).

Codex writes one ``rollout-*.jsonl`` per conversation with the exact turn
signals ``task_started`` / ``task_complete``. This module only ever reads from
inside ``~/.codex/sessions`` (``realpath``-checked), with size/line caps, and
never returns ``session_meta`` (account/installation ids).

Nothing here writes to the user's Codex config or sessions.
"""
from __future__ import annotations

import glob
import json
import os
import re
from typing import Any

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_BYTES = 8 * 1024 * 1024
MAX_LINES = 4000


def sessions_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def _within(path: str, root: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _tail_lines(path: str) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > MAX_BYTES:
                fh.seek(size - MAX_BYTES)
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > MAX_BYTES and lines:
        lines = lines[1:]  # first line may be cut
    return lines[-MAX_LINES:]


def find_rollout(conversation_id: str | None = None, *, cwd: str | None = None,
                 root: str | None = None) -> str | None:
    """Newest rollout inside the sessions tree, optionally matching an id/cwd."""
    root = root or sessions_root()
    if not os.path.isdir(root):
        return None
    patterns = [os.path.join(root, "**", "rollout-*.jsonl")]
    candidates: list[str] = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern, recursive=True))
    candidates = [p for p in candidates if _within(p, root)]
    if conversation_id:
        if not SESSION_ID_RE.match(conversation_id or ""):
            return None
        matches = [p for p in candidates if conversation_id in os.path.basename(p)
                   or conversation_id in p]
        if matches:
            candidates = matches
        else:
            return None
    if cwd:
        real_cwd = os.path.realpath(cwd)
        with_cwd = []
        for path in candidates:
            if _file_mentions_cwd(path, real_cwd):
                with_cwd.append(path)
        if with_cwd:
            candidates = with_cwd
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda p: os.path.getmtime(p))
    except OSError:
        return candidates[0]


def _file_mentions_cwd(path: str, real_cwd: str) -> bool:
    # Without parsing every line, a cheap textual probe over the tail is enough.
    marker = json.dumps(real_cwd)
    return marker in "".join(_tail_lines(path)[:200])


def _messages(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") != "response_item":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = "\n".join(
            str(block.get("text", "")) for block in (payload.get("content") or [])
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text")
        ).strip()
        if text:
            out.append({"role": role, "text": text})
    return out


def read(conversation_id: str | None = None, *, cwd: str | None = None,
         limit: int = 200, root: str | None = None) -> dict[str, Any]:
    path = find_rollout(conversation_id, cwd=cwd, root=root)
    if not path:
        return {"available": False, "messages": [], "total": 0, "start": 0}
    messages = _messages(_tail_lines(path))
    total = len(messages)
    limit = max(1, int(limit))
    window = messages[-limit:]
    return {"available": True, "messages": window, "total": total,
            "start": max(0, total - len(window))}


def latest_signal(conversation_id: str | None = None, *, cwd: str | None = None,
                  root: str | None = None) -> dict[str, Any]:
    """``{model, last_event, at}`` from the newest rollout, or ``{}``."""
    path = find_rollout(conversation_id, cwd=cwd, root=root)
    if not path:
        return {}
    model, last_event, at = None, None, None
    for raw in _tail_lines(path):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        entry_type = entry.get("type")
        payload = entry.get("payload")
        if entry_type == "turn_context" and isinstance(payload, dict):
            model = payload.get("model") or model
        elif entry_type == "event_msg" and isinstance(payload, dict):
            kind = payload.get("type")
            if kind in ("task_started", "task_complete"):
                last_event = kind
                at = payload.get("completed_at") or payload.get("started_at") or entry.get("timestamp")
    return {"model": model, "last_event": last_event, "at": at} if (model or last_event) else {}
