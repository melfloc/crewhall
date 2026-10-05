"""Run agents in a tmux session on a *remote* machine reached over SSH.

This backend reuses every ``TmuxBackend`` operation; only ``_run`` is
intercepted so each tmux command is executed as a single, fully quoted remote
command::

    ssh <fixed options> -- <destination> 'tmux -L <socket> -f /dev/null ...'

The SSH options are hardcoded: the configured host cannot change host-key
checking, enable agent/X11 forwarding or inject arbitrary options.  The
destination, the remote working directory, the tmux socket and every tmux
argument are passed as ``argv`` elements (never through a local shell), so a
hostile value cannot execute anything extra on either side.

A remote *agent* needs its environment (identity token, hooks, …) but that
environment must never appear in ``argv`` (visible in ``ps``).  ``start``
therefore feeds the variables over the SSH stdin to a tiny remote ``sh`` that
writes them to a 0600 temp file, sources it and deletes it before ``exec``.
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from .. import paths
from .tmux import TmuxBackend

if TYPE_CHECKING:
    from ..types import SessionSpec


class HostUnreachable(RuntimeError):
    """The SSH transport failed (or host-key verification did); not a tmux error."""


# Fixed by design; kept as a flat list for the tests to assert on.
SSH_BASE_OPTIONS = (
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "ClearAllForwardings=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=600",
)

_CONTROL_SUBDIR = "ssh"


def control_dir() -> str:
    """0700 directory that owns the SSH control sockets (verified, no symlinks)."""
    path = os.path.join(paths.runtime_dir(), _CONTROL_SUBDIR)
    if os.path.islink(path):
        raise RuntimeError(f"refusing symlinked SSH control directory: {path}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"not a directory: {path}")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"SSH control directory is not owned by us: {path}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def _check_control_path(path: str) -> None:
    """Refuse a ControlPath that leaves our private 0700 directory."""
    base = os.path.realpath(control_dir())
    parent = os.path.realpath(os.path.dirname(path))
    if parent != base:
        raise RuntimeError(f"ControlPath outside the private directory: {path}")


def _remote_session_name(alias: str | None, session_id: str) -> str:
    """Namespace a remote tmux session so it can never clash with a local one."""
    if alias:
        return f"{alias}__{session_id}"
    return session_id


def session_id_from_remote_name(alias: str | None, name: str) -> str:
    prefix = f"{alias}__" if alias else ""
    if prefix and name.startswith(prefix):
        return name[len(prefix):]
    return name


class SshTmuxBackend(TmuxBackend):
    name = "ssh-tmux"

    def __init__(self, host: dict[str, Any], socket: str | None = None) -> None:
        self.host_alias = host.get("name")
        self.destination = host["ssh"]
        self.port = int(host.get("port") or 22)
        self.identity = host.get("identity")
        self.known_hosts = host.get("known_hosts")
        remote_socket = host.get("tmux_socket") or "crewhall"
        super().__init__(socket=remote_socket)
        self.remote_socket = remote_socket
        self._unreachable = False

    # ---------------------------------------------------------------- transport
    def _ssh_local_argv(self, *, allocate_tty: bool = False) -> list[str]:
        argv = ["ssh"]
        if allocate_tty:
            argv.append("-t")
        argv += list(SSH_BASE_OPTIONS)
        control_path = os.path.join(control_dir(), "%C")
        _check_control_path(control_path)
        argv += ["-o", f"ControlPath={control_path}"]
        if self.known_hosts:
            # A pinned known_hosts file: host-key checking remains strict.
            argv += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        if self.identity:
            argv += ["-i", self.identity]
        if self.port != 22:
            argv += ["-p", str(self.port)]
        # ``--`` ends local options: a destination can never be parsed as one.
        argv += ["--", self.destination]
        return argv

    def _ssh_run(
        self, remote_command: str, *, input: str | None = None, timeout: float = 10.0
    ) -> subprocess.CompletedProcess:
        argv = [*self._ssh_local_argv(), remote_command]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                input=input,
            )
        except subprocess.TimeoutExpired:
            self._unreachable = True
            # Mirror ssh's own "connection failed" status so callers treat it
            # uniformly as the host being unreachable.
            return subprocess.CompletedProcess(argv, 255, "", "ssh: connection timed out")
        self._unreachable = proc.returncode == 255
        return proc

    def _run(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        remote = shlex.join(["tmux", "-L", self.remote_socket, "-f", "/dev/null", *args])
        return self._ssh_run(remote, timeout=timeout)

    def _check(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        proc = self._run(*args, timeout=timeout)
        if proc.returncode != 0:
            if proc.returncode == 255:
                raise HostUnreachable(
                    f"host {self.host_alias or self.destination} unreachable"
                )
            raise RuntimeError(
                f"tmux {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc

    # ------------------------------------------------------------------- launch
    def start(self, spec: SessionSpec) -> None:
        argv = spec.argv()
        cwd = spec.cwd  # a *remote* path: never substitute the local cwd
        name = _remote_session_name(self.host_alias, self.session.session_id)
        env_items = [
            (key, value)
            for key, value in (spec.env or {}).items()
            if key and value is not None
        ]
        head = ["new-session", "-d", "-s", name, "-x", str(spec.cols), "-y", str(spec.rows)]
        if cwd:
            head += ["-c", cwd]
        tail = [";", "set-option", "-t", name, "remain-on-exit", "on"]
        tmux = ["tmux", "-L", self.remote_socket, "-f", "/dev/null"]
        if env_items:
            # Secrets travel over stdin into a 0600 remote temp file. The *pane*
            # command (not this ssh client) sources and removes it before the
            # agent execs, so the env also reaches panes created on an already
            # running tmux server, and never appears in argv/ps.
            env_data = "".join(
                f"export {key}={shlex.quote(value)}\n" for key, value in env_items
            )
            remote = (
                'umask 077; f=$(mktemp "${TMPDIR:-/tmp}/at-env-XXXXXX"); cat >"$f"; '
                'c="/bin/sh -c \'. \\"\\$0\\" && rm -f \\"\\$0\\" && exec \\"\\$@\\"\' $f "'
                + shlex.quote(shlex.join(argv))
                + "; exec "
                + shlex.join([*tmux, *head])
                + ' "$c" '
                + shlex.join(tail)
            )
            proc = self._ssh_run(remote, input=env_data)
        else:
            proc = self._run(*head, shlex.join(argv), *tail)
        if proc.returncode != 0:
            if proc.returncode == 255:
                raise HostUnreachable(
                    f"host {self.host_alias or self.destination} unreachable"
                )
            raise RuntimeError(f"tmux new-session failed: {proc.stderr.strip()}")

        self.tmux_name = name
        self._run("set-option", "-t", name, "@at_backend", "tmux")
        self._run("set-option", "-t", name, "@at_command", spec.display())
        self._run("set-option", "-t", name, "@at_created", str(self.session.created_at))
        self.pane_id = self._first_pane()
        self._reader = threading.Thread(
            target=self._read_loop, name=f"ssh-tmux-reader-{name}", daemon=True
        )
        self._reader.start()

    def adopt(self, name: str) -> None:
        self.tmux_name = _remote_session_name(self.host_alias, name)
        self.pane_id = self._first_pane()
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name=f"ssh-tmux-reader-{self.tmux_name}", daemon=True
        )
        self._reader.start()

    # -------------------------------------------------------------------- state
    def poll(self) -> int | None:
        if not self.tmux_name:
            return None
        proc = self._run("has-session", "-t", self.tmux_name)
        if proc.returncode == 255:
            return None  # unknown: host down, never "exited"
        if proc.returncode != 0:
            return 0
        proc = self._run(
            "display-message", "-p", "-t", self._target(),
            "#{pane_dead} #{pane_dead_status}",
        )
        if proc.returncode == 255:
            return None
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

    def pid(self) -> int | None:
        # A *remote* pid must never be mistaken for a local one: the process
        # inspector reads /proc and can signal.  Remote processes are n/d.
        return None

    def meta(self) -> dict:
        return {
            "tmux_session": self.tmux_name,
            "pane_id": self.pane_id,
            "socket": self.remote_socket,
            "host": self.host_alias,
            "host_unreachable": self._unreachable,
        }

    # ------------------------------------------------------------------- attach
    @classmethod
    def attach_command(cls, host: dict[str, Any], tmux_name: str) -> list[str]:
        inst = cls(host)
        return [
            *inst._ssh_local_argv(allocate_tty=True),
            "tmux", "-L", inst.remote_socket, "-f", "/dev/null",
            "attach-session", "-t", tmux_name,
        ]


def existing_sessions(host: dict[str, Any]) -> list[dict]:
    """Adoptable remote tmux sessions on ``host`` (raises HostUnreachable)."""
    backend = SshTmuxBackend(host)
    alias = host.get("name")
    proc = backend._run("list-sessions", "-F", "#{session_name}")
    if proc.returncode == 255:
        raise HostUnreachable(f"host {alias or host['ssh']} unreachable")
    if proc.returncode != 0:
        return []
    out: list[dict] = []
    for name in proc.stdout.split():
        meta = {
            "session_id": session_id_from_remote_name(alias, name),
            "tmux_session": name,
            "host": alias,
        }
        for key, option in (
            ("command", "@at_command"),
            ("created_at", "@at_created"),
            ("backend", "@at_backend"),
        ):
            opt = backend._run("show-options", "-t", name, "-v", option)
            if opt.returncode == 0:
                meta[key] = opt.stdout.strip()
        out.append(meta)
    return out
