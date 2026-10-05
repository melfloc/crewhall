"""Directory completion on the machine where the daemon runs.

The Web UI is a remote control: a workspace typed in it is a path on the
*daemon's* host, so suggestions have to come from there. Only directory names
are returned (never files or their contents), at most ``LIMIT`` per call.

When ``roots`` is given (the daemon always passes the configured
``security.fs_roots`` plus team workspaces and running agents' directories),
suggestions are confined to those real paths: a ``..`` or a symlink that escapes
a root yields nothing, never a listing.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

LIMIT = 80
RATE_PER_MINUTE = 240

_RATE: dict[str, list[float]] = {}
_RATE_LOCK = threading.Lock()


def _rate_ok(actor: str | None) -> bool:
    if not actor:
        return True
    now = time.time()
    key = actor[:64]
    with _RATE_LOCK:
        hits = [t for t in _RATE.get(key, []) if now - t < 60.0]
        if len(hits) >= RATE_PER_MINUTE:
            _RATE[key] = hits
            return False
        hits.append(now)
        _RATE[key] = hits
    return True


def _resolve_roots(roots: list[str] | None) -> list[str] | None:
    if roots is None:
        return None
    out = []
    for root in roots:
        try:
            out.append(os.path.realpath(os.path.expanduser(str(root))))
        except (OSError, ValueError):
            continue
    return out


def _within(path: str, roots: list[str]) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def complete(prefix: str | None, *, roots: list[str] | None = None,
             actor: str | None = None) -> dict[str, Any]:
    text = (prefix or "").strip()
    if len(text) > 1024 or "\0" in text:
        return {"base": "", "entries": [], "truncated": False, "error": "invalid path"}
    if not _rate_ok(actor):
        return {"base": "", "entries": [], "truncated": False, "error": "too many requests"}
    if not text:
        text = "~/"
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        expanded = os.path.join(os.getcwd(), expanded)  # relative paths mean the daemon's cwd
    if expanded.endswith(os.sep):
        base, stem = expanded, ""
    else:
        base, stem = os.path.dirname(expanded) + os.sep, os.path.basename(expanded)
    base = os.path.normpath(base) + (os.sep if base != os.sep else "")
    resolved_roots = _resolve_roots(roots)
    if resolved_roots is not None and not _within(base, resolved_roots):
        return {"base": "", "entries": [], "truncated": False,
                "error": "outside the allowed roots"}
    try:
        names = os.listdir(base)
    except OSError as exc:
        return {"base": base, "entries": [], "truncated": False, "error": exc.strerror or str(exc)}
    show_hidden = stem.startswith(".")
    low = stem.lower()
    matches = []
    for name in names:
        if name.startswith(".") and not show_hidden:
            continue
        if not name.lower().startswith(low):
            continue
        full = os.path.join(base, name)
        try:
            if not os.path.isdir(full):  # follows symlinks: a link to a directory is one
                continue
        except OSError:
            continue
        if resolved_roots is not None and not _within(full, resolved_roots):
            continue  # a symlink pointing outside every root
        matches.append(name)
    matches.sort(key=lambda n: (not n.startswith(stem), n.lower()))  # exact-case matches first
    truncated = len(matches) > LIMIT
    return {"base": base, "truncated": truncated, "error": None,
            "entries": [{"name": n, "path": os.path.join(base, n) + os.sep} for n in matches[:LIMIT]]}
