from __future__ import annotations

import os

from . import brand


def _makedirs(path: str) -> None:
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except FileExistsError:
        pass


def _dir_writable(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    probe = os.path.join(path, f".probe-{os.getpid()}")
    try:
        with open(probe, "wb") as fh:
            fh.write(b"x" * 4096)
        os.unlink(probe)
        return True
    except OSError:
        return False


def _has_space(path: str, need: int = 256 * 1024 * 1024) -> bool:
    import shutil

    try:
        return shutil.disk_usage(path).free >= need
    except OSError:
        return False


def _home_cache() -> str:
    return os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")


def _fallback_runtime_dir() -> str:
    path = os.path.join(_home_cache(), f"{brand.runtime_name()}-runtime", brand.runtime_name())
    _makedirs(path)
    return path


def _tighten_private(path: str) -> None:
    """Keep a per-user base directory private.

    ``os.makedirs`` applies ``mode`` only to the leaf, so the ``/tmp/crewhall-<uid>``
    fallback base can be born 0755. Other parts of crewhall (the remote gateway
    preparation) require it to be 0700, so we fix it here, but only for a real
    directory we own under ``/tmp`` (never a symlink or someone else's dir).
    """
    try:
        if path.startswith("/tmp") and os.path.isdir(path) and not os.path.islink(path) \
                and os.stat(path).st_uid == os.getuid():
            os.chmod(path, 0o700)
    except OSError:
        pass


def usable_tmpdir() -> str | None:
    """Return the dedicated TMPDIR that agent-managed processes must use.

    Agent CLIs (Bun/OpenCode) unpack a large native library (~14 MB) into
    ``TMPDIR`` on every start and never remove it, so a shared temp filesystem
    (``/tmp`` tmpfs with a per-user quota) can be exhausted. To contain this,
    crewhall always gives its agents a *dedicated* directory under the
    user's state home, which it owns and prunes. An explicit user ``TMPDIR`` is
    still respected (we return None to mean "leave the environment alone").
    """
    explicit = os.environ.get("TMPDIR")
    # A relative TMPDIR would scatter temp dirs inside whatever cwd an agent
    # has (observed as random-named dirs in project roots): ignore it.
    if explicit and os.path.isabs(explicit) and os.path.isdir(explicit):
        return None
    dedicated = tmpdir_root()
    _makedirs(dedicated)
    if _dir_writable(dedicated) and _has_space(dedicated, need=64 * 1024 * 1024):
        return dedicated
    # Last resort: home cache (same filesystem as state home in practice).
    fallback = os.path.join(_home_cache(), f"{brand.runtime_name()}-tmp")
    _makedirs(fallback)
    return fallback


def tmpdir_root() -> str:
    """Dedicated, crewhall-owned temp directory for agent processes."""
    return os.path.join(state_dir(), "tmp")


def cleanup_tmpdir(max_age_seconds: int = 3600, max_bytes: int = 512 * 1024 * 1024) -> int:
    """Delete stale files from the dedicated tmpdir; cap total size.

    Bun/OpenCode leave one ~14 MB ``.so`` per start. We remove files older than
    ``max_age_seconds`` and, if the directory still exceeds ``max_bytes``,
    remove the oldest files until it fits. Returns the number of files removed.
    Never touches anything outside the dedicated directory.
    """
    import glob
    import time

    root = tmpdir_root()
    if not os.path.isdir(root):
        return 0
    removed = 0
    now = time.time()
    entries: list[tuple[float, int, str]] = []
    for path in glob.glob(os.path.join(root, "*")) + glob.glob(os.path.join(root, ".*")):
        if not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if now - st.st_mtime > max_age_seconds:
            try:
                os.unlink(path)
                removed += 1
                continue
            except OSError:
                pass
        entries.append((st.st_mtime, st.st_size, path))
    total = sum(size for _m, size, _p in entries)
    if total > max_bytes:
        for _mtime, size, path in sorted(entries):
            if total <= max_bytes:
                break
            try:
                os.unlink(path)
                total -= size
                removed += 1
            except OSError:
                continue
    return removed


_RUNTIME_CHOICE: dict[str, str] = {}


def runtime_dir() -> str:
    """Resolve a writable runtime directory, choosing once per process.

    The choice must be stable: concurrent clients of the same process must all
    agree on one socket, otherwise they spawn competing daemons. We therefore
    cache the decision after the first successful resolution.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{brand.runtime_name()}-{os.getuid()}"
    cached = _RUNTIME_CHOICE.get(base)
    if cached is not None:
        return cached
    if base.startswith("/tmp") and not _dir_writable(base):
        # Default temp filesystem is unusable (quota-exhausted tmpfs).
        chosen = _fallback_runtime_dir()
    else:
        path = os.path.join(base, brand.runtime_name())
        try:
            _makedirs(path)
            _tighten_private(base)
            os.chmod(path, 0o700)
            chosen = path
        except OSError:
            chosen = _fallback_runtime_dir()
    _RUNTIME_CHOICE[base] = chosen
    return chosen


def socket_path() -> str:
    return os.path.join(runtime_dir(), "daemon.sock")


def pid_path() -> str:
    return os.path.join(runtime_dir(), "daemon.pid")


def lock_path() -> str:
    """Single-instance lock held by the daemon for its whole lifetime."""
    return os.path.join(runtime_dir(), "daemon.lock")


def spawn_lock_path() -> str:
    """Short-lived lock serialising daemon spawning across clients."""
    return os.path.join(runtime_dir(), "spawn.lock")


def log_path() -> str:
    return os.path.join(runtime_dir(), "daemon.log")


def state_dir() -> str:
    """Deterministic, cwd-independent location for persistent control-plane state.

    Unlike the runtime dir (which may live on a tmpfs and disappear on reboot),
    this is under the user's data home so Teams/agents survive UI restarts.
    """
    path = brand.state_root_dir()
    _makedirs(path)
    return path


def state_path() -> str:
    return os.path.join(state_dir(), "state.json")


def project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
