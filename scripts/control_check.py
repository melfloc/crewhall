#!/usr/bin/env python3
"""Real, isolated control check with three OpenCode agents (lead + 2 workers).

This is not part of the default test suite: it launches real agents and spends a
few tokens. It runs in its own XDG dirs and tmux socket (like ``crewhall.smoke``)
so it can never touch the developer's real agents or state.

    SMOKE_MODEL=opencode/deepseek-v4.1-flash python scripts/control_check.py

It exercises the lead-authority model end to end:

* a lead sees a worker's background shells and stops them with --all-shells;
* a message queued behind a working turn is reported blocked and delivered
  after ``agent interrupt``;
* ``agent restart`` keeps the identity and resumes the conversation (memory of
  a secret survives), then --fresh starts clean, and the brake rejects a 4th;
* a non-lead worker is refused for signal/restart.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from crewhall import Controller  # noqa: E402
from crewhall.messaging import MessagingError  # noqa: E402

MODEL = os.environ.get("SMOKE_MODEL")
BACKEND = "tmux" if shutil.which("tmux") else "pty"
_ROOT: str | None = None


def isolate() -> str:
    global _ROOT
    if _ROOT is not None:
        return _ROOT
    root = _ROOT = tempfile.mkdtemp(prefix="at-control-")
    os.environ.update({
        "AT_TEST_ISOLATED": "1",
        "XDG_STATE_HOME": os.path.join(root, "state"),
        "XDG_CONFIG_HOME": os.path.join(root, "config"),
        "XDG_RUNTIME_DIR": os.path.join(root, "run"),
        "CREWHALL_TMUX_SOCKET": f"at_control_{os.getpid()}",
        "AGENT_TERMINAL_TMUX_SOCKET": f"at_control_{os.getpid()}",
        "CREWHALL_HOOKS": "0",
    })
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
    with contextlib.suppress(Exception):
        from crewhall import paths

        paths._RUNTIME_CHOICE.clear()
    return root


def wait(predicate, timeout: float = 90.0, interval: float = 0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class Check:
    def __init__(self) -> None:
        self.c = Controller(adopt=False, persist=True)
        self.steps_failed: list[str] = []

    # -- setup ----------------------------------------------------------
    def step_agents(self) -> None:
        cwd = tempfile.mkdtemp(prefix="at-control-work-")
        self.cwd = cwd
        args = ["--model", MODEL] if MODEL else None

        def make(name):
            return self.c.create_agent("opencode", name=name, backend=BACKEND, cwd=cwd,
                                       args=args, wait_ready=True, timeout=120)

        self.lead = make("lead")
        self.w1 = make("worker-1")
        self.w2 = make("worker-2")
        self.c.create_team("crew", ["lead", "worker-1", "worker-2"], leads=["lead"])
        for h in (self.lead, self.w1, self.w2):
            assert wait(lambda h=h: h.state().usable, 120), f"{h.name} never became ready"

    def token(self, h) -> str:
        return self.c._agent_token(h.agent_id)

    # -- T3: shells + signal --all-shells -------------------------------
    def step_shells(self) -> None:
        body = ("Run this exact bash command now, in one call, and nothing else "
                "(it stays running): sleep 600 & sleep 600 & wait")
        for _ in range(3):                      # the model is stochastic; give it a few tries
            # Resend only once the previous copy was delivered and handled: a copy
            # still queued would start new shells after the kill and keep the
            # worker busy for the next steps.
            wait(lambda: self.w1.state().usable
                 and not self.c.messaging.queue_info(self.w1.agent_id)["count"], 120)
            if self._shell_pids(self.w1.agent_id):
                break
            self.c.send_message_as("lead", self.token(self.lead), "worker-1", body)
            if wait(lambda: self._shell_pids(self.w1.agent_id), 90):
                break
        assert self._shell_pids(self.w1.agent_id), "worker-1 never started background shells"

        st = self.c.agent_status(self.w1.agent_id)
        shells = st["processes"]["shells"]
        pids = [s["pid"] for s in shells]
        assert pids, "the lead did not see worker-1's shells"
        ps = subprocess.run(["ps", "-o", "pid=,args=", "-p", ",".join(map(str, pids))],
                            capture_output=True, text=True).stdout.strip()
        print(f"    worker-1 shells: {pids}\n    ps: {ps!r}")
        self.c.signal_agent(self.w1.agent_id, "TERM", all_shells=True, by=self.lead.agent_id)
        assert wait(lambda: all(not alive(p) for p in pids), 20), \
            f"shells survived --all-shells: {[p for p in pids if alive(p)]}"
        print(f"    after --all-shells, still alive: {[p for p in pids if alive(p)]}")

    def _shell_pids(self, agent_id: str) -> list[int]:
        with contextlib.suppress(Exception):
            return [s["pid"] for s in self.c.list_processes(agent_id).get("shells", [])]
        return []

    # -- T2/T3: status blocked, interrupt, message delivered ------------
    def step_interrupt_queued(self) -> None:
        assert wait(lambda: self.w1.state().usable, 120), "worker-1 stuck"
        self.c.send_message_as("lead", self.token(self.lead), "worker-1",
                               "Run: sleep 120 ; then reply DONE")
        assert wait(lambda: self.w1.state().value == "working", 60), \
            "worker-1 never started a long turn"
        delivery = self.c.send_message_as("lead", self.token(self.lead), "worker-1",
                                          f"queued-{int(time.time())}")
        assert delivery.queued, f"message should queue behind a working turn: {delivery.to_dict()}"
        st = self.c.agent_status(self.w1.agent_id)
        assert "working" in st["blocked_by"], st["blocked_by"]
        assert st["deliverable_now"] is False
        assert self.c.interrupt_agent(self.w1.agent_id, by=self.lead.agent_id)["interrupted"]
        assert wait(lambda: self.w1.state().usable, 60), "worker-1 did not stop"

    # -- T4: forced restart, resume, fresh, brake -----------------------
    def step_restart(self) -> None:
        assert wait(lambda: self.w1.state().usable, 120), "worker-1 stuck"
        secret = f"ZEPHYR-{int(time.time())}"
        self.c.send_message_as("lead", self.token(self.lead), "worker-1",
                               f"Remember this codeword: {secret}. Reply OK.")
        assert wait(lambda: self.w1.state().usable, 120), "worker-1 never went idle"
        w1_id = self.w1.agent_id
        tree = self.c._capture_tree(self.w1)

        first = self.c.force_restart_agent(self.w1.agent_id, by=self.lead.agent_id,
                                           reason="control-check")
        assert first["resumed"] is True, first
        assert self.w1.agent_id == w1_id, "identity changed across restart"
        assert not [p for p in tree if alive(p)], "old process tree survived the restart"
        print(f"    restart terminated: {first['terminated']}")
        self.w1 = self.c.get_agent(w1_id)
        assert wait(lambda: any("restarted by" in m.get("body", "")
                               for m in self.c.messaging.history(agent=w1_id, limit=20)), 120), \
            "the restarted agent got no system notice"
        # The resumed conversation remembers the secret.
        self.c.send_message_as("lead", self.token(self.lead), "worker-1",
                               "What was the codeword? Reply with just the codeword.")
        assert wait(lambda: secret in self.w1.capture(), 90), \
            "the resumed conversation lost its memory"

        fresh = self.c.force_restart_agent(self.w1.agent_id, fresh=True,
                                           by=self.lead.agent_id, reason="fresh")
        assert fresh["fresh"] is True, fresh
        self.w1 = self.c.get_agent(w1_id)
        self.c.force_restart_agent(self.w1.agent_id, fresh=True, by=self.lead.agent_id,
                                   reason="third")
        try:
            self.c.force_restart_agent(self.w1.agent_id, fresh=True, by=self.lead.agent_id,
                                       reason="fourth")
        except MessagingError as exc:
            assert "brake" in str(exc), exc
        else:
            raise AssertionError("the 4th forced restart was not rejected")

    # -- T3: a non-lead cannot contain a teammate -----------------------
    def step_non_lead_refused(self) -> None:
        for action in (
            lambda: self.c._authorize_lead(self.w2, self.w1),
            lambda: self.c._authorize_lead(self.w2, self.lead),
        ):
            try:
                action()
            except MessagingError:
                continue
            raise AssertionError("a non-lead was allowed to contain a teammate")

    # -- lifecycle ------------------------------------------------------
    def cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.c.shutdown(kill_agents=True)
        if _ROOT:
            shutil.rmtree(_ROOT, ignore_errors=True)
        with contextlib.suppress(Exception):
            shutil.rmtree(getattr(self, "cwd", ""), ignore_errors=True)
        if shutil.which("tmux"):
            subprocess.run(["tmux", "-L", os.environ["CREWHALL_TMUX_SOCKET"], "kill-server"],
                           capture_output=True)


STEPS = [
    ("agents", Check.step_agents),
    ("shells+signal", Check.step_shells),
    ("interrupt+queued", Check.step_interrupt_queued),
    ("restart", Check.step_restart),
    ("non-lead-refused", Check.step_non_lead_refused),
]


def main(argv: list[str] | None = None) -> int:
    if shutil.which("opencode") is None:
        print("SKIP: opencode CLI not found")
        return 2
    isolate()
    check = Check()
    failures: list[str] = []
    try:
        for name, fn in STEPS:
            started = time.monotonic()
            try:
                fn(check)
            except Exception as exc:  # noqa: BLE001 - the report is the point
                failures.append(name)
                print(f"✗ {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"✓ {name} ({time.monotonic() - started:.1f}s)")
    finally:
        check.cleanup()
    print(f"{len(STEPS) - len(failures)}/{len(STEPS)} steps passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
