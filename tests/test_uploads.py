from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from crewhall import settings, uploads


class UploadStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-up-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.d, "XDG_STATE_HOME": self.d})
        env.start(); self.addCleanup(env.stop)

    def test_clean_name_strips_directories_and_control_characters(self):
        self.assertEqual(uploads._clean_name("../../etc/passwd"), "passwd")
        self.assertEqual(uploads._clean_name("a\\b\\c.txt"), "c.txt")
        self.assertEqual(uploads._clean_name(".."), "file")
        self.assertEqual(uploads._clean_name("x\x00\x1fy.png"), "xy.png")
        self.assertTrue(uploads._clean_name("q" * 500 + ".txt").endswith(".txt"))
        self.assertLessEqual(len(uploads._clean_name("q" * 500 + ".txt")), uploads.MAX_NAME + 5)

    def test_store_temp_writes_private_unique_file(self):
        info = uploads.store("my report.txt", b"data")
        self.assertTrue(os.path.isabs(info["path"]))
        self.assertEqual(info["mode"], "temp")
        self.assertTrue(info["name"].endswith(".txt"))
        self.assertEqual(stat.S_IMODE(os.stat(info["path"]).st_mode), 0o600)
        with open(info["path"], "rb") as fh:
            self.assertEqual(fh.read(), b"data")
        # A second upload with the same name does not clobber the first.
        second = uploads.store("my report.txt", b"other")
        self.assertNotEqual(info["path"], second["path"])
        os.unlink(info["path"]); os.unlink(second["path"])

    def test_store_permanent_uses_the_configured_directory(self):
        target = os.path.join(self.d, "permanent")
        settings.patch({"uploads.mode": "permanent", "uploads.dir": target})
        info = uploads.store("pic.png", b"\x89PNG")
        self.assertTrue(info["path"].startswith(target + os.sep))
        self.assertEqual(info["mode"], "permanent")
        os.unlink(info["path"])

    def test_max_bytes_follows_the_setting(self):
        settings.patch({"uploads.max_mb": 3})
        self.assertEqual(uploads.max_bytes(), 3 * 1024 * 1024)

    def test_cleanup_prunes_old_temp_uploads(self):
        info = uploads.store("old.bin", b"x")
        old = os.path.getmtime(info["path"]) - 10 * 86400
        os.utime(info["path"], (old, old))
        self.assertGreaterEqual(uploads.cleanup(), 1)
        self.assertFalse(os.path.exists(info["path"]))


class CleanName(unittest.TestCase):
    def test_extension_is_only_kept_when_it_looks_like_one(self):
        self.assertEqual(uploads._clean_name("archive.tar.gz"), "archive.tar.gz")
        self.assertEqual(uploads._clean_name("noext"), "noext")
        self.assertEqual(uploads._clean_name("weird.this_is_a_very_long_extension"),
                         "weird.this_is_a_very_long_extension")
