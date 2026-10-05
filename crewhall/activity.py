"""What an agent is doing right now, and which model runs it.

Derived from real signals only: the agent state, the interactions waiting for
the user, the transcript (Claude) or the local server (OpenCode) for the tool
that is still running, and ``/proc`` for background shells. Anything that
cannot be observed stays ``None``; nothing is guessed.

Kinds: ``starting`` ``thinking`` ``tool`` ``shell`` ``subagent`` ``asking``
``waiting`` ``idle`` ``background`` ``exited`` ``error`` ``unknown``.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from . import transcripts

_TAIL = 256 * 1024
_MODEL_SCAN = 8 * 1024 * 1024  # how far back to look for a model when the tail has none
_SHELL_TOOLS = {"bash", "shell", "powershell"}
_AGENT_TOOLS = {"task", "agent"}
_BRIEF_KEYS = ("command", "description", "file_path", "filePath", "path", "pattern", "query", "url")


def _tail_lines(path: str, window: int = _TAIL) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - window))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > window and lines else lines


def _brief(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in _BRIEF_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:160]
    return None


def _window(lines: list[str], window: int) -> list[str]:
    """The trailing lines that fit ``window`` bytes (for an already-fetched tail)."""
    out: list[str] = []
    size = 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > window and out:
            break
        out.append(line)
    return out[::-1]


def claude_snapshot(session_id: str | None, host: dict[str, Any] | None = None) -> dict[str, Any]:
    """Model and still-unanswered tool call from the end of a Claude transcript.

    For a remote agent the last fetched tail is used (never a network wait here);
    nothing known yet means no data, not a guess.
    """
    if host is not None:
        from . import remote_files

        fetched = remote_files.snapshot_lines(host, session_id or "")
        if not fetched:
            return {}
        tail = lambda window: _window(fetched, window)  # noqa: E731
    else:
        path = transcripts.find_session_file(session_id or "")
        if not path:
            return {}
        tail = lambda window: _tail_lines(path, window)  # noqa: E731
    model, pending = None, {}
    for line in tail(_TAIL):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        if entry.get("type") == "assistant":
            if message.get("model") and not str(message["model"]).startswith("<"):
                model = message["model"]
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                pending[block.get("id")] = {"name": block.get("name") or "tool",
                                            "brief": _brief(block.get("input"))}
            elif block.get("type") == "tool_result":
                pending.pop(block.get("tool_use_id"), None)
    running = list(pending.values())[-1] if pending else None
    if not model:  # a huge tool result can push every assistant entry out of the tail
        for line in reversed(tail(_MODEL_SCAN)):
            if '"model"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            found = (entry.get("message") or {}).get("model")
            if entry.get("type") == "assistant" and found and not str(found).startswith("<"):
                model = found
                break
    return {"model": model, "tool": running}


_CLAUDE_HEAD = re.compile(
    r"^[\s▐▛▜▝▘▀█▌▄▖▗]*([A-Z][\w .\-\[\]()]*?\S)\s+·\s+(?:Claude|API|Bedrock|Vertex|Team|Enterprise|Max|Pro)")
_OPENCODE_FOOT = re.compile(r"^[\s┃│]*\w+\s+·\s+(\S.*?\S)\s*$")


def claude_model(text: str | None) -> str | None:
    """Model name Claude's own screen shows (its header line)."""
    for line in (text or "").splitlines()[:12]:
        m = _CLAUDE_HEAD.match(line)
        if m:
            return m.group(1).strip()
    return None


def opencode_model(text: str | None) -> str | None:
    """Model name OpenCode's footer shows."""
    for line in (text or "").splitlines():
        if "┃" in line:
            m = _OPENCODE_FOOT.match(line)
            if m:
                return m.group(1)
    return None


def model_from_screen(kind: str, text: str | None) -> str | None:
    """Display name of the model the TUI shows, dispatched to the adapter.

    Kept as a kind-based helper for callers/tests that have only a kind; the
    controller uses ``Harness.model_from_screen`` directly.
    """
    from .harness import HARNESSES

    cls = HARNESSES.get(kind)
    return cls.model_from_screen(text) if cls is not None else None


def model_from_args(args: list[str] | None) -> str | None:
    """``--model X`` / ``--model=X`` / ``-m X`` given when the agent was created."""
    items = list(args or [])
    for i, arg in enumerate(items):
        if arg in ("--model", "-m") and i + 1 < len(items):
            return items[i + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1] or None
    return None


def opencode_snapshot(link: Any, session_id: str | None) -> dict[str, Any]:
    """Model and running tool from the latest OpenCode messages."""
    if link is None or not session_id:
        return {}
    try:
        raw = link.get_json(f"/session/{session_id}/message?limit=6", timeout=1.5)
    except Exception:  # noqa: BLE001 - the server may be busy/restarting
        return {}
    model, running = None, None
    for item in raw if isinstance(raw, list) else []:
        info = (item or {}).get("info") or {}
        if info.get("role") == "assistant" and info.get("modelID"):
            model = info["modelID"]
        elif not model and (info.get("model") or {}).get("modelID"):
            model = info["model"]["modelID"]  # the prompt already carries it, before any reply
        for part in (item or {}).get("parts") or []:
            state = part.get("state") or {}
            if part.get("type") == "tool" and state.get("status") in ("running", "pending"):
                running = {"name": part.get("tool") or "tool", "brief": _brief(state.get("input"))}
    return {"model": model, "tool": running}


def classify(state: str, *, asking: int, tool: dict[str, Any] | None, shells: int) -> dict[str, Any]:
    """Pure mapping from signals to ``{kind, label, detail}``."""
    if state in ("exited", "error", "starting"):
        return {"kind": state, "label": state.capitalize(), "detail": None}
    if asking:
        return {"kind": "asking", "label": "Needs your answer", "detail": None}
    if state == "working":
        if tool:
            name = tool["name"]
            low = name.lower()
            kind = "shell" if low in _SHELL_TOOLS else "subagent" if low in _AGENT_TOOLS else "tool"
            label = {"shell": "Running command", "subagent": "Running sub-agent"}.get(kind, f"Using {name}")
            return {"kind": kind, "label": label, "detail": tool.get("brief")}
        return {"kind": "thinking", "label": "Thinking", "detail": None}
    if shells:
        return {"kind": "background", "label": f"{shells} process{'es' if shells != 1 else ''} running", "detail": None}
    if state == "waiting_input":
        return {"kind": "waiting", "label": "Waiting for input", "detail": None}
    if state == "ready":
        return {"kind": "idle", "label": "Idle", "detail": None}
    return {"kind": "unknown", "label": "Unknown", "detail": None}
