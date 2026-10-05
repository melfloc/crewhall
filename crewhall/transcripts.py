"""Full conversation history for agents whose CLI persists one.

Claude Code repaints its TUI and keeps no terminal scrollback, so anything that
scrolled out of the viewport is gone from tmux. It does write the whole
conversation to ``<config>/projects/<project>/<session-id>.jsonl``; crewhall
launches Claude with a known ``--session-id`` and reads that file back.
"""
from __future__ import annotations

import glob
import json
import os
import re
from typing import Any

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_TEXT = 20000


def claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def find_session_file(session_id: str) -> str | None:
    if not _UUID.match(session_id or ""):  # also blocks path tricks in the glob
        return None
    hits = glob.glob(os.path.join(claude_config_dir(), "projects", "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        content = "\n".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content
        )
    return str(content)


_PASTE_TAG = re.compile(r"</?pasted_content[^>]*>")
_CMD = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_CMD_MSG = re.compile(r"<command-message>.*?</command-message>", re.S)
_CMD_OUT = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)


def clean_user_text(text: str) -> str:
    """Hide Claude's internal wrappers so the conversation reads like what the user saw."""
    text = _PASTE_TAG.sub("", text)
    if "<command-name>" in text:
        name = (_CMD.search(text) or [None, ""])[1].strip()
        args = (_CMD_ARGS.search(text) or [None, ""])[1].strip()
        return (name + (" " + args if args else "")).strip() or text
    m = _CMD_OUT.search(text)
    if m:
        return "↳ " + m.group(1).strip()
    return _CMD_MSG.sub("", text).strip()


def _blocks(content: Any, role: str = "assistant") -> list[dict[str, Any]]:
    """Structured blocks: text | tool_use | tool_result (thinking/images dropped)."""
    if isinstance(content, str):
        content = clean_user_text(content) if role == "user" else content
        return [{"type": "text", "text": content}] if content.strip() else []
    out: list[dict[str, Any]] = []
    for block in content or []:
        if isinstance(block, str):
            block = clean_user_text(block) if role == "user" else block
            if block.strip():
                out.append({"type": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text", "").strip():
            text = clean_user_text(block["text"]) if role == "user" else block["text"]
            if text.strip():
                out.append({"type": "text", "text": text})
        elif kind == "tool_use":
            args = json.dumps(block.get("input", {}), ensure_ascii=False, indent=2)
            out.append({"type": "tool_use", "name": block.get("name", "?"),
                        "text": args[:2000]})
        elif kind == "tool_result":
            out.append({"type": "tool_result", "text": _result_text(block.get("content", ""))[:4000]})
    return out


def _flat(blocks: list[dict[str, Any]]) -> str:
    parts = []
    for b in blocks:
        if b["type"] == "text":
            parts.append(b["text"])
        elif b["type"] == "tool_use":
            parts.append(f"[tool: {b['name']}] {b['text'][:300]}")
        else:
            parts.append(f"[result] {b['text'][:600]}")
    return "\n".join(parts).strip()


def parse_entries(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        role = entry.get("type")
        if role not in ("user", "assistant") or entry.get("isMeta"):
            continue
        blocks = _blocks((entry.get("message") or {}).get("content", ""), role)
        for block in blocks:
            if len(block["text"]) > MAX_TEXT:
                block["text"] = block["text"][:MAX_TEXT]
                block["truncated"] = True
        text = _flat(blocks)
        if not text:
            continue
        out.append(
            {
                "role": role,
                "text": text[:MAX_TEXT],
                "blocks": blocks,
                "truncated": any(b.get("truncated") for b in blocks),
                "at": entry.get("timestamp"),
            }
        )
    return out


_CACHE: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}


def _entries_for(path: str) -> list[dict[str, Any]] | None:
    """Parsed entries, cached by (mtime, size): polling a 2 MB file stays cheap."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _CACHE.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            entries = parse_entries(fh.readlines())
    except OSError:
        return None
    if len(_CACHE) > 32:
        _CACHE.clear()
    _CACHE[path] = (st.st_mtime, st.st_size, entries)
    return entries


def read_history(session_id: str, limit: int = 200, before: int | None = None) -> dict[str, Any]:
    """Return a page of messages ending at index ``before`` (default: latest)."""
    path = find_session_file(session_id)
    if path is None:
        return {"available": False, "messages": [], "total": 0, "start": 0}
    entries = _entries_for(path)
    if entries is None:
        return {"available": False, "messages": [], "total": 0, "start": 0}
    total = len(entries)
    end = total if before is None else max(0, min(int(before), total))
    start = max(0, end - max(1, min(int(limit), 1000)))
    return {"available": True, "messages": entries[start:end], "total": total, "start": start}
