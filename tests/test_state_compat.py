"""Layer 6: every released state/queue/history fixture stays loadable."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from crewhall import Controller
from crewhall.persistence import HistoryStore, PendingQueueStore, StateStore

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _named(prefix: str) -> list[str]:
    return sorted(name for name in os.listdir(FIXTURES) if name.startswith(prefix))


class StateFixtures(unittest.TestCase):
    def test_all_state_fixtures_restore(self):
        names = _named("state-")
        self.assertTrue(names)
        for name in names:
            tmp = tempfile.mkdtemp(prefix="at-fx-")
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            shutil.copy(os.path.join(FIXTURES, name), os.path.join(tmp, "state.json"))
            controller = Controller(adopt=False, persist=False)
            controller._store = StateStore(os.path.join(tmp, "state.json"))
            controller.restore()
            agents = {a["name"] for a in controller.list_agents()}
            self.assertTrue({"orquestador", "ejecutor"} <= agents, name)

    def test_all_queue_fixtures_load(self):
        names = _named("queue-")
        self.assertTrue(names)
        for name in names:
            items = PendingQueueStore(os.path.join(FIXTURES, name)).load()
            self.assertTrue(items, name)
            self.assertTrue(all(isinstance(item, dict) for item in items), name)

    def test_all_history_fixtures_load(self):
        names = _named("history-")
        self.assertTrue(names)
        for name in names:
            store = HistoryStore(os.path.join(FIXTURES, name), max_age_days=None)
            records = store.load()
            self.assertTrue(records, name)
            from crewhall.messaging import delivery_from_dict

            for record in records:
                self.assertIsNotNone(delivery_from_dict(record), name)


if __name__ == "__main__":
    unittest.main()
