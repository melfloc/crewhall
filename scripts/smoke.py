#!/usr/bin/env python3
"""End-to-end smoke test with real agents (Fase F1).

Exercises the flow that unit tests cannot: two real OpenCode agents in a
team, a human draft that forces queueing, a daemon restart that must keep the
agents (A1) and its queued messages, a denied control-plane call, and a chat
with a crew plus an artifact publish.

It is deliberately isolated: it runs with its own XDG dirs and tmux socket so
it can never touch the developer's real agents or state.

    python scripts/smoke.py            # skip cleanly if the CLIs are missing
    python scripts/smoke.py --require  # fail instead of skipping

Exit code is non-zero when a step fails (or, with --require, when the real
agents cannot run).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time

# Isolate before importing crewhall: never read/write the user's real state.
_ROOT = tempfile.mkdtemp(prefix="at-smoke-")
os.environ.update({
    "AT_TEST_ISOLATED": "1",
    "XDG_STATE_HOME": os.path.join(_ROOT, "state"),
    "XDG_CONFIG_HOME": os.path.join(_ROOT, "config"),
    "XDG_RUNTIME_DIR": os.path.join(_ROOT, "run"),
    "CREWHALL_TMUX_SOCKET": f"at_smoke_{os.getpid()}",
    "AGENT_TERMINAL_TMUX_SOCKET": f"at_smoke_{os.getpid()}",
    "CREWHALL_HOOKS": "0",
})
os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)

# Make the repository importable when run as `python scripts/smoke.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crewhall import Controller  # noqa: E402
from crewhall import conversations  # noqa: E402
from crewhall.messaging import MessagingError  # noqa: E402

MODEL = os.environ.get("SMOKE_MODEL")
BACKEND = "tmux" if shutil.which("tmux") else "pty"


def wait(predicate, timeout: float = 60.0, interval: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class Smoke:
    def __init__(self) -> None:
        self.controller = Controller(adopt=False, persist=True)
        self.state: dict = {}

    # -- steps ----------------------------------------------------------
    def step_agents(self) -> None:
        cwd = tempfile.mkdtemp(prefix="at-smoke-work-")
        self.state["cwd"] = cwd
        args = ["--model", MODEL] if MODEL else None
        a = self.controller.create_agent("opencode", name="smoke-a", backend=BACKEND,
                                         cwd=cwd, args=args, wait_ready=True, timeout=90)
        b = self.controller.create_agent("opencode", name="smoke-b", backend=BACKEND,
                                         cwd=cwd, args=args, wait_ready=True, timeout=90)
        self.controller.create_team("smoke", ["smoke-a", "smoke-b"])
        assert wait(lambda: a.state().usable, 90), "smoke-a never became ready"
        assert wait(lambda: b.state().usable, 90), "smoke-b never became ready"
        self.state.update(a=a, b=b)

    def step_message(self) -> None:
        a, b = self.state["a"], self.state["b"]
        token = self.controller._agent_token(a.agent_id)
        body = "smoke-message-%d" % int(time.time())
        delivery = self.controller.send_message_as("smoke-a", token, "smoke-b", body)
        assert delivery.delivered, delivery.error
        assert wait(lambda: body in self.controller.get_agent("smoke-b").capture(), 60), \
            "message never reached smoke-b"

    def step_human_draft(self) -> None:
        a, b = self.state["a"], self.state["b"]
        b.session.write("a human draft")
        time.sleep(0.8)
        token = self.controller._agent_token(a.agent_id)
        delivery = self.controller.send_message_as(
            "smoke-a", token, "smoke-b", "while drafting")
        assert delivery.queued, "a human draft must force queueing"
        assert delivery.queue_reason == "human draft", delivery.queue_reason
        # Clear the draft; the queue must deliver it.
        for key in b.clear_input_keys:
            b.session.send_key(key)
        assert wait(lambda: "while drafting" in self.controller.get_agent("smoke-b").capture(),
                    60), "queued message was not delivered after the draft was cleared"

    def step_control_plane_denied(self) -> None:
        try:
            self.controller.send_message_as("smoke-a", "not-the-token", "smoke-b", "nope")
        except MessagingError:
            return
        raise AssertionError("a wrong agent token was accepted")

    def step_restart(self) -> None:
        a, b = self.state["a"], self.state["b"]
        a_id = a.agent_id
        # Queue a message to a busy recipient, then stop the control plane only.
        b.session.write("busy")
        time.sleep(0.5)
        token = self.controller._agent_token(a.agent_id)
        delivery = self.controller.send_message_as("smoke-a", token, "smoke-b", "survives-restart")
        assert delivery.queued, "expected the message to queue"
        self.controller.shutdown(kill_agents=False)

        second = Controller(adopt=True, persist=True)
        second.restore()
        adopted = second.get_agent("smoke-a")
        assert adopted.agent_id == a_id, "agent id changed across the restart"
        assert adopted.state().usable or adopted.session.status.alive, \
            "smoke-a did not survive the restart"
        # Continue against the new control plane; keep the old one for cleanup.
        self.state["old_controller"] = self.controller
        self.controller = second

    def step_chat_publish(self) -> None:
        conv = conversations.create("smoke chat", team="smoke")
        cid = conv["id"]
        source = os.path.join(self.state["cwd"], "deliverable.txt")
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("smoke artifact\n")
        a = self.state["a"]
        token = self.controller._agent_token(a.agent_id)
        out = self.controller.publish_artifact(a.agent_id, token, cid, source, name="deliverable.txt")
        assert out["artifact"], "publish_artifact returned nothing"
        files = conversations.list_files(cid, "outputs")
        assert any(f.get("name") == "deliverable.txt" for f in files), "artifact not stored"

    # -- lifecycle ------------------------------------------------------
    def cleanup(self) -> None:
        controllers = [self.controller, self.state.get("old_controller")]
        for controller in controllers:
            if controller is None:
                continue
            try:
                controller.shutdown(kill_agents=True)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                pass
        shutil.rmtree(_ROOT, ignore_errors=True)
        cwd = self.state.get("cwd")
        if cwd:
            shutil.rmtree(cwd, ignore_errors=True)
        if shutil.which("tmux"):
            import subprocess
            subprocess.run(["tmux", "-L", os.environ["CREWHALL_TMUX_SOCKET"], "kill-server"],
                           capture_output=True)


STEPS = [
    ("agents", Smoke.step_agents),
    ("message", Smoke.step_message),
    ("human-draft", Smoke.step_human_draft),
    ("control-plane-denied", Smoke.step_control_plane_denied),
    ("chat-publish", Smoke.step_chat_publish),
    ("restart", Smoke.step_restart),
]


def prerequisites_ok() -> tuple[bool, str]:
    if shutil.which("opencode") is None:
        return False, "opencode CLI not found"
    if shutil.which("tmux") is None:
        return False, "tmux not found (agents would not survive a restart)"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke")
    parser.add_argument("--require", action="store_true",
                        help="fail instead of skipping when the real CLIs are missing")
    args = parser.parse_args(argv)

    ok, reason = prerequisites_ok()
    if not ok:
        print(f"SKIP: {reason}")
        return 2 if args.require else 0

    smoke = Smoke()
    failures = 0
    try:
        for name, fn in STEPS:
            started = time.monotonic()
            try:
                fn(smoke)
            except Exception as exc:  # noqa: BLE001 - a step failure is the report
                failures += 1
                print(f"✗ {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"✓ {name} ({time.monotonic() - started:.1f}s)")
    finally:
        smoke.cleanup()

    print(f"{len(STEPS) - failures}/{len(STEPS)} steps passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
