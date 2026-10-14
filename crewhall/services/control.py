"""Controller services: agent containment and forced restart (leads only).

These are the authority-guarded ops an agent may run on a *teammate* it leads:
interrupt its current turn, signal its background processes, or restart it with
the same identity. The human control plane uses the same code without the lead
check. Authorization itself lives in ``_authorize_lead``; this layer only acts.
"""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import collections
import contextlib
import os
import signal
import threading
import time
from typing import Any

from .. import processes as procs, settings
from ..harness import Harness, get_harness
from ..messaging import MessagingError
from ..processes import ProcessError
from ..session import InteractiveSession
from ..types import SessionSpec
from ..controller import log

# Grace between TERM and KILL when a forced restart tears down a process tree.
RESTART_GRACE = 5.0
# Kinds whose conversation can be resumed on a forced restart (same identity).
RESUMABLE = ("claude", "opencode", "codex")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class ServicesControl:

    def interrupt_agent(self, target: str, by: str = "user") -> dict[str, Any]:
        harness = self.agents.resolve(target)
        self._assert_operable(harness)
        ok = harness.interrupt()
        self._log_action(harness, "interrupt", by)
        if by != "user":
            self._notify_action("interrupted", harness, by)
        return {"agent_id": harness.agent_id, "name": harness.name or harness.agent_id,
                "interrupted": bool(ok)}

    def signal_agent(
        self, target: str, signal_name: str = "TERM", *,
        pid: int | None = None, all_shells: bool = False, by: str = "user",
    ) -> dict[str, Any]:
        if pid is None and not all_shells:
            raise ProcessError("a --pid or --all-shells is required")
        harness = self.agents.resolve(target)
        # Containment is never gated: killing a stuck process must work even on
        # an agent whose CLI version crewhall will not drive.
        results: list[dict[str, Any]] = []
        if all_shells:
            listing = self.list_processes(harness.agent_id)
            for shell in listing.get("shells", []):
                results.append(self.signal_process(
                    harness.agent_id, int(shell["pid"]), signal_name, by=by))
        elif pid is not None:
            results.append(self.signal_process(
                harness.agent_id, int(pid), signal_name, by=by))
        if by != "user":
            self._notify_action(
                f"signalled ({signal_name.upper()})", harness, by,
                extra=(f"{len(results)} shell(s)" if all_shells else f"pid {pid}"))
        return {"agent_id": harness.agent_id, "name": harness.name or harness.agent_id,
                "count": len(results), "all_shells": all_shells, "signalled": results}

    def force_restart_agent(
        self, target: str, *, fresh: bool = False, reason: str | None = None,
        by: str = "user", timeout: float = 30.0,
    ) -> dict[str, Any]:
        harness = self.agents.resolve(target)
        # Restart is never gated: it is exactly what relaunches the agent on the
        # compatible binary (the version report is recalculated afterwards).
        agent_id = harness.agent_id
        if by != "user" and by == agent_id:
            raise MessagingError("an agent cannot restart itself")
        if by != "user":
            # Anti-loop brake for leads; the human operator is never throttled.
            self._check_restart_brake(agent_id)
        started = time.time()
        with self._lock_for_agent(agent_id):
            tree = self._capture_tree(harness)
            termination = self._terminate_tree(harness, tree)
            kind = harness.kind
            spec = harness.session.spec
            backend_name = harness.session.backend.name
            host = self._agent_hosts.get(agent_id)
            conversation_id = harness.conversation_id
            teams = self._team_names_of(agent_id)
            args = self._agent_args.get(agent_id, [])
            base_env = dict(self._base_env.get(agent_id) or {})
            env = self._build_agent_env(agent_id, harness.name or agent_id, teams, base_env, host)
            link = None if host else self._new_opencode_link(kind, args)
            if link:
                env["OPENCODE_SERVER_PASSWORD"] = link.password
            can_resume = bool(conversation_id and kind in RESUMABLE and not host)
            resume_id = conversation_id if can_resume and not fresh else None
            new_conv = resume_id if resume_id else self._new_conversation_id(kind, args)
            new_spec = SessionSpec(
                command=self._launch_command(
                    kind, args, new_conv, link.port if link else None, host=host,
                    resume=resume_id, enforce=False,
                ),
                cwd=spec.cwd,
                env=env,
                cols=spec.cols,
                rows=spec.rows,
                name=harness.name,
                host=host,
            )
            session = InteractiveSession(
                self._backend_for(backend_name, host), new_spec, session_id=agent_id)
            session.start()
            self.registry.add(session)
            revived = get_harness(kind)(session, name=harness.name)
            revived.conversation_id = new_conv
            self._attach_link(revived, link)
            self._attach_version(revived, kind, host)
            self.agents.add(revived)
            self._persist()
            if not host:
                self._ensure_control_file(spec.cwd, kind)
        state = session.status.value
        try:
            state = revived.start(timeout=timeout).value
        except Exception:  # noqa: BLE001 - report the final state even on a timeout
            log.exception("forced restart: %s did not become ready", agent_id)
            state = revived.state().value
        note = f"restarted by {by}: {reason or 'no reason given'}"
        try:
            self.messaging.send_system(agent_id, f"[system] {note}", origin="control")
        except Exception:  # noqa: BLE001 - the notice is best effort
            log.debug("could not notify %s of its restart", agent_id, exc_info=True)
        self._log_action(revived, "restart", by, extra={"reason": reason, "fresh": fresh or not resume_id})
        if by != "user":
            self._notify_action("restarted", revived, by, extra=reason)
        return {
            "agent_id": agent_id,
            "name": harness.name or agent_id,
            "state": state,
            "resumed": bool(resume_id),
            "fresh": not bool(resume_id),
            "reason": reason,
            "terminated": termination,
            "duration": round(time.time() - started, 1),
        }

    # -- process tree -----------------------------------------------------

    def _capture_tree(self, harness: Harness) -> list[int]:
        """The agent's process tree *before* killing (root + every descendant)."""
        if self._agent_hosts.get(harness.agent_id):
            return []  # a remote tree is not on this machine
        try:
            root = int(harness.session.pid) if harness.session.pid else 0
        except (TypeError, ValueError):
            root = 0
        if not root:
            return []
        return [root, *procs.descendants(root)]

    def _terminate_tree(self, harness: Harness, tree: list[int]) -> dict[str, Any]:
        """TERM the tree, wait, KILL what is left, then close the backend.

        Returns what was signalled and what (if anything) survived, so the caller
        can prove no process of the captured tree is still alive.
        """
        signalled: list[int] = []
        for pid in reversed(tree):  # children first
            try:
                os.kill(pid, signal.SIGTERM)
                signalled.append(pid)
            except OSError:
                pass
        deadline = time.monotonic() + RESTART_GRACE
        while time.monotonic() < deadline and any(_alive(p) for p in tree):
            time.sleep(0.1)
        killed = [p for p in tree if _alive(p)]
        for pid in reversed(killed):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        for _ in range(20):
            if not any(_alive(p) for p in tree):
                break
            time.sleep(0.05)
        try:
            harness.session.backend.close()
        except Exception:  # noqa: BLE001 - the session is being rebuilt anyway
            log.debug("backend close failed for %s", harness.agent_id, exc_info=True)
        return {"signalled": signalled, "killed": killed,
                "remaining": [p for p in tree if _alive(p)]}

    # -- restart brake ----------------------------------------------------

    def _check_restart_brake(self, agent_id: str) -> None:
        limit = int(settings.get("agents.restart_max"))
        window = float(settings.get("agents.restart_window_minutes")) * 60.0
        with self._locks_guard:  # check and reserve atomically (concurrent leads)
            times = self._restart_times.setdefault(agent_id, collections.deque())
            now = time.time()
            while times and now - times[0] > window:
                times.popleft()
            if len(times) >= limit:
                raise MessagingError(
                    f"restart brake: {agent_id} was restarted {len(times)} times in the "
                    f"last {settings.get('agents.restart_window_minutes')} min; "
                    f"wait before trying again"
                )
            times.append(now)

    # -- shared notices ---------------------------------------------------

    def _log_action(self, harness: Harness, action: str, by: str,
                    extra: dict[str, Any] | None = None) -> None:
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger(action, {"by": by, **(extra or {})})

    def _notify_action(self, action: str, harness: Harness, by: str,
                       extra: str | None = None) -> None:
        """Tell the human that a lead acted on one of its agents (best effort)."""
        name = harness.name or harness.agent_id
        title = f"crewhall: {name} {action}"
        body = f"by {by}" + (f": {extra}" if extra else "")

        def send() -> None:
            try:
                from .. import watch

                watch.notify(title, body)
            except Exception:  # noqa: BLE001 - a notice must never break the action
                log.debug("could not send the desktop notice", exc_info=True)

        threading.Thread(target=send, name="control-notice", daemon=True).start()
