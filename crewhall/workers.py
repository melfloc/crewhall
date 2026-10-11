"""Supervised background workers.

The daemon launches a growing number of threads (message-queue drains,
acknowledgement watchers, the tmpdir/terminals janitors, the OnlyOffice
poller, the SSH adopter…). Launched bare, a thread that dies on an
unexpected exception disappears silently and its feature just stops.

``WorkerRegistry`` records every worker (name, state, last heartbeat, error,
restarts) and can relaunch one that died from an exception. The snapshot is
exposed through `crewhall doctor` and the `health` op.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("crewhall.workers")


@dataclass
class Worker:
    name: str
    target: Callable[..., Any]
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    daemon: bool = True
    restart: bool = True
    state: str = "pending"  # pending | running | restarting | stopped | dead
    thread: threading.Thread | None = None
    started_at: float | None = None
    last_heartbeat: float | None = None
    restarts: int = 0
    last_error: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)

    def heartbeat(self) -> None:
        self.last_heartbeat = time.time()

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        beat = self.last_heartbeat
        return {
            "name": self.name,
            "state": self.state,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "last_heartbeat_age_s": None if beat is None else round(now - beat, 3),
            "alive": bool(self.thread is not None and self.thread.is_alive()),
        }


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}
        self._lock = threading.RLock()
        self._closed = False

    def spawn(
        self,
        name: str,
        target: Callable[..., Any],
        *,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        daemon: bool = True,
        restart: bool = True,
    ) -> Worker:
        with self._lock:
            existing = self._workers.get(name)
            if existing is not None and existing.thread is not None and existing.thread.is_alive():
                return existing  # already supervised: never double-spawn
            worker = Worker(
                name=name, target=target, args=tuple(args or ()),
                kwargs=dict(kwargs or {}), daemon=daemon, restart=restart,
            )
            self._workers[name] = worker
            thread = threading.Thread(
                target=self._supervise, args=(worker,), name=f"worker-{name}", daemon=daemon,
            )
            worker.thread = thread
            thread.start()
            return worker

    def _supervise(self, worker: Worker) -> None:
        while True:
            worker.state = "running"
            worker.started_at = time.time()
            worker.heartbeat()
            try:
                worker.target(*worker.args, **worker.kwargs)
            except Exception as exc:  # noqa: BLE001 - this is the supervisor
                worker.last_error = f"{type(exc).__name__}: {exc}"
                worker.state = "restarting"
                log.exception("worker %s died", worker.name)
                if not worker.restart or worker.stop_event.is_set() or self._closed:
                    worker.state = "dead"
                    return
                worker.restarts += 1
                worker.stop_event.wait(min(worker.restarts, 5.0))
                continue
            # A normal return means the worker finished on purpose: drop it from
            # the registry so per-message workers do not accumulate.
            with self._lock:
                if self._workers.get(worker.name) is worker:
                    self._workers.pop(worker.name, None)
            return

    def beat(self, name: str) -> None:
        with self._lock:
            worker = self._workers.get(name)
        if worker is not None:
            worker.heartbeat()

    def get(self, name: str) -> Worker | None:
        with self._lock:
            return self._workers.get(name)

    def remove(self, name: str) -> None:
        with self._lock:
            self._workers.pop(name, None)

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            workers = list(self._workers.values())
        return [w.to_dict(now) for w in workers if w.state != "stopped"]

    def stop_all(self, timeout: float = 0.2) -> None:
        """Signal every worker to stop; joining is best-effort (all are daemons)."""
        self._closed = True
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.stop_event.set()
        deadline = time.monotonic() + timeout
        for worker in workers:
            thread = worker.thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
