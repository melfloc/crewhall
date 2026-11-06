"""Team-bound permission and limits for agent-controlled agent creation."""
from __future__ import annotations

import threading
import time
from typing import Any

from . import settings
from .project_agents import (
    discover_project_agents,
    project_agent_catalog,
)


class AgentSpawnPolicy:
    """Policy for the single spawner assigned to each Team."""

    def __init__(self) -> None:
        self.lock = threading.RLock()

    @classmethod
    def from_settings(cls) -> AgentSpawnPolicy:
        # Kept as a constructor hook for Controller; the permission itself lives
        # on each Team and can change without restarting the daemon.
        return cls()

    @property
    def max_live(self) -> int:
        return int(settings.get("agent_spawn.max_live"))

    @property
    def max_created_per_day(self) -> int:
        return int(settings.get("agent_spawn.max_created_per_day"))

    @staticmethod
    def _workspace(team: Any, spawner: Any) -> str | None:
        return team.workspace or getattr(spawner.session.spec, "cwd", None)

    def spawner_team(self, controller: Any, spawner: Any) -> Any:
        teams = [team for team in controller.teams.list()
                 if team.spawner_id == spawner.agent_id
                 and spawner.agent_id in team.agent_ids]
        if not teams:
            raise ValueError("this agent is not the spawner for a team")
        if len(teams) != 1:
            raise ValueError("an agent may be the spawner for only one team")
        team = teams[0]
        if team.host or getattr(team, "workspace_mode", None) == "worktree":
            raise ValueError("agent creation is limited to local shared-workspace teams")
        return team

    def project_agents(self, controller: Any, team: Any, spawner: Any) -> list[dict[str, Any]]:
        return discover_project_agents(self._workspace(team, spawner))

    def child_ids(self, controller: Any, creator_id: str) -> list[str]:
        owners = getattr(controller, "_agent_creators", {})
        return sorted(agent_id for agent_id, owner in owners.items() if owner == creator_id)

    def create_request(
        self, controller: Any, creator: Any, request: dict[str, Any]
    ) -> dict[str, Any]:
        team = self.spawner_team(controller, creator)
        team_ref = request.get("team")
        if team_ref and team_ref not in (team.team_id, team.name):
            raise ValueError("a spawner can create agents only in its own team")

        profile_name = request.get("profile")
        if not isinstance(profile_name, str) or not profile_name:
            raise ValueError("choose a profile from this project's cwh-agents.json")
        cwd = self._workspace(team, creator)
        catalog = project_agent_catalog(cwd)
        if catalog["error"]:
            raise ValueError(catalog["error"])
        if not team.spawner_catalog_sha256:
            raise ValueError(
                "this Team has no operator-approved agent catalog; approve it with "
                "`crewhall team approve-profiles`"
            )
        if catalog["sha256"] != team.spawner_catalog_sha256:
            raise ValueError(
                "the project agent catalog changed after approval; ask the user to "
                "review it and run `crewhall team approve-profiles`"
            )
        project_agent = next(
            (item for item in catalog["profiles"] if item["profile"] == profile_name), None,
        )
        if project_agent is None:
            raise ValueError("that profile is not available in this project's cwh-agents.json")

        name = request.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("a name of 1 to 120 characters is required")
        name = name.strip()
        if any(ord(char) < 32 for char in name):
            raise ValueError("agent name contains a control character")
        if any(agent.name == name for agent in controller.agents.all()):
            raise ValueError(f'agent name "{name}" already exists')

        kind = project_agent["kind"]
        model = project_agent.get("model")
        effort = project_agent.get("effort")
        if kind == "opencode" and effort and effort != "default":
            model = model or settings.provider(kind).get("default_model") or None
            if not model:
                raise ValueError(
                    "an OpenCode effort needs a model in cwh-agents.json or Settings"
                )
            if "#" not in model:
                model = f"{model}#{effort}"
        args = settings.provider_args(
            kind, (["--model", model] if model else None),
        )
        if model:
            args.extend(["--model", model])
        if project_agent.get("agent"):
            args.extend(["--agent", project_agent["agent"]])
        if kind == "claude" and effort and effort != "default":
            args.extend(["--effort", effort])
        for flag, value in project_agent["flags"].items():
            if flag == "--thinking":
                if value:
                    args.append(flag)
            else:
                args.extend([flag, str(value)])
        return {
            "kind": kind,
            "name": name,
            "backend": creator.session.backend.name,
            "cwd": cwd,
            "team": team.team_id,
            "args": args,
            "workspace_mode": "shared",
            "host": None,
            "profile": project_agent["profile"],
            "profile_sha256": project_agent["manifest_sha256"],
            "cols": request.get("cols", 120),
            "rows": request.get("rows", 40),
            "created_by": creator.agent_id,
        }

    def live_children(self, controller: Any, creator_id: str) -> int:
        owners = getattr(controller, "_agent_creators", {})
        count = 0
        registered: set[str] = set()
        for harness in controller.agents.all():
            if owners.get(harness.agent_id) != creator_id:
                continue
            registered.add(harness.agent_id)
            try:
                if harness.session.status.alive:
                    count += 1
            except Exception:  # noqa: BLE001 - uncertain status still consumes a slot
                count += 1
        # A reservation without a registered Harness can still own a pane while
        # startup reconciles it, so it consumes a slot too.
        count += sum(1 for agent_id in self.child_ids(controller, creator_id)
                     if agent_id not in registered)
        return count

    def check_create_slot(self, controller: Any, creator_id: str) -> None:
        if self.live_children(controller, creator_id) >= self.max_live:
            raise ValueError(f"max_live ({self.max_live}) reached for this spawner")

    def check_create_rate(self, controller: Any, creator_id: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        events = getattr(controller, "_agent_creation_events", {}).get(creator_id, [])
        recent = [float(at) for at in events if now - 86400 < float(at) <= now + 60]
        if len(recent) >= self.max_created_per_day:
            raise ValueError(
                f"max_created_per_day ({self.max_created_per_day}) reached for this spawner"
            )

    def reserve_create(self, controller: Any, creator_id: str, *, now: float | None = None) -> None:
        """Persist a daily spawn reservation before starting a provider process."""
        now = time.time() if now is None else now
        if getattr(controller, "_store", None) is None:
            raise ValueError("agent-controlled creation requires persistent state")
        self.check_create_rate(controller, creator_id, now=now)
        events = getattr(controller, "_agent_creation_events", None)
        if not isinstance(events, dict):
            events = {}
            controller._agent_creation_events = events
        recent = [float(at) for at in events.get(creator_id, [])
                  if now - 86400 < float(at) <= now + 60]
        recent.append(now)
        events[creator_id] = recent
        controller._persist()
        saved = controller._store.load().get("agent_creation_events", {})
        persisted = saved.get(creator_id, []) if isinstance(saved, dict) else []
        if not persisted or abs(float(persisted[-1]) - now) > 0.001:
            events[creator_id] = recent[:-1]
            raise ValueError("could not persist the agent spawn limit; creation cancelled")

    def authorize_stop(self, controller: Any, creator: Any, target: str) -> Any:
        team = self.spawner_team(controller, creator)
        child = controller.agents.resolve(target)
        if child.agent_id == creator.agent_id:
            raise ValueError("an agent cannot stop itself")
        if getattr(controller, "_agent_creators", {}).get(child.agent_id) != creator.agent_id:
            raise ValueError("a spawner may stop only agents it created")
        if child.agent_id not in team.agent_ids:
            raise ValueError("the worker must remain in its spawner's team")
        return child

    def check_restart_slot(self, controller: Any, child: Any) -> None:
        """Keep a stopped child from reviving above its creator's live limit."""
        if child.session.status.alive:
            return
        creator_id = getattr(controller, "_agent_creators", {}).get(child.agent_id)
        if not creator_id:
            return
        if self.live_children(controller, creator_id) >= self.max_live:
            raise ValueError(
                f"max_live ({self.max_live}) reached for this worker's spawner"
            )
