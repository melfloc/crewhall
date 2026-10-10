"""Reverse SSH tunnel that lets agents on a remote host reach the local gateway.

Per tunnelled host (``hosts.<name>.tunnel = true``) the daemon runs

* an :class:`~crewhall.gateway.AgentGateway` on a private local socket, and
* one supervised ``ssh -N -R <remote-sock>:<gateway-sock>`` process.

The remote socket lives in a private (0700, owned-by-us, non-symlink)
directory on the remote machine.  The tunnel process ignores the user's
``~/.ssh/config`` (``-F /dev/null``) so no configured forwarding, proxy or
include can alter it, and uses the same fixed, strict options as the command
transport.  A dropped tunnel is re-established with a bounded backoff; while it
is down the host's agents simply cannot message (``state`` says so).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Any
from collections.abc import Callable

from .backends import ssh_tmux
from .backends.ssh_tmux import HostUnreachable, SshTmuxBackend
from .gateway import AgentGateway

log = logging.getLogger("crewhall.remote_link")

READY_TIMEOUT = 12.0
BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)

# Remote preparation: private runtime dir, stale socket removed, path printed.
# ``$1`` is the socket file name (validated alias + ".sock").
_PREPARE = (
    'base="${XDG_RUNTIME_DIR:-/tmp/crewhall-$(id -u)}"; d="$base/crewhall-gw"; '
    'umask 077; mkdir -p -m 700 "$base" "$d" || exit 3; '
    'for x in "$base" "$d"; do '
    '[ -d "$x" ] && [ ! -L "$x" ] && [ -O "$x" ] && '
    '[ -z "$(find "$x" -maxdepth 0 -perm /077)" ] || exit 4; done; '
    'rm -f "$d/$1" && printf %s "$d/$1"'
)


class HostLink:
    def __init__(
        self,
        host: dict[str, Any],
        dispatch: Callable[[dict[str, Any]], dict[str, Any]],
        host_of: Callable[[str], str | None],
    ) -> None:
        self.host = host
        self.alias = host["name"]
        self._backend = SshTmuxBackend(host)
        self.gateway_path = os.path.join(ssh_tmux.control_dir(), f"gw-{self.alias}.sock")
        if ":" in self.gateway_path:
            raise RuntimeError("the gateway socket path must not contain ':'")
        self.gateway = AgentGateway(self.gateway_path, self.alias, dispatch, host_of)
        self.remote_socket: str | None = None
        self.state = "stopped"  # stopped|connecting|connected|reconnecting
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -------------------------------------------------------------- public
    def ensure(self, timeout: float | None = None) -> str:
        """Start the link if needed and return the remote socket path."""
        if timeout is None:
            timeout = READY_TIMEOUT + 5
        with self._lock:
            if self._thread is None:
                self._stop.clear()
                self.state = "connecting"
                self.gateway.start()
                self._thread = threading.Thread(
                    target=self._supervise, name=f"ssh-tunnel-{self.alias}", daemon=True
                )
                self._thread.start()
        if not self._connected.wait(timeout) or not self.remote_socket:
            raise HostUnreachable(f"host {self.alias}: gateway tunnel is not up")
        return self.remote_socket

    def stop(self) -> None:
        self._stop.set()
        self._connected.clear()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
        self.gateway.stop()
        self.state = "stopped"

    # ------------------------------------------------------------ internals
    def _tunnel_argv(self, remote_socket: str) -> list[str]:
        cfg = self.host
        argv = [
            "ssh", "-F", "/dev/null", "-N", "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
        ]
        if cfg.get("known_hosts"):
            argv += ["-o", f"UserKnownHostsFile={cfg['known_hosts']}"]
        if cfg.get("identity"):
            argv += ["-i", cfg["identity"]]
        if int(cfg.get("port") or 22) != 22:
            argv += ["-p", str(int(cfg["port"]))]
        argv += ["-R", f"{remote_socket}:{self.gateway_path}", "--", cfg["ssh"]]
        return argv

    def _prepare_remote(self) -> str:
        proc = self._backend._ssh_run(
            "sh -c " + _shell_quote(_PREPARE) + " sh " + _shell_quote(f"{self.alias}.sock")
        )
        if proc.returncode != 0 or not proc.stdout.startswith("/"):
            raise HostUnreachable(
                f"host {self.alias}: cannot prepare the remote gateway directory"
            )
        return proc.stdout.strip()

    def _remote_socket_ready(self, path: str) -> bool:
        proc = self._backend._ssh_run("test -S " + _shell_quote(path), timeout=6.0)
        return proc.returncode == 0

    def _supervise(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                path = self._prepare_remote()
                self.remote_socket = path
                self._proc = subprocess.Popen(
                    self._tunnel_argv(path),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + READY_TIMEOUT
                ready = False
                while time.monotonic() < deadline and not self._stop.is_set():
                    if self._proc.poll() is not None:
                        break
                    if self._remote_socket_ready(path):
                        ready = True
                        break
                    time.sleep(0.2)
                if ready:
                    attempt = 0
                    self.state = "connected"
                    self._connected.set()
                    while not self._stop.is_set() and self._proc.poll() is None:
                        self._stop.wait(1.0)
            except HostUnreachable as exc:
                log.warning("%s", exc)
            except Exception:  # noqa: BLE001
                log.exception("tunnel supervisor for %s failed", self.alias)
            finally:
                self._connected.clear()
                proc = self._proc
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            if self._stop.is_set():
                return
            self.state = "reconnecting"
            self._stop.wait(BACKOFF[min(attempt, len(BACKOFF) - 1)])
            attempt += 1


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


class RemoteLinks:
    """The daemon's set of host links (created lazily, closed on shutdown)."""

    def __init__(self) -> None:
        self._links: dict[str, HostLink] = {}
        self._lock = threading.Lock()
        self.dispatch: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.host_of: Callable[[str], str | None] = lambda _agent: None

    def ensure(self, host: dict[str, Any]) -> HostLink:
        if self.dispatch is None:
            raise RuntimeError("the agent gateway is not available in this process")
        with self._lock:
            link = self._links.get(host["name"])
            if link is None:
                link = HostLink(host, self.dispatch, self.host_of)
                self._links[host["name"]] = link
        link.ensure()
        return link

    def stop(self, name: str) -> None:
        with self._lock:
            link = self._links.pop(name, None)
        if link is not None:
            link.stop()

    def states(self) -> dict[str, str]:
        with self._lock:
            return {name: link.state for name, link in self._links.items()}

    def stop_all(self) -> None:
        with self._lock:
            links = list(self._links.values())
            self._links.clear()
        for link in links:
            link.stop()
