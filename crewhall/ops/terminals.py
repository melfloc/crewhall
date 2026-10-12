"""Daemon ops: terminals (mixins composed by :class:`crewhall.daemon.Server`)."""
from __future__ import annotations

# File-wide F401: each mixin keeps the daemon's name surface so a moved
# handler needs no import surgery; ruff's unused-import check is not useful here.
# ruff: noqa: F401
from typing import Any
from .. import settings
from ..terminals import TerminalError
from .common import OpsBase


class OpsTerminals(OpsBase):

    def _op_terminal_create(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.create_terminal(
            host=request.get("host"),
            cwd=request.get("cwd"),
            shell=request.get("shell"),
            command=request.get("command"),
            title=request.get("title"),
            readonly=request.get("readonly", False),
            cols=request.get("cols", 120),
            rows=request.get("rows", 32),
            owner=request.get("owner"),
        )
        return {"terminal": self.controller.terminal_info(session.session_id)}

    def _op_terminal_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"terminals": self.controller.list_terminals(request.get("host"))}

    def _op_terminal_info(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"terminal": self.controller.terminal_info(request["id"])}

    def _op_terminal_write(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller._resolve_terminal(request["id"])
        self.controller.guard_terminal_input(session)
        text = request.get("text")
        if not isinstance(text, str):
            raise TerminalError("text must be a string")
        limit = int(settings.get("terminals.max_message_bytes"))
        if len(text.encode("utf-8")) > limit:
            raise TerminalError(f"text too large (max {limit} bytes)")
        session.write(text)
        if request.get("enter"):
            session.send_enter()
        return {"written": len(text.encode("utf-8"))}

    def _op_terminal_key(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller._resolve_terminal(request["id"])
        self.controller.guard_terminal_input(session)
        session.send_key(request["key"])
        return {}

    def _op_terminal_capture(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller._resolve_terminal(request["id"])
        output = session.capture(escapes=bool(request.get("escapes", False)))
        if request.get("recent", True):
            max_lines = int(request.get("max_lines", 500) or 0)
            if max_lines > 0:
                output = "\n".join(output.splitlines()[-max_lines:])
        return {"output": output}

    def _op_terminal_resize(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller._resolve_terminal(request["id"])
        self.controller.guard_terminal_input(session)
        cols = self.controller._terminal_dim(request.get("cols"), "cols", 10, 500)
        rows = self.controller._terminal_dim(request.get("rows"), "rows", 2, 200)
        session.resize(cols, rows)
        return {}

    def _op_terminal_close(self, request: dict[str, Any]) -> dict[str, Any]:
        closed = self.controller.close_terminal(request["id"])
        return {"closed": closed}
