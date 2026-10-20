"""Reusable OpenCode v2 ``serve`` double for the adapter's contract tests.

It implements the routes in the v2 handoff (§4.1) with v2's *shapes* and its
two surprising behaviours, so the tests exercise the real contract instead of a
friendly mock:

* ``POST /api/session/{id}/agent|model`` answers **204 even for an unknown
  value** and simply does not change the session (crewhall must read back).
* ``POST .../permission/{rid}/reply`` answers **400 ``Missing key ["decision"]``**
  unless the body is the v2 shape.

Nothing here touches the user's OpenCode config, database or processes.
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PASSWORD = "test-password"


class FakeOpenCodeV2(BaseHTTPRequestHandler):
    version = "2.0.29"
    sessions: dict[str, dict[str, Any]] = {}
    messages: dict[str, list[dict[str, Any]]] = {}
    active: set[str] = set()
    permissions: list[dict[str, Any]] = []
    forms: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = [
        {"id": "build", "name": "Build", "mode": "primary"},
        {"id": "plan", "name": "Plan", "mode": "primary"},
    ]
    default_model: dict[str, Any] | None = {
        "providerID": "opencode-go", "id": "deepseek-v4.1-flash",
    }
    requests: list[tuple[str, str, dict[str, Any]]] = []
    info_status: int | None = None

    def log_message(self, *args: Any) -> None:  # noqa: D401 - silence the test log
        pass

    @classmethod
    def reset(cls) -> None:
        cls.sessions = {}
        cls.messages = {}
        cls.active = set()
        cls.permissions = []
        cls.forms = []
        cls.models = []
        cls.default_model = {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"}
        cls.requests = []
        cls.info_status = None

    # -- helpers -----------------------------------------------------------
    def _authed(self) -> bool:
        want = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self._empty(401)
            return False
        return True

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _record(self, method: str, path: str, body: dict[str, Any] | None = None) -> None:
        type(self).requests.append((method, path, body or {}))

    @classmethod
    def find_session(cls, sid: str) -> dict[str, Any] | None:
        return cls.sessions.get(sid)

    @classmethod
    def paths(cls) -> list[str]:
        return [path for _method, path, _body in cls.requests]

    @classmethod
    def bodies(cls, suffix: str) -> list[dict[str, Any]]:
        return [body for _method, path, body in cls.requests if path.endswith(suffix)]

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._authed():
            return
        path, _, query = self.path.partition("?")
        type(self).requests.append(("GET", path, {"query": query}))
        if path == "/api/info":
            if type(self).info_status:
                self._empty(type(self).info_status)
                return
            self._json({"version": type(self).version, "pid": 4242,
                        "urls": [f"http://127.0.0.1:{self.server.server_address[1]}"],
                        "capabilities": {"persistentPty": True}})
            return
        if path == "/api/session/active":
            self._json({"data": {sid: {"type": "running"} for sid in type(self).active}})
            return
        if path == "/api/session":
            sessions = list(type(self).sessions.values())
            directory = None
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "directory":
                    from urllib.parse import unquote
                    directory = unquote(value)
            if directory:
                sessions = [
                    s for s in sessions
                    if (s.get("location") or {}).get("directory") == directory
                ]
            if "order=desc" in query:
                sessions.sort(key=lambda s: (s.get("time") or {}).get("created") or 0,
                              reverse=True)
            elif "order=asc" in query:
                sessions.sort(key=lambda s: (s.get("time") or {}).get("created") or 0)
            if "limit=" in query:
                try:
                    limit = int(query.split("limit=", 1)[1].split("&", 1)[0])
                    sessions = sessions[:limit]
                except ValueError:
                    pass
            self._json({"data": sessions})
            return
        if path.startswith("/api/session/") and path.endswith("/message"):
            sid = path.split("/")[3]
            if sid not in type(self).sessions:
                self._empty(404)
                return
            self._json({"data": type(self).messages.get(sid, [])})
            return
        if path.startswith("/api/session/"):
            sid = path.split("/")[3]
            session = type(self).sessions.get(sid)
            if session is None:
                self._empty(404)
                return
            self._json({"data": session})
            return
        if path == "/api/permission/request":
            self._json({"data": type(self).permissions})
            return
        if path == "/api/form":
            self._json({"data": type(self).forms})
            return
        if path.startswith("/api/session/") and path.endswith("/permission"):
            sid = path.split("/")[3]
            self._json({"data": [p for p in type(self).permissions
                                 if p.get("sessionID") == sid]})
            return
        if path.startswith("/api/session/") and path.endswith("/form"):
            sid = path.split("/")[3]
            self._json({"data": [f for f in type(self).forms
                                 if f.get("sessionID") == sid]})
            return
        if path == "/api/model":
            self._json({"data": type(self).models})
            return
        if path == "/api/model/default":
            self._json({"data": type(self).default_model})
            return
        if path == "/api/agent":
            self._json({"data": type(self).agents})
            return
        self._empty(404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authed():
            return
        path, _, _query = self.path.partition("?")
        body = self._body()
        self._record("POST", path, body)
        if path == "/api/session":
            sid = f"ses_fake{len(type(self).sessions) + 1}"
            session = {
                "id": sid, "title": "New session", "agent": "build",
                "model": dict(body.get("model") or type(self).default_model or {}),
                "time": {"created": 1000 + len(type(self).sessions), "updated": 0},
                "location": {"directory": body.get("directory") or "/tmp/work"},
                "outcome": None,
            }
            type(self).sessions[sid] = session
            self._json({"data": session})
            return
        if path.startswith("/api/session/"):
            parts = path.split("/")
            sid = parts[3]
            session = type(self).sessions.get(sid)
            if session is None:
                self._empty(404)
                return
            tail = parts[4:]
            if tail == ["prompt"]:
                text = body.get("text")
                if not isinstance(text, str) or not text:
                    self._empty(400)
                    return
                type(self).messages.setdefault(sid, []).append({
                    "id": "msg_user", "type": "user", "text": text,
                    "time": {"created": 2000},
                })
                type(self).active.add(sid)
                self._empty(204)
                return
            if tail == ["agent"]:
                wanted = body.get("agent")
                if any(a.get("id") == wanted for a in type(self).agents):
                    session["agent"] = wanted
                self._empty(204)  # 204 even for an unknown agent, as v2 does
                return
            if tail == ["model"]:
                ref = body.get("model") or {}
                if any(
                    m.get("providerID") == ref.get("providerID")
                    and m.get("modelID", m.get("id")) == ref.get("id")
                    for m in type(self).models
                ):
                    session["model"] = dict(ref)
                self._empty(204)  # 204 even for an unknown model, as v2 does
                return
            if tail == ["interrupt"]:
                type(self).active.discard(sid)
                self._empty(204)
                return
            if len(tail) == 3 and tail[0] == "permission" and tail[2] == "reply":
                if "decision" not in body:
                    self._json({"error": 'Missing key ["decision"]'}, status=400)
                    return
                if body["decision"] not in ("once", "always", "reject"):
                    self._json({"error": "invalid decision"}, status=400)
                    return
                type(self).permissions = [
                    p for p in type(self).permissions if p.get("id") != tail[1]
                ]
                self._empty(204)
                return
            if len(tail) == 3 and tail[0] == "form" and tail[2] == "reply":
                if not isinstance(body.get("answer"), dict):
                    self._json({"error": 'Missing key ["answer"]'}, status=400)
                    return
                form = next((f for f in type(self).forms if f.get("id") == tail[1]), None)
                if form is None:
                    self._empty(404)
                    return
                form["answer"] = body["answer"]
                type(self).forms = [f for f in type(self).forms if f.get("id") != tail[1]]
                self._empty(204)
                return
        self._empty(404)

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authed():
            return
        path, _, _query = self.path.partition("?")
        self._record("DELETE", path, {})
        parts = path.split("/")
        if len(parts) == 5 and parts[3] == "form":
            sid = parts[2]
            if sid not in type(self).sessions:
                self._empty(404)
                return
            type(self).forms = [f for f in type(self).forms if f.get("id") != parts[4]]
            self._empty(204)
            return
        self._empty(404)


def start_server(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the double on a loopback port; caller owns shutdown()."""
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeOpenCodeV2)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]
