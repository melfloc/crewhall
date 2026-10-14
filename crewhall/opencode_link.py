"""Bridge to an OpenCode instance's own local HTTP server.

crewhall launches OpenCode with ``--port <free port>`` and a per-launch
``OPENCODE_SERVER_PASSWORD`` (passed through the private env file, never argv).
That gives each agent, without screen scraping and without session ambiguity:

* an event stream (``session.created`` / ``session.status`` / ``session.idle``)
  -> exact turn start/end signals and the agent's own session id;
* the full message history of that session, mapped here to the same block
  structure used for Claude (see ``transcripts``).

The server only listens on 127.0.0.1 and requires the password.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, UTC
from typing import Any
from collections.abc import Iterator

_SESSION = re.compile(r"^ses_[A-Za-z0-9]+$")
_REQUEST = re.compile(r"^(per|que)_?[A-Za-z0-9]+$")
# v2 ids are opaque; accept any token without path separators.
_V2_ID = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_TEXT = 20000


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _iso(ms: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def map_v2_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenCode v2 projected messages -> entries shaped like ``parse_entries``.

    v2 (``GET /api/session/{id}/message`` -> ``{data: [...]}``) is a flat list of
    typed messages: ``user`` carries ``text``; ``assistant`` carries ``content``
    parts (``text`` / ``reasoning`` / ``tool``). This maps them to the same block
    structure ``map_messages`` (v1) produces, so transcripts are unchanged.
    """
    out: list[dict[str, Any]] = []
    for item in messages:
        kind = item.get("type")
        if kind not in ("user", "assistant"):
            continue
        blocks: list[dict[str, Any]] = []
        if kind == "user":
            text = item.get("text") or ""
            if text.strip():
                blocks.append({"type": "text", "text": text})
        else:
            for part in item.get("content") or []:
                ptype = part.get("type")
                if ptype == "text" and (part.get("text") or "").strip():
                    blocks.append({"type": "text", "text": part["text"]})
                elif ptype == "tool":
                    state = part.get("state") or {}
                    args = json.dumps(state.get("input", part.get("input", {})),
                                      ensure_ascii=False, indent=2)
                    blocks.append({"type": "tool_use", "name": part.get("tool", "?"),
                                   "text": args[:2000]})
                    output = state.get("output", part.get("output"))
                    if output:
                        blocks.append({"type": "tool_result", "text": str(output)[:4000]})
        for block in blocks:
            if len(block["text"]) > MAX_TEXT:
                block["text"] = block["text"][:MAX_TEXT]
                block["truncated"] = True
        if not blocks:
            continue
        flat = "\n".join(
            b["text"] if b["type"] == "text"
            else f"[tool: {b.get('name', '')}] {b['text'][:300]}" if b["type"] == "tool_use"
            else f"[result] {b['text'][:600]}"
            for b in blocks
        )
        out.append({
            "role": "user" if kind == "user" else "assistant",
            "text": flat[:MAX_TEXT],
            "blocks": blocks,
            "truncated": any(b.get("truncated") for b in blocks),
            "at": _iso((item.get("time") or {}).get("created")),
        })
    return out


def map_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenCode ``[{info, parts}]`` -> entries shaped like transcripts.parse_entries."""
    out: list[dict[str, Any]] = []
    for item in messages:
        info = item.get("info") or {}
        role = info.get("role")
        if role not in ("user", "assistant"):
            continue
        blocks: list[dict[str, Any]] = []
        for part in item.get("parts") or []:
            kind = part.get("type")
            if kind == "text" and (part.get("text") or "").strip():
                blocks.append({"type": "text", "text": part["text"]})
            elif kind == "tool":
                state = part.get("state") or {}
                args = json.dumps(state.get("input", {}), ensure_ascii=False, indent=2)
                blocks.append({"type": "tool_use", "name": part.get("tool", "?"),
                               "text": args[:2000]})
                if state.get("output"):
                    blocks.append({"type": "tool_result", "text": str(state["output"])[:4000]})
        for block in blocks:
            if len(block["text"]) > MAX_TEXT:
                block["text"] = block["text"][:MAX_TEXT]
                block["truncated"] = True
        if not blocks:
            continue
        flat = "\n".join(
            b["text"] if b["type"] == "text"
            else f"[tool: {b.get('name', '')}] {b['text'][:300]}" if b["type"] == "tool_use"
            else f"[result] {b['text'][:600]}"
            for b in blocks
        )
        out.append({
            "role": role,
            "text": flat[:MAX_TEXT],
            "blocks": blocks,
            "truncated": any(b.get("truncated") for b in blocks),
            "at": _iso((info.get("time") or {}).get("created")),
        })
    return out


class OpenCodeLink:
    """HTTP bridge to one OpenCode server.

    ``api`` selects the contract: ``"v1"`` (routes at the root) or ``"v2"``
    (everything under ``/api`` with different shapes: cursor-paged sessions and
    messages, ``interrupt`` instead of ``abort``, per-session permissions and
    ``form`` instead of ``question``). It defaults to v1 so existing behaviour is
    unchanged; the controller sets it from the CLI version it launched.
    """

    def __init__(self, port: int, password: str, api: str = "v1") -> None:
        self.port = port
        self.password = password
        self.api = api
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}

    @classmethod
    def detect_api(cls, port: int, password: str) -> str:
        """Probe a running server: v2 answers ``/api/info``; v1 does not.

        An HTTP error means the server is up but is v1; a connection error
        propagates so the caller can retry until the TUI has opened its port.
        """
        probe = cls(port, password)
        try:
            probe.get_json("/api/info", timeout=2.0)
            return "v2"
        except urllib.error.HTTPError:
            return "v1"

    def _open(self, path: str, timeout: float):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=self._auth
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def get_json(self, path: str, timeout: float = 5.0) -> Any:
        with self._open(path, timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_json(self, path: str, body: Any, timeout: float = 10.0,
                  method: str = "POST") -> Any:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode("utf-8"),
            headers={**self._auth, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else None

    def _id_ok(self, request_id: str) -> bool:
        pattern = _V2_ID if self.api == "v2" else _REQUEST
        return bool(pattern.match(request_id or ""))

    def running_tools(self, session_id: str, limit: int = 6) -> dict[str, dict[str, Any]]:
        """Bash tool calls still running in the latest messages: ``{command: {call, output}}``."""
        if not _SESSION.match(session_id or ""):
            return {}
        if self.api == "v2":
            raw = self.get_json(f"/api/session/{session_id}/message?limit={int(limit)}")
            data = raw.get("data") if isinstance(raw, dict) else raw
            parts = (
                part
                for item in (data if isinstance(data, list) else [])
                for part in ((item or {}).get("content") or [])
            )
        else:
            raw = self.get_json(f"/session/{session_id}/message?limit={int(limit)}")
            parts = (
                part
                for item in (raw if isinstance(raw, list) else [])
                for part in ((item or {}).get("parts") or [])
            )
        out: dict[str, dict[str, Any]] = {}
        for part in parts:
            state = part.get("state") or {}
            if part.get("type") != "tool" or state.get("status") != "running":
                continue
            command = (state.get("input") or {}).get("command")
            if isinstance(command, str):
                out[command] = {"call": part.get("callID") or part.get("id"),
                                "output": str((state.get("metadata") or {}).get("output") or "")}
        return out

    def abort(self, session_id: str) -> None:
        """Stop the session's current turn (what ESC does in the TUI)."""
        if not _SESSION.match(session_id or ""):
            raise ValueError(f"invalid session id {session_id!r}")
        if self.api == "v2":
            self.post_json(f"/api/session/{session_id}/interrupt", {})
        else:
            self.post_json(f"/session/{session_id}/abort", {})

    # -- permissions / questions (answered from crewhall's front-ends) --
    @staticmethod
    def _data_list(raw: Any) -> list[dict[str, Any]]:
        """v2 wraps lists in ``{data: [...]}``; v1 returns a bare list."""
        if isinstance(raw, dict):
            raw = raw.get("data")
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def pending_permissions(self) -> list[dict[str, Any]]:
        path = "/api/permission/request" if self.api == "v2" else "/permission"
        return self._data_list(self.get_json(path))

    def pending_questions(self) -> list[dict[str, Any]]:
        # v2 renamed "questions" to "forms".
        path = "/api/form" if self.api == "v2" else "/question"
        return self._data_list(self.get_json(path))

    def reply_permission(self, request_id: str, reply: str, message: str | None = None,
                         session_id: str | None = None) -> None:
        if not self._id_ok(request_id):
            raise ValueError(f"invalid request id {request_id!r}")
        body: dict[str, Any] = {"reply": reply}
        if message:
            body["message"] = message
        if self.api == "v2":
            if not _SESSION.match(session_id or ""):
                raise ValueError(f"invalid session id {session_id!r}")
            self.post_json(f"/api/session/{session_id}/permission/{request_id}/reply", body)
        else:
            self.post_json(f"/permission/{request_id}/reply", body)

    def reply_question(self, request_id: str, answers: list[list[str]],
                       session_id: str | None = None) -> None:
        if not self._id_ok(request_id):
            raise ValueError(f"invalid request id {request_id!r}")
        if self.api == "v2":
            if not _SESSION.match(session_id or ""):
                raise ValueError(f"invalid session id {session_id!r}")
            self.post_json(f"/api/session/{session_id}/form/{request_id}/reply",
                           {"answers": answers})
        else:
            self.post_json(f"/question/{request_id}/reply", {"answers": answers})

    def reject_question(self, request_id: str, session_id: str | None = None) -> None:
        if not self._id_ok(request_id):
            raise ValueError(f"invalid request id {request_id!r}")
        if self.api == "v2":
            if not _SESSION.match(session_id or ""):
                raise ValueError(f"invalid session id {session_id!r}")
            self.post_json(f"/api/session/{session_id}/form/{request_id}", {}, method="DELETE")
        else:
            self.post_json(f"/question/{request_id}/reject", {})

    def discover(self, directory: str | None, since_ms: float, exclude: set[str]) -> str | None:
        """Fallback when the ``session.created`` event was missed.

        The agent's session is the earliest one created in its directory after
        it started that no other agent has claimed.
        """
        if not directory:
            return None
        if self.api == "v2":
            from urllib.parse import quote

            query = f"?directory={quote(directory)}&parentID=null&order=asc"
            try:
                raw = self.get_json(f"/api/session{query}")
            except (OSError, ValueError):
                return None
            sessions = raw.get("data") if isinstance(raw, dict) else raw
        else:
            try:
                sessions = self.get_json("/session")
            except (OSError, ValueError):
                return None
        want = os.path.realpath(directory)
        found = [
            (s.get("time", {}).get("created", 0), s["id"])
            for s in (sessions if isinstance(sessions, list) else [])
            if isinstance(s, dict)
            and s.get("id") and s["id"] not in exclude
            and os.path.realpath((s.get("location") or {}).get("directory")
                                 or s.get("directory") or "") == want
            and s.get("time", {}).get("created", 0) >= since_ms - 2000
        ]
        return min(found)[1] if found else None

    def history(self, session_id: str, limit: int = 200, before: int | None = None) -> dict:
        unavailable = {"available": False, "messages": [], "total": 0, "start": 0}
        if not _SESSION.match(session_id or ""):
            return unavailable
        try:
            if self.api == "v2":
                raw = self.get_json(f"/api/session/{session_id}/message")
                data = raw.get("data") if isinstance(raw, dict) else raw
                entries = map_v2_messages(data if isinstance(data, list) else [])
            else:
                raw = self.get_json(f"/session/{session_id}/message")
                entries = map_messages(raw if isinstance(raw, list) else [])
        except (OSError, ValueError):
            return unavailable
        total = len(entries)
        end = total if before is None else max(0, min(int(before), total))
        start = max(0, end - max(1, min(int(limit), 1000)))
        return {"available": True, "messages": entries[start:end], "total": total, "start": start}

    def events(self, should_stop) -> Iterator[dict[str, Any]]:
        """Yield decoded events until the stream ends or ``should_stop()``."""
        path = "/api/event" if self.api == "v2" else "/event"
        with self._open(path, timeout=30.0) as resp:
            for raw in resp:
                if should_stop():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    yield json.loads(line[5:])
                except ValueError:
                    continue
