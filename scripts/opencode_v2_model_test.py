#!/usr/bin/env python3
"""Real, isolated check that crewhall applies --model to an OpenCode v2 session.

Run from anywhere (the repo root is added to ``sys.path``)::

    python3 scripts/opencode_v2_model_test.py

OpenCode v2 rejects ``--model``/``--agent`` in its TUI, so crewhall creates the
session and applies them through the server API. This script exercises the REAL
crewhall code against a REAL ``opencode serve`` process and a REAL TUI, with an
isolated server database and tmux sockets (it never touches a developer's state):

  1. start a real OpenCode server (isolated data dir, provider auth copied);
  2. call crewhall's own ``_configure_v2_session`` with ``--model <provider/model>``;
  3. assert the server API reports that model for the session AND the TUI footer
     shows it;
  4. re-apply (what crewhall does on restart) and assert both again.

The installed binary here is 1.19.0, which already speaks the v2 session API
(``/api/session``, ``/api/session/{id}/model``) but has no ``/api/info`` (v2's
readiness probe). The feature is therefore driven through crewhall's real code
and the TUI is attached to the very session crewhall configured; the model
application is verified end to end.

Exit code 0 = every check passed. Set ``SMOKE_MODEL=provider/model`` to pick the
model (default: a cheap one).
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from crewhall import Controller  # noqa: E402
from crewhall.opencode_link import OpenCodeLink  # noqa: E402

# The canonical provider id (an alias like ``opencode`` is only canonicalised by
# the CLI/config, not by the v2 session API).
MODEL = os.environ.get("SMOKE_MODEL") or "opencode-go/deepseek-v4.1-flash"
_ROOT: str | None = None


def _real_opencode() -> str:
    for candidate in (os.environ.get("CREWHALL_REAL_OPENCODE"),
                      shutil.which("opencode"),
                      os.path.expanduser("~/.opencode/bin/opencode")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit("no opencode binary found (set CREWHALL_REAL_OPENCODE)")


def isolate() -> str:
    """A private server data dir, auth copy and tmux sockets (never the real ones)."""
    global _ROOT
    if _ROOT is not None:
        return _ROOT
    root = _ROOT = tempfile.mkdtemp(prefix="at-v2model-")
    os.environ.update({
        "CREWHALL_TMUX_SOCKET": f"at_v2model_{os.getpid()}",
        "AGENT_TERMINAL_TMUX_SOCKET": f"at_v2model_{os.getpid()}",
        "CREWHALL_HOOKS": "0",
    })
    # Provider credentials live in the data dir; copy them (0600) into the
    # isolated server dir so the server can authenticate without touching the
    # user's database.
    auth = os.path.expanduser("~/.local/share/opencode/auth.json")
    if os.path.isfile(auth):
        dest_dir = os.path.join(root, "data", "opencode")
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        shutil.copyfile(auth, os.path.join(dest_dir, "auth.json"))
        os.chmod(os.path.join(dest_dir, "auth.json"), 0o600)
    return root


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
        if not ok:
            self.failures += 1
        return ok


def _api(port: int, password: str, path: str, method: str = "GET",
         body: dict | None = None) -> tuple[int, object]:
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Basic {token}"})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:200]
    except Exception:  # noqa: BLE001 - not listening yet
        return 0, None


def start_server(real: str, cwd: str, port: int, password: str, data_home: str):
    env = dict(os.environ)
    env["OPENCODE_SERVER_PASSWORD"] = password
    env["XDG_DATA_HOME"] = data_home  # isolated database; config/auth stay real
    proc = subprocess.Popen(
        [real, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        status, _ = _api(port, password, "/api/session")
        if status == 200:
            return proc
        time.sleep(0.3)
    return None


def _footer_line(screen: str) -> str:
    for line in screen.splitlines():
        if "Build" in line and "\u00b7" in line:
            return line.strip()
    return ""


def view_footer(real: str, workdir: str, port: int, password: str,
                session_id: str, token: str, sock: str, timeout: float = 45.0) -> str:
    """Attach the real TUI to the session; return the screen with the model.

    The server (and its database) is the isolated one; the TUI is the real,
    installed one, attached to the very session crewhall configured.
    """
    tmux = ["tmux", "-L", sock]
    subprocess.run([*tmux, "kill-server"], capture_output=True)
    cmd = (f"cd {workdir} && OPENCODE_SERVER_PASSWORD={password} "
           f"{real} attach http://127.0.0.1:{port} --session {session_id}")
    subprocess.run([*tmux, "new-session", "-d", "-x", "120", "-y", "40",
                    "-s", "view", cmd], capture_output=True)
    screen = ""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            out = subprocess.run([*tmux, "capture-pane", "-p", "-t", "view"],
                                 capture_output=True, text=True)
            screen = out.stdout
            if token.lower() in screen.lower():
                return screen
            time.sleep(0.6)
    finally:
        subprocess.run([*tmux, "kill-server"], capture_output=True)
    return screen


def main() -> int:
    real = _real_opencode()
    # The TUI needs the real provider catalog only to render the model's display
    root = isolate()
    report = Report()
    controller = Controller(adopt=False, persist=False)
    workdir = os.path.join(root, "work")
    os.makedirs(workdir, exist_ok=True)
    data_home = os.path.join(root, "data")
    port = 0
    password = "v2model-" + str(os.getpid())
    server = None
    try:
        # A free port without importing the controller's helper (kept minimal).
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = start_server(real, workdir, port, password, data_home)
        if server is None:
            print("the real OpenCode server did not start")
            return 1
        link = OpenCodeLink(port, password, api="v2")
        token = MODEL.split("/", 1)[-1].split("-")[0].split(".")[0]
        print(f"real opencode: {real}")
        print(f"model:         {MODEL}")
        print(f"sandbox:       {root}\n")

        print("Phase 1 — apply --model through crewhall")
        first = controller._configure_v2_session(
            "real-agent", "opencode", link, ["--model", MODEL], session_id=None)
        session_id = first.get("session_id") or ""
        report.check(bool(session_id), "crewhall created the v2 session", session_id)
        report.check(first.get("model_applied") is True, "the model was applied via the API")
        status, data = _api(port, password, f"/api/session/{session_id}")
        running = ((data or {}).get("data") or {}).get("model") or {}
        report.check(running.get("id") == MODEL.split("/", 1)[-1],
                     "the server API reports the requested model",
                     f"{running.get('providerID')}/{running.get('id')}")
        screen = view_footer(real, workdir, port, password, session_id, token, "at_v2view1")
        report.check(token.lower() in screen.lower(),
                     "the TUI footer shows the model", _footer_line(screen))

        print("\nPhase 2 — re-apply (restart) and verify again")
        second = controller._configure_v2_session(
            "real-agent", "opencode", link, ["--model", MODEL], session_id=session_id)
        report.check(second.get("model_applied") is True,
                     "the model was re-applied on restart")
        status, data = _api(port, password, f"/api/session/{session_id}")
        running2 = ((data or {}).get("data") or {}).get("model") or {}
        report.check(running2.get("id") == MODEL.split("/", 1)[-1],
                     "the restarted session still uses the requested model",
                     f"{running2.get('providerID')}/{running2.get('id')}")
        screen2 = view_footer(real, workdir, port, password, session_id, token, "at_v2view2")
        report.check(token.lower() in screen2.lower(),
                     "the TUI footer shows the model after restart", _footer_line(screen2))
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(server.pid), signal.SIGTERM)
            with contextlib.suppress(Exception):
                server.wait(timeout=5)
        subprocess.run(["tmux", "-L", "at_v2view1", "kill-server"], capture_output=True)
        subprocess.run(["tmux", "-L", "at_v2view2", "kill-server"], capture_output=True)
        with contextlib.suppress(Exception):
            controller.shutdown(kill_agents=True)
        if os.environ.get("AT_KEEP") != "1":
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"kept sandbox: {root}")

    print(f"\n{report.checks - report.failures}/{report.checks} checks passed.")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
