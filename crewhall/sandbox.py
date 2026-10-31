"""OS sandbox for local coding agents.

The daemon and its agents share one Unix uid, so a daemon-side policy cannot
protect the control plane from an agent that can read files or reach the tmux
socket. This module wraps every local agent launch in bubblewrap:

* private mount, pid, ipc, uts and cgroup namespaces;
* read-only root, writable workspace and agent temp dir;
* the control-plane state/config dirs, tmux sockets, docker/podman sockets,
  ``~/.ssh``, ``~/.secrets`` and ``~/.gnupg`` masked;
* only the daemon socket exposed from the runtime dir;
* capabilities dropped and supplementary groups removed by bwrap.

The launcher is a standalone script copied into the state dir at daemon start.
Its hash is verified before every launch, so an agent cannot replace the code
that builds the namespace. There is deliberately **no** environment variable or
setting that disables the boundary; a missing bwrap is a hard failure.
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
from typing import Any

from . import paths

log = logging.getLogger("crewhall.sandbox")

LAUNCHER_NAME = "agent-sandbox-launcher.py"
_LAUNCHER: tuple[str, str] | None = None  # (state_dir, sha256)
_LOCK = threading.RLock()
_LOGGED: set[str] = set()

SENSITIVE_ENV = (
    "TMUX",
    "TMUX_PANE",
    "DOCKER_HOST",
    "SSH_AUTH_SOCK",
    "DBUS_SESSION_BUS_ADDRESS",
)

CONTAINER_SOCKETS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/run/podman/podman.sock",
    "/run/podman/podman.sock",
)


class SandboxError(RuntimeError):
    pass


def _bwrap() -> str:
    """An absolute, root-owned bwrap that other users cannot modify."""
    found = shutil.which("bwrap")
    if not found:
        raise SandboxError(
            "bubblewrap (bwrap) is required for agent isolation and was not found; "
            "install it (e.g. apt install bubblewrap) before creating agents"
        )
    path = os.path.realpath(found)
    try:
        st = os.stat(path)
    except OSError as exc:
        raise SandboxError(f"cannot stat bwrap at {path}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode) or not os.access(path, os.X_OK):
        raise SandboxError(f"bwrap at {path} is not an executable file")
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise SandboxError(
            f"refusing bwrap at {path}: it must be root-owned and not writable by others"
        )
    return path


def _launcher_dir() -> str:
    path = os.path.join(paths.state_dir(), "sandbox")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def launcher_path() -> str:
    return os.path.join(_launcher_dir(), LAUNCHER_NAME)


def prepare_launcher() -> str:
    """Copy the standalone launcher into the state dir and remember its hash.

    The copy is written atomically, made 0700 and never read from the workspace,
    so editing the repo cannot change the already-running daemon's boundary.
    """
    global _LAUNCHER
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox_launcher.py")
    try:
        with open(source, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise SandboxError(f"cannot read the sandbox launcher: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    path = launcher_path()
    with _LOCK:
        try:
            with open(path, "rb") as fh:
                current = fh.read()
        except OSError:
            current = None
        if current != data:
            fd, tmp = tempfile.mkstemp(prefix=".launcher-", dir=os.path.dirname(path))
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, 0o700)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        else:
            os.chmod(path, 0o700)
        _LAUNCHER = (paths.state_dir(), digest)
    return path


def _verify_launcher() -> str:
    state = paths.state_dir()
    with _LOCK:
        current = _LAUNCHER
    if current is None or current[0] != state:
        prepare_launcher()
        with _LOCK:
            current = _LAUNCHER
    expected = current[1] if current is not None else None
    path = launcher_path()
    try:
        with open(path, "rb") as fh:
            actual = hashlib.sha256(fh.read()).hexdigest()
        st = os.stat(path)
    except OSError as exc:
        raise SandboxError(f"the sandbox launcher disappeared: {exc}") from exc
    if actual != expected:
        raise SandboxError(
            "the sandbox launcher changed after the daemon started; refusing to launch an agent"
        )
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SandboxError("the sandbox launcher has unsafe ownership or permissions")
    return path


def sanitize_env(env: dict[str, str]) -> dict[str, str]:
    """Remove variables that point at the parent control plane or its sockets."""
    for key in SENSITIVE_ENV:
        env.pop(key, None)
    return env


def _log_once(reason: str) -> None:
    with _LOCK:
        if reason in _LOGGED:
            return
        _LOGGED.add(reason)
    log.error("sandbox configuration: %s", reason)


def decision_from(cfg: dict[str, Any], name: str | None) -> tuple[str | None, str | None]:
    """Pure form of :func:`decision_for` against an already-read config."""
    if not name:
        return None, None
    if not cfg["agents"] and not cfg["invalid"]:
        return None, None
    if cfg["poison_all"]:
        reason = "; ".join(cfg["poison_all"])
        _log_once(reason)
        return None, f"invalid sandbox configuration: {reason}"
    for entry in cfg["agents"]:
        if entry["regex"] is not None and entry["regex"].fullmatch(name):
            profile = entry["profile"]
            if profile not in cfg["profiles"]:
                detail = "; ".join(cfg["invalid"]) or f"unknown profile {profile!r}"
                reason = (f"agent {name!r} matches {entry['name_pattern']!r} but its "
                          f"sandbox profile is unusable: {detail}")
                _log_once(reason)
                return None, reason
            return profile, None
    if cfg["invalid"]:
        reason = "; ".join(cfg["invalid"])
        _log_once(reason)
        return None, f"invalid sandbox configuration: {reason}"
    return None, None


def decision_for(name: str | None) -> tuple[str | None, str | None]:
    """``(profile_name, None)``, ``(None, None)`` if unsandboxed, or an error.

    An agent is sandboxed only when its name fully matches an ``agents[]``
    pattern. Any invalid section fails closed: a malformed entry could have
    been meant for any name, so no local agent is launched until it is fixed.
    """
    from . import settings

    return decision_from(settings.sandbox_config(), name)


def profile_for(name: str | None) -> dict | None:
    """The validated sandbox profile for an agent name, or None when unsandboxed.

    Raises :class:`SandboxError` when the name is sandboxed but the config is
    invalid: callers never launch such an agent unsandboxed.
    """
    profile, error = decision_for(name)
    if error:
        raise SandboxError(error)
    if profile is None:
        return None
    from . import settings

    return settings.sandbox_config()["profiles"].get(profile)


def _expand_deny(entries: list[str]) -> list[str]:
    out: list[str] = []
    for entry in entries:
        if "*" in entry:
            out.extend(sorted(glob.glob(entry)))
        elif os.path.lexists(entry):
            out.append(entry)
    return out


def launch_values(name: str | None) -> dict[str, Any]:
    """What the launch path needs for ``name``: sandboxed flag and profile name.

    Raises :class:`SandboxError` for a name the (invalid) config cannot place:
    it is never launched unsandboxed *and* never launched sandboxed with a
    guessed profile.
    """
    from . import settings

    profile, error = decision_for(name)
    if error:
        raise SandboxError(error)
    if profile is None:
        return {"sandboxed": False, "sandbox_profile": None}
    return {"sandboxed": True,
            "sandbox_profile": settings.sandbox_config()["profiles"][profile]["name"]}


def _inside(path: str, parent: str) -> bool:
    path = os.path.normpath(path)
    parent = os.path.normpath(parent)
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _validate_workspace(workspace: str, home: str, state: str, config: str,
                        runtime: str) -> str:
    """Resolve the workspace and refuse trees that would collapse the boundary."""
    real = os.path.realpath(workspace)
    if real == "/" or real == home or real == os.path.dirname(home):
        raise SandboxError(f"refusing to bind {real!r} read-write for an agent")
    for forbidden in (state, config, runtime):
        if _inside(forbidden, real):
            raise SandboxError(f"workspace {real!r} contains control-plane data")
    for forbidden in (config, runtime):
        if _inside(real, forbidden):
            raise SandboxError(f"workspace {real!r} is inside {forbidden!r}")
    return real


def _agent_cache(tmpdir: str, agent_id: str) -> str:
    path = os.path.join(tmpdir, "cache", agent_id)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def command_for_spec(spec) -> list[str]:
    """The argv to execute for ``spec``: bwrap + the standalone launcher."""
    if not getattr(spec, "sandboxed", False):
        return spec.argv()
    agent_id = getattr(spec, "sandbox_agent_id", None) or getattr(spec, "session_id", "") or "agent"
    from . import settings

    name = getattr(spec, "name", None)
    profile = profile_for(name)
    if profile is None:
        raise SandboxError(
            f"{name or agent_id!r} is marked sandboxed but matches no sandbox profile")
    declared = getattr(spec, "sandbox_profile", None)
    if declared and declared != profile["name"]:
        raise SandboxError(
            f"{name!r} is sandboxed with profile {profile['name']!r}, not {declared!r}")
    bwrap = _bwrap()
    launcher = _verify_launcher()
    env = dict(spec.env or {})
    state = os.path.normpath(paths.state_dir())
    config = os.path.normpath(settings.config_dir())
    runtime = os.path.normpath(paths.runtime_dir())
    socket = os.path.normpath(paths.socket_path())
    home = os.path.normpath(os.path.expanduser("~"))
    workspace = os.path.normpath(spec.cwd or os.getcwd())
    if profile["workspace"] == "rw":
        workspace = _validate_workspace(
            spec.cwd or os.getcwd(), home, state, config, runtime)
    tmpdir = os.path.normpath(env.get("TMPDIR") or paths.tmpdir_root())
    os.makedirs(tmpdir, mode=0o700, exist_ok=True)
    os.chmod(tmpdir, 0o700)
    opencode_state = env.get("XDG_STATE_HOME")
    if opencode_state:
        opencode_state = os.path.normpath(opencode_state)
        if not os.path.isdir(opencode_state):
            opencode_state = None
    tmux_tmpdir = os.environ.get("TMUX_TMPDIR")
    if tmux_tmpdir:
        tmux_tmpdir = os.path.normpath(tmux_tmpdir)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    container_sockets: list[str] = []
    for candidate in (*CONTAINER_SOCKETS,
                      os.path.join(runtime, "docker.sock"),
                      os.path.join(runtime, "podman", "podman.sock"),
                      os.path.join(xdg_runtime, "docker.sock") if xdg_runtime else "",
                      os.path.join(xdg_runtime, "podman", "podman.sock") if xdg_runtime else "",
                      os.path.join(home, ".docker", "run", "docker.sock"),
                      os.path.join(xdg_runtime, "bus") if xdg_runtime else "",
                      os.path.join(xdg_runtime, "systemd", "private") if xdg_runtime else ""):
        if not candidate:
            continue
        # /var/run is often a symlink to /run; bwrap cannot bind under a
        # symlinked parent, so mask the resolved path instead.
        resolved = os.path.realpath(candidate)
        if resolved not in container_sockets:
            container_sockets.append(resolved)
    cfg = {
        "argv": spec.argv(),
        "cwd": workspace,
        "workspace": workspace,
        "agent_id": agent_id,
        "state_dir": state,
        "config_dir": config,
        "runtime_dir": runtime,
        "socket": socket,
        "tmpdir": tmpdir,
        "cache_dir": _agent_cache(tmpdir, agent_id),
        "opencode_state": opencode_state,
        "home": home,
        "tmux_tmpdir": tmux_tmpdir,
        "container_sockets": container_sockets,
        "bwrap": bwrap,
        "launcher": launcher,
        # Stage 2: per-role binds/env/network/denies. The launcher re-checks
        # every destination; the daemon never lets a profile replace the base
        # masks (state/config/runtime/tmux/docker/credentials).
        "profile": {**profile, "deny": _expand_deny(profile["deny"])},
    }
    # Keep the JSON compact: it travels through tmux's command queue and must
    # never approach the argv limit. It contains paths, never secrets.
    payload = json.dumps(cfg, separators=(",", ":"), sort_keys=True)
    return [sys.executable, "-P", launcher, payload]
