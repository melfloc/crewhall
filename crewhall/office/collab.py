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
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from typing import Any

from .. import paths, settings
import contextlib

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
# The bootstrap page is served from 127.0.0.1 and embeds the Document Server's
# editor iframe. When the Document Server is published under another host (a
# Tailscale name, a LAN IP) the iframe is cross-site, and Chromium's site
# isolation moves it to its own process/target: its execution context never
# shows up on the page we drive, so the co-editor never becomes "ready". This
# headless browser only ever loads our page and the configured Document Server,
# in a throwaway profile, so keeping them in one process is safe.
SAME_PROCESS_FLAGS = (
    "--disable-site-isolation-trials",
    "--disable-features=IsolateOrigins,site-per-process",
)

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


# -- resource accounting / stale-editor cleanup ------------------------------
_EDITORS_LOCK = threading.Lock()


def _office_state_dir() -> str:
    path = os.path.join(paths.state_dir(), "office")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _editors_path() -> str:
    return os.path.join(_office_state_dir(), "editors.json")


def _load_editor_records() -> list[dict[str, Any]]:
    try:
        with open(_editors_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _save_editor_records(records: list[dict[str, Any]]) -> None:
    tmp = ""
    try:
        path = _editors_path()
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(records, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _remember_editor(profile: str, pid: int, port: int) -> None:
    with _EDITORS_LOCK:
        try:
            records = [r for r in _load_editor_records() if r.get("profile") != profile]
            records.append({"profile": profile, "pid": pid, "port": port,
                            "started": time.time()})
            _save_editor_records(records)
        except OSError:
            pass  # diagnostics bookkeeping must never break an editor


def _forget_editor(profile: str) -> None:
    with _EDITORS_LOCK:
        try:
            records = [r for r in _load_editor_records() if r.get("profile") != profile]
            _save_editor_records(records)
        except OSError:
            pass


def _profile_roots() -> set[str]:
    roots = {os.path.realpath(tempfile.gettempdir())}
    with contextlib.suppress(Exception):
        roots.add(os.path.realpath(paths.tmpdir_root()))
    return roots


def _allowed_profile(path: str) -> bool:
    try:
        real = os.path.realpath(path)
        uid = os.stat(real).st_uid
    except OSError:
        return False
    return (uid == os.getuid()
            and os.path.basename(real).startswith("crewhall-collab-")
            and os.path.dirname(real) in _profile_roots())


def _profile_dirs() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for root in _profile_roots():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.startswith("crewhall-collab-"):
                continue
            path = os.path.join(root, name)
            if path in seen:
                continue
            seen.add(path)
            if os.path.isdir(path) and not os.path.islink(path) and _allowed_profile(path):
                found.append(path)
    return found


def _proc_fields(pid: int) -> tuple[str, int, int] | None:
    """(state, ppid, pgrp) from /proc/<pid>/stat; None if it is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
        tail = raw[raw.rfind(")") + 2:].split()
        return tail[0], int(tail[1]), int(tail[2])
    except (OSError, ValueError, IndexError):
        return None


def _group_pids(pgid: int) -> list[int]:
    out: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        fields = _proc_fields(pid)
        if fields is not None and fields[0] != "Z" and fields[2] == pgid:
            out.append(pid)
    return out


def _rss_kb(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _group_rss_kb(pgid: int | None) -> int | None:
    if not pgid:
        return None
    total = 0
    found = False
    for pid in _group_pids(pgid):
        kb = _rss_kb(pid)
        if kb is not None:
            total += kb
            found = True
    return total if found else None


def _pids_for_profile(profile: str) -> list[int]:
    needle = f"--user-data-dir={profile}".encode("utf-8", "replace")
    out: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                cmdline = fh.read()
        except OSError:
            continue
        if needle in cmdline:
            out.append(int(name))
    return out


def _terminate_group(pgid: int, timeout: float = 5.0) -> None:
    """SIGTERM the whole Chromium process group, then SIGKILL if it lingers.

    Chromium is started with ``start_new_session=True``, so ``proc.pid`` is the
    group leader. Terminating only the leader leaves renderer children behind
    when it hangs; this is what kept gigabytes alive after a close/crash.
    """
    if pgid <= 0:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        with contextlib.suppress(OSError):
            os.kill(pgid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_pids(pgid):
            return
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        os.killpg(pgid, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _group_pids(pgid):
            return
        time.sleep(0.05)


def sweep_orphans() -> int:
    """Kill headless editors left by a crashed daemon; remove their profiles.

    Only touches profiles under the temp/state temp roots whose basename starts
    with ``crewhall-collab-``, and only processes whose argv names that exact
    profile. Called after the daemon takes its singleton lock, before it opens
    any editor.
    """
    if not os.path.isdir("/proc"):
        return 0  # process inspection is Linux-only; never delete a live profile blindly
    profiles = {str(r.get("profile")) for r in _load_editor_records() if r.get("profile")}
    profiles.update(_profile_dirs())
    cleaned = 0
    for profile in profiles:
        if not _allowed_profile(profile):
            continue
        for pid in _pids_for_profile(profile):
            with contextlib.suppress(Exception):  # sweeping is best effort
                _terminate_group(pid)
        if not _pids_for_profile(profile):
            shutil.rmtree(profile, ignore_errors=True)
            cleaned += 1
    with _EDITORS_LOCK:
        _save_editor_records([])
    return cleaned


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
        with contextlib.suppress(OSError):
            self.sock.close()


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
        self.pid: int | None = None
        self.ws: _WsClient | None = None
        self.httpd: Any = None
        self.web_port = 0
        self._id = 0
        self._contexts: dict[str, int] = {}
        self._lock = threading.Lock()
        self.last_used = time.time()
        self._closed = False
        try:
            self._start()
        except BaseException:
            self.close()
            raise

    def touch(self) -> None:
        """Mark real activity (an agent/user edit), not the 2 s watcher poll."""
        self.last_used = time.time()

    @property
    def idle_s(self) -> float:
        return max(0.0, time.time() - self.last_used)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def rss_kb(self) -> int | None:
        """RSS of the whole Chromium process group (main + renderers)."""
        return _group_rss_kb(self.pid)

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
            "--disable-background-networking", "--disable-component-update",
            "--disable-default-apps", "--disable-sync", "--metrics-recording-only",
            "--mute-audio", "--no-default-browser-check",
            *SAME_PROCESS_FLAGS,
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={self.port}",
            url,
        ]
        self.proc = subprocess.Popen(  # noqa: S603 - our own argv
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
        self.pid = self.proc.pid
        _remember_editor(self.profile, self.pid, self.port)
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
                pass  # noqa: BLE001 - justified boundary guard (B1)
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
        if self._closed:
            return
        self._closed = True
        if self.ws:
            self.ws.close()
            self.ws = None
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass  # noqa: BLE001 - justified boundary guard (B1)
            self.httpd = None
        proc = self.proc
        if proc is not None:
            if proc.poll() is None:  # never kill a recycled PID
                _terminate_group(proc.pid)
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=1)
        self.proc = None
        self.pid = None
        _forget_editor(self.profile)
        shutil.rmtree(self.profile, ignore_errors=True)


# Ids of the OnlyOffice comments already forwarded to the agent, per artifact,
# kept in the conversation's folder so they survive a reopen or a restart.
_FORWARDED_FILE = ".office-forwarded.json"
_FORWARDED_LOCK = threading.Lock()


def _forwarded_path(conversation_id: str) -> str | None:
    from .. import chat as conversations

    try:
        return os.path.join(conversations.conversation_dir(conversation_id), _FORWARDED_FILE)
    except Exception:  # noqa: BLE001 - unknown conversation: memory only
        return None


def _load_forwarded(conversation_id: str, path: str) -> set[str]:
    file = _forwarded_path(conversation_id)
    if not file:
        return set()
    try:
        with open(file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    ids = data.get(path) if isinstance(data, dict) else None
    return {str(i) for i in ids} if isinstance(ids, list) else set()


def _save_forwarded(conversation_id: str, path: str, ids: set[str]) -> None:
    file = _forwarded_path(conversation_id)
    if not file:
        return
    with _FORWARDED_LOCK:
        try:
            with open(file, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[path] = sorted(ids)
        tmp = file + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, file)
        except OSError:
            pass


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

    def count(self) -> int:
        with self._lock:
            return len(self._editors)

    def sweep_idle(self, now: float | None = None) -> int:
        """Close co-editors that saw no chat, comment or edit for the TTL.

        ``poll_forward`` forwards first and sweeps afterwards, so a message that
        arrived just before the TTL is delivered and *then* refreshes the clock.
        """
        try:
            ttl = float(settings.get("office.collab_idle_s") or 0)
        except Exception:  # noqa: BLE001 - a bad setting must not break the sweep
            ttl = 600.0
        if ttl <= 0:
            return 0
        now = time.time() if now is None else now
        closed = 0
        for key, editor in list(self._editors.items()):
            last = float(getattr(editor, "last_used", now) or now)
            if now - last >= ttl:
                self.close_by_key(key)
                closed += 1
        return closed

    def _evict_for_capacity(self, *, reserve: bool = False) -> None:
        """Close least-recently-used editors until the configured limit holds.

        ``reserve=True`` makes room for an editor that is about to be created;
        the default trims after it was added (two concurrent opens).
        """
        try:
            limit = max(1, int(settings.get("office.collab_max_editors") or 1))
        except Exception:  # noqa: BLE001 - default to the tight bound
            limit = 1
        target = limit - 1 if reserve else limit
        while True:
            with self._lock:
                if len(self._editors) <= target:
                    return
                victim = min(
                    self._editors.items(),
                    key=lambda item: float(getattr(item[1], "last_used", 0.0) or 0.0),
                )[0]
            self.close_by_key(victim)

    def poll_forward(self) -> int:
        """Forward new co-authoring chat messages and comments to the agent.

        Returns how many were forwarded. The agent's own messages/comments are
        skipped so a reply never loops back into its own conversation.
        """
        forwarded = 0
        for key, editor in list(self._editors.items()):
            if getattr(editor, "alive", True) is False:
                self.close_by_key(key)  # the browser died: reclaim immediately
                continue
            conversation_id, _, path = key.partition(":")
            forwarded += self._forward_chat(key, editor, conversation_id, path)
            forwarded += self._forward_comments(key, editor, conversation_id, path)
        self.sweep_idle()
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
        touch = getattr(editor, "touch", None)
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
                    pass  # noqa: BLE001 - justified boundary guard (B1)
        self._seen[key] = len(messages)
        if count and callable(touch):
            touch()
        return count

    def _forward_comments(self, key: str, editor: "CoEditor", conversation_id: str,
                          path: str) -> int:
        try:
            comments = editor.comments()
        except Exception:  # noqa: BLE001
            return 0
        seen = self._seen_comments.get(key)
        if seen is None:
            # Comments live in the document: after a reopen or a daemon restart
            # they are all listed again. Start from what was already forwarded.
            seen = _load_forwarded(conversation_id, path)
        new_ids: set[str] = set()
        count = 0
        touch = getattr(editor, "touch", None)
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
                    pass  # noqa: BLE001 - justified boundary guard (B1)
        self._seen_comments[key] = seen | new_ids
        if new_ids:
            _save_forwarded(conversation_id, path, seen | new_ids)
        if count and callable(touch):
            touch()
        return count

    @staticmethod
    def key(conversation_id: str, path: str) -> str:
        return f"{conversation_id}:{path}"

    def open(self, conversation_id: str, path: str, config: dict[str, Any],
             public_url: str) -> dict[str, Any]:
        from . import collab_enabled

        if not collab_enabled():
            raise CollabError("live co-editing is disabled (office.collab_enabled)")
        k = self.key(conversation_id, path)
        self.close(conversation_id, path)
        self._evict_for_capacity(reserve=True)
        editor = CoEditor(config, public_url)
        with self._lock:
            self._editors[k] = editor
        # Trim again: two concurrent opens can both pass the pre-create check.
        self._evict_for_capacity()
        return {"open": True, "conversation": conversation_id, "path": path,
                "user": "AI Agent"}

    def get(self, conversation_id: str, path: str) -> CoEditor:
        editor = self._editors.get(self.key(conversation_id, path))
        if editor is None:
            raise CollabError("no open co-editor for this document")
        return editor

    def _touch(self, conversation_id: str, path: str) -> CoEditor:
        editor = self.get(conversation_id, path)
        touch = getattr(editor, "touch", None)
        if callable(touch):
            touch()
        return editor

    def command(self, conversation_id: str, path: str, method: str,
                args: list[Any] | None) -> Any:
        return self._touch(conversation_id, path).command(method, args)

    def read(self, conversation_id: str, path: str) -> str:
        return self._touch(conversation_id, path).read()

    def insert(self, conversation_id: str, path: str, text: str) -> Any:
        return self._touch(conversation_id, path).insert(text)

    def save(self, conversation_id: str, path: str) -> Any:
        return self._touch(conversation_id, path).save()

    def users(self, conversation_id: str, path: str) -> list[dict[str, Any]]:
        return self._touch(conversation_id, path).users()

    def chat_messages(self, conversation_id: str, path: str) -> list[dict[str, Any]]:
        return self._touch(conversation_id, path).chat_messages()

    def chat_send(self, conversation_id: str, path: str, text: str) -> Any:
        return self._touch(conversation_id, path).chat_send(text)

    def comments(self, conversation_id: str, path: str) -> list[dict[str, Any]]:
        return self._touch(conversation_id, path).comments()

    def add_comment(self, conversation_id: str, path: str, text: str) -> Any:
        return self._touch(conversation_id, path).add_comment(text)

    def close_by_key(self, key: str) -> bool:
        with self._lock:
            editor = self._editors.pop(key, None)
            self._seen.pop(key, None)
            self._seen_comments.pop(key, None)
        if editor:
            editor.close()
            return True
        return False

    def close(self, conversation_id: str, path: str) -> bool:
        return self.close_by_key(self.key(conversation_id, path))

    def close_conversation(self, conversation_id: str) -> int:
        """Close every co-editor of a conversation (e.g. when it is deleted)."""
        closed = 0
        for key in list(self._editors):
            cid, _, _path = key.partition(":")
            if cid == conversation_id:
                self.close_by_key(key)
                closed += 1
        return closed

    def list(self) -> list[dict[str, Any]]:
        out = []
        now = time.time()
        for key, editor in list(self._editors.items()):
            cid, _, path = key.partition(":")
            last = float(getattr(editor, "last_used", now) or now)
            rss = None
            rss_fn = getattr(editor, "rss_kb", None)
            if callable(rss_fn):
                with contextlib.suppress(Exception):
                    rss = rss_fn()
            out.append({"conversation": cid, "path": path,
                        "pid": getattr(editor, "pid", None),
                        "idle_s": round(max(0.0, now - last), 1),
                        "memory_kb": rss})
        return out

    def close_all(self) -> None:
        for key in list(self._editors):
            self.close_by_key(key)


REGISTRY = CollabRegistry()
