"""Test package.

Tests run real daemons that adopt every session on the tmux server and close
them on shutdown. Give the whole run its own tmux server socket (inherited by
the daemons/CLI subprocesses) so it can never kill the user's live agents.
"""
import atexit
import glob
import os
import shutil
import signal
import subprocess
import tempfile
import time

# The socket the user's real daemon uses. Tests must never touch it.
PRODUCTION_SOCKET = "crewhall"

# Temporary namespace owned by the test suite (``tempfile.mkdtemp(prefix="at-…")``).
_TEMP_ROOTS = ["/tmp"]
_TEMP_PREFIXES = ("at-", "ati-")


def _resolve_test_socket() -> str:
    """Return the private socket for this run, never the production one.

    This is deliberately *not* guarded by import order: ``unittest discover``
    imports the ``crewhall`` package (which used to freeze the socket at
    import time) before it imports this package, so we also override an
    inherited production value.
    """
    current = os.environ.get("CREWHALL_TMUX_SOCKET") or os.environ.get("AGENT_TERMINAL_TMUX_SOCKET")
    if not current or current == PRODUCTION_SOCKET:
        current = f"at_test_{os.getpid()}"
    # Set both: the new name is preferred, the legacy one keeps older tests and
    # subprocesses that still read it working (no deprecation warning in tests).
    os.environ["CREWHALL_TMUX_SOCKET"] = current
    os.environ["CREWHALL_TMUX_SOCKET"] = current
    return current


def _snapshot_temp_dirs() -> set[str]:
    found: set[str] = set()
    for root in _TEMP_ROOTS:
        for prefix in _TEMP_PREFIXES:
            found.update(glob.glob(os.path.join(root, prefix + "*")))
    return found


_TEST_SOCKET = _resolve_test_socket()
_PREEXISTING: set[str] = _snapshot_temp_dirs()

# Tests run real daemons: they must never read or write the user's real state/config/runtime
# (a test daemon once rewrote the saved web-interface mode). Individual tests may still override.
if not os.environ.get("AT_TEST_ISOLATED"):
    _ROOT = tempfile.mkdtemp(prefix="at-tests-")
    os.environ.update({"AT_TEST_ISOLATED": "1", "XDG_STATE_HOME": os.path.join(_ROOT, "state"),
                       "XDG_CONFIG_HOME": os.path.join(_ROOT, "config"),
                       "XDG_RUNTIME_DIR": os.path.join(_ROOT, "run")})
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
else:
    _ROOT = None

# Real `claude` launches get hooks only where a test opts in.
os.environ.setdefault("CREWHALL_HOOKS", "0")
os.environ.setdefault("CREWHALL_CONVERSATIONS", "0")


def _is_test_artifact_cwd(cwd: str) -> bool:
    return any(cwd.startswith(os.path.join(root, prefix))
               for root in _TEMP_ROOTS for prefix in _TEMP_PREFIXES)


def _process_holds_test_artifact(pid: int) -> bool:
    try:
        if _is_test_artifact_cwd(os.readlink(f"/proc/{pid}/cwd")):
            return True
    except OSError:
        pass
    fddir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fddir)
    except OSError:
        return False
    for fd in fds:
        try:
            target = os.readlink(os.path.join(fddir, fd))
        except OSError:
            continue
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        if _is_test_artifact_cwd(target):
            return True
    return False


def _kill_orphan_test_processes() -> None:
    """Terminate processes left behind by tests that hold a test temp artifact.

    Only processes whose cwd or an open file is under ``/tmp/at-*`` / ``/tmp/ati-*``
    are touched: the user's real daemon and agents never live there (they use the
    XDG state dir), so this can never kill production work.
    """
    own = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == own:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        if not any(tag in cmdline for tag in ("python", "crewhall", "opencode", "claude", "tmux")):
            continue
        if _process_holds_test_artifact(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


def _sweep_new_temp_dirs(only: set[str] | None = None) -> list[str]:
    """Remove /tmp/at-* directories created during this run (never pre-existing).

    Keeps normal ``tempfile`` semantics (tests still write under /tmp, so
    ``clean`` and CLI subprocesses behave the same) while guaranteeing the run
    leaves no temporary directory behind (Fase 0, 0.48.0).
    """
    removed: list[str] = []
    for path in sorted(_snapshot_temp_dirs()):
        if path in _PREEXISTING:
            continue
        if only is not None and path not in only:
            continue
        for _ in range(3):
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path) or os.path.islink(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if not os.path.lexists(path):
                removed.append(path)
                break
            time.sleep(0.2)
    return removed


def _cleanup() -> None:
    try:
        subprocess.run(
            ["tmux", "-L", _TEST_SOCKET, "kill-server"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    try:
        os.unlink(os.path.join(base, f"tmux-{os.getuid()}", _TEST_SOCKET))
    except OSError:
        pass
    _kill_orphan_test_processes()
    time.sleep(0.3)
    _sweep_new_temp_dirs()


atexit.register(_cleanup)
