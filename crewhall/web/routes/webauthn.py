"""WebAuthn / passkey routes (Handler mixin)."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import hashlib
import json
import secrets
import time
from typing import Any

from .. import auth, webauthn
from ..shared import _session_key


class WebAuthnRoutes:
    def _webauthn_rp(self) -> tuple[str, str]:
        host = self.headers.get("Host", "")
        rp_id = webauthn.rp_id_from_host(host)
        origin = self.headers.get("Origin", "").rstrip("/")
        return rp_id, origin

    def _webauthn_store_challenge(self, kind: str, challenge: bytes, origin: str,
                                  rp_id: str) -> str:
        ceremony = secrets.token_urlsafe(24)
        now = time.time()
        with self.server.webauthn_lock:
            for key in [k for k, v in self.server.webauthn_challenges.items()
                        if v.get("expires", 0) < now]:
                self.server.webauthn_challenges.pop(key, None)
            self.server.webauthn_challenges[ceremony] = {
                "challenge": challenge, "origin": origin, "rp_id": rp_id, "kind": kind,
                "session": _session_key(auth.session_value(self.headers)),
                "expires": now + 300.0, "used": False,
            }
        return ceremony

    def _webauthn_take_challenge(self, ceremony: str, kind: str) -> dict[str, Any]:
        with self.server.webauthn_lock:
            rec = self.server.webauthn_challenges.get(ceremony)
            if not rec or rec.get("used") or rec.get("kind") != kind:
                raise ValueError("unknown or expired ceremony")
            if rec.get("expires", 0) < time.time():
                raise ValueError("expired ceremony")
            if rec.get("session") != _session_key(auth.session_value(self.headers)):
                raise ValueError("ceremony is bound to another session")
            rec["used"] = True
            return dict(rec)

    def _do_webauthn(self, action: str) -> None:
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
        rp_id, origin = self._webauthn_rp()
        try:
            if action == "register/begin":
                return self._webauthn_register_begin(rp_id, origin)
            if action == "register/finish":
                return self._webauthn_register_finish(payload, rp_id, origin)
            if action == "login/begin":
                return self._webauthn_login_begin(rp_id, origin)
            if action == "login/finish":
                return self._webauthn_login_finish(payload, rp_id, origin)
            if action == "credentials":
                return self._json({"ok": True, "credentials": webauthn.list_credentials(),
                                   "file_permissions_ok": webauthn.file_permissions_ok()})
            if action == "revoke":
                if not self._guard():
                    return
                removed = webauthn.revoke(payload.get("id"))
                audit.record("web_passkey_revoke", actor=self._audit_actor(),
                             result="ok" if removed else "error",
                             summary=f"id={payload.get('id')}")
                return self._json({"ok": True, "revoked": removed})
        except ValueError as exc:
            return self._error(str(exc), 400)
        return self._error("not found", 404)

    def _webauthn_register_begin(self, rp_id: str, origin: str) -> None:
        if not self._guard():
            return
        challenge = webauthn.new_challenge()
        ceremony = self._webauthn_store_challenge("register", challenge, origin, rp_id)
        user_id = webauthn.b64u_encode(hashlib.sha256(b"crewhall").digest()[:16])
        self._json({"ok": True, "ceremony": ceremony, "publicKey": {
            "challenge": webauthn.b64u_encode(challenge),
            "rp": {"id": rp_id, "name": "crewhall"},
            "user": {"id": user_id, "name": "crewhall", "displayName": "crewhall"},
            "pubKeyCredParams": [{"type": "public-key", "alg": -7},
                                 {"type": "public-key", "alg": -257}],
            "timeout": 60000, "attestation": "none",
            # A discoverable credential lets the device find the passkey on login
            # without falling back to the cross-device (QR) flow.
            "authenticatorSelection": {"residentKey": "required",
                                       "userVerification": "preferred"},
            "excludeCredentials": [{"type": "public-key", "id": cid}
                                   for cid in webauthn.credential_ids()],
        }})

    def _webauthn_register_finish(self, payload: dict[str, Any], rp_id: str, origin: str) -> None:
        from ... import audit

        if not self._guard():
            return
        rec = self._webauthn_take_challenge(payload.get("ceremony", ""), "register")
        result = webauthn.verify_registration(
            payload.get("clientDataJSON", ""), payload.get("attestationObject", ""),
            rec["challenge"], rec["origin"], rec["rp_id"])
        record = webauthn.add_credential(
            result["credential_id"], result["cose_bytes"], result["alg"],
            result["sign_count"], label=payload.get("label") or "passkey")
        audit.record("web_passkey_add", actor=self._audit_actor(), result="ok",
                     summary=f"id={record['id']}")
        return self._json({"ok": True, "credential": {
            "id": record["id"], "label": record["label"]}})

    def _webauthn_login_begin(self, rp_id: str, origin: str) -> None:
        challenge = webauthn.new_challenge()
        ceremony = self._webauthn_store_challenge("login", challenge, origin, rp_id)
        self._json({"ok": True, "ceremony": ceremony, "publicKey": {
            "challenge": webauthn.b64u_encode(challenge),
            "rpId": rp_id, "timeout": 60000, "userVerification": "preferred",
            # Tell the browser which credentials we know: a local one is used
            # directly (no cross-device QR); an empty list would force discovery.
            "allowCredentials": [{"type": "public-key", "id": cid}
                                 for cid in webauthn.credential_ids()],
        }})

    def _webauthn_login_finish(self, payload: dict[str, Any], rp_id: str, origin: str) -> None:
        from ... import audit

        rec = self._webauthn_take_challenge(payload.get("ceremony", ""), "login")
        cred_id = webauthn.b64u_decode(str(payload.get("id", "")))
        stored = webauthn.find(cred_id)
        if not stored:
            return self._error("unknown passkey", 401)
        key = webauthn.public_key(stored)
        new_count = webauthn.verify_assertion(
            payload.get("clientDataJSON", ""), payload.get("authenticatorData", ""),
            payload.get("signature", ""), rec["challenge"], rec["origin"], rec["rp_id"],
            key, int(stored.get("sign_count") or 0))
        webauthn.touch(cred_id, new_count)
        ip = self._client_label()
        session = auth.issue_session(ip=ip, user_agent=self.headers.get("User-Agent", ""))
        audit.record("web_login", actor=self._audit_actor(), result="ok", summary="passkey")
        return self._json({"ok": True}, extra=[self._session_cookie(session)])
