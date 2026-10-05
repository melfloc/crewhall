from __future__ import annotations

import os
import tempfile
import time
import unittest

from agent_terminal import paths


class TmpdirContainment(unittest.TestCase):
    """Fase 11 containment: agents get a dedicated, pruned TMPDIR."""

    def setUp(self) -> None:
        self._prev_state = os.environ.get("XDG_STATE_HOME")
        self._prev_tmpdir = os.environ.pop("TMPDIR", None)
        self.state = tempfile.mkdtemp(prefix="at-cstate-")
        os.environ["XDG_STATE_HOME"] = self.state

    def tearDown(self) -> None:
        import shutil

        if self._prev_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._prev_state
        if self._prev_tmpdir is not None:
            os.environ["TMPDIR"] = self._prev_tmpdir
        shutil.rmtree(self.state, ignore_errors=True)

    def test_tmpdir_root_is_under_state(self):
        self.assertTrue(paths.tmpdir_root().startswith(self.state))
        self.assertIn(os.sep + "tmp", paths.tmpdir_root())

    def test_usable_tmpdir_is_dedicated_not_shared(self):
        chosen = paths.usable_tmpdir()
        self.assertEqual(chosen, paths.tmpdir_root())
        self.assertNotEqual(chosen, "/tmp")
        self.assertNotIn("/dev/shm", chosen)

    def test_explicit_tmpdir_respected(self):
        os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="at-explicit-")
        try:
            self.assertIsNone(paths.usable_tmpdir())
        finally:
            os.environ.pop("TMPDIR", None)

    def _leak(self, name: str, size: int, age: float) -> str:
        root = paths.tmpdir_root()
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, name)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    def test_cleanup_removes_old_files(self):
        old = self._leak(".old-00000000.so", 1024, age=7200)
        fresh = self._leak(".fresh-00000000.so", 1024, age=10)
        removed = paths.cleanup_tmpdir(max_age_seconds=3600)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_cleanup_caps_total_size(self):
        # four 20MB files, all fresh; cap at 50MB -> must drop the oldest.
        for i in range(4):
            self._leak(f".f{i}-00000000.so", 20 * 1024 * 1024, age=100)
        paths.cleanup_tmpdir(max_age_seconds=99999, max_bytes=50 * 1024 * 1024)
        total = sum(
            os.path.getsize(os.path.join(paths.tmpdir_root(), f))
            for f in os.listdir(paths.tmpdir_root())
        )
        self.assertLessEqual(total, 50 * 1024 * 1024)

    def test_cleanup_ignores_non_tmpdir(self):
        # A file outside the dedicated dir must never be removed.
        outside = tempfile.mkdtemp(prefix="at-outside-")
        victim = os.path.join(outside, ".dont-touch-00000000.so")
        with open(victim, "wb") as fh:
            fh.write(b"x")
        paths.cleanup_tmpdir()
        self.assertTrue(os.path.exists(victim))
        import shutil

        shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
