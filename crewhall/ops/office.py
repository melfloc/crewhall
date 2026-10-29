"""Daemon ops: office (mixins composed by :class:`crewhall.daemon.Server`)."""
from __future__ import annotations

# File-wide F401: each mixin keeps the daemon's name surface so a moved
# handler needs no import surgery; ruff's unused-import check is not useful here.
# ruff: noqa: F401
import os
import threading
from typing import Any
from .common import OpsBase, log


class OpsOffice(OpsBase):

    def _op_office_collab_open(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import chat as conversations, office
        from ..office import collab as office_collab

        if not office.enabled():
            raise ValueError("OnlyOffice is not enabled")
        if not office.collab_enabled():
            raise ValueError("live co-editing is disabled (office.collab_enabled)")
        cid = str(request["id"])
        rel = str(request.get("path") or "")
        path = conversations.resolve(cid, "outputs", rel)
        ok, why = office.validate_file(path)
        if not ok:
            raise ValueError(f"the artifact is not a valid {os.path.splitext(path)[1]} file "
                             f"({why}); regenerate it before co-editing it")
        base = office.agent_base(str(request.get("base") or ""))
        if not base:
            raise ValueError("office.base_url (or web.public_url) must be set "
                             "so the Document Server can fetch the document")
        config = office.build_config(
            conversation_id=cid, relpath=rel, name=os.path.basename(path), path=path,
            document_base=base, callback_base=base, user_id="crewhall-ai",
            user_name="AI Agent",
        )
        return office_collab.REGISTRY.open(cid, rel, config, office.public_url())

    def _op_office_collab_read(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        text = office_collab.REGISTRY.read(str(request["id"]), str(request.get("path") or ""))
        return {"text": text}

    def _op_office_collab_command(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        result = office_collab.REGISTRY.command(
            str(request["id"]), str(request.get("path") or ""),
            str(request["method"]), request.get("args"))
        return {"result": result}

    def _op_office_collab_insert(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"result": office_collab.REGISTRY.insert(
            str(request["id"]), str(request.get("path") or ""), str(request.get("text") or ""))}

    def _op_office_collab_save(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"result": office_collab.REGISTRY.save(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_close(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"closed": office_collab.REGISTRY.close(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_list(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"editors": office_collab.REGISTRY.list()}

    def _op_office_collab_users(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"users": office_collab.REGISTRY.users(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_chat(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"messages": office_collab.REGISTRY.chat_messages(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_say(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"result": office_collab.REGISTRY.chat_send(
            str(request["id"]), str(request.get("path") or ""), str(request.get("text") or ""))}

    def _op_office_collab_comments(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"comments": office_collab.REGISTRY.comments(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_comment_add(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..office import collab as office_collab

        return {"result": office_collab.REGISTRY.add_comment(
            str(request["id"]), str(request.get("path") or ""), str(request.get("text") or ""))}

    def _op_office_status(self, request: dict[str, Any]) -> dict[str, Any]:
        """Report the OnlyOffice infrastructure state (for the Web UI button)."""
        from ..office import setup as office_setup

        try:
            return {"status": office_setup.status()}
        except Exception as exc:  # noqa: BLE001 - a broken probe must not break the UI
            return {"status": {"docker": False, "compose": False, "healthy": False,
                               "error": str(exc)}}

    def _op_office_start(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start/restart/stop the Document Server from the Web UI.

        ``start``/``restart`` run in the background (they may pull an image and take
        a while); the caller polls ``office_status`` until it turns healthy. ``stop``
        is quick and runs inline.
        """
        from ..office import setup as office_setup

        action = str(request.get("action") or "start")
        if action == "stop":
            return {"result": office_setup.stop()}
        if action not in ("start", "restart"):
            raise ValueError(f"unknown office action: {action}")

        def work() -> None:
            try:
                if action == "restart":
                    office_setup.restart()
                office_setup.start(wait=False, log=lambda *a: log.info("office: %s", " ".join(map(str, a))))
            except Exception:  # noqa: BLE001 - a failed background start must not crash the daemon
                log.exception("office %s failed", action)

        threading.Thread(target=work, name=f"office-{action}", daemon=True).start()
        return {"started": True, "action": action}
