"""Files uploaded from the Web UI and handed to an agent as an absolute path.

The Web UI accepts an upload and this module stores it on the server (the
server is the proxy): either in the dedicated temp directory, where the janitor
prunes it, or in a permanent directory the user configures. The agent only ever
receives the path; it reads the file with its own tools, so images, PDFs, source
files and archives are all just a path away.

Safety: the client filename is reduced to a basename, control characters are
dropped, the stored name is made unique, and the file is written 0600 with
``O_EXCL`` inside a directory this module owns. A file never executes.
"""
from __future__ import annotations

import os
import re
import secrets
import time

from . import paths, settings

# Filenames longer than this (including the unique suffix) are truncated.
MAX_NAME = 120
_BAD = re.compile(r"[\x00-\x1f\x7f/\\]")
# An extension longer than this is not one: drop it instead of keeping noise.
MAX_EXT = 16


class UploadError(RuntimeError):
    pass


def _clean_name(name: str) -> str:
    """A safe basename: no directories, no control characters, a kept extension."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _BAD.sub("", base)
    base = base.strip(". ") or "file"
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem or len(ext) > MAX_EXT or not ext.isalnum():
        stem, ext = base, ""
    stem = stem[:MAX_NAME] or "file"
    return f"{stem}.{ext}" if ext else stem


def _unique(directory: str, name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for _ in range(5):
        candidate = os.path.join(directory, f"{stem}-{secrets.token_hex(4)}{'.' + ext if ext else ''}")
        if not os.path.exists(candidate):
            return candidate
    raise UploadError("could not find a free name")


def _temp_dir() -> str:
    directory = os.path.join(paths.tmpdir_root(), "uploads")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory


def _permanent_dir(create: bool = True) -> str:
    raw = str(settings.get("uploads.dir") or "").strip()
    if not raw:
        raise UploadError("no upload directory configured")
    directory = os.path.expanduser(raw)
    if not os.path.isabs(directory):
        raise UploadError("the upload directory must be absolute")
    if create:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory


def mode() -> str:
    value = str(settings.get("uploads.mode") or "temp").lower()
    return value if value in ("temp", "permanent") else "temp"


def max_bytes() -> int:
    try:
        return max(1, int(settings.get("uploads.max_mb"))) * 1024 * 1024
    except (TypeError, ValueError):
        return 25 * 1024 * 1024


def store(name: str, data: bytes, *, storage: str | None = None) -> dict[str, object]:
    """Write ``data`` under a safe name; return the absolute path and metadata."""
    if not isinstance(data, (bytes, bytearray)):
        raise UploadError("no data")
    chosen = (storage or mode()).lower()
    if chosen not in ("temp", "permanent"):
        chosen = mode()
    directory = _permanent_dir() if chosen == "permanent" else _temp_dir()
    clean = _clean_name(name)
    path = _unique(directory, clean)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise UploadError(str(exc)) from exc
    return {"path": path, "name": os.path.basename(path), "size": len(data),
            "mode": chosen}


def cleanup() -> int:
    """Prune uploads: temp by age, permanent by ``uploads.keep_days``.

    Called by the daemon janitor. Never touches anything outside the two
    directories this module owns.
    """
    removed = 0
    now = time.time()
    temp = _temp_dir()
    try:
        temp_age = max(3600.0, float(settings.get("maintenance.tmp_max_age_minutes")) * 60)
    except (TypeError, ValueError):
        temp_age = 3600.0
    try:
        keep_days = int(settings.get("uploads.keep_days"))
    except (TypeError, ValueError):
        keep_days = 30
    try:
        permanent = _permanent_dir(create=False)
    except (UploadError, OSError):
        permanent = None
    for directory, max_age in ((temp, temp_age),
                               (permanent, keep_days * 86400 if keep_days else None)):
        if not directory or not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            full = os.path.join(directory, entry)
            if not os.path.isfile(full):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if max_age is not None and now - st.st_mtime > max_age:
                try:
                    os.unlink(full)
                    removed += 1
                except OSError:
                    continue
    return removed


def info() -> dict[str, object]:
    """Where uploads land and how many are stored (for the Settings panel)."""
    temp = _temp_dir()
    try:
        permanent = _permanent_dir(create=False)
    except (UploadError, OSError):
        permanent = None
    return {"mode": mode(), "temp_dir": temp, "permanent_dir": permanent,
            "max_mb": max_bytes() // (1024 * 1024)}
