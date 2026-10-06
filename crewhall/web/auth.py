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
from typing import Any

from .. import brand

# Access token + optional session cookies for the Web UI. Secrets live in a
# dedicated credentials file (0600), never in StateStore, logs, AgentInfo or
# the repo. Tailscale provides transport; this module provides the app's own
# authentication boundary.

TOKEN_BYTES = 32
SESSION_TTL = 12 * 3600  # 12h
COOKIE_NAME = "at_session"


def _config_dir() -> str:
    path = brand.config_dir()
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def token_path() -> str:
    return os.path.join(_config_dir(), "web-token")


def _write_private(path: str, data: str) -> None:
    directory = os.path.dirname(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _ = directory


def generate_token(*, rotate: bool = False) -> str:
    """Create (or rotate) the access token. Returns the token once."""
    path = token_path()
    if os.path.exists(path) and not rotate:
        raise FileExistsError(
            "token already exists; use rotate to replace it"
        )
    token = secrets.token_urlsafe(TOKEN_BYTES)
    _write_private(path, token + "\n")
    return token


def token_exists() -> bool:
    return os.path.isfile(token_path())


def load_token() -> str | None:
    try:
        with open(token_path(), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def revoke_token() -> bool:
    try:
        os.unlink(token_path())
        return True
    except FileNotFoundError:
        return False


def token_fingerprint() -> str | None:
    """Short, non-reversible id for status display (never the token)."""
    token = load_token()
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return "sha256:" + digest[:12]


def verify_token(candidate: str | None) -> bool:
    expected = load_token()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------- login limits
# Per-IP failed-login limiter: bounded memory, exponential backoff and a
# temporary lockout. It is process-local (resets on restart) on purpose: it
# raises the cost of online guessing without ever locking a user out forever.
LOGIN_MAX_ATTEMPTS = 5          # first lockout after this many failures
LOGIN_WINDOW = 900.0            # failures older than this are forgotten
LOGIN_MAX_LOCKOUT = 300.0       # cap on the backoff (seconds)
_LOGIN: dict[str, dict[str, float]] = {}
_LOGIN_LOCK = threading.Lock()


def _login_key(ip: str | None) -> str:
    return (ip or "?").strip()[:64] or "?"


def login_retry_after(ip: str | None) -> float:
    """Seconds the client must wait, or 0 when it may try now."""
    now = time.time()
    with _LOGIN_LOCK:
        rec = _LOGIN.get(_login_key(ip))
        if not rec:
            return 0.0
        if now - rec["last"] > LOGIN_WINDOW:
            _LOGIN.pop(_login_key(ip), None)
            return 0.0
        locked_until = rec.get("locked_until", 0.0)
        return max(0.0, locked_until - now)


def note_login_failure(ip: str | None) -> float:
    """Record a failed attempt and return the lockout in seconds (0 = not yet)."""
    now = time.time()
    with _LOGIN_LOCK:
        key = _login_key(ip)
        rec = _LOGIN.get(key)
        if not rec or now - rec["last"] > LOGIN_WINDOW:
            rec = {"fails": 0.0, "last": now, "locked_until": 0.0}
        rec["fails"] += 1
        rec["last"] = now
        lockout = 0.0
        if rec["fails"] >= LOGIN_MAX_ATTEMPTS:
            lockout = min(LOGIN_MAX_LOCKOUT, 2.0 ** (rec["fails"] - LOGIN_MAX_ATTEMPTS))
            rec["locked_until"] = now + lockout
        _LOGIN[key] = rec
        return lockout


def note_login_success(ip: str | None) -> None:
    with _LOGIN_LOCK:
        _LOGIN.pop(_login_key(ip), None)


def reset_login_limits() -> None:
    """Testing hook: forget every per-IP failure counter."""
    with _LOGIN_LOCK:
        _LOGIN.clear()


# ------------------------------------------------------------------ sessions
def _session_secret() -> bytes:
    """Derive a signing key from the token (never stored separately)."""
    token = load_token()
    if not token:
        return b""
    return hashlib.sha256(("session:" + token).encode("utf-8")).digest()


# Connected devices: issued sessions with a non-sensitive label (ip + a short
# user-agent). This is process-local memory, never persisted; revoking clears it.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_LOCK = threading.Lock()


def _label_key(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()[:16]


def issue_session(ttl: int | None = None, *, ip: str = "", user_agent: str = "") -> str:
    if ttl is None:
        try:
            from .. import settings

            ttl = int(settings.get("security.session_ttl_hours")) * 3600
        except Exception:  # noqa: BLE001 - never block a login over a settings problem
            ttl = SESSION_TTL
    # A per-login nonce keeps two sessions issued in the same second distinct.
    payload = {"sub": "web", "exp": int(time.time()) + ttl, "jti": secrets.token_hex(8)}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=")
    sig = hmac.new(_session_secret(), body, hashlib.sha256).digest()
    value = body.decode("ascii") + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    with _SESSIONS_LOCK:
        _SESSIONS[_label_key(value)] = {
            "value": value, "ip": ip or "", "user_agent": (user_agent or "")[:160],
            "issued_at": time.time(), "expires_at": time.time() + ttl, "last_seen": time.time(),
        }
    return value


def touch_session(value: str | None) -> None:
    if not value:
        return
    with _SESSIONS_LOCK:
        rec = _SESSIONS.get(_label_key(value))
        if rec:
            rec["last_seen"] = time.time()


def list_sessions() -> list[dict[str, Any]]:
    """Non-sensitive view of issued sessions (never the token/cookie value)."""
    now = time.time()
    with _SESSIONS_LOCK:
        for k in [k for k, r in _SESSIONS.items() if r["expires_at"] <= now]:
            _SESSIONS.pop(k, None)
        return [
            {"ip": r["ip"], "user_agent": r["user_agent"], "issued_at": r["issued_at"],
             "expires_at": r["expires_at"], "last_seen": r["last_seen"]}
            for r in _SESSIONS.values()
        ]


def revoke_sessions(keep: str | None = None) -> int:
    """Drop every issued session (all devices must sign in again), optionally except ``keep``."""
    with _SESSIONS_LOCK:
        kept = _SESSIONS.get(_label_key(keep)) if keep else None
        n = len(_SESSIONS) - (1 if kept else 0)
        _SESSIONS.clear()
        if kept:
            _SESSIONS[_label_key(keep)] = kept
    with _TERMINAL_LOCK:
        if keep:
            rec = _TERMINAL_GRANTS.get(_label_key(keep))
            _TERMINAL_GRANTS.clear()
            if rec:
                _TERMINAL_GRANTS[_label_key(keep)] = rec
        else:
            _TERMINAL_GRANTS.clear()
    return n


def revoke_session(value: str | None) -> bool:
    """Drop one issued session (used by logout)."""
    if not value:
        return False
    with _TERMINAL_LOCK:
        _TERMINAL_GRANTS.pop(_label_key(value), None)
    with _SESSIONS_LOCK:
        return _SESSIONS.pop(_label_key(value), None) is not None


def session_value(headers: Any) -> str | None:
    """Return the verified session cookie value, if any."""
    cookie = headers.get("Cookie", "")
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and verify_session(value):
            return value
    return None


# Terminal scopes attached to a web session by "unlocking" with a terminal
# token. Process-local, never persisted; cleared when sessions are revoked.
_TERMINAL_GRANTS: dict[str, dict[str, Any]] = {}
_TERMINAL_LOCK = threading.Lock()


def unlock_terminal(value: str | None, *, scopes: list[str], hosts: list[str],
                    token_id: str) -> bool:
    if not value:
        return False
    with _TERMINAL_LOCK:
        _TERMINAL_GRANTS[_label_key(value)] = {
            "scopes": list(scopes), "hosts": list(hosts),
            "token_id": token_id, "unlocked_at": time.time(),
        }
    return True


def terminal_grant(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    with _TERMINAL_LOCK:
        rec = _TERMINAL_GRANTS.get(_label_key(value))
        return dict(rec) if rec else None


def verify_session(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    body_b64, sig_b64 = value.split(".", 1)
    try:
        body = body_b64.encode("ascii")
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except (ValueError, base64.binascii.Error):
        return False
    expected = hmac.new(_session_secret(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
        )
    except (ValueError, json.JSONDecodeError):
        return False
    return int(payload.get("exp", 0)) > time.time()


def authenticate(headers: Any) -> bool:
    """Validate an incoming request: Bearer token OR signed session cookie."""
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        if verify_token(auth[7:].strip()):
            return True
    cookie = headers.get("Cookie", "")
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and verify_session(value):
            touch_session(value)
            return True
    return False


def authorize(identity: str = "web") -> bool:
    """Authorization boundary. Authentication implies full access today;
    this hook exists so per-user/per-team rules can be added later."""
    return identity == "web"


def cookie_header(value: str, *, secure: bool = False) -> str:
    parts = [f"{COOKIE_NAME}={value}", "HttpOnly", "SameSite=Strict", "Path=/"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def file_permissions_ok() -> bool:
    path = token_path()
    if not os.path.isfile(path):
        return False
    mode = stat.S_IMODE(os.stat(path).st_mode)
    return mode == 0o600
