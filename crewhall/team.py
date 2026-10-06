from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .harness import AgentInfo, Harness


class TeamError(RuntimeError):
    pass


class TeamNotFound(KeyError):
    pass


KEEP: Any = object()  # "leave the host as it is" for set_workspace


def new_team_id() -> str:
    return "team_" + uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class Team:
    """A named, logical grouping of existing agents.

    A Team is identity (``team_id``) + name + a set of ``agent_id`` references,
    optionally a shared **workspace** (a directory used as the default cwd for
    its members). It stores references to agents, never Harness instances,
    sessions, pids or panes. It is not a workflow, not a role model and not an
    orchestrator.
    """

    team_id: str
    name: str
    agent_ids: tuple[str, ...]
    created_at: float
    workspace: str | None = None
    workspace_mode: str | None = None  # shared | worktree
    # A configured SSH host: the workspace is then a path *on that host* and only
    # agents running there may be members.  None = a local team (as before).
    host: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "name": self.name,
            "agent_ids": list(self.agent_ids),
            "created_at": self.created_at,
            "workspace": self.workspace,
            "workspace_mode": self.workspace_mode,
            "host": self.host,
        }


class TeamRegistry:
    """Owns Team identity and membership. Membership is owned by the Team.

    Agents do not know which Teams they belong to (no back-reference), so the
    AgentRegistry stays untouched and an agent may belong to several Teams.
    Membership is validated against the AgentRegistry at creation time.
    """

    def __init__(self, agents: Any) -> None:
        self._agents = agents
        # Set by the controller: ``(host, path) -> None`` raising TeamError when
        # the directory cannot be confirmed on that host (user-initiated changes).
        self.remote_validator: Any = None
        self._teams: dict[str, Team] = {}
        self._lock = threading.RLock()

    def create(
        self,
        name: str,
        agent_ids: list[str] | tuple[str, ...],
        *,
        team_id: str | None = None,
        workspace: str | None = None,
        workspace_mode: str | None = None,
        created_at: float | None = None,
        host: str | None = None,
        verify_remote: bool = True,
    ) -> Team:
        if not isinstance(name, str) or not name.strip():
            raise TeamError("team name must be a non-empty string")
        requested = list(agent_ids)
        if len(set(requested)) != len(requested):
            raise TeamError("duplicate agents in team membership")

        resolved: list[str] = []
        for target in requested:
            harness = self._resolve_agent(target)
            if harness.agent_id in resolved:
                raise TeamError(
                    f"duplicate agents in team membership ({harness.agent_id})"
                )
            resolved.append(harness.agent_id)

        for agent_id in resolved:
            self._check_host_member(host, agent_id)
        ws = self._validate_ws(workspace, host, verify_remote)
        if workspace_mode not in (None, "shared", "worktree"):
            raise TeamError("workspace_mode must be 'shared' or 'worktree'")
        team = Team(
            team_id=team_id or new_team_id(),
            name=name.strip(),
            agent_ids=tuple(resolved),
            created_at=created_at if created_at is not None else time.time(),
            workspace=ws,
            workspace_mode=workspace_mode,
            host=host or None,
        )
        with self._lock:
            if team.team_id in self._teams:
                raise TeamError(f"team id already exists: {team.team_id}")
            self._teams[team.team_id] = team
        return team

    def _validate_ws(self, workspace: str | None, host: str | None, verify: bool) -> str | None:
        if not workspace:
            return None
        if not host:
            return validate_workspace(workspace)
        path = validate_remote_workspace(workspace)
        if verify and self.remote_validator is not None:
            self.remote_validator(host, path)
        return path

    def _agent_host(self, agent_id: str) -> str | None:
        try:
            spec = self._agents.resolve(agent_id).session.spec
        except Exception:  # noqa: BLE001 - not a real session (or gone)
            return None
        return getattr(spec, "host", None) or None

    def _check_host_member(self, host: str | None, agent_id: str) -> None:
        if host and self._agent_host(agent_id) != host:
            raise TeamError(
                f"team host is {host!r}: agent {agent_id} does not run there "
                f"(a remote team only takes agents of its own host)"
            )

    def set_workspace(self, target_team: str, workspace: str | None, host: Any = KEEP) -> Team:
        """Set the workspace (and optionally the host of an empty team)."""
        with self._lock:
            team = self.get(target_team)
            new_host = team.host if host is KEEP else (host or None)
            if new_host != team.host and team.agent_ids:
                raise TeamError(
                    "the host of a team with members cannot change: remove its agents first"
                )
            ws = self._validate_ws(workspace, new_host, True)
            updated = Team(
                team_id=team.team_id,
                name=team.name,
                agent_ids=team.agent_ids,
                created_at=team.created_at,
                workspace=ws,
                workspace_mode=team.workspace_mode,
                host=new_host,
            )
            self._teams[team.team_id] = updated
            return updated

    def add_member(self, target_team: str, target_agent: str) -> Team:
        with self._lock:
            team = self.get(target_team)
            harness = self._resolve_agent(target_agent)
            if harness.agent_id in team.agent_ids:
                raise TeamError(
                    f"agent {harness.agent_id} is already a member of "
                    f"team {team.team_id}"
                )
            self._check_host_member(team.host, harness.agent_id)
            return self._replace(
                team, team.agent_ids + (harness.agent_id,)
            )

    def remove_member(self, target_team: str, target_agent: str) -> Team:
        with self._lock:
            team = self.get(target_team)
            agent_id = self._resolve_member_id(team, target_agent)
            if agent_id not in team.agent_ids:
                raise TeamError(
                    f"agent {target_agent!r} is not a member of team "
                    f"{team.team_id}"
                )
            remaining = tuple(a for a in team.agent_ids if a != agent_id)
            return self._replace(team, remaining)

    def _replace(self, team: Team, agent_ids: tuple[str, ...]) -> Team:
        updated = Team(
            team_id=team.team_id,
            name=team.name,
            agent_ids=agent_ids,
            created_at=team.created_at,
            workspace=team.workspace,
            workspace_mode=team.workspace_mode,
            host=team.host,
        )
        self._teams[team.team_id] = updated
        return updated

    def _resolve_member_id(self, team: Team, target: str) -> str:
        try:
            return self._resolve_agent(target).agent_id
        except TeamError:
            if target in team.agent_ids:
                return target
            raise TeamError(f"unknown agent {target!r}") from None

    def get(self, target: str) -> Team:
        with self._lock:
            if target in self._teams:
                return self._teams[target]
            matches = [
                t
                for t in self._teams.values()
                if t.name == target or t.team_id.startswith(target)
            ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise TeamNotFound(target)
            raise TeamNotFound(
                f"ambiguous team {target!r}: {', '.join(t.team_id for t in matches)}"
            )

    def list(self) -> list[Team]:
        with self._lock:
            return list(self._teams.values())

    def remove(self, target: str) -> Team:
        team = self.get(target)
        with self._lock:
            self._teams.pop(team.team_id, None)
        return team

    def forget_agent(self, agent_id: str) -> None:
        """Drop a deleted agent from every Team's membership.

        Called when an agent is removed so Teams do not keep a dangling
        reference that would surface as ``(missing)``.
        """
        with self._lock:
            for team in list(self._teams.values()):
                if agent_id in team.agent_ids:
                    remaining = tuple(a for a in team.agent_ids if a != agent_id)
                    self._replace(team, remaining)

    def member_harnesses(self, team: Team) -> list[Harness]:
        out: list[Harness] = []
        for agent_id in team.agent_ids:
            try:
                out.append(self._agents.resolve(agent_id))
            except KeyError:
                continue
        return out

    def members(self, team: Team) -> list[AgentInfo]:
        return [harness.info() for harness in self.member_harnesses(team)]

    def missing(self, team: Team) -> list[str]:
        alive = {harness.agent_id for harness in self.member_harnesses(team)}
        return [agent_id for agent_id in team.agent_ids if agent_id not in alive]

    def info(self, team: Team) -> dict[str, Any]:
        return {
            **team.to_dict(),
            "members": [info.to_dict() for info in self.members(team)],
            "missing": self.missing(team),
        }

    def _resolve_agent(self, target: str) -> Harness:
        try:
            return self._agents.resolve(target)
        except KeyError as exc:
            raise TeamError(f"unknown agent {target!r}") from exc


def validate_workspace(workspace: str) -> str:
    """Validate a Team workspace path: must be an existing directory.

    Relative paths are rejected on purpose (the daemon may run from any cwd);
    callers must pass an absolute path. Existence is required because the
    workspace is used as the default cwd for agents.
    """
    import os

    if not isinstance(workspace, str) or not workspace.strip():
        raise TeamError("workspace must be a non-empty string")
    path = os.path.abspath(os.path.expanduser(workspace))
    if not os.path.isabs(path):
        raise TeamError("workspace must be an absolute path")
    if not os.path.isdir(path):
        raise TeamError(f"workspace does not exist or is not a directory: {path}")
    import os as _os

    if not _os.access(path, _os.R_OK | _os.X_OK):
        raise TeamError(f"workspace is not accessible: {path}")
    return path


def validate_remote_workspace(workspace: str) -> str:
    """Syntax check of a workspace path on a *remote* host (never touches the local FS).

    Existence on the host is verified separately, over SSH, by the controller.
    """
    import posixpath

    if not isinstance(workspace, str) or not workspace.strip():
        raise TeamError("workspace must be a non-empty string")
    if len(workspace) > 4096 or any(c in workspace for c in "\0\n\r"):
        raise TeamError("workspace: invalid characters")
    if not workspace.startswith("/"):
        raise TeamError("a remote workspace must be an absolute path on the host (starting with /)")
    return posixpath.normpath(workspace)


def workspace_for_agent(teams: list[Team], agent_id: str) -> str | None:
    """Return the shared workspace of the first Team containing agent_id."""
    for team in teams:
        if agent_id in team.agent_ids and team.workspace:
            return team.workspace
    return None
