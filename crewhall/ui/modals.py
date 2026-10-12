"""Modal dialogs: their data, the open_* builders and key/submit handling."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import os
from dataclasses import dataclass, field
from typing import Any

from ..types import parse_agent_args


@dataclass
class ModalField:
    name: str
    label: str
    type: str = "text"  # text | select | checklist
    value: str = ""
    options: list[str] = field(default_factory=list)
    index: int = 0
    checked: list[bool] = field(default_factory=list)
    cursor: int = 0
    editable: bool = True


@dataclass
class Modal:
    kind: str
    title: str
    fields: list[ModalField] = field(default_factory=list)
    focus: int = 0
    error: str | None = None
    button: int = 1
    target: str | None = None
    action: str | None = None
    lines: list[str] = field(default_factory=list)
    index: int = 0


class UiModals:
    """The modal builders and handlers, mixed into :class:`AppModel`."""

    def open_create_agent(self, team_id: str | None = None) -> None:
        kinds = [h["kind"] for h in self.meta.get("harnesses", [])] or ["opencode"]
        backends = self.meta.get("backends", []) or ["pty"]
        bdef = backends.index("tmux") if "tmux" in backends else 0
        title = "NEW AGENT IN TEAM" if team_id else "NEW AGENT"
        hosts = [h["name"] for h in self.meta.get("hosts", [])]
        team = next((t for t in self.teams if t["team_id"] == team_id), None) if team_id else None
        team_host = (team or {}).get("host")
        if team_host and team_host not in hosts:
            hosts = [*hosts, team_host]
        # A remote team fixes the host: its agents run there, in its directory.
        host_index = (hosts.index(team_host) + 1) if team_host else 0
        host_field = (
            [ModalField("host", "Run on (a host = directory on that machine)", "select",
                        options=["(this machine)", *hosts], index=host_index)]
            if hosts else []
        )
        self._new_modal(
            "create_agent",
            title,
            target=team_id,
            fields=[
                ModalField("name", "Name", "text", ""),
                ModalField("kind", "Agent type", "select", options=kinds, index=0),
                ModalField("backend", "Backend", "select", options=backends, index=bdef),
                *host_field,
                ModalField("cwd", "Working directory", "text",
                           (team or {}).get("workspace") or "" if team_host else self.cwd),
                ModalField("args", "Command arguments (optional)", "text", ""),
            ],
            button=0,
        )

    def open_create_team(self) -> None:
        self._new_modal(
            "create_team",
            "NEW TEAM",
            fields=[ModalField("name", "Name", "text", "")],
            button=0,
        )

    def open_add_members(self) -> None:
        team = self.selected_team()
        if team is None:
            self.status = "select a team first"
            self.status_kind = "warning"
            return
        member_ids = {m["agent_id"] for m in team["members"]}
        names = [str(a.get("name") or a["agent_id"]) for a in self.agents
                 if a["agent_id"] not in member_ids]
        self._new_modal(
            "add_members",
            f"ADD MEMBERS \u00b7 {team['name']}",
            target=team["team_id"],
            fields=[
                ModalField("members", "Agents", "checklist", options=names,
                           checked=[False] * len(names), cursor=0)
            ],
            button=0,
        )

    def open_remove_members(self) -> None:
        team = self.selected_team()
        if team is None:
            self.status = "select a team first"
            self.status_kind = "warning"
            return
        names = [m.get("name") for m in team["members"]]
        self._new_modal(
            "remove_members",
            f"REMOVE MEMBERS \u00b7 {team['name']}",
            target=team["team_id"],
            fields=[
                ModalField("members", "Members", "checklist", options=names,
                           checked=[False] * len(names), cursor=0)
            ],
            button=0,
        )

    def open_message(self) -> None:
        agent = self.selected_agent()
        if agent is None:
            self.status = "select an agent to send from"
            self.status_kind = "warning"
            return
        sender = agent.get("name") or agent["agent_id"]
        recips = [n for n in self.agent_names() if n != sender]
        if not recips:
            self.status = "no other agent to send to"
            self.status_kind = "warning"
            return
        self._new_modal(
            "message",
            "SEND MESSAGE",
            fields=[
                ModalField("from", "From", "text", sender, editable=False),
                ModalField("to", "To", "select", options=recips),
                ModalField("body", "Message", "text", ""),
            ],
            target=sender,
            button=0,
        )

    def open_confirm(self, action: str, target: str, title: str, lines: list[str]) -> None:
        self._new_modal("confirm", title, target=target, action=action, lines=lines, button=1)

    def open_help(self) -> None:
        self._new_modal("help", "HELP", lines=HELP_LINES)

    def open_activity(self) -> None:
        self._new_modal("activity", "ACTIVITY", lines=self.activity_lines(), index=0)

    def open_agent_info(self) -> None:
        agent = self.selected_agent()
        if agent is None:
            self.status = "no agent selected"
            self.status_kind = "warning"
            return
        self._new_modal("agent_info", "AGENT INFO", lines=agent_info_lines(agent), index=0)

    def _modal_key(self, key: str) -> None:
        modal = self.modal
        assert modal is not None
        if modal.kind in ("help", "agent_info", "activity"):
            if key in ("esc", "enter", "q", "?"):
                self.close_modal()
            elif key == "up":
                modal.index = max(0, modal.index - 1)
            elif key == "down":
                modal.index += 1
            return
        if modal.kind == "palette":
            self._palette_key(key)
            return
        if modal.kind == "confirm":
            if key in ("esc", "n", "N"):
                self.close_modal()
            elif key in ("y", "Y"):
                self._confirm_yes()
            elif key in ("left", "right", "tab"):
                modal.button = 1 - modal.button
            elif key == "enter":
                if modal.button == 0:
                    self._confirm_yes()
                else:
                    self.close_modal()
            return
        if key == "esc":
            self.close_modal()
        elif key == "tab":
            modal.focus = (modal.focus + 1) % len(modal.fields)
        elif key == "enter":
            self._submit_modal()
        elif modal.fields:
            self._field_key(modal.fields[modal.focus], key)

    def _field_key(self, field: ModalField, key: str) -> None:
        if field.type == "text":
            if not field.editable:
                return
            if key == "backspace":
                field.value = field.value[:-1]
            elif len(key) == 1 and key.isprintable():
                field.value += key
        elif field.type == "select":
            if key in ("left", "up"):
                field.index = max(0, field.index - 1)
            elif key in ("right", "down"):
                field.index = min(max(0, len(field.options) - 1), field.index + 1)
        elif field.type == "checklist":
            if key == "up":
                field.cursor = max(0, field.cursor - 1)
            elif key == "down":
                field.cursor = min(max(0, len(field.options) - 1), field.cursor + 1)
            elif key in ("space", " ", "x") and field.options:
                field.checked[field.cursor] = not field.checked[field.cursor]

    def _confirm_yes(self) -> None:
        modal = self.modal
        assert modal is not None
        action, target = modal.action, modal.target
        self.close_modal()
        if action == "remove_agent" and target:
            self.remove_agent(target)
        elif action == "remove_team" and target:
            self.remove_team(target)

    def _option(self, field: ModalField) -> str | None:
        if not field.options:
            return None
        return field.options[min(field.index, len(field.options) - 1)]

    def _checked(self, field: ModalField) -> list[str]:
        return [o for o, c in zip(field.options, field.checked, strict=False) if c]

    def _submit_modal(self) -> None:
        modal = self.modal
        assert modal is not None
        kind = modal.kind
        if kind == "create_agent":
            fields = {f.name: f for f in modal.fields}
            name = fields["name"].value.strip()
            harness = self._option(fields["kind"]) or "opencode"
            backend = self._option(fields["backend"])
            host = self._option(fields["host"]) if "host" in fields else None
            host = None if host == "(this machine)" else host
            team_obj = next((t for t in self.teams if t["team_id"] == modal.target), None)
            if team_obj and team_obj.get("host"):
                if host and host != team_obj["host"]:
                    modal.error = f"team runs on {team_obj['host']}: pick that host"
                    return
                host = team_obj["host"]
            cwd = fields["cwd"].value.strip() or (None if host else self.cwd)
            extra = fields["args"].value.strip()
            if not name:
                modal.error = "name is required"
                return
            try:
                parse_agent_args(extra)
            except ValueError as exc:
                modal.error = str(exc)
                return
            if name in self.agent_names():
                modal.error = f'agent "{name}" already exists'
                return
            if not host and cwd and not os.path.isdir(os.path.expanduser(cwd)):
                modal.error = "working directory does not exist"
                return
            team = modal.target
            self.close_modal()
            self.create_agent(name, harness, backend, cwd, team=team, args=extra or None, host=host)
        elif kind == "create_team":
            name = modal.fields[0].value.strip()
            if not name:
                modal.error = "name is required"
                return
            self.close_modal()
            self.create_team(name, [])
        elif kind == "add_members":
            field = modal.fields[0]
            team = modal.target
            if team is None:
                return
            if not field.options:
                # No agents available: offer to create one in this team.
                self.close_modal()
                self.open_create_agent(team)
                return
            names = self._checked(field)
            self.close_modal()
            self.add_members(team, names)
        elif kind == "remove_members":
            names = self._checked(modal.fields[0])
            team = modal.target
            if team is None:
                return
            self.close_modal()
            self.remove_members(team, names)
        elif kind == "message":
            fields = {f.name: f for f in modal.fields}
            body = fields["body"].value.strip()
            to = self._option(fields["to"])
            sender = modal.target or fields["from"].value
            if not to or not body:
                modal.error = "recipient and message are required"
                return
            self.close_modal()
            self.send_message(sender, to, body)


HELP_LINES = [
    "NAVIGATION",
    "  \u2191 \u2193        move selection (j / k)",
    "  Enter      team: expand/collapse \u00b7 agent: INTERACTIVE FOCUS",
    "  PgUp PgDn  scroll transcript",
    "  Home End   transcript top / follow",
    "  Esc        cancel / close modal",
    "",
    "AGENTS",
    "  n  New Agent (or New Agent in Team)   d  Delete Agent",
    "  Enter  Interactive Focus \u00b7 keyboard goes to the agent TUI",
    "",
    "INTERACTIVE FOCUS",
    "  type directly into the agent (Claude Code / OpenCode)",
    "  Esc, arrows, Ctrl+B/C/D, Tab, /slash commands -> the agent",
    "  Ctrl+Alt+K   view next agent   (CREWHALL_NEXT_SEQUENCE)",
    "  Ctrl+Alt+J   view previous agent (CREWHALL_PREV_SEQUENCE)",
    "  Ctrl+Alt+B   return to Navigator (CREWHALL_EXIT_SEQUENCE)",
    "",
    "TEAMS",
    "  t  New Team           a  Add Members",
    "  x  Remove Members     n  New Agent in Team",
    "  R  Delete Team",
    "",
    "MESSAGING",
    "  m  Send Message (from selected agent)",
    "",
    "APPLICATION",
    "  Ctrl+P  Command Palette (Navigator only)   e  Activity",
    "  ?  Help                                    q  Quit",
]


def agent_info_lines(agent: dict[str, Any]) -> list[str]:
    return [
        f"name      {agent.get('name') or '-'}",
        f"id        {agent['agent_id']}",
        f"harness   {agent['kind']}",
        f"backend   {agent['backend']}",
        f"state     {agent['state']}",
        f"pid       {agent.get('pid')}",
        f"cwd       {agent.get('cwd') or '-'}",
        f"evidence  {agent.get('evidence', '')}",
    ]
