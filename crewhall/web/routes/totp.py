"""TOTP code routes (Handler mixin)."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import json
import secrets
import time

from .. import auth, terminal_tokens, totp


class TotpRoutes:
    def _do_totp(self, action: str) -> None:
        from ... import audit

        if not self._host_ok() or not self._origin_ok():
            return self._error("request not allowed", 403)
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error("bad json")
        if action == "begin":
            if not self._guard():
                return
            secret = totp.new_secret()
            ceremony = secrets.token_urlsafe(24)
            label = (payload.get("label") or "web")[:80]
            now = time.time()
            with self.server.totp_lock:
                for key in [k for k, v in self.server.totp_pending.items()
                            if v.get("expires", 0) < now]:
                    self.server.totp_pending.pop(key, None)
                self.server.totp_pending[ceremony] = {
                    "secret": secret, "label": label, "expires": now + 600.0}
            return self._json({"ok": True, "ceremony": ceremony, "secret": secret,
                               "uri": totp.otpauth_uri(secret, label)})
        if action == "confirm":
            if not self._guard():
                return
            with self.server.totp_lock:
                pending = self.server.totp_pending.get(payload.get("ceremony", ""))
                if pending and pending.get("expires", 0) < time.time():
                    pending = None
            if not pending:
                return self._error("unknown or expired ceremony", 400)
            if totp.verify(pending["secret"], payload.get("code")) is None:
                return self._error("invalid code", 401)
            record = totp.add_enrollment(pending["secret"], pending["label"])
            with self.server.totp_lock:
                self.server.totp_pending.pop(payload.get("ceremony", ""), None)
            audit.record("web_totp_add", actor=self._audit_actor(), result="ok",
                         summary=f"id={record['id']}")
            return self._json({"ok": True, "enrollment": {"id": record["id"],
                                                          "label": record["label"]}})
        if action == "login":
            enrollment = totp.verify_any(payload.get("code"))
            if not enrollment:
                ip = "totp:" + self._client_label()
                auth.note_login_failure(ip)
                return self._error("invalid code", 401)
            auth.note_login_success("totp:" + self._client_label())
            session = auth.issue_session(ip=self._client_label(),
                                         user_agent=self.headers.get("User-Agent", ""))
            self._grant_totp_terminals(session)
            audit.record("web_login", actor=self._audit_actor(), result="ok", summary="totp")
            return self._json({"ok": True}, extra=[self._session_cookie(session)])
        if action == "list":
            return self._json({"ok": True, "enrollments": totp.list_enrollments(),
                               "file_permissions_ok": totp.file_permissions_ok()})
        if action == "revoke":
            if not self._guard():
                return
            removed = totp.revoke(payload.get("id"))
            audit.record("web_totp_revoke", actor=self._audit_actor(),
                         result="ok" if removed else "error",
                         summary=f"id={payload.get('id')}")
            return self._json({"ok": True, "revoked": removed})
        return self._error("not found", 404)

    def _grant_totp_terminals(self, session: str | None) -> bool:
        """Unlock every terminal for a TOTP session when ``terminals.totp_grants`` is on."""
        from ... import settings

        if not settings.get("terminals.enabled") or not settings.get("terminals.totp_grants"):
            return False
        return auth.unlock_terminal(
            session, scopes=[terminal_tokens.SCOPE_READ, terminal_tokens.SCOPE_WRITE],
            hosts=["*"], token_id=None)
