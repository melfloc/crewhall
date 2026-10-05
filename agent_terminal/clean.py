"""Integrated cleanup: temporary test leftovers, old backups, old releases.

Everything here is conservative by design:

- It runs as a **dry run by default** (the CLI asks for confirmation before it
  deletes anything, and ``--dry-run`` prints exactly what it would remove).
- It never touches a live agent: only explicitly-listed categories are removed.
- It refuses to delete anything that is not clearly a known artifact.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from . import paths

# Temporary directories created by the test suite (``tempfile.mkdtemp``).
_TMP_PREFIXES = ("at-tests-", "at-web-", "at-wsev-", "at-specs-", "at-bundle-", "at-hist-")

KEEP_BACKUPS = 5   # control-backups / pre-update-*: keep the newest N per group


@dataclass
class Item:
    path: str
    kind: str
    bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "bytes": self.bytes}


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(i.bytes for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"items": [i.to_dict() for i in self.items], "total_bytes": self.total_bytes}


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _size(path: str) -> int:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            return _dir_size(path)
        return os.stat(path).st_size
    except OSError:
        return 0


def _oldest_first(paths_: list[str]) -> list[str]:
    def mtime(p: str) -> float:
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0.0

    return sorted(paths_, key=mtime)


def plan_cleanup(
    *,
    tmp_roots: list[str] | None = None,
    keep_backups: int = KEEP_BACKUPS,
    max_age_days: int | None = None,
    managed_prefix: str | None = None,
    state_dir: str | None = None,
) -> Plan:
    """List what cleanup *would* remove. Pure: nothing is deleted here."""
    plan = Plan()
    now = time.time()
    max_age = max_age_days * 86400 if max_age_days else None

    # 1. Temporary test leftovers in the system temp roots.
    for root in tmp_roots if tmp_roots is not None else ["/tmp"]:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if not name.startswith(_TMP_PREFIXES):
                continue
            full = os.path.join(root, name)
            if max_age is not None and now - os.stat(full).st_mtime < max_age:
                continue
            plan.items.append(Item(full, "tmp", _size(full)))

    state = state_dir if state_dir is not None else paths.state_dir()

    # 2. Old control-file backups: keep the newest ``keep_backups`` per key.
    cb_root = os.path.join(state, "control-backups")
    if os.path.isdir(cb_root):
        for key in os.listdir(cb_root):
            d = os.path.join(cb_root, key)
            if not os.path.isdir(d):
                continue
            files = _oldest_first([os.path.join(d, n) for n in os.listdir(d)])
            for path in files[:-keep_backups] if keep_backups else files:
                plan.items.append(Item(path, "control-backup", _size(path)))
            if not os.listdir(d):
                plan.items.append(Item(d, "control-backup-dir", 0))

    # 3. Old update backups.
    up = os.path.join(state, "backups")
    if os.path.isdir(up):
        files = _oldest_first(
            [os.path.join(up, n) for n in os.listdir(up) if n.startswith("pre-update-")]
        )
        for path in files[:-keep_backups] if keep_backups else files:
            plan.items.append(Item(path, "update-backup", _size(path)))

    # 4. Old releases in a managed installation (keep the newest).
    prefix = managed_prefix or _managed_prefix()
    if prefix:
        rel_root = os.path.join(prefix, "releases")
        if os.path.isdir(rel_root):
            entries = _oldest_first(
                [os.path.join(rel_root, n) for n in os.listdir(rel_root)
                 if os.path.isdir(os.path.join(rel_root, n))]
            )
            for path in entries[:-1]:  # keep the newest release
                plan.items.append(Item(path, "release", _size(path)))

    return plan


def list_worktrees(root: str | None = None) -> list[dict[str, Any]]:
    """Orphan git worktrees under the managed directory (never deleted here).

    ``clean`` only lists them: a dirty or unmerged worktree is never removed by
    cleanup; discarding is an explicit, typed action.
    """
    from . import worktrees as wt

    root = root or wt.root()
    out: list[dict[str, Any]] = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirs, _files in os.walk(root):
        if os.path.isfile(os.path.join(dirpath, ".git")):
            dirs[:] = []
            st = wt.status(dirpath)
            out.append({"path": dirpath, "branch": st["branch"], "dirty": st["dirty"],
                        "unmerged": st["unmerged"],
                        "removable": not st["dirty"] and not st["unmerged"]})
    return out


def _managed_prefix() -> str | None:
    try:
        from .updater import install_root

        return install_root()
    except Exception:  # noqa: BLE001
        return None


def apply_cleanup(plan: Plan) -> dict[str, Any]:
    """Delete the planned items. Never follows a symlink out of a known dir."""
    removed: list[str] = []
    failed: list[dict[str, str]] = []
    # Delete children before their (empty) directories.
    ordered = sorted(plan.items, key=lambda i: i.path.count(os.sep), reverse=True)
    for item in ordered:
        try:
            if os.path.isdir(item.path) and not os.path.islink(item.path):
                if os.listdir(item.path):
                    continue  # not empty: something else is there, leave it
                os.rmdir(item.path)
            elif os.path.exists(item.path):
                os.unlink(item.path)
            else:
                continue
            removed.append(item.path)
        except OSError as exc:
            failed.append({"path": item.path, "error": str(exc)})
    return {"removed": removed, "failed": failed, "bytes": plan.total_bytes}
