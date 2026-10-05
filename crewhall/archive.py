"""Archive of closed agents: keep a conversation + summary instead of deleting it.

When an agent is removed, its definition and a bounded summary are written to
``<state>/archive/<agent_id>.json``. This is deliberately separate from
``state.json`` (no schema change) and is read-only: an archived agent is never
restored automatically, it is just there to look back at what it did.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from . import paths

_MAX_MESSAGES = 50
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


def archive_dir() -> str:
    return os.path.join(paths.state_dir(), "archive")


def _path_for(agent_id: str) -> str:
    if not _SAFE_ID.match(agent_id or ""):
        raise ValueError("invalid agent id")
    return os.path.join(archive_dir(), agent_id + ".json")


def archive_agent(
    *,
    agent_id: str,
    name: str | None,
    kind: str,
    cwd: str | None,
    teams: list[str],
    conversation_id: str | None,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write an archive record for a just-closed agent."""
    record = {
        "agent_id": agent_id,
        "name": name,
        "kind": kind,
        "cwd": cwd,
        "teams": list(teams),
        "conversation_id": conversation_id,
        "archived_at": time.time(),
        "summary": _summarize(messages),
        "messages": messages[-_MAX_MESSAGES:],
    }
    d = archive_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = _path_for(agent_id)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return record


def _summarize(messages: list[dict[str, Any]]) -> dict[str, Any]:
    sent = sum(1 for m in messages if m.get("sender") and m.get("sender") == m.get("agent_id"))
    return {
        "messages": len(messages),
        "last": messages[-1]["body"][:200] if messages else None,
        "_sent": sent,
    }


def list_archived() -> list[dict[str, Any]]:
    d = archive_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "agent_id": rec.get("agent_id"),
            "name": rec.get("name"),
            "kind": rec.get("kind"),
            "cwd": rec.get("cwd"),
            "teams": rec.get("teams", []),
            "archived_at": rec.get("archived_at"),
            "summary": rec.get("summary", {}),
        })
    return sorted(out, key=lambda r: r.get("archived_at") or 0, reverse=True)


def read_archived(agent_id: str) -> dict[str, Any] | None:
    try:
        path = _path_for(agent_id)
    except ValueError:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def forget(agent_id: str) -> bool:
    try:
        os.unlink(_path_for(agent_id))
        return True
    except (OSError, ValueError):
        return False
