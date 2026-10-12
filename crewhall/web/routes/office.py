"""OnlyOffice integration routes (Handler mixin)."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import json
import os
from typing import Any


class OfficeRoutes:
    def _request_base(self) -> str:
        host = self.headers.get("Host") or "127.0.0.1"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}"

    def _office_config(self, query: dict[str, list[str]]) -> None:
        from ... import chat as conversations, office

        if not office.enabled():
            return self._error("not found", 404)
        cid = (query.get("id") or [""])[0]
        rel = (query.get("path") or [""])[0]
        try:
            path = conversations.resolve(cid, "outputs", rel)
        except conversations.ConversationError as exc:
            return self._error(str(exc), 404)
        if not os.path.isfile(path):
            return self._error("artifact not found", 404)
        ok, why = office.validate_file(path)
        if not ok:
            ext = os.path.splitext(path)[1]
            return self._error(
                f"this artifact is not a valid {ext} file ({why}); regenerate it before "
                "opening it in the editor", 422)
        base = office.base_url(self._request_base())
        if not base:
            return self._error("office.base_url is not configured", 409)
        config = office.build_config(
            conversation_id=cid, relpath=rel, name=os.path.basename(path), path=path,
            document_base=base, callback_base=base, user_id="web", user_name="you",
        )
        return self._json({"ok": True, "config": config, "public_url": office.public_url()})

    def _office_callback(self, query: dict[str, list[str]]) -> None:
        import urllib.request

        from ... import audit, office
        from ... import chat as conversations

        if not office.enabled() or not self._host_ok():
            return self._error("not found", 404)
        raw = self._read_body()
        if raw is None:
            return
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error("bad json")
        if not self._office_token_ok(body):
            return self._error("invalid OnlyOffice token", 403)
        status = body.get("status")
        if status not in (2, 6):
            return self._json({"error": 0})
        cid = (query.get("id") or [""])[0]
        rel = (query.get("path") or [""])[0]
        url = body.get("url")
        if not url:
            return self._json({"error": 1})
        try:
            path = conversations.resolve(cid, "outputs", rel)
        except conversations.ConversationError:
            return self._json({"error": 1})
        cap = conversations.max_bytes()
        try:
            with urllib.request.urlopen(str(url), timeout=30) as resp:  # noqa: S310 - OnlyOffice-provided URL, JWT verified
                data = resp.read(cap + 1)
        except Exception:  # noqa: BLE001
            return self._json({"error": 1})
        if len(data) > cap:
            return self._json({"error": 1})
        # Never overwrite a good artifact with a corrupt one: OnlyOffice can return
        # an error page or a truncated file instead of the document. A valid OOXML
        # is required so Microsoft Office (and any other suite) can open it.
        name = os.path.basename(path)
        ok, why = office.validate_document(name, data)
        if not ok:
            audit.record("conversation_artifact_download", actor="onlyoffice", result="rejected",
                         summary=f"id={cid} saved={name} reason={why}")
            return self._json({"error": 1})
        try:
            conversations.write_artifact(cid, rel, data)
        except conversations.ConversationError:
            return self._json({"error": 1})
        audit.record("conversation_artifact_download", actor="onlyoffice", result="ok",
                     summary=f"id={cid} saved={name}")
        return self._json({"error": 0})

    def _office_token_ok(self, body: dict[str, Any]) -> bool:
        from ... import office

        if not office.jwt_active():
            return True
        if office.callback_body_ok(body):
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            try:
                office.jwt_decode(header[7:])
                return True
            except office.OfficeError:
                return False
        return False
