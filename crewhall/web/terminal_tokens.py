"""Scoped tokens for web terminals (Phase 3).

A terminal token is a *separate* credential from the master Web UI token. It is
never stored in clear: only ``sha256(token)`` lives in
``web-terminal-tokens.json`` (0600, atomic writes). A token grants the scopes
``terminal:read`` / ``terminal:write`` (write implies read) and is restricted to
a set of hosts (``["local","prod1"]`` or ``["*"]``).

A normal web session starts with NO terminal scopes; it must *unlock* with a
terminal token (``POST /api/terminal-unlock``), which attaches that token's
scopes/hosts to the session.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
import uuid
from typing import Any

from .. import brand

TOKEN_BYTES = 32
DEFAULT_TTL = 8 * 3600
SCOPE_READ = "terminal:read"
SCOPE_WRITE = "terminal:write"
VALID_SCOPES = {SCOPE_READ, SCOPE_WRITE}
_LOCK = threading.RLock()


def _config_dir() -> str:
    path = brand.config_dir()
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def store_path() -> str:
    return os.path.join(_config_dir(), "web-terminal-tokens.json")


def _load() -> list[dict[str, Any]]:
    try:
        with open(store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    records = data.get("tokens") if isinstance(data, dict) else None
    return records if isinstance(records, list) else []


def _save(records: list[dict[str, Any]]) -> None:
    path = store_path()
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"tokens": records}, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_hosts(hosts: Any) -> list[str]:
    if hosts in (None, "", []):
        return ["*"]
    if isinstance(hosts, str):
        hosts = [h.strip() for h in hosts.split(",") if h.strip()]
    out = []
    for h in hosts:
        if h in ("local", "this", "this machine"):
            h = "local"
        if h == "*" or h:
            out.append(str(h))
    return out or ["*"]


def issue(label: str, *, scope: str = "read", hosts: Any = None,
          ttl: int | None = DEFAULT_TTL) -> tuple[str, dict[str, Any]]:
    """Create a token; returns (plaintext_token, record). Shown once."""
    if scope not in ("read", "write"):
        raise ValueError("scope must be read or write")
    scopes = [SCOPE_READ] if scope == "read" else [SCOPE_READ, SCOPE_WRITE]
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = time.time()
    record = {
        "id": "tt_" + uuid.uuid4().hex[:8],
        "label": (label or "")[:80],
        "hash": _hash(token),
        "scopes": scopes,
        "hosts": _normalize_hosts(hosts),
        "created_at": now,
        "expires_at": (now + int(ttl)) if ttl else None,
        "last_used": None,
    }
    purge_expired()
    with _LOCK:
        records = _load()
        records.append(record)
        _save(records)
    return token, dict(record)


def verify(token: str | None) -> dict[str, Any] | None:
    """Return the record for a valid, unexpired token (updates last_used)."""
    if not token:
        return None
    digest = _hash(token)
    now = time.time()
    with _LOCK:
        records = _load()
        for rec in records:
            if hmac.compare_digest(str(rec.get("hash", "")), digest):
                exp = rec.get("expires_at")
                if exp and float(exp) <= now:
                    return None
                rec["last_used"] = now
                _save(records)
                return dict(rec)
    return None


def purge_expired() -> int:
    """Drop expired records so they do not accumulate. Returns how many were removed."""
    now = time.time()
    with _LOCK:
        records = _load()
        kept = [r for r in records
                if not (r.get("expires_at") and float(r["expires_at"]) <= now)]
        removed = len(records) - len(kept)
        if removed:
            _save(kept)
        return removed


def list_tokens() -> list[dict[str, Any]]:
    """Metadata only: never the token or its hash."""
    purge_expired()
    now = time.time()
    out = []
    with _LOCK:
        for rec in _load():
            exp = rec.get("expires_at")
            out.append({
                "id": rec.get("id"), "label": rec.get("label"),
                "scopes": list(rec.get("scopes") or []), "hosts": list(rec.get("hosts") or []),
                "created_at": rec.get("created_at"), "expires_at": exp,
                "last_used": rec.get("last_used"), "expired": bool(exp and float(exp) <= now),
            })
    return out


def revoke(token_id: str) -> bool:
    with _LOCK:
        records = _load()
        kept = [r for r in records if r.get("id") != token_id]
        if len(kept) == len(records):
            return False
        _save(kept)
        return True


def file_permissions_ok() -> bool:
    path = store_path()
    if not os.path.isfile(path):
        return True
    return stat.S_IMODE(os.stat(path).st_mode) == 0o600


def scopes_imply(scopes: Any, needed: str) -> bool:
    s = set(scopes or [])
    if needed == SCOPE_READ:
        return SCOPE_READ in s or SCOPE_WRITE in s
    return needed in s


def host_allowed(hosts: Any, host: str | None) -> bool:
    allowed = set(hosts or [])
    if "*" in allowed:
        return True
    return (host or "local") in allowed
