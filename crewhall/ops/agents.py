"""Daemon ops: agents (mixins composed by :class:`crewhall.daemon.Server`)."""
from __future__ import annotations

# File-wide F401: each mixin keeps the daemon's name surface so a moved
# handler needs no import surgery; ruff's unused-import check is not useful here.
# ruff: noqa: F401
import contextlib
import time
from typing import Any
from .. import settings
from ..harness import AgentState
from .common import OpsBase, PERMISSION_GRACE, PERMISSION_WAIT, PERMISSION_WAIT_MAX, VIEWER_FRESH, _dim, log


class OpsAgents(OpsBase):

    def _op_agent_create(self, request: dict[str, Any]) -> dict[str, Any]:
        team = request.get("team")
        if request.get("conversation") and not team:
            # A chat with a crew: its lead joins the Team (isolation of its cwd is
            # preserved by the controller). A remote team is not attached to a
            # chat: its agent must run locally to keep the conversation workspace.
            from .. import chat as conversations

            try:
                team = conversations.get(str(request["conversation"])).get("team")
                team_obj = self.controller._team_or_none(team)
                if team_obj is not None and team_obj.host:
                    team = None
            except Exception:  # noqa: BLE001 - unknown conversation/team: no crew
                team = None
        harness = self.controller.create_agent(
            request["kind"],
            name=request.get("name"),
            backend=request.get("backend"),
            cwd=request.get("cwd"),
            team=team,
            cols=_dim(request.get("cols", 120), "cols"),
            rows=_dim(request.get("rows", 40), "rows"),
            wait_ready=bool(request.get("wait_ready", False)),
            timeout=float(request.get("timeout", 30.0)),
            args=request.get("args"),
            workspace_mode=request.get("workspace_mode"),
            host=request.get("host"),
            conversation=request.get("conversation"),
        )
        if request.get("conversation"):
            from .. import chat as conversations

            with contextlib.suppress(conversations.ConversationError):
                conversations.set_agent(str(request["conversation"]), harness.agent_id)
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_list(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("viewer"):
            self._viewer_seen = time.monotonic()
        # Chat agents are isolated from Cowork: the default scope hides them, so
        # they never show up in the agents sidebar, teams or mission control.
        from .. import chat as conversations

        agents = self.controller.list_agents()
        scope = request.get("scope") or "cowork"
        if scope != "all":
            chat_ids = conversations.agent_ids()
            if scope == "chat":
                agents = [a for a in agents if a.get("agent_id") in chat_ids]
            else:
                agents = [a for a in agents if a.get("agent_id") not in chat_ids]
        return {"agents": agents}

    def _op_agent_info(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_send(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        harness.send(request["prompt"], timeout=float(request.get("timeout", 30.0)))
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_write(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        harness.write_raw(request["text"])
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_key(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        harness.send_key(request["key"])
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_resize(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        harness.resize(_dim(request["cols"], "cols"), _dim(request["rows"], "rows"))
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_interrupt(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        return {"interrupted": harness.interrupt()}

    def _op_agent_models(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"models": self.controller.agent_models(request["target"])}

    def _op_agent_set_model(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"agent": self.controller.set_agent_model(
            request["target"], request.get("model"))}

    def _op_agent_history(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        before = request.get("before")
        out = harness.history(
            int(request.get("limit", 200)), None if before is None else int(before)
        )
        # Lets a viewer notice /clear or /new even before the new one has messages.
        return {**out, "conversation_id": harness.conversation_id}

    def _op_agent_hook(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.record_hook(
            request["agent"], request.get("token"), request["event"],
            conversation_id=request.get("conversation_id"),
        )

    def _op_agent_permission_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Claude's PermissionRequest/PreToolUse hook: wait for an answer from a front-end.

        The card is always created so it is never lost; while a Web UI is connected
        it waits the full ``agents.permission_wait``; with no viewer it holds a short
        grace (:data:`PERMISSION_GRACE`) so a front-end that reconnects right after
        still sees it. ``CREWHALL_PERMISSION_WAIT`` (seconds, 0 disables) sets the
        full wait.
        """
        try:
            wait = float(settings.get("agents.permission_wait"))
        except ValueError:
            wait = PERMISSION_WAIT
        if time.monotonic() - self._viewer_seen > VIEWER_FRESH:
            wait = min(wait, PERMISSION_GRACE)
        return self.controller.claude_permission_request(
            request["agent"], request.get("token"), request.get("payload"),
            wait=max(0.0, min(wait, PERMISSION_WAIT_MAX)),
        )

    def _op_agent_new_session(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        return self.controller.new_session(harness, timeout=float(request.get("timeout", 20.0)))

    def _op_agent_processes(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.list_processes(request["target"])

    def _op_agent_process_output(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.process_output(
            request["target"], task=request.get("task"), call=request.get("call"),
        )

    def _op_agent_process_signal(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.signal_process(
            request["target"], int(request["pid"]), str(request.get("signal", "INT")),
            by=str(request.get("by") or "user")[:40],
        )

    def _op_agent_identity(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.agent_identity(request["target"], request.get("token"))

    def _op_agent_capture(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        output = harness.capture()
        if request.get("recent"):
            output = harness.capture_recent(int(request.get("max_lines", 40)))
        return {"output": output, "agent": self.controller.agent_summary(harness)}

    def _op_agent_transcript(self, request: dict[str, Any]) -> dict[str, Any]:
        """Capture with the agent's own input area removed (for Web UIs that
        draw their own input)."""
        harness = self.controller.get_agent(request["target"])
        output = harness.transcript()
        max_lines = int(request.get("max_lines", 0) or 0)
        if max_lines > 0:
            output = "\n".join(output.splitlines()[-max_lines:])
        return {"output": output, "agent": self.controller.agent_summary(harness)}

    def _op_agent_state(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_wait(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        target = AgentState(request["state"])
        state = harness.wait_for_state(
            target, timeout=float(request.get("timeout", 30.0))
        )
        info = self.controller.agent_summary(harness)
        return {"reached": state is target or state == target, "agent": info}

    def _op_agent_restart(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.restart_agent(request["target"])
        try:
            harness.start(timeout=float(request.get("timeout", 20.0)))
        except Exception:  # noqa: BLE001 - return the summary even if readiness times out
            log.exception("agent restart: readiness wait failed")
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_stop(self, request: dict[str, Any]) -> dict[str, Any]:
        info = self.controller.remove_agent(
            request["target"], force=bool(request.get("force", False))
        )
        return {"agent": info.to_dict()}

    def _op_message_send(self, request: dict[str, Any]) -> dict[str, Any]:
        delivery = self.controller.send_message(
            request["sender"], request["recipient"], request["body"],
            idempotency_key=request.get("idempotency_key"),
            message_id=request.get("message_id"),
            conversation_id=request.get("conversation_id"),
        )
        return {"delivery": delivery.to_dict()}

    def _op_message_history(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "messages": self.controller.message_history(
                agent=request.get("agent"), limit=request.get("limit")
            )
        }

    def _op_agent_archive_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"archived": self.controller.list_archived_agents()}

    def _op_agent_archive_get(self, request: dict[str, Any]) -> dict[str, Any]:
        rec = self.controller.read_archived_agent(str(request.get("target") or ""))
        if rec is None:
            return {"found": False}
        return {"found": True, "archive": rec}
