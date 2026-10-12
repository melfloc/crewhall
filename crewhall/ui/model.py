from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Any
from collections.abc import Callable

from .control import ControlPort
from .keys import KEY_FORWARD
from .keys import (
    exit_sequence,
    next_sequence,
    prev_sequence,
    translate_sequence,
)
from .modals import HELP_LINES, Modal, ModalField, UiModals, agent_info_lines
from .palette import COMMANDS, CONTEXT_COMMANDS, UiPalette

__all__ = [
    "AppModel", "TaskRunner", "Modal", "ModalField",
    "COMMANDS", "CONTEXT_COMMANDS", "HELP_LINES", "agent_info_lines",
    "exit_sequence", "prev_sequence", "next_sequence", "translate_sequence",
    "KEY_FORWARD", "HEADERS", "STATE_SYMBOL", "STATE_WORD", "BACKEND_DESC",
    "TERMINAL_STATES",
]

log = logging.getLogger("crewhall.ui")

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


class AppModel(UiModals, UiPalette):
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
            pass  # noqa: BLE001 - justified boundary guard (B1)

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

    def activity_lines(self) -> list[str]:
        lines: list[str] = []
        for message in self.activity:
            sender = message.get("sender_name") or message["sender"]
            recipient = message.get("recipient_name") or message["recipient"]
            mark = "\u2713" if message.get("delivered") else "\u2717"
            lines.append(f"{mark}  {sender} \u2192 {recipient}")
            lines.append(f"     {message['body']}")
        return lines or ["(no activity yet)"]

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
        except Exception:  # noqa: BLE001 - a failed refresh must not break the UI
            log.debug("view refresh failed", exc_info=True)

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

    def show_terminals(self) -> None:
        """Minimal TUI support: the TUI cannot suspend, so show the exact
        ``attach`` command (see NOTES.md)."""
        try:
            terms = self.control.list_terminals()
        except Exception as exc:  # noqa: BLE001 - surface any daemon error in the status bar
            self.status = f"terminals unavailable: {exc}"
            self.status_kind = "error"
            return
        if not terms:
            self.status = "no terminals (create with: crewhall terminal new)"
            self.status_kind = "info"
            return
        first = terms[0]["session_id"]
        self.status = (f"{len(terms)} terminal(s); attach: "
                       f"crewhall terminal attach {first}")
        self.status_kind = "info"

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
