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
import urllib.request
from datetime import datetime, UTC
from typing import Any
from collections.abc import Iterator

_SESSION = re.compile(r"^ses_[A-Za-z0-9]+$")
_REQUEST = re.compile(r"^(per|que)_?[A-Za-z0-9]+$")
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
    def __init__(self, port: int, password: str) -> None:
        self.port = port
        self.password = password
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}

    def _open(self, path: str, timeout: float):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", headers=self._auth
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def get_json(self, path: str, timeout: float = 5.0) -> Any:
        with self._open(path, timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_json(self, path: str, body: Any, timeout: float = 10.0) -> Any:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={**self._auth, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else None

    def running_tools(self, session_id: str, limit: int = 6) -> dict[str, dict[str, Any]]:
        """Bash tool calls still running in the latest messages: ``{command: {call, output}}``."""
        if not _SESSION.match(session_id or ""):
            return {}
        raw = self.get_json(f"/session/{session_id}/message?limit={int(limit)}")
        out: dict[str, dict[str, Any]] = {}
        for item in raw if isinstance(raw, list) else []:
            for part in (item or {}).get("parts") or []:
                state = part.get("state") or {}
                if part.get("type") != "tool" or state.get("status") != "running":
                    continue
                command = (state.get("input") or {}).get("command")
                if isinstance(command, str):
                    out[command] = {"call": part.get("callID"),
                                    "output": str((state.get("metadata") or {}).get("output") or "")}
        return out

    def abort(self, session_id: str) -> None:
        """Stop the session's current turn (what ESC does in the TUI)."""
        if not _SESSION.match(session_id or ""):
            raise ValueError(f"invalid session id {session_id!r}")
        self.post_json(f"/session/{session_id}/abort", {})

    # -- permissions / questions (answered from crewhall's front-ends) --
    def pending_permissions(self) -> list[dict[str, Any]]:
        data = self.get_json("/permission")
        return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []

    def pending_questions(self) -> list[dict[str, Any]]:
        data = self.get_json("/question")
        return [q for q in data if isinstance(q, dict)] if isinstance(data, list) else []

    def reply_permission(self, request_id: str, reply: str, message: str | None = None) -> None:
        if not _REQUEST.match(request_id or ""):
            raise ValueError(f"invalid request id {request_id!r}")
        body: dict[str, Any] = {"reply": reply}
        if message:
            body["message"] = message
        self.post_json(f"/permission/{request_id}/reply", body)

    def reply_question(self, request_id: str, answers: list[list[str]]) -> None:
        if not _REQUEST.match(request_id or ""):
            raise ValueError(f"invalid request id {request_id!r}")
        self.post_json(f"/question/{request_id}/reply", {"answers": answers})

    def reject_question(self, request_id: str) -> None:
        if not _REQUEST.match(request_id or ""):
            raise ValueError(f"invalid request id {request_id!r}")
        self.post_json(f"/question/{request_id}/reject", {})

    def discover(self, directory: str | None, since_ms: float, exclude: set[str]) -> str | None:
        """Fallback when the ``session.created`` event was missed.

        The agent's session is the earliest one created in its directory after
        it started that no other agent has claimed.
        """
        if not directory:
            return None
        try:
            sessions = self.get_json("/session")
        except (OSError, ValueError):
            return None
        want = os.path.realpath(directory)
        found = [
            (s.get("time", {}).get("created", 0), s["id"])
            for s in sessions if isinstance(s, dict)
            if s.get("id") and s["id"] not in exclude
            and os.path.realpath(s.get("directory") or "") == want
            and s.get("time", {}).get("created", 0) >= since_ms - 2000
        ]
        return min(found)[1] if found else None

    def history(self, session_id: str, limit: int = 200, before: int | None = None) -> dict:
        unavailable = {"available": False, "messages": [], "total": 0, "start": 0}
        if not _SESSION.match(session_id or ""):
            return unavailable
        try:
            raw = self.get_json(f"/session/{session_id}/message")
        except (OSError, ValueError):
            return unavailable
        entries = map_messages(raw if isinstance(raw, list) else [])
        total = len(entries)
        end = total if before is None else max(0, min(int(before), total))
        start = max(0, end - max(1, min(int(limit), 1000)))
        return {"available": True, "messages": entries[start:end], "total": total, "start": start}

    def events(self, should_stop) -> Iterator[dict[str, Any]]:
        """Yield decoded events until the stream ends or ``should_stop()``."""
        with self._open("/event", timeout=30.0) as resp:
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
