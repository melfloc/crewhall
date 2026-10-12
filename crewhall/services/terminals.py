"""Controller services: terminals (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import time
from typing import Any
from .. import settings, terminals as termlib
from ..session import InteractiveSession
from ..terminals import is_terminal, new_terminal_id
from ..types import SessionSpec
from ..controller import log


class ServicesTerminals:

    def terminals_enabled(self) -> bool:
        return bool(settings.get("terminals.enabled"))

    def _require_terminals_enabled(self) -> None:
        if not self.terminals_enabled():
            raise termlib.TerminalError("terminals are disabled")

    @staticmethod
    def _terminal_dim(value: Any, name: str, lo: int, hi: int) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise termlib.TerminalError(f"{name} must be a whole number")
        value = int(value)
        if not lo <= value <= hi:
            raise termlib.TerminalError(f"{name} must be between {lo} and {hi}")
        return value

    def create_terminal(
        self,
        *,
        host: str | None = None,
        cwd: str | None = None,
        shell: str | None = None,
        command: str | None = None,
        title: str | None = None,
        readonly: Any = False,
        cols: Any = 120,
        rows: Any = 32,
        owner: str | None = None,
    ) -> InteractiveSession:
        self._require_terminals_enabled()
        host_name = termlib.validate_host(host)
        cwd_val = termlib.validate_cwd(cwd, host_name)
        shell_path = termlib.validate_shell(shell, host_name)
        title_val = termlib.validate_title(title)
        command_val = termlib.validate_command(command)
        readonly_val = termlib.validate_readonly(readonly)
        cols_val = self._terminal_dim(cols, "cols", 10, 500)
        rows_val = self._terminal_dim(rows, "rows", 2, 200)
        existing = self.list_terminals()
        termlib.check_limits(
            existing,
            host_name,
            int(settings.get("terminals.max_total")),
            int(settings.get("terminals.max_per_host")),
        )
        max_per_token = int(settings.get("terminals.max_per_token"))
        if owner and max_per_token:
            same = sum(1 for t in existing if t.get("owner") == owner)
            if same >= max_per_token:
                raise termlib.TerminalError(
                    f"too many terminals for this token (max {max_per_token})"
                )
        spec = SessionSpec(
            command=[shell_path] if shell_path else "",
            cwd=cwd_val,
            cols=cols_val,
            rows=rows_val,
            host=host_name,
            kind=termlib.KIND_TERMINAL,
            readonly=readonly_val,
            title=title_val,
            owner=owner,
        )
        session = InteractiveSession(
            self._backend_for(None, host_name), spec, session_id=new_terminal_id()
        )
        session.start()
        self.registry.add(session)
        self._persist()
        if command_val:
            # Sent as literal text to the already-open shell, so the terminal
            # stays alive after the command finishes.
            session.write(command_val)
            session.send_enter()
        return session

    def _resolve_terminal(self, target: str) -> InteractiveSession:
        self._require_terminals_enabled()
        session = self.registry.resolve(target)
        if not is_terminal(session):
            raise termlib.TerminalError(f"{target} is not a terminal")
        return session

    def _terminal_dict(self, session: InteractiveSession) -> dict[str, Any]:
        session.poll()
        info = session.info().to_dict()
        meta = info.get("meta") or {}
        info["host_unreachable"] = bool(meta.get("host_unreachable"))
        return info

    def list_terminals(self, host: str | None = None) -> list[dict[str, Any]]:
        self._require_terminals_enabled()
        wanted = termlib.validate_host(host)
        out = []
        for session in self.registry.all():
            if not is_terminal(session):
                continue
            info = self._terminal_dict(session)
            if host is not None and (info.get("host") or None) != wanted:
                continue
            out.append(info)
        return out

    def terminal_info(self, target: str) -> dict[str, Any]:
        self._require_terminals_enabled()
        return self._terminal_dict(self._resolve_terminal(target))

    def close_terminal(self, target: str) -> str:
        self._require_terminals_enabled()
        session = self._resolve_terminal(target)
        session.close()
        self.registry.remove(session.session_id)
        self._persist()
        return session.session_id

    def prune_terminals(self, keep_exited_seconds: int) -> int:
        """Close and remove terminals whose shell exited more than ``keep`` seconds ago.

        Only touches ``kind="terminal"``: never agents or raw sessions. ``keep <= 0``
        keeps them forever.
        """
        if keep_exited_seconds <= 0 or not self.terminals_enabled():
            return 0
        now = time.time()
        removed = 0
        for session in list(self.registry.all()):
            if not is_terminal(session):
                continue
            try:
                session.poll()
            except Exception:  # noqa: BLE001 - a dead session is reaped below
                log.debug("poll failed for %s", session.session_id, exc_info=True)
            if session.status.alive:
                continue
            exited = getattr(session, "_exited_at", None)
            if exited is None:
                exited = session.created_at
            if now - exited < keep_exited_seconds:
                continue
            try:
                session.close()
            except Exception:  # noqa: BLE001 - reaping continues regardless
                log.debug("close failed for %s", session.session_id, exc_info=True)
            self.registry.remove(session.session_id)
            removed += 1
        if removed:
            self._persist()
        return removed

    def guard_terminal_input(self, session: InteractiveSession) -> None:
        """Single choke point for read-only terminals across raw + terminal ops."""
        if is_terminal(session) and bool(getattr(session.spec, "readonly", False)):
            raise termlib.TerminalError("terminal is read-only")

    def get(self, target: str) -> InteractiveSession:
        return self.registry.resolve(target)
