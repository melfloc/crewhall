from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..client import Client, RpcError, ensure_daemon
from . import auth, ws

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
<div class="err" id="e"></div><button>Sign in</button></form>
<script src="/static/login.js"></script></body></html>"""

# Strict Content-Security-Policy: no inline script, no inline style, no eval.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
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


def _snapshot(client: Client) -> dict[str, Any]:
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
    return {"agents": agents, "teams": teams, "messages": messages,
            "requests": requests, "hosts": hosts, "version": __version__}


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
        if path.startswith("/static/") or path == "/sw.js":
            # like the index, static assets hold no secrets; the API enforces auth
            return self._serve_static("sw.js" if path == "/sw.js" else path[len("/static/"):])
        if not self._guard():
            return
        if path == "/api/state":
            try:
                return self._json({"ok": True, **_snapshot(self.control)})
            except RpcError as exc:
                return self._error(str(exc), 502)
        if path == "/ws":
            return self._websocket()
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
                  "web_token_revoke", "web_token_rotate", "audit_list"):
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
        return self._json(
            {"ok": True},
            extra=[("Set-Cookie", auth.cookie_header(session, secure=self.security.secure_cookie))],
        )

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
        if op.startswith("terminal_"):
            from .. import settings

            if not settings.get("terminals.enabled"):
                # Closed by default: the route does not even exist.
                return self._error("not found", 404)
            if op == "terminal_create":
                params = {**params, "owner": self._client_label()}
        if op in ("interaction_respond", "agent_process_signal"):
            params = {**params, "by": "web"}  # the audit trail names the channel, not the client's claim
        try:
            result = self.control.call(op, **{**params, "_actor": self._actor()})
        except RpcError as exc:
            return self._error(str(exc), 502)
        result.pop("ok", None)
        return self._json({"ok": True, **result})

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
                    snap = _snapshot(self.control)
                except RpcError:
                    time.sleep(0.5)
                    continue
                agent_ids = [a["agent_id"] for a in snap["agents"]]
                state = json.dumps(
                    {"agents": snap["agents"], "teams": snap["teams"],
                     "messages": snap["messages"]},
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
        super().__init__(*args, **kwargs)

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
