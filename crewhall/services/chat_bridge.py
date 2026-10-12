"""Controller services: chat_bridge (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import time
import uuid
from typing import Any
from .. import interactions as ix, settings
from ..harness import Harness
from ..messaging import MessagingError
from ..opencode_link import OpenCodeLink
from ..requests import OPEN_STATES, RequestError
from ..team import TeamError
from ..controller import AGENT_SEND_WAIT, log


class ServicesChatBridge:

    def _open_opencode_request(self, harness: Harness, kind: str, req: dict[str, Any]) -> None:
        rid = req.get("id")
        if not isinstance(rid, str) or not rid:
            return
        if kind == "permission":
            data = ix.opencode_permission(req)
        else:
            data = {"questions": ix.normalize_questions(req.get("questions")),
                    "session": req.get("sessionID")}
            if not data["questions"]:
                return
        item, created = self.interactions.open(
            harness.agent_id, kind, f"{harness.agent_id}:{rid}", data,
            ref={"source": "opencode", "request_id": rid},
        )
        if created:
            self._log_interaction(harness, "asked", item)

    def _on_opencode_interaction(self, harness: Harness, kind: str, props: dict[str, Any]) -> None:
        if kind == "permission.asked":
            self._open_opencode_request(harness, "permission", props)
        elif kind == "question.asked":
            self._open_opencode_request(harness, "question", props)
        else:  # replied / rejected, possibly in the TUI itself
            rid = props.get("requestID")
            if isinstance(rid, str):
                answer = (
                    {"reply": props.get("reply")} if kind == "permission.replied"
                    else {"answers": props.get("answers")} if kind == "question.replied"
                    else {"reject": True}
                )
                item = self.interactions.close(f"{harness.agent_id}:{rid}", answer=answer, by="agent")
                if item is not None and item.answered_by == "agent":
                    self._log_interaction(harness, "answered", item)

    def _sync_opencode_interactions(self, harness: Harness, link: OpenCodeLink) -> None:
        try:
            perms, questions = link.pending_permissions(), link.pending_questions()
        except Exception:  # noqa: BLE001 - the server may not be up yet
            return
        live = set()
        for req in perms:
            self._open_opencode_request(harness, "permission", req)
            live.add(f"{harness.agent_id}:{req.get('id')}")
        for req in questions:
            self._open_opencode_request(harness, "question", req)
            live.add(f"{harness.agent_id}:{req.get('id')}")
        for item in self.interactions.pending(harness.agent_id):
            if item.ref.get("source") == "opencode" and item.interaction_id not in live:
                self.interactions.close(item.interaction_id)

    def _maybe_sync_interactions(self, harness: Harness) -> None:
        """Defensive poll: recover OpenCode questions if the event was missed.

        The bridge relies on the agent's event stream; a dropped ``question.asked``
        (stream hiccup, reconnect race) would otherwise leave the request invisible.
        Summarising the agent (which the UI and the daemon do regularly) re-checks
        the CLI's own pending list, throttled so it is cheap.
        """
        link = getattr(harness, "link", None)
        if link is None:
            return
        now = time.monotonic()
        if now - self._interaction_sync.get(harness.agent_id, 0.0) < 2.0:
            return
        self._interaction_sync[harness.agent_id] = now
        self._sync_opencode_interactions(harness, link)

    def _log_interaction(self, harness: Harness, what: str, item: ix.Interaction) -> None:
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("interaction", {"what": what, "id": item.interaction_id, "kind": item.kind,
                                   "answer": item.answer, "by": item.answered_by})

    def list_interactions(self, target: str | None = None) -> list[dict[str, Any]]:
        agent_id = self.agents.resolve(target).agent_id if target else None
        return [i.to_dict() for i in self.interactions.pending(agent_id)]

    def respond_interaction(
        self, interaction_id: str, answer: Any, by: str = "user",
    ) -> dict[str, Any]:
        """Answer a pending permission/question through the agent CLI's own API."""
        item = self.interactions.get(interaction_id)
        if item.state != "pending":
            raise ix.InteractionError("this request was already answered")
        clean = ix.validate_answer(item, answer)
        harness = self.agents.resolve(item.agent_id)
        if item.ref.get("source") == "claude":
            # The agent's permission hook is blocked on this; it hands the decision to Claude.
            closed = self.interactions.close(interaction_id, answer=clean, by=by)
            if closed is not None:
                self._log_interaction(harness, "answered", closed)
            return (closed or item).to_dict()
        link = harness.link
        if item.ref.get("source") != "opencode" or link is None:
            raise ix.InteractionError("this agent cannot be answered from here")
        rid = item.ref["request_id"]
        try:
            if item.kind == "permission":
                link.reply_permission(rid, clean["reply"], clean.get("message"))
            elif clean.get("reject"):
                link.reject_question(rid)
            else:
                link.reply_question(rid, clean["answers"])
        except OSError as exc:
            # Gone on the agent's side (answered in the TUI, agent restarted…).
            self._sync_opencode_interactions(harness, link)
            raise ix.InteractionError(f"the agent did not accept the answer: {exc}") from exc
        closed = self.interactions.close(interaction_id, answer=clean, by=by)
        if closed is not None:
            self._log_interaction(harness, "answered", closed)
        return (closed or item).to_dict()

    def claude_permission_request(
        self, agent: str, token: str | None, payload: Any, wait: float,
    ) -> dict[str, Any]:
        """A Claude permission dialog / question, held until answered or ``wait`` passes.

        Returns ``{"decision": {...}}`` for Claude, or ``{"decision": None}``
        to let Claude show its own dialog (no answer in time, answered "in the
        terminal", unusable payload, or the feature disabled with ``wait <= 0``).
        """
        harness = self.agents.resolve(agent)
        self._check_token(harness.agent_id, token)
        if wait <= 0 or not isinstance(payload, dict):
            return {"decision": None}
        parsed = ix.claude_request(payload)
        if parsed is None:
            return {"decision": None}
        kind, data = parsed
        ref_id = payload.get("tool_use_id")
        if not (isinstance(ref_id, str) and ref_id.isascii() and len(ref_id) <= 100):
            ref_id = uuid.uuid4().hex
        item, created = self.interactions.open(
            harness.agent_id, kind, f"{harness.agent_id}:{ref_id}", data,
            ref={"source": "claude"},
        )
        if created:
            self._log_interaction(harness, "asked", item)
        if not item.done.wait(wait):
            # Nobody answered here: the TUI dialog takes over.
            self.interactions.close(item.interaction_id, answer={"terminal": True}, by="timeout")
        return {"decision": ix.claude_decision(item, payload, item.answer)}

    def _follow_opencode_session(self, harness: Harness, sid: str) -> None:
        children = self._opencode_children.setdefault(harness.agent_id, set())
        if sid in children or harness.link is None:
            return
        if sid in self._retired_conversations.get(harness.agent_id, ()):
            return  # the old conversation finishing up after /new
        try:
            info = harness.link.get_json(f"/session/{sid}")
        except Exception:  # noqa: BLE001
            return
        if not isinstance(info, dict):
            return
        if info.get("parentID"):
            children.add(sid)
        else:
            self._adopt_conversation(harness, sid)

    def publish_artifact(
        self, sender: str, token: str | None, conversation_id: str,
        source: str, name: str | None = None,
    ) -> dict[str, Any]:
        """Copy a cowork deliverable into a chat's outputs, tagged with producer.

        The acting agent must be the chat's own lead or a member of its crew. The
        source is read (never run) and must live under a known Cowork root, so a
        compromised worker cannot exfiltrate files outside its scope.
        """
        import os as _os
        import stat as _stat

        from .. import chat as conversations

        sender_h = self.agents.resolve(sender)
        if not self.agent_in_conversation(sender_h.agent_id, token, conversation_id):
            raise MessagingError("permission denied: not a member of this chat's crew")
        src = _os.path.abspath(_os.path.expanduser(str(source or "")))
        if not _os.path.isfile(src) or _os.path.islink(src):
            raise conversations.ConversationError("source file not found")
        real = _os.path.realpath(src)
        roots = [_os.path.realpath(r) for r in self._cowork_roots() if r]
        if not any(real == r or real.startswith(r + _os.sep) for r in roots):
            raise conversations.ConversationError(
                "the source is outside the allowed directories")
        # Open with O_NOFOLLOW and re-check on the *opened* fd: the islink/root
        # checks above are advisory, a symlink swapped in between them and the
        # open (TOCTOU) must not let an outside file through.
        nofollow = getattr(_os, "O_NOFOLLOW", 0)
        fd = _os.open(real, _os.O_RDONLY | nofollow)
        try:
            st = _os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode):
                raise conversations.ConversationError("source is not a regular file")
            if st.st_size > conversations.max_bytes():
                raise conversations.ConversationError("file too large")
            data = b""
            while True:
                chunk = _os.read(fd, 1 << 20)
                if not chunk:
                    break
                data += chunk
        finally:
            _os.close(fd)
        info = conversations.store_artifact(
            conversation_id, name or _os.path.basename(real), data)
        conversations.set_producer(conversation_id, info["relpath"], sender_h.agent_id)
        return {"artifact": info, "producer": sender_h.agent_id}

    def set_conversation_team(
        self, conversation_id: str, team: str | None,
    ) -> dict[str, Any]:
        """Attach/detach a chat's crew and move its live lead accordingly.

        Without this the stored ``team`` and the lead's real membership drift:
        an existing lead could not delegate until it was recreated.
        """
        from .. import chat as conversations

        conv = conversations.get(conversation_id)
        new_team = None
        if team:
            team_obj = self.teams.get(str(team))  # unknown team -> error
            if team_obj.host:
                raise conversations.ConversationError(
                    "a remote team cannot be a chat's crew")
            new_team = team_obj.team_id
        lead = conv.get("agent_id")
        old_team = self._team_or_none(conv.get("team"))
        if lead and old_team is not None and old_team.team_id != new_team:
            try:
                self.teams.remove_member(old_team.team_id, str(lead))
            except Exception:  # noqa: BLE001 - not a member anymore
                log.debug("ignored exception in a boundary guard", exc_info=True)
        if lead and new_team:
            try:
                if str(lead) not in self.teams.get(new_team).agent_ids:
                    self.teams.add_member(new_team, str(lead))
            except (KeyError, TeamError):
                pass  # the lead is not running here; it joins when created
        meta = conversations.set_team(conversation_id, new_team)
        self._persist()
        return meta

    def conversation_activity(
        self, conversation_id: str, limit: int = 50, all_team: bool = False
    ) -> dict[str, Any]:
        """The crew's in-flight handoffs/requests for a chat's activity strip.

        By default only traffic tagged with this ``conversation_id`` is shown, so
        two chats sharing a Team each see their own strip. ``all_team`` widens it
        to every message between the crew's members (the "see all team" toggle).
        """
        from .. import chat as conversations

        conv = conversations.get(conversation_id)
        members = self._team_members(conv.get("team"))
        if conv.get("agent_id"):
            members.add(str(conv["agent_id"]))
        history = self.messaging.history(limit=None)
        if all_team:
            messages = [
                m for m in history
                if m.get("sender") in members or m.get("recipient") in members
            ]
        else:
            messages = [m for m in history if m.get("conversation_id") == conversation_id]
        requests = [
            r for r in self.list_requests(open_only=False)
            if r.get("sender") in members or r.get("recipient") in members
        ]
        return {
            "conversation_id": conversation_id,
            "agent_id": conv.get("agent_id"),
            "team": conv.get("team"),
            "members": sorted(members),
            "messages": messages[-limit:],
            "requests": requests[-limit:],
        }

    def message_history(
        self, *, agent: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return self.messaging.history(agent=agent, limit=limit)

    def search_conversations(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search the readable conversation of every agent for a substring."""
        q = (query or "").strip()
        if len(q) < 2:
            return []
        low = q.lower()
        out: list[dict[str, Any]] = []
        for harness in self.agents.all():
            try:
                data = harness.history(limit=200)
            except Exception:  # noqa: BLE001 - one unreadable agent must not stop the search
                continue
            if not data.get("available"):
                continue
            start = int(data.get("start") or 0)
            for i, msg in enumerate(data.get("messages") or []):
                text = str(msg.get("text") or "")
                pos = text.lower().find(low)
                if pos < 0:
                    continue
                snippet = text.replace("\n", " ").strip()
                pos = snippet.lower().find(low)
                cut = max(0, pos - 60)
                out.append({
                    "agent_id": harness.agent_id,
                    "name": harness.name or harness.agent_id,
                    "kind": harness.kind,
                    "role": msg.get("role"),
                    "at": msg.get("at"),
                    "index": start + i,
                    "snippet": snippet[cut:cut + 220],
                })
                if len(out) >= limit:
                    return out
        return out

    def create_request(self, sender: str, token: str | None, recipient: str, task: str, *,
                       timeout: float | None = None, wait: bool = True) -> dict[str, Any]:
        """Send a request and (optionally) wait for the correlated reply."""
        sender_h = self.agents.resolve(sender)
        self._check_token(sender_h.agent_id, token)
        recipient_h = self.agents.resolve(recipient)
        self._authorize_team(sender_h, recipient_h)
        req = self.requests.create(sender_h.agent_id, recipient_h.agent_id, task, timeout=timeout)
        body = (
            f"[request {req.request_id} from {sender_h.name or sender_h.agent_id}]\n{req.task}\n"
            f"Reply with: crewhall message reply {req.request_id} \"<your answer>\""
        )
        delivery = self.messaging.send(
            sender_h.agent_id, recipient_h.agent_id, body,
            authorize=self._authorize_team, prefix_sender=False, wait=AGENT_SEND_WAIT,
        )
        if delivery.acknowledged_at or delivery.delivered:
            self.requests.accept(req.request_id)
        if not (delivery.delivered or delivery.queued):
            self.requests.fail(req.request_id, delivery.error or "not delivered")
            failed = self.requests.get(req.request_id) or req
            return {"request": failed.to_dict(), "delivery": delivery.to_dict()}
        if not wait:
            current = self.requests.get(req.request_id) or req
            return {"request": current.to_dict(), "delivery": delivery.to_dict()}
        return {"request": self._await_reply(req.request_id),
                "delivery": delivery.to_dict()}

    def _await_reply(self, request_id: str) -> dict[str, Any]:
        while True:
            req = self.requests.get(request_id)
            if req is None:
                raise RequestError(f"unknown request {request_id!r}")
            if req.state not in OPEN_STATES:
                return req.to_dict()
            remaining = req.deadline - time.time()
            if remaining <= 0:
                return req.to_dict()  # sweep marked it expired
            time.sleep(min(0.25, max(0.02, remaining)))

    def reply_to_request(self, agent: str, token: str | None, request_id: str,
                         body: str) -> dict[str, Any]:
        harness = self.agents.resolve(agent)
        self._check_token(harness.agent_id, token)
        req = self.requests.reply(request_id, harness.agent_id, body)
        self.messaging.send(
            harness.agent_id, req.sender, f"[reply {request_id}] {req.reply}",
            authorize=self._authorize_team, prefix_sender=False, wait=AGENT_SEND_WAIT,
        )
        return req.to_dict()

    def cancel_request(self, agent: str, token: str | None, request_id: str) -> dict[str, Any]:
        harness = self.agents.resolve(agent)
        self._check_token(harness.agent_id, token)
        return self.requests.cancel(request_id, harness.agent_id).to_dict()

    def list_requests(self, *, open_only: bool = False, agent: str | None = None,
                      team_ids: set[str] | None = None) -> list[dict[str, Any]]:
        return self.requests.list(open_only=open_only, agent=agent, team_ids=team_ids)

    def _cowork_roots(self) -> list[str]:
        """Directories a publish source may come from (no arbitrary reads)."""
        roots: list[str] = []
        try:
            roots += [str(r) for r in (settings.get("security.fs_roots") or []) if r]
        except Exception:  # noqa: BLE001
            log.debug("ignored exception in a boundary guard", exc_info=True)
        for team in self.teams.list():
            if team.workspace:
                roots.append(team.workspace)
        for harness in self.agents.all():
            spec = getattr(harness.session, "spec", None)
            if spec is not None and spec.cwd:
                roots.append(spec.cwd)
        return roots

    def _authorize_team(self, sender: Harness, recipient: Harness) -> None:
        if not self._shared_team(sender.agent_id, recipient.agent_id):
            raise MessagingError(
                "permission denied: recipient is not a member of a shared Team"
            )

    def agent_in_conversation(
        self, sender: str, token: str | None, conversation_id: str,
    ) -> bool:
        """Whether an authenticated agent leads ``conversation_id`` or is in its crew."""
        from .. import chat as conversations

        sender_h = self.agents.resolve(sender)
        self._check_token(sender_h.agent_id, token)
        conv = conversations.get(conversation_id)
        members = self._team_members(conv.get("team"))
        if conv.get("agent_id"):
            members.add(str(conv["agent_id"]))
        return sender_h.agent_id in members
