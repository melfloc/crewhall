"""Validation and identity helpers for web/CLI *terminals*.

A terminal is a raw interactive session (``kind="terminal"``) managed by the
daemon, distinct from an agent.  It is never registered as an agent and never
appears in teams, messaging or ``agent_*`` operations.

Everything that comes from a client (host, cwd, shell, title, command, id) is
validated here, *before* tmux/SSH is touched, and every value is ultimately
passed to tmux as an ``argv`` element (never interpolated into a shell).
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from typing import Any

KIND_SESSION = "session"
KIND_TERMINAL = "terminal"


class TerminalError(ValueError):
    """A client-controlled terminal parameter failed validation."""

TERMINAL_PREFIX = "term_"
TERMINAL_ID_RE = re.compile(r"^term_[0-9a-f]{8}$")
TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}$")

SHELL_WHITELIST = {"bash", "zsh", "sh", "fish"}
MAX_COMMAND = 4096
MAX_MESSAGE_BYTES = 65536


def new_terminal_id() -> str:
    return TERMINAL_PREFIX + uuid.uuid4().hex[:8]


def is_terminal(obj: Any) -> bool:
    """True for SessionSpec/SessionInfo/InteractiveSession/dict terminals."""
    if obj is None:
        return False
    if isinstance(obj, dict):
        return obj.get("kind") == KIND_TERMINAL
    kind = getattr(obj, "kind", None)
    if kind is None:
        spec = getattr(obj, "spec", None)
        kind = getattr(spec, "kind", None)
    return kind == KIND_TERMINAL


def validate_terminal_id(value: Any) -> str:
    if not isinstance(value, str) or not TERMINAL_ID_RE.match(value):
        raise ValueError("invalid terminal id")
    return value


def validate_host(host: Any) -> str | None:
    """None/"local"/"" mean local; anything else must be a configured host."""
    from . import settings

    if host in (None, "", "local"):
        return None
    if not isinstance(host, str):
        raise ValueError("host must be a string")
    if host not in settings.hosts():
        raise ValueError(f"unknown host {host!r}")
    return host


def validate_cwd(cwd: Any, host: str | None) -> str | None:
    if cwd in (None, ""):
        return None
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("cwd must be a non-empty string")
    if host is not None:
        from . import team

        return team.validate_remote_workspace(cwd)
    path = os.path.abspath(os.path.expanduser(cwd))
    if not os.path.isdir(path):
        raise ValueError(f"cwd is not a directory: {path}")
    if not os.access(path, os.R_OK | os.X_OK):
        raise ValueError(f"cwd is not accessible: {path}")
    return path


def validate_shell(shell: Any, host: str | None) -> str | None:
    if host is not None:
        if shell not in (None, ""):
            raise ValueError("shell is only allowed for local terminals")
        return None
    if shell in (None, ""):
        return None
    if not isinstance(shell, str):
        raise ValueError("shell must be a string")
    name = shell
    if shell == "$SHELL":
        base = os.path.basename(os.environ.get("SHELL", ""))
        name = base if base in SHELL_WHITELIST else "bash"
    if name not in SHELL_WHITELIST:
        raise ValueError("shell must be one of: " + ", ".join(sorted(SHELL_WHITELIST)))
    path = shutil.which(name)
    if not path or not os.path.isabs(path) or not os.path.isfile(path):
        raise ValueError(f"shell not found: {name}")
    return path


def validate_title(title: Any) -> str | None:
    if title in (None, ""):
        return None
    if not isinstance(title, str) or not TITLE_RE.match(title):
        raise ValueError("title must be 1-40 chars: letters, digits, space, _ . -")
    return title


def validate_command(command: Any) -> str | None:
    if command in (None, ""):
        return None
    if not isinstance(command, str) or len(command) > MAX_COMMAND or "\0" in command:
        raise ValueError("command is invalid")
    return command


def validate_readonly(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("readonly must be true or false")
    return value


def check_limits(
    terminals: list[dict[str, Any]], host: str | None, max_total: int, max_per_host: int
) -> None:
    if len(terminals) >= max_total:
        raise ValueError(f"too many terminals (max {max_total})")
    same = sum(1 for t in terminals if (t.get("host") or None) == (host or None))
    if same >= max_per_host:
        where = host or "local"
        raise ValueError(f"too many terminals on {where} (max {max_per_host})")
