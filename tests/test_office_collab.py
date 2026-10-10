from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import office_collab, settings


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-collab-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.d,
            "XDG_STATE_HOME": self.d,
        })
        env.start(); self.addCleanup(env.stop)


class Bootstrap(_Base):
    def test_contains_api_and_config(self):
        html = office_collab.build_bootstrap(
            "https://ds.example/web-apps/apps/api/documents/api.js",
            {"document": {"key": "k"}, "token": "t"})
        self.assertIn("https://ds.example/web-apps/apps/api/documents/api.js", html)
        self.assertIn('"key"', html)
        self.assertIn("new DocsAPI.DocEditor", html)
        self.assertIn("AI Agent", html)
        # The config must not carry function-valued events (it breaks the editor).
        self.assertNotIn("onDocumentReady", html)
        self.assertNotIn("createConnector", html)

    def test_chromium_binary_configured(self):
        exe = os.path.join(self.d, "chromium")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        settings.patch({"office.chromium": exe})
        self.assertEqual(office_collab.chromium_binary(), exe)

    def test_chromium_binary_absent(self):
        settings.patch({"office.chromium": ""})
        with mock.patch("crewhall.office_collab.shutil.which", return_value=None):
            self.assertIsNone(office_collab.chromium_binary())


class Registry(_Base):
    def test_open_without_chromium_fails_clearly(self):
        reg = office_collab.CollabRegistry()
        with mock.patch("crewhall.office_collab.chromium_binary", return_value=None):
            with self.assertRaises(office_collab.CollabError):
                reg.open("c-1", "doc.docx", {"document": {"key": "k"}}, "http://ds")

    def test_command_on_unknown_document(self):
        reg = office_collab.CollabRegistry()
        with self.assertRaises(office_collab.CollabError):
            reg.command("c-1", "nope.docx", "Api.GetDocument().GetAllText", [])
        self.assertFalse(reg.close("c-1", "nope.docx"))
        self.assertEqual(reg.list(), [])

    def test_poll_forward_delivers_and_skips_self(self):
        reg = office_collab.CollabRegistry()

        class FakeEditor:
            def __init__(self):
                self.msgs = []
                self.cmts = []

            def chat_messages(self):
                return self.msgs

            def comments(self):
                return self.cmts

        editor = FakeEditor()
        reg._editors[reg.key("c-1", "doc.txt")] = editor
        got = []
        reg.set_forward(lambda cid, path, user, kind, text: got.append((cid, path, user, kind, text)))
        editor.msgs = [{"UserName": "you", "Text": "hola"},
                       {"UserName": "AI Agent", "Text": "eco"}]
        editor.cmts = [{"Id": "4_1", "Data": {"UserName": "you", "Text": "cambia esto",
                                              "QuoteText": "parrafo 2"}},
                       {"Id": "4_2", "Data": {"UserName": "AI Agent", "Text": "eco"}}]
        # chat + comment delivered; both self-authored skipped
        self.assertEqual(reg.poll_forward(), 2)
        self.assertEqual(got[0], ("c-1", "doc.txt", "you", "chat", "hola"))
        self.assertEqual(got[1], ("c-1", "doc.txt", "you", "comment",
                                  'sobre "parrafo 2": cambia esto'))
        self.assertEqual(reg.poll_forward(), 0)   # nothing new
        editor.msgs.append({"UserName": "you", "Text": "otra"})
        editor.cmts.append({"Id": "4_3", "Data": {"UserName": "you", "Text": "y esto"}})
        self.assertEqual(reg.poll_forward(), 2)
        self.assertEqual(got[-1][3], "comment")


if __name__ == "__main__":
    unittest.main()
