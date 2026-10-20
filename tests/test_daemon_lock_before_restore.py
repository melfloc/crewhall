"""A daemon that loses the singleton lock must not adopt or restore any agent.

Adopting tmux sessions and re-attaching the OpenCode ``serve`` processes are
side effects: a duplicate daemon that does them before discovering it lost the
lock races the winner for the same ports (and raises false notices).
"""
from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import daemon, paths


class LockBeforeRestore(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("XDG_RUNTIME_DIR")
        self.tmp = tempfile.mkdtemp(prefix="at-lockfirst-")
        os.environ["XDG_RUNTIME_DIR"] = self.tmp
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prev is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self._prev

    def test_a_losing_daemon_never_builds_or_restores_the_controller(self):
        os.makedirs(os.path.dirname(paths.lock_path()), exist_ok=True)
        with open(paths.lock_path(), "w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(daemon, "Controller") as controller, \
                    mock.patch.object(daemon, "_setup_logging"):
                code = daemon.main(["--foreground"])
        self.assertEqual(code, 0)
        controller.assert_not_called()


if __name__ == "__main__":
    unittest.main()
