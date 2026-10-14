"""Controller services: teams (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import time
from typing import Any
from .. import settings
from ..backends import ssh_tmux as ssh_tmux_backend
from ..harness import Harness
from ..messaging import MessagingError
from ..team import Team, TeamError
from ..controller import _shq, _ssh_hint


class ServicesTeams:

    def create_team(
        self, name: str, agent_ids: list[str] | tuple[str, ...], **kwargs: Any
    ) -> Team:
        if kwargs.get("host"):
            settings.host(kwargs["host"])  # an unknown host is rejected up front
        team = self.teams.create(name, agent_ids, **kwargs)
        self._persist()
        return team

    def team_up(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Idempotently create a team and its agents from a parsed team spec."""
        name = spec["name"]
        try:
            team = self.teams.get(name)
            created_team = False
        except Exception:  # noqa: BLE001 (TeamNotFound)
            team = self.create_team(
                name, [], workspace=spec.get("workspace"),
                workspace_mode=spec.get("workspace_mode"), host=spec.get("host"),
            )
            created_team = True
        created: list[str] = []
        existing: list[str] = []
        for entry in spec["agents"]:
            agent_name = entry["name"]
            if any(h.name == agent_name for h in self.agents.all()):
                existing.append(agent_name)
                try:
                    self.add_team_member(team.team_id, agent_name)
                except Exception as exc:  # noqa: BLE001
                    if "already a member" not in str(exc):
                        raise  # e.g. an existing agent on another host than a remote team
                continue
            self.create_agent(
                entry.get("kind") or "opencode",
                name=agent_name,
                backend=entry.get("backend"),
                cwd=entry.get("cwd"),
                team=team.team_id,
                args=entry.get("args"),
                host=entry.get("host"),
            )
            created.append(agent_name)
        # Leads may be named by agent name or id (as in the membership).
        spec_leads = spec.get("leads") or []
        if spec_leads:
            leads: list[str] = []
            for lead in spec_leads:
                try:
                    leads.append(self.agents.resolve(lead).agent_id)
                except Exception as exc:  # noqa: BLE001
                    raise TeamError(f"unknown lead {lead!r}") from exc
            try:
                self.teams.set_leads(team.team_id, leads)
                self._persist()
            except Exception as exc:  # noqa: BLE001
                raise TeamError(f"team leads: {exc}") from exc
        return {
            "team": self.team_info(team.team_id),
            "team_created": created_team,
            "created": created,
            "existing": existing,
        }

    def set_team_workspace(self, team: str, workspace: str | None, **kwargs: Any) -> Team:
        if kwargs.get("host"):
            settings.host(kwargs["host"])
        updated = self.teams.set_workspace(team, workspace, **kwargs)
        self._persist()
        return updated

    def get_team(self, target: str) -> Team:
        return self.teams.get(target)

    def list_teams(self) -> list[dict[str, Any]]:
        return [self.teams.info(team) for team in self.teams.list()]

    def live_team_defs(self) -> list[dict[str, Any]]:
        """Running teams as rebuildable definitions (for configuration bundles)."""
        out = []
        for team in self.teams.list():
            agents = []
            for agent_id in team.agent_ids:
                try:
                    h = self.agents.resolve(agent_id)
                except Exception:  # noqa: BLE001 - a missing member is not exported
                    continue
                info = h.info()
                agents.append({"name": info.name or info.agent_id, "kind": info.kind, "backend": info.backend,
                               "cwd": info.cwd, "args": list(self._agent_args.get(agent_id, [])),
                               "host": self._agent_hosts.get(agent_id)})
            out.append({"name": team.name, "workspace": team.workspace,
                        "workspace_mode": getattr(team, "workspace_mode", None),
                        "host": team.host, "agents": agents})
        return out

    def team_info(self, target: str) -> dict[str, Any]:
        return self.teams.info(self.teams.get(target))

    def team_members(self, target: str) -> list[dict[str, Any]]:
        return [info.to_dict() for info in self.teams.members(self.teams.get(target))]

    def remove_team(self, target: str) -> Team:
        team = self.teams.remove(target)
        self._persist()
        return team

    def add_team_member(self, team: str, agent: str) -> Team:
        result = self.teams.add_member(team, agent)
        self._persist()
        return result

    def remove_team_member(self, team: str, agent: str) -> Team:
        result = self.teams.remove_member(team, agent)
        self._persist()
        return result

    def set_team_leads(self, team: str, agents: list[str] | tuple[str, ...]) -> Team:
        result = self.teams.set_leads(team, agents)
        self._persist()
        return result

    def _conversation_leads(self) -> dict[str, set[str]]:
        """``team_id -> {agent_id}`` of agents leading a chat with that crew.

        A conversation's own agent is treated as a lead of its crew even if the
        Team's explicit ``leads`` does not list it (the chat is the reason the
        crew exists). Uses the same stored link chat_bridge relies on.
        """
        from .. import chat as conversations

        out: dict[str, set[str]] = {}
        for conv in conversations.list_all():
            team_id = conv.get("team")
            agent_id = conv.get("agent_id")
            if team_id and agent_id:
                out.setdefault(str(team_id), set()).add(str(agent_id))
        return out

    def _is_team_lead(self, agent_id: str, team: Team, conv_leads: dict[str, set[str]]) -> bool:
        return agent_id in team.leads or agent_id in conv_leads.get(team.team_id, set())

    def _authorize_lead(self, sender: Harness, target: Harness) -> None:
        """A lead may act on a member of its team, but not on a peer lead nor itself.

        Authority comes only from the sender's identity (already authenticated
        from its token), never from a field the caller chooses.
        """
        if sender.agent_id == target.agent_id:
            raise MessagingError("an agent cannot control itself this way")
        conv_leads = self._conversation_leads()
        tname = target.name or target.agent_id
        peer_lead_of: str | None = None
        for team in self.teams.list():
            if target.agent_id not in team.agent_ids:
                continue
            if not self._is_team_lead(sender.agent_id, team, conv_leads):
                continue
            if self._is_team_lead(target.agent_id, team, conv_leads):
                peer_lead_of = team.name  # keep looking: another team may allow it
                continue
            return
        if peer_lead_of is not None:
            raise MessagingError(f"not allowed: {tname} is also a lead of team {peer_lead_of!r}")
        raise MessagingError(
            f"not a lead of a team that contains {tname}"
        )

    def _lead_team_ids(self, agent_id: str) -> set[str]:
        conv_leads = self._conversation_leads()
        return {
            team.team_id for team in self.teams.list()
            if agent_id in team.agent_ids and self._is_team_lead(agent_id, team, conv_leads)
        }

    def _lead_of(self, sender: Harness, target: Harness) -> bool:
        """Whether ``sender`` is a lead of some team that contains ``target``."""
        conv_leads = self._conversation_leads()
        return any(
            target.agent_id in team.agent_ids
            and self._is_team_lead(sender.agent_id, team, conv_leads)
            for team in self.teams.list()
        )

    def _verify_remote_dir(self, host: str, path: str) -> None:
        """Confirm over SSH that ``path`` is an accessible directory on ``host``."""

        try:
            cfg = settings.host(host)
        except settings.SettingsError as exc:
            raise TeamError(str(exc)) from exc
        proc = ssh_tmux_backend.SshTmuxBackend(cfg)._ssh_run(
            'sh -c \'[ -d "$1" ] && [ -x "$1" ]\' sh ' + _shq(path), timeout=20.0
        )
        if proc.returncode == 255:
            raise TeamError(f"host {host} is unreachable: cannot confirm the workspace {path}")
        if proc.returncode != 0:
            raise TeamError(f"workspace does not exist or is not accessible on {host}: {path}")

    def _team_ids_of(self, agent_id: str) -> list[str]:
        return [t.team_id for t in self.teams.list() if agent_id in t.agent_ids]

    def _team_names_of(self, agent_id: str) -> list[str]:
        return [t.name for t in self.teams.list() if agent_id in t.agent_ids]

    def _shared_team(self, a: str, b: str) -> bool:
        return bool(set(self._team_ids_of(a)) & set(self._team_ids_of(b)))

    def _team_members(self, team: str | None) -> set[str]:
        if not team:
            return set()
        try:
            return set(self.teams.get(team).agent_ids)
        except Exception:  # noqa: BLE001 - a unknown team is simply empty
            return set()

    def _team_or_none(self, team: str | None) -> Team | None:
        if not team:
            return None
        try:
            return self.teams.get(team)
        except Exception:  # noqa: BLE001 - unknown team: the caller reports it later
            return None

    def host_status(self) -> list[dict[str, Any]]:
        """Configured hosts with what is *observed* about them (``unknown`` if nothing)."""
        links = self.remote_links.states()
        out: list[dict[str, Any]] = []
        for name, cfg in settings.hosts().items():
            mine = [h for h in self.agents.all() if self._agent_hosts.get(h.agent_id) == name]
            states = [self._agent_host_state(h) for h in mine]
            health = self._host_health.get(name)
            tunnel = links.get(name) if cfg.get("tunnel") else None
            if "unreachable" in states:
                state = "unreachable"
            elif tunnel == "reconnecting" or "reconnecting" in states:
                state = "reconnecting"
            elif mine:
                state = "ok"
            elif health is not None:
                state = "ok" if health[0] else "unreachable"
            else:
                state = "unknown"
            out.append({
                "name": name, "ssh": cfg["ssh"], "port": cfg["port"],
                "identity": cfg.get("identity"), "known_hosts": cfg.get("known_hosts"),
                "tmux_socket": cfg.get("tmux_socket"),
                "tunnel": bool(cfg.get("tunnel")), "tunnel_state": tunnel,
                "state": state, "agents": len(mine),
                "checked_at": health[1] if health else None,
            })
        return out

    def _live_agents_on(self, name: str) -> list[str]:
        return [h.name or h.agent_id for h in self.agents.all()
                if self._agent_hosts.get(h.agent_id) == name]

    def _agent_host_state(self, harness: Harness) -> str | None:
        """``ok`` / ``unreachable`` / ``reconnecting`` for a remote agent (None = local).

        Passive: derived from the last SSH outcome the backend saw and the tunnel
        supervisor, never from a new network call (the listing is polled).
        """
        host = self._agent_hosts.get(harness.agent_id)
        if not host:
            return None
        if harness.session.backend.meta().get("host_unreachable"):
            return "unreachable"
        if self.remote_links.states().get(host) == "reconnecting":
            return "reconnecting"
        return "ok"

    def host_set(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Add or edit a host (validated like the settings file)."""
        existing = settings.hosts()
        if name in existing:
            busy = self._live_agents_on(name)
            if busy:
                raise ValueError(
                    f"host {name!r} has agents ({', '.join(busy)}): delete them before editing it"
                )
        table = {**existing, name: {k: v for k, v in body.items() if k != "name"}}
        try:
            settings.patch({"hosts": table})
        except settings.SettingsError as exc:
            raise ValueError(str(exc)) from exc
        self.remote_links.stop(name)  # the next agent re-opens it with the new config
        return next(h for h in self.host_status() if h["name"] == name)

    def host_remove(self, name: str) -> dict[str, Any]:
        existing = settings.hosts()
        if name not in existing:
            raise ValueError(f"unknown host {name!r}")
        busy = self._live_agents_on(name)
        if busy:
            raise ValueError(f"host {name!r} has agents ({', '.join(busy)}): delete them first")
        existing.pop(name)
        settings.patch({"hosts": existing})
        self.remote_links.stop(name)
        self._host_health.pop(name, None)
        return {"removed": name}

    def host_test(self, name: str) -> dict[str, Any]:
        """One short SSH round trip: can we connect, and what is installed there?"""
        cfg = settings.host(name)
        script = ('echo crewhall-ok; command -v tmux >/dev/null 2>&1 && echo tmux=1; '
                  'command -v crewhall >/dev/null 2>&1 && echo crewhall=1; '
                  'command -v git >/dev/null 2>&1 && echo git=1; '
                  'command -v claude >/dev/null 2>&1 && echo claude=1; '
                  'command -v opencode >/dev/null 2>&1 && echo opencode=1; exit 0')
        proc = ssh_tmux_backend.SshTmuxBackend(cfg)._ssh_run(script, timeout=20.0)
        self._host_health[name] = (proc.returncode == 0, time.time())
        if proc.returncode != 0 or "crewhall-ok" not in proc.stdout:
            err = (proc.stderr or "").strip().splitlines()
            detail = err[-1][:200] if err else f"ssh exit {proc.returncode}"
            return {"ok": False, "error": _ssh_hint(cfg, detail), "detail": detail}
        found = {line.split("=")[0] for line in proc.stdout.splitlines() if line.endswith("=1")}
        return {"ok": True, "tmux": "tmux" in found, "crewhall": "crewhall" in found,
                "git": "git" in found, "claude": "claude" in found,
                "opencode": "opencode" in found}

    def agent_identity(self, target: str, token: str | None) -> dict[str, Any]:
        harness = self.agents.resolve(target)
        self._check_token(harness.agent_id, token)
        team_names = self._team_names_of(harness.agent_id)
        teammates = []
        seen = set()
        for team in self.teams.list():
            if harness.agent_id not in team.agent_ids:
                continue
            for other_id in team.agent_ids:
                if other_id == harness.agent_id or other_id in seen:
                    continue
                seen.add(other_id)
                try:
                    info = self.agents.resolve(other_id).info()
                except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                    teammates.append({"agent_id": other_id, "name": other_id,
                                      "kind": "?", "state": "missing"})
                    continue
                teammates.append({
                    "agent_id": info.agent_id, "name": info.name or "",
                    "kind": info.kind, "state": info.state.value,
                })
        return {
            "agent": self.agent_summary(harness),
            "teams": team_names,
            "teammates": teammates,
        }

    def teammates(self, target: str) -> list[dict[str, Any]]:
        """Agents that share at least one Team with the target (no token needed)."""
        harness = self.agents.resolve(target)
        return self.agent_identity(harness.agent_id, self._agent_tokens.get(harness.agent_id))[
            "teammates"
        ]
