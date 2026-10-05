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

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "name": self.name,
            "agent_ids": list(self.agent_ids),
            "created_at": self.created_at,
            "workspace": self.workspace,
            "workspace_mode": self.workspace_mode,
        }


class TeamRegistry:
    """Owns Team identity and membership. Membership is owned by the Team.

    Agents do not know which Teams they belong to (no back-reference), so the
    AgentRegistry stays untouched and an agent may belong to several Teams.
    Membership is validated against the AgentRegistry at creation time.
    """

    def __init__(self, agents: Any) -> None:
        self._agents = agents
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

        ws = validate_workspace(workspace) if workspace else None
        if workspace_mode not in (None, "shared", "worktree"):
            raise TeamError("workspace_mode must be 'shared' or 'worktree'")
        team = Team(
            team_id=team_id or new_team_id(),
            name=name.strip(),
            agent_ids=tuple(resolved),
            created_at=created_at if created_at is not None else time.time(),
            workspace=ws,
            workspace_mode=workspace_mode,
        )
        with self._lock:
            if team.team_id in self._teams:
                raise TeamError(f"team id already exists: {team.team_id}")
            self._teams[team.team_id] = team
        return team

    def set_workspace(self, target_team: str, workspace: str | None) -> Team:
        with self._lock:
            team = self.get(target_team)
            ws = validate_workspace(workspace) if workspace else None
            updated = Team(
                team_id=team.team_id,
                name=team.name,
                agent_ids=team.agent_ids,
                created_at=team.created_at,
                workspace=ws,
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


def workspace_for_agent(teams: list[Team], agent_id: str) -> str | None:
    """Return the shared workspace of the first Team containing agent_id."""
    for team in teams:
        if agent_id in team.agent_ids and team.workspace:
            return team.workspace
    return None
