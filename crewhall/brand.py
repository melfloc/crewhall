"""Single source of truth for the product's names.

The project is being renamed to **crewhall** (confirmed). Everything that
carries the name — distribution, command, environment prefix, directories,
socket, control-file markers, systemd units — is defined here so the switch is
one place, and a compatibility layer keeps old names working.

Activation: the command, environment prefix (with ``AGENT_TERMINAL_*``
fallback), control-file markers and directories are all ``crewhall``. On first
access, a legacy ``agent-terminal`` config/state tree is *copied* (never moved or
deleted) to the new location, so an upgrade keeps its data and can be rolled back.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any

NAME = "crewhall"
LEGACY_NAME = "agent-terminal"
PACKAGE = "crewhall"

ENV_PREFIX = "CREWHALL"
LEGACY_ENV_PREFIX = "AGENT_TERMINAL"

# Directory base names. Legacy by default until migration has run.
DIR_BASE = LEGACY_NAME
NEW_DIR_BASE = NAME
# New installs and upgrades use the crewhall directories; the first access copies
# (never moves) an existing legacy tree, so the old data stays as a backup.
USE_NEW_PATHS = True

CONTROL_BEGIN = f"<!-- BEGIN {ENV_PREFIX} MANAGED SECTION -->"
CONTROL_END = f"<!-- END {ENV_PREFIX} MANAGED SECTION -->"
LEGACY_CONTROL_BEGIN = f"<!-- BEGIN {LEGACY_ENV_PREFIX.replace('_', '-')} MANAGED SECTION -->"
LEGACY_CONTROL_END = f"<!-- END {LEGACY_ENV_PREFIX.replace('_', '-')} MANAGED SECTION -->"

SYSTEMD_UNITS = ("crewhall.service", "crewhall-web.service")
LEGACY_SYSTEMD_UNITS = ("agent-terminal.service", "agent-terminal-web.service", "at-web.service")

_warned: set[str] = set()


def _warn(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        print(f"crewhall: {message}", file=sys.stderr)


def env(suffix: str, default: str | None = None) -> str | None:
    """Read ``CREWHALL_<suffix>``, falling back to ``AGENT_TERMINAL_<suffix>``.

    The legacy value is still honoured, with a one-time deprecation notice.
    """
    new = os.environ.get(f"{ENV_PREFIX}_{suffix}")
    if new not in (None, ""):
        return new
    old = os.environ.get(f"{LEGACY_ENV_PREFIX}_{suffix}")
    if old not in (None, ""):
        _warn(f"{LEGACY_ENV_PREFIX}_{suffix} is deprecated; use {ENV_PREFIX}_{suffix}")
        return old
    return default


def dir_name(kind: str) -> str:
    """Base directory name for ``config`` / ``state`` / ``runtime``."""
    return NEW_DIR_BASE if USE_NEW_PATHS else DIR_BASE


def control_markers() -> tuple[bytes, bytes, list[tuple[bytes, bytes]]]:
    """(write_begin, write_end, legacy_pairs) for the managed section."""
    return (
        CONTROL_BEGIN.encode(),
        CONTROL_END.encode(),
        [(LEGACY_CONTROL_BEGIN.encode(), LEGACY_CONTROL_END.encode())],
    )


def _copy_tree(src: str, dst: str) -> bool:
    """Copy ``src``→``dst`` (never deletes or modifies ``src``). Reversible."""
    if not os.path.isdir(src) or os.path.exists(dst):
        return False
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copytree(src, dst)
    try:
        with open(os.path.join(dst, ".migrated-from"), "w", encoding="utf-8") as fh:
            fh.write(src + "\n")
    except OSError:
        pass
    return True


def migrate_tree(base_old: str, base_new: str, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Plan/perform copies of the legacy directory trees to the new name.

    Only copies; the source is left untouched so the change is reversible.
    Returns the list of ``{from, to, status}`` entries.
    """
    out: list[dict[str, Any]] = []
    for kind in ("config", "state", "runtime"):
        src = os.path.join(base_old, kind)
        dst = os.path.join(base_new, kind)
        if not os.path.exists(src):
            continue
        status = "would-copy" if dry_run else ("copied" if _copy_tree(src, dst) else "exists")
        out.append({"from": src, "to": dst, "status": status})
    return out


def _xdg(var: str, fallback: tuple[str, ...]) -> str:
    return os.environ.get(var) or os.path.join(os.path.expanduser("~"), *fallback)


def _resolve(root: str, kind: str, *, migrate: bool) -> str:
    new = os.path.join(root, dir_name(kind))
    if migrate and USE_NEW_PATHS and not os.path.exists(new):
        legacy = os.path.join(root, LEGACY_NAME)
        try:
            if os.path.isdir(legacy):
                shutil.copytree(legacy, new, ignore=shutil.ignore_patterns("*.sock", "*.lock"))
                try:
                    os.chmod(new, 0o700)
                    with open(os.path.join(new, ".migrated-from"), "w", encoding="utf-8") as fh:
                        fh.write(legacy + "\n")
                except OSError:
                    pass
        except OSError as exc:
            _warn(f"could not migrate {legacy}: {exc}")
    return new


def config_dir() -> str:
    """``$XDG_CONFIG_HOME/crewhall`` (migrated from ``agent-terminal`` on first use)."""
    return _resolve(_xdg("XDG_CONFIG_HOME", (".config",)), "config", migrate=True)


def state_root_dir() -> str:
    """``$XDG_STATE_HOME/crewhall`` (migrated from ``agent-terminal`` on first use)."""
    return _resolve(_xdg("XDG_STATE_HOME", (".local", "state")), "state", migrate=True)


def runtime_name() -> str:
    """Directory/file base name for runtime sockets (no migration: they are ephemeral)."""
    return dir_name("runtime")
