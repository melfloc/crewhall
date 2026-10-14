"""Controller services: processes (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import glob
import os
import re
import threading
import time
from typing import Any
from .. import paths, processes as procs
from ..harness import Harness, HarnessError
from ..controller import OPENCODE_UNSTICK_GRACE


class ServicesProcesses:

    def list_processes(self, target: str) -> dict[str, Any]:
        harness = self.agents.resolve(target)
        root = self._agent_root(harness)
        running = self._opencode_running(harness) or None
        out = procs.agent_processes(root, running_tools=running)
        out["agent_pid"] = root
        if harness.supports_task_files:
            dirs = self._task_dirs(harness)
            for shell in out["shells"]:
                dirs.add(os.path.dirname(shell["stdout"]))
            # Finished commands of the current conversation only (/clear starts another).
            conv = harness.conversation_id
            dirs = {d for d in dirs if conv and os.path.basename(os.path.dirname(d)) == conv}
            live = {s["output"]["task"] for s in out["shells"] if s.get("output", {}).get("kind") == "file"}
            finished: list[dict[str, Any]] = []
            for directory in dirs:
                for path in glob.glob(os.path.join(directory, "*.output")):
                    task = os.path.basename(path)[: -len(".output")]
                    if task in live:
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    finished.append({"task": task, "size": st.st_size, "mtime": st.st_mtime})
            out["finished"] = sorted(finished, key=lambda t: -t["mtime"])[:30]
        return out

    def process_output(self, target: str, task: str | None = None, call: str | None = None) -> dict[str, Any]:
        """Live output of a shell: a Claude task file or an OpenCode running tool."""
        harness = self.agents.resolve(target)
        if task:
            if not re.match(r"^[A-Za-z0-9_-]{1,64}$", task):
                raise procs.ProcessError("invalid task id")
            for directory in self._task_dirs(harness):
                path = os.path.join(directory, f"{task}.output")
                if os.path.isfile(path):
                    return procs.tail(path)
            return {"available": False, "output": "", "size": 0}
        if call:
            for info in self._opencode_running(harness).values():
                if info.get("call") == call:
                    text = info.get("output") or ""
                    return {"available": True, "output": text[-procs.MAX_OUTPUT:],
                            "size": len(text), "truncated": len(text) > procs.MAX_OUTPUT}
            return {"available": False, "output": "", "size": 0, "finished": True}
        raise procs.ProcessError("task or call is required")

    def signal_process(self, target: str, pid: int, signal: str, by: str = "user") -> dict[str, Any]:
        harness = self.agents.resolve(target)
        root = self._agent_root(harness)
        call = None
        if harness.uses_local_server:
            listing = self.list_processes(harness.agent_id)
            shell = next((s for s in listing["shells"] if s["pid"] == int(pid)), None)
            call = ((shell or {}).get("output") or {}).get("call")
        sent = procs.signal_tree(root, int(pid), str(signal).upper())
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("process_signal", {"pid": int(pid), "signal": str(signal).upper(), "by": by})
        if call:
            threading.Thread(
                target=self._unstick_opencode_tool, args=(harness, int(pid), call),
                name=f"oc-unstick-{harness.agent_id}", daemon=True,
            ).start()
        return {"pid": int(pid), "signal": str(signal).upper(), "signalled": sent,
                "abort_if_stuck": bool(call)}

    def _unstick_opencode_tool(self, harness: Harness, pid: int, call: str,
                               grace: float = OPENCODE_UNSTICK_GRACE) -> None:
        """OpenCode does not notice a tool command killed from outside: its turn
        stays "running" forever. Once the process is gone and the tool still runs,
        stop the turn through its API (as ESC would), so the agent is usable again."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and os.path.exists(f"/proc/{pid}"):
            time.sleep(0.2)
        if os.path.exists(f"/proc/{pid}"):
            return  # still alive (e.g. it ignored Ctrl-C): nothing to unstick
        time.sleep(grace)
        running = self._opencode_running(harness)
        if not any(info.get("call") == call for info in running.values()):
            return  # OpenCode noticed it after all
        try:
            harness.link.abort(harness.conversation_id)
        except Exception:  # noqa: BLE001
            return
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("process_signal", {"aborted_turn": call})

    def _agent_root(self, harness: Harness) -> int:
        if self._agent_hosts.get(harness.agent_id):
            raise HarnessError(
                harness.agent_id, "process inspection is not available for remote agents (n/d)"
            )
        # OpenCode v2 runs the agent's tools inside its `serve` process (the TUI
        # is only a client), so the shells live under the server, not the TUI.
        server = self._opencode_servers.get(harness.agent_id)
        if server is not None and server.poll() is None:
            return int(server.pid)
        pid = harness.session.pid
        if not pid or not harness.session.status.alive:
            raise HarnessError(harness.agent_id, "the agent is not running")
        return int(pid)

    def _opencode_running(self, harness: Harness) -> dict[str, dict[str, Any]]:
        return harness.running_shells()

    def _task_dirs(self, harness: Harness) -> set[str]:
        dirs = self._agent_task_dirs.setdefault(harness.agent_id, set())
        roots = [r for r in (paths.usable_tmpdir(), os.environ.get("TMPDIR"), "/tmp") if r]
        dirs.update(procs.claude_task_files(harness.conversation_id, roots))
        return dirs
