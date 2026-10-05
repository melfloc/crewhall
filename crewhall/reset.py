"""Emergency reset: get a wedged setup moving again without a terminal.

Three levels, each previewed first (``plan``) and only then applied:

* ``clean``     touches nothing alive: drops dead sessions, stale temp files,
                orphan tmux sessions nobody registered, in-memory caches.
* ``services``  ``clean`` + restarts the Web UI listeners and signs every
                browser out.
* ``full``      stops every agent, removes the crewhall tmux sessions and
                the dedicated temp directory, optionally forgets saved
                teams/agents and the settings, and restarts the daemon in place.
                A backup (config + state) is written first, always.

Nothing here deletes a conversation (Claude transcripts / OpenCode database
belong to the tools themselves) nor any file of your projects.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

from . import bundle, paths, settings

LEVELS = ("clean", "services", "full")
CONFIRM_WORD = "RESET"
MIN_AGE_CLEAN = 600  # seconds: a temp file younger than this may belong to a live agent


def _dir_bytes(path: str) -> tuple[int, int]:
    total = count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
                count += 1
            except OSError:
                pass
    return count, total


def _orphan_tmux(controller: Any) -> list[str]:
    from .backends import tmux

    known = {s.session_id for s in controller.registry.all()}
    try:
        return [s["session_id"] for s in tmux.existing_sessions() if s["session_id"] not in known]
    except Exception:  # noqa: BLE001
        return []


def plan(controller: Any, level: str) -> dict[str, Any]:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    live = [h for h in controller.agents.all() if h.session.status.alive]
    dead = [h for h in controller.agents.all() if not h.session.status.alive]
    tmp = paths.tmpdir_root()
    files, size = _dir_bytes(tmp) if os.path.isdir(tmp) else (0, 0)
    steps: list[dict[str, Any]] = [
        {"id": "dead", "text": f"Drop {len(dead)} finished/dead session(s) from memory", "count": len(dead)},
        {"id": "tmp", "text": f"Remove stale agent temp files ({files} file(s), {size // 1048576} MB in the temp directory)"
                               + ("" if level == "full" else f" older than {MIN_AGE_CLEAN // 60} min"), "count": files},
        {"id": "orphans", "text": "Close tmux sessions that no agent owns", "count": len(_orphan_tmux(controller))},
        {"id": "caches", "text": "Clear in-memory caches (conversations, activity)", "count": 1},
    ]
    if level in ("services", "full"):
        steps.append({"id": "frontends", "text": "Restart the Web UI listeners and sign every browser out", "count": 1})
    if level == "full":
        steps = [{"id": "backup", "text": "Back up configuration and state first (kept in the backups directory)", "count": 1},
                 {"id": "agents", "text": f"Stop {len(live)} running agent(s) — their unsaved TUI state is lost "
                                           "(conversations stay on disk and can be resumed)", "count": len(live)},
                 *steps,
                 {"id": "restart", "text": "Restart the daemon in place (same process id, clean memory)", "count": 1}]
    return {"level": level, "live_agents": [{"id": h.agent_id, "name": h.name, "kind": h.kind} for h in live],
            "steps": steps, "needs_confirm": level == "full", "confirm_word": CONFIRM_WORD,
            "options": {"forget_state": "Also forget saved teams and agent definitions",
                        "reset_settings": "Also restore Settings to defaults"} if level == "full" else {}}


def _backup() -> str | None:
    d = os.path.join(paths.state_dir(), "backups")
    os.makedirs(d, mode=0o700, exist_ok=True)
    dest = os.path.join(d, f"pre-reset-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    out = bundle.export_bundle(dest, with_state=True)
    keep = int(settings.get("maintenance.keep_backups"))
    for name in sorted(n for n in os.listdir(d) if n.startswith("pre-reset-"))[:-keep]:
        try:
            os.unlink(os.path.join(d, name))
        except OSError:
            pass
    return dest if out["files"] else None


def _clear_tmp(everything: bool) -> int:
    tmp = paths.tmpdir_root()
    if not os.path.isdir(tmp):
        return 0
    now, removed = time.time(), 0
    for name in os.listdir(tmp):
        full = os.path.join(tmp, name)
        try:
            if not everything and now - os.lstat(full).st_mtime < MIN_AGE_CLEAN:
                continue
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                os.unlink(full)
            removed += 1
        except OSError:
            pass
    return removed


def _kill_tmux_sessions(names: list[str]) -> int:
    from .backends import tmux

    done = 0
    for name in names:
        try:
            if subprocess.run(["tmux", "-L", tmux.socket_name(), "-f", "/dev/null", "kill-session", "-t", name],
                              capture_output=True, timeout=10).returncode == 0:
                done += 1
        except (OSError, subprocess.SubprocessError):
            pass
    return done


def _clear_caches() -> None:
    from . import transcripts

    transcripts._CACHE.clear()


def apply(controller: Any, frontends: Any, level: str, *, forget_state: bool = False,
          reset_settings: bool = False) -> dict[str, Any]:
    """Run the plan. Returns what was done; the caller restarts the daemon for ``full``."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    done: list[str] = []
    errors: list[str] = []

    def step(label: str, fn) -> Any:
        try:
            out = fn()
            done.append(label if out is None else f"{label}: {out}")
            return out
        except Exception as exc:  # noqa: BLE001 - one failing step must not stop the rest
            errors.append(f"{label}: {exc}")
            return None

    backup = None
    if level == "full":
        backup = step("backup", _backup)
        if backup is None and not errors:
            done.append("backup: nothing to back up")
        if errors:  # without a backup we do not destroy anything
            return {"level": level, "done": done, "errors": errors, "backup": None, "restart": False}
        for harness in list(controller.agents.all()):
            step(f"stop {harness.name or harness.agent_id}", lambda h=harness: h.stop(force=True))
    step("dead sessions", lambda: f"{controller.prune()} dropped")
    orphans = _orphan_tmux(controller) if level != "full" else None
    if level == "full":
        from .backends import tmux
        orphans = [s["session_id"] for s in tmux.existing_sessions()]
        for session in list(controller.registry.all()):
            step("close session", lambda s=session: s.close())
    step("tmux sessions", lambda: f"{_kill_tmux_sessions(orphans or [])} closed")
    step("temp files", lambda: f"{_clear_tmp(everything=level == 'full')} removed")
    step("caches", _clear_caches)
    if level == "full":
        if forget_state:
            def forget() -> str:
                for harness in list(controller.agents.all()):
                    controller.remove_agent(harness.agent_id, force=True)
                for team in list(controller.teams.list()):
                    controller.remove_team(team.team_id)
                return "teams and agents forgotten"
            step("forget state", forget)
        if reset_settings:
            step("settings", lambda: (settings.reset(), "defaults restored")[1])
    if level in ("services", "full"):
        def bounce() -> str:
            from .web import auth

            mode = frontends.mode()
            auth.revoke_sessions()
            frontends.set_mode("off", persist=False)
            if mode != "off":
                frontends.set_mode(mode, persist=False)
            return f"mode {mode}, sessions cleared"
        if level == "services":
            step("web ui", bounce)
        else:
            done.append("web ui: restarts with the daemon")
    return {"level": level, "done": done, "errors": errors, "backup": backup, "restart": level == "full"}
