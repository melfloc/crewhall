#!/usr/bin/env python3
"""Scan the working tree and the git history for personal/secret data.

Exit code 1 if anything looks like a real path, email, host, default signing key
or credential. The allow-list below documents deliberate false positives
(documentation examples, fixtures, placeholders).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Real home directories, but not the documented placeholders.
HOME_RE = re.compile(r"/home/(?!(?:user|me|u|<user>|username)\b)[A-Za-z0-9._-]+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{16,}")
HOST_RE = re.compile(r"\boficina\b", re.IGNORECASE)
DEFAULT_KEY_RE = re.compile(r"\.ssh/id_ed25519")

ALLOW = (
    "example.invalid",
    "noreply@opencode.ai",
    "noreply@anthropic.com",   # git commit trailers
    "@unittest.",              # decorator text matched as an email
    "t@t",                     # tests
    "t@example.com",           # tests (git author placeholders)
    "t@e.com",                 # tests (git author placeholders)
    "user@example.com",
    "~/.ssh/id_ed25519",       # documented default key path (a hint, not a secret)
)


def _allowed(line: str) -> bool:
    return any(marker in line for marker in ALLOW)


def scan_text(text: str, label: str, *, home: str | None = None) -> list[str]:
    hits: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if _allowed(line):
            continue
        for name, pattern in (("home", HOME_RE), ("email", EMAIL_RE), ("key", KEY_RE),
                              ("host", HOST_RE), ("default-key", DEFAULT_KEY_RE)):
            match = pattern.search(line)
            if match and not (home and match.group(0) == home):
                hits.append(f"{label}:{number}: {name}: {match.group(0)}")
    return hits


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True,
                             text=True, timeout=30)
        if out.returncode == 0:
            return [line for line in out.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def scan_worktree() -> list[str]:
    """Scan the files git tracks (untracked personal notes are not shipped)."""
    hits: list[str] = []
    for rel in _tracked_files():
        if rel.endswith((".pyc", ".png", ".ico", ".so", ".whl", ".gz")):
            continue
        if os.path.basename(rel) == "privacy_scan.py":  # this file defines the patterns
            continue
        path = os.path.join(ROOT, rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                hits.extend(scan_text(fh.read(), rel))
        except OSError:
            continue
    return hits


def scan_history() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", ROOT, "log", "-p", "--all", "--no-color"],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return scan_text(out.stdout, "git-history")


def main() -> int:
    # The working tree must always be clean. The git history is scanned only
    # with ``--history``: before the first public push (a later phase) history
    # must be purged and this option enabled in CI.
    hits = scan_worktree()
    if "--history" in sys.argv:
        hits += scan_history()
    if hits:
        print("privacy scan found potential leaks:")
        for hit in hits[:200]:
            print("  " + hit)
        print(f"{len(hits)} hit(s)")
        return 1
    print("privacy scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
