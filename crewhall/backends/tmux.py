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

    return brand.env("TMUX_SOCKET", DEFAULT_SOCKET) or DEFAULT_SOCKET


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

    # Large prompts are pasted in chunks: tmux's `send-keys -l` fails with
    # "command too long" and would also put the prompt in the argument list.
    PASTE_CHUNK = 8192
    # The reader only feeds the bounded ``read`` buffer. Capturing the whole
    # scrollback (`-S -`) every 200 ms made the delta re-emit thousands of lines
    # once tmux history filled; a short tail is enough for interactive reads and
    # keeps both CPU and the emitted deltas bounded. Full-screen state detection
    # still uses ``capture()``.
    READ_TAIL_LINES = 200

    def _run(self, *args: str, timeout: float = 10.0,
             input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )

    def _check(self, *args: str, timeout: float = 10.0,
               input_text: str | None = None) -> subprocess.CompletedProcess:
        proc = self._run(*args, timeout=timeout, input_text=input_text)
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc

    def start(self, spec: SessionSpec) -> None:
        from ..terminals import is_terminal

        argv = spec.argv()
        # The tmux server may have an older, narrower PATH than this process
        # (see paths.extend_provider_path): launch by absolute path.
        if argv and "/" not in argv[0] and (found := shutil.which(argv[0])):
            argv = [found, *argv[1:]]
        terminal = is_terminal(spec)
        cwd = spec.cwd or os.getcwd()
        assert self.session is not None
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

        new_args = [
            "new-session", "-d", "-s", name, "-x", str(spec.cols), "-y", str(spec.rows),
        ]
        if cwd:
            new_args += ["-c", cwd]
        # A terminal with no start command uses tmux's own login shell (or the
        # chosen shell, passed as a single argv element).
        if not (terminal and not argv):
            new_args.append(command)
        # Same tmux command queue as new-session: a command that exits at once
        # must leave a dead pane (with its exit status), not a vanished session.
        new_args += [";", "set-option", "-t", name, "remain-on-exit", "on"]
        if terminal:
            # The size is fixed by `resize-window`, not by any attaching client.
            new_args += [";", "set-option", "-t", name, "window-size", "manual"]
        self._check(*new_args)
        self.tmux_name = name
        self._run("set-option", "-t", name, "@at_backend", "tmux")
        self._run("set-option", "-t", name, "@at_command", spec.display())
        self._run("set-option", "-t", name, "@at_created", str(self.session.created_at))
        self._set_terminal_labels(spec, name)
        self.pane_id = self._first_pane()

        self._reader = threading.Thread(
            target=self._read_loop, name=f"tmux-reader-{name}", daemon=True
        )
        self._reader.start()

    def _set_terminal_labels(self, spec: SessionSpec, name: str) -> None:
        from ..terminals import is_terminal

        if not is_terminal(spec):
            return
        # Every value is passed as an argv element; never interpolated.
        self._run("set-option", "-t", name, "@at_kind", "terminal")
        self._run("set-option", "-t", name, "@at_readonly", "1" if spec.readonly else "0")
        if spec.title:
            self._run("set-option", "-t", name, "@at_title", spec.title)
        if spec.owner:
            self._run("set-option", "-t", name, "@at_owner", spec.owner)

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
            "list-panes", "-t", self.tmux_name or "", "-F", "#{pane_id}"
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

    @staticmethod
    def _delta(previous: str, current: str) -> str:
        """New text between two sliding-window captures.

        ``capture-pane -S -N`` returns the last N lines, so after a new line the
        window slides: the common part is a *suffix of ``previous``* and a
        *prefix of ``current``*. Returning ``current`` whenever the prefix check
        fails re-emits the whole pane on every scroll; this finds the small
        overlap (usually one line) and returns only what was appended.
        """
        if current == previous or not current:
            return ""
        if not previous:
            return current
        if current.startswith(previous):
            return current[len(previous):]
        prev_lines = previous.splitlines(keepends=True)
        cur_lines = current.splitlines(keepends=True)
        # Scrolling usually drops one line, but a redraw can drop several.
        max_drop = min(len(prev_lines) - 1, 32)
        for drop in range(1, max_drop + 1):
            common = len(prev_lines) - drop
            if len(cur_lines) < common or cur_lines[0] != prev_lines[drop]:
                continue
            if cur_lines[:common] == prev_lines[drop:]:
                return "".join(cur_lines[common:])
        # No overlap (clear/redraw): the pane itself is the only reliable delta.
        return current

    def _capture_tail(self) -> str:
        if not self.tmux_name:
            return ""
        # Keep the whole visible pane even for a tall terminal, plus some history.
        rows = int(getattr(self.session, "rows", 0) or 0) if self.session is not None else 0
        lines = max(self.READ_TAIL_LINES, rows + 50)
        proc = self._run("capture-pane", "-p", "-t", self._target(), "-S", f"-{lines}")
        if proc.returncode != 0:
            return ""
        return proc.stdout

    def _read_loop(self) -> None:
        last = ""
        while not self._stop.is_set():
            text = self._capture_tail()
            if text != last:
                delta = self._delta(last, text)
                last = text
                if delta and self.session is not None:
                    self.session._emit_output(delta)
            code = self.poll()
            if code is not None:
                if self.session is not None:
                    self.session._emit_exit(code)
                return
            time.sleep(0.2)

    def write(self, text: str) -> None:
        """Paste ``text`` through a private buffer, in bracketed-paste chunks.

        `send-keys -l` puts the whole prompt in tmux's argv (``command too
        long`` for long prompts, and the text leaks into error logs) and does not
        mark newlines as a paste. `load-buffer -` reads it from stdin instead and
        `paste-buffer -p` tells the TUI it is one paste, so multiline prompts do
        not submit line by line.
        """
        if not text:
            return
        # OpenCode's v2 TUI consumes typed keys but ignores a tmux paste event;
        # short single-line input therefore keeps the historical send-keys path.
        # Long or multiline prompts use a private buffer + bracketed paste, which
        # also avoids tmux's "command too long" and keeps the text out of argv.
        if "\n" not in text and len(text) <= max(1, int(self.PASTE_CHUNK)):
            try:
                self._check("send-keys", "-t", self._target(), "-l", "--", text)
            except RuntimeError:
                import hashlib

                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                raise RuntimeError(
                    f"tmux send-keys failed for a {len(text)}-char prompt "
                    f"(sha256:{digest})") from None
            return
        buffer = f"crewhall-{os.getpid()}-{threading.get_ident()}"
        size = max(1, int(self.PASTE_CHUNK))
        chunks = [text[i:i + size] for i in range(0, len(text), size)] or [text]
        for index, chunk in enumerate(chunks):
            try:
                self._check("load-buffer", "-b", buffer, "-", input_text=chunk)
                args = ["paste-buffer", "-p", "-b", buffer]
                if index == len(chunks) - 1:
                    args.append("-d")  # delete the buffer after the last chunk
                args += ["-t", self._target()]
                self._check(*args)
            except RuntimeError:
                import hashlib

                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                raise RuntimeError(
                    f"tmux paste failed for a {len(text)}-char prompt "
                    f"(sha256:{digest})") from None

    def send_key(self, key: str) -> None:
        self._check("send-keys", "-t", self._target(), tmux_name(key))

    def _target(self) -> str:
        return self.pane_id or self.tmux_name or ""

    def capture(self, escapes: bool = False) -> str:
        if not self.tmux_name:
            return ""
        args = ["capture-pane", "-p", "-t", self._target(), "-S", "-"]
        if escapes:
            args.append("-e")
        proc = self._run(*args)
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
            ("kind", "@at_kind"),
            ("readonly", "@at_readonly"),
            ("title", "@at_title"),
            ("owner", "@at_owner"),
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
