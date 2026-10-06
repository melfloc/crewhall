"""Output streaming for web terminals (Phase 2).

One ``TerminalHub`` per terminal owns a single output source shared by every
connected client:

* local: a FIFO fed by ``tmux pipe-pane`` (reader opens the FIFO before the
  pipe-pane is launched);
* remote: a long-lived ``ssh`` process running a small shell script that
  creates its own FIFO on the host and ``cat``s it back over the connection,
  using a dedicated ``ControlPath`` so it never exhausts sshd ``MaxSessions``;
* fallback: ``capture-pane`` polling every 300 ms (``stream="poll"``).

The reader thread never blocks on a client: each client has a bounded byte
queue and a token bucket; a client that overflows either is dropped alone with
close code 1013, while the others and the terminal keep working.
"""
from __future__ import annotations

import collections
import os
import shlex
import stat
import subprocess
import threading
import time

from .web import ws as wsmod

CHUNK = 65536
POLL_INTERVAL = 0.3
INPUT_WINDOW = 0.010  # coalesce keystrokes for at most 10 ms
MAX_HEX_CHUNK = 512


def terminals_dir() -> str:
    """0700 private directory that owns the stream FIFOs (verified, no symlinks)."""
    from . import paths

    path = os.path.join(paths.runtime_dir(), "terminals")
    if os.path.islink(path):
        raise RuntimeError(f"refusing symlinked terminals directory: {path}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"not a directory: {path}")
    if st.st_uid != os.getuid():
        raise RuntimeError(f"terminals directory is not owned by us: {path}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


class HubClient:
    """One connected client: bounded queue + token bucket + sender thread."""

    def __init__(self, send, *, queue_bytes: int, max_bytes_per_sec: int,
                 on_drop=None, autostart: bool = True) -> None:
        self._send = send
        self._on_drop = on_drop
        self._limit = max(1, int(queue_bytes))
        self._rate = max(1, int(max_bytes_per_sec))
        self._chunks: collections.deque[bytes] = collections.deque()
        self._bytes = 0
        self._tokens = float(self._rate)
        self._last = time.monotonic()
        self._cv = threading.Condition()
        self._closed = False
        self.dropped_code: int | None = None
        self.mode = "read"
        self.control_cb = None  # set by the WS handler: callable(dict)
        self.thread = threading.Thread(target=self._run, name="term-client", daemon=True)
        if autostart:
            self.thread.start()

    def notify(self, obj: dict) -> None:
        cb = self.control_cb
        if cb is not None:
            try:
                cb(obj)
            except Exception:
                pass

    def enqueue(self, data: bytes) -> bool:
        if not data:
            return True
        with self._cv:
            if self._closed:
                return False
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < len(data):
                return False  # over the per-client rate cap
            if self._bytes + len(data) > self._limit:
                return False  # queue full: this client cannot keep up
            self._tokens -= len(data)
            self._chunks.append(data)
            self._bytes += len(data)
            self._cv.notify()
            return True

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._chunks and not self._closed:
                    self._cv.wait(0.2)
                if self._closed and not self._chunks:
                    return
                data = self._chunks.popleft()
                self._bytes -= len(data)
            try:
                self._send(data)
            except Exception:
                self.close(wsmod.CLOSE_GOING_AWAY)
                return

    def close(self, code: int = wsmod.CLOSE_GOING_AWAY) -> None:
        with self._cv:
            if self._closed:
                return
            self._closed = True
            self.dropped_code = code
            self._chunks.clear()
            self._bytes = 0
            self._cv.notify_all()
        if self._on_drop is not None:
            try:
                self._on_drop(self, code)
            except Exception:
                pass


# -- output sources ----------------------------------------------------------
class _PipeSource:
    """A blocking-ish byte source with ``read(n)`` and ``close()``."""

    def read(self, n: int) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> bytes:
        return b""


class LocalPipeSource(_PipeSource):
    def __init__(self, terminal_id: str, socket: str, pane: str) -> None:
        self.socket = socket
        self.pane = pane
        self.fifo = os.path.join(terminals_dir(), f"{terminal_id}.fifo")
        if os.path.lexists(self.fifo):
            os.unlink(self.fifo)
        os.mkfifo(self.fifo, 0o600)
        # Open for reading before launching pipe-pane so no output is lost.
        self.fd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        cmd = shlex.quote(self.fifo)
        self._run(
            "pipe-pane", "-O", "-t", self.pane, f"cat >> {cmd}"
        )

    def _run(self, *args: str) -> None:
        proc = subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", *args],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"tmux {' '.join(args)} failed: {proc.stderr.decode(errors='replace')}")

    def read(self, n: int) -> bytes:
        import select

        try:
            ready, _, _ = select.select([self.fd], [], [], 0.2)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            return os.read(self.fd, n)
        except BlockingIOError:
            return b""
        except OSError:
            return b""

    def snapshot(self) -> bytes:
        return capture_with_cursor_local(self.socket, self.pane)

    def close(self) -> None:
        try:
            self._run("pipe-pane", "-t", self.pane)
        except Exception:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.unlink(self.fifo)
        except OSError:
            pass


REMOTE_SCRIPT = r'''
set -eu
d="${XDG_RUNTIME_DIR:-/tmp/crewhall-$(id -u)}/crewhall-term"
if [ -L "$d" ]; then echo "refusing symlink" >&2; exit 1; fi
mkdir -p "$d"
chmod 700 "$d"
[ "$(stat -c %u "$d")" = "$(id -u)" ] || { echo "not owned" >&2; exit 1; }
fifo="$d/$3.fifo"
rm -f "$fifo"
mkfifo -m 600 "$fifo"
cleanup() {
  tmux -L "$1" -f /dev/null pipe-pane -t "$2" >/dev/null 2>&1 || true
  rm -f "$fifo"
}
trap cleanup EXIT HUP TERM INT
tmux -L "$1" -f /dev/null pipe-pane -O -t "$2" "cat >> '$fifo'"
cat "$fifo"
'''


class RemotePipeSource(_PipeSource):
    def __init__(self, terminal_id: str, host_cfg: dict, socket: str, pane: str) -> None:
        from .backends.ssh_tmux import SshTmuxBackend

        self.host_cfg = host_cfg
        self.socket = socket
        self.pane = pane
        self.backend = SshTmuxBackend(host_cfg)
        # ssh joins the remote command argv with spaces, so the whole thing must
        # be a single, correctly quoted argument for the remote shell.
        remote_cmd = shlex.join(
            ["sh", "-c", REMOTE_SCRIPT, "sh", socket, pane, terminal_id]
        )
        argv = [*self.backend._ssh_local_argv(control_suffix="-stream"), remote_cmd]
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        )

    def read(self, n: int) -> bytes:
        if self.proc.stdout is None:
            return b""
        # read1 returns as soon as some bytes are available (a plain read(n)
        # would block until n bytes or EOF, stalling the stream).
        return self.proc.stdout.read1(n) or b""

    def snapshot(self) -> bytes:
        return capture_with_cursor_remote(self.host_cfg, self.socket, self.pane)

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass


class PollSource(_PipeSource):
    """Fallback when pipe-pane is unavailable: repaint the whole pane."""

    def __init__(self, socket: str, pane: str, host_cfg: dict | None = None) -> None:
        self.socket = socket
        self.pane = pane
        self.host_cfg = host_cfg
        self._last = ""

    def read(self, n: int) -> bytes:
        time.sleep(POLL_INTERVAL)
        try:
            text = capture_local(self.socket, self.pane) if self.host_cfg is None \
                else capture_remote(self.host_cfg, self.socket, self.pane)
        except Exception:
            return b""
        if text == self._last:
            return b""
        self._last = text
        return b"\x1b[2J\x1b[H" + text.replace("\n", "\r\n").encode("utf-8", "replace")

    def snapshot(self) -> bytes:
        try:
            text = capture_local(self.socket, self.pane) if self.host_cfg is None \
                else capture_remote(self.host_cfg, self.socket, self.pane)
        except Exception:
            return b""
        return text.replace("\n", "\r\n").encode("utf-8", "replace")

    def close(self) -> None:
        return None


# -- tmux helpers ------------------------------------------------------------
def _tmux(socket: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", socket, "-f", "/dev/null", *args],
        capture_output=True, timeout=timeout,
    )


def capture_local(socket: str, pane: str, escapes: bool = True) -> str:
    args = ["capture-pane", "-p", "-t", pane, "-S", "-"]
    if escapes:
        args.append("-e")
    proc = _tmux(socket, *args)
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else ""


def capture_with_cursor_local(socket: str, pane: str) -> bytes:
    text = capture_local(socket, pane, escapes=True)
    cursor = _tmux(socket, "display-message", "-p", "-t", pane, "#{cursor_x} #{cursor_y}")
    out = text.replace("\n", "\r\n").encode("utf-8", "replace")
    if cursor.returncode == 0:
        parts = cursor.stdout.decode("utf-8", "replace").strip().split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            x, y = int(parts[0]), int(parts[1])
            out += f"\x1b[{y + 1};{x + 1}H".encode("ascii")
    return out


def _ssh_backend(host_cfg: dict):
    from .backends.ssh_tmux import SshTmuxBackend

    return SshTmuxBackend(host_cfg)


def capture_remote(host_cfg: dict, socket: str, pane: str, escapes: bool = True) -> str:
    backend = _ssh_backend(host_cfg)
    args = ["capture-pane", "-p", "-t", pane, "-S", "-"]
    if escapes:
        args.append("-e")
    proc = backend._run(*args)
    return proc.stdout if proc.returncode == 0 else ""


def capture_with_cursor_remote(host_cfg: dict, socket: str, pane: str) -> bytes:
    text = capture_remote(host_cfg, socket, pane, escapes=True)
    backend = _ssh_backend(host_cfg)
    cursor = backend._run("display-message", "-p", "-t", pane, "#{cursor_x} #{cursor_y}")
    out = text.replace("\n", "\r\n").encode("utf-8", "replace")
    if cursor.returncode == 0:
        parts = cursor.stdout.strip().split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            x, y = int(parts[0]), int(parts[1])
            out += f"\x1b[{y + 1};{x + 1}H".encode("ascii")
    return out


def send_input_local(socket: str, pane: str, data: bytes) -> None:
    for i in range(0, len(data), MAX_HEX_CHUNK):
        chunk = data[i : i + MAX_HEX_CHUNK]
        hexes = [f"{b:02x}" for b in chunk]
        proc = _tmux(socket, "send-keys", "-t", pane, "-H", *hexes)
        if proc.returncode != 0:
            # Older tmux without -H: fall back to literal text (documented lossy).
            text = chunk.decode("utf-8", "ignore")
            _tmux(socket, "send-keys", "-t", pane, "-l", "--", text)


def send_input_remote(host_cfg: dict, socket: str, pane: str, data: bytes) -> None:
    backend = _ssh_backend(host_cfg)
    for i in range(0, len(data), MAX_HEX_CHUNK):
        chunk = data[i : i + MAX_HEX_CHUNK]
        hexes = [f"{b:02x}" for b in chunk]
        backend._run("send-keys", "-t", pane, "-H", *hexes)


def resize_local(socket: str, tmux_session: str, cols: int, rows: int) -> bool:
    proc = _tmux(socket, "resize-window", "-t", tmux_session, "-x", str(cols), "-y", str(rows))
    return proc.returncode == 0


def resize_remote(host_cfg: dict, tmux_session: str, cols: int, rows: int) -> bool:
    backend = _ssh_backend(host_cfg)
    proc = backend._run("resize-window", "-t", tmux_session, "-x", str(cols), "-y", str(rows))
    return proc.returncode == 0


class InputPump:
    """Coalesce keystrokes arriving within ``INPUT_WINDOW`` into one send."""

    def __init__(self, send_bytes) -> None:
        self._send = send_bytes
        self._buf = bytearray()
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._buf += data
            if self._timer is None:
                self._timer = threading.Timer(INPUT_WINDOW, self._flush)
                self._timer.daemon = True
                self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            data = bytes(self._buf)
            self._buf.clear()
            self._timer = None
        if data:
            try:
                self._send(data)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._flush()


class TerminalHub:
    """Owns one output source and fans it out to N clients without blocking."""

    def __init__(self, terminal_id: str, *, pane: str, tmux_session: str, socket: str,
                 host_cfg: dict | None = None, queue_bytes: int = 524288,
                 max_bytes_per_sec: int = 4194304, cols: int = 120, rows: int = 32) -> None:
        self.terminal_id = terminal_id
        self.pane = pane
        self.tmux_session = tmux_session
        self.socket = socket
        self.host_cfg = host_cfg
        self.cols = cols
        self.rows = rows
        self.queue_bytes = queue_bytes
        self.max_bytes_per_sec = max_bytes_per_sec
        self.stream_mode = "pipe"
        self.clients: list[HubClient] = []
        self.writer: HubClient | None = None
        self.readonly = False
        self.host = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._source: _PipeSource | None = None
        self._pump = InputPump(self._send_input)
        self._stop_timer: threading.Timer | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._reader is not None:
                if self._stop_timer is not None:
                    self._stop_timer.cancel()
                    self._stop_timer = None
                return
            try:
                self._source = self._make_source()
            except Exception:
                self._source = PollSource(self.socket, self.pane, self.host_cfg)
                self.stream_mode = "poll"
            self._stop.clear()
            self._reader = threading.Thread(
                target=self._read_loop, name=f"term-hub-{self.terminal_id}", daemon=True
            )
            self._reader.start()

    def _make_source(self) -> _PipeSource:
        if self.host_cfg is not None:
            return RemotePipeSource(self.terminal_id, self.host_cfg, self.socket, self.pane)
        return LocalPipeSource(self.terminal_id, self.socket, self.pane)

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            reader = self._reader
            self._reader = None
        if reader is not None:
            reader.join(timeout=2.0)
        src = self._source
        self._source = None
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        self._pump.close()

    def schedule_stop(self, delay: float = 5.0) -> None:
        with self._lock:
            if self._stop_timer is not None:
                return
            self._stop_timer = threading.Timer(delay, self._deferred_stop)
            self._stop_timer.daemon = True
            self._stop_timer.start()

    def _deferred_stop(self) -> None:
        with self._lock:
            self._stop_timer = None
            if self.clients:
                return
        self.stop()

    # -- clients -----------------------------------------------------------
    def add_client(self, client: HubClient) -> None:
        self.start()
        with self._lock:
            if self._stop_timer is not None:
                self._stop_timer.cancel()
                self._stop_timer = None
            self.clients.append(client)

    def remove_client(self, client: HubClient) -> None:
        with self._lock:
            if client in self.clients:
                self.clients.remove(client)
            if self.writer is client:
                self.writer = None  # the keyboard is free until someone claims it
            empty = not self.clients
        if empty:
            self.schedule_stop(5.0)

    def mode_for_new_client(self, client: HubClient) -> str:
        with self._lock:
            if self.readonly:
                client.mode = "read"
                return "read"
            if self.writer is None or self.writer not in self.clients:
                self.writer = client
                client.mode = "write"
                return "write"
            client.mode = "read"
            return "read"

    def claim(self, client: HubClient) -> None:
        with self._lock:
            if self.readonly:
                return
            prev = self.writer
            self.writer = client
            client.mode = "write"
        if prev is not None and prev is not client:
            prev.mode = "read"
            prev.notify({"mode": "read"})

    def _read_loop(self) -> None:
        source = self._source
        if source is None:
            return
        while not self._stop.is_set():
            try:
                data = source.read(CHUNK)
            except Exception:
                break
            if data == b"":
                # EOF or idle: keep polling a pipe; stop if the source died.
                if self._stop.is_set():
                    break
                if isinstance(source, PollSource):
                    continue
                if getattr(source, "fd", None) is not None:
                    continue
                break
            with self._lock:
                clients = list(self.clients)
            for client in clients:
                if not client.enqueue(data):
                    client.close(wsmod.CLOSE_SLOW)
                    self.remove_client(client)

    # -- input / resize ----------------------------------------------------
    def _send_input(self, data: bytes) -> None:
        if self.host_cfg is not None:
            send_input_remote(self.host_cfg, self.socket, self.pane, data)
        else:
            send_input_local(self.socket, self.pane, data)

    def feed_input(self, data: bytes) -> None:
        self._pump.feed(data)

    def resize(self, cols: int, rows: int) -> bool:
        if self.host_cfg is not None:
            return resize_remote(self.host_cfg, self.tmux_session, cols, rows)
        return resize_local(self.socket, self.tmux_session, cols, rows)

    def snapshot(self) -> bytes:
        source = self._source
        if source is None:
            try:
                source = self._make_source()
            except Exception:
                return b""
        return source.snapshot()
