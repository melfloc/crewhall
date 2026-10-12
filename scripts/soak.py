#!/usr/bin/env python3
"""Weekly soak test (layer 4, ~2 h): 500 messages, random drafts, 3 daemon
restarts and one agent killed mid-way, then the invariants.

It runs isolated (its own XDG dirs and tmux socket) so it can never touch the
developer's real agents or state. The invariant logic lives in
``crewhall.soak`` and is unit-tested; this script only drives the workload.

    python scripts/soak.py                 # the full weekly run
    python scripts/soak.py --quick         # a short sanity run (do not soak)
    python scripts/soak.py --no-notify     # no desktop notification on failure
"""
from __future__ import annotations

import argparse
import contextlib
import os
import random
import shutil
import sys
import tempfile
import time

_ROOT = tempfile.mkdtemp(prefix="at-soak-")
os.environ.update({
    "AT_TEST_ISOLATED": "1",
    "XDG_STATE_HOME": os.path.join(_ROOT, "state"),
    "XDG_CONFIG_HOME": os.path.join(_ROOT, "config"),
    "XDG_RUNTIME_DIR": os.path.join(_ROOT, "run"),
    "CREWHALL_TMUX_SOCKET": f"at_soak_{os.getpid()}",
    "AGENT_TERMINAL_TMUX_SOCKET": f"at_soak_{os.getpid()}",
    "CREWHALL_HOOKS": "0",
})
os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crewhall import Controller, health, soak, watch  # noqa: E402

BACKEND = "tmux" if shutil.which("tmux") else "pty"


def _metric(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _limits(controller: Controller) -> dict:
    import resource

    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    threads = 0
    with contextlib.suppress(OSError, ValueError, IndexError), \
            open("/proc/self/status", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("Threads:"):
                threads = int(line.split()[1])
    return {
        "memory_kb": rss_kb, "memory_max_kb": 2 * 1024 * 1024,
        "threads": threads, "threads_max": 300,
        "log_bytes": _metric(os.path.join(os.environ["XDG_RUNTIME_DIR"], "crewhall", "daemon.log")),
        "log_max_bytes": 256 * 1024 * 1024,
    }


def run(messages: int, *, restarts: int, kill_at: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    cwd = tempfile.mkdtemp(prefix="at-soak-work-")
    controller = Controller(adopt=False, persist=True)
    args = ["--model", os.environ["SMOKE_MODEL"]] if os.environ.get("SMOKE_MODEL") else None
    agents = controller.create_agent(
        "opencode", name="soak-a", backend=BACKEND, cwd=cwd, args=args,
        wait_ready=True, timeout=120)
    controller.create_agent(
        "opencode", name="soak-b", backend=BACKEND, cwd=cwd, args=args,
        wait_ready=True, timeout=120)
    controller.create_team("soak", ["soak-a", "soak-b"])

    sent: list[dict] = []
    delivered: list[dict] = []
    drafts: list[dict] = []
    restart_points = {int(messages * (i + 1) / (restarts + 1)) for i in range(restarts)}
    try:
        for index in range(messages):
            recipient = controller.get_agent("soak-b")
            if not recipient.session.status.alive:
                controller.restart_agent(recipient.agent_id)
                recipient = controller.get_agent("soak-b")
            draft = None
            if rng.random() < 0.2:
                draft = f"human draft {index}"
                recipient.session.write(draft)
                time.sleep(0.2)
            body = f"soak-{seed}-{index}"
            token = controller._agent_token(agents.agent_id)
            delivery = controller.send_message_as("soak-a", token, "soak-b", body)
            sent.append({"recipient": "soak-b", "body": body, "seq": index})
            if delivery.delivered:
                delivered.append({"recipient": "soak-b", "body": body})
            if draft is not None:
                # The draft must survive the (queued) message injection untouched.
                actual = draft if draft in recipient.session.capture() else "(lost)"
                drafts.append({"agent": "soak-b", "expected": draft, "actual": actual})
                for key in recipient.clear_input_keys:
                    with contextlib.suppress(Exception):
                        recipient.session.send_key(key)
            if index == kill_at:
                with contextlib.suppress(Exception):
                    recipient.session.kill()
            if index in restart_points:
                controller.shutdown(kill_agents=False)
                controller = Controller(adopt=True, persist=True)
                controller.restore()

        # Let the deferred queue drain, then read the final state.
        time.sleep(5)
        queue = []
        with contextlib.suppress(Exception):
            holders = getattr(controller.messaging, "_queues", {})
            for items in holders.values():
                for item in items:
                    queue.append({"recipient": item.recipient, "body": item.body,
                                  "enqueued_at": getattr(item, "created", time.time())})
        pending = getattr(controller.messaging, "_history", None)
        if pending is not None and hasattr(pending, "load"):
            with contextlib.suppress(Exception):
                for record in pending.load():
                    body = record.get("body")
                    if record.get("delivered") and body and not any(
                            d["body"] == body for d in delivered):
                        delivered.append({"recipient": record.get("recipient"), "body": body})
        return soak.check_invariants(sent=sent, delivered=delivered, queue=queue,
                                     drafts=drafts, limits=_limits(controller))
    finally:
        with contextlib.suppress(Exception):
            controller.shutdown(kill_agents=True)
        shutil.rmtree(cwd, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="soak")
    parser.add_argument("--messages", type=int, default=500)
    parser.add_argument("--restarts", type=int, default=3)
    parser.add_argument("--kill-at", type=int, default=250, dest="kill_at")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--quick", action="store_true", help="small sanity run")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--result", default=None, metavar="PATH")
    args = parser.parse_args(argv)

    if shutil.which("opencode") is None:
        print("SKIP: opencode CLI not found")
        return 0

    messages = 20 if args.quick else args.messages
    started = time.monotonic()
    violations = run(messages, restarts=args.restarts, kill_at=args.kill_at, seed=args.seed)
    elapsed = time.monotonic() - started

    result = {
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "messages": messages, "elapsed_s": round(elapsed, 1),
        "ok": not violations, "violations": violations,
    }
    path = args.result or health.smoke_path().replace("smoke", "soak")
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            import json

            os.fchmod(fh.fileno(), 0o600)
            json.dump(result, fh, indent=2, sort_keys=True)

    if violations:
        print(f"SOAK FAILED after {elapsed:.0f}s:")
        for violation in violations:
            print(f"  - {violation}")
        if not args.no_notify:
            watch.notify("crewhall soak failed", "; ".join(violations[:3]))
        return 1
    print(f"SOAK OK ({messages} messages in {elapsed:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
