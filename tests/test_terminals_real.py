"""Integration tests for terminals against an isolated tmux server (and sshd)."""
from __future__ import annotations

import os
import shutil
import tempfile
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


if __name__ == "__main__":
    unittest.main()
