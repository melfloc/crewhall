"""Web auth routes: login/logout, sessions and web-admin ops (Handler mixin)."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import json
from typing import Any

from .. import auth, terminal_tokens
from ..shared import _LOGIN_HTML


class AuthRoutes:
    def _do_logout(self) -> None:
        # Revoke this session server-side too (clear the cookie and the record).
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == auth.COOKIE_NAME and value:
                auth.revoke_session(value)
        return self._json(
            {"ok": True},
            extra=[("Set-Cookie", auth.cookie_header("", secure=self.security.secure_cookie) + "; Max-Age=0")],
        )

    def _web_admin(self, op: str, payload: dict[str, Any] | None = None) -> None:
        """Session/token administration, handled in the web layer (no daemon)."""
        from ... import audit

        payload = payload or {}
        if op == "web_sessions":
            return self._json({"ok": True, "sessions": auth.list_sessions()})
        if op == "audit_list":
            return self._json({"ok": True, "events": audit.recent(
                limit=payload.get("limit", 200),
                op=payload.get("filter_op") or None,
                result=payload.get("filter_result") or None,
            )})
        if op == "web_session_revoke":
            revoked = auth.revoke_sessions()
            audit.record("web_session_revoke", actor=self._audit_actor(),
                         result="ok", summary=f"revoked={revoked}")
            return self._json({"ok": True, "revoked": revoked})
        if op == "web_token_status":
            return self._json({
                "ok": True, "token_exists": auth.token_exists(),
                "fingerprint": auth.token_fingerprint(),
                "email": None, "file_permissions_ok": auth.file_permissions_ok(),
                "require_auth": self.security.require_auth,
            })
        if op == "web_token_rotate":
            # A new token (shown once); every other device must sign in again, this one stays signed in.
            token = auth.generate_token(rotate=True)
            mine = ""
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == auth.COOKIE_NAME:
                    mine = value
            dropped = auth.revoke_sessions(keep=mine or None)
            audit.record("web_token_rotate", actor=self._audit_actor(), result="ok",
                         summary=f"signed_out={dropped}")
            return self._json({"ok": True, "token": token, "fingerprint": auth.token_fingerprint(),
                               "signed_out_devices": dropped})
        if op == "web_token_revoke":
            # Only meaningful when a token exists; revoking also drops every session.
            removed = auth.revoke_token()
            auth.revoke_sessions()
            audit.record("web_token_revoke", actor=self._audit_actor(), result="ok",
                         summary=f"removed={removed}")
            return self._json({"ok": True, "removed": removed})
        if op == "terminal_token_list":
            return self._json({"ok": True, "tokens": terminal_tokens.list_tokens(),
                               "file_permissions_ok": terminal_tokens.file_permissions_ok()})
        if op == "terminal_token_issue":
            scope = "write" if payload.get("scope") == "write" else "read"
            ttl = payload.get("ttl")
            try:
                ttl = int(ttl) if ttl not in (None, "", 0) else None
            except (TypeError, ValueError):
                ttl = None
            token, rec = terminal_tokens.issue(
                payload.get("label") or "", scope=scope,
                hosts=payload.get("hosts"), ttl=ttl,
            )
            audit.record("web_terminal_token_issue", actor=self._audit_actor(), result="ok",
                         summary=audit.summarize("web_terminal_token_issue",
                                                 {"id": rec["id"], "scope": scope}, {}))
            return self._json({"ok": True, "token": token, "record": {
                "id": rec["id"], "label": rec["label"], "scopes": rec["scopes"],
                "hosts": rec["hosts"], "expires_at": rec["expires_at"]}})
        if op == "terminal_token_revoke":
            token_id = str(payload.get("id") or "")
            removed = terminal_tokens.revoke(token_id)
            audit.record("web_terminal_token_revoke", actor=self._audit_actor(),
                         result="ok" if removed else "error",
                         summary=audit.summarize("web_terminal_token_revoke",
                                                 {"id": token_id}, {}))
            return self._json({"ok": True, "revoked": removed})

    def _do_login(self) -> None:
        from ... import audit

        if not self._host_ok() or not self._origin_ok():
            return self._error("request not allowed", 403)
        ip = self._client_label()
        retry = auth.login_retry_after(ip)
        if retry > 0:
            audit.record("web_login", actor=self._audit_actor(), result="error",
                         summary="rate limited")
            return self._error("too many attempts; try again later", 429,
                               extra=[("Retry-After", str(int(retry) + 1))])
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error("bad json")
        if not auth.verify_token(payload.get("token")):
            # Never log the attempted token.
            auth.note_login_failure(ip)
            audit.record("web_login", actor=self._audit_actor(), result="error",
                         summary="invalid token")
            return self._error("invalid token", 401)
        auth.note_login_success(ip)
        session = auth.issue_session(
            ip=ip, user_agent=self.headers.get("User-Agent", ""),
        )
        audit.record("web_login", actor=self._audit_actor(), result="ok", summary="token")
        return self._json({"ok": True}, extra=[self._session_cookie(session)])

    def _session_cookie(self, session: str) -> tuple[str, str]:
        """A persistent session cookie (survives closing the browser)."""
        return ("Set-Cookie", auth.cookie_header(
            session, secure=self.security.secure_cookie, max_age=auth.session_ttl()))

    def _serve_login_page(self) -> None:
        body = _LOGIN_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _session_or_new(self) -> tuple[str | None, list[tuple[str, str]]]:
        """Existing session cookie, or a fresh one when auth is not required."""
        session = auth.session_value(self.headers)
        if session:
            return session, []
        if self.security.require_auth:
            return None, []
        session = auth.issue_session(ip=self._client_label(),
                                     user_agent=self.headers.get("User-Agent", ""))
        return session, [self._session_cookie(session)]
