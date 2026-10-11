from __future__ import annotations

import threading
import time
import types
import unittest

from crewhall.daemon import Server
from crewhall.messaging import Messaging
from crewhall.workers import WorkerRegistry


class WorkerSupervision(unittest.TestCase):
    """Fase B3: a crashed worker is recorded and relaunched."""

    def test_crashed_worker_is_recorded_and_relaunched(self) -> None:
        registry = WorkerRegistry()
        calls = {"n": 0}

        def flaky() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            time.sleep(30)  # stay alive once restarted

        self.addCleanup(registry.stop_all)
        registry.spawn("flaky", flaky, restart=True)

        deadline = time.time() + 5
        while time.time() < deadline:
            worker = registry.get("flaky")
            if worker and worker.restarts >= 1 and worker.state == "running" and calls["n"] >= 2:
                break
            time.sleep(0.05)

        worker = registry.get("flaky")
        self.assertIsNotNone(worker)
        self.assertGreaterEqual(worker.restarts, 1)
        self.assertEqual(worker.state, "running")
        self.assertIn("boom", worker.last_error or "")
        names = {row["name"] for row in registry.snapshot()}
        self.assertIn("flaky", names)

    def test_heartbeat_is_recorded(self) -> None:
        registry = WorkerRegistry()
        stop = threading.Event()
        self.addCleanup(registry.stop_all)

        def loop() -> None:
            while not stop.is_set():
                registry.beat("hb")
                time.sleep(0.01)

        registry.spawn("hb", loop)
        time.sleep(0.2)
        worker = registry.get("hb")
        self.assertIsNotNone(worker)
        self.assertIsNotNone(worker.last_heartbeat)
        stop.set()

    def test_spawn_does_not_duplicate_a_live_worker(self) -> None:
        registry = WorkerRegistry()
        stop = threading.Event()
        self.addCleanup(registry.stop_all)
        registry.spawn("one", lambda: stop.wait(30))
        first = registry.get("one")
        registry.spawn("one", lambda: stop.wait(30))
        self.assertIs(registry.get("one"), first)


class HealthOp(unittest.TestCase):
    def test_health_reports_workers_and_queues(self) -> None:
        registry = WorkerRegistry()
        registry.spawn("looper", lambda: time.sleep(30))
        self.addCleanup(registry.stop_all)

        messaging = Messaging(types.SimpleNamespace(resolve=lambda _t: None, all=list))
        fake = types.SimpleNamespace(controller=types.SimpleNamespace(
            workers=registry, messaging=messaging))
        out = Server._op_health(fake, {})
        self.assertIn("workers", out)
        self.assertIn("queues", out)
        self.assertIn("looper", {w["name"] for w in out["workers"]})


if __name__ == "__main__":
    unittest.main()
