"""OnlyOffice Document Server integration: signed config, tokens and callbacks.

No third-party dependency: the JWT (HS256) is built with ``hmac``/``hashlib``
and the download token is an HMAC over the document identity. The three URLs
that matter are kept distinct (this is the classic OnlyOffice mistake):

- the browser loads the SDK from ``office.public_url`` (relaxed CSP);
- the Document Server downloads the file from ``document.url`` (this module's
  temporary download token, no session cookie);
- the Document Server posts saves to ``callbackUrl`` and we verify its JWT.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from . import settings

DOCUMENT_TYPES = {
    ".docx": "word", ".doc": "word", ".odt": "word", ".rtf": "word",
    ".txt": "word", ".md": "word",
    ".xlsx": "cell", ".xls": "cell", ".ods": "cell", ".csv": "cell",
    ".pptx": "slide", ".ppt": "slide", ".odp": "slide",
    ".pdf": "pdf",
}
VIEW_ONLY = {".pdf"}
FILE_TYPES = {
    ".docx": "docx", ".doc": "doc", ".odt": "odt", ".rtf": "rtf", ".txt": "txt",
    ".md": "txt", ".csv": "csv", ".xlsx": "xlsx", ".xls": "xls", ".ods": "ods",
    ".pptx": "pptx", ".ppt": "ppt", ".odp": "odp",
    ".pdf": "pdf",
}
DOWNLOAD_TTL = 3600.0


class OfficeError(RuntimeError):
    pass


def enabled() -> bool:
    try:
        return bool(settings.get("office.enabled"))
    except KeyError:
        return False


def collab_enabled() -> bool:
    try:
        return enabled() and bool(settings.get("office.collab_enabled"))
    except KeyError:
        return False


def configure() -> None:
    """Touch a setting so a bad key raises early (used by the CLI/doctor)."""
    settings.get("office.enabled")


def public_url() -> str:
    return str(settings.get("office.public_url") or "").rstrip("/")


def base_url(request_base: str = "") -> str:
    """Base URL the Document Server uses to reach crewhall."""
    configured = str(settings.get("office.base_url") or "").rstrip("/")
    if configured:
        return configured
    web = str(settings.get("web.public_url") or "").rstrip("/")
    if web:
        return web
    return request_base.rstrip("/")


def public_origin() -> str:
    from urllib.parse import urlparse

    parsed = urlparse(public_url())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _secret() -> bytes:
    return str(settings.get("office.jwt_secret") or "").encode("utf-8")


def jwt_enabled() -> bool:
    try:
        return bool(settings.get("office.jwt_enabled"))
    except KeyError:
        return True


def jwt_active() -> bool:
    """JWT signing is on and a secret is configured."""
    return jwt_enabled() and bool(_secret())


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def jwt_encode(payload: dict[str, Any], *, ttl: float | None = None) -> str:
    body = dict(payload)
    if ttl is not None:
        body["exp"] = int(time.time() + ttl)
    header = {"alg": "HS256", "typ": "JWT"}
    segments = (
        _b64e(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64e(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    )
    signature = hmac.new(_secret(), segments.encode("ascii"), hashlib.sha256).digest()
    return segments + "." + _b64e(signature)


def jwt_decode(token: str) -> dict[str, Any]:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError as exc:
        raise OfficeError("malformed token") from exc
    expected = hmac.new(
        _secret(),
        f"{header_b64}.{payload_b64}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        given = _b64d(sig_b64)
    except (ValueError, base64.binascii.Error) as exc:
        raise OfficeError("malformed signature") from exc
    if not hmac.compare_digest(expected, given):
        raise OfficeError("bad signature")
    try:
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, base64.binascii.Error) as exc:
        raise OfficeError("malformed payload") from exc
    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and time.time() > exp:
        raise OfficeError("token expired")
    return payload


def file_key(path: str) -> str:
    try:
        st = os.stat(path)
        raw = f"{os.path.realpath(path)}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        raw = os.path.realpath(path)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:20]  # noqa: S324 - cache key, not security


def document_type(name: str) -> tuple[str, str, bool]:
    """(documentType, fileType, view_only) for a filename."""
    ext = os.path.splitext(name)[1].lower()
    doc_type = DOCUMENT_TYPES.get(ext, "word")
    file_type = FILE_TYPES.get(ext, "txt")
    return doc_type, file_type, ext in VIEW_ONLY


def download_token(conversation_id: str, relpath: str, *, ttl: float = DOWNLOAD_TTL) -> str:
    return jwt_encode({"t": "dl", "id": conversation_id, "p": relpath}, ttl=ttl)


def verify_download_token(token: str, conversation_id: str, relpath: str) -> bool:
    try:
        payload = jwt_decode(token)
    except OfficeError:
        return False
    return payload.get("t") == "dl" and payload.get("id") == conversation_id \
        and payload.get("p") == relpath


def build_config(
    *,
    conversation_id: str,
    relpath: str,
    name: str,
    path: str,
    document_base: str,
    callback_base: str,
    user_id: str,
    user_name: str,
    can_edit: bool = True,
) -> dict[str, Any]:
    doc_type, file_type, view_only = document_type(name)
    effective_edit = can_edit and not view_only
    token = download_token(conversation_id, relpath)
    lang = str(settings.get("office.lang") or "es")
    config: dict[str, Any] = {
        "document": {
            "fileType": file_type,
            "key": file_key(path),
            "title": name,
            "url": f"{document_base}/api/conversation/artifact?id={conversation_id}"
                   f"&path={_q(relpath)}&dt={token}",
            "permissions": {
                "edit": effective_edit, "download": True, "print": True,
                "comment": True, "fillForms": effective_edit, "modifyFilter": effective_edit,
                "modifyContentControl": effective_edit,
            },
        },
        "documentType": doc_type,
        "editorConfig": {
            "callbackUrl": f"{callback_base}/api/office/callback?id={conversation_id}"
                           f"&path={_q(relpath)}",
            "lang": lang,
            "mode": "edit" if effective_edit else "view",
            "user": {"id": user_id, "name": user_name},
            "customization": {
                "autosave": True,
                # Enables the editor's forceSave API, so the agent's edits reach
                # the callback without waiting for the session to close.
                "forcesave": True,
                "logo": {"visible": False},
                # The co-authoring chat is the channel the user uses to talk to
                # the agent from inside the document ("AI Agent" is a co-editor).
                "chat": True,
                "comments": True,
                "feedback": {"visible": False},
            },
        },
    }
    if jwt_active():
        # OnlyOffice expects the token to cover the config it will receive.
        config["token"] = jwt_encode(config)
    return config


def callback_body_ok(body: dict[str, Any]) -> bool:
    """Verify the Document Server JWT on the callback (or allow when disabled)."""
    if not jwt_enabled() or not _secret():
        return True
    token = body.get("token")
    if isinstance(token, str) and token:
        try:
            jwt_decode(token)
            return True
        except OfficeError:
            return False
    return False


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
