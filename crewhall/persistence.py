from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

from . import paths

log = logging.getLogger("crewhall.persistence")

SCHEMA_VERSION = 1


class StateStore:
    """Persist the logical control-plane state (Teams + agent definitions).

    Only non-sensitive, reconstructible data is stored: no identity tokens, no
    provider credentials, no process content. Runtime state (live sessions,
    pids, output) is owned by the daemon and is never persisted.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or paths.state_path()

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"schema": SCHEMA_VERSION, "teams": [], "agents": [], "terminals": []}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read state %s: %s", self.path, exc)
            return {"schema": SCHEMA_VERSION, "teams": [], "agents": [], "terminals": []}
        data.setdefault("teams", [])
        data.setdefault("agents", [])
        data.setdefault("requests", [])
        data.setdefault("terminals", [])
        return data

    def save(self, data: dict[str, Any]) -> None:
        data = {**data, "schema": SCHEMA_VERSION}
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not persist state %s: %s", self.path, exc)

    def snapshot(self, controller: Any) -> dict[str, Any]:
        """Build a persistable snapshot from a Controller."""
        teams = []
        for team in controller.teams.list():
            teams.append(
                {
                    "team_id": team.team_id,
                    "name": team.name,
                    "agent_ids": list(team.agent_ids),
                    "created_at": team.created_at,
                    "workspace": team.workspace,
                    "workspace_mode": getattr(team, "workspace_mode", None),
                    "host": getattr(team, "host", None),
                }
            )
        agents = []
        for harness in controller.agents.all():
            spec = harness.session.spec
            agents.append(
                {
                    "agent_id": harness.agent_id,
                    "name": harness.name,
                    "kind": harness.kind,
                    "backend": harness.session.backend.name,
                    "cwd": spec.cwd,
                    "cols": spec.cols,
                    "rows": spec.rows,
                    "args": list(controller._agent_args.get(harness.agent_id, [])),
                    "host": getattr(controller, "_agent_hosts", {}).get(harness.agent_id),
                    "conversation_id": harness.conversation_id,
                    # user-provided env only; identity/secret vars are omitted
                    "env": dict(controller._base_env.get(harness.agent_id) or {}),
                }
            )
        requests = []
        board = getattr(controller, "requests", None)
        if board is not None:
            requests = board.snapshot()
        terminals = []
        from .terminals import is_terminal

        for session in controller.registry.all():
            if not is_terminal(session):
                continue
            spec = session.spec
            shell = None
            if isinstance(spec.command, list) and len(spec.command) == 1:
                shell = spec.command[0]
            terminals.append({
                "session_id": session.session_id,
                "host": spec.host,
                "cwd": spec.cwd,
                "shell": shell,
                "title": spec.title,
                "readonly": bool(spec.readonly),
                "owner": spec.owner,
                "cols": spec.cols,
                "rows": spec.rows,
                "created_at": session.created_at,
            })
        return {"schema": SCHEMA_VERSION, "teams": teams, "agents": agents,
                "requests": requests, "terminals": terminals}
