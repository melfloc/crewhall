"""Per-agent git worktrees (optional isolation).

``workspace_mode: shared | worktree`` (default shared). In worktree mode the
agent works on its own branch ``at/<team>/<agent>`` in a worktree under
``state/worktrees/<team>/<agent>``. Everything is argv-based (never a shell),
names are sanitized, and a non-git workspace falls back to shared with a
warning — never silently.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from . import paths

BRANCH_PREFIX = "at"
MAX_NAME = 40
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class WorktreeError(RuntimeError):
    pass


def sanitize(value: str | None) -> str:
    text = _SAFE.sub("-", (value or "").strip()).strip("-.")
    return (text or "agent")[:MAX_NAME]


def root() -> str:
    return os.path.join(paths.state_dir(), "worktrees")


def worktree_path(team: str | None, agent: str) -> str:
    return os.path.join(root(), sanitize(team), sanitize(agent))


def branch_name(team: str | None, agent: str) -> str:
    return f"{BRANCH_PREFIX}/{sanitize(team)}/{sanitize(agent)}"


def _git(repo: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          timeout=timeout)


def is_git_repo(repo: str | None) -> bool:
    if not repo or not os.path.isdir(repo):
        return False
    try:
        out = _git(repo, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def _within_state(path: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root())
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def create(repo: str, team: str | None, agent: str, *, base: str = "HEAD") -> str:
    """Create the worktree; returns its path. Raises WorktreeError."""
    if not is_git_repo(repo):
        raise WorktreeError(f"{repo!r} is not a git repository")
    path = worktree_path(team, agent)
    if not _within_state(path):
        raise WorktreeError("worktree path escapes the state directory")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    branch = branch_name(team, agent)
    if os.path.exists(path):
        raise WorktreeError(f"worktree already exists at {path}")
    proc = _git(repo, "worktree", "add", "-b", branch, path, base)
    if proc.returncode != 0:
        proc = _git(repo, "worktree", "add", path, branch)  # branch already exists
    if proc.returncode != 0:
        raise WorktreeError((proc.stderr or proc.stdout).strip() or "git worktree add failed")
    return path


def status(path: str | None, *, repo: str | None = None, base: str | None = None) -> dict[str, Any]:
    """``{exists, dirty, unmerged, branch}``; conservative when unknown."""
    if not path or not os.path.isdir(path):
        return {"exists": False, "dirty": False, "unmerged": False, "branch": None}
    info: dict[str, Any] = {"exists": True, "dirty": False, "unmerged": False, "branch": None}
    try:
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        info["branch"] = branch.stdout.strip() or None
        porcelain = _git(path, "status", "--porcelain")
        info["dirty"] = bool(porcelain.stdout.strip())
        if repo and base:
            unmerged = _git(repo, "log", "--oneline", f"{base}..{info['branch']}")
            info["unmerged"] = bool(unmerged.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        info["dirty"] = True  # unknown: never claim it is safe to delete
        info["unmerged"] = True
    return info


def can_remove(path: str, *, repo: str | None = None, base: str = "HEAD") -> tuple[bool, str]:
    state = status(path, repo=repo, base=base)
    if not state["exists"]:
        return True, "missing"
    if state["dirty"]:
        return False, "worktree has uncommitted changes"
    if state["unmerged"]:
        return False, "worktree has commits not merged into the base branch"
    return True, "clean"


def remove(path: str, *, force: bool = False) -> None:
    if not _within_state(path):
        raise WorktreeError("refusing to remove a worktree outside the state directory")
    if not os.path.isdir(path):
        return
    args = ["worktree", "remove", *(["--force"] if force else []), path]
    proc = _git(path, *args)
    if proc.returncode != 0:
        # Fall back to the main repository, resolved from the worktree's git dir.
        try:
            common = _git(path, "rev-parse", "--git-common-dir")
            main = os.path.dirname(os.path.realpath(os.path.join(path, common.stdout.strip())))
            proc = _git(main, *args)
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.returncode != 0:
        raise WorktreeError((proc.stderr or proc.stdout).strip() or "git worktree remove failed")


def list_worktrees(repo: str) -> list[dict[str, Any]]:
    if not is_git_repo(repo):
        return []
    try:
        proc = _git(repo, "worktree", "list", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return []
    out: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                out.append(current)
            current = {"path": line[len("worktree "):]}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].rsplit("/", 1)[-1]
    if current:
        out.append(current)
    return out

