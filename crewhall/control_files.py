"""Safe injection of crewhall's instructions into a project's control file.

Projects handed to crewhall may be important, so the rules are strict and
enforced (and verified after every write):

* The file is handled as raw bytes: no decoding, no newline translation, BOM and
  odd encodings are preserved untouched.
* First injection **appends at the very end** (``O_APPEND``): the original bytes
  are, by construction, an untouched prefix of the result.
* An existing managed block (exactly one BEGIN followed by one END) is refreshed
  in place; every byte outside it is preserved. Anything ambiguous (markers
  quoted in prose, lone/duplicated markers) makes us leave the file alone.
* The line-ending style of the file (LF/CRLF) is followed for the added block.
* Symlinks are followed to their target, which is edited in place (the link is
  never replaced); writes that replace content are atomic (temp + fsync + rename).
* A copy of the original is kept in the state dir before any modification, and
  after writing we re-read the file: if the invariant does not hold, the original
  is restored. Concurrent agents are serialised with a file lock.
* ``CREWHALL_CONTROL_FILES=off`` disables all of this.
"""
from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass

from . import brand, paths

log = logging.getLogger("crewhall.control_files")

# Write the new markers; the legacy pair is still recognised and refreshed
# (migrated in place) so files managed before the rename keep working.
BEGIN = brand.CONTROL_BEGIN
END = brand.CONTROL_END
_LEGACY_MARKERS = ((brand.LEGACY_CONTROL_BEGIN, brand.LEGACY_CONTROL_END),)
CONTROL_FILENAMES = ("CLAUDE.md", "AGENTS.md")
MAX_BYTES = 5 * 1024 * 1024
KEEP_BACKUPS = 5

# Which file each agent kind actually reads, in order of preference.
KIND_FILES = {
    "claude": ("CLAUDE.md",),
    "opencode": ("AGENTS.md", "CLAUDE.md"),
}


@dataclass
class ControlFileResult:
    path: str
    created: bool
    updated: bool
    unchanged: bool
    skipped: str | None = None  # why the file was deliberately left alone


def managed_section() -> str:
    return "\n".join(
        [
            BEGIN,
            "## Agent-terminal collaboration",
            "",
            "This project is being worked on inside crewhall.",
            "",
            "When you need to interact with another agent, use the **crewhall**",
            "command-line interface from your terminal:",
            "",
            "```",
            "crewhall agent identity",
            "crewhall agent list --team <team>",
            'crewhall message send --to <agent> --message "<message>"',
            "```",
            "",
            "Your sender identity is determined automatically by crewhall. Do not",
            "attempt to specify another sender identity.",
            "",
            "Before communicating with another agent:",
            "",
            "1. Check your identity and Teams.  (`crewhall agent identity`)",
            "2. Discover the available agents in your Team.  (`crewhall agent list --team <team>`)",
            "3. Send the message using crewhall.  (`crewhall message send ...`)",
            "",
            "Messages are routed through crewhall; there is no direct agent-to-agent",
            "channel. Communication is restricted to agents that share a Team. If the",
            "recipient is stopped, sending a message may activate it automatically.",
            "",
            "A successful delivery means crewhall accepted and delivered the message",
            "to the recipient's own terminal. It does not mean the recipient completed the",
            "requested work. Use direct, concise messages describing the requested action",
            "and the relevant context.",
            "",
            "Messages arrive typed into the recipient's terminal as `[from: <sender>] <text>`.",
            "The `[from: ...]` prefix is added by crewhall (do not write it yourself).",
            "When you expect an answer, say so and ask the recipient to reply with",
            "`crewhall message send --to <sender> ...`.",
            "",
            "If the recipient is busy or not ready to receive input, the command prints",
            "`queued` and exits 0: crewhall keeps retrying for a while and delivers",
            "the message as soon as the recipient is free. Do not send it again (that",
            "would duplicate it). Do not wait or poll for the answer either: finish your",
            "turn; the reply will arrive later as a new message in your own terminal.",
            "",
            "To give a teammate a clean conversation before a new task (Claude `/clear`,",
            "OpenCode `/new`), run `crewhall agent new-session <agent>`. Do not send",
            "`/clear` or `/new` as a message: messages are prefixed and would not run the",
            "command. It waits up to 20 s for the agent to be idle and is refused if it",
            "stays busy. It discards that agent's current context: only do it when its",
            "previous work is finished.",
            "",
            "Do not edit inside this managed section; edit the rest of the file freely.",
            END,
        ]
    )


class _Corrupt(Exception):
    pass


def enabled() -> bool:
    return brand.env("CONTROL_FILES", "append").lower() not in (
        "off", "0", "false", "no",
    )


def _newline(data: bytes) -> bytes:
    """Line-ending style of the file: CRLF only if it clearly dominates."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    return b"\r\n" if crlf and crlf >= lf else b"\n"


def _section_bytes(nl: bytes) -> bytes:
    return managed_section().replace("\n", nl.decode()).encode("utf-8")


def _locate(data: bytes, begin: bytes, end: bytes) -> tuple[int, int] | None:
    nb, ne = data.count(begin), data.count(end)
    if nb == 0 and ne == 0:
        return None
    if nb != 1 or ne != 1:
        raise _Corrupt("duplicated or unpaired markers")
    start, stop = data.index(begin), data.index(end)
    if stop < start:
        raise _Corrupt("END before BEGIN")
    # Markers must stand on their own line, otherwise they are quoted prose.
    line_start = data.rfind(b"\n", 0, start) + 1
    if data[line_start:start].strip():
        raise _Corrupt("BEGIN marker is inside a line of text")
    stop_end = stop + len(end)
    nl = data.find(b"\n", stop_end)
    tail = data[stop_end: nl if nl != -1 else len(data)]
    if tail.strip():
        raise _Corrupt("END marker is followed by text on its line")
    return start, stop_end


def _find_block(data: bytes) -> tuple[int, int] | None:
    """(start, end) of THE managed block (new markers, else a legacy pair)."""
    found = _locate(data, BEGIN.encode(), END.encode())
    if found is not None:
        return found
    for legacy_begin, legacy_end in _LEGACY_MARKERS:
        found = _locate(data, legacy_begin.encode(), legacy_end.encode())
        if found is not None:
            return found
    return None


def _lock_path(real: str) -> str:
    d = os.path.join(paths.state_dir(), "locks")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, hashlib.sha1(real.encode()).hexdigest() + ".lock")


def _backup(real: str, data: bytes) -> None:
    if not data:
        return
    key = hashlib.sha1(real.encode()).hexdigest()[:16]
    d = os.path.join(paths.state_dir(), "control-backups", key)
    os.makedirs(d, mode=0o700, exist_ok=True)
    with open(os.path.join(d, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.path.basename(real)}"), "wb") as fh:
        fh.write(data)
    olds = sorted(os.listdir(d))
    for name in olds[:-KEEP_BACKUPS]:
        try:
            os.unlink(os.path.join(d, name))
        except OSError:
            pass


def _atomic_replace(real: str, data: bytes) -> None:
    directory = os.path.dirname(real)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".at-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(real, tmp)
        try:
            st = os.stat(real)
            os.chown(tmp, st.st_uid, st.st_gid)
        except (PermissionError, OSError):
            pass
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pick(cwd: str, kind: str | None) -> tuple[str, bool]:
    """(path, exists) of the control file this kind of agent reads."""
    names = KIND_FILES.get(kind or "", CONTROL_FILENAMES)
    for name in names:
        path = os.path.join(cwd, name)
        if os.path.isfile(path):
            return path, True
    return os.path.join(cwd, names[0]), False


def ensure_managed_section(
    cwd: str, *, create: bool = True, kind: str | None = None
) -> ControlFileResult | None:
    """Ensure the managed block exists, touching nothing else. See module docs."""
    if not enabled() or not cwd or not os.path.isdir(cwd):
        return None
    path, exists = _pick(cwd, kind)
    real = os.path.realpath(path)
    if not exists and not create:
        return None
    try:
        with open(_lock_path(real), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _ensure_locked(path, real, exists)
    except OSError as exc:
        log.warning("control file %s left untouched: %s", path, exc)
        return None


def _skip(path: str, why: str) -> ControlFileResult:
    log.warning("control file %s left untouched: %s", path, why)
    return ControlFileResult(path, created=False, updated=False, unchanged=False, skipped=why)


def _ensure_locked(path: str, real: str, exists: bool) -> ControlFileResult | None:
    if not exists:
        if os.path.lexists(path):  # dangling symlink etc.: not ours to create through
            return _skip(path, "not a regular file")
        with open(path, "xb") as fh:
            fh.write(_section_bytes(b"\n") + b"\n")
        return ControlFileResult(path, created=True, updated=False, unchanged=False)

    with open(real, "rb") as fh:
        original = fh.read(MAX_BYTES + 1)
    if len(original) > MAX_BYTES:
        return _skip(path, "file too large")
    if b"\x00" in original:
        return _skip(path, "binary content")
    nl = _newline(original)
    section = _section_bytes(nl)
    try:
        block = _find_block(original)
    except _Corrupt as exc:
        return _skip(path, str(exc))

    if block is not None:
        start, stop = block
        if original[start:stop] == section:
            return ControlFileResult(path, created=False, updated=False, unchanged=True)
        updated = original[:start] + section + original[stop:]
        _backup(real, original)
        _atomic_replace(real, updated)
        with open(real, "rb") as fh:
            ok = fh.read() == updated
        if not ok or updated[:start] != original[:start] or updated[start + len(section):] != original[stop:]:
            _atomic_replace(real, original)
            return _skip(path, "post-write verification failed; original restored")
        return ControlFileResult(path, created=False, updated=True, unchanged=False)

    # First injection: strictly append at the end of the file.
    if original.endswith(nl) or not original:
        sep = nl if original else b""
    else:
        sep = nl + nl
    addition = sep + section + nl
    _backup(real, original)
    with open(real, "ab") as fh:  # O_APPEND: existing bytes are never rewritten
        fh.write(addition)
        fh.flush()
        os.fsync(fh.fileno())
    with open(real, "rb") as fh:
        after = fh.read()
    if after != original + addition:
        _atomic_replace(real, original)
        return _skip(path, "post-write verification failed; original restored")
    return ControlFileResult(path, created=False, updated=True, unchanged=False)
