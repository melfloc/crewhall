"""Per-agent git worktrees on a *remote* host (same contract as ``worktrees``).

Everything runs as fixed ``sh`` scripts over SSH with the variable parts passed
as positional arguments (never interpolated), names go through the same
sanitiser as local worktrees, and removal is confined to the remote managed
directory (``$XDG_STATE_HOME/crewhall/worktrees``).  An unreachable host is
reported conservatively: a worktree whose state is unknown is never declared
clean or safe to delete.
"""
from __future__ import annotations

import shlex
from typing import Any

from .backends.ssh_tmux import SshTmuxBackend
from .worktrees import BRANCH_PREFIX, WorktreeError, branch_name, sanitize

_ROOT = '${XDG_STATE_HOME:-$HOME/.local/state}/crewhall/worktrees'

# $1 repo  $2 team/agent (already sanitised)  $3 branch  $4 base
_CREATE = (
    f'root="{_ROOT}"; repo=$1; path="$root/$2"; '
    'git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 20; '
    '[ -e "$path" ] && exit 21; '
    'mkdir -p "$(dirname "$path")" || exit 23; '
    '{ git -C "$repo" worktree add -b "$3" "$path" "$4" >&2 '
    '|| git -C "$repo" worktree add "$path" "$3" >&2; } || exit 22; '
    'printf %s "$path"'
)

# $1 repo
_IS_REPO = 'git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1'

# $1 path  $2 repo (may be empty)  $3 base
_STATUS = (
    'p=$1; [ -d "$p" ] || { echo exists=0; exit 0; }; '
    'b=$(git -C "$p" rev-parse --abbrev-ref HEAD 2>/dev/null); '
    'd=0; [ -n "$(git -C "$p" status --porcelain 2>/dev/null)" ] && d=1; '
    'u=0; if [ -n "$2" ] && [ -n "$b" ]; then '
    '[ -n "$(git -C "$2" log --oneline "$3..$b" 2>/dev/null)" ] && u=1; fi; '
    'echo exists=1; echo branch=$b; echo dirty=$d; echo unmerged=$u'
)

# $1 path  $2 "force"|"" ; confined to the managed root, no ".." components.
_REMOVE = (
    f'root="{_ROOT}"; p=$1; '
    'case "$p" in *..*) exit 30;; esac; '
    'case "$p" in "$root"/*) ;; *) exit 30;; esac; '
    '[ -d "$p" ] || exit 0; '
    'if [ "$2" = force ]; then git -C "$p" worktree remove --force "$p"; '
    'else git -C "$p" worktree remove "$p"; fi'
)


def _run(host: dict[str, Any], script: str, *args: str, timeout: float = 60.0):
    cmd = "sh -c " + shlex.quote(script) + " sh " + " ".join(shlex.quote(a) for a in args)
    return SshTmuxBackend(host)._ssh_run(cmd, timeout=timeout)


def is_git_repo(host: dict[str, Any], repo: str | None) -> bool:
    if not repo:
        return False
    return _run(host, _IS_REPO, repo, timeout=20.0).returncode == 0


def create(host: dict[str, Any], repo: str, team: str | None, agent: str,
           *, base: str = "HEAD") -> str:
    rel = f"{sanitize(team)}/{sanitize(agent)}"
    proc = _run(host, _CREATE, repo, rel, branch_name(team, agent), base)
    if proc.returncode == 255:
        raise WorktreeError(f"host {host.get('name')} unreachable")
    if proc.returncode == 20:
        raise WorktreeError(f"{repo!r} is not a git repository")
    if proc.returncode == 21:
        raise WorktreeError("worktree already exists on the host")
    if proc.returncode != 0 or not proc.stdout.startswith("/"):
        raise WorktreeError((proc.stderr or "").strip() or "git worktree add failed")
    return proc.stdout.strip()


def status(host: dict[str, Any], path: str | None, *, repo: str | None = None,
           base: str | None = None) -> dict[str, Any]:
    """``{exists, dirty, unmerged, branch}``; unknown (host down) is conservative."""
    unknown = {"exists": True, "dirty": True, "unmerged": True, "branch": None,
               "unknown": True}
    if not path:
        return {"exists": False, "dirty": False, "unmerged": False, "branch": None}
    proc = _run(host, _STATUS, path, repo or "", base or "HEAD", timeout=30.0)
    if proc.returncode != 0:
        return unknown
    fields = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if fields.get("exists") != "1":
        return {"exists": False, "dirty": False, "unmerged": False, "branch": None}
    return {"exists": True, "dirty": fields.get("dirty") == "1",
            "unmerged": fields.get("unmerged") == "1", "branch": fields.get("branch") or None}


def can_remove(host: dict[str, Any], path: str, *, repo: str | None = None,
               base: str = "HEAD") -> tuple[bool, str]:
    state = status(host, path, repo=repo, base=base)
    if state.get("unknown"):
        return False, "host unreachable: worktree state unknown"
    if not state["exists"]:
        return True, "missing"
    if state["dirty"]:
        return False, "worktree has uncommitted changes"
    if state["unmerged"]:
        return False, "worktree has commits not merged into the base branch"
    return True, "clean"


def remove(host: dict[str, Any], path: str, *, force: bool = False) -> None:
    proc = _run(host, _REMOVE, path, "force" if force else "")
    if proc.returncode == 30:
        raise WorktreeError("refusing to remove a worktree outside the managed directory")
    if proc.returncode == 255:
        raise WorktreeError(f"host {host.get('name')} unreachable")
    if proc.returncode != 0:
        raise WorktreeError((proc.stderr or "").strip() or "git worktree remove failed")


__all__ = ["BRANCH_PREFIX", "WorktreeError", "is_git_repo", "create", "status",
           "can_remove", "remove"]
