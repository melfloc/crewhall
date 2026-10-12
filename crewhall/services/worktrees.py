"""Controller services: worktrees (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import os
from typing import Any
from .. import settings


class ServicesWorktrees:

    def _make_worktree(self, agent_id: str, agent_name: str, team: str | None,
                       cwd: str | None, host: str | None = None) -> str | None:
        from .. import remote_worktrees, worktrees

        hcfg = settings.host(host) if host else None
        wt = remote_worktrees if hcfg else worktrees
        extra = (hcfg,) if hcfg else ()
        if not cwd:
            self._worktree_warnings[agent_id] = "a worktree needs a repository path"
            return cwd
        if not wt.is_git_repo(*extra, cwd):
            self._worktree_warnings[agent_id] = (
                f"{cwd} is not a git repository; using the shared workspace"
            )
            return cwd
        try:
            path = wt.create(*extra, cwd, team, agent_name)
        except worktrees.WorktreeError as exc:
            self._worktree_warnings[agent_id] = f"worktree not created: {exc}"
            return cwd
        self._worktrees[agent_id] = {
            "path": path, "repo": cwd, "team": team,
            "branch": worktrees.branch_name(team, agent_name),
            "host": host,
        }
        return path

    @staticmethod
    def _wt_host(wt: dict[str, Any]) -> dict[str, Any] | None:
        """Host config of a remote worktree (None = local)."""
        if not wt.get("host"):
            return None
        return settings.host(wt["host"])

    def worktree_info(self, agent_id: str) -> dict[str, Any]:
        return dict(self._worktrees.get(agent_id, {}))

    def _cleanup_worktree(self, agent_id: str, *, force: bool = False) -> None:
        """Remove the worktree on delete only when it is safe; otherwise keep it."""
        from .. import remote_worktrees, worktrees

        wt = self._worktrees.get(agent_id)
        if not wt:
            return
        hcfg = self._wt_host(wt)
        mod = remote_worktrees if hcfg else worktrees
        extra = (hcfg,) if hcfg else ()
        if force:
            ok, reason = True, "forced"
        else:
            ok, reason = mod.can_remove(*extra, wt["path"], repo=wt["repo"])
        if ok:
            try:
                mod.remove(*extra, wt["path"], force=force)
                self._worktrees.pop(agent_id, None)
                return
            except worktrees.WorktreeError as exc:
                reason = str(exc)
        self._worktree_warnings[agent_id] = f"worktree kept at {wt['path']}: {reason}"

    def list_worktrees(self) -> list[dict[str, Any]]:
        from .. import remote_worktrees, worktrees

        out: list[dict[str, Any]] = []
        for agent_id, wt in self._worktrees.items():
            hcfg = self._wt_host(wt)
            st = (remote_worktrees.status(hcfg, wt["path"], repo=wt["repo"]) if hcfg
                  else worktrees.status(wt["path"], repo=wt["repo"]))
            out.append({"agent_id": agent_id, "path": wt["path"], "branch": wt.get("branch"),
                        "repo": wt["repo"], "dirty": st["dirty"], "unmerged": st["unmerged"],
                        "exists": st["exists"], "orphan": False, "host": wt.get("host"),
                        "unknown": bool(st.get("unknown"))})
        root = worktrees.root()
        known = {os.path.realpath(w["path"]) for w in self._worktrees.values()}
        if os.path.isdir(root):
            for dirpath, dirs, _files in os.walk(root):
                if os.path.isfile(os.path.join(dirpath, ".git")):
                    dirs[:] = []
                    if os.path.realpath(dirpath) not in known:
                        st = worktrees.status(dirpath)
                        out.append({"agent_id": None, "path": dirpath, "branch": st["branch"],
                                    "repo": None, "dirty": st["dirty"], "unmerged": st["unmerged"],
                                    "exists": st["exists"], "orphan": True})
        return out

    def discard_worktree(self, path: str, confirm: str | None) -> dict[str, Any]:
        from .. import remote_worktrees, worktrees

        if confirm != "DISCARD":
            raise ValueError("type DISCARD to discard a worktree with changes")
        for agent_id, wt in list(self._worktrees.items()):
            if wt.get("host") and wt["path"] == path:  # a managed remote worktree
                wt_host = self._wt_host(wt)
                if wt_host is not None:
                    remote_worktrees.remove(wt_host, path, force=True)
                self._worktrees.pop(agent_id, None)
                return {"discarded": path, "host": wt["host"]}
        real, base = os.path.realpath(path), os.path.realpath(worktrees.root())
        if not (real == base or real.startswith(base + os.sep)):
            raise ValueError("worktree is outside the managed directory")
        worktrees.remove(path, force=True)
        for agent_id, wt in list(self._worktrees.items()):
            if os.path.realpath(wt["path"]) == real:
                self._worktrees.pop(agent_id, None)
        return {"discarded": path}
