"""Integration tests for terminals against an isolated tmux server (and sshd)."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest

from crewhall import settings
from crewhall.controller import Controller
from crewhall.terminals import is_terminal
from crewhall.types import SessionSpec

from .test_ssh_tmux_real import HAVE as SSH_HAVE, SshdHarness

HAVE_TMUX = shutil.which("tmux") is not None


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@unittest.skipUnless(HAVE_TMUX, "tmux not installed")
class TerminalLocalRealTests(unittest.TestCase):
    def setUp(self):
        settings.patch({"terminals.enabled": True}, confirm=True)
        self.dir = tempfile.mkdtemp(prefix="ati-term-", dir="/tmp")
        self.ctrl = Controller(adopt=False, persist=False)

    def tearDown(self):
        for t in self.ctrl.list_terminals():
            try:
                self.ctrl.close_terminal(t["session_id"])
            except Exception:
                pass
        settings.patch({"terminals.enabled": False})
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_echo_roundtrip(self):
        t = self.ctrl.create_terminal(cwd=self.dir)
        self.ctrl.get(t.session_id).write("echo MARK_$((6*7))")
        self.ctrl.get(t.session_id).send_enter()
        self.assertTrue(
            _wait_for(lambda: "MARK_42" in self.ctrl.get(t.session_id).capture()),
            "MARK_42 not found in capture",
        )

    def test_initial_command_keeps_terminal_alive(self):
        t = self.ctrl.create_terminal(cwd=self.dir, command="echo HELLO_INIT")
        self.assertTrue(
            _wait_for(lambda: "HELLO_INIT" in self.ctrl.get(t.session_id).capture())
        )
        # The shell survived the command (remain-on-exit would show a dead pane).
        self.assertIsNone(self.ctrl.get(t.session_id).backend.poll())

    def test_close(self):
        t = self.ctrl.create_terminal(cwd=self.dir)
        self.ctrl.close_terminal(t.session_id)
        self.assertEqual(self.ctrl.list_terminals(), [])

    def test_persist_and_restore(self):
        from crewhall.persistence import StateStore

        store = StateStore(path=os.path.join(self.dir, "state.json"))
        self.ctrl._store = store
        t = self.ctrl.create_terminal(cwd=self.dir, title="keepme")
        self.ctrl._persist()
        self.ctrl.shutdown()  # like a graceful daemon stop: the tmux session goes
        other = Controller(adopt=False, persist=False)
        other._store = store
        other.restore()
        self.ctrl = other  # so tearDown cleans up the restored terminal
        info = other.terminal_info(t.session_id)
        self.assertEqual(info["title"], "keepme")
        other.get(t.session_id).write("echo RESTORED_$((6*7))")
        other.get(t.session_id).send_enter()
        self.assertTrue(
            _wait_for(lambda: "RESTORED_42" in other.get(t.session_id).capture()),
            "restored terminal did not respond",
        )

    def test_readopt_kind(self):
        t = self.ctrl.create_terminal(cwd=self.dir, title="keepme")
        raw = self.ctrl.create(SessionSpec(command="sleep 30", cwd=self.dir))
        # A second controller adopts everything on the same tmux server.
        other = Controller(adopt=True, persist=False)
        info = other.terminal_info(t.session_id)
        self.assertEqual(info["kind"], "terminal")
        self.assertEqual(info["title"], "keepme")
        raw_info = other.registry.resolve(raw.session_id).info().to_dict()
        self.assertEqual(raw_info["kind"], "session")
        other.shutdown()


@unittest.skipUnless(HAVE_TMUX and SSH_HAVE, "tmux/ssh/sshd not installed")
class TerminalRemoteRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sshd = SshdHarness()
        cls.sshd.start()
        cls.host = cls.sshd.host_config()

    @classmethod
    def tearDownClass(cls):
        cls.sshd.cleanup()

    def setUp(self):
        settings.patch({"hosts": {"loop": self.host}}, confirm=True)
        settings.patch({"terminals.enabled": True}, confirm=True)
        self.dir = tempfile.mkdtemp(prefix="ati-termr-", dir="/tmp")
        self.ctrl = Controller(adopt=False, persist=False)

    def tearDown(self):
        for t in self.ctrl.list_terminals():
            try:
                self.ctrl.close_terminal(t["session_id"])
            except Exception:
                pass
        settings.patch({"terminals.enabled": False})
        settings.patch({"hosts": {}}, confirm=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_remote_echo_and_adopt(self):
        t = self.ctrl.create_terminal(host="loop", cwd="/tmp")
        self.ctrl.get(t.session_id).write("echo RMARK_$((6*7))")
        self.ctrl.get(t.session_id).send_enter()
        self.assertTrue(
            _wait_for(lambda: "RMARK_42" in self.ctrl.get(t.session_id).capture()),
            "remote MARK_42 not found",
        )
        other = Controller(adopt=False, persist=False)
        other._adopt_remote_host("loop", settings.host("loop"))
        info = other.terminal_info(t.session_id)
        self.assertEqual(info["kind"], "terminal")
        self.assertEqual(info["host"], "loop")
        other.shutdown()

    def test_remote_host_unreachable(self):
        t = self.ctrl.create_terminal(host="loop", cwd="/tmp")
        self.sshd.stop()
        info = self.ctrl.terminal_info(t.session_id)
        self.assertTrue(info["host_unreachable"])
        self.sshd.start()


@unittest.skipUnless(HAVE_TMUX, "tmux not installed")
class TerminalStreamRealTests(unittest.TestCase):
    def setUp(self):
        from crewhall import settings

        settings.patch({"terminals.enabled": True}, confirm=True)
        self.dir = tempfile.mkdtemp(prefix="ati-term-stream-", dir="/tmp")
        self.ctrl = Controller(adopt=False, persist=False)
        self.term = self.ctrl.create_terminal(cwd=self.dir)
        self.hubs = []

    def tearDown(self):
        from crewhall import settings, terminal_stream

        for hub in self.hubs:
            hub.stop()
        try:
            self.ctrl.close_terminal(self.term.session_id)
        except Exception:
            pass
        settings.patch({"terminals.enabled": False})
        shutil.rmtree(self.dir, ignore_errors=True)

    def _hub(self, **kw):
        from crewhall import settings, terminal_stream

        info = self.ctrl.terminal_info(self.term.session_id)
        meta = info["meta"]
        hub = terminal_stream.TerminalHub(
            self.term.session_id, pane=meta["pane_id"], tmux_session=meta["tmux_session"],
            socket=meta["socket"], queue_bytes=int(settings.get("terminals.client_queue_bytes")),
            max_bytes_per_sec=int(settings.get("terminals.max_bytes_per_sec")), **kw,
        )
        self.hubs.append(hub)
        return hub

    def test_stream_echo_and_snapshot(self):
        from crewhall.terminal_stream import HubClient

        hub = self._hub()
        got: list[bytes] = []
        client = HubClient(lambda d: got.append(d), queue_bytes=10 ** 7,
                           max_bytes_per_sec=10 ** 9)
        hub.start()
        snap = hub.snapshot()
        client.enqueue(snap)
        hub.add_client(client)
        hub.feed_input(b"echo STREAM_$((6*7))\n")
        deadline = time.monotonic() + 8
        while b"STREAM_42" not in b"".join(got) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(b"STREAM_42", b"".join(got))

    def test_resize_changes_stty_size(self):
        from crewhall.terminal_stream import HubClient

        hub = self._hub()
        got: list[bytes] = []
        client = HubClient(lambda d: got.append(d), queue_bytes=10 ** 7,
                           max_bytes_per_sec=10 ** 9)
        hub.start()
        hub.add_client(client)
        self.assertTrue(hub.resize(70, 24))
        hub.feed_input(b"stty size\n")
        deadline = time.monotonic() + 8
        while b"24 70" not in b"".join(got) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(b"24 70", b"".join(got))

    def test_no_residual_fifo_after_stop(self):
        from crewhall import terminal_stream

        hub = self._hub()
        hub.start()
        fifo = os.path.join(terminal_stream.terminals_dir(), f"{self.term.session_id}.fifo")
        self.assertTrue(os.path.exists(fifo))
        hub.stop()
        self.assertFalse(os.path.exists(fifo))

    def test_slow_client_dropped_daemon_stays_responsive(self):
        import time as _t

        from crewhall import settings
        from crewhall.terminal_stream import HubClient
        from crewhall.web import ws

        hub = self._hub()
        fast_bytes: list[int] = []
        slow_started = threading.Event()

        def slow_send(data):
            slow_started.set()
            _t.sleep(30)

        slow = HubClient(slow_send, queue_bytes=4096, max_bytes_per_sec=10 ** 9)
        fast = HubClient(lambda d: fast_bytes.append(len(d)), queue_bytes=10 ** 7,
                         max_bytes_per_sec=10 ** 9)
        hub.add_client(slow)
        hub.add_client(fast)
        # 50 MB of output (reduced from 200 MB for test runtime; same path).
        hub.feed_input(b"yes | head -c 50000000\n")
        deadline = _t.monotonic() + 40
        while slow.dropped_code is None and _t.monotonic() < deadline:
            _t.sleep(0.1)
        self.assertEqual(slow.dropped_code, ws.CLOSE_SLOW)
        self.assertGreater(sum(fast_bytes), 0)
        # A daemon ping must answer quickly while the terminal floods.
        t0 = _t.monotonic()
        self.ctrl.registry.all()  # local controller is responsive
        self.assertLess(_t.monotonic() - t0, 1.0)


@unittest.skipUnless(HAVE_TMUX and SSH_HAVE, "tmux/ssh/sshd not installed")
class TerminalStreamRemoteRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sshd = SshdHarness()
        cls.sshd.start()

    @classmethod
    def tearDownClass(cls):
        cls.sshd.cleanup()

    def setUp(self):
        from crewhall import settings

        self.host = self.sshd.host_config()
        settings.patch({"hosts": {"loop": self.host}}, confirm=True)
        settings.patch({"terminals.enabled": True}, confirm=True)
        self.ctrl = Controller(adopt=False, persist=False)
        self.term = self.ctrl.create_terminal(host="loop", cwd="/tmp")
        self.hubs = []

    def tearDown(self):
        from crewhall import settings

        for hub in self.hubs:
            hub.stop()
        try:
            self.ctrl.close_terminal(self.term.session_id)
        except Exception:
            pass
        settings.patch({"terminals.enabled": False})
        settings.patch({"hosts": {}}, confirm=True)

    def test_terminal_ssh_argv_uses_fixed_options(self):
        from crewhall.backends.ssh_tmux import SSH_BASE_OPTIONS, SshTmuxBackend

        backend = SshTmuxBackend(self.host)
        for argv in (backend._ssh_local_argv(), backend._ssh_local_argv(control_suffix="-stream")):
            for opt in SSH_BASE_OPTIONS:
                self.assertIn(opt, argv)
            self.assertIn("ForwardAgent=no", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("BatchMode=yes", argv)

    def test_remote_stream_uses_distinct_controlpath_and_echoes(self):
        from crewhall import terminal_stream
        from crewhall.backends.ssh_tmux import SshTmuxBackend
        from crewhall.terminal_stream import HubClient

        info = self.ctrl.terminal_info(self.term.session_id)
        meta = info["meta"]
        hub = terminal_stream.TerminalHub(
            self.term.session_id, pane=meta["pane_id"], tmux_session=meta["tmux_session"],
            socket=meta["socket"], host_cfg=self.host,
        )
        self.hubs.append(hub)
        got: list[bytes] = []
        client = HubClient(lambda d: got.append(d), queue_bytes=10 ** 7,
                           max_bytes_per_sec=10 ** 9)
        hub.start()
        # The stream's ssh argv must use a distinct ControlPath suffix.
        src = hub._source
        self.assertIsNotNone(src)
        argv = getattr(getattr(src, "proc", None), "args", None)
        self.assertIsNotNone(argv, "remote source is an ssh process")
        control = [a for a in argv if a.startswith("ControlPath=")]
        self.assertTrue(control and control[0].endswith("%C-stream"), control)
        default = SshTmuxBackend(self.host)._ssh_local_argv()
        default_control = [a for a in default if a.startswith("ControlPath=")]
        self.assertNotEqual(control[0], default_control[0])
        hub.add_client(client)
        # Wait for the remote ssh + pipe-pane to be up before typing (input sent
        # earlier is not captured yet).
        time.sleep(2.0)
        hub.feed_input(b"echo RSTREAM_$((6*7))\n")
        deadline = time.monotonic() + 10
        while b"RSTREAM_42" not in b"".join(got) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIn(b"RSTREAM_42", b"".join(got))


if __name__ == "__main__":
    unittest.main()
