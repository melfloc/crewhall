#!/usr/bin/env python3
"""Run the test suite in parallel shards (standard library only).

Whole ``TestCase`` classes are distributed across worker processes, so each
class keeps its ``setUpClass``/ordering. Every worker is a fresh process and
therefore gets its own test isolation (its own tmux socket and XDG dirs); the
workers skip the global process/temp sweeps and the parent runner does a single
cleanup at the end.

Usage:
    scripts/test_parallel.py [--workers N] [--sequential]

``AT_TEST_WORKERS=1`` (or ``--sequential``) falls back to plain ``unittest``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Rough cost multipliers so heavy modules do not all land in the same worker.
HEAVY = {
    "tests.test_web_ui_browser": 3.0,   # each test launches Chromium
    "tests.test_terminal_security": 2.0,  # includes a 30 s idle-timeout test
    "tests.test_terminals_real": 2.0,
    "tests.test_ssh_tmux_real": 2.0,
    "tests.test_collaboration_real": 2.0,
    "tests.test_messaging_real": 2.0,
    "tests.test_teams_real": 2.0,
    "tests.test_ui_real": 2.0,
    "tests.test_remote_team": 2.0,
}


def discover_classes() -> dict[str, int]:
    os.chdir(REPO)
    suite = unittest.defaultTestLoader.discover(os.path.join(REPO, "tests"), top_level_dir=REPO)
    counts: dict[str, int] = {}

    def walk(node) -> None:
        for item in node:
            if isinstance(item, unittest.TestSuite):
                walk(item)
            else:
                cls = type(item)
                name = f"{cls.__module__}.{cls.__qualname__}"
                counts[name] = counts.get(name, 0) + 1

    walk(suite)
    return counts


def weight(name: str, count: int) -> float:
    for prefix, mult in HEAVY.items():
        if name.startswith(prefix):
            return count * mult
    return float(count)


def partition(classes: dict[str, int], workers: int) -> list[list[str]]:
    buckets: list[list[str]] = [[] for _ in range(workers)]
    load = [0.0] * workers
    for name, count in sorted(classes.items(), key=lambda kv: -weight(kv[0], kv[1])):
        i = load.index(min(load))
        buckets[i].append(name)
        load[i] += weight(name, count)
    return [b for b in buckets if b]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=int(os.environ.get("AT_TEST_WORKERS") or 0))
    parser.add_argument("--sequential", action="store_true")
    args = parser.parse_args(argv)
    os.chdir(REPO)

    workers = args.workers or min(os.cpu_count() or 1, 6)
    if args.sequential or workers <= 1:
        return subprocess.call(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=REPO)

    classes = discover_classes()
    total = sum(classes.values())
    buckets = partition(classes, workers)
    print(f"parallel test run: {total} tests across {len(buckets)} workers", flush=True)

    env = dict(os.environ)
    for key in ("AT_TEST_ISOLATED", "CREWHALL_TMUX_SOCKET", "AGENT_TERMINAL_TMUX_SOCKET",
                "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
        env.pop(key, None)
    env["AT_TEST_PARALLEL"] = "1"  # workers skip the global sweeps

    procs = []
    for index, bucket in enumerate(buckets):
        log_path = os.path.join("/tmp", f"at-parallel-{os.getpid()}-{index}.log")
        log = open(log_path, "wb")
        proc = subprocess.Popen(
            [sys.executable, "-m", "unittest", *bucket],  # no -P: `tests` must be importable
            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        procs.append((proc, log, log_path))

    started = time.monotonic()
    failed = 0
    for index, (proc, log, log_path) in enumerate(procs):
        rc = proc.wait()
        log.close()
        if rc != 0:
            failed += 1
            print(f"--- worker {index} failed (rc={rc}) ---", flush=True)
            with open(log_path, "rb") as fh:
                sys.stdout.buffer.write(fh.read())
                sys.stdout.buffer.flush()
    elapsed = time.monotonic() - started

    print(f"Ran {total} tests in {elapsed:.1f}s (parallel, {len(buckets)} workers)")
    if failed:
        print(f"FAILED (workers={failed})")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
