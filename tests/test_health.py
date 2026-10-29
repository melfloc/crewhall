"""Layer 5: the health journal and `doctor --trend` summarisation."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

from crewhall import health


class Journal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-health-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "journal.jsonl")
        self.now = time.time()

    def _sample(self, **overrides) -> dict:
        data = {"ts": self.now, "rt": "x", "memory_kb": 10, "threads": 2, "queue": 0,
                "failures_last_hour": 0, "workers_restarted": 0, "version": "1.0"}
        data.update(overrides)
        return data

    def test_append_read_summarize(self):
        health.append_sample(self._sample(ts=self.now - 100, memory_kb=10, threads=2),
                             path=self.path)
        health.append_sample(self._sample(ts=self.now - 10, memory_kb=20, threads=3,
                                          queue=5, failures_last_hour=1,
                                          workers_restarted=1), path=self.path)
        records = health.read_journal(30, path=self.path, now=self.now)
        self.assertEqual(len(records), 2)
        summary = health.summarize_trend(records)
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["memory_kb"]["max"], 20)
        self.assertEqual(summary["queue"]["last"], 5)
        self.assertEqual(summary["threads"]["min"], 2)
        self.assertEqual(summary["failures_last_hour"]["sum"], 1)
        self.assertEqual(summary["versions"], ["1.0"])

    def test_old_samples_are_filtered(self):
        health.append_sample(self._sample(ts=self.now - 40 * 86400), path=self.path)
        health.append_sample(self._sample(ts=self.now - 60), path=self.path)
        self.assertEqual(len(health.read_journal(30, path=self.path, now=self.now)), 1)
        self.assertEqual(len(health.read_journal(0, path=self.path, now=self.now)), 2)

    def test_summarize_empty(self):
        self.assertEqual(health.summarize_trend([]), {"samples": 0})

    def test_sample_without_controller_is_safe(self):
        sample = health.sample()
        self.assertIn("ts", sample)
        self.assertIn("version", sample)
        self.assertIn("children_rss_kb", sample)
        self.assertIn("office_editors", sample)
        self.assertTrue(sample["children_rss_kb"] is None
                        or sample["children_rss_kb"] >= 0)
        self.assertTrue(sample["office_editors"] is None
                        or sample["office_editors"] >= 0)

    def test_children_memory_is_summarised(self):
        records = [self._sample(children_rss_kb=100, office_editors=1),
                   self._sample(children_rss_kb=300, office_editors=2)]
        summary = health.summarize_trend(records)
        self.assertEqual(summary["children_rss_kb"]["max"], 300)
        self.assertEqual(summary["office_editors"]["max"], 2)


class FailuresFromAudit(unittest.TestCase):
    def test_counts_recent_errors_only(self):
        tmp = tempfile.mkdtemp(prefix="at-audit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "audit.jsonl")
        now = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now - 10, "result": "error"}) + "\n")
            fh.write(json.dumps({"ts": now - 20, "result": "ok"}) + "\n")
            fh.write(json.dumps({"ts": now - 7200, "result": "error"}) + "\n")
            fh.write("not json\n")
        sample = health.sample(audit_path=path, now=now)
        self.assertEqual(sample["failures_last_hour"], 1)


if __name__ == "__main__":
    unittest.main()
