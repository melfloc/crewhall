"""Layer 4: the soak-test invariants (pure; the 2 h runner is scripts/soak.py)."""
from __future__ import annotations

import unittest

from crewhall import soak


class Invariants(unittest.TestCase):
    def test_healthy_run_has_no_violations(self):
        violations = soak.check_invariants(
            sent=[{"recipient": "b", "body": "one", "seq": 0},
                  {"recipient": "b", "body": "two", "seq": 1}],
            delivered=[{"recipient": "b", "body": "one"},
                       {"recipient": "b", "body": "two"}],
            queue=[],
            drafts=[{"agent": "b", "expected": "draft", "actual": "draft"}],
            limits={"memory_kb": 1, "memory_max_kb": 2, "threads": 1,
                    "threads_max": 2, "log_bytes": 1, "log_max_bytes": 2},
            now=1000.0,
        )
        self.assertEqual(violations, [])

    def test_message_stuck_in_queue(self):
        violations = soak.check_invariants(
            queue=[{"recipient": "b", "body": "x", "enqueued_at": 0.0}],
            now=10000.0, queue_ttl=3600.0)
        self.assertEqual(len(violations), 1)
        self.assertIn("stuck", violations[0])

    def test_duplicate_delivery(self):
        violations = soak.check_invariants(
            delivered=[{"recipient": "b", "body": "same"},
                       {"recipient": "b", "body": "same"}])
        self.assertTrue(any("twice" in v for v in violations))

    def test_per_recipient_order(self):
        violations = soak.check_invariants(
            sent=[{"recipient": "b", "body": "one", "seq": 0},
                  {"recipient": "b", "body": "two", "seq": 1}],
            delivered=[{"recipient": "b", "body": "two"},
                       {"recipient": "b", "body": "one"}])
        self.assertTrue(any("out-of-order" in v for v in violations))

    def test_draft_altered(self):
        violations = soak.check_invariants(
            drafts=[{"agent": "b", "expected": "keep me", "actual": "(lost)"}])
        self.assertTrue(any("draft" in v for v in violations))

    def test_bounded_resources(self):
        violations = soak.check_invariants(limits={
            "memory_kb": 10, "memory_max_kb": 5,
            "threads": 2, "threads_max": 3,
            "log_bytes": 7, "log_max_bytes": 8,
        })
        self.assertEqual(len(violations), 1)
        self.assertIn("memory", violations[0])


if __name__ == "__main__":
    unittest.main()
