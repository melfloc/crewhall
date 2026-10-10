from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from crewhall import conversations, office, settings
from crewhall.web.server import WebServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ArtifactRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._prev = {k: os.environ.get(k) for k in
                     ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        cls.tmp = tempfile.mkdtemp(prefix="at-convweb-")
        os.environ["XDG_RUNTIME_DIR"] = cls.tmp
        os.environ["XDG_CONFIG_HOME"] = cls.tmp
        os.environ["XDG_STATE_HOME"] = cls.tmp
        settings.patch({"office.enabled": True, "office.jwt_secret": "s3cr3t",
                        "office.jwt_enabled": True})
        cls.port = _free_port()
        cls.server = WebServer(host="127.0.0.1", port=cls.port)
        cls.server.start()
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server.stop()
        except Exception:
            pass
        try:
            from crewhall.client import Client

            Client(autostart=False).call("shutdown")
        except Exception:
            pass
        for k, prev in cls._prev.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_serve_and_download_with_token(self):
        cid = conversations.create("chat")["id"]
        conversations.write_artifact(cid, "hello.txt", b"bonjour")

        # Download token (what the Document Server would use).
        dt = office.download_token(cid, "hello.txt")
        with urllib.request.urlopen(self._url(
                f"/api/conversation/artifact?id={cid}&path=hello.txt&dt={dt}")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"bonjour")

        # A wrong token and no session still works locally (auth off), but a
        # traversal path is refused regardless.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url(
                f"/api/conversation/artifact?id={cid}&path=../meta.json"))
        self.assertEqual(ctx.exception.code, 404)

    def test_office_config_for_artifact(self):
        cid = conversations.create("chat")["id"]
        conversations.write_artifact(cid, "doc.docx", b"x")
        with urllib.request.urlopen(self._url(
                f"/api/office/config?id={cid}&path=doc.docx")) as resp:
            import json

            body = json.loads(resp.read())
        self.assertTrue(body["ok"])
        cfg = body["config"]
        self.assertEqual(cfg["documentType"], "word")
        self.assertIn("/api/office/callback", cfg["editorConfig"]["callbackUrl"])
        self.assertTrue(cfg["document"]["url"].startswith("http://127.0.0.1"))

    def test_conversation_upload_route(self):
        cid = conversations.create("chat")["id"]
        req = urllib.request.Request(
            self._url(f"/api/conversation/upload?id={cid}"), data=b"data",
            method="POST", headers={"X-Filename": "note.txt"})
        with urllib.request.urlopen(req) as resp:
            import json

            body = json.loads(resp.read())
        self.assertTrue(body["ok"])
        self.assertTrue(body["path"].endswith(".txt"))
        self.assertEqual(len(conversations.list_files(cid, "inputs")), 1)

    def test_conversation_export_is_a_zip_with_transcript_and_files(self):
        import io
        import zipfile

        cid = conversations.create("export me")["id"]
        conversations.write_artifact(cid, "out.txt", b"hello")
        info = conversations.store_input(cid, "in.txt", b"world")
        with urllib.request.urlopen(self._url(f"/api/conversation/export?id={cid}")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get_content_type(), "application/zip")
            self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
            data = resp.read()
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        self.assertIn("meta.json", names)
        self.assertIn("transcript.md", names)
        self.assertIn("outputs/out.txt", names)
        self.assertIn(f"inputs/{info['name']}", names)
        self.assertEqual(zf.read("outputs/out.txt"), b"hello")

    def test_conversation_export_unknown_id_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url("/api/conversation/export?id=c-nope"))
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
