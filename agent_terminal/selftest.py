"""``crewhall selftest``: a real end-to-end check of the installed system.

Creates a throw-away team with real agents (one pair per available CLI kind), exercises
the stack the way you use it (messaging with attribution, delivery status, turn signals,
full conversation history, interrupt), and always cleans up after itself. It spends a few
tokens per agent; ``--quick`` skips the interrupt step (the longest one).

Use it before cutting a release and right after every deploy to a new machine.
"""
from __future__ import annotations

import shutil
import time
import uuid
from typing import Any
from collections.abc import Callable

from .client import Client, RpcError, ping

Log = Callable[[str], None]


class _Report:
    def __init__(self, log: Log) -> None:
        self.steps: list[dict[str, Any]] = []
        self.log = log

    def step(self, name: str):
        report = self

        class Ctx:
            def __enter__(self_inner):
                self_inner.t0 = time.monotonic()
                return self_inner

            def __exit__(self_inner, exc_type, exc, tb):
                seconds = round(time.monotonic() - self_inner.t0, 1)
                ok = exc is None
                detail = "" if ok else (str(exc) or exc_type.__name__)
                report.steps.append({"step": name, "ok": ok, "detail": detail, "seconds": seconds})
                report.log(f"  {'✓' if ok else '✗'} {name} ({seconds}s)" + (f" — {detail}" if detail else ""))
                return exc_type is not None and issubclass(exc_type, Exception)  # report it, keep going

        return Ctx()

    @property
    def ok(self) -> bool:
        return all(s["ok"] for s in self.steps)


class _Fail(Exception):
    pass


def _failed(report: _Report) -> bool:
    return not report.steps[-1]["ok"]


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise _Fail(msg)


def _wait(fn: Callable[[], bool], timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return fn()


def run(cwd: str, kinds: list[str] | None = None, quick: bool = False, log: Log = print) -> dict[str, Any]:
    kinds = [k for k in (kinds or ["claude", "opencode"])]
    report = _Report(log)
    suffix = uuid.uuid4().hex[:6]
    team = f"selftest-{suffix}"
    created: list[str] = []
    team_id: list[str] = []
    with report.step("daemon reachable"):
        _expect(bool(ping()), "daemon not running")
    client = Client()
    try:
        with report.step("create the throw-away team"):
            team_id.append(client.call("team_create", name=team, agent_ids=[], workspace=cwd)["team"]["team_id"])
        if _failed(report):
            return {"ok": False, "steps": report.steps}
        for kind in kinds:
            if not shutil.which(kind):
                log(f"  - {kind}: CLI not installed, skipped")
                continue
            log(f"[{kind}]")
            _kind(client, report, kind, team, suffix, cwd, quick, created)
        with report.step("web interfaces report their state"):
            st = client.call("frontend_status")
            _expect(st["mode"] in ("off", "local", "tailscale", "both"), f"odd mode {st.get('mode')}")
    finally:
        with report.step("cleanup (agents and team removed)"):
            for name in created:
                try:
                    client.call("agent_stop", target=name)
                except RpcError:
                    pass
            for tid in team_id:
                try:
                    client.call("team_remove", target=tid)
                except RpcError:
                    pass
            left = [a["name"] for a in client.call("agent_list")["agents"] if a["name"] in created]
            _expect(not left, f"agents left behind: {left}")
            teams_left = [t["name"] for t in client.call("team_list")["teams"] if t["name"] == team]
            _expect(not teams_left, f"team left behind: {teams_left}")
    return {"ok": report.ok, "steps": report.steps}


def _kind(client: Client, report: _Report, kind: str, team: str, suffix: str,
          cwd: str, quick: bool, created: list[str]) -> None:
    a, b = f"st-{kind}-a-{suffix}", f"st-{kind}-b-{suffix}"
    marker = f"SELFTEST_{uuid.uuid4().hex[:8].upper()}"
    with report.step(f"{kind}: create 2 agents in the team"):
        for name in (a, b):
            client.call("agent_create", kind=kind, name=name, team=team, cwd=cwd)
            created.append(name)
    if _failed(report):
        return
    with report.step(f"{kind}: both agents become ready"):
        for name in (a, b):
            ok = _wait(lambda n=name: client.call("agent_state", target=n)["agent"]["state"]
                       in ("ready", "waiting_input"), 90, 1.0)
            _expect(ok, f"{name} never became ready (for Claude: open it once in {cwd} and accept the "
                        "folder-trust dialog)")
    if _failed(report):
        return
    sent: dict[str, Any] = {}
    with report.step(f"{kind}: message a→b is delivered and acknowledged"):
        d = client.call("message_send", sender=a, recipient=b,
                        body=f"Responde exactamente con la palabra {marker} y nada más.")["delivery"]
        sent["id"] = d["message_id"]
        _expect(d["delivered"] or d["queued"], d.get("error") or "not delivered")

        def acked() -> bool:
            hist = client.call("message_history", agent=b, limit=20)["messages"]
            return any(m["message_id"] == sent["id"] and m["status"] == "acknowledged" for m in hist)

        _expect(_wait(acked, 40), "delivered but the recipient never started a turn (status stuck at injected/queued)")
    if _failed(report):
        return
    with report.step(f"{kind}: turn completes (exact signal) and the reply is in the conversation history"):
        _expect(_wait(lambda: client.call("agent_state", target=b)["agent"]["state"] == "waiting_input", 90),
                "agent did not return to waiting_input")

        def history_has_both() -> bool:
            h = client.call("agent_history", target=b)
            if not h.get("available"):
                return False
            texts = [m["text"] for m in h["messages"]]
            return any(f"[from: {a}]" in t for t in texts) and any(
                m["role"] == "assistant" and marker in m["text"] for m in h["messages"])

        _expect(_wait(history_has_both, 30),
                "history missing the attributed incoming message and/or the reply containing the marker")
    if _failed(report):
        return
    if not quick:
        with report.step(f"{kind}: Ctrl+C-style interrupt stops a running turn"):
            client.call("agent_write", target=a, text="Escribe un ensayo de 2500 palabras sobre el océano, sin usar herramientas.")
            client.call("agent_key", target=a, key="ENTER")
            _expect(_wait(lambda: client.call("agent_state", target=a)["agent"]["state"] == "working", 40),
                    "the agent never started working")
            r = client.call("agent_interrupt", target=a)
            _expect(r["interrupted"], "interrupt reported nothing to interrupt")
            _expect(_wait(lambda: client.call("agent_state", target=a)["agent"]["state"] != "working", 30),
                    "the agent kept working after the interrupt")
