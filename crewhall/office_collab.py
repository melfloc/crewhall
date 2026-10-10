"""Level 3 collaboration: a headless OnlyOffice editor the agent drives.

The human edits in their browser; crewhall runs a second, headless OnlyOffice
editor (Chromium + DocsAPI) that joins the *same* co-editing session (same
``document.key``) as the user "AI Agent". The agent applies changes through the
editor's Automation API via the Chrome DevTools Protocol (CDP), so its edits
appear live, with their own cursor, in the human's editor.

No third-party dependency (stdlib only): the CDP transport is a minimal
WebSocket client built on ``socket``/``struct``. Chromium must be installed and
``office.enabled`` on; without it the ops fail clearly and the app falls back to
the snapshot save path (the OnlyOffice callback still works).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from typing import Any

from . import settings

CANDIDATES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
              "chrome", "brave-browser", "microsoft-edge")
BOOTSTRAP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>crewhall co-editor</title>
<style>html,body{margin:0;height:100%;width:100%}#holder{width:100vw;height:100vh}</style>
<script src="__API__"></script></head>
<body><div id="holder"></div>
<script>
window.__ready = false;
window.__error = null;
try {
  var config = __CONFIG__;
  config.editorConfig.user = {id: "crewhall-ai", name: "AI Agent"};
  config.editorConfig.mode = "edit";
  // Do NOT add editorConfig.events with functions: DocsAPI serialises the
  // config to the editor iframe and a function value breaks initialisation
  // (the document never loads). Readiness is detected via CDP instead.
  window.__editor = new DocsAPI.DocEditor("holder", config);
} catch (e) { window.__error = String(e && e.message || e); }
</script></body></html>"""

# The Automation API lives INSIDE the editor frame (``Asc.editor``); this is the
# OnlyOffice 8.1 entry point (the parent ``DocsAPI.DocEditor`` has no
# ``createConnector``). ``read`` returns the document text; ``insert`` appends
# at the cursor; ``command`` calls a method on the editor.
READY_EXPR = ("(function(){try{return (typeof Asc!=='undefined' && typeof Asc.editor==='object'"
              " && typeof Asc.editor.ContentToHTML==='function')"
              " ? String(Asc.editor.ContentToHTML()).length : 0;}catch(e){return 0;}})()")
READ_EXPR = ("(function(){try{var h=Asc.editor.ContentToHTML();"
             "var d=new DOMParser().parseFromString(h,'text/html');"
             "return (d.body && d.body.textContent) || h || '';}catch(e){return 'ERR:'+e;}})()")


class CollabError(RuntimeError):
    pass


def chromium_binary() -> str | None:
    configured = str(settings.get("office.chromium") or "").strip()
    if configured:
        path = os.path.expanduser(configured)
        return path if os.path.exists(path) else None
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def build_bootstrap(api_url: str, config: dict[str, Any]) -> str:
    return (BOOTSTRAP_HTML
            .replace("__API__", api_url)
            .replace("__CONFIG__", json.dumps(config, ensure_ascii=False)))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- minimal CDP WebSocket client --------------------------------------------
class _WsClient:
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        self.sock = socket.create_connection(
            (parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=timeout)
        self._buf = bytearray()
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        self._read_headers()

    def _read_headers(self) -> None:
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CollabError("CDP handshake closed")
            self._buf += chunk
        head, _, rest = bytes(self._buf).partition(b"\r\n\r\n")
        self._buf = bytearray(rest)
        if b" 101 " not in head.split(b"\r\n", 1)[0] + b" ":
            raise CollabError("CDP handshake failed")

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _fill(self) -> bool:
        chunk = self.sock.recv(65536)
        if not chunk:
            return False
        self._buf += chunk
        return True

    def recv_text(self) -> str:
        while True:
            if len(self._buf) >= 2:
                length = self._buf[1] & 0x7F
                idx = 2
                if length == 126:
                    if len(self._buf) >= 4:
                        length = struct.unpack(">H", self._buf[2:4])[0]
                        idx = 4
                    else:
                        length = -1
                elif length == 127:
                    if len(self._buf) >= 10:
                        length = struct.unpack(">Q", self._buf[2:10])[0]
                        idx = 10
                    else:
                        length = -1
                if length >= 0 and len(self._buf) >= idx + length:
                    payload = self._buf[idx:idx + length]
                    del self._buf[:idx + length]
                    return bytes(payload).decode("utf-8", "replace")
            if not self._fill():
                raise CollabError("CDP connection closed")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class CoEditor:
    """One headless editor process for one document."""

    def __init__(self, config: dict[str, Any], public_url: str) -> None:
        binary = chromium_binary()
        if not binary:
            raise CollabError("chromium not found (set office.chromium)")
        self.binary = binary
        self.public_url = public_url.rstrip("/")
        self.profile = tempfile.mkdtemp(prefix="crewhall-collab-")
        api = self.public_url + "/web-apps/apps/api/documents/api.js"
        html = build_bootstrap(api, config)
        self.page = os.path.join(self.profile, "index.html")
        with open(self.page, "w", encoding="utf-8") as fh:
            fh.write(html)
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None
        self.ws: _WsClient | None = None
        self.httpd = None
        self.web_port = 0
        self._id = 0
        self._contexts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._start()

    def _serve_page(self) -> str:
        """Serve the bootstrap over HTTP (OnlyOffice refuses to run from file://)."""
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        handler = functools.partial(SimpleHTTPRequestHandler, directory=self.profile)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.web_port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.web_port}/index.html"

    def _start(self) -> None:
        url = self._serve_page()
        args = [
            self.binary, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", "--no-first-run", "--disable-extensions",
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={self.port}",
            url,
        ]
        self.proc = subprocess.Popen(  # noqa: S603 - our own argv
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 20.0
        target = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/list", timeout=1) as resp:
                    pages = json.loads(resp.read())
                target = next((p for p in pages if p.get("type") == "page"
                               and p.get("webSocketDebuggerUrl")), None)
                if target:
                    break
            except Exception:  # noqa: BLE001 - still booting
                pass
            time.sleep(0.3)
        if not target:
            self.close()
            raise CollabError("chromium did not expose a debugger target")
        self.ws = _WsClient(target["webSocketDebuggerUrl"])
        self._call("Runtime.enable", {})
        self._call("Page.enable", {})
        try:
            self._wait_ready()
        except CollabError:
            self.close()
            raise

    def _call(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> Any:
        if not self.ws:
            raise CollabError("editor is not connected")
        with self._lock:
            self._id += 1
            mid = self._id
            self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params}))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                message = json.loads(self.ws.recv_text())
                if message.get("method") == "Runtime.executionContextCreated":
                    ctx = message["params"]["context"]
                    frame = ctx.get("auxData", {}).get("frameId")
                    if frame:
                        self._contexts[frame] = ctx["id"]
                    continue
                if message.get("id") != mid:
                    continue
                if "error" in message:
                    raise CollabError(str(message["error"]))
                return message.get("result")
            raise CollabError(f"CDP timeout on {method}")

    def _evaluate(self, expression: str, await_promise: bool = True) -> Any:
        result = self._call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": await_promise,
        })
        out = result.get("result") or {}
        if result.get("exceptionDetails"):
            raise CollabError(str(result["exceptionDetails"]))
        return out.get("value")

    def _editor_frame(self) -> int | None:
        """Execution-context id of the OnlyOffice editor iframe (via CDP)."""
        tree = self._call("Page.getFrameTree", {})
        found: dict[str, str | None] = {"id": None}

        def walk(node: dict[str, Any]) -> None:
            frame = node.get("frame", {})
            if "documenteditor" in (frame.get("url") or ""):
                found["id"] = frame.get("id")
            for child in node.get("childFrames", []) or []:
                walk(child)

        walk(tree.get("frameTree", {}))
        frame_id = found["id"]
        return self._contexts.get(frame_id) if frame_id else None

    def _eval_editor(self, expression: str) -> Any:
        context = self._editor_frame()
        if context is None:
            raise CollabError("editor frame is not available yet")
        result = self._call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "contextId": context,
        })
        if result.get("exceptionDetails"):
            raise CollabError(str(result["exceptionDetails"]))
        return (result.get("result") or {}).get("value")

    def _wait_ready(self, timeout: float = 45.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = self._eval_editor(READY_EXPR)
                if isinstance(value, (int, float)) and value > 0:
                    self._register_chat()
                    return
            except CollabError:
                pass
            time.sleep(0.5)
        raise CollabError("the headless editor did not become ready")

    def _register_chat(self) -> None:
        """Buffer the co-authoring chat messages the human sends to "AI Agent"."""
        self._eval_editor(
            "(function(){window.__chat=window.__chat||[];"
            "try{Asc.editor.asc_registerCallback('asc_onCoAuthoringChatReceiveMessage',"
            "function(m){window.__chat.push(m);});}catch(e){}return true;})()")

    # -- commands the agent uses (Automation API inside the editor frame) ----
    def read(self) -> str:
        return str(self._eval_editor(READ_EXPR))

    def command(self, method: str, args: list[Any] | None = None) -> Any:
        name = str(method)
        for prefix in ("Api.GetDocument().", "Api."):
            if name.startswith(prefix):
                name = name[len(prefix):]
        payload = json.dumps(args or [], ensure_ascii=False)
        expr = ("(function(){try{return String(Asc.editor["
                f"{json.dumps(name)}].apply(Asc.editor,{payload}));}}"
                "catch(e){return 'ERR:'+e;}})()")
        return self._eval_editor(expr)

    def insert(self, text: str) -> Any:
        payload = json.dumps(str(text), ensure_ascii=False)
        expr = ("(function(){try{Asc.editor.Add_Text(" + payload
                + ");return 'ok';}catch(e){return 'ERR:'+e;}})()")
        result = self._eval_editor(expr)
        self.save()  # persist through the DS callback
        return result

    def save(self) -> Any:
        """Force the Document Server to save the document (fires our callback).

        ``asc_Save`` is the editor's force-save entry (``forceSave`` is only a
        UI gate); it makes the agent's edits reach the artifact on disk without
        waiting for the session to close.
        """
        return self._eval_editor(
            "(function(){try{Asc.editor.asc_Save();return 'ok';}catch(e){return 'ERR:'+e;}})()")

    # -- co-authoring channels: the user talks to the agent from the document --
    def users(self) -> list[dict[str, Any]]:
        value = self._eval_editor(
            "(function(){try{var u=Asc.editor.asc_coAuthoringGetUsers();"
            "return JSON.stringify(u||[]);}catch(e){return '[]';}})()")
        try:
            return json.loads(value) if isinstance(value, str) else []
        except (TypeError, json.JSONDecodeError):
            return []

    def chat_messages(self) -> list[dict[str, Any]]:
        """Messages the human wrote in the editor's co-authoring chat.

        The SDK callback may hand over a message or an array of messages, so the
        buffer is flattened into a plain list of objects.
        """
        value = self._eval_editor("JSON.stringify(window.__chat||[])")
        try:
            raw = json.loads(value) if isinstance(value, str) else []
        except (TypeError, json.JSONDecodeError):
            raw = []
        out: list[dict[str, Any]] = []
        stack = list(raw) if isinstance(raw, list) else []
        while stack:
            item = stack.pop(0)
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, list):
                stack = list(item) + stack
        return out

    def chat_send(self, text: str) -> Any:
        payload = json.dumps(str(text), ensure_ascii=False)
        expr = ("(function(){try{Asc.editor.asc_coAuthoringChatSendMessage(" + payload
                + ");return 'ok';}catch(e){return 'ERR:'+e;}})()")
        return self._eval_editor(expr)

    def comments(self) -> list[dict[str, Any]]:
        """All comments in the document (each anchored to its selected text)."""
        value = self._eval_editor(
            "(function(){try{var c=Asc.editor.pluginMethod_GetAllComments();"
            "return JSON.stringify(c||[]);}catch(e){return 'ERR:'+e;}})()")
        if isinstance(value, str) and value.startswith("ERR:"):
            return [{"error": value}]
        try:
            return json.loads(value) if isinstance(value, str) else []
        except (TypeError, json.JSONDecodeError):
            return []

    def add_comment(self, text: str, author: str = "AI Agent") -> Any:
        payload = json.dumps({"Text": str(text), "UserName": author,
                              "Data": {"Text": str(text)}}, ensure_ascii=False)
        expr = ("(function(){try{Asc.editor.pluginMethod_AddComment(" + payload
                + ");return 'ok';}catch(e){return 'ERR:'+e;}})()")
        return self._eval_editor(expr)

    def close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.httpd = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.proc.kill()
                except OSError:
                    pass
        self.proc = None
        shutil.rmtree(self.profile, ignore_errors=True)


class CollabRegistry:
    """The live headless editors, one per conversation/artifact."""

    def __init__(self) -> None:
        self._editors: dict[str, CoEditor] = {}
        self._seen: dict[str, int] = {}
        self._seen_comments: dict[str, set[str]] = {}
        self._forward = None  # callable(conversation_id, path, user, kind, text)
        self._lock = threading.Lock()

    def set_forward(self, fn) -> None:
        """Install the callback that delivers OnlyOffice chat/comments to the agent."""
        self._forward = fn

    def poll_forward(self) -> int:
        """Forward new co-authoring chat messages and comments to the agent.

        Returns how many were forwarded. The agent's own messages/comments are
        skipped so a reply never loops back into its own conversation.
        """
        forwarded = 0
        for key, editor in list(self._editors.items()):
            conversation_id, _, path = key.partition(":")
            forwarded += self._forward_chat(key, editor, conversation_id, path)
            forwarded += self._forward_comments(key, editor, conversation_id, path)
        return forwarded

    def _forward_chat(self, key: str, editor: "CoEditor", conversation_id: str,
                      path: str) -> int:
        try:
            messages = editor.chat_messages()
        except Exception:  # noqa: BLE001 - a closed editor must not break the loop
            return 0
        seen = self._seen.get(key, 0)
        if len(messages) <= seen:
            return 0
        count = 0
        for message in messages[seen:]:
            data = message if isinstance(message, dict) else {}
            # The co-authoring chat callback uses {message, username, ...}; other
            # builds use Text/UserName — accept both.
            text = str(data.get("message") or data.get("Text") or data.get("text") or "").strip()
            user = str(data.get("username") or data.get("UserName")
                       or data.get("Username") or "").strip()
            if text and user not in ("AI Agent", "crewhall-ai") and self._forward:
                try:
                    self._forward(conversation_id, path, user, "chat", text)
                    count += 1
                except Exception:  # noqa: BLE001
                    pass
        self._seen[key] = len(messages)
        return count

    def _forward_comments(self, key: str, editor: "CoEditor", conversation_id: str,
                          path: str) -> int:
        try:
            comments = editor.comments()
        except Exception:  # noqa: BLE001
            return 0
        seen = self._seen_comments.get(key, set())
        new_ids: set[str] = set()
        count = 0
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            data = comment.get("Data") or {}
            if not isinstance(data, dict):
                data = {}
            comment_id = str(comment.get("Id") or "")
            if not comment_id or comment_id in seen:
                continue
            new_ids.add(comment_id)
            user = str(data.get("UserName") or "").strip()
            text = str(data.get("Text") or "").strip()
            quote = str(data.get("QuoteText") or "").strip()
            if text and user != "AI Agent" and self._forward:
                payload = f'sobre "{quote}": {text}' if quote else text
                try:
                    self._forward(conversation_id, path, user, "comment", payload)
                    count += 1
                except Exception:  # noqa: BLE001
                    pass
        if new_ids:
            self._seen_comments[key] = seen | new_ids
        return count

    @staticmethod
    def key(conversation_id: str, path: str) -> str:
        return f"{conversation_id}:{path}"

    def open(self, conversation_id: str, path: str, config: dict[str, Any],
             public_url: str) -> dict[str, Any]:
        k = self.key(conversation_id, path)
        self.close(conversation_id, path)
        editor = CoEditor(config, public_url)
        with self._lock:
            self._editors[k] = editor
        return {"open": True, "conversation": conversation_id, "path": path,
                "user": "AI Agent"}

    def get(self, conversation_id: str, path: str) -> CoEditor:
        editor = self._editors.get(self.key(conversation_id, path))
        if editor is None:
            raise CollabError("no open co-editor for this document")
        return editor

    def command(self, conversation_id: str, path: str, method: str,
                args: list[Any] | None) -> Any:
        return self.get(conversation_id, path).command(method, args)

    def read(self, conversation_id: str, path: str) -> str:
        return self.get(conversation_id, path).read()

    def insert(self, conversation_id: str, path: str, text: str) -> Any:
        return self.get(conversation_id, path).insert(text)

    def save(self, conversation_id: str, path: str) -> Any:
        return self.get(conversation_id, path).save()

    def users(self, conversation_id: str, path: str) -> list[dict[str, Any]]:
        return self.get(conversation_id, path).users()

    def chat_messages(self, conversation_id: str, path: str) -> list[dict[str, Any]]:
        return self.get(conversation_id, path).chat_messages()

    def chat_send(self, conversation_id: str, path: str, text: str) -> Any:
        return self.get(conversation_id, path).chat_send(text)

    def comments(self, conversation_id: str, path: str) -> list[dict[str, Any]]:
        return self.get(conversation_id, path).comments()

    def add_comment(self, conversation_id: str, path: str, text: str) -> Any:
        return self.get(conversation_id, path).add_comment(text)

    def close(self, conversation_id: str, path: str) -> bool:
        key = self.key(conversation_id, path)
        with self._lock:
            editor = self._editors.pop(key, None)
            self._seen.pop(key, None)
            self._seen_comments.pop(key, None)
        if editor:
            editor.close()
            return True
        return False

    def list(self) -> list[dict[str, Any]]:
        out = []
        for key in list(self._editors):
            cid, _, path = key.partition(":")
            out.append({"conversation": cid, "path": path})
        return out

    def close_all(self) -> None:
        for key in list(self._editors):
            cid, _, path = key.partition(":")
            self.close(cid, path)


REGISTRY = CollabRegistry()
