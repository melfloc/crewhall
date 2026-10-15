#!/usr/bin/env python3
"""Real, isolated check of an OpenCode v1 -> v2 upgrade on an existing agent.

    CREWHALL_OPENCODE_V2=/tmp/opencode/oc-v2/node_modules/.bin/opencode \
        python3 scripts/opencode_v1_to_v2_migration_test.py

It never touches the user's real agents, settings, config or database: it runs
with private XDG dirs and tmux socket, copying only the provider ``auth.json``
(0600) and the ``opencode`` config so both v1 and v2 can authenticate. The v1
binary is ``CREWHALL_OPENCODE_V1`` (default: the ``opencode`` on PATH); the v2
binary is ``CREWHALL_OPENCODE_V2`` (required, so an isolated install is used).

Phases:

  1. create an OpenCode agent on v1 and give it a codeword (a real turn);
  2. "upgrade" the provider command to the v2 binary and force-restart the
     agent, exactly as ``crewhall agent restart`` does;
  3. ask for the codeword again and report whether the conversation was
     resumed (the agent still remembers) or a brand new session was used.

The restart must never hang, and a v1 conversation that v2 cannot resume must
fall back to a fresh session **without an error** and with a clear notice.

Exit code 0 = every check passed.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from crewhall import AgentState, Controller, notices, settings  # noqa: E402
from crewhall.harness import versions  # noqa: E402

MODEL = os.environ.get("SMOKE_MODEL") or ""
BACKEND = "tmux" if shutil.which("tmux") else "pty"
_READY = (AgentState.WAITING_INPUT, AgentState.READY)


def _find_v1() -> str:
    for candidate in (os.environ.get("CREWHALL_OPENCODE_V1"),
                      shutil.which("opencode"),
                      os.path.expanduser("~/.opencode/bin/opencode")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit("no v1 opencode found (set CREWHALL_OPENCODE_V1)")


def _find_v2() -> str:
    candidate = os.environ.get("CREWHALL_OPENCODE_V2")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    raise SystemExit("set CREWHALL_OPENCODE_V2 to an isolated v2 binary")


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

    def section(self, title: str) -> None:
        print(f"\n{title}")


def isolate() -> str:
    """Private XDG dirs + tmux socket; copy only auth and the opencode config."""
    root = tempfile.mkdtemp(prefix="at-v1v2-")
    data_home = os.path.join(root, "data")
    config_home = os.path.join(root, "config")
    os.environ.update({
        "AT_TEST_ISOLATED": "1",
        "XDG_DATA_HOME": data_home,
        "XDG_CONFIG_HOME": config_home,
        "XDG_STATE_HOME": os.path.join(root, "state"),
        "XDG_RUNTIME_DIR": os.path.join(root, "run"),
        "CREWHALL_TMUX_SOCKET": f"at_v1v2_{os.getpid()}",
        "AGENT_TERMINAL_TMUX_SOCKET": f"at_v1v2_{os.getpid()}",
        "CREWHALL_HOOKS": "0",
    })
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700, exist_ok=True)
    real_config = os.path.expanduser("~/.config/opencode")
    if os.path.isdir(real_config):
        shutil.copytree(real_config, os.path.join(config_home, "opencode"),
                        ignore=shutil.ignore_patterns("node_modules"), dirs_exist_ok=True)
    _seed_data_home(data_home)
    return root


def _seed_data_home(data_home: str) -> None:
    """An opencode data dir with the real credentials (0600), nothing else."""
    auth = os.path.expanduser("~/.local/share/opencode/auth.json")
    if os.path.isfile(auth):
        dest_dir = os.path.join(data_home, "opencode")
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        shutil.copyfile(auth, os.path.join(dest_dir, "auth.json"))
        os.chmod(os.path.join(dest_dir, "auth.json"), 0o600)


def _wait(agent, states, timeout: float) -> AgentState:
    """Wait for a state, never raising: return the current state on timeout."""
    with contextlib.suppress(Exception):
        return agent.wait_for_state(states, timeout=timeout)
    return agent.state()


def _ask(agent, prompt: str, timeout: float = 180.0) -> str:
    """Send a prompt and return the captured screen once the turn settles."""
    agent.send(prompt)
    _wait(agent, AgentState.WORKING, timeout=30.0)
    _wait(agent, _READY, timeout=timeout)
    return agent.capture()


def main() -> int:
    v1 = _find_v1()
    v2 = _find_v2()
    root = isolate()
    report = Report()
    controller = Controller(adopt=False, persist=False)
    cwd = os.path.join(root, "work")
    os.makedirs(cwd, exist_ok=True)
    agent = None
    try:
        args = ["--model", MODEL] if MODEL else []
        print(f"v1 binary: {v1}")
        print(f"v2 binary: {v2}")
        print(f"sandbox:   {root}\n")

        # -- phase 1: a v1 agent with a real conversation -------------------
        report.section("Phase 1 — create an OpenCode agent with v1 and converse")
        settings.patch({"providers.opencode.command": v1}, confirm=True)
        versions._clear_probe_cache()
        agent = controller.create_agent(
            "opencode", name="mig", backend=BACKEND, cwd=cwd,
            wait_ready=True, timeout=180, args=args)
        state = _wait(agent, (AgentState.READY, *_READY), timeout=60)
        report.check(agent.is_waiting() or state.usable, "the v1 agent is usable",
                     f"state={state.value}")

        secret = "LOBO" + uuid.uuid4().hex[:8].upper()
        _ask(agent, f"Remember the codeword {secret}. Reply with just: OK")
        v1_conv = agent.conversation_id or ""
        report.check(bool(v1_conv), "v1 conversation tracked", v1_conv)
        recap = _ask(agent, "What is the codeword I asked you to remember? "
                            "Reply with just the codeword.")
        report.check(secret.lower() in recap.lower(),
                     "the v1 agent remembers the codeword (pre-migration)",
                     f"secret={secret}")
        print(f"  v1 conversation: {v1_conv}")

        # -- phase 2: upgrade the binary and force-restart -------------------
        report.section("Phase 2 — upgrade to v2 and force-restart the agent")
        if os.environ.get("AT_V1V2_NEW_DATA") == "1":
            # Force the "not resumable" case: the v2 server gets its own, empty
            # data dir, so the v1 conversation is genuinely absent from it.
            fresh_data = os.path.join(root, "data-v2")
            _seed_data_home(fresh_data)
            os.environ["XDG_DATA_HOME"] = fresh_data
            print(f"  (v2 uses a separate data dir: {fresh_data})")
        settings.patch({"providers.opencode.command": v2}, confirm=True)
        versions._clear_probe_cache()
        notices.clear()
        started = time.monotonic()
        resp = controller.force_restart_agent(
            agent.agent_id, fresh=False, reason="v1->v2 upgrade", timeout=90)
        elapsed = time.monotonic() - started
        report.check(elapsed < 120, "the force-restart returned (no hang)",
                     f"{elapsed:.1f}s, state={resp.get('state')}")
        report.check(resp.get("state") in ("ready", "waiting_input"),
                     "the restarted agent is usable", str(resp.get("state")))
        report.check(not resp.get("error"), "the restart did not raise")
        print(f"  resumed={resp.get('resumed')} fresh={resp.get('fresh')}")

        # -- phase 3: memory after the upgrade ------------------------------
        report.section("Phase 3 — does the conversation survive the upgrade?")
        restored = controller.get_agent(agent.agent_id)
        # The server-side history is the reliable signal: the conversation either
        # kept its messages (resumed) or it did not. A single model turn is only
        # extra evidence, because the model itself may be slow/unavailable.
        try:
            history = json.dumps(restored.history(200), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - report a read failure honestly
            history = ""
            print(f"  (history read failed: {exc})")
        by_history = secret.lower() in history.lower()
        print(f"  conversation id before={v1_conv} after={restored.conversation_id}")
        if by_history:
            report.check(True, "the v2 conversation still holds the codeword "
                               "(conversation resumed)")
            try:
                after = _ask(restored, "What is the codeword I asked you to "
                                       "remember? Reply with just the codeword.",
                             timeout=60)
                print(f"  [info] v2 reply still has the codeword: "
                      f"{secret.lower() in after.lower()}")
            except Exception as exc:  # noqa: BLE001 - the model may be slow here
                print(f"  [info] v2 turn skipped: {exc}")
        else:
            print("  conversation NOT resumed (empty/new session in v2)")
            migrate = [n for n in notices.pending()
                       if str(n.get("key", "")).startswith("v2migrate:")]
            report.check(bool(migrate), "a 'v1 not resumable in v2' notice was recorded",
                         migrate[0]["title"] if migrate else "no notice")
            report.check(bool(resp.get("fresh")), "the restart fell back to a fresh session",
                         f"resumed={resp.get('resumed')} fresh={resp.get('fresh')}")
        print(f"\nnotices: {[n.get('title') for n in notices.pending()]}")
    finally:
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
