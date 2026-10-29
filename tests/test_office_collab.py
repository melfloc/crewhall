from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from crewhall import settings
from crewhall.office import collab as office_collab


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
        with mock.patch("crewhall.office.collab.shutil.which", return_value=None):
            self.assertIsNone(office_collab.chromium_binary())


class ChromiumFlags(_Base):
    def test_editor_iframe_is_kept_in_process(self):
        # Regression: with the Document Server on another host (Tailscale name)
        # the editor iframe is cross-site; site isolation moved it out of the
        # page we drive and the co-editor never became ready.
        captured = []

        def popen(args, *a, **k):
            captured.append(list(args))
            raise RuntimeError("stop after launch")

        with mock.patch("crewhall.office.collab.chromium_binary", return_value="/bin/true"), \
                mock.patch("crewhall.office.collab.subprocess.Popen", side_effect=popen):
            with self.assertRaises(RuntimeError):
                office_collab.CoEditor({"document": {"key": "k"}}, "http://ds.example:8081")
        args = captured[0]
        for flag in office_collab.SAME_PROCESS_FLAGS:
            self.assertIn(flag, args)
        self.assertTrue(args[-1].startswith("http://127.0.0.1:"))  # the page stays last


class Registry(_Base):
    def test_open_without_chromium_fails_clearly(self):
        reg = office_collab.CollabRegistry()
        with mock.patch("crewhall.office.collab_enabled", return_value=True), \
                mock.patch("crewhall.office.collab.chromium_binary", return_value=None):
            with self.assertRaises(office_collab.CollabError):
                reg.open("c-1", "doc.docx", {"document": {"key": "k"}}, "http://ds")

    def test_command_on_unknown_document(self):
        reg = office_collab.CollabRegistry()
        with self.assertRaises(office_collab.CollabError):
            reg.command("c-1", "nope.docx", "Api.GetDocument().GetAllText", [])
        self.assertFalse(reg.close("c-1", "nope.docx"))
        self.assertEqual(reg.list(), [])

    def test_open_requires_live_collab_enabled(self):
        reg = office_collab.CollabRegistry()
        with mock.patch("crewhall.office.collab_enabled", return_value=False):
            with self.assertRaises(office_collab.CollabError):
                reg.open("c-1", "doc.docx", {"document": {"key": "k"}}, "http://ds")

    def test_max_editors_evicts_the_least_recently_used(self):
        class FakeEditor:
            def __init__(self, config, public_url):
                self.pid = None
                self.last_used = time.time()
                self.closed = False

            def close(self):
                self.closed = True

            def touch(self):
                self.last_used = time.time()

            def chat_messages(self):
                return []

            def comments(self):
                return []

        reg = office_collab.CollabRegistry()
        values = {"office.collab_max_editors": 1, "office.collab_idle_s": 600}

        def fake_get(key, default=None):
            return values.get(key, default)

        with mock.patch("crewhall.office.collab_enabled", return_value=True), \
                mock.patch("crewhall.office.collab.settings.get", side_effect=fake_get), \
                mock.patch("crewhall.office.collab.CoEditor", FakeEditor):
            reg.open("c-1", "a.docx", {}, "http://ds")
            first = reg.get("c-1", "a.docx")
            first.last_used -= 60
            reg.open("c-2", "b.docx", {}, "http://ds")
        self.assertTrue(first.closed)
        self.assertEqual([e["conversation"] for e in reg.list()], ["c-2"])

    def test_idle_editors_are_closed(self):
        class FakeEditor:
            last_used = time.time() - 1000
            pid = None

            def chat_messages(self):
                return []

            def comments(self):
                return []

            def close(self):
                self.closed = True

        editor = FakeEditor()
        reg = office_collab.CollabRegistry()
        reg._editors[reg.key("c-1", "doc.txt")] = editor
        with mock.patch("crewhall.office.collab.settings.get",
                        side_effect=lambda k, d=None: 60 if k == "office.collab_idle_s" else d):
            self.assertEqual(reg.poll_forward(), 0)
        self.assertTrue(editor.closed)
        self.assertEqual(reg.count(), 0)

    def test_list_reports_pid_memory_and_idle(self):
        class FakeEditor:
            pid = 123
            last_used = time.time() - 5

            def rss_kb(self):
                return 4567

        reg = office_collab.CollabRegistry()
        reg._editors[reg.key("c-1", "doc.txt")] = FakeEditor()
        row = reg.list()[0]
        self.assertEqual(row["pid"], 123)
        self.assertEqual(row["memory_kb"], 4567)
        self.assertGreaterEqual(row["idle_s"], 5)

    def test_dead_editor_is_reclaimed_on_poll(self):
        class FakeEditor:
            alive = False

            def close(self):
                self.closed = True

        editor = FakeEditor()
        reg = office_collab.CollabRegistry()
        reg._editors[reg.key("c-1", "doc.txt")] = editor
        self.assertEqual(reg.poll_forward(), 0)
        self.assertTrue(editor.closed)
        self.assertEqual(reg.count(), 0)

    def test_close_conversation_only_closes_its_editors(self):
        class FakeEditor:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        reg = office_collab.CollabRegistry()
        one, two, other = FakeEditor(), FakeEditor(), FakeEditor()
        reg._editors[reg.key("c-1", "a.docx")] = one
        reg._editors[reg.key("c-1", "b.docx")] = two
        reg._editors[reg.key("c-2", "c.docx")] = other
        self.assertEqual(reg.close_conversation("c-1"), 2)
        self.assertTrue(one.closed and two.closed)
        self.assertFalse(other.closed)
        self.assertEqual(reg.count(), 1)

    def test_sweep_orphans_terminates_and_clears_profiles(self):
        profile = os.path.join(tempfile.gettempdir(), "crewhall-collab-test")
        with mock.patch("crewhall.office.collab._load_editor_records", return_value=[]), \
                mock.patch("crewhall.office.collab._profile_dirs", return_value=[profile]), \
                mock.patch("crewhall.office.collab._allowed_profile", return_value=True), \
                mock.patch("crewhall.office.collab._pids_for_profile",
                           side_effect=[[123], []]), \
                mock.patch("crewhall.office.collab._terminate_group") as terminate, \
                mock.patch("crewhall.office.collab._save_editor_records") as save:
            self.assertEqual(office_collab.sweep_orphans(), 1)
        terminate.assert_called_once_with(123)
        save.assert_called_once_with([])

    def test_sweep_orphans_kills_a_real_process_group(self):
        profile = tempfile.mkdtemp(prefix="crewhall-collab-", dir=tempfile.gettempdir())
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)",
             f"--user-data-dir={profile}"],
            start_new_session=True,
        )
        try:
            with mock.patch("crewhall.office.collab._load_editor_records",
                            return_value=[]), \
                    mock.patch("crewhall.office.collab._profile_dirs",
                               return_value=[profile]):
                self.assertGreaterEqual(office_collab.sweep_orphans(), 1)
            time.sleep(0.1)
            self.assertIsNotNone(proc.poll())
            self.assertFalse(os.path.exists(profile))
        finally:
            if proc.poll() is None:
                proc.kill()
            shutil.rmtree(profile, ignore_errors=True)

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
