from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

from . import paths
import contextlib

log = logging.getLogger("crewhall.persistence")

SCHEMA_VERSION = 1


class StateStore:
    """Persist the logical control-plane state (Teams + agent definitions).

    Only non-sensitive, reconstructible data is stored: no identity tokens, no
    provider credentials, no process content. Runtime state (live sessions,
    pids, output) is owned by the daemon and is never persisted.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or paths.state_path()

    def load(self) -> dict[str, Any]:
        data: dict[str, Any]
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"schema": SCHEMA_VERSION, "teams": [], "agents": [], "terminals": []}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read state %s: %s", self.path, exc)
            return {"schema": SCHEMA_VERSION, "teams": [], "agents": [], "terminals": []}
        data.setdefault("teams", [])
        data.setdefault("agents", [])
        data.setdefault("requests", [])
        data.setdefault("terminals", [])
        return data

    def save(self, data: dict[str, Any]) -> None:
        data = {**data, "schema": SCHEMA_VERSION}
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not persist state %s: %s", self.path, exc)

    def snapshot(self, controller: Any) -> dict[str, Any]:
        """Build a persistable snapshot from a Controller."""
        teams = []
        for team in controller.teams.list():
            teams.append(
                {
                    "team_id": team.team_id,
                    "name": team.name,
                    "agent_ids": list(team.agent_ids),
                    "created_at": team.created_at,
                    "workspace": team.workspace,
                    "workspace_mode": getattr(team, "workspace_mode", None),
                    "host": getattr(team, "host", None),
                    "leads": list(getattr(team, "leads", ()) or ()),
                }
            )
        agents = []
        for harness in controller.agents.all():
            spec = harness.session.spec
            agents.append(
                {
                    "agent_id": harness.agent_id,
                    "name": harness.name,
                    "kind": harness.kind,
                    "backend": harness.session.backend.name,
                    "cwd": spec.cwd,
                    "cols": spec.cols,
                    "rows": spec.rows,
                    "args": list(controller._agent_args.get(harness.agent_id, [])),
                    "host": getattr(controller, "_agent_hosts", {}).get(harness.agent_id),
                    "conversation_id": harness.conversation_id,
                    # user-provided env only; identity/secret vars are omitted
                    "env": dict(controller._base_env.get(harness.agent_id) or {}),
                }
            )
        requests = []
        board = getattr(controller, "requests", None)
        if board is not None:
            requests = board.snapshot()
        terminals = []
        from .terminals import is_terminal

        for session in controller.registry.all():
            if not is_terminal(session):
                continue
            spec = session.spec
            shell = None
            if isinstance(spec.command, list) and len(spec.command) == 1:
                shell = spec.command[0]
            terminals.append({
                "session_id": session.session_id,
                "host": spec.host,
                "cwd": spec.cwd,
                "shell": shell,
                "title": spec.title,
                "readonly": bool(spec.readonly),
                "owner": spec.owner,
                "cols": spec.cols,
                "rows": spec.rows,
                "created_at": session.created_at,
            })
        return {"schema": SCHEMA_VERSION, "teams": teams, "agents": agents,
                "requests": requests, "terminals": terminals}


class PendingQueueStore:
    """Durable store for messages still waiting to be delivered.

    Keeps only what is needed to resume a bounded retry after a daemon restart:
    the two agent ids, the body, the id and the absolute (wall-clock) expiry.
    Bodies live on the user's own disk under the 0700 state directory; identity
    tokens and credentials are never stored here.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(paths.state_dir(), "pending-messages.json")

    def load(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read pending queue %s: %s", self.path, exc)
            return []
        items = data.get("items") if isinstance(data, dict) else data
        return [item for item in (items or []) if isinstance(item, dict)]

    def save(self, items: list[dict[str, Any]]) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pending-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump({"schema": SCHEMA_VERSION, "items": items}, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not persist pending queue %s: %s", self.path, exc)


class HistoryStore:
    """Append-only JSONL history of agent messages, rotated on disk.

    One JSON object per line, under the 0700 state directory. The file never
    grows past ``max_entries`` (+ a small batch between rotations) and entries
    older than ``max_age_days`` are dropped. It exists so the crew activity
    strip and the Message flow still show what happened before a restart.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        max_entries: int = 2000,
        max_age_days: float | None = 30,
    ) -> None:
        self.path = path or os.path.join(paths.state_dir(), "messages.jsonl")
        self.max_entries = max(0, int(max_entries))
        self.max_age_days = None if max_age_days is None else max(0.0, float(max_age_days))
        self._lock = threading.Lock()
        self._lines = 0

    def _fresh_enough(self, record: dict[str, Any], now: float) -> bool:
        if self.max_age_days is None or self.max_age_days <= 0:
            return True
        try:
            ts = float(record.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            return True
        return (now - ts) <= self.max_age_days * 86400.0

    def load(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("could not read message history %s: %s", self.path, exc)
            return []
        now = time.time()
        records = [r for r in records if self._fresh_enough(r, now)]
        if self.max_entries and len(records) > self.max_entries:
            records = records[-self.max_entries:]
            self._rewrite(records)
        with self._lock:
            self._lines = len(records)
        return records

    def append(self, record: dict[str, Any]) -> None:
        if self.max_entries == 0 and self.max_age_days in (None, 0):
            return
        line = json.dumps(record, separators=(",", ":"), sort_keys=True)
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    with contextlib.suppress(OSError):
                        os.fchmod(fh.fileno(), 0o600)
                    fh.write(line + "\n")
                self._lines += 1
            except OSError as exc:
                log.warning("could not append message history %s: %s", self.path, exc)
                return
            over = self.max_entries and self._lines > self.max_entries + max(64, self.max_entries // 4)
        if over:
            # Reload (also applies the age cut), keep the newest and rewrite.
            with self._lock:
                records = self._read_lines()
                now = time.time()
                records = [r for r in records if self._fresh_enough(r, now)]
                if self.max_entries and len(records) > self.max_entries:
                    records = records[-self.max_entries:]
                self._rewrite_locked(records)
                self._lines = len(records)

    def _read_lines(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            return []
        return records

    def _rewrite(self, records: list[dict[str, Any]]) -> None:
        with self._lock:
            self._rewrite_locked(records)
            self._lines = len(records)

    def _rewrite_locked(self, records: list[dict[str, Any]]) -> None:
        directory = os.path.dirname(self.path)
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".messages-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                for record in records:
                    fh.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not rotate message history %s: %s", self.path, exc)


class AgentTokenStore:
    """The agents' identity tokens, so a restarted daemon still recognises them.

    A tmux agent survives ``daemon stop`` (Fase A1) with ``CREWHALL_TOKEN`` in its
    environment; without this file the new daemon would mint a different token
    and the surviving agent could no longer message its Team. The file is 0600
    in the 0700 state directory (next to ``control-token``), it is covered by the
    agents' Claude ``permissions.deny`` rules and never goes into ``state.json``,
    bundles or logs.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(paths.state_dir(), "agent-tokens.json")

    def load(self) -> dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read agent tokens %s: %s", self.path, exc)
            return {}
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, dict):
            return {}
        return {str(k): str(v) for k, v in tokens.items() if k and v}

    def save(self, tokens: dict[str, str]) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tokens-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump({"schema": SCHEMA_VERSION, "tokens": tokens}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not persist agent tokens %s: %s", self.path, exc)

