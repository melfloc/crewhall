"""Command palette: the command list, filtering, keys and dispatch."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
from .modals import ModalField

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
    ("Terminals", "terminals"),
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


class UiPalette:
    """The command palette, mixed into :class:`AppModel`."""

    def open_palette(self) -> None:
        self._new_modal(
            "palette", "COMMAND PALETTE",
            fields=[ModalField("query", ">", "text", "")], button=0,
        )

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
        elif action == "terminals":
            self.show_terminals()
        elif action == "refresh":
            self.refresh()
            self.status = "refreshed"
            self.status_kind = "info"
        elif action == "help":
            self.open_help()
        elif action == "quit":
            self.should_quit = True
