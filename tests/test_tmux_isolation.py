from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall.backends import tmux


class TmuxSocketIsolation(unittest.TestCase):
    def test_tests_never_use_the_production_tmux_socket(self):
        # Real daemons started by the suite adopt every session on their tmux
        # server and close them on shutdown; sharing the user's server killed
        # their live agents.
        self.assertNotEqual(tmux.socket_name(), "crewhall")
        self.assertEqual(tmux.socket_name(), os.environ["CREWHALL_TMUX_SOCKET"])
        # The legacy attribute must resolve lazily too, not stay frozen.
        self.assertEqual(tmux.SOCKET, os.environ["CREWHALL_TMUX_SOCKET"])

    def test_socket_is_resolved_on_every_use_not_at_import(self):
        # Regression (0.48.0): `unittest discover` imports crewhall before
        # tests/__init__ sets the env var, so a module-level constant used to
        # freeze the production socket for the whole run.
        with mock.patch.dict(os.environ,
                             {"CREWHALL_TMUX_SOCKET": "at_test_dynamic"}):
            self.assertEqual(tmux.socket_name(), "at_test_dynamic")
            self.assertEqual(tmux.SOCKET, "at_test_dynamic")
            self.assertEqual(tmux.TmuxBackend().socket, "at_test_dynamic")
            self.assertEqual(tmux.TmuxBackend.attach_command("s")[2], "at_test_dynamic")
        # The legacy name alone is still honoured during the migration.
        with mock.patch.dict(os.environ, {"AGENT_TERMINAL_TMUX_SOCKET": "at_test_legacy"}):
            os.environ.pop("CREWHALL_TMUX_SOCKET", None)
            self.assertEqual(tmux.socket_name(), "at_test_legacy")
        self.assertEqual(tmux.TmuxBackend().socket, os.environ["CREWHALL_TMUX_SOCKET"])

    def test_no_tmux_call_reaches_the_production_socket(self):
        real_run = tmux.subprocess.run

        def guard(argv, *args, **kwargs):
            argv = list(argv) if isinstance(argv, (list, tuple)) else argv
            if isinstance(argv, list) and argv[:1] == ["tmux"] and "-L" in argv:
                socket = argv[argv.index("-L") + 1]
                if socket == "crewhall":
                    raise AssertionError(
                        f"test tried to touch the production tmux socket: {argv}"
                    )
            return real_run(argv, *args, **kwargs)

        with mock.patch.object(tmux.subprocess, "run", side_effect=guard):
            backend = tmux.TmuxBackend()
            self.assertNotEqual(backend.socket, "crewhall")
            backend._run("list-sessions")  # must not raise from the guard


class TempDirIsolation(unittest.TestCase):
    def test_sweep_removes_dirs_created_during_the_run(self):
        # Regression (0.48.0): mkdtemp used to scatter /tmp/at-* dirs that the
        # suite never cleaned up. The run sweeps anything it created and never
        # touches directories that already existed when it started.
        import tests

        made = tempfile.mkdtemp(prefix="at-regression-")
        self.assertNotIn(made, tests._PREEXISTING)
        removed = tests._sweep_new_temp_dirs(only={made})
        self.assertIn(made, removed)
        self.assertFalse(os.path.exists(made))

    def test_sweep_never_touches_preexisting_dirs(self):
        import tests

        # A directory that existed before the run (snapshot) is left alone even
        # if it matches the test prefix.
        victim = tempfile.mkdtemp(prefix="at-regression-keep-")
        self.addCleanup(shutil.rmtree, victim, ignore_errors=True)
        with mock.patch.object(tests, "_PREEXISTING", tests._PREEXISTING | {victim}):
            self.assertEqual(tests._sweep_new_temp_dirs(only={victim}), [])
        self.assertTrue(os.path.exists(victim))
