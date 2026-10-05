from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from typing import TYPE_CHECKING

from ..backend import Backend
from ..keys import tmux_name

if TYPE_CHECKING:
    from ..types import SessionSpec

# Dedicated tmux server socket. Overridable so test runs (which start real
# daemons that adopt every session on the server and close them on shutdown)
# can never touch the user's live agents.
#
# The value is resolved on *every* use, never at import time: `unittest
# discover` imports this module as part of the `crewhall` package before
# `tests/__init__` can set CREWHALL_TMUX_SOCKET, so a module-level
# constant would freeze the production socket for the whole test run and leak
# test sessions into the user's tmux server (Fase 0, 0.48.0).
DEFAULT_SOCKET = "crewhall"


def socket_name() -> str:
    from .. import brand

    return brand.env("TMUX_SOCKET", DEFAULT_SOCKET)


def __getattr__(name: str) -> str:
    # Keep ``tmux.SOCKET`` working for callers that read it, but resolving it
    # lazily so it reflects the environment at read time, not at import time.
    if name == "SOCKET":
        return socket_name()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _private_dir() -> str:
    """0700 directory for short-lived launch files (never world-readable)."""
    from .. import paths

    path = os.path.join(paths.state_dir(), "launch")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


class TmuxBackend(Backend):
    name = "tmux"

    def __init__(self, socket: str | None = None) -> None:
        super().__init__()
        self.socket = socket or socket_name()
        self.tmux_name: str | None = None
        self.pane_id: str | None = None
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None

    def _run(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _check(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        proc = self._run(*args, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc

    def start(self, spec: SessionSpec) -> None:
        argv = spec.argv()
        cwd = spec.cwd or os.getcwd()
        name = self.session.session_id
        # The tmux server may predate this process; pass the session env
        # explicitly with `env VAR=val` so agents inherit it regardless of the
        # server's own environment (e.g. a usable TMPDIR).
        prefix = [
            f"{key}={value}"
            for key, value in (spec.env or {}).items()
            if key and value is not None
        ]
        command = shlex.join(argv)
        if prefix:
            # Secrets (e.g. the agent token) must not appear in `ps`: hand the
            # environment over through a private file that the launcher sources
            # and deletes before exec'ing the agent.
            fd, env_file = tempfile.mkstemp(prefix="at-env-", dir=_private_dir())
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for item in prefix:
                    key, _, value = item.partition("=")
                    fh.write(f"export {key}={shlex.quote(value)}\n")
            command = shlex.join(
                ["/bin/sh", "-c", '. "$0" && rm -f "$0" && exec "$@"', env_file, *argv]
            )

        self._check(
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            str(spec.cols),
            "-y",
            str(spec.rows),
            "-c",
            cwd,
            command,
            # Same tmux command queue as new-session: a command that exits at once
            # must leave a dead pane (with its exit status), not a vanished session.
            ";",
            "set-option",
            "-t",
            name,
            "remain-on-exit",
            "on",
        )
        self.tmux_name = name
        self._run("set-option", "-t", name, "@at_backend", "tmux")
        self._run("set-option", "-t", name, "@at_command", spec.display())
        self._run("set-option", "-t", name, "@at_created", str(self.session.created_at))
        self.pane_id = self._first_pane()

        self._reader = threading.Thread(
            target=self._read_loop, name=f"tmux-reader-{name}", daemon=True
        )
        self._reader.start()

    def adopt(self, name: str) -> None:
        self.tmux_name = name
        self.pane_id = self._first_pane()
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name=f"tmux-reader-{name}", daemon=True
        )
        self._reader.start()

    def _first_pane(self) -> str | None:
        proc = self._run(
            "list-panes", "-t", self.tmux_name, "-F", "#{pane_id}"
        )
        line = proc.stdout.strip().splitlines()
        return line[0] if line else None

    def _pane_pid(self) -> int | None:
        proc = self._run(
            "display-message", "-p", "-t", self.pane_id or self.tmux_name or "",
            "#{pane_pid}",
        )
        out = proc.stdout.strip()
        return int(out) if out.isdigit() else None

    def _read_loop(self) -> None:
        last = ""
        while not self._stop.is_set():
            text = self.capture()
            if text != last:
                delta = text[len(last):] if text.startswith(last) else text
                last = text
                if delta:
                    self.session._emit_output(delta)
            code = self.poll()
            if code is not None:
                if self.session is not None:
                    self.session._emit_exit(code)
                return
            time.sleep(0.2)

    def write(self, text: str) -> None:
        self._check("send-keys", "-t", self._target(), "-l", "--", text)

    def send_key(self, key: str) -> None:
        self._check("send-keys", "-t", self._target(), tmux_name(key))

    def _target(self) -> str:
        return self.pane_id or self.tmux_name or ""

    def capture(self) -> str:
        if not self.tmux_name:
            return ""
        proc = self._run("capture-pane", "-p", "-t", self._target(), "-S", "-")
        if proc.returncode != 0:
            return ""
        return proc.stdout

    def resize(self, cols: int, rows: int) -> None:
        if self.tmux_name:
            self._check(
                "resize-window", "-t", self.tmux_name, "-x", str(cols), "-y", str(rows)
            )

    def interrupt(self) -> None:
        self._check("send-keys", "-t", self._target(), "C-c")

    def terminate(self) -> None:
        if self.tmux_name:
            self._run("kill-session", "-t", self.tmux_name)

    def kill(self) -> None:
        if self.tmux_name:
            self._run("kill-session", "-t", self.tmux_name)

    def poll(self) -> int | None:
        if not self.tmux_name:
            return None
        if not self._exists():
            return 0
        proc = self._run(
            "display-message",
            "-p",
            "-t",
            self._target(),
            "#{pane_dead} #{pane_dead_status}",
        )
        if proc.returncode != 0:
            return None
        parts = proc.stdout.strip().split()
        if not parts:
            return None
        if parts[0] == "1":
            status = parts[1] if len(parts) > 1 else "0"
            try:
                return int(status)
            except ValueError:
                return 0
        return None

    def _exists(self) -> bool:
        if not self.tmux_name:
            return False
        proc = self._run("has-session", "-t", self.tmux_name)
        return proc.returncode == 0

    def pid(self) -> int | None:
        return self._pane_pid()

    def meta(self) -> dict:
        return {
            "tmux_session": self.tmux_name,
            "pane_id": self.pane_id,
            "socket": self.socket,
        }

    def close(self) -> None:
        self._stop.set()
        if self.tmux_name and self._exists():
            self._run("kill-session", "-t", self.tmux_name)

    @classmethod
    def attach_command(cls, tmux_name: str, socket: str | None = None) -> list[str]:
        return [
            "tmux", "-L", socket or socket_name(), "-f", "/dev/null",
            "attach-session", "-t", tmux_name,
        ]


def existing_sessions(socket: str | None = None) -> list[dict]:
    socket = socket or socket_name()
    if not tmux_available():
        return []
    proc = subprocess.run(
        ["tmux", "-L", socket, "-f", "/dev/null", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    out: list[dict] = []
    for name in proc.stdout.split():
        meta = {"session_id": name, "tmux_session": name}
        for key, option in (
            ("command", "@at_command"),
            ("created_at", "@at_created"),
            ("backend", "@at_backend"),
        ):
            opt = subprocess.run(
                ["tmux", "-L", socket, "-f", "/dev/null",
                 "show-options", "-t", name, "-v", option],
                capture_output=True,
                text=True,
            )
            if opt.returncode == 0:
                meta[key] = opt.stdout.strip()
        out.append(meta)
    return out
