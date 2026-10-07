"""TOTP (RFC 6238) codes for the Web UI, standard library only.

Generates 6-digit codes that change every 30 s and are enrolled in any
authenticator app (Authy, Google Authenticator, Samsung Pass, 1Password…) by
scanning an ``otpauth://`` QR or typing the base32 secret. Codes work over plain
HTTP too, so they are usable even when the Web UI is not a secure context.

The shared secret lives in ``web-totp.json`` (0600). A used time step is
remembered per enrollment, so a code cannot be replayed inside its window.
"""
from __future__ import annotations

import base64
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
from urllib.parse import quote

from .. import brand

DIGITS = 6
PERIOD = 30
SKEW = 1  # accept the previous/next step (clock drift)
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
    return os.path.join(_config_dir(), "web-totp.json")


def _load() -> list[dict[str, Any]]:
    try:
        with open(store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    records = data.get("enrollments") if isinstance(data, dict) else None
    return records if isinstance(records, list) else []


def _save(records: list[dict[str, Any]]) -> None:
    path = store_path()
    tmp = path + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"enrollments": records}, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# -- codes -------------------------------------------------------------------
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded)
    digest = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = ((digest[offset] & 0x7F) << 24 | digest[offset + 1] << 16
             | digest[offset + 2] << 8 | digest[offset + 3])
    return f"{value % (10 ** DIGITS):0{DIGITS}d}"


def code_at(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at if at is not None else time.time()) // PERIOD))


def verify(secret: str, code: Any, last_counter: int = 0) -> int | None:
    """Return the matched time step (and reject replays), or None."""
    text = str(code or "").strip().replace(" ", "")
    if not text.isdigit() or len(text) != DIGITS:
        return None
    current = int(time.time() // PERIOD)
    for step in range(current - SKEW, current + SKEW + 1):
        if hmac.compare_digest(_hotp(secret, step), text):
            if step <= int(last_counter or 0):
                return None  # already used inside the window
            return step
    return None


def otpauth_uri(secret: str, label: str, issuer: str = "crewhall") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(label)}?secret={secret}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}")


# -- store -------------------------------------------------------------------
def add_enrollment(secret: str, label: str) -> dict[str, Any]:
    record = {
        "id": "totp_" + uuid.uuid4().hex[:8],
        "label": (label or "")[:80],
        "secret": secret,
        "created_at": time.time(),
        "last_used": None,
        "last_counter": 0,
    }
    with _LOCK:
        records = _load()
        records.append(record)
        _save(records)
    return dict(record)


def list_enrollments() -> list[dict[str, Any]]:
    return [{"id": r.get("id"), "label": r.get("label"),
             "created_at": r.get("created_at"), "last_used": r.get("last_used")}
            for r in _load()]


def has_enrollments() -> bool:
    return bool(_load())


def verify_any(code: Any) -> str | None:
    """Verify a code against every enrollment; return the enrollment id or None."""
    with _LOCK:
        records = _load()
        for rec in records:
            step = verify(str(rec.get("secret", "")), code, int(rec.get("last_counter") or 0))
            if step is not None:
                rec["last_counter"] = step
                rec["last_used"] = time.time()
                _save(records)
                return str(rec.get("id"))
    return None


def revoke(enrollment_id: str) -> bool:
    with _LOCK:
        records = _load()
        kept = [r for r in records if r.get("id") != enrollment_id]
        if len(kept) == len(records):
            return False
        _save(kept)
        return True


def file_permissions_ok() -> bool:
    path = store_path()
    if not os.path.isfile(path):
        return True
    return stat.S_IMODE(os.stat(path).st_mode) == 0o600
