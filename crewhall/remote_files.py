"""Read-only access to a remote agent's Claude transcript over SSH.

The transcript is the only remote file crewhall reads.  The session id must be a
UUID (it is also passed as a positional argument, never interpolated), symlinks
are refused, and at most ``MAX_BYTES`` of the file's tail travel back.  Results
are cached briefly; the polled path (``snapshot_lines``) never blocks on the
network: it returns the last known tail and refreshes in the background, so a
host that is down cannot stall the agent listing.
"""
from __future__ import annotations

import logging
import threading
import time

from .backends.ssh_tmux import SshTmuxBackend
from .transcripts import _UUID

log = logging.getLogger("crewhall.remote_files")

MAX_BYTES = 8 * 1024 * 1024
TTL = 3.0
STALE_AFTER = 120.0  # past this, a cached tail is no longer shown as current

# First line printed is the file size, then the tail.
_SCRIPT = (
    'd="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"; '
    'for f in "$d"/*/"$1".jsonl; do '
    '[ -f "$f" ] && [ ! -L "$f" ] && { wc -c <"$f"; tail -c "$2" "$f"; exit 0; }; done; exit 9'
)

_cache: dict[tuple[str, str], tuple[float, list[str], bool]] = {}
_inflight: set[tuple[str, str]] = set()
_lock = threading.Lock()


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def fetch(host: dict, session_id: str, timeout: float = 15.0) -> tuple[list[str], bool] | None:
    """(lines, truncated) of the remote transcript, or None if unavailable."""
    if not _UUID.match(session_id or ""):
        return None
    cmd = f"sh -c {_shell_quote(_SCRIPT)} sh {_shell_quote(session_id)} {MAX_BYTES}"
    proc = SshTmuxBackend(host)._ssh_run(cmd, timeout=timeout)
    if proc.returncode != 0:
        return None
    head, _, body = proc.stdout.partition("\n")
    try:
        size = int(head.strip())
    except ValueError:
        return None
    lines = body.splitlines()
    truncated = size > MAX_BYTES
    if truncated and lines:
        lines = lines[1:]  # the first line of a tail is usually cut in half
    return lines, truncated


def lines_cached(host: dict, session_id: str) -> tuple[list[str], bool] | None:
    """Synchronous (explicit user request) with a short cache."""
    key = (host.get("name") or host["ssh"], session_id)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1], hit[2]
    got = fetch(host, session_id)
    if got is None:
        return (hit[1], hit[2]) if hit and now - hit[0] < STALE_AFTER else None
    with _lock:
        if len(_cache) > 32:
            _cache.clear()
        _cache[key] = (time.monotonic(), got[0], got[1])
    return got


def snapshot_lines(host: dict, session_id: str) -> list[str] | None:
    """Last known tail, never blocking; refreshes in the background when stale."""
    if not _UUID.match(session_id or ""):
        return None
    key = (host.get("name") or host["ssh"], session_id)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        stale = not hit or now - hit[0] >= TTL
        start = stale and key not in _inflight
        if start:
            _inflight.add(key)
    if start:
        threading.Thread(
            target=_refresh, args=(host, session_id, key), name="remote-transcript", daemon=True
        ).start()
    if hit and now - hit[0] < STALE_AFTER:
        return hit[1]
    return None


def _refresh(host: dict, session_id: str, key: tuple[str, str]) -> None:
    try:
        got = fetch(host, session_id)
        if got is not None:
            with _lock:
                if len(_cache) > 32:
                    _cache.clear()
                _cache[key] = (time.monotonic(), got[0], got[1])
    except Exception:  # noqa: BLE001
        log.exception("remote transcript refresh failed")
    finally:
        with _lock:
            _inflight.discard(key)
