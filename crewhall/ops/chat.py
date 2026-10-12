"""Daemon ops: chat (mixins composed by :class:`crewhall.daemon.Server`)."""
from __future__ import annotations

# File-wide F401: each mixin keeps the daemon's name surface so a moved
# handler needs no import surgery; ruff's unused-import check is not useful here.
# ruff: noqa: F401
from typing import Any
from .common import OpsBase


class OpsChat(OpsBase):

    def _op_conversation_search(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"matches": self.controller.search_conversations(
            str(request.get("query") or ""), int(request.get("limit", 50) or 50))}

    def _op_conversation_create(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations

        team = request.get("team") or None
        if team:
            # Store the stable team_id (a name can be renamed) and refuse a
            # remote team: a chat's lead runs locally with its own workspace.
            team_obj = self.controller.teams.get(str(team))
            if team_obj.host:
                raise conversations.ConversationError(
                    "a remote team cannot be a chat's crew")
            team = team_obj.team_id
        meta = conversations.create(
            str(request.get("title") or ""), agent_id=request.get("agent_id"),
            team=team)
        env = conversations.env(meta["id"])
        meta = {**meta, "dir": env["CREWHALL_CONVERSATION_DIR"],
                "outputs": env["CREWHALL_OUTPUTS"], "inputs": env["CREWHALL_INPUTS"],
                "workspace": env["CREWHALL_WORKSPACE"]}
        return {"conversation": meta}

    def _op_conversation_list(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations

        return {"conversations": [self._conversation_row(m) for m in conversations.list_all()]}

    def _conversation_row(self, meta: dict[str, Any]) -> dict[str, Any]:
        """A conversation as list views show it: file counts, live agent, crew name."""
        from .. import chat as conversations

        row = dict(meta)
        row["inputs"] = len(conversations.list_files(meta["id"], "inputs"))
        row["outputs"] = len(conversations.list_files(meta["id"], "outputs"))
        agent = None
        if meta.get("agent_id"):
            try:
                agent = self.controller.agent_summary(
                    self.controller.get_agent(meta["agent_id"]))
            except Exception:  # noqa: BLE001 - a gone agent must not hide the chat
                agent = None
        row["agent"] = agent
        team = self.controller._team_or_none(meta.get("team"))
        row["team_name"] = team.name if team is not None else None
        return row

    def _op_conversation_info(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations

        info = conversations.info(str(request["id"]))
        agent = None
        if info.get("agent_id"):
            try:
                agent = self.controller.agent_summary(
                    self.controller.get_agent(info["agent_id"]))
            except Exception:  # noqa: BLE001
                agent = None
        info["agent"] = agent
        return {"conversation": info}

    def _op_conversation_rename(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations

        return {"conversation": conversations.rename(
            str(request["id"]), str(request.get("title") or ""))}

    def _op_conversation_set_agent(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations

        return {"conversation": conversations.set_agent(
            str(request["id"]), request.get("agent_id"))}

    def _op_conversation_set_team(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"conversation": self.controller.set_conversation_team(
            str(request["id"]), request.get("team"))}

    def _op_conversation_activity(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.conversation_activity(
            str(request["id"]), limit=int(request.get("limit", 50) or 50),
            all_team=bool(request.get("all_team", False)))

    def _op_conversation_crew(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations

        team = str(request.get("team") or "")
        if team:
            metas = conversations.crew_conversations(self.controller.teams.get(team).team_id)
        else:
            metas = [m for m in conversations.list_all() if m.get("team")]
        return {"conversations": [self._conversation_row(m) for m in metas]}

    def _op_conversation_publish(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self.controller.publish_artifact(
            request["sender"], request.get("token"), str(request["id"]),
            str(request.get("source") or ""), request.get("name"))
        return result

    def _op_conversation_delete(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations

        return conversations.delete(
            str(request["id"]), delete_files=bool(request.get("delete_files")))

    def _op_conversation_artifact_delete(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations

        return {"artifact": conversations.delete_artifact(
            str(request["id"]), str(request.get("path") or ""))}
