"""Per-agent MCP wiring: temporary 0600 files that reference the launched
module (never the token) and are deleted when the agent closes."""
from __future__ import annotations

import json
import os
import sys
import tempfile

from .. import paths


def mcp_command() -> list[str]:
    return [sys.executable, "-P", "-m", "agent_terminal.mcp"]


def _private_dir() -> str:
    path = os.path.join(paths.state_dir(), "mcp")
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def write_claude_config() -> str:
    """Claude's ``--mcp-config`` JSON. Contains no secret: the server reads the
    agent's identity from the environment it inherits."""
    body = {"mcpServers": {"crewhall": {"command": mcp_command()[0],
                                              "args": mcp_command()[1:]}}}
    fd, path = tempfile.mkstemp(prefix="mcp-", suffix=".json", dir=_private_dir())
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    os.chmod(path, 0o600)
    return path


def codex_args() -> list[str]:
    """Codex ``-c`` overrides registering the same stdio server."""
    command, *args = mcp_command()
    return [
        "-c", f"mcp_servers.agent_terminal.command={json.dumps(command)}",
        "-c", "mcp_servers.agent_terminal.args=" + json.dumps(args),
    ]


def cleanup(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass
