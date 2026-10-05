"""Stdio JSON-RPC 2.0 MCP server. Identity and authorization come from the
environment + the daemon; this process decides nothing by itself."""
from __future__ import annotations

import json
import sys
import time
from typing import Any

from .. import brand

MAX_MESSAGE = 8000
MAX_TO = 128
RATE_MAX = 60           # calls...
RATE_WINDOW = 60.0      # ...per this many seconds

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "crewhall"

TOOLS = [
    {
        "name": "whoami",
        "description": "Return your agent id, name and teams.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_teammates",
        "description": "List the agents that share at least one team with you.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "send_message",
        "description": "Send a message to a teammate (must share a team with you).",
        "inputSchema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "message": {"type": "string"}},
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request",
        "description": "Ask a teammate and wait for their correlated reply (or a timeout).",
        "inputSchema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "task": {"type": "string"},
                           "timeout": {"type": "number"}},
            "required": ["to", "task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reply",
        "description": "Reply to a request you received (only works for its original recipient).",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}, "message": {"type": "string"}},
            "required": ["request_id", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "handoff",
        "description": "Hand a task to a teammate without waiting for an answer.",
        "inputSchema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "message": {"type": "string"}},
            "required": ["to", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "new_session",
        "description": "Start a fresh conversation for yourself only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class McpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Session:
    """Holds the caller's identity and a bounded rate limiter."""

    def __init__(self) -> None:
        self.agent_id = brand.env("AGENT_ID") or ""
        self.token = brand.env("TOKEN") or ""
        self._calls: list[float] = []

    def identity(self) -> tuple[str, str]:
        if not self.agent_id or not self.token:
            raise McpError(-32001, "not running inside an crewhall agent session")
        return self.agent_id, self.token

    def rate_ok(self) -> bool:
        now = time.time()
        self._calls = [t for t in self._calls if now - t < RATE_WINDOW]
        if len(self._calls) >= RATE_MAX:
            return False
        self._calls.append(now)
        return True

    def call(self, op: str, **params: Any) -> dict[str, Any]:
        from ..client import Client, RpcError

        try:
            return Client(autostart=False).call(op, **params)
        except RpcError as exc:
            raise McpError(-32002, str(exc)) from exc


def _require_object(arguments: Any) -> dict[str, Any]:
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise McpError(-32602, "arguments must be an object")
    return arguments


def _no_args(arguments: dict[str, Any]) -> None:
    if arguments:
        raise McpError(-32602, f"unexpected arguments: {', '.join(arguments)}")


def _text(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error}


def call_tool(session: Session, name: str, arguments: Any) -> dict[str, Any]:
    if not session.rate_ok():
        raise McpError(-32003, "rate limit exceeded; slow down")
    args = _require_object(arguments)
    agent_id, token = session.identity()
    if name == "whoami":
        _no_args(args)
        info = session.call("agent_identity", target=agent_id, token=token)
        return _text({"agent_id": info.get("agent_id"), "name": info.get("name"),
                      "teams": info.get("teams", [])})
    if name == "list_teammates":
        _no_args(args)
        info = session.call("agent_identity", target=agent_id, token=token)
        return _text(info.get("teammates", []))
    if name == "send_message":
        to = args.get("to")
        message = args.get("message")
        if not isinstance(to, str) or not to.strip() or len(to) > MAX_TO:
            raise McpError(-32602, "to must be a teammate name or id")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
            raise McpError(-32602, f"message must be 1..{MAX_MESSAGE} characters")
        result = session.call("team_send", sender=agent_id, token=token,
                              recipient=to.strip(), body=message)
        return _text(result.get("delivery", result))
    if name == "handoff":
        to = args.get("to")
        message = args.get("message")
        if not isinstance(to, str) or not to.strip() or len(to) > MAX_TO:
            raise McpError(-32602, "to must be a teammate name or id")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
            raise McpError(-32602, f"message must be 1..{MAX_MESSAGE} characters")
        result = session.call("team_send", sender=agent_id, token=token,
                              recipient=to.strip(), body=message)
        return _text({"handoff": True, "delivery": result.get("delivery", result)})
    if name == "request":
        to = args.get("to")
        task = args.get("task")
        if not isinstance(to, str) or not to.strip() or len(to) > MAX_TO:
            raise McpError(-32602, "to must be a teammate name or id")
        if not isinstance(task, str) or not task.strip() or len(task) > MAX_MESSAGE:
            raise McpError(-32602, f"task must be 1..{MAX_MESSAGE} characters")
        timeout = args.get("timeout")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise McpError(-32602, "timeout must be a positive number of seconds")
        result = session.call("request_create", sender=agent_id, token=token,
                              recipient=to.strip(), task=task, timeout=timeout, wait=True)
        request = result.get("request", {})
        return _text({"request_id": request.get("request_id"), "state": request.get("state"),
                      "reply": request.get("reply")})
    if name == "reply":
        request_id = args.get("request_id")
        message = args.get("message")
        if not isinstance(request_id, str) or not request_id.strip():
            raise McpError(-32602, "request_id is required")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
            raise McpError(-32602, f"message must be 1..{MAX_MESSAGE} characters")
        result = session.call("request_reply", agent=agent_id, token=token,
                              request_id=request_id.strip(), body=message)
        return _text({"request_id": result.get("request", {}).get("request_id"),
                      "state": result.get("request", {}).get("state")})
    if name == "new_session":
        _no_args(args)
        result = session.call("team_new_session", sender=agent_id, token=token, target=agent_id)
        return _text({"conversation_id": result.get("conversation_id"),
                      "previous": result.get("previous"), "confirmed": result.get("confirmed")})
    raise McpError(-32601, f"unknown tool {name!r}")


def handle(session: Session, request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, -32602, "params must be an object")
        try:
            result = call_tool(session, str(params.get("name")), params.get("arguments"))
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except McpError as exc:
            if exc.code == -32001:
                return _error(request_id, exc.code, exc.message)
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": _text(exc.message, is_error=True)}
    if method is None:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "invalid request"}}
    return _error(request_id, -32601, f"method not found: {method}")


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _version() -> str:
    from .. import __version__

    return __version__


def main(argv: list[str] | None = None) -> int:
    session = Session()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            out.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            out.flush()
            continue
        response = handle(session, request)
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()
    return 0
