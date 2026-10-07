from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..client import Client, RpcError, ensure_daemon
from . import auth, terminal_tokens, webauthn, ws

# Terminal ops that need a write scope vs a read scope.
TERMINAL_WRITE_OPS = {
    "terminal_create", "terminal_write", "terminal_key", "terminal_resize",
    "terminal_close",
}
TERMINAL_READ_OPS = {"terminal_list", "terminal_info", "terminal_capture"}
MAX_WS_CLIENTS_PER_TERMINAL = 8
MAX_WS_CLIENTS_PER_SESSION = 4
TICKET_TTL = 30.0


def _session_key(value: str | None) -> str:
    import hashlib

    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.realpath(os.path.join(HERE, "static"))
INDEX = os.path.join(STATIC, "index.html")
_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".json": "application/json", ".webmanifest": "application/manifest+json",
}

# The login page keeps its CSS/JS in static files so a strict CSP (no inline
# scripts/styles) holds on every response, including this one.
_LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crewhall — sign in</title>
<link rel="stylesheet" href="/static/login.css"></head><body>
<form id="f"><div>crewhall</div>
<input id="t" type="password" placeholder="access token" autofocus>
<div class="err" id="e"></div><button type="submit">Sign in</button>
<button type="button" id="pk" class="pk init-hidden">Sign in with a passkey</button></form>
<script src="/static/login.js"></script></body></html>"""

# Content-Security-Policy: no inline script, no eval. Inline *styles* are
# allowed because xterm.js injects a stylesheet for its dynamic sizing; that is
# the only relaxation (see SECURITY.md).
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'; worker-src 'self'; manifest-src 'self'")

_SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
)


def _hostname_of(host: str) -> str:
    """Extract the hostname from a Host header value (handles IPv6 brackets)."""
    host = (host or "").strip()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host.strip("[]")
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


class SecurityPolicy:
    """Origin/Host allow-list and auth requirement for the Web UI."""

    def __init__(
        self,
        *,
        require_auth: bool = False,
        allowed_hosts: list[str] | None = None,
        allowed_origins: list[str] | None = None,
        secure_cookie: bool = False,
    ) -> None:
        self.require_auth = require_auth
        self.allowed_hosts = allowed_hosts or []
        self.allowed_origins = allowed_origins or []
        self.secure_cookie = secure_cookie

    def host_allowed(self, host: str) -> bool:
        hostname = _hostname_of(host)
        if hostname in ("127.0.0.1", "localhost", "::1"):
            return True
        if not self.allowed_hosts:
            # Remote binding without an explicit allow-list: still restrict to
            # loopback/localhost semantics; the operator must pass --allow-host
            # (or --tailscale, which auto-allows the tailnet) for other hosts.
            return False
        return hostname in self.allowed_hosts

    def origin_allowed(self, origin: str) -> bool:
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.netloc
        return self.host_allowed(host)

# Largest accepted request body (ops are small JSON documents).
MAX_BODY_BYTES = 1024 * 1024
# A configuration bundle upload (it is verified before being stored).
MAX_BUNDLE_UPLOAD = 8 * 1024 * 1024

# Operations a web client may invoke; all map 1:1 to control-plane ops.
ALLOWED_OPS = {
    "ping", "meta_info", "agent_list", "agent_info", "agent_create",
    "agent_send", "agent_write", "agent_key", "agent_resize", "agent_capture",
    "agent_transcript", "agent_history", "agent_interrupt",
    "agent_state", "agent_wait", "agent_stop",
    "team_list", "team_info", "team_create", "team_set_workspace",
    "team_add_member", "team_remove_member", "team_remove", "team_members", "team_up",
    "message_send", "message_history", "agent_identity",
    "interaction_list", "interaction_respond", "agent_new_session",
    "agent_processes", "agent_process_output", "agent_process_signal",
    "clean_plan", "clean_apply", "update_status",
    "bundle_export", "bundle_list", "bundle_import", "bundle_delete", "fs_complete",
    "settings_get", "settings_set", "settings_reset", "provider_check",
    "frontend_status", "frontend_set", "reset_plan", "reset_apply",
    "agent_archive_list", "agent_archive_get",
    "request_list", "request_cancel",
    "worktree_list", "worktree_discard", "host_list", "host_set", "host_remove", "host_test",
    "terminal_create", "terminal_list", "terminal_info", "terminal_write",
    "terminal_key", "terminal_capture", "terminal_resize", "terminal_close",
}


def _snapshot(client: Client, *, include_terminals: bool = False) -> dict[str, Any]:
    """A consistent view for the WebSocket push (server-side adaptation)."""
    agents = client.call("agent_list", viewer=True)["agents"]
    teams = client.call("team_list")["teams"]
    messages = client.call("message_history", limit=100)["messages"]
    try:
        requests = client.call("request_list", open_only=True)["requests"]
    except RpcError:  # an older daemon without the op
        requests = []
    from .. import __version__

    # The page reloads itself when this changes (an update must not leave a stale UI open).
    try:
        hosts = client.call("host_list")["hosts"]
    except RpcError:
        hosts = []
    terminals: list[dict[str, Any]] = []
    if include_terminals:
        try:
            terminals = client.call("terminal_list")["terminals"]
        except RpcError:
            terminals = []
    return {"agents": agents, "teams": teams, "messages": messages,
            "requests": requests, "hosts": hosts, "terminals": terminals,
            "version": __version__}


class Handler(BaseHTTPRequestHandler):
    server_version = "crewhall-web"
    protocol_version = "HTTP/1.1"

    # injected by the server
    control: Client
    security: "SecurityPolicy"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        pass

    # -- security headers -------------------------------------------------
    def send_response(self, code: int, message: str | None = None) -> None:
        self._sent_headers: set[str] = set()
        super().send_response(code, message)

    def send_header(self, keyword: str, value: str) -> None:
        self._sent_headers.add(keyword.lower())
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        sent = getattr(self, "_sent_headers", set())
        for key, value in _SECURITY_HEADERS:
            if key.lower() not in sent:
                self.send_header(key, value)
        if self.path.startswith("/api/") and "cache-control" not in sent:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj: Any, status: int = 200, extra: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400,
               extra: list[tuple[str, str]] | None = None) -> None:
        self._json({"ok": False, "error": message}, status, extra)

    def _client_label(self) -> str:
        """Non-sensitive client label for logs (no headers/credentials)."""
        try:
            return self.client_address[0]
        except Exception:  # noqa: BLE001
            return "?"

    def _audit_actor(self) -> str:
        """Who did it: the client IP, never the token/cookie."""
        return f"web:{self._client_label()}"

    def _actor(self) -> dict[str, str]:
        return {"channel": "web", "ip": self._client_label(),
                "label": self._audit_actor(),
                "ua": (self.headers.get("User-Agent", "") or "")[:120]}

    # -- security ---------------------------------------------------------
    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # same-origin / non-browser clients (curl)
        return self.security.origin_allowed(origin)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host")
        if not host:
            return True
        return self.security.host_allowed(host)

    def _guard(self) -> bool:
        """Common pre-flight for protected routes. True if allowed."""
        if not self._host_ok():
            self._error("invalid Host header", 400)
            return False
        if not self._origin_ok():
            self._error("origin not allowed", 403)
            return False
        if self.security.require_auth and not auth.authenticate(self.headers):
            self._error("authentication required", 401)
            return False
        return True

    def _read_body(self) -> bytes | None:
        """Read the request body with a hard size cap; None after an error reply."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._error("bad Content-Length", 400)
            return None
        if length < 0:
            self._error("bad Content-Length", 400)
            return None
        if length > MAX_BODY_BYTES:
            # The body is not consumed: drop the connection after replying.
            self.close_connection = True
            self._error("request body too large", 413)
            return None
        return self.rfile.read(length) if length else b"{}"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            # index is public (contains no secrets); the app enforces auth on
            # the API/WebSocket. If auth is required this serves a login page.
            return self._serve_index()
        if path == "/login":
            return self._serve_login_page()
        if path == "/terminal.html":
            return self._serve_terminal_page()
        if path.startswith("/static/") or path == "/sw.js":
            # like the index, static assets hold no secrets; the API enforces auth
            return self._serve_static("sw.js" if path == "/sw.js" else path[len("/static/"):])
        if not self._guard():
            return
        if path == "/api/state":
            try:
                return self._json({"ok": True, **_snapshot(
                    self.control, include_terminals=self._terminals_readable())})
            except RpcError as exc:
                return self._error(str(exc), 502)
        if path == "/ws":
            return self._websocket()
        if path.startswith("/ws/terminal/"):
            return self._ws_terminal(unquote(path[len("/ws/terminal/"):]), parsed)
        if path.startswith("/api/bundle/"):
            return self._bundle_download(unquote(path[len("/api/bundle/"):]))
        if path == "/api/op":
            query = parse_qs(parsed.query)
            op = (query.get("op") or [""])[0]
            return self._dispatch(op, {})
        return self._error("not found", 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/login":
            return self._do_login()
        if parsed.path == "/logout":
            return self._do_logout()
        if parsed.path == "/api/bundle":
            return self._bundle_upload(parse_qs(parsed.query))
        if parsed.path == "/api/terminal-unlock":
            return self._do_terminal_unlock()
        if parsed.path == "/api/terminal-ticket":
            return self._do_terminal_ticket()
        if parsed.path.startswith("/api/webauthn/"):
            return self._do_webauthn(parsed.path[len("/api/webauthn/"):])
        if parsed.path != "/api/op":
            return self._error("not found", 404)
        if not self._guard():
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self._error(f"bad json: {exc}")
        op = payload.pop("op", None)
        if op in ("web_sessions", "web_session_revoke", "web_token_status",
                  "web_token_revoke", "web_token_rotate", "audit_list",
                  "terminal_token_list", "terminal_token_issue", "terminal_token_revoke"):
            return self._web_admin(op, payload)
        if op == "frontend_set" and payload.get("mode") == "off":
            # From a browser this would cut the connection you are using, with no way back from here.
            return self._error("turning the Web UI off from the Web UI would lock you out; "
                               "run `crewhall off` on the server if you really want that", 400)
        return self._dispatch(op, payload)

    def _bundle_download(self, name: str) -> None:
        from .. import bundle

        try:
            path = bundle.resolve_name(name)
            with open(path, "rb") as fh:
                data = fh.read()
        except (bundle.BundleError, OSError) as exc:
            return self._error(str(exc), 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bundle_upload(self, query: dict[str, list[str]]) -> None:
        from .. import bundle

        if not self._guard():
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._error("bad Content-Length", 400)
        if length <= 0:
            return self._error("empty upload", 400)
        if length > MAX_BUNDLE_UPLOAD:
            self.close_connection = True
            return self._error("bundle too large", 413)
        data = self.rfile.read(length)
        try:
            name = bundle.store_upload((query.get("name") or [""])[0], data)
        except bundle.BundleError as exc:
            return self._error(f"not a valid bundle: {exc}", 400)
        return self._json({"ok": True, "name": name})

    def _do_logout(self) -> None:
        # Revoke this session server-side too (clear the cookie and the record).
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == auth.COOKIE_NAME and value:
                auth.revoke_session(value)
        return self._json(
            {"ok": True},
            extra=[("Set-Cookie", auth.cookie_header("", secure=self.security.secure_cookie) + "; Max-Age=0")],
        )

    def _web_admin(self, op: str, payload: dict[str, Any] | None = None) -> None:
        """Session/token administration, handled in the web layer (no daemon)."""
        from .. import audit

        payload = payload or {}
        if op == "web_sessions":
            return self._json({"ok": True, "sessions": auth.list_sessions()})
        if op == "audit_list":
            return self._json({"ok": True, "events": audit.recent(
                limit=payload.get("limit", 200),
                op=payload.get("filter_op") or None,
                result=payload.get("filter_result") or None,
            )})
        if op == "web_session_revoke":
            revoked = auth.revoke_sessions()
            audit.record("web_session_revoke", actor=self._audit_actor(),
                         result="ok", summary=f"revoked={revoked}")
            return self._json({"ok": True, "revoked": revoked})
        if op == "web_token_status":
            return self._json({
                "ok": True, "token_exists": auth.token_exists(),
                "fingerprint": auth.token_fingerprint(),
                "email": None, "file_permissions_ok": auth.file_permissions_ok(),
                "require_auth": self.security.require_auth,
            })
        if op == "web_token_rotate":
            # A new token (shown once); every other device must sign in again, this one stays signed in.
            token = auth.generate_token(rotate=True)
            mine = ""
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == auth.COOKIE_NAME:
                    mine = value
            dropped = auth.revoke_sessions(keep=mine or None)
            audit.record("web_token_rotate", actor=self._audit_actor(), result="ok",
                         summary=f"signed_out={dropped}")
            return self._json({"ok": True, "token": token, "fingerprint": auth.token_fingerprint(),
                               "signed_out_devices": dropped})
        if op == "web_token_revoke":
            # Only meaningful when a token exists; revoking also drops every session.
            removed = auth.revoke_token()
            auth.revoke_sessions()
            audit.record("web_token_revoke", actor=self._audit_actor(), result="ok",
                         summary=f"removed={removed}")
            return self._json({"ok": True, "removed": removed})
        if op == "terminal_token_list":
            return self._json({"ok": True, "tokens": terminal_tokens.list_tokens(),
                               "file_permissions_ok": terminal_tokens.file_permissions_ok()})
        if op == "terminal_token_issue":
            scope = "write" if payload.get("scope") == "write" else "read"
            ttl = payload.get("ttl")
            try:
                ttl = int(ttl) if ttl not in (None, "", 0) else None
            except (TypeError, ValueError):
                ttl = None
            token, rec = terminal_tokens.issue(
                payload.get("label") or "", scope=scope,
                hosts=payload.get("hosts"), ttl=ttl,
            )
            audit.record("web_terminal_token_issue", actor=self._audit_actor(), result="ok",
                         summary=audit.summarize("web_terminal_token_issue",
                                                 {"id": rec["id"], "scope": scope}, {}))
            return self._json({"ok": True, "token": token, "record": {
                "id": rec["id"], "label": rec["label"], "scopes": rec["scopes"],
                "hosts": rec["hosts"], "expires_at": rec["expires_at"]}})
        if op == "terminal_token_revoke":
            token_id = payload.get("id")
            removed = terminal_tokens.revoke(token_id)
            audit.record("web_terminal_token_revoke", actor=self._audit_actor(),
                         result="ok" if removed else "error",
                         summary=audit.summarize("web_terminal_token_revoke",
                                                 {"id": token_id}, {}))
            return self._json({"ok": True, "revoked": removed})

    def _do_login(self) -> None:
        from .. import audit

        if not self._host_ok() or not self._origin_ok():
            return self._error("request not allowed", 403)
        ip = self._client_label()
        retry = auth.login_retry_after(ip)
        if retry > 0:
            audit.record("web_login", actor=self._audit_actor(), result="error",
                         summary="rate limited")
            return self._error("too many attempts; try again later", 429,
                               extra=[("Retry-After", str(int(retry) + 1))])
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error("bad json")
        if not auth.verify_token(payload.get("token")):
            # Never log the attempted token.
            auth.note_login_failure(ip)
            audit.record("web_login", actor=self._audit_actor(), result="error",
                         summary="invalid token")
            return self._error("invalid token", 401)
        auth.note_login_success(ip)
        session = auth.issue_session(
            ip=ip, user_agent=self.headers.get("User-Agent", ""),
        )
        audit.record("web_login", actor=self._audit_actor(), result="ok", summary="token")
        return self._json({"ok": True}, extra=[self._session_cookie(session)])

    def _session_cookie(self, session: str) -> tuple[str, str]:
        """A persistent session cookie (survives closing the browser)."""
        return ("Set-Cookie", auth.cookie_header(
            session, secure=self.security.secure_cookie, max_age=auth.session_ttl()))

    def _serve_terminal_page(self) -> None:
        try:
            with open(os.path.join(STATIC, "terminal.html"), "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error("terminal page missing", 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # xterm.js needs inline styles for its dynamic sizing; this page is a
        # separate, same-origin document, so its CSP is relaxed only here.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _session_or_new(self) -> tuple[str | None, list[tuple[str, str]]]:
        """Existing session cookie, or a fresh one when auth is not required."""
        session = auth.session_value(self.headers)
        if session:
            return session, []
        if self.security.require_auth:
            return None, []
        session = auth.issue_session(ip=self._client_label(),
                                     user_agent=self.headers.get("User-Agent", ""))
        return session, [self._session_cookie(session)]

    def _do_terminal_unlock(self) -> None:
        from .. import audit

        if not self._host_ok() or not self._origin_ok():
            return self._error("request not allowed", 403)
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error("bad json")
        ip = "tt:" + self._client_label()
        retry = auth.login_retry_after(ip)
        if retry > 0:
            return self._error("too many attempts; try again later", 429,
                               extra=[("Retry-After", str(int(retry) + 1))])
        token = payload.get("token")
        rec = terminal_tokens.verify(token) if isinstance(token, str) else None
        if not rec:
            auth.note_login_failure(ip)
            return self._error("invalid terminal token", 401)
        auth.note_login_success(ip)
        session, extra = self._session_or_new()
        if not session:
            return self._error("sign in first", 401)
        auth.unlock_terminal(session, scopes=rec["scopes"], hosts=rec["hosts"],
                             token_id=rec["id"])
        return self._json({"ok": True, "scopes": rec["scopes"], "hosts": rec["hosts"]},
                          extra=extra)

    def _do_terminal_ticket(self) -> None:
        from .. import settings

        if not settings.get("terminals.enabled"):
            return self._error("not found", 404)
        if not self._host_ok() or not self._origin_ok():
            return self._error("request not allowed", 403)
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error("bad json")
        terminal_id = payload.get("id")
        mode = "write" if payload.get("mode") == "write" else "read"
        session, extra = self._session_or_new()
        grant = self._effective_grant()
        if not grant:
            return self._error("forbidden", 403)
        needed = (terminal_tokens.SCOPE_WRITE if mode == "write"
                  else terminal_tokens.SCOPE_READ)
        if not terminal_tokens.scopes_imply(grant.get("scopes"), needed):
            return self._error("forbidden", 403)
        try:
            info = self.control.call("terminal_info", id=terminal_id)["terminal"]
        except RpcError:
            return self._error("not found", 404)
        if not terminal_tokens.host_allowed(grant.get("hosts"), info.get("host")):
            return self._error("forbidden", 403)
        if info.get("readonly"):
            mode = "read"
        ticket = secrets.token_urlsafe(32)
        with self.server.tickets_lock:
            now = time.time()
            for key in [k for k, v in self.server.tickets.items() if v.get("expires", 0) < now]:
                self.server.tickets.pop(key, None)
            self.server.tickets[ticket] = {
                "terminal_id": terminal_id, "mode": mode, "session": _session_key(session),
                "origin": self.headers.get("Origin", ""), "expires": now + TICKET_TTL,
                "used": False,
            }
        return self._json({"ok": True, "ticket": ticket, "mode": mode}, extra=extra)

    # -- WebAuthn / passkeys ----------------------------------------------
    def _webauthn_rp(self) -> tuple[str, str]:
        host = self.headers.get("Host", "")
        rp_id = webauthn.rp_id_from_host(host)
        origin = self.headers.get("Origin", "").rstrip("/")
        return rp_id, origin

    def _webauthn_store_challenge(self, kind: str, challenge: bytes, origin: str,
                                  rp_id: str) -> str:
        ceremony = secrets.token_urlsafe(24)
        now = time.time()
        with self.server.webauthn_lock:
            for key in [k for k, v in self.server.webauthn_challenges.items()
                        if v.get("expires", 0) < now]:
                self.server.webauthn_challenges.pop(key, None)
            self.server.webauthn_challenges[ceremony] = {
                "challenge": challenge, "origin": origin, "rp_id": rp_id, "kind": kind,
                "session": _session_key(auth.session_value(self.headers)),
                "expires": now + 300.0, "used": False,
            }
        return ceremony

    def _webauthn_take_challenge(self, ceremony: str, kind: str) -> dict[str, Any]:
        with self.server.webauthn_lock:
            rec = self.server.webauthn_challenges.get(ceremony)
            if not rec or rec.get("used") or rec.get("kind") != kind:
                raise ValueError("unknown or expired ceremony")
            if rec.get("expires", 0) < time.time():
                raise ValueError("expired ceremony")
            if rec.get("session") != _session_key(auth.session_value(self.headers)):
                raise ValueError("ceremony is bound to another session")
            rec["used"] = True
            return dict(rec)

    def _do_webauthn(self, action: str) -> None:
        from .. import audit, settings

        if not self._host_ok() or not self._origin_ok():
            return self._error("request not allowed", 403)
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error("bad json")
        rp_id, origin = self._webauthn_rp()
        try:
            if action == "register/begin":
                return self._webauthn_register_begin(rp_id, origin)
            if action == "register/finish":
                return self._webauthn_register_finish(payload, rp_id, origin)
            if action == "login/begin":
                return self._webauthn_login_begin(rp_id, origin)
            if action == "login/finish":
                return self._webauthn_login_finish(payload, rp_id, origin)
            if action == "credentials":
                return self._json({"ok": True, "credentials": webauthn.list_credentials(),
                                   "file_permissions_ok": webauthn.file_permissions_ok()})
            if action == "revoke":
                if not self._guard():
                    return
                removed = webauthn.revoke(payload.get("id"))
                audit.record("web_passkey_revoke", actor=self._audit_actor(),
                             result="ok" if removed else "error",
                             summary=f"id={payload.get('id')}")
                return self._json({"ok": True, "revoked": removed})
        except ValueError as exc:
            return self._error(str(exc), 400)
        return self._error("not found", 404)

    def _webauthn_register_begin(self, rp_id: str, origin: str) -> None:
        if not self._guard():
            return
        challenge = webauthn.new_challenge()
        ceremony = self._webauthn_store_challenge("register", challenge, origin, rp_id)
        user_id = webauthn.b64u_encode(hashlib.sha256(b"crewhall").digest()[:16])
        self._json({"ok": True, "ceremony": ceremony, "publicKey": {
            "challenge": webauthn.b64u_encode(challenge),
            "rp": {"id": rp_id, "name": "crewhall"},
            "user": {"id": user_id, "name": "crewhall", "displayName": "crewhall"},
            "pubKeyCredParams": [{"type": "public-key", "alg": -7},
                                 {"type": "public-key", "alg": -257}],
            "timeout": 60000, "attestation": "none",
            "authenticatorSelection": {"residentKey": "preferred",
                                       "userVerification": "preferred"},
            "excludeCredentials": [{"type": "public-key", "id": cid}
                                   for cid in webauthn.credential_ids()],
        }})

    def _webauthn_register_finish(self, payload: dict[str, Any], rp_id: str, origin: str) -> None:
        from .. import audit

        if not self._guard():
            return
        rec = self._webauthn_take_challenge(payload.get("ceremony", ""), "register")
        result = webauthn.verify_registration(
            payload.get("clientDataJSON", ""), payload.get("attestationObject", ""),
            rec["challenge"], rec["origin"], rec["rp_id"])
        record = webauthn.add_credential(
            result["credential_id"], result["cose_bytes"], result["alg"],
            result["sign_count"], label=payload.get("label") or "passkey")
        audit.record("web_passkey_add", actor=self._audit_actor(), result="ok",
                     summary=f"id={record['id']}")
        return self._json({"ok": True, "credential": {
            "id": record["id"], "label": record["label"]}})

    def _webauthn_login_begin(self, rp_id: str, origin: str) -> None:
        challenge = webauthn.new_challenge()
        ceremony = self._webauthn_store_challenge("login", challenge, origin, rp_id)
        self._json({"ok": True, "ceremony": ceremony, "publicKey": {
            "challenge": webauthn.b64u_encode(challenge),
            "rpId": rp_id, "timeout": 60000, "userVerification": "preferred",
            "allowCredentials": [],
        }})

    def _webauthn_login_finish(self, payload: dict[str, Any], rp_id: str, origin: str) -> None:
        from .. import audit

        rec = self._webauthn_take_challenge(payload.get("ceremony", ""), "login")
        cred_id = webauthn.b64u_decode(str(payload.get("id", "")))
        stored = webauthn.find(cred_id)
        if not stored:
            return self._error("unknown passkey", 401)
        key = webauthn.public_key(stored)
        new_count = webauthn.verify_assertion(
            payload.get("clientDataJSON", ""), payload.get("authenticatorData", ""),
            payload.get("signature", ""), rec["challenge"], rec["origin"], rec["rp_id"],
            key, int(stored.get("sign_count") or 0))
        webauthn.touch(cred_id, new_count)
        ip = self._client_label()
        session = auth.issue_session(ip=ip, user_agent=self.headers.get("User-Agent", ""))
        audit.record("web_login", actor=self._audit_actor(), result="ok", summary="passkey")
        return self._json({"ok": True}, extra=[self._session_cookie(session)])

    def _serve_login_page(self) -> None:
        body = _LOGIN_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, op: str | None, params: dict[str, Any]) -> None:
        if not op:
            return self._error("missing op")
        if op not in ALLOWED_OPS:
            return self._error(f"operation not allowed: {op}", 403)
        # Never trust underscore-prefixed fields from the client (e.g. _actor).
        params = {k: v for k, v in params.items() if not str(k).startswith("_")}
        grant = None
        if op.startswith("terminal_"):
            from .. import settings

            if not settings.get("terminals.enabled"):
                # Closed by default: the route does not even exist.
                return self._error("not found", 404)
            grant = self._authorize_terminal_op(op, params)
            if grant is None:
                return self._error("forbidden", 403)
            if op == "terminal_create":
                params = {**params, "owner": grant.get("token_id")}
        if op in ("interaction_respond", "agent_process_signal"):
            params = {**params, "by": "web"}  # the audit trail names the channel, not the client's claim
        try:
            result = self.control.call(op, **{**params, "_actor": self._actor()})
        except RpcError as exc:
            return self._error(str(exc), 502)
        result.pop("ok", None)
        if op == "terminal_list" and grant is not None and "*" not in set(grant.get("hosts") or []):
            allowed = set(grant.get("hosts") or [])
            result["terminals"] = [
                t for t in (result.get("terminals") or []) if (t.get("host") or "local") in allowed
            ]
        return self._json({"ok": True, **result})

    def _effective_grant(self) -> dict[str, Any] | None:
        """The session's terminal grant, or a synthetic one for the master session
        when ``terminals.master_grants`` is on."""
        from .. import settings

        session = auth.session_value(self.headers)
        grant = auth.terminal_grant(session)
        if grant:
            return grant
        # Token-free localhost is already trusted; a master session needs a cookie.
        if settings.get("terminals.master_grants") and (session or not self.security.require_auth):
            return {"scopes": [terminal_tokens.SCOPE_READ, terminal_tokens.SCOPE_WRITE],
                    "hosts": ["*"], "token_id": None, "master": True}
        return None

    def _terminals_readable(self) -> bool:
        from .. import settings

        if not settings.get("terminals.enabled"):
            return False
        grant = self._effective_grant()
        return bool(grant) and terminal_tokens.scopes_imply(
            grant.get("scopes"), terminal_tokens.SCOPE_READ)

    def _authorize_terminal_op(self, op: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return the session's terminal grant when the op is allowed, else None."""
        grant = self._effective_grant()
        if not grant:
            return None
        needed = (terminal_tokens.SCOPE_WRITE if op in TERMINAL_WRITE_OPS
                  else terminal_tokens.SCOPE_READ)
        if not terminal_tokens.scopes_imply(grant.get("scopes"), needed):
            return None
        host = params.get("host")
        if op in ("terminal_info", "terminal_write", "terminal_key", "terminal_capture",
                  "terminal_resize", "terminal_close"):
            try:
                info = self.control.call("terminal_info", id=params.get("id"))["terminal"]
            except RpcError:
                return None
            host = info.get("host")
        if not terminal_tokens.host_allowed(grant.get("hosts"), host):
            return None
        return grant

    def _serve_static(self, rel: str) -> None:
        full = os.path.realpath(os.path.join(STATIC, rel))
        ctype = _STATIC_TYPES.get(os.path.splitext(full)[1].lower())
        if not full.startswith(STATIC + os.sep) or ctype is None or not os.path.isfile(full):
            return self._error("not found", 404)
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        try:
            with open(INDEX, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._error("index missing", 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._error("not a websocket request", 400)
        accept = ws.accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        try:
            self._ws_loop()
        except (OSError, ValueError):
            pass

    # -- terminal WebSocket ------------------------------------------------
    def _terminal_context(self, terminal_id: str):
        """(info, host_cfg) for an attachable terminal, or raise RpcError."""
        from .. import settings, terminals as termlib

        termlib.validate_terminal_id(terminal_id)
        info = self.control.call("terminal_info", id=terminal_id)["terminal"]
        host = info.get("host")
        host_cfg = settings.host(host) if host else None
        return info, host_cfg

    def _ws_terminal(self, terminal_id: str, parsed) -> None:
        from .. import settings

        if not settings.get("terminals.enabled"):
            return self._error("not found", 404)
        if not self.headers.get("Sec-WebSocket-Key"):
            return self._error("not a websocket request", 400)
        try:
            info, host_cfg = self._terminal_context(terminal_id)
        except (RpcError, ValueError):
            return self._error("not found", 404)
        meta = info.get("meta") or {}
        pane = meta.get("pane_id") or meta.get("tmux_session") or ""
        tmux_session = meta.get("tmux_session") or ""
        socket_name = meta.get("socket") or "crewhall"
        if not pane or not tmux_session:
            return self._error("terminal is not attachable", 409)

        # Phase 3: Origin (anti-CSWSH), session, scopes and a single-use ticket.
        origin = self.headers.get("Origin", "")
        if origin:
            if not self.security.origin_allowed(origin):
                return self._error("forbidden", 403)
        elif not settings.get("terminals.allow_no_origin"):
            return self._error("forbidden", 403)
        session = auth.session_value(self.headers)
        if self.security.require_auth and not session:
            return self._error("unauthorized", 401)
        grant = self._effective_grant()
        if not grant:
            return self._error("forbidden", 403)
        query = parse_qs(parsed.query)
        ticket = (query.get("ticket") or [""])[0]
        rec = self._consume_ticket(ticket, terminal_id, session, origin)
        if rec is None:
            return self._error("forbidden", 403)
        mode = rec["mode"]
        needed = (terminal_tokens.SCOPE_WRITE if mode == "write"
                  else terminal_tokens.SCOPE_READ)
        if not terminal_tokens.scopes_imply(grant.get("scopes"), needed):
            return self._error("forbidden", 403)
        if not terminal_tokens.host_allowed(grant.get("hosts"), info.get("host")):
            return self._error("forbidden", 403)
        if not self._reserve_terminal_client(terminal_id, session):
            return self._error("too many terminal connections", 429)

        accept = ws.accept_key(self.headers.get("Sec-WebSocket-Key", ""))
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        from .. import audit
        from ..terminal_stream import HubClient, TerminalHub

        queue_bytes = int(settings.get("terminals.client_queue_bytes"))
        max_rate = int(settings.get("terminals.max_bytes_per_sec"))
        readonly = bool(info.get("readonly"))
        with self.server.terminal_hubs_lock:
            hub = self.server.terminal_hubs.get(terminal_id)
            if hub is None:
                hub = TerminalHub(
                    terminal_id, pane=pane, tmux_session=tmux_session, socket=socket_name,
                    host_cfg=host_cfg, queue_bytes=queue_bytes, max_bytes_per_sec=max_rate,
                    cols=int(info.get("cols") or 120), rows=int(info.get("rows") or 32),
                )
                hub.readonly = readonly
                hub.host = info.get("host")
                self.server.terminal_hubs[terminal_id] = hub

        send_lock = threading.Lock()
        counters = {"in": 0, "out": 0}
        started = time.time()
        token_id = grant.get("token_id")

        def send_raw(data: bytes) -> None:
            with send_lock:
                self.connection.sendall(data)

        def send_frame(payload: bytes) -> None:
            send_raw(ws.encode_binary(payload))

        def send_output(data: bytes) -> None:
            counters["out"] += len(data)
            send_frame(bytes([0x02]) + data)

        def control_state(mode: str) -> dict[str, Any]:
            return {
                "mode": mode, "cols": int(info.get("cols") or 120),
                "rows": int(info.get("rows") or 32), "stream": hub.stream_mode,
                "readonly": readonly, "closed": False, "host": info.get("host"),
            }

        def send_control(obj: dict[str, Any]) -> None:
            send_frame(bytes([0x06]) + json.dumps(obj).encode("utf-8"))

        client = HubClient(send_output, queue_bytes=queue_bytes, max_bytes_per_sec=max_rate)
        mode_seen = {"v": mode}

        def control_cb(obj: dict[str, Any]) -> None:
            new_mode = obj.get("mode")
            if new_mode and new_mode != mode_seen["v"]:
                mode_seen["v"] = new_mode
                audit.record("terminal_ws_mode", actor=self._audit_actor(), result="ok",
                             summary=audit.summarize("terminal_ws_mode", {
                                 "terminal_id": terminal_id, "host": info.get("host"),
                                 "mode": new_mode, "token_id": token_id}, {}))
            send_control({**control_state(client.mode), **obj})

        client.control_cb = control_cb

        audit.record("terminal_ws_open", actor=self._audit_actor(), result="ok",
                     summary=audit.summarize("terminal_ws_open", {
                         "terminal_id": terminal_id, "host": info.get("host"),
                         "mode": mode, "token_id": token_id}, {}))
        try:
            hub.start()
            snap = hub.snapshot()
            if snap:
                client.enqueue(snap)
            hub.add_client(client)
            if mode == "read":
                # A read ticket is never granted the keyboard, even if first.
                client.mode = "read"
                actual = "read"
            else:
                actual = hub.mode_for_new_client(client)
            send_control(control_state(actual))
            self._terminal_ws_loop(hub, client, send_raw, send_control, control_state,
                                   info, counters)
        finally:
            client.close(ws.CLOSE_GOING_AWAY)
            hub.remove_client(client)
            self._release_terminal_client(session)
            audit.record("terminal_ws_close", actor=self._audit_actor(), result="ok",
                         summary=audit.summarize("terminal_ws_close", {
                             "terminal_id": terminal_id, "host": info.get("host"),
                             "token_id": token_id, "duration": round(time.time() - started, 1),
                             "in_bytes": counters["in"], "out_bytes": counters["out"]}, {}))
            try:
                self.connection.close()
            except OSError:
                pass

    def _consume_ticket(self, ticket: str, terminal_id: str, session: str | None,
                        origin: str) -> dict[str, Any] | None:
        if not ticket:
            return None
        now = time.time()
        with self.server.tickets_lock:
            for key in [k for k, v in self.server.tickets.items() if v.get("expires", 0) < now]:
                self.server.tickets.pop(key, None)
            rec = self.server.tickets.get(ticket)
            if not rec or rec.get("used") or rec.get("expires", 0) < now:
                return None
            if (rec.get("terminal_id") != terminal_id
                    or rec.get("session") != _session_key(session)
                    or rec.get("origin", "") != origin):
                return None
            rec["used"] = True
            return dict(rec)

    def _reserve_terminal_client(self, terminal_id: str, session: str | None) -> bool:
        key = _session_key(session)
        with self.server.terminal_hubs_lock:
            hub = self.server.terminal_hubs.get(terminal_id)
            if hub is not None and len(hub.clients) >= MAX_WS_CLIENTS_PER_TERMINAL:
                return False
            used = self.server.terminal_clients_by_session.get(key, 0)
            if used >= MAX_WS_CLIENTS_PER_SESSION:
                return False
            self.server.terminal_clients_by_session[key] = used + 1
            return True

    def _release_terminal_client(self, session: str | None) -> None:
        key = _session_key(session)
        with self.server.terminal_hubs_lock:
            used = self.server.terminal_clients_by_session.get(key, 0)
            if used <= 1:
                self.server.terminal_clients_by_session.pop(key, None)
            else:
                self.server.terminal_clients_by_session[key] = used - 1

    def _terminal_ws_loop(self, hub, client, send_raw, send_control, control_state,
                          info, counters) -> None:
        from .. import settings

        max_size = int(settings.get("terminals.max_message_bytes"))
        idle_timeout = int(settings.get("terminals.idle_timeout") or 0)
        reader = ws.FrameReader(self.connection, max_size, timeout=0.5)
        unknown = 0
        last_activity = time.monotonic()

        def close_with(code: int) -> None:
            try:
                send_raw(ws.encode_close(code))
            except OSError:
                pass

        while True:
            if idle_timeout and time.monotonic() - last_activity >= idle_timeout:
                send_control({**control_state(client.mode), "closed": True})
                return
            try:
                frame = reader.read()
            except TimeoutError:
                continue
            except ws.FrameError as exc:
                close_with(exc.code)
                return
            except (OSError, ValueError):
                return
            if frame is None:
                return
            last_activity = time.monotonic()
            if frame.opcode == ws.OP_CLOSE:
                return
            if frame.opcode == ws.OP_PING:
                send_raw(ws.encode_pong(frame.payload))
                continue
            if frame.opcode == ws.OP_PONG:
                continue
            if not frame.fin or frame.opcode == ws.OP_CONT:
                close_with(ws.CLOSE_UNSUPPORTED)
                return
            if frame.opcode != ws.OP_BINARY:
                unknown += 1
                if unknown > 50:
                    close_with(ws.CLOSE_PROTOCOL)
                    return
                continue
            payload = frame.payload
            if not payload:
                unknown += 1
                if unknown > 50:
                    close_with(ws.CLOSE_PROTOCOL)
                    return
                continue
            mtype, body = payload[0], payload[1:]
            if mtype == 0x01:  # input
                counters["in"] += len(body)
                if client.mode == "write" and not hub.readonly:
                    hub.feed_input(body)
            elif mtype == 0x03:  # resize
                if len(body) == 4 and client.mode == "write" and not hub.readonly:
                    cols = struct.unpack(">H", body[:2])[0]
                    rows = struct.unpack(">H", body[2:])[0]
                    if 10 <= cols <= 500 and 2 <= rows <= 200 and hub.resize(cols, rows):
                        info["cols"], info["rows"] = cols, rows
                        for other in list(hub.clients):
                            if other is not client:
                                other.notify({"cols": cols, "rows": rows})
            elif mtype == 0x04:  # app ping
                send_raw(ws.encode_binary(bytes([0x05])))
            elif mtype == 0x05:  # app pong
                pass
            elif mtype == 0x07:  # claim keyboard
                if not hub.readonly:
                    hub.claim(client)
                    send_control(control_state(client.mode))
            else:
                unknown += 1
                if unknown > 50:
                    close_with(ws.CLOSE_PROTOCOL)
                    return

    # Cadence of the server-side adaptation loop. The focused agent (the one the
    # browser is showing) is refreshed fast; the state snapshot and the other
    # agents' transcripts are much slower, so the cost no longer grows with the
    # number of agents on every 0.2 s tick.
    WS_TICK = 0.2
    WS_SNAPSHOT_EVERY = 1.0
    WS_OTHERS_EVERY = 1.5

    def _ws_loop(self) -> None:
        import select

        last_state = ""
        last_output: dict[str, str] = {}
        focus: str | None = None
        agent_ids: list[str] = []
        next_snapshot = 0.0
        next_others = 0.0
        sock = self.connection
        # Reads use select() with a timeout; the socket itself stays blocking so
        # that _send can write partial/full frames reliably. A non-blocking
        # sendall would raise BlockingIOError on a slow/full remote buffer and
        # silently kill this loop (local loopback hid the bug).
        sock.setblocking(True)

        def push_output(aid: str) -> bool:
            try:
                out = self.control.call(
                    "agent_transcript", target=aid, max_lines=200
                )["output"]
            except RpcError:
                return True
            if out != last_output.get(aid):
                last_output[aid] = out
                return self._send({"type": "output", "agent_id": aid, "output": out})
            return True

        while True:
            try:
                ready, _, _ = select.select([sock], [], [], self.WS_TICK)
            except OSError:
                return
            if ready:
                try:
                    data = sock.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    opcode, _flen, payload = ws.parse_frame(data)
                except (ValueError, IndexError):
                    opcode, payload = None, b""
                if opcode == 8:
                    try:
                        sock.sendall(ws.encode_close())
                    except OSError:
                        pass
                    return
                if opcode == 1:
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        msg = {}
                    if msg.get("type") == "focus" and isinstance(msg.get("agent_id"), str):
                        focus = msg["agent_id"]
                        last_output.pop(focus, None)  # force an immediate push
                        if not push_output(focus):
                            return
            now = time.monotonic()
            if now >= next_snapshot:
                next_snapshot = now + self.WS_SNAPSHOT_EVERY
                try:
                    snap = _snapshot(self.control,
                                     include_terminals=self._terminals_readable())
                except RpcError:
                    time.sleep(0.5)
                    continue
                agent_ids = [a["agent_id"] for a in snap["agents"]]
                # Include hosts and terminals so creating/unlocking a terminal
                # (or a host change) is pushed without a manual reload. Only the
                # stable terminal fields count, so live output does not push the
                # whole snapshot every tick.
                state = json.dumps(
                    {"agents": snap["agents"], "teams": snap["teams"],
                     "messages": snap["messages"], "hosts": snap["hosts"],
                     "terminals": [
                         {k: t.get(k) for k in (
                             "session_id", "kind", "status", "title", "host",
                             "readonly", "cols", "rows", "cwd")}
                         for t in snap["terminals"]]},
                    sort_keys=True,
                )
                if state != last_state:
                    last_state = state
                    if not self._send({"type": "state", **snap}):
                        return
            if focus in agent_ids and not push_output(focus):
                return
            if now >= next_others:
                next_others = now + self.WS_OTHERS_EVERY
                for aid in agent_ids:
                    if aid != focus and not push_output(aid):
                        return

    def _send(self, obj: dict[str, Any]) -> bool:
        """Send a WebSocket text frame, tolerating a momentarily full buffer.

        The socket is blocking, so sendall will not raise BlockingIOError; but
        we still guard against a dead peer and never let a send failure crash
        the loop. Frames are bounded in size (max_lines=200 of text).
        """
        try:
            self.connection.settimeout(10.0)
            self.connection.sendall(ws.encode_text(json.dumps(obj)))
            return True
        except OSError:
            return False


class _TrackingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that can also drop its open connections.

    ``shutdown()`` only stops accepting; keep-alive HTTP and WebSocket
    connections would otherwise outlive a "disable", leaving access open.
    """

    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._clients: set[Any] = set()
        self._clients_lock = threading.Lock()
        self.terminal_hubs: dict[str, Any] = {}
        self.terminal_hubs_lock = threading.Lock()
        self.tickets: dict[str, Any] = {}
        self.tickets_lock = threading.Lock()
        self.terminal_clients_by_session: dict[str, int] = {}
        self.webauthn_challenges: dict[str, Any] = {}
        self.webauthn_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def close_terminal_hubs(self) -> None:
        with self.terminal_hubs_lock:
            hubs, self.terminal_hubs = list(self.terminal_hubs.values()), {}
        for hub in hubs:
            try:
                hub.stop()
            except Exception:
                pass

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._clients_lock:
            self._clients.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        with self._clients_lock:
            self._clients.discard(request)
        super().shutdown_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client vanishing mid-request is routine: one log line, not a traceback."""
        import logging
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (OSError, TimeoutError)):  # BrokenPipe, ConnectionReset, bad probes…
            logging.getLogger("crewhall.web").info("client %s: %s", client_address[0], exc)
            return
        super().handle_error(request, client_address)

    def close_clients(self) -> None:
        with self._clients_lock:
            clients, self._clients = list(self._clients), set()
        for sock in clients:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


class WebServer:
    def __init__(
        self, host: str = "127.0.0.1", port: int = 8765,
        socket_path: str | None = None,
        policy: SecurityPolicy | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.socket_path = socket_path
        self.policy = policy or SecurityPolicy()
        self._httpd: _TrackingHTTPServer | None = None
        self._serving = False

    def start(self) -> None:
        ensure_daemon(self.socket_path)
        control = Client(socket_path=self.socket_path, autostart=True)

        handler = type(
            "BoundHandler", (Handler,),
            {"control": control, "security": self.policy},
        )
        self._httpd = _TrackingHTTPServer((self.host, self.port), handler)

    def serve_forever(self) -> None:
        assert self._httpd is not None
        self._serving = True
        try:
            self._httpd.serve_forever()
        finally:
            self._serving = False

    def stop(self) -> None:
        """Stop serving: close the listening socket and every open connection."""
        if self._httpd is not None:
            if self._serving:  # shutdown() would block forever otherwise
                self._httpd.shutdown()
            try:
                self._httpd.close_terminal_hubs()
            except Exception:
                pass
            self._httpd.server_close()
            self._httpd.close_clients()


def resolve_host(args: Any) -> tuple[str, list[str]]:
    """Return (bind_host, extra_allowed_hosts)."""
    from . import tailscale

    if args.tailscale:
        if not tailscale.available():
            raise SystemExit(
                "Tailscale is not available/configured (install/login to tailscale)."
            )
        ip = tailscale.local_ipv4()
        if not ip:
            raise SystemExit(
                "Tailscale is available but no Tailscale IPv4 address was found."
            )
        allowed = [ip]
        dns = tailscale.dns_name()
        if dns:
            allowed.append(dns)
        if args.host and args.host != "127.0.0.1":
            ip = args.host
        for extra in args.allow_host or []:
            allowed.append(extra)
        return ip, allowed
    allowed = list(args.allow_host or [])
    return args.host, allowed


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="crewhall web",
        description="Web UI client for crewhall (local by default).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--socket", default=None)
    parser.add_argument(
        "--tailscale", action="store_true",
        help="bind to this machine's Tailscale IPv4 address (private tailnet access)",
    )
    parser.add_argument(
        "--allow-host", action="append", default=None,
        help="additional Host header value to accept (repeatable)",
    )
    parser.add_argument(
        "--require-auth", action="store_true",
        help="require an access token even on localhost",
    )
    args = parser.parse_args(argv)

    from .. import settings

    host, allowed_hosts = resolve_host(args)
    # Remote binds must be authenticated; localhost can be token-free unless the
    # user asked for it (--require-auth or security.local_requires_token).
    remote = host not in ("127.0.0.1", "localhost", "::1")
    require_auth = args.require_auth or remote or bool(settings.get("security.local_requires_token"))
    if require_auth and not auth.token_exists():
        if args.require_auth or remote:
            raise SystemExit(
                "authentication is required but no token exists; run "
                "`crewhall web token generate` first."
            )
        auth.generate_token(rotate=True)
    policy = SecurityPolicy(
        require_auth=require_auth,
        allowed_hosts=allowed_hosts + ([] if remote else ["127.0.0.1", "localhost"]),
        allowed_origins=[],
        secure_cookie=False,
    )
    server = WebServer(host=host, port=args.port, socket_path=args.socket, policy=policy)
    server.start()
    scheme = "http"
    print(
        f"crewhall web on {scheme}://{host}:{args.port} "
        f"(auth={'required' if require_auth else 'off'})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
