from __future__ import annotations

import shlex

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    TERMINATED = "terminated"
    ERROR = "error"

    @property
    def alive(self) -> bool:
        return self in (Status.STARTING, Status.RUNNING)


def new_session_id() -> str:
    return "sess_" + uuid.uuid4().hex[:12]


@dataclass
class SessionSpec:
    command: Any
    cwd: str | None = None
    env: dict[str, str] | None = None
    cols: int = 80
    rows: int = 24
    shell: bool = False
    name: str | None = None
    shell_path: str = "/bin/bash"

    def argv(self) -> list[str]:
        if isinstance(self.command, str):
            if self.shell:
                return [self.shell_path, "-lc", self.command]
            return self.command.split()
        return list(self.command)

    def display(self) -> str:
        if isinstance(self.command, str):
            return self.command
        return " ".join(str(c) for c in self.command)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.display(),
            "cwd": self.cwd,
            "env": self.env,
            "cols": self.cols,
            "rows": self.rows,
            "shell": self.shell,
            "name": self.name,
        }


@dataclass
class Event:
    type: str
    session_id: str
    at: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "at": self.at,
            "data": self.data,
        }


@dataclass
class SessionInfo:
    session_id: str
    backend: str
    pid: int | None
    command: str
    cwd: str
    status: Status
    created_at: float
    cols: int
    rows: int
    name: str | None = None
    exit_code: int | None = None
    last_input_at: float | None = None
    last_output_at: float | None = None
    exited_at: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "backend": self.backend,
            "pid": self.pid,
            "command": self.command,
            "cwd": self.cwd,
            "status": self.status.value,
            "created_at": self.created_at,
            "cols": self.cols,
            "rows": self.rows,
            "name": self.name,
            "exit_code": self.exit_code,
            "last_input_at": self.last_input_at,
            "last_output_at": self.last_output_at,
            "exited_at": self.exited_at,
            "meta": self.meta,
        }

MAX_AGENT_ARGS = 64
MAX_AGENT_ARG_LEN = 4096


def parse_agent_args(value: Any) -> list[str]:
    """Normalize extra CLI arguments for an agent into an argv list.

    Accepts ``None``, a string (split shell-style with ``shlex``, e.g.
    ``--agent reviewer``) or a list of strings. The arguments are appended to
    the harness command and executed as argv (never through a shell).
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            items = shlex.split(value)
        except ValueError as exc:
            raise ValueError(f"invalid arguments: {exc}") from exc
    elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        items = list(value)
    else:
        raise ValueError("arguments must be a string or a list of strings")
    if len(items) > MAX_AGENT_ARGS:
        raise ValueError(f"too many arguments (max {MAX_AGENT_ARGS})")
    for item in items:
        if len(item) > MAX_AGENT_ARG_LEN:
            raise ValueError("argument too long")
        if any(c in item for c in ("\x00", "\n", "\r")):
            raise ValueError("arguments must not contain newlines or NUL")
    return items
