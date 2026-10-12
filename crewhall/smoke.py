"""End-to-end smoke test with real agents (Fase F1).

Exercises the flow that unit tests cannot: two real OpenCode agents in a
team, a human draft that forces queueing, a daemon restart that must keep the
agents (A1) and its queued messages, a denied control-plane call, and a chat
with a crew plus an artifact publish.

It is deliberately isolated: it runs with its own XDG dirs and tmux socket so
it can never touch the developer's real agents or state. This module is the
implementation; ``scripts/smoke.py`` and ``crewhall watch smoke`` both call it.

    crewhall watch smoke            # skip cleanly if the CLIs are missing
    crewhall watch smoke --require  # fail instead of skipping

Exit code is non-zero when a step fails (or, with --require, when the real
agents cannot run).
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import tempfile
import time

from . import Controller
from . import chat as conversations
from .messaging import MessagingError

MODEL = os.environ.get("SMOKE_MODEL")
BACKEND = "tmux" if shutil.which("tmux") else "pty"

# The isolation root, created once per process by ``_isolate``.
_ROOT: str | None = None


def _isolate() -> str:
    """Give this process its own XDG dirs and tmux socket (idempotent)."""
    global _ROOT
    if _ROOT is not None:
        return _ROOT
    root = _ROOT = tempfile.mkdtemp(prefix="at-smoke-")
    os.environ.update({
        "AT_TEST_ISOLATED": "1",
        "XDG_STATE_HOME": os.path.join(root, "state"),
        "XDG_CONFIG_HOME": os.path.join(root, "config"),
        "XDG_RUNTIME_DIR": os.path.join(root, "run"),
        "CREWHALL_TMUX_SOCKET": f"at_smoke_{os.getpid()}",
        "AGENT_TERMINAL_TMUX_SOCKET": f"at_smoke_{os.getpid()}",
        "CREWHALL_HOOKS": "0",
    })
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
    # Drop any runtime dir resolved before isolation (import-time callers).
    with contextlib.suppress(Exception):
        from . import paths

        paths._RUNTIME_CHOICE.clear()
    return root


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
        a = self.state["a"]
        token = self.controller._agent_token(a.agent_id)
        body = f"smoke-message-{int(time.time())}"
        delivery = self.controller.send_message_as("smoke-a", token, "smoke-b", body)
        assert delivery.delivered, delivery.error
        assert wait(lambda: body in self.controller.get_agent("smoke-b").capture(), 60), \
            "message never reached smoke-b"

    def step_human_draft(self) -> None:
        a, b = self.state["a"], self.state["b"]
        # The previous step started a turn in smoke-b: a draft only counts as
        # the reason once the agent is idle (otherwise it is queued as "busy").
        assert wait(lambda: b.state().usable, 120), "smoke-b never went idle"
        b.session.write("a human draft")
        time.sleep(0.8)
        token = self.controller._agent_token(a.agent_id)
        delivery = self.controller.send_message_as(
            "smoke-a", token, "smoke-b", "while drafting")
        assert delivery.queued, f"a human draft must force queueing: {delivery.to_dict()}"
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
        # Queue a message behind a draft, then stop the control plane only.
        assert wait(lambda: b.state().usable, 120), "smoke-b never went idle"
        b.session.write("a draft across the restart")
        time.sleep(0.5)
        token = self.controller._agent_token(a.agent_id)
        delivery = self.controller.send_message_as("smoke-a", token, "smoke-b", "survives-restart")
        assert delivery.queued, f"expected the message to queue: {delivery.to_dict()}"
        self.controller.shutdown(kill_agents=False)

        second = Controller(adopt=True, persist=True)
        second.restore()
        adopted = second.get_agent("smoke-a")
        assert adopted.agent_id == a_id, "agent id changed across the restart"
        assert adopted.state().usable or adopted.session.status.alive, \
            "smoke-a did not survive the restart"
        # The surviving agent still carries its old CREWHALL_TOKEN: the new
        # daemon must accept it (agent tokens persist across restarts).
        after = second.send_message_as("smoke-a", token, "smoke-b", "after-restart")
        assert after.delivered or after.queued, f"token rejected after restart: {after.to_dict()}"
        # Clear the draft: the message queued before the restart is delivered.
        b2 = second.get_agent("smoke-b")
        for key in b2.clear_input_keys:
            b2.session.send_key(key)
        assert wait(lambda: "survives-restart" in b2.capture(), 90), \
            "the message queued before the restart was not delivered"
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
        # Artifacts get a unique name (deliverable-<hex>.txt): match the relpath.
        relpath = out["artifact"]["relpath"]
        files = conversations.list_files(cid, "outputs")
        assert any(f.get("relpath") == relpath for f in files), f"artifact not stored: {files}"
        info = conversations.info(cid)
        produced = {o["relpath"]: o.get("producer") for o in info["outputs"]}
        assert produced.get(relpath) == a.agent_id, f"producer not recorded: {produced}"

    # -- lifecycle ------------------------------------------------------
    def cleanup(self) -> None:
        controllers = [self.controller, self.state.get("old_controller")]
        for controller in controllers:
            if controller is None:
                continue
            with contextlib.suppress(Exception):  # cleanup is best effort
                controller.shutdown(kill_agents=True)
        root = _ROOT
        if root:
            shutil.rmtree(root, ignore_errors=True)
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


def write_result(path: str, total: int, failed: list[str]) -> dict:
    """Persist the smoke outcome (layer 3) as a 0600 JSON file."""
    from . import health

    data = health.smoke_result(total, failed)
    if path:
        health.write_json(path, data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke")
    parser.add_argument("--require", action="store_true",
                        help="fail instead of skipping when the real CLIs are missing")
    parser.add_argument("--result", default=None, metavar="PATH",
                        help="write the outcome JSON here (default: state/health/smoke-<date>.json)")
    parser.add_argument("--no-notify", action="store_true",
                        help="do not send a desktop notification when a step fails")
    args = parser.parse_args(argv)

    _isolate()

    ok, reason = prerequisites_ok()
    if not ok:
        print(f"SKIP: {reason}")
        return 2 if args.require else 0

    smoke = Smoke()
    failures: list[str] = []
    try:
        for name, fn in STEPS:
            started = time.monotonic()
            try:
                fn(smoke)
            except Exception as exc:  # noqa: BLE001 - a step failure is the report
                failures.append(name)
                print(f"✗ {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"✓ {name} ({time.monotonic() - started:.1f}s)")
    finally:
        smoke.cleanup()

    result_path = args.result
    if result_path is None:
        from . import health

        result_path = health.smoke_path()
    write_result(result_path, len(STEPS), failures)
    if failures and not args.no_notify:
        from . import watch

        watch.notify("crewhall smoke failed",
                     f"{len(failures)}/{len(STEPS)} step(s) failed: {', '.join(failures)}")

    print(f"{len(STEPS) - len(failures)}/{len(STEPS)} steps passed")
    return 1 if failures else 0
