"""Append-only audit log of privileged operations.

``state/audit.jsonl`` (mode 0600) receives one JSON object per line:
``{ts, actor, op, result, summary}``. It is rotated by size (keeping a few
generations) so it cannot grow without bound, and it never stores secret
values: sensitive settings are recorded by name and a short, non-reversible
hash of the value only.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any

from . import paths

FILENAME = "audit.jsonl"
MAX_BYTES = 5 * 1024 * 1024
KEEP = 3
MAX_LINE = 4000

_LOCK = threading.Lock()

# Operations worth an audit line (never the high-frequency read-only polls).
AUDITED = {
    "agent_create", "agent_stop", "agent_send", "message_send", "team_send",
    "team_create", "team_up", "team_set_workspace", "team_add_member",
    "team_remove_member", "team_remove",
    "request_create", "request_reply", "request_cancel", "worktree_discard",
    "interaction_respond", "agent_process_signal", "agent_new_session", "team_new_session",
    "settings_set", "settings_reset", "provider_check", "frontend_set",
    "reset_plan", "reset_apply", "clean_plan", "clean_apply",
    "bundle_export", "bundle_import", "bundle_delete", "fs_complete",
    "web_token_rotate", "web_token_revoke", "web_session_revoke", "web_login",
}


def audit_path() -> str:
    return os.path.join(paths.state_dir(), FILENAME)


def _short_hash(value: Any) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return "sha256:" + digest[:12]


def _rotate_locked() -> None:
    path = audit_path()
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return
    for index in range(KEEP - 1, 0, -1):
        src, dst = f"{path}.{index}", f"{path}.{index + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(path, f"{path}.1")
    except OSError:
        pass


def record(op: str, *, actor: str = "local", result: str = "ok", summary: str = "",
           extra: dict[str, Any] | None = None) -> None:
    """Append one event. Never raises: auditing must not break an operation."""
    entry: dict[str, Any] = {
        "ts": round(time.time(), 3),
        "actor": str(actor)[:120] or "local",
        "op": str(op)[:80],
        "result": str(result)[:40],
        "summary": str(summary)[:MAX_LINE],
    }
    if extra:
        for key, value in extra.items():
            entry[str(key)[:40]] = value
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    try:
        with _LOCK:
            directory = os.path.dirname(audit_path())
            os.makedirs(directory, mode=0o700, exist_ok=True)
            _rotate_locked()
            fd = os.open(audit_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)
    except OSError:
        pass


def _iter_entries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in [f"{audit_path()}.{i}" for i in range(KEEP, 0, -1)] + [audit_path()]:
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        out.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return out


def recent(limit: int = 200, *, op: str | None = None, result: str | None = None) -> list[dict[str, Any]]:
    """Newest-first list of events, optionally filtered by op/result."""
    entries = _iter_entries()
    if op:
        entries = [e for e in entries if e.get("op") == op]
    if result:
        entries = [e for e in entries if e.get("result") == result]
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    return list(reversed(entries))[:limit]


def file_permissions_ok() -> bool:
    import stat

    path = audit_path()
    if not os.path.isfile(path):
        return True  # not created yet
    return stat.S_IMODE(os.stat(path).st_mode) == 0o600


# -- summaries (never raw secrets) -------------------------------------------
def summarize(op: str, request: dict[str, Any], result: dict[str, Any] | None = None) -> str:
    """A short, secret-free description of what an operation did."""
    result = result or {}
    if op in ("settings_set", "settings_reset"):
        changes = request.get("changes") or {}
        parts = []
        for key in sorted(str(k) for k in changes):
            if key.endswith(".env") or key.endswith(".command"):
                # Never the raw value: a short, non-reversible hash for correlation.
                parts.append(f"{key}={_short_hash(changes[key])}")
            else:
                parts.append(key)
        confirmed = " confirmed" if request.get("confirm") == "CONFIRM" else ""
        return f"keys={','.join(parts[:8])}{confirmed}"
    if op == "provider_check":
        return f"kind={request.get('kind') or 'all'}"
    if op == "agent_create":
        return f"kind={request.get('kind')} name={request.get('name')} team={request.get('team')}"
    if op in ("agent_stop", "agent_info", "agent_state"):
        return f"target={request.get('target')}"
    if op in ("message_send", "team_send"):
        return f"sender={request.get('sender')} recipient={request.get('recipient')}"
    if op.startswith("team_") and op not in ("team_send",):
        return f"team={request.get('target') or request.get('name')} member={request.get('agent')}"
    if op.startswith("request_"):
        return f"request={request.get('request_id') or request.get('recipient')}"
    if op == "worktree_discard":
        return f"path={request.get('path')}"
    if op == "interaction_respond":
        return f"id={request.get('id')} by={request.get('by')}"
    if op == "agent_process_signal":
        return f"target={request.get('target')} pid={request.get('pid')} signal={request.get('signal')}"
    if op == "frontend_set":
        return f"mode={request.get('mode')} port={request.get('port')}"
    if op.startswith("bundle_"):
        return f"name={request.get('name')}"
    if op.startswith("reset_"):
        return f"level={request.get('level')}"
    if op == "fs_complete":
        return "fs_complete"
    if op in ("clean_plan", "clean_apply"):
        return "clean"
    if op.startswith("web_token") or op.startswith("web_session"):
        return op
    return ""
