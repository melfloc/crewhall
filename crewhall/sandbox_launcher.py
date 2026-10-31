"""Standalone bubblewrap launcher for crewhall agents.

This file is copied verbatim by :mod:`crewhall.sandbox` to the daemon's state
directory and executed *outside* the sandbox by the tmux/pty backend. It has no
imports from the crewhall package: the agent may share the Unix uid with the
daemon, so the launcher must be verifiable and must not execute repo code.

The daemon passes one JSON document on argv[1]. It describes the agent command,
its workspace and the paths that may be exposed. Everything not listed there is
either read-only (the root bind) or masked by a private tmpfs.
"""
from __future__ import annotations

import json
import os
import sys

SENSITIVE_ENV = (
    "TMUX",
    "TMUX_PANE",
    "DOCKER_HOST",
    "SSH_AUTH_SOCK",
    "DBUS_SESSION_BUS_ADDRESS",
)


class LaunchError(RuntimeError):
    pass


def _inside(path: str, parent: str) -> bool:
    path = os.path.normpath(path)
    parent = os.path.normpath(parent)
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _parents(path: str) -> list[str]:
    out: list[str] = []
    current = os.path.dirname(os.path.normpath(path))
    while current and current != "/":
        out.append(current)
        current = os.path.dirname(current)
    return list(reversed(out))


def _add_dirs(args: list[str], destination: str) -> None:
    """Create the parent chain inside the sandbox (idempotent, safe on ro root)."""
    for parent in _parents(destination):
        args += ["--dir", parent]


def _exists(path: str | None) -> bool:
    return bool(path) and os.path.lexists(path)


def _mask(args: list[str], path: str | None) -> None:
    """Hide a file/dir behind an empty tmpfs, or a file behind /dev/null."""
    if not _exists(path):
        return
    assert path is not None
    if os.path.isdir(path) and not os.path.islink(path):
        _add_dirs(args, path)
        args += ["--tmpfs", path]
    else:
        _add_dirs(args, path)
        args += ["--ro-bind", "/dev/null", path]


def _bind(args: list[str], source: str, destination: str, *, readonly: bool = False) -> None:
    if not _exists(source):
        raise LaunchError(f"missing bind source: {source}")
    _add_dirs(args, destination)
    args += ["--ro-bind" if readonly else "--bind", source, destination]


def _tmpfs(args: list[str], path: str | None) -> None:
    if not _exists(path):
        return
    assert path is not None
    _add_dirs(args, path)
    args += ["--tmpfs", path]


def _unsafe_workspace(path: str, cfg: dict) -> bool:
    """Never give the whole filesystem, the whole home, or control dirs rw."""
    path = os.path.realpath(path)
    home = cfg.get("home") or ""
    for forbidden in (cfg.get("state_dir"), cfg.get("config_dir"), cfg.get("runtime_dir")):
        if forbidden and _inside(forbidden, path):
            return True
    for forbidden in (cfg.get("config_dir"), cfg.get("runtime_dir")):
        if forbidden and _inside(path, forbidden):
            return True
    return path in ("/", home, os.path.dirname(home))


def build_argv(cfg: dict) -> list[str]:
    bwrap = cfg["bwrap"]
    state = cfg.get("state_dir")
    config = cfg.get("config_dir")
    runtime = cfg.get("runtime_dir")
    socket = cfg.get("socket")
    workspace = cfg.get("workspace") or cfg.get("cwd") or "/"
    home = cfg.get("home") or ""

    args = [
        bwrap,
        "--ro-bind", "/", "/",
        "--tmpfs", "/tmp",
        "--dev", "/dev",
        "--proc", "/proc",
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup",
        "--die-with-parent", "--new-session", "--cap-drop", "ALL",
        "--hostname", "crewhall-" + str(cfg.get("agent_id") or "agent")[:8],
    ]
    # A private /dev/shm: POSIX shared memory is another cross-agent channel.
    _tmpfs(args, "/dev/shm")
    # Private temp filesystems, before any path below them is re-exposed.
    _tmpfs(args, "/var/tmp")
    _tmpfs(args, "/run/tmux")

    # crewhall's own state: masked, then only the files an agent legitimately
    # needs are mounted back (its token lives in the environment, never here).
    if state and os.path.isdir(state):
        _tmpfs(args, state)
        hooks_dir = os.path.join(state, "hooks")
        if os.path.isdir(hooks_dir):
            _bind(args, hooks_dir, hooks_dir, readonly=True)
        mcp_dir = os.path.join(state, "mcp")
        if os.path.isdir(mcp_dir):
            _bind(args, mcp_dir, mcp_dir, readonly=True)
        tmpdir = cfg.get("tmpdir")
        if tmpdir:
            _bind(args, tmpdir, tmpdir)
        opencode_state = cfg.get("opencode_state")
        if opencode_state and os.path.isdir(opencode_state):
            _bind(args, opencode_state, opencode_state)
        # Chat/worktree agents live under the state dir; expose only that cwd.
        if _inside(workspace, state):
            _bind(args, workspace, workspace)
        # The verified launcher is readable (never writable) for auditability.
        launcher = cfg.get("launcher")
        if launcher and os.path.exists(launcher):
            # /tmp is already a writable private tmpfs here (the root is ro).
            _bind(args, launcher, "/tmp/crewhall-sandbox-launcher.py",
                  readonly=True)

    # The provider config (settings.json) is control-plane data.
    _tmpfs(args, config)

    # Credentials and other agents' private material.
    for name in (".ssh", ".secrets", ".gnupg"):
        _mask(args, os.path.join(home, name) if home else None)
    tmux_tmpdir = cfg.get("tmux_tmpdir")
    if tmux_tmpdir:
        _tmpfs(args, tmux_tmpdir)

    # Docker/Podman control sockets must not be reachable from an agent.
    for path in cfg.get("container_sockets") or []:
        if _exists(path):
            _add_dirs(args, path)
            args += ["--ro-bind", "/dev/null", path]

    # Only the daemon socket from the runtime dir: not its log/pid/lock.
    if runtime and os.path.isdir(runtime):
        _tmpfs(args, runtime)
        # The daemon socket is the only runtime entry exposed. An in-process
        # Controller (tests, embedded UI) may not have a daemon listening; the
        # agent then simply has no control socket, which is not a boundary hole.
        if socket and os.path.exists(socket):
            _bind(args, socket, socket)

    # The workspace is the one user tree the agent may modify.
    if not _unsafe_workspace(workspace, cfg):
        _bind(args, workspace, workspace)
    else:
        raise LaunchError(f"refusing to bind {workspace!r} read-write")

    args += ["--chdir", cfg.get("cwd") or workspace]
    args += ["--", *cfg["argv"]]
    return args


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        raise SystemExit("usage: agent-sandbox-launcher.py '<json>'")
    try:
        cfg = json.loads(argv[0])
        if not isinstance(cfg, dict) or not isinstance(cfg.get("argv"), list):
            raise LaunchError("invalid launcher configuration")
        env = dict(os.environ)
        for key in SENSITIVE_ENV:
            env.pop(key, None)
        cache_dir = cfg.get("cache_dir")
        if cache_dir:
            # HOME is read-only inside the sandbox; give the CLI a writable,
            # per-agent cache that is not shared with its siblings.
            os.makedirs(cache_dir, mode=0o700, exist_ok=True)
            env["XDG_CACHE_HOME"] = cache_dir
        os.execvpe(cfg["bwrap"], build_argv(cfg), env)
    except (LaunchError, OSError, ValueError) as exc:
        print(f"crewhall sandbox: {exc}", file=sys.stderr)
        return 127
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
