"""Terminal routes: page, unlock/ticket, authorization and the WebSocket (mixin)."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import contextlib
import json
import os
import secrets
import struct
import threading
import time
from typing import Any
from urllib.parse import parse_qs

from ...client import RpcError
from .. import auth, terminal_tokens, totp, ws
from ..shared import (
    MAX_WS_CLIENTS_PER_SESSION,
    MAX_WS_CLIENTS_PER_TERMINAL,
    STATIC,
    TERMINAL_WRITE_OPS,
    TICKET_TTL,
    _session_key,
)


class TerminalRoutes:
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

    def _do_terminal_unlock(self) -> None:
        from ... import audit, settings

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
        code = payload.get("code")
        rec = terminal_tokens.verify(token) if isinstance(token, str) else None
        # A TOTP code is an alternative unlock factor, gated by terminals.totp_grants.
        code_ok = bool(settings.get("terminals.totp_grants")) and isinstance(code, str)
        if not rec and not code_ok:
            auth.note_login_failure(ip)
            return self._error("invalid terminal token", 401)
        session, extra = self._session_or_new()
        if not session:
            return self._error("sign in first", 401)
        if rec:
            auth.note_login_success(ip)
            auth.unlock_terminal(session, scopes=rec["scopes"], hosts=rec["hosts"],
                                 token_id=rec["id"])
            return self._json({"ok": True, "scopes": rec["scopes"], "hosts": rec["hosts"]},
                              extra=extra)
        if not totp.verify_any(code):
            auth.note_login_failure(ip)
            return self._error("invalid code", 401)
        auth.note_login_success(ip)
        scopes = [terminal_tokens.SCOPE_READ, terminal_tokens.SCOPE_WRITE]
        auth.unlock_terminal(session, scopes=scopes, hosts=["*"], token_id=None)
        audit.record("web_terminal_unlock", actor=self._audit_actor(), result="ok",
                     summary="totp")
        return self._json({"ok": True, "scopes": scopes, "hosts": ["*"]}, extra=extra)

    def _do_terminal_ticket(self) -> None:
        from ... import settings

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

    def _effective_grant(self) -> dict[str, Any] | None:
        """The session's terminal grant, or a synthetic one for the master session
        when ``terminals.master_grants`` is on."""
        from ... import settings

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
        from ... import settings

        if not settings.get("terminals.enabled"):
            return False
        grant = self._effective_grant()
        return grant is not None and terminal_tokens.scopes_imply(
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

    def _terminal_context(self, terminal_id: str):
        """(info, host_cfg) for an attachable terminal, or raise RpcError."""
        from ... import settings, terminals as termlib

        termlib.validate_terminal_id(terminal_id)
        info = self.control.call("terminal_info", id=terminal_id)["terminal"]
        host = info.get("host")
        host_cfg = settings.host(host) if host else None
        return info, host_cfg

    def _ws_terminal(self, terminal_id: str, parsed) -> None:
        from ... import settings

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

        from ... import audit
        from ...terminal_stream import HubClient, TerminalHub

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
            with contextlib.suppress(OSError):
                self.connection.close()

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
        from ... import settings

        max_size = int(settings.get("terminals.max_message_bytes"))
        idle_timeout = int(settings.get("terminals.idle_timeout") or 0)
        reader = ws.FrameReader(self.connection, max_size, timeout=0.5)
        unknown = 0
        last_activity = time.monotonic()

        def close_with(code: int) -> None:
            with contextlib.suppress(OSError):
                send_raw(ws.encode_close(code))

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
