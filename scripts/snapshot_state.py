#!/usr/bin/env python3
"""Snapshot the current state/queue/history into tests/fixtures (layer 6).

Every release must keep its own state format so the suite can prove that newer
versions still read the older ones (see RELEASING.md). Run this once per release
*before* the release commit, with a live daemon that has some state:

    python scripts/snapshot_state.py            # writes tests/fixtures/state-<v>.json, ...
    python scripts/snapshot_state.py --dry-run

It never mutates the live state: it only copies.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crewhall import __version__, paths  # noqa: E402

FIXTURES = os.path.join(paths.project_root(), "tests", "fixtures")
SOURCES = {
    "state": "state.json",
    "queue": "pending-messages.json",
    "history": "messages.jsonl",
}


def snapshot(dry_run: bool = False) -> list[tuple[str, str]]:
    results = []
    for kind, name in SOURCES.items():
        source = os.path.join(paths.state_dir(), name)
        if not os.path.exists(source):
            continue
        suffix = ".jsonl" if name.endswith(".jsonl") else ".json"
        target = os.path.join(FIXTURES, f"{kind}-{__version__}{suffix}")
        results.append((source, target))
        if not dry_run:
            os.makedirs(FIXTURES, exist_ok=True)
            shutil.copyfile(source, target)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="snapshot_state")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    results = snapshot(args.dry_run)
    if not results:
        print("no live state to snapshot (start a daemon and create some teams first)")
        return 0
    verb = "would write" if args.dry_run else "wrote"
    for source, target in results:
        print(f"{verb} {target}  <- {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
