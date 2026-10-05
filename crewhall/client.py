from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from typing import Any
from collections.abc import Iterator

from . import paths


class RpcError(RuntimeError):
    pass


class ClientConnectionError(RpcError):
    """The daemon could not be reached (socket missing/refused/closed)."""


class Client:
    def __init__(self, socket_path: str | None = None, autostart: bool = True) -> None:
        # Agents invoked from their own TUI must talk to the daemon that launched
        # them, not to a freshly-resolved runtime path. Prefer the socket the
        # control plane put in our environment.
        from . import brand

        self.socket_path = (
            socket_path
            or brand.env("SOCKET")
            or paths.socket_path()
        )
        # A tunnelled remote agent talks to a gateway, never to a daemon it
        # could (wrongly) spawn on its own machine.
        self.autostart = autostart and not brand.env("GATEWAY")

    def call(self, op: str, **params: Any) -> dict[str, Any]:
        if self.autostart:
            ensure_daemon(self.socket_path)
        try:
            return self._roundtrip({"op": op, **params})
        except ClientConnectionError:
            # The cached socket path may be stale (daemon died / socket removed).
            # Recover inside the connection layer, never in the UI.
            ensure_daemon(self.socket_path)
            return self._roundtrip({"op": op, **params})

    def _roundtrip(self, request: dict[str, Any]) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            try:
                sock.connect(self.socket_path)
            except OSError as exc:
                raise ClientConnectionError(
                    f"cannot connect to daemon at {self.socket_path}: {exc}"
                ) from exc
            try:
                sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
                response = _read_line(sock)
            except OSError as exc:
                raise ClientConnectionError(f"daemon connection lost: {exc}") from exc
        if response is None:
            raise ClientConnectionError("daemon closed the connection")
        if not response.get("ok", False):
            raise RpcError(response.get("error", "unknown error"))
        return response


def _read_line(sock: socket.socket) -> dict[str, Any] | None:
    chunks = bytearray()
    while True:
        data = sock.recv(65536)
        if not data:
            break
        chunks.extend(data)
        if b"\n" in chunks:
            break
    if not chunks:
        return None
    line = bytes(chunks).split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def ping(socket_path: str | None = None, timeout: float = 0.3) -> bool:
    path = socket_path or paths.socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except OSError:
            return False
        try:
            sock.sendall(b'{"op":"ping"}\n')
            return _read_line(sock) is not None
        except OSError:
            return False


@contextlib.contextmanager
def _exclusive(path: str) -> Iterator[None]:
    fd = open(path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


def ensure_daemon(socket_path: str | None = None, start_timeout: float = 10.0) -> None:
    """Make sure a daemon is listening; spawn one if needed.

    Serialised with a lock so concurrent clients never spawn several daemons.
    """
    path = socket_path or paths.socket_path()
    if ping(path):
        return
    with _exclusive(paths.spawn_lock_path()):
        if ping(path):  # another client started it while we waited
            return
        _spawn(path)
        deadline = time.monotonic() + start_timeout
        while time.monotonic() < deadline:
            if ping(path):
                return
            time.sleep(0.05)
    raise RpcError(
        f"daemon did not start within {start_timeout}s (see {paths.log_path()})"
    )


_SPAWNED: list[subprocess.Popen] = []


def _spawn(socket_path: str) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = paths.project_root() + os.pathsep + env.get("PYTHONPATH", "")
    # Agent CLIs (Bun/OpenCode) only honour TMPDIR from the process environment
    # they inherit, not from a later Popen(env=...). Propagate a usable TMPDIR
    # to the daemon so the agents it spawns inherit it too.
    tmpdir = paths.usable_tmpdir()
    if tmpdir and not env.get("TMPDIR"):
        env["TMPDIR"] = tmpdir
        env.setdefault("CLAUDE_CODE_TMPDIR", tmpdir)
    log = open(paths.log_path(), "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-P", "-m", "crewhall.daemon", "--foreground",
         "--socket", socket_path],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        env=env,
    )
    log.close()
    _SPAWNED[:] = [p for p in _SPAWNED if p.poll() is None]
    _SPAWNED.append(proc)
