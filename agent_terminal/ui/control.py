from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..backends import available as available_backends
from ..client import Client, ensure_daemon
from ..controller import Controller
from ..harness import available_harnesses, get_harness


def meta_info() -> dict[str, Any]:
    """Harnesses and backends actually available in this environment."""
    from .. import settings

    enabled = settings.enabled_kinds()
    return {
        # Only enabled providers can start new agents (Settings -> Providers).
        "harnesses": [
            {"kind": kind, "command": settings.provider_command(kind) or get_harness(kind).command()}
            for kind in available_harnesses() if kind in enabled
        ],
        "disabled": [k for k in available_harnesses() if k not in enabled],
        "backends": available_backends(),
        "defaults": {"kind": settings.get("agents.default_kind"),
                     "backend": settings.get("agents.default_backend")},
    }


@runtime_checkable
class ControlPort(Protocol):
    """The only surface the UI is allowed to use.

    Implemented by the daemon RPC client (normal use) and by an in-process
    Controller wrapper (tests / offline). The UI never talks to tmux, a PTY,
    a Harness or an InteractiveSession directly.
    """

    def meta(self) -> dict[str, Any]: ...
    def list_agents(self) -> list[dict[str, Any]]: ...
    def list_teams(self) -> list[dict[str, Any]]: ...
    def create_agent(
        self,
        kind: str,
        name: str,
        backend: str | None,
        cwd: str | None,
        team: str | None = None,
        args: str | None = None,
    ) -> dict[str, Any]: ...
    def remove_agent(self, target: str) -> dict[str, Any]: ...
    def capture(self, target: str, recent: bool, max_lines: int) -> str: ...
    def send_text(self, target: str, text: str) -> None: ...
    def send_key(self, target: str, key: str) -> None: ...
    def resize(self, target: str, cols: int, rows: int) -> None: ...
    def send_prompt(
        self, target: str, prompt: str, timeout: float
    ) -> dict[str, Any]: ...
    def create_team(
        self, name: str, agent_ids: list[str], workspace: str | None = None
    ) -> dict[str, Any]: ...
    def set_team_workspace(self, team: str, workspace: str | None) -> dict[str, Any]: ...
    def add_member(self, team: str, agent: str) -> dict[str, Any]: ...
    def remove_member(self, team: str, agent: str) -> dict[str, Any]: ...
    def remove_team(self, team: str) -> dict[str, Any]: ...
    def send_message(
        self, sender: str, recipient: str, body: str
    ) -> dict[str, Any]: ...
    def message_history(
        self, agent: str | None, limit: int | None
    ) -> list[dict[str, Any]]: ...


class DaemonControl:
    """ControlPort backed by the resident daemon (the real Controller)."""

    def __init__(self, socket_path: str | None = None) -> None:
        ensure_daemon(socket_path)
        self.client = Client(socket_path=socket_path, autostart=False)

    def meta(self) -> dict[str, Any]:
        return self.client.call("meta_info")

    def list_agents(self) -> list[dict[str, Any]]:
        return self.client.call("agent_list")["agents"]

    def list_teams(self) -> list[dict[str, Any]]:
        return self.client.call("team_list")["teams"]

    def create_agent(self, kind, name, backend=None, cwd=None, team=None, args=None):
        return self.client.call(
            "agent_create", kind=kind, name=name, backend=backend, cwd=cwd,
            team=team, args=args,
        )["agent"]

    def remove_agent(self, target: str) -> dict[str, Any]:
        return self.client.call("agent_stop", target=target)["agent"]

    def capture(self, target: str, recent: bool = True, max_lines: int = 200) -> str:
        return self.client.call(
            "agent_capture", target=target, recent=recent, max_lines=max_lines
        )["output"]

    def send_text(self, target, text):
        self.client.call("agent_write", target=target, text=text)

    def send_key(self, target, key):
        self.client.call("agent_key", target=target, key=key)

    def resize(self, target, cols, rows):
        self.client.call("agent_resize", target=target, cols=cols, rows=rows)

    def send_prompt(self, target, prompt, timeout=30.0):
        return self.client.call(
            "agent_send", target=target, prompt=prompt, timeout=timeout
        )["agent"]

    def create_team(self, name, agent_ids, workspace=None):
        return self.client.call(
            "team_create", name=name, agent_ids=agent_ids, workspace=workspace
        )["team"]

    def set_team_workspace(self, team, workspace):
        return self.client.call(
            "team_set_workspace", target=team, workspace=workspace
        )["team"]

    def add_member(self, team, agent):
        return self.client.call(
            "team_add_member", target=team, agent=agent
        )["team"]

    def remove_member(self, team, agent):
        return self.client.call(
            "team_remove_member", target=team, agent=agent
        )["team"]

    def remove_team(self, team):
        return self.client.call("team_remove", target=team)["team"]

    def send_message(self, sender, recipient, body):
        return self.client.call(
            "message_send", sender=sender, recipient=recipient, body=body
        )["delivery"]

    def message_history(self, agent=None, limit=None):
        return self.client.call(
            "message_history", agent=agent, limit=limit
        )["messages"]


class LocalControl:
    """ControlPort backed by an in-process Controller (tests / offline demo)."""

    def __init__(self, controller: Controller) -> None:
        self.controller = controller

    def meta(self) -> dict[str, Any]:
        return meta_info()

    def list_agents(self) -> list[dict[str, Any]]:
        return self.controller.list_agents()

    def list_teams(self) -> list[dict[str, Any]]:
        return self.controller.list_teams()

    def create_agent(self, kind, name, backend=None, cwd=None, team=None, args=None):
        return self.controller.agent_summary(
            self.controller.create_agent(
                kind, name=name, backend=backend, cwd=cwd, team=team, args=args
            )
        )

    def remove_agent(self, target: str) -> dict[str, Any]:
        return self.controller.remove_agent(target).to_dict()

    def capture(self, target: str, recent: bool = True, max_lines: int = 200) -> str:
        harness = self.controller.get_agent(target)
        return (
            harness.capture_recent(max_lines=max_lines)
            if recent
            else harness.capture()
        )

    def send_text(self, target, text):
        self.controller.get_agent(target).write_raw(text)

    def send_key(self, target, key):
        self.controller.get_agent(target).send_key(key)

    def resize(self, target, cols, rows):
        self.controller.get_agent(target).resize(cols, rows)

    def send_prompt(self, target, prompt, timeout=30.0):
        harness = self.controller.get_agent(target)
        harness.send(prompt, timeout=timeout)
        return self.controller.agent_summary(harness)

    def create_team(self, name, agent_ids, workspace=None):
        team = self.controller.create_team(name, agent_ids, workspace=workspace)
        return self.controller.team_info(team.team_id)

    def set_team_workspace(self, team, workspace):
        t = self.controller.set_team_workspace(team, workspace)
        return self.controller.team_info(t.team_id)

    def add_member(self, team, agent):
        t = self.controller.add_team_member(team, agent)
        return self.controller.team_info(t.team_id)

    def remove_member(self, team, agent):
        t = self.controller.remove_team_member(team, agent)
        return self.controller.team_info(t.team_id)

    def remove_team(self, team):
        return self.controller.remove_team(team).to_dict()

    def send_message(self, sender, recipient, body):
        return self.controller.send_message(sender, recipient, body).to_dict()

    def message_history(self, agent=None, limit=None):
        return self.controller.message_history(agent=agent, limit=limit)
