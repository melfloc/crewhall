from __future__ import annotations

import collections
import threading
import time
from typing import Any

from .backend import Backend
from .types import Event, SessionInfo, SessionSpec, Status, new_session_id


class SessionTimeout(TimeoutError):
    def __init__(self, session_id: str, waited: float, text: str) -> None:
        super().__init__(f"session {session_id}: timed out after {waited:.2f}s")
        self.text = text


class InteractiveSession:
    def __init__(
        self,
        backend: Backend,
        spec: SessionSpec,
        session_id: str | None = None,
    ) -> None:
        self.backend = backend
        backend.bind(self)
        self.spec = spec
        self.session_id = session_id or new_session_id()
        self.name = spec.name
        self.created_at = time.time()
        self.cols = spec.cols
        self.rows = spec.rows

        self._status = Status.STARTING
        self._exit_code: int | None = None
        self._exited_at: float | None = None
        self._last_input_at: float | None = None
        self._last_output_at: float | None = None

        self._buffer: collections.deque[str] = collections.deque()
        self._events: collections.deque[Event] = collections.deque(maxlen=2000)
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)

    @classmethod
    def adopt(
        cls,
        backend: Backend,
        spec: SessionSpec,
        session_id: str,
        created_at: float | None = None,
    ) -> InteractiveSession:
        session = cls(backend, spec, session_id=session_id)
        if created_at is not None:
            session.created_at = created_at
        backend.adopt(session_id)
        with session._lock:
            session._status = Status.RUNNING
        session._log("adopted", {"backend": backend.name})
        return session

    def start(self) -> InteractiveSession:
        with self._lock:
            self._status = Status.STARTING
        try:
            self.backend.start(self.spec)
        except BaseException:
            with self._lock:
                self._status = Status.ERROR
            raise
        with self._lock:
            self._status = Status.RUNNING
        self._log("started", {"backend": self.backend.name})
        return self

    def _emit_output(self, text: str) -> None:
        if not text:
            return
        now = time.time()
        with self._lock:
            self._buffer.append(text)
            self._last_output_at = now
            self._events.append(Event("output", self.session_id, now, {"size": len(text)}))
            self._cv.notify_all()

    def _emit_exit(self, code: int | None) -> None:
        with self._lock:
            if not self._status.alive and self._status is not Status.STARTING:
                return
            self._exit_code = code
            self._exited_at = time.time()
            self._status = Status.EXITED
            self._events.append(
                Event("exited", self.session_id, self._exited_at, {"code": code})
            )
            self._cv.notify_all()

    def _log(self, type_: str, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._events.append(
                Event(type_, self.session_id, time.time(), data or {})
            )

    @property
    def status(self) -> Status:
        with self._lock:
            return self._status

    @property
    def exit_code(self) -> int | None:
        with self._lock:
            return self._exit_code

    @property
    def pid(self) -> int | None:
        return self.backend.pid()

    def write(self, text: str) -> None:
        self.backend.write(text)
        with self._lock:
            self._last_input_at = time.time()
        self._log("input", {"kind": "write", "size": len(text)})

    def send_key(self, key: str) -> None:
        self.backend.send_key(key)
        with self._lock:
            self._last_input_at = time.time()
        self._log("input", {"kind": "key", "key": key})

    def send_enter(self) -> None:
        self.send_key("ENTER")

    def capture(self) -> str:
        return self.backend.capture()

    def read(self, timeout: float | None = None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while not self._buffer:
                if not self._status.alive:
                    return ""
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return ""
                self._cv.wait(remaining if remaining is not None else 0.5)
            out = "".join(self._buffer)
            self._buffer.clear()
        return out

    def read_until(
        self,
        pattern: str,
        timeout: float = 10.0,
        interval: float = 0.05,
    ) -> str:
        deadline = time.monotonic() + timeout
        out = ""
        while True:
            out = self.capture()
            if pattern in out:
                return out
            if not self.status.alive:
                return out
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionTimeout(self.session_id, timeout, out)
            with self._lock:
                self._cv.wait(min(interval, remaining))

    def resize(self, cols: int, rows: int) -> None:
        self.backend.resize(cols, rows)
        with self._lock:
            self.cols = cols
            self.rows = rows
        self._log("resized", {"cols": cols, "rows": rows})

    def interrupt(self) -> None:
        self.backend.interrupt()
        self._log("interrupted", {})

    def terminate(self) -> None:
        with self._lock:
            if not self._status.alive:
                return
            self._status = Status.TERMINATED
        self.backend.terminate()
        self._exited_at = time.time()
        self._log("terminated", {})

    def kill(self) -> None:
        with self._lock:
            if not self._status.alive:
                return
            self._status = Status.TERMINATED
        self.backend.kill()
        self._exited_at = time.time()
        self._log("killed", {})

    def poll(self) -> Status:
        code = self.backend.poll()
        if code is not None and self.status.alive:
            self._emit_exit(code)
        return self.status

    def wait(self, timeout: float | None = None) -> int | None:
        code = self.backend.wait(timeout)
        if code is not None:
            self._emit_exit(code)
        return code

    def wait_for_idle(self, quiet_ms: int = 800, timeout: float = 30.0) -> bool:
        quiet = quiet_ms / 1000.0
        deadline = time.monotonic() + timeout
        while True:
            if not self.status.alive:
                return True
            last = self._last_output_at
            if last is not None and (time.time() - last) >= quiet:
                return True
            if time.monotonic() >= deadline:
                return False
            with self._lock:
                self._cv.wait(0.1)

    def is_idle(self, quiet_ms: int = 800) -> bool:
        last = self._last_output_at
        if last is None:
            return False
        return (time.time() - last) >= (quiet_ms / 1000.0)

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._events]

    def info(self) -> SessionInfo:
        with self._lock:
            meta = self.backend.meta()
            return SessionInfo(
                session_id=self.session_id,
                backend=self.backend.name,
                pid=self.backend.pid(),
                command=self.spec.display(),
                cwd=self.spec.cwd or "",
                status=self._status,
                created_at=self.created_at,
                cols=self.cols,
                rows=self.rows,
                name=self.name,
                exit_code=self._exit_code,
                last_input_at=self._last_input_at,
                last_output_at=self._last_output_at,
                exited_at=self._exited_at,
                host=meta.get("host"),
                meta=meta,
            )

    def close(self) -> None:
        try:
            if self.status.alive:
                self.terminate()
        finally:
            self.backend.close()

    def __enter__(self) -> InteractiveSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<InteractiveSession {self.session_id} backend={self.backend.name} "
            f"status={self.status.value}>"
        )
