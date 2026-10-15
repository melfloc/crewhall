"""Controller services: agent status (mixins of the Controller facade).

``agent_status`` answers the question an agent asks before re-sending: *why*
would a message to a teammate be queued right now, and what is that teammate
doing? It only ever reports observable facts and never leaks the human draft's
text, tokens, environment variables or process output (the last only for leads,
with an explicit flag).
"""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import time
from typing import Any

from ..harness import Harness

# A shell whose task output changed more recently than this counts as still
# producing output.
OUTPUT_RECENT = 10.0


class ServicesStatus:

    def agent_status(self, target: str, include_output: bool = False) -> dict[str, Any]:
        harness = self.agents.resolve(target)
        return self._agent_status(harness, include_output=include_output)

    def _agent_status(self, harness: Harness, *, include_output: bool = False) -> dict[str, Any]:
        summary = self.agent_summary(harness)
        state = summary["state"]
        info = harness.info()
        activity = summary.get("activity") or {}
        interactions = summary.get("interactions") or []
        host = self._agent_hosts.get(harness.agent_id)
        draft = self._human_draft(harness)
        version_blocked = bool(getattr(harness, "version_blocked", False))
        blocked = self._blocked_by(state, interactions, draft, version_blocked)
        since = self._state_since(harness, state)
        out: dict[str, Any] = {
            "agent_id": harness.agent_id,
            "name": harness.name or harness.agent_id,
            "kind": harness.kind,
            "state": state,
            "state_since": since,
            "state_age": round(max(0.0, time.time() - since), 1) if since else None,
            "alive": bool(info.session_id and harness.session.status.alive),
            "backend": info.backend,
            "host": host,
            "activity": {
                "kind": activity.get("kind"),
                "label": activity.get("label"),
                "detail": activity.get("detail"),
                "model": activity.get("model"),
            },
            "interactions": [
                {"kind": i.get("kind"), "title": i.get("title") or i.get("kind"),
                 "id": i.get("id")}
                for i in interactions
            ],
            "human_draft": draft,  # boolean only: never the draft's text
            "version_blocked": version_blocked,
            "cli": summary.get("cli"),
            "session_config": summary.get("session_config"),
            "processes": self._status_processes(harness, host, include_output),
            "subagents": sorted(self._opencode_children.get(harness.agent_id, set())),
            "queue": self.messaging.queue_info(harness.agent_id),
            "deliverable_now": not blocked,
            "blocked_by": blocked,
        }
        return out

    def _human_draft(self, harness: Harness) -> bool:
        try:
            return not harness.input_is_clean()
        except Exception:  # noqa: BLE001 - an unreadable input is not a claim
            return False

    @staticmethod
    def _blocked_by(state: str, interactions: list[dict[str, Any]], draft: bool,
                    version_blocked: bool = False) -> list[str]:
        reasons: list[str] = []
        if version_blocked:
            # Hard block: the CLI is outside the range its adapter was verified
            # against, so crewhall refuses to type into it (killing/restarting it
            # is still allowed). The version and the reason ride along in "cli".
            reasons.append("unsupported_cli_version")
        if state == "starting":
            reasons.append("starting")
        elif state in ("exited", "error"):
            reasons.append("exited" if state == "exited" else "error")
        elif state == "working":
            reasons.append("working")
        if any(i.get("kind") in ("permission", "question") for i in interactions):
            reasons.append("permission_pending")
        if draft:
            reasons.append("human_draft")
        return reasons

    def _state_since(self, harness: Harness, state: str) -> float | None:
        last = self._hook_events.get(harness.agent_id)
        if last and last.get("at"):
            event = last.get("event")
            if (state == "working" and event == "prompt_submit") or (
                state in ("ready", "waiting_input") and event == "stop"
            ):
                return float(last["at"])
        try:
            return float(harness.session.created_at)
        except Exception:  # noqa: BLE001
            return None

    def _status_processes(
        self, harness: Harness, host: str | None, include_output: bool,
    ) -> dict[str, Any]:
        if host:
            return {"available": False, "reason": "unavailable on remote host",
                    "shells": [], "others": []}
        try:
            listing = self.list_processes(harness.agent_id)
        except Exception as exc:  # noqa: BLE001
            reason = "the agent is not running" if not harness.session.status.alive else str(exc)
            return {"available": False, "reason": reason, "shells": [], "others": []}
        shells = []
        for entry in listing.get("shells", []):
            age = entry.get("last_output_ago")
            shells.append({
                "pid": entry.get("pid"),
                "command": str(entry.get("command") or "")[:200],
                "age": entry.get("elapsed"),
                "producing_output": bool(age is not None and age < OUTPUT_RECENT),
                "last_output_ago": age,
            })
        others = [
            {"pid": e.get("pid"), "command": str(e.get("command") or "")[:200],
             "age": e.get("elapsed")}
            for e in listing.get("others", [])
        ]
        out = {"available": True, "reason": None, "shells": shells, "others": others}
        if include_output:
            out["output"] = self._process_outputs(harness, listing)
        return out

    def _process_outputs(self, harness: Harness, listing: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for shell in listing.get("shells", []):
            output = shell.get("output") or {}
            try:
                if output.get("kind") == "file":
                    data = self.process_output(harness.agent_id, task=output.get("task"))
                elif output.get("kind") == "opencode":
                    data = self.process_output(harness.agent_id, call=output.get("call"))
                else:
                    continue
            except Exception:  # noqa: BLE001 - output is best effort
                continue
            out.append({"pid": shell.get("pid"), "task": output.get("task"),
                        "call": output.get("call"), **data})
        return out
