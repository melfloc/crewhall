from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from .. import brand
from ..types import parse_agent_args
from .control import ControlPort

HEADERS = {"header"}

STATE_SYMBOL = {
    "starting": "\u25cb",       # ○
    "ready": "\u25cf",          # ●
    "working": "\u25c9",        # ◉
    "waiting_input": "\u25c9",  # ◉ (subdued)
    "unknown": "?",
    "exited": "\u00d7",         # ×
    "error": "!",
}
STATE_WORD = {
    "starting": "STARTING",
    "ready": "READY",
    "working": "WORKING",
    "waiting_input": "WAITING",
    "unknown": "UNKNOWN",
    "exited": "EXITED",
    "error": "ERROR",
}
BACKEND_DESC = {"tmux": "persistent / attachable", "pty": "native / ephemeral"}

# States where the agent can no longer receive input (auto-leave Interactive Focus).
TERMINAL_STATES = {"exited", "error"}

# UI key name -> backend key name (keys.py canonical) for Interactive Focus.
# Ctrl+B is streamed to the agent (it is tmux's default prefix; crewhall
# must not consume it). The exit key is Ctrl+Alt+B (ESC + Ctrl+B).
KEY_FORWARD = {
    "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
    "home": "HOME", "end": "END", "pgup": "PAGE_UP", "pgdn": "PAGE_DOWN",
    "tab": "TAB", "enter": "ENTER", "backspace": "BACKSPACE", "delete": "DELETE",
    "esc": "ESC", "ctrl_b": "CTRL_B", "ctrl_c": "CTRL_C", "ctrl_d": "CTRL_D",
    "ctrl_p": "CTRL_P",
}

# Ctrl+Alt+B is delivered by terminals/tmux as ESC followed by Ctrl+B (0x02).
DEFAULT_EXIT_SEQUENCE = b"\x1b\x02"
# Ctrl+Alt+J / Ctrl+Alt+K are ESC followed by the letter.
DEFAULT_PREV_SEQUENCE = b"\x1bJ"
DEFAULT_NEXT_SEQUENCE = b"\x1bK"

_CONTROL_CHARS = {2: "ctrl_b", 3: "ctrl_c", 4: "ctrl_d", 16: "ctrl_p"}


def _env_sequence(name: str, default: bytes) -> bytes:
    raw = brand.env(name)
    if not raw:
        return default
    try:
        return raw.encode("utf-8").decode("unicode_escape").encode("latin-1")
    except Exception:  # noqa: BLE001
        return default


def exit_sequence() -> bytes:
    return _env_sequence("EXIT_SEQUENCE", DEFAULT_EXIT_SEQUENCE)


def prev_sequence() -> bytes:
    return _env_sequence("PREV_SEQUENCE", DEFAULT_PREV_SEQUENCE)


def next_sequence() -> bytes:
    return _env_sequence("NEXT_SEQUENCE", DEFAULT_NEXT_SEQUENCE)


def translate_sequence(data: bytes) -> list[str]:
    """Map a raw byte sequence to UI keys.

    Special Ctrl+Alt combinations become named UI actions
    (``exit_interactive`` / ``interactive_prev`` / ``interactive_next``);
    everything else is forwarded verbatim (ESC included).
    """
    if not data:
        return []
    for seq, name in (
        (exit_sequence(), "exit_interactive"),
        (prev_sequence(), "interactive_prev"),
        (next_sequence(), "interactive_next"),
    ):
        if seq and data[: len(seq)] == seq:
            rest = data[len(seq):]
            return [name, *[_byte_key(b) for b in rest if _byte_key(b)]]
    return [k for k in (_byte_key(b) for b in data) if k]


def _byte_key(byte: int) -> str | None:
    if byte == 27:
        return "esc"
    if byte == 9:
        return "tab"
    if byte in (10, 13):
        return "enter"
    if byte in (8, 127):
        return "backspace"
    if byte in _CONTROL_CHARS:
        return _CONTROL_CHARS[byte]
    if byte == 0:
        return "ctrl_space"
    if 32 <= byte < 127:
        return chr(byte)
    return None

# (label, action) — order defines palette order.
COMMANDS: list[tuple[str, str]] = [
    ("New Agent", "create_agent"),
    ("New Team", "create_team"),
    ("Add Members", "add_members"),
    ("Remove Members", "remove_members"),
    ("Send Message", "message"),
    ("Delete Agent", "delete_agent"),
    ("Delete Team", "delete_team"),
    ("Agent Info", "agent_info"),
    ("Agent Activity", "activity"),
    ("Refresh", "refresh"),
    ("Help", "help"),
    ("Quit", "quit"),
]
CONTEXT_COMMANDS = {
    "add_members",
    "remove_members",
    "delete_team",
    "message",
    "delete_agent",
    "agent_info",
}


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


class TaskRunner:
    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def submit(self, label: str, fn: Callable[[], Any]) -> None:
        def work() -> None:
            try:
                self._queue.put((label, fn(), None))
            except Exception as exc:  # noqa: BLE001
                self._queue.put((label, None, f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def drain(self) -> list[tuple[str, Any, str | None]]:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out


class AppModel:
    """Headless application state and behaviour on top of a ControlPort."""

    def __init__(
        self,
        control: ControlPort,
        *,
        cwd: str | None = None,
        async_ops: bool = False,
        activity_limit: int = 80,
    ) -> None:
        self.control = control
        self.cwd = cwd or os.getcwd()
        self.async_ops = async_ops
        self.activity_limit = activity_limit
        self.tasks = TaskRunner()

        self.meta: dict[str, Any] = {"harnesses": [], "backends": []}
        self.agents: list[dict[str, Any]] = []
        self.teams: list[dict[str, Any]] = []
        self.activity: list[dict[str, Any]] = []
        self.capture_cache: str = ""

        self.cursor = 0
        self.collapsed: set[str] = set()
        self.focus = "nav"  # nav | interactive
        self.modal: Modal | None = None
        self.status = "ready"
        self.status_kind = "info"
        self._error: str | None = None
        self.connected = True
        self.should_quit = False

        self.follow = True
        self.scroll = 0
        self._pending_select: tuple[str, str] | None = None
        self._pending_focus: str | None = None
        self._last_resize: tuple[str, int, int] | None = None

    # ---------------------------------------------------------------- lookups
    def agent_names(self) -> list[str]:
        return [a.get("name") or a["agent_id"] for a in self.agents]

    def team_names(self) -> list[str]:
        return [t["name"] for t in self.teams]

    def _member_ids(self) -> set[str]:
        ids: set[str] = set()
        for team in self.teams:
            for member in team["members"]:
                ids.add(member["agent_id"])
            ids.update(team["missing"])
        return ids

    def ungrouped_agents(self) -> list[dict[str, Any]]:
        member_ids = self._member_ids()
        return [a for a in self.agents if a["agent_id"] not in member_ids]

    def command_for_kind(self, kind: str) -> str:
        for harness in self.meta.get("harnesses", []):
            if harness.get("kind") == kind:
                return " ".join(harness.get("command", []) or [])
        return kind

    def state_symbol(self, state: str) -> str:
        return STATE_SYMBOL.get(state, "?")

    def state_word(self, state: str) -> str:
        return STATE_WORD.get(state, state.upper())

    # ------------------------------------------------------------------ rows
    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = [{"kind": "header", "label": "WORKSPACE"}]
        if self.teams:
            rows.append({"kind": "header", "label": "TEAMS"})
            for team in self.teams:
                collapsed = team["team_id"] in self.collapsed
                rows.append(
                    {
                        "kind": "team",
                        "key": team["team_id"],
                        "label": team["name"],
                        "team_id": team["team_id"],
                        "collapsed": collapsed,
                        "count": len(team["members"]),
                    }
                )
                if collapsed:
                    continue
                for member in team["members"]:
                    rows.append(self._agent_row(member, indent=1, team_id=team["team_id"]))
                for missing in team["missing"]:
                    rows.append(
                        {
                            "kind": "missing",
                            "key": missing,
                            "label": missing,
                            "team_id": team["team_id"],
                            "indent": 1,
                        }
                    )
            ungrouped = self.ungrouped_agents()
            if ungrouped:
                rows.append({"kind": "header", "label": "UNGROUPED"})
                for agent in ungrouped:
                    rows.append(self._agent_row(agent, indent=0))
        else:
            rows.append({"kind": "header", "label": "AGENTS"})
            for agent in self.agents:
                rows.append(self._agent_row(agent, indent=0))
        return rows

    def _agent_row(
        self, agent: dict[str, Any], indent: int, team_id: str | None = None
    ) -> dict[str, Any]:
        return {
            "kind": "agent",
            "key": agent["agent_id"],
            "label": agent.get("name") or agent["agent_id"],
            "name": agent.get("name") or agent["agent_id"],
            "agent_id": agent["agent_id"],
            "state": agent["state"],
            "harness": agent["kind"],
            "indent": indent,
            "team_id": team_id,
        }

    def current_row(self) -> dict[str, Any] | None:
        rows = self.rows()
        if not rows:
            return None
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        if rows[self.cursor]["kind"] in HEADERS:
            saved = self.cursor
            self.move(1)
            if self.rows()[self.cursor]["kind"] in HEADERS:
                self.move(-1)
            if self.rows()[self.cursor]["kind"] in HEADERS:
                self.cursor = saved
        row = self.rows()[self.cursor]
        return None if row["kind"] in HEADERS else row

    def selected_agent(self) -> dict[str, Any] | None:
        row = self.current_row()
        if not row or row["kind"] != "agent":
            return None
        agent_id = row["agent_id"]
        return next((a for a in self.agents if a["agent_id"] == agent_id), None)

    def selected_team(self) -> dict[str, Any] | None:
        row = self.current_row()
        if not row or row["kind"] != "team":
            return None
        return next((t for t in self.teams if t["team_id"] == row["team_id"]), None)

    def move(self, delta: int) -> None:
        rows = self.rows()
        index = self.cursor
        for _ in range(len(rows)):
            index += delta
            if index < 0 or index >= len(rows):
                return
            if rows[index]["kind"] not in HEADERS:
                self.cursor = index
                return

    def toggle_collapse(self) -> None:
        team = self.selected_team()
        if team is None:
            return
        tid = team["team_id"]
        if tid in self.collapsed:
            self.collapsed.discard(tid)
        else:
            self.collapsed.add(tid)

    # --------------------------------------------------------------- output
    def output_lines(self) -> list[str]:
        return self.capture_cache.splitlines()

    def output_slice(self, count: int) -> list[str]:
        if count <= 0:
            return []
        lines = self.output_lines()
        if self.follow:
            return lines[-count:]
        end = len(lines) - self.scroll
        if end <= 0:
            return lines[:count]
        start = max(0, end - count)
        return lines[start:end]

    def scroll_output(self, delta: int) -> None:
        lines = self.output_lines()
        if not lines:
            return
        self.follow = False
        self.scroll = max(0, min(self.scroll + delta, max(0, len(lines) - 1)))
        if self.scroll == 0:
            self.follow = True

    def output_home(self) -> None:
        self.follow = False
        self.scroll = len(self.output_lines())

    def output_end(self) -> None:
        self.follow = True
        self.scroll = 0

    # ------------------------------------------------------------ operations
    def _call(self, label: str, fn: Callable[[], Any]) -> None:
        self._error = None
        if self.async_ops:
            self.status = f"{label}\u2026"
            self.status_kind = "working"
            self.tasks.submit(label, fn)
            return
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self.status = f"{label} failed: {exc}"
            self.status_kind = "error"
            return
        self.status = f"{label} ok"
        self.status_kind = "success"

    @property
    def error(self) -> str | None:
        return self._error

    @error.setter
    def error(self, value: str | None) -> None:
        self._error = value

    def poll_tasks(self) -> None:
        for label, _result, error in self.tasks.drain():
            if error:
                self.status = f"{label} failed: {error}"
                self.status_kind = "error"
            else:
                self.status = f"{label} ok"
                self.status_kind = "success"

    def refresh(self) -> None:
        self.poll_tasks()
        try:
            if not self.meta.get("harnesses"):
                self.meta = self.control.meta()
            self.agents = self.control.list_agents()
            self.teams = self.control.list_teams()
            self.activity = self.control.message_history(limit=self.activity_limit)
            agent = self.selected_agent()
            self.capture_cache = (
                self.control.capture(agent["agent_id"], recent=True, max_lines=400)
                if agent is not None
                else ""
            )
            if not self.connected:
                self.connected = True
                self.status = "reconnected"
                self.status_kind = "success"
            self._apply_pending_select()
            self._check_interactive_focus()
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            self.status = f"daemon unavailable: {exc}"
            self.status_kind = "error"

    def _check_interactive_focus(self) -> None:
        """Leave Interactive Focus if the selected agent can no longer take input."""
        if self.focus != "interactive":
            return
        agent = self.selected_agent()
        if agent is None or agent["state"] in TERMINAL_STATES:
            self.focus = "nav"
            self._last_resize = None
            if agent is not None:
                self.status = f"{agent.get('name')} {agent['state']}"
                self.status_kind = "warning" if agent["state"] == "exited" else "error"

    def sync_viewport(self, cols: int, rows: int) -> None:
        """Propagate the real session viewport size to the focused agent."""
        if self.focus != "interactive":
            return
        agent = self.selected_agent()
        if agent is None or agent["state"] in TERMINAL_STATES:
            return
        key = (agent["agent_id"], cols, rows)
        if key == self._last_resize:
            return
        try:
            self.control.resize(agent["agent_id"], cols, rows)
            self._last_resize = key
        except Exception:  # noqa: BLE001 - never crash the UI on a resize race
            pass

    def _apply_pending_select(self) -> None:
        if not self._pending_select:
            return
        kind, name = self._pending_select
        for index, row in enumerate(self.rows()):
            if row["kind"] == kind and row.get("label") == name:
                self.cursor = index
                self._pending_select = None
                if self._pending_focus:
                    self.focus = self._pending_focus
                    self._pending_focus = None
                return

    def retry(self) -> None:
        self.refresh()

    # -- direct operations --------------------------------------------------
    def create_agent(
        self,
        name: str,
        kind: str,
        backend: str | None = None,
        cwd: str | None = None,
        team: str | None = None,
        args: str | None = None,
        host: str | None = None,
    ) -> None:
        self._pending_select = ("agent", name)
        self._pending_focus = "interactive"

        def op() -> None:
            self.control.create_agent(
                kind, name, backend, cwd if host else (cwd or self.cwd),
                team=team, args=args or None, host=host,
            )

        label = f"create {name}" + (" in team" if team else "") + (f" on {host}" if host else "")
        self._call(label, op)

    def create_team(self, name: str, members: list[str] | None = None) -> None:
        self._pending_select = ("team", name)
        self._pending_focus = None
        self._call(f"create team {name}", lambda: self.control.create_team(name, members or []))

    def add_members(self, team: str, agents: list[str]) -> None:
        if not agents:
            self.status = "no agents selected"
            self.status_kind = "warning"
            return
        self._call(
            f"add {len(agents)} member(s)",
            lambda: [self.control.add_member(team, a) for a in agents],
        )

    def remove_members(self, team: str, agents: list[str]) -> None:
        if not agents:
            self.status = "no members selected"
            self.status_kind = "warning"
            return
        self._call(
            f"remove {len(agents)} member(s)",
            lambda: [self.control.remove_member(team, a) for a in agents],
        )

    def remove_team(self, team: str) -> None:
        self._call(f"remove team {team}", lambda: self.control.remove_team(team))

    def remove_agent(self, target: str) -> None:
        self._call(f"remove agent {target}", lambda: self.control.remove_agent(target))

    def send_message(self, sender: str, recipient: str, body: str) -> None:
        self._call(
            f"message {sender}\u2192{recipient}",
            lambda: self.control.send_message(sender, recipient, body),
        )

    def send_to_selected(self, text: str) -> None:
        agent = self.selected_agent()
        if agent is None:
            self.status = "no agent selected"
            self.status_kind = "error"
            return
        name = agent.get("name") or agent["agent_id"]
        self._call(
            f"send to {name}",
            lambda: self.control.send_prompt(agent["agent_id"], text, 30.0),
        )

    # -------------------------------------------------------------- modals
    def close_modal(self) -> None:
        self.modal = None

    def _new_modal(self, kind: str, title: str, **kwargs: Any) -> None:
        self.modal = Modal(kind=kind, title=title, **kwargs)

    def open_create_agent(self, team_id: str | None = None) -> None:
        kinds = [h["kind"] for h in self.meta.get("harnesses", [])] or ["opencode"]
        backends = self.meta.get("backends", []) or ["pty"]
        bdef = backends.index("tmux") if "tmux" in backends else 0
        title = "NEW AGENT IN TEAM" if team_id else "NEW AGENT"
        hosts = [h["name"] for h in self.meta.get("hosts", [])]
        host_field = (
            [ModalField("host", "Run on (a host = directory on that machine)", "select",
                        options=["(this machine)", *hosts], index=0)]
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
                ModalField("cwd", "Working directory", "text", self.cwd),
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
        names = [a.get("name") for a in self.agents if a["agent_id"] not in member_ids]
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

    def open_palette(self) -> None:
        self._new_modal(
            "palette", "COMMAND PALETTE",
            fields=[ModalField("query", ">", "text", "")], button=0,
        )

    def activity_lines(self) -> list[str]:
        lines: list[str] = []
        for message in self.activity:
            sender = message.get("sender_name") or message["sender"]
            recipient = message.get("recipient_name") or message["recipient"]
            mark = "\u2713" if message.get("delivered") else "\u2717"
            lines.append(f"{mark}  {sender} \u2192 {recipient}")
            lines.append(f"     {message['body']}")
        return lines or ["(no activity yet)"]

    def available_commands(self) -> list[tuple[str, str]]:
        agent = self.selected_agent()
        team = self.selected_team()
        out: list[tuple[str, str]] = []
        for label, action in COMMANDS:
            if action in CONTEXT_COMMANDS:
                if action in ("add_members", "remove_members", "delete_team") and not team:
                    continue
                if action in ("message", "delete_agent", "agent_info") and not agent:
                    continue
            out.append((label, action))
        return out

    def filtered_commands(self) -> list[tuple[str, str]]:
        query = self.modal.fields[0].value.lower() if self.modal else ""
        return [c for c in self.available_commands() if query in c[0].lower()]

    # ------------------------------------------------------------ key routes
    def handle_key(self, key: str) -> None:
        if self.modal is not None:
            self._modal_key(key)
        elif self.focus == "interactive":
            self._interactive_key(key)
        else:
            self._nav_key(key)

    def _nav_key(self, key: str) -> None:
        if key in ("up", "k"):
            self.move(-1)
        elif key in ("down", "j"):
            self.move(1)
        elif key == "pgup":
            self.scroll_output(10)
        elif key == "pgdn":
            self.scroll_output(-10)
        elif key == "home":
            self.output_home()
        elif key == "end":
            self.output_end()
        elif key == "enter":
            if self.selected_team() is not None:
                self.toggle_collapse()
            else:
                agent = self.selected_agent()
                if agent is not None and agent["state"] in TERMINAL_STATES:
                    self.open_agent_info()
                elif agent is not None:
                    self._enter_interactive()
        elif key == "i":
            agent = self.selected_agent()
            if agent is not None and agent["state"] not in TERMINAL_STATES:
                self._enter_interactive()
        elif key == "n":
            team = self.selected_team()
            self.open_create_agent(team["team_id"] if team else None)
        elif key == "t":
            self.open_create_team()
        elif key == "a":
            self.open_add_members()
        elif key == "x":
            self.open_remove_members()
        elif key == "m":
            self.open_message()
        elif key == "d":
            agent = self.selected_agent()
            if agent is not None:
                name = agent.get("name") or agent["agent_id"]
                self.open_confirm(
                    "remove_agent", agent["agent_id"], f'DELETE AGENT "{name}"',
                    ["This STOPS the agent and removes it from the registry."],
                )
        elif key == "R":
            team = self.selected_team()
            if team is not None:
                self.open_confirm(
                    "remove_team", team["team_id"], f'DELETE TEAM "{team["name"]}"',
                    ["Agents will NOT be stopped."],
                )
        elif key == "e":
            self.open_activity()
        elif key == "?":
            self.open_help()
        elif key == "ctrl_p":
            self.open_palette()
        elif key == "r" and not self.connected:
            self.retry()
        elif key in ("q", "ctrl_c"):
            self.should_quit = True

    def _enter_interactive(self) -> None:
        self.focus = "interactive"
        self._last_resize = None

    def _interactive_key(self, key: str) -> None:
        # Agent Interactive Focus: the agent's own TUI owns the keyboard.
        # Only the explicit Ctrl+Alt combinations control the UI itself.
        if key == "exit_interactive":
            self.focus = "nav"
            self._last_resize = None
            return
        if key == "interactive_prev":
            self.select_agent_by_offset(-1)
            return
        if key == "interactive_next":
            self.select_agent_by_offset(1)
            return
        self._forward(key)

    def select_agent_by_offset(self, delta: int) -> None:
        """Move the visual selection to another agent without leaving Interactive
        Focus. Only changes what is *viewed*: it never stops, restarts, or sends
        input to any agent.
        """
        agent_rows = [r for r in self.rows() if r["kind"] == "agent"]
        if not agent_rows:
            return
        current = self.selected_agent()
        current_id = current["agent_id"] if current else None
        offsets = [r["agent_id"] for r in agent_rows]
        try:
            idx = offsets.index(current_id)
        except ValueError:
            idx = 0
        new_idx = (idx + delta) % len(offsets)
        target_id = offsets[new_idx]
        for i, row in enumerate(self.rows()):
            if row["kind"] == "agent" and row["agent_id"] == target_id:
                self.cursor = i
                break
        self._last_resize = None
        self.follow = True
        self.scroll = 0
        name = next(
            (a.get("name") for a in self.agents if a["agent_id"] == target_id),
            target_id,
        )
        self.status = f"viewing {name}"
        self.status_kind = "info"
        try:
            self.refresh()
        except Exception:  # noqa: BLE001
            pass

    def _forward(self, key: str) -> None:
        agent = self.selected_agent()
        if agent is None or agent["state"] in TERMINAL_STATES:
            self.focus = "nav"
            return
        target = agent["agent_id"]
        try:
            if key in KEY_FORWARD:
                self.control.send_key(target, KEY_FORWARD[key])
            elif len(key) == 1 and key.isprintable():
                self.control.send_text(target, key)
        except Exception as exc:  # noqa: BLE001
            self.status = f"input failed: {exc}"
            self.status_kind = "error"

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

    def _palette_key(self, key: str) -> None:
        modal = self.modal
        assert modal is not None
        if key == "esc":
            self.close_modal()
        elif key == "up":
            modal.index = max(0, modal.index - 1)
        elif key == "down":
            modal.index += 1
        elif key == "backspace":
            modal.fields[0].value = modal.fields[0].value[:-1]
            modal.index = 0
        elif key == "enter":
            self._run_palette()
        elif len(key) == 1 and key.isprintable():
            modal.fields[0].value += key
            modal.index = 0

    def _run_palette(self) -> None:
        assert self.modal is not None
        commands = self.filtered_commands()
        if not commands:
            return
        index = min(self.modal.index, len(commands) - 1)
        action = commands[index][1]
        self.close_modal()
        self.run_command(action)

    def run_command(self, action: str) -> None:
        if action == "create_agent":
            team = self.selected_team()
            self.open_create_agent(team["team_id"] if team else None)
        elif action == "create_team":
            self.open_create_team()
        elif action == "add_members":
            self.open_add_members()
        elif action == "remove_members":
            self.open_remove_members()
        elif action == "message":
            self.open_message()
        elif action == "delete_agent":
            self._nav_key("d")
        elif action == "delete_team":
            self._nav_key("R")
        elif action == "agent_info":
            self.open_agent_info()
        elif action == "activity":
            self.open_activity()
        elif action == "refresh":
            self.refresh()
            self.status = "refreshed"
            self.status_kind = "info"
        elif action == "help":
            self.open_help()
        elif action == "quit":
            self.should_quit = True

    def _confirm_yes(self) -> None:
        modal = self.modal
        assert modal is not None
        action, target = modal.action, modal.target
        self.close_modal()
        if action == "remove_agent":
            self.remove_agent(target)
        elif action == "remove_team":
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
            if not host and not os.path.isdir(os.path.expanduser(cwd)):
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

    # ------------------------------------------------------------- footer
    def footer_hints(self) -> list[str]:
        if self.modal is not None:
            kind = self.modal.kind
            if kind in ("add_members", "remove_members"):
                return ["\u2191\u2193 Navigate", "Space Select", "Enter Apply", "Esc Cancel"]
            if kind == "palette":
                return ["Type Filter", "\u2191\u2193 Select", "Enter Run", "Esc Cancel"]
            if kind in ("help", "activity", "agent_info"):
                return ["\u2191\u2193 Scroll", "Esc Close"]
            if kind == "confirm":
                return ["y Delete", "n Cancel", "Esc Cancel"]
            return ["Tab Next", "Enter Confirm", "Esc Cancel"]
        if self.focus == "interactive":
            return ["Ctrl+Alt+J/K Prev/Next", "Ctrl+Alt+B Navigator", "Agent input active"]
        if self.selected_team() is not None:
            return ["\u2191\u2193 Navigate", "Enter Expand", "a Add", "x Remove", "n New Agent", "R Delete", "? Help"]
        agent = self.selected_agent()
        if agent is not None:
            if agent["state"] in TERMINAL_STATES:
                return ["\u2191\u2193 Navigate", "d Delete", "? Help"]
            return ["\u2191\u2193 Navigate", "Enter Interactive", "m Message", "d Delete", "? Help"]
        return ["\u2191\u2193 Navigate", "n New Agent", "t New Team", "? Help"]


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
