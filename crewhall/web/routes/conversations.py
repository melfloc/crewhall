"""Conversation files: uploads, artifacts, exports and config bundles (mixin)."""
from __future__ import annotations

# mypy: disable-error-code="attr-defined"
import json
import os
from typing import Any
from urllib.parse import unquote

from ...client import RpcError
from ..shared import MAX_BUNDLE_UPLOAD


class ConversationRoutes:
    def _bundle_download(self, name: str) -> None:
        from ... import bundle

        try:
            path = bundle.resolve_name(name)
            with open(path, "rb") as fh:
                data = fh.read()
        except (bundle.BundleError, OSError) as exc:
            return self._error(str(exc), 404)
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bundle_upload(self, query: dict[str, list[str]]) -> None:
        from ... import bundle

        if not self._guard():
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._error("bad Content-Length", 400)
        if length <= 0:
            return self._error("empty upload", 400)
        if length > MAX_BUNDLE_UPLOAD:
            self.close_connection = True
            return self._error("bundle too large", 413)
        data = self.rfile.read(length)
        try:
            name = bundle.store_upload((query.get("name") or [""])[0], data)
        except bundle.BundleError as exc:
            return self._error(f"not a valid bundle: {exc}", 400)
        return self._json({"ok": True, "name": name})

    def _upload(self, query: dict[str, list[str]]) -> None:
        """Accept a file from the Web UI and store it on the server (proxy).

        The body is the raw file (no multipart parsing); the original name
        travels in ``X-Filename`` (percent-encoded) and the optional storage
        mode in ``?mode=temp|permanent``. The stored path is what the UI hands
        to the agent, so the agent only ever reads a path.
        """
        from ... import audit, uploads

        if not self._guard():
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._error("bad Content-Length", 400)
        cap = uploads.max_bytes()
        if length <= 0:
            return self._error("empty upload", 400)
        if length > cap:
            self.close_connection = True
            return self._error(f"file too large (max {cap // (1024 * 1024)} MB)", 413)
        data = self.rfile.read(length)
        raw_name = self.headers.get("X-Filename", "")
        name = unquote(raw_name) if raw_name else (query.get("name") or ["file"])[0]
        storage = (query.get("mode") or [""])[0].lower() or None
        try:
            info = uploads.store(name, data, storage=storage)
        except uploads.UploadError as exc:
            return self._error(str(exc), 400)
        audit.record("web_upload", actor=self._audit_actor(), result="ok",
                     summary=f"mode={info['mode']} size={info['size']}")
        return self._json({"ok": True, **info})

    def _conversation_upload(self, query: dict[str, list[str]]) -> None:
        """Accept a file for a conversation and store it in its ``inputs/``."""
        from ... import audit
        from ... import chat as conversations

        if not self._guard():
            return
        cid = (query.get("id") or [""])[0]
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._error("bad Content-Length", 400)
        cap = conversations.max_bytes()
        if length <= 0:
            return self._error("empty upload", 400)
        if length > cap:
            self.close_connection = True
            return self._error(f"file too large (max {cap // (1024 * 1024)} MB)", 413)
        data = self.rfile.read(length)
        raw_name = self.headers.get("X-Filename", "")
        name = unquote(raw_name) if raw_name else (query.get("name") or ["file"])[0]
        try:
            info = conversations.store_input(cid, name, data)
        except conversations.ConversationError as exc:
            return self._error(str(exc), 400)
        audit.record("conversation_artifact_download", actor=self._audit_actor(), result="ok",
                     summary=f"id={cid} upload={info['name']} size={info['size']}")
        return self._json({"ok": True, **info})

    def _serve_artifact(self, query: dict[str, list[str]]) -> None:
        """Serve an artifact from a conversation (session or download token)."""
        import mimetypes

        from ... import audit, office
        from ... import chat as conversations

        cid = (query.get("id") or [""])[0]
        rel = (query.get("path") or [""])[0]
        token = (query.get("dt") or [""])[0]
        download = (query.get("download") or [""])[0].lower() in ("1", "true", "yes")
        token_ok = bool(token) and office.verify_download_token(token, cid, rel)
        if not token_ok and not self._guard():
            return
        try:
            path = conversations.resolve(cid, "outputs", rel)
        except conversations.ConversationError as exc:
            return self._error(str(exc), 404)
        if not os.path.isfile(path):
            return self._error("not found", 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        # Never render user HTML/SVG/XML inline on our own origin.
        if ctype.startswith(("text/html", "image/svg", "text/xml", "application/xml",
                             "application/xhtml")):
            download = True
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return self._error(str(exc), 500)
        name = os.path.basename(path).replace('"', "")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         ("attachment" if download else "inline") + f'; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(data)
        if not token_ok:
            audit.record("conversation_artifact_download", actor=self._audit_actor(),
                         result="ok", summary=f"id={cid} name={name}")

    def _conversation_export(self, query: dict[str, list[str]]) -> None:
        """Download a whole conversation as a ZIP (transcript + inputs + outputs).

        Everything stays server-side: the transcript comes from the daemon and the
        files are read straight from the conversation folders, so an exported chat
        is a single portable archive.
        """
        import io
        import zipfile

        from ... import audit
        from ... import chat as conversations

        cid = (query.get("id") or [""])[0]
        try:
            meta = conversations.info(cid)
        except conversations.ConversationError as exc:
            return self._error(str(exc), 404)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2, default=str))
            zf.writestr("transcript.md", self._conversation_transcript(meta))
            for section in ("inputs", "outputs"):
                for f in meta.get(section, []):
                    try:
                        path = conversations.resolve(cid, section, f["relpath"])
                        with open(path, "rb") as fh:
                            zf.writestr(f"{section}/{f['relpath']}", fh.read())
                    except (conversations.ConversationError, OSError):
                        continue
        data = buf.getvalue()
        audit.record("conversation_export", actor=self._audit_actor(), result="ok", summary=f"id={cid}")
        filename = f"chat-{(meta.get('title') or cid)}.zip".replace('"', "").replace("/", "-")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _conversation_transcript(self, meta: dict[str, Any]) -> str:
        """A readable Markdown transcript, best effort (never fails the export)."""
        lines = [f"# {meta.get('title') or 'Chat'}", "",
                 f"- id: {meta.get('id')}",
                 f"- agent: {meta.get('agent_id') or '(none)'}", ""]
        agent_id = meta.get("agent_id")
        if not agent_id:
            return "\n".join(lines)
        try:
            history = self.control.call("agent_history", target=agent_id, limit=1000)
        except RpcError as exc:
            lines.append(f"_(transcript unavailable: {exc})_")
            return "\n".join(lines)
        for m in history.get("messages") or []:
            role = "You" if m.get("role") == "user" else "Agent"
            text = m.get("text") or ""
            if not text and m.get("blocks"):
                text = "\n".join(b.get("text", "") for b in m["blocks"]
                                 if isinstance(b, dict) and b.get("type") == "text")
            if not text.strip():
                continue
            lines += [f"## {role}", "", text, ""]
        return "\n".join(lines)
