"""Layer 3: the nightly smoke outcome JSON and its failure notification."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from crewhall import health, watch


class SmokeResult(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-smoke-res-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "smoke-2026-01-01.json")

    def test_result_written_0600_and_reports_failed_step(self):
        health.write_json(self.path, health.smoke_result(6, ["human-draft"]))
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertFalse(data["ok"])
        self.assertEqual(data["failed"], ["human-draft"])
        self.assertEqual(data["passed"], 5)
        self.assertEqual(data["total"], 6)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_ok_result(self):
        data = health.smoke_result(6, [])
        self.assertTrue(data["ok"])
        self.assertEqual(data["passed"], 6)

    def test_watch_notify_is_best_effort_without_notify_send(self):
        # No notify-send must never raise nor claim success.
        with mock.patch("crewhall.watch.shutil.which", return_value=None):
            self.assertFalse(watch.notify("t", "b"))


if __name__ == "__main__":
    unittest.main()
