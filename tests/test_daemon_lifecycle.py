from __future__ import annotations

import os
import shutil
import signal
import tempfile
import threading
import time
import unittest

from agent_terminal import paths
from agent_terminal.client import Client, ensure_daemon, ping


def _read_pid() -> int | None:
    try:
        with open(paths.pid_path()) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


class DaemonLifecycle(unittest.TestCase):
    """Regression tests for the Fase 7B DISCONNECTED bug (real daemons)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._prev_runtime = os.environ.get("XDG_RUNTIME_DIR")
        cls.tmp = tempfile.mkdtemp(prefix="at-lifecycle-")
        os.environ["XDG_RUNTIME_DIR"] = cls.tmp

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            Client(autostart=False).call("shutdown")
            time.sleep(0.3)
        except Exception:
            pid = _read_pid()
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        if cls._prev_runtime is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = cls._prev_runtime
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        try:
            Client(autostart=False).call("shutdown")
        except Exception:
            pass
        for _ in range(50):
            if not ping():
                break
            time.sleep(0.05)
        pid = _read_pid()
        if pid:
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.05)
        for path in (paths.socket_path(), paths.pid_path(), paths.lock_path()):
            try:
                os.unlink(path)
            except OSError:
                pass
        self.client = Client(autostart=False)

    def test_fresh_start(self) -> None:
        self.assertFalse(ping())
        ensure_daemon()
        self.assertTrue(ping())
        self.assertIsNotNone(_read_pid())
        self.assertTrue(self.client.call("ping")["ok"])

    def test_existing_daemon_is_reused(self) -> None:
        ensure_daemon()
        first = _read_pid()
        ensure_daemon()
        self.assertEqual(_read_pid(), first)

    def test_stale_socket_recovers(self) -> None:
        self.client.call("shutdown") if ping() else None
        time.sleep(0.3)
        try:
            os.unlink(paths.socket_path())
        except OSError:
            pass
        open(paths.socket_path(), "w").close()  # stale, non-listening file
        self.assertFalse(ping())
        ensure_daemon()
        self.assertTrue(ping())
        self.assertTrue(self.client.call("ping")["ok"])

    def test_reconnect_after_daemon_death(self) -> None:
        ensure_daemon()
        pid = _read_pid()
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.4)
        # Client(autostart=False) must still recover on the next call.
        self.assertTrue(self.client.call("meta_info")["ok"])
        self.assertNotEqual(_read_pid(), pid)

    def test_concurrent_start_single_daemon(self) -> None:
        errors: list[Exception] = []

        def worker() -> None:
            try:
                ensure_daemon()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertTrue(ping())

    def test_client_raises_rpcerror_without_daemon_and_recovers(self) -> None:
        # A call with autostart=False on a dead environment recovers via ensure.
        ensure_daemon()
        Client(autostart=False).call("shutdown")
        for _ in range(60):
            if not ping():
                break
            time.sleep(0.05)
        self.assertFalse(ping())
        # call() re-ensures even though autostart is False
        self.assertTrue(self.client.call("ping")["ok"])


    def _alive(self, pid):
        """Running (a zombie child that already exited does not count)."""
        try:
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False

    def test_orphaned_daemon_exits_when_a_new_one_takes_over(self):
        ensure_daemon()
        first = _read_pid()
        self.assertTrue(first)
        for path in (paths.socket_path(), paths.lock_path()):
            os.unlink(path)  # what a careless cleanup does
        ensure_daemon()  # spawns a fresh daemon: the socket is gone
        second = _read_pid()
        self.assertNotEqual(first, second)
        deadline = time.monotonic() + 10
        while self._alive(first) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(self._alive(first), "orphaned daemon kept running")
        self.assertTrue(self._alive(second))
        self.assertTrue(ping())

    def test_lock_is_ours_helper(self):
        from agent_terminal.daemon import lock_is_ours

        path = os.path.join(self.tmp, "x.lock")
        with open(path, "w") as fh:
            self.assertTrue(lock_is_ours(fh, path))
            os.unlink(path)
            self.assertFalse(lock_is_ours(fh, path))
            open(path, "w").close()  # same name, different file
            self.assertFalse(lock_is_ours(fh, path))


if __name__ == "__main__":
    unittest.main()

