from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..client import Client, RpcError, ensure_daemon
from . import auth
from .shared import ALLOWED_OPS, MAX_BODY_BYTES
from .routes.auth import AuthRoutes
from .routes.conversations import ConversationRoutes
from .routes.office import OfficeRoutes
from .routes.static import StaticRoutes
from .routes.terminals import TerminalRoutes
from .routes.totp import TotpRoutes
from .routes.webauthn import WebAuthnRoutes
from .routes.ws import WsRoutes
import contextlib


def _office_origins() -> list[str]:
    """Origins (http + ws) the OnlyOffice Document Server is served from."""
    try:
        from .. import office

        if not office.enabled():
            return []
        origin = office.public_origin()
    except Exception:  # noqa: BLE001 - a broken office setting must not break the UI
        return []
    if not origin:
        return []
    netloc = origin.split("://", 1)[-1]
    ws = f"ws://{netloc}" if origin.startswith("http://") else f"wss://{netloc}"
    return [origin, ws]


def content_security_policy() -> str:
    """No inline script, no eval. Inline *styles* are allowed because xterm.js
    injects a stylesheet for its dynamic sizing. When the OnlyOffice editor is
    enabled, its origin is added to the directives the embedded editor needs."""
    extra = (" " + " ".join(_office_origins())) if _office_origins() else ""
    return ("default-src 'self'; script-src 'self'" + extra + "; "
            "style-src 'self' 'unsafe-inline'" + extra + "; img-src 'self' data:" + extra + "; "
            "font-src 'self'" + extra + "; connect-src 'self'" + extra + "; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
            "worker-src 'self' blob:" + extra + "; manifest-src 'self'; frame-src 'self'" + extra)


# The static CSP under a non-office install; tests read `CSP` by name.
CSP = content_security_policy()

_SECURITY_HEADERS = (
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
    # Chats with a crew show up in Mission Control as nodes (an older daemon
    # without the op simply has none).
    try:
        conversations = client.call("conversation_crew")["conversations"]
    except Exception:  # noqa: BLE001 - an optional field must never break the push
        conversations = []
    return {"agents": agents, "teams": teams, "messages": messages,
            "requests": requests, "hosts": hosts, "terminals": terminals,
            "conversations": conversations, "version": __version__}


class Handler(
    AuthRoutes,
    WebAuthnRoutes,
    TotpRoutes,
    ConversationRoutes,
    OfficeRoutes,
    TerminalRoutes,
    StaticRoutes,
    WsRoutes,
    BaseHTTPRequestHandler,
):
    server_version = "crewhall-web"
    protocol_version = "HTTP/1.1"

    # injected by the server
    control: Client
    security: "SecurityPolicy"
    # BaseHTTPRequestHandler types ``self.server`` as BaseServer; this handler is
    # always served by our own subclass (tickets, terminal hubs, locks…).
    server: "_TrackingHTTPServer"

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
        sent: set[str] = getattr(self, "_sent_headers", set())
        headers = (("Content-Security-Policy", content_security_policy()), *_SECURITY_HEADERS)
        for key, value in headers:
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
        if path == "/api/conversation/artifact":
            # The Document Server fetches this with a temporary token, not a
            # session cookie, so it authenticates itself inside.
            return self._serve_artifact(parse_qs(parsed.query))
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
        if path == "/api/office/config":
            return self._office_config(parse_qs(parsed.query))
        if path == "/api/conversation/export":
            return self._conversation_export(parse_qs(parsed.query))
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
        if parsed.path == "/api/upload":
            return self._upload(parse_qs(parsed.query))
        if parsed.path == "/api/conversation/upload":
            return self._conversation_upload(parse_qs(parsed.query))
        if parsed.path == "/api/office/callback":
            return self._office_callback(parse_qs(parsed.query))
        if parsed.path == "/api/terminal-unlock":
            return self._do_terminal_unlock()
        if parsed.path == "/api/terminal-ticket":
            return self._do_terminal_ticket()
        if parsed.path.startswith("/api/webauthn/"):
            return self._do_webauthn(parsed.path[len("/api/webauthn/"):])
        if parsed.path.startswith("/api/totp/"):
            return self._do_totp(parsed.path[len("/api/totp/"):])
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
        self.totp_pending: dict[str, Any] = {}
        self.totp_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def close_terminal_hubs(self) -> None:
        with self.terminal_hubs_lock:
            hubs, self.terminal_hubs = list(self.terminal_hubs.values()), {}
        for hub in hubs:
            try:
                hub.stop()
            except Exception:  # noqa: BLE001 - one stuck hub must not stop the rest
                logging.getLogger("crewhall.web").debug("terminal hub stop failed", exc_info=True)

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
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()


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
            except Exception:  # noqa: BLE001 - stop serving even if hubs misbehave
                logging.getLogger("crewhall.web").debug(
                    "closing terminal hubs failed", exc_info=True)
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
