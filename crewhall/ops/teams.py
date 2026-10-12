"""Daemon ops: teams (mixins composed by :class:`crewhall.daemon.Server`)."""
from __future__ import annotations

# File-wide F401: each mixin keeps the daemon's name surface so a moved
# handler needs no import surgery; ruff's unused-import check is not useful here.
# ruff: noqa: F401
from typing import Any
from .common import OpsBase


class OpsTeams(OpsBase):

    def _op_team_create(self, request: dict[str, Any]) -> dict[str, Any]:
        team = self.controller.create_team(
            request["name"],
            request["agent_ids"],
            workspace=request.get("workspace"),
            workspace_mode=request.get("workspace_mode"),
            host=request.get("host"),
        )
        return {"team": self.controller.team_info(team.team_id)}

    def _op_team_up(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.team_up(request["spec"])

    def _op_team_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"teams": self.controller.list_teams()}

    def _op_team_info(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"team": self.controller.team_info(request["target"])}

    def _op_team_add_member(self, request: dict[str, Any]) -> dict[str, Any]:
        team = self.controller.add_team_member(request["target"], request["agent"])
        return {"team": self.controller.team_info(team.team_id)}

    def _op_team_remove_member(self, request: dict[str, Any]) -> dict[str, Any]:
        team = self.controller.remove_team_member(request["target"], request["agent"])
        return {"team": self.controller.team_info(team.team_id)}

    def _op_team_set_workspace(self, request: dict[str, Any]) -> dict[str, Any]:
        extra = {"host": request["host"] or None} if "host" in request else {}
        team = self.controller.set_team_workspace(
            request["target"], request.get("workspace"), **extra
        )
        return {"team": self.controller.team_info(team.team_id)}

    def _op_team_remove(self, request: dict[str, Any]) -> dict[str, Any]:
        team = self.controller.remove_team(request["target"])
        return {"team": team.to_dict()}

    def _op_team_send(self, request: dict[str, Any]) -> dict[str, Any]:
        delivery = self.controller.send_message_as(
            request["sender"], request.get("token"),
            request["recipient"], request["body"],
            idempotency_key=request.get("idempotency_key"),
            message_id=request.get("message_id"),
            conversation_id=request.get("conversation_id"),
        )
        return {"delivery": delivery.to_dict()}

    def _op_team_members(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"members": self.controller.team_members(request["target"])}

    def _op_team_new_session(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.new_session_as(
            request["sender"], request.get("token"), request["target"],
            timeout=float(request.get("timeout", 20.0)),
        )
