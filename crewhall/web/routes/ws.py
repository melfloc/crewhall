"""The main WebSocket: handshake and the snapshot/output push loop (mixin)."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import contextlib
import json
import time
from typing import Any

from ...client import RpcError
from .. import ws


class WsRoutes:
    # Cadence of the server-side adaptation loop. The focused agent (the one the
    # browser is showing) is refreshed fast; the state snapshot and the other
    # agents' transcripts are much slower, so the cost no longer grows with the
    # number of agents on every 0.2 s tick.
    WS_TICK = 0.2
    WS_SNAPSHOT_EVERY = 1.0
    WS_OTHERS_EVERY = 1.5

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
        with contextlib.suppress(OSError, ValueError):
            self._ws_loop()

    def _ws_loop(self) -> None:
        import select

        # Imported here (not at module load) to avoid a server<->routes cycle.
        from ..server import _snapshot

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
                    with contextlib.suppress(OSError):
                        sock.sendall(ws.encode_close())
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
