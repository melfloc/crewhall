"""Agent lifecycle hooks: exact turn signals pushed to the daemon.

Claude Code can run a command on ``UserPromptSubmit`` and ``Stop``. We point it
(via ``--settings``) at ``crewhall agent hook <event>``, which reports to
the daemon using the agent's own identity/token from its environment. This
replaces guessing "the turn is over" from screen text.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile

from . import paths

EVENTS = ("session_start", "prompt_submit", "stop")
# SessionStart also fires after ``/clear`` and ``/resume``: its payload carries the
# new ``session_id``, which is how the agent keeps the history it is showing.
_CLAUDE_EVENTS = {
    "SessionStart": "session_start", "UserPromptSubmit": "prompt_submit", "Stop": "stop",
}


def hook_command(event: str) -> str:
    exe = (shutil.which("crewhall") or shutil.which("agent-terminal"))
    base = shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -P -m agent_terminal"
    # Never let a hook problem surface in (or block) the agent's TUI.
    return f"{base} agent hook {event} >/dev/null 2>&1 || true"


# Upper bound Claude gives the permission hook; the daemon answers (or gives
# up, letting Claude show its own dialog) well before that.
PERMISSION_HOOK_TIMEOUT = 600


def permission_hook_command() -> str:
    exe = (shutil.which("crewhall") or shutil.which("agent-terminal"))
    base = shlex.quote(exe) if exe else f"{shlex.quote(sys.executable)} -P -m agent_terminal"
    # stdout carries the decision; no output means "ask in the TUI as usual".
    return f"{base} agent hook permission_request 2>/dev/null || true"


def claude_settings() -> dict:
    settings = {
        "hooks": {
            name: [{"hooks": [{"type": "command", "command": hook_command(event)}]}]
            for name, event in _CLAUDE_EVENTS.items()
        }
    }
    # A permission dialog (or AskUserQuestion) can be answered from the Web UI.
    settings["hooks"]["PermissionRequest"] = [{"hooks": [{
        "type": "command", "command": permission_hook_command(),
        "timeout": PERMISSION_HOOK_TIMEOUT,
    }]}]
    return settings


def ensure_claude_settings() -> str:
    """Write (if changed) the shared Claude settings file; return its path."""
    directory = os.path.join(paths.state_dir(), "hooks")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "claude-settings.json")
    text = json.dumps(claude_settings(), indent=2, sort_keys=True)
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return path
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path
