"""What exactly is running: release (built from a tagged commit) or a dev checkout.

``scripts/release.sh`` bakes ``_build_info.json`` (commit, date, channel) into the
wheel; a source checkout has no such file and is reported as ``dev``. The update
mechanism only ever installs ``release`` builds, so a machine's version string
always maps back to one commit of the main line.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from . import __version__

_FILE = os.path.join(os.path.dirname(__file__), "_build_info.json")


def _git(*args: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def build_info() -> dict[str, Any]:
    try:
        with open(_FILE, encoding="utf-8") as fh:
            info = json.load(fh)
        return {"kind": "release", "version": __version__, **info}
    except (OSError, ValueError):
        commit = _git("rev-parse", "--short", "HEAD")
        dirty = bool(_git("status", "--porcelain")) if commit else False
        return {"kind": "dev", "version": __version__, "commit": commit or None, "dirty": dirty}


def describe() -> str:
    info = build_info()
    commit = info.get("commit") or "?"
    if info["kind"] == "release":
        return f"{info['version']} (release {commit})"
    return f"{info['version']} (dev {commit}{'+dirty' if info.get('dirty') else ''})"
