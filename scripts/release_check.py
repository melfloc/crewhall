#!/usr/bin/env python3
"""Release gates (used by scripts/release.sh; importable for tests).

Each gate raises ``GateError`` with an actionable message. Nothing here mutates the repo.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib

MAIN_BRANCH = "main"


class GateError(Exception):
    pass


def _git(root: str, *args: str) -> str:
    out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise GateError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def parse(v: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", v.strip())
    if not m:
        raise GateError(f"not a MAJOR.MINOR.PATCH version: {v!r}")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def pyproject_version(root: str) -> str:
    with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def package_version(root: str) -> str:
    text = open(os.path.join(root, "crewhall", "__init__.py"), encoding="utf-8").read()
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise GateError("crewhall/__init__.py has no __version__")
    return m.group(1)


def changelog_top(root: str) -> str:
    text = open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8").read()
    m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)
    if not m:
        raise GateError("CHANGELOG.md has no '## [X.Y.Z]' section")
    return m.group(1)


def changelog_section(root: str, version: str) -> str:
    text = open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8").read()
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    if not m or not m.group(1).strip():
        raise GateError(f"CHANGELOG.md has no notes for {version}")
    return m.group(1).strip()


def gate_versions(root: str) -> str:
    versions = {"pyproject.toml": pyproject_version(root),
                "crewhall/__init__.py": package_version(root),
                "CHANGELOG.md": changelog_top(root)}
    if len(set(versions.values())) != 1:
        raise GateError("version mismatch: " + ", ".join(f"{k}={v}" for k, v in versions.items()))
    version = versions["pyproject.toml"]
    parse(version)
    changelog_section(root, version)
    return version


def gate_branch_and_clean(root: str, branch: str = MAIN_BRANCH) -> None:
    current = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if current != branch:
        raise GateError(f"releases come only from '{branch}' (you are on '{current}')")
    dirty = _git(root, "status", "--porcelain")
    if dirty:
        raise GateError("the working tree is not clean; commit or stash first:\n" + dirty)


def latest_tag_version(root: str) -> tuple[int, int, int] | None:
    tags = [t for t in _git(root, "tag", "--list", "v[0-9]*").splitlines() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return max((parse(t) for t in tags), default=None)


def gate_new_version(root: str, version: str) -> None:
    if _git(root, "tag", "--list", f"v{version}"):
        raise GateError(f"tag v{version} already exists: released versions are immutable; bump the version")
    latest = latest_tag_version(root)
    if latest and parse(version) <= latest:
        raise GateError(f"{version} is not newer than the latest release v{'.'.join(map(str, latest))}")


def run_gates(root: str, branch: str = MAIN_BRANCH) -> str:
    gate_branch_and_clean(root, branch)
    version = gate_versions(root)
    gate_new_version(root, version)
    return version


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["gates", "version", "notes"])
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--branch", default=MAIN_BRANCH)
    args = ap.parse_args(argv)
    try:
        if args.command == "gates":
            print(run_gates(args.root, args.branch))
        elif args.command == "version":
            print(gate_versions(args.root))
        else:
            print(changelog_section(args.root, gate_versions(args.root)))
    except GateError as exc:
        print(f"RELEASE GATE FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
