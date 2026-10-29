from __future__ import annotations

import collections
import inspect
import threading
import time
from typing import Any

from .backend import Backend
from .types import Event, SessionInfo, SessionSpec, Status, new_session_id


class SessionTimeout(TimeoutError):
    def __init__(self, session_id: str, waited: float, text: str) -> None:
        super().__init__(f"session {session_id}: timed out after {waited:.2f}s")
        self.text = text


def _output_buffer_bytes() -> int:
    """Per-session cap for the ``read`` buffer (maintenance.session_output_buffer_kb)."""
    try:
        from . import settings

        return max(64 * 1024, int(settings.get("maintenance.session_output_buffer_kb")) * 1024)
    except Exception:  # noqa: BLE001 - a settings problem must not break sessions
        return 1024 * 1024


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
        # True when this object wraps a process/session that already existed
        # (adopted after a restart) rather than one this daemon started.
        self._adopted = False

        # ``read`` is the only consumer of this buffer (the TUI/Web use capture()).
        # It must be bounded: a long-running tmux agent can emit for days, and an
        # unbounded deque here was the daemon's largest memory growth path.
        self._buffer: collections.deque[str] = collections.deque()
        self._buffer_bytes = 0
        self._buffer_limit = _output_buffer_bytes()
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
        session._adopted = True
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
            limit = self._buffer_limit
            if len(text) >= limit:
                # One chunk (e.g. a full tmux scrollback) larger than the whole
                # cap: keep only its tail; this is what ``read`` can still use.
                self._buffer.clear()
                self._buffer.append(text[-limit:])
                self._buffer_bytes = limit
            else:
                self._buffer.append(text)
                self._buffer_bytes += len(text)
                while self._buffer_bytes > limit and len(self._buffer) > 1:
                    self._buffer_bytes -= len(self._buffer.popleft())
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

    def capture(self, escapes: bool = False) -> str:
        # Older/third-party backends may still expose capture() without the
        # optional ``escapes`` argument; only pass it when they accept it.
        fn = self.backend.capture
        try:
            if "escapes" in inspect.signature(fn).parameters:
                return fn(escapes=escapes)
        except (TypeError, ValueError):
            pass
        return fn()

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
            self._buffer_bytes = 0
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

    @property
    def buffered_bytes(self) -> int:
        """Bytes currently waiting for ``read`` (never above the configured cap)."""
        with self._lock:
            return self._buffer_bytes

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
                kind=getattr(self.spec, "kind", "session"),
                readonly=bool(getattr(self.spec, "readonly", False)),
                title=getattr(self.spec, "title", None),
                owner=getattr(self.spec, "owner", None),
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
