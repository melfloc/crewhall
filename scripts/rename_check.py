#!/usr/bin/env python3
"""Report every place the old product name appears, for a rename.

Usage: ``scripts/rename_check.py NEW_NAME [--dry-run]``

Dry-run by default: it only lists occurrences and the plan. It never edits
anything (the actual rename is a deliberate release), and it works against a
fictitious name in the tests.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY = "agent-terminal"
LEGACY_PKG = "agent_terminal"
LEGACY_ENV = "AGENT_TERMINAL_"

SKIP_EXT = (".pyc", ".png", ".ico", ".so", ".whl", ".gz")


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True,
                             text=True, timeout=30)
        if out.returncode == 0:
            return [line for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def report(new_name: str) -> dict:
    name = new_name.strip()
    env = re.sub(r"[^A-Za-z0-9]+", "_", name).upper().strip("_")
    files = 0
    occurrences = 0
    env_occurrences = 0
    hits: list[str] = []
    for rel in _tracked():
        if rel.endswith(SKIP_EXT) or os.path.basename(rel) == "rename_check.py":
            continue
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        count = text.count(LEGACY) + text.count(LEGACY_PKG)
        env_count = text.count(LEGACY_ENV)
        if count or env_count:
            files += 1
            occurrences += count
            env_occurrences += env_count
            hits.append(f"{rel}: name={count} env={env_count}")
    return {
        "new_name": name, "new_env_prefix": env,
        "files": files, "occurrences": occurrences, "env_occurrences": env_occurrences,
        "hits": hits,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 2
    data = report(args[0])
    print(f"rename plan -> {data['new_name']} (env prefix {data['new_env_prefix']})")
    print(f"  files touched: {data['files']}")
    print(f"  name occurrences: {data['occurrences']}")
    print(f"  env-var occurrences: {data['env_occurrences']}")
    for line in data["hits"][:200]:
        print("  " + line)
    print("\n[dry-run] nothing was changed. The compatibility layer in "
          "agent_terminal/brand.py keeps old env vars, markers and the old "
          "command working during the migration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
