"""Restricted agent gateway: the only daemon surface a *remote* host can reach.

The daemon's own socket is a full control plane (create/kill/write/settings…)
protected only by filesystem permissions.  Exposing it through an SSH reverse
tunnel would let anything running on the remote machine drive the local one.
Instead every tunnelled host gets its own gateway socket that

* accepts only the token-authenticated *agent* operations (identity, messages,
  requests) — everything else is refused before reaching the daemon;
* requires a non-empty token and an acting agent that lives on **that host**,
  so a compromised host cannot speak as a local agent or as an agent of
  another host (the token is still verified by the controller);
* strips every client-supplied ``_``-prefixed field (``_actor`` is ours);
* bounds request size, read time and concurrency.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Any, Callable

log = logging.getLogger("crewhall.gateway")

# op -> request field that names the acting agent.
ALLOWED_OPS = {
    "agent_identity": "target",
    "team_send": "sender",
    "request_create": "sender",
    "request_reply": "agent",
    "request_cancel": "agent",
}
MAX_LINE = 256 * 1024
READ_TIMEOUT = 30.0
MAX_CONNECTIONS = 16


class AgentGateway:
    def __init__(
        self,
        path: str,
        alias: str,
        dispatch: Callable[[dict[str, Any]], dict[str, Any]],
        host_of: Callable[[str], str | None],
    ) -> None:
        self.path = path
        self.alias = alias
        self._dispatch = dispatch
        self._host_of = host_of
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    # ------------------------------------------------------------- policy
    def handle_request(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {"ok": False, "error": "bad request"}
        op = request.get("op")
        if op == "ping":  # liveness only: never reveal daemon details
            return {"ok": True}
        field = ALLOWED_OPS.get(op) if isinstance(op, str) else None
        if field is None:
            return {"ok": False, "error": f"operation {op!r} is not available through the gateway"}
        actor = request.get(field)
        token = request.get("token")
        if not isinstance(actor, str) or not actor or not isinstance(token, str) or not token:
            return {"ok": False, "error": "invalid agent identity/token"}
        try:
            actor_host = self._host_of(actor)
        except Exception:  # noqa: BLE001 - unknown agent
            actor_host = None
        if actor_host != self.alias:
            return {"ok": False, "error": "invalid agent identity/token"}
        clean = {k: v for k, v in request.items() if not str(k).startswith("_")}
        clean["_actor"] = f"ssh:{self.alias}"
        return self._dispatch(clean)

    # ------------------------------------------------------------- server
    def start(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            sock.bind(self.path)
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        sock.listen(16)
        sock.settimeout(0.5)
        self._sock = sock
        threading.Thread(
            target=self._serve, name=f"gateway-{self.alias}", daemon=True
        ).start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(READ_TIMEOUT)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf.extend(chunk)
                if len(buf) > MAX_LINE:
                    self._reply(conn, {"ok": False, "error": "request too large"})
                    return
            line = bytes(buf).split(b"\n", 1)[0]
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._reply(conn, {"ok": False, "error": "bad json"})
                return
            conn.settimeout(None)  # request_create may legitimately wait
            self._reply(conn, self.handle_request(request))
        except OSError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("gateway request failed")
        finally:
            conn.close()
            self._slots.release()

    @staticmethod
    def _reply(conn: socket.socket, response: dict[str, Any]) -> None:
        try:
            conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except OSError:
            pass
