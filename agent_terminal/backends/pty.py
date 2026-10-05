from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import TYPE_CHECKING

from ..backend import Backend
from ..keys import pty_bytes

if TYPE_CHECKING:
    from ..types import SessionSpec


class PtyBackend(Backend):
    name = "pty"

    def __init__(self) -> None:
        super().__init__()
        self.master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._exit_code: int | None = None
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._acc: list[str] = []
        self._acc_len = 0
        self._acc_max = 2_000_000

    def start(self, spec: SessionSpec) -> None:
        argv = spec.argv()
        cwd = spec.cwd or os.getcwd()
        env = dict(os.environ)
        if spec.env:
            env.update(spec.env)

        master, slave = pty.openpty()
        self._set_winsize_fd(master, spec.cols, spec.rows)

        def _preexec() -> None:
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        try:
            proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env=env,
                close_fds=True,
                start_new_session=True,
                preexec_fn=_preexec,
            )
        finally:
            os.close(slave)

        self._proc = proc
        self.master = master
        os.set_blocking(master, False)
        self._reader = threading.Thread(
            target=self._read_loop, name=f"pty-reader-{proc.pid}", daemon=True
        )
        self._reader.start()

    def _set_winsize_fd(self, fd: int, cols: int, rows: int) -> None:
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)

    def _read_loop(self) -> None:
        master = self.master
        if master is None:
            return
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([master], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not ready:
                if self._poll_wait() is not None:
                    break
                continue
            try:
                data = os.read(master, 65536)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                break
            if not data:
                break
            text = data.decode("utf-8", "replace")
            self._acc.append(text)
            self._acc_len += len(text)
            while self._acc_len > self._acc_max and len(self._acc) > 1:
                self._acc_len -= len(self._acc.pop(0))
            self.session._emit_output(text)

        code = self._poll_wait()
        if code is None:
            code = self._wait_blocking(timeout=2.0 if self._stop.is_set() else 5.0)
        try:
            os.close(master)
        except OSError:
            pass
        if self.master is master:
            self.master = None
        if self.session is not None:
            self.session._emit_exit(code)

    def _poll_wait(self) -> int | None:
        if self._exit_code is not None:
            return self._exit_code
        if self._proc is None:
            return None
        code = self._proc.poll()
        if code is not None:
            self._exit_code = code
        return code

    def _wait_blocking(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while True:
            code = self._poll_wait()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def _write_bytes(self, data: bytes) -> None:
        master = self.master
        if master is None:
            raise RuntimeError("session is not running")
        view = memoryview(data)
        deadline = time.monotonic() + 5.0
        while view:
            try:
                written = os.write(master, view)
                view = view[written:]
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("pty write timed out") from None
                    select.select([], [master], [], 0.1)
                    continue
                raise

    def write(self, text: str) -> None:
        self._write_bytes(text.encode("utf-8"))

    def send_key(self, key: str) -> None:
        self._write_bytes(pty_bytes(key))

    def capture(self) -> str:
        return "".join(self._acc)

    def resize(self, cols: int, rows: int) -> None:
        if self.master is not None:
            self._set_winsize_fd(self.master, cols, rows)

    def _signal(self, sig: int) -> None:
        if self._proc is None:
            return
        try:
            os.killpg(self._proc.pid, sig)
        except ProcessLookupError:
            pass

    def interrupt(self) -> None:
        self._signal(signal.SIGINT)

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def poll(self) -> int | None:
        return self._poll_wait()

    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def meta(self) -> dict:
        return {"pty": True, "pid": self.pid()}

    def close(self) -> None:
        self._stop.set()
        if self.session is not None:
            self.session._emit_exit(self._poll_wait())
