"""The shells an agent runs (its tools' commands), read from ``/proc``.

An agent often looks stuck because a command it started is still running (a
server, a watcher, a prompt waiting on input it never gets). Its TUI lists
them, but only inside the terminal; this module makes them visible and
stoppable from crewhall's front-ends.

* Claude Code runs every Bash command as ``bash -c '… eval '<command>' …'`` with
  stdout/stderr going to ``<tmp>/claude-<uid>/<project>/<session>/tasks/<id>.output``,
  so the live output is that file.
* OpenCode runs ``bash -c <command>`` with stdout on a socket; the live output
  is in its server's running tool part (``metadata.output``).

Both use ``/dev/null`` as stdin: the commands cannot be typed into, only
watched and interrupted/terminated/killed (with everything they started).
"""
from __future__ import annotations

import glob
import os
import re
import signal as _signal
import time
from typing import Any

class ProcessError(ValueError):
    pass


SIGNALS = {"INT": _signal.SIGINT, "TERM": _signal.SIGTERM, "KILL": _signal.SIGKILL}
MAX_OUTPUT = 64 * 1024
_CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
# Claude's wrapper: ``… && eval '<command>' < /dev/null && pwd -P >| …``
_CLAUDE_EVAL = re.compile(r"eval '((?:[^']|'\"'\"')*)'")
_TASK_FILE = re.compile(r"/tasks/([A-Za-z0-9_-]+)\.output$")


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return None


def _link(pid: int, fd: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/fd/{fd}")
    except OSError:
        return None


def _uptime() -> float:
    text = _read("/proc/uptime") or "0"
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return 0.0


def _stat(pid: int) -> dict[str, Any] | None:
    raw = _read(f"/proc/{pid}/stat")
    if not raw or ")" not in raw:
        return None
    rest = raw.rsplit(")", 1)[1].split()
    try:
        return {
            "state": rest[0], "ppid": int(rest[1]), "pgid": int(rest[2]),
            "cpu": (int(rest[11]) + int(rest[12])) / _CLK,
            "start": int(rest[19]) / _CLK,
        }
    except (IndexError, ValueError):
        return None


def children_map() -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        st = _stat(int(name))
        if st:
            out.setdefault(st["ppid"], []).append(int(name))
    return out


def descendants(root: int, kids: dict[int, list[int]] | None = None) -> list[int]:
    kids = children_map() if kids is None else kids
    out, stack = [], list(kids.get(root, []))
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(kids.get(pid, []))
    return out


def claude_command(argv: list[str]) -> str:
    """The command Claude was asked to run, out of its bash wrapper."""
    script = argv[-1] if argv else ""
    m = _CLAUDE_EVAL.search(script)
    return m.group(1).replace("'\"'\"'", "'") if m else script


def describe(pid: int, now: float | None = None) -> dict[str, Any] | None:
    st = _stat(pid)
    raw = _read(f"/proc/{pid}/cmdline")
    if st is None or raw is None:
        return None
    argv = [a for a in raw.split("\0") if a]
    now = _uptime() if now is None else now
    out = _link(pid, 1)
    return {
        "pid": pid, "ppid": st["ppid"], "pgid": st["pgid"], "state": st["state"],
        "cpu": round(st["cpu"], 2), "elapsed": max(0.0, round(now - st["start"], 1)),
        "argv": argv[:64], "stdout": out, "stdin": _link(pid, 0),
    }


def _shell_command(info: dict[str, Any]) -> str:
    argv = info["argv"]
    if _is_shell(info):
        return claude_command(argv) if "eval '" in argv[-1] else argv[-1]
    return " ".join(argv)


def _is_shell(info: dict[str, Any]) -> bool:
    argv = info["argv"]
    return len(argv) >= 3 and os.path.basename(argv[0]) in ("bash", "sh", "zsh") and argv[1] == "-c"


def _match_tools(entries: list[dict[str, Any]], tools: dict[str, dict[str, Any]]) -> None:
    """Pair OpenCode's running bash tool calls with ``bash -c`` processes.

    The process can differ from the tool's text (``bash -c 'x'`` asked for is
    exec'ed as ``bash -c x``): exact match first, then containment, then a
    single leftover on each side.
    """
    free = dict(tools)
    pending = [e for e in entries if _is_shell(e)]
    for rule in (lambda c, t: c == t, lambda c, t: c in t or t in c):
        for entry in list(pending):
            hit = next((t for t in free if rule(entry["command"].strip(), t.strip())), None)
            if hit is not None:
                entry["output"] = {"kind": "opencode", "call": free.pop(hit)["call"]}
                pending.remove(entry)
    if len(pending) == 1 and len(free) == 1:
        pending[0]["output"] = {"kind": "opencode", "call": next(iter(free.values()))["call"]}


def agent_processes(root: int, *, running_tools: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Shells under the agent process ``root`` and the other processes it runs.

    ``running_tools`` (OpenCode): ``{command: {"call": id, …}}`` of tool parts
    still running, matched to the ``bash -c <command>`` processes.
    """
    kids = children_map()
    now, wall = _uptime(), time.time()
    entries = []
    for pid in kids.get(root, []):
        info = describe(pid, now)
        if info is None:
            continue
        sub = [d for d in (describe(p, now) for p in descendants(pid, kids)) if d]
        entry = {**info, "command": _shell_command(info)[:4000],
                 "children": [{"pid": d["pid"], "state": d["state"], "elapsed": d["elapsed"],
                               "command": " ".join(d["argv"])[:300]} for d in sub[:50]]}
        task = _TASK_FILE.search(info["stdout"] or "")
        if task:
            entry["output"] = {"kind": "file", "task": task.group(1)}
            try:
                entry["last_output_ago"] = round(wall - os.stat(info["stdout"]).st_mtime, 1)
            except OSError:
                pass
        entries.append(entry)
    if running_tools is not None:
        _match_tools([e for e in entries if "output" not in e], running_tools)
    # OpenCode runs every tool command as ``bash -c``; Claude's carry a task file.
    tool_shells = running_tools is not None
    shells = [e for e in entries if "output" in e or (tool_shells and _is_shell(e))]
    others = [e for e in entries if e not in shells]
    shells.sort(key=lambda e: -e["elapsed"])
    return {"shells": shells, "others": others}


def tail(path: str, max_bytes: int = MAX_OUTPUT) -> dict[str, Any]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            data = fh.read(max_bytes)
        mtime = os.path.getmtime(path)
    except OSError:
        return {"available": False, "output": "", "size": 0}
    return {"available": True, "output": data.decode("utf-8", "replace"), "size": size,
            "truncated": size > max_bytes, "last_output_ago": round(time.time() - mtime, 1)}


def claude_task_files(conversation_id: str | None, roots: list[str]) -> list[str]:
    """``tasks`` directories of a Claude conversation (finished commands included)."""
    if not conversation_id or not re.match(r"^[0-9a-f-]{36}$", conversation_id):
        return []
    found: list[str] = []
    for root in dict.fromkeys(r for r in roots if r):
        found += glob.glob(os.path.join(root, f"claude-{os.getuid()}", "*", conversation_id, "tasks"))
    return found


def signal_tree(root: int, pid: int, name: str) -> list[int]:
    """Signal ``pid`` and everything it started; it must run under ``root``."""
    sig = SIGNALS.get(name)
    if sig is None:
        raise ProcessError(f"signal must be one of {', '.join(SIGNALS)}")
    kids = children_map()
    if pid == root or pid not in descendants(root, kids):
        raise ProcessError(f"process {pid} is not run by this agent")
    targets = [pid] + descendants(pid, kids)
    root_st, st = _stat(root), _stat(pid)
    sent: list[int] = []
    if st and root_st and st["pgid"] == pid and st["pgid"] != root_st["pgid"]:
        try:  # its own process group: reaches whatever it backgrounded too
            os.killpg(pid, sig)
            return targets
        except OSError:
            pass
    for target in reversed(targets):  # children first, so none is re-parented and missed
        try:
            os.kill(target, sig)
            sent.append(target)
        except OSError:
            pass
    return sent
