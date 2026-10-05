from __future__ import annotations

import os
import tempfile
import time
import unittest

from crewhall import clean


class CleanupPlan(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-clean-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _mk(self, rel: str, content: bytes = b"x") -> str:
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def test_finds_test_leftovers_and_ignores_other_entries(self):
        os.makedirs(os.path.join(self.tmp, "at-tests-abc"))
        os.makedirs(os.path.join(self.tmp, "at-web-def"))
        os.makedirs(os.path.join(self.tmp, "important-project"))
        plan = clean.plan_cleanup(tmp_roots=[self.tmp])
        paths = {i.path for i in plan.items}
        self.assertIn(os.path.join(self.tmp, "at-tests-abc"), paths)
        self.assertIn(os.path.join(self.tmp, "at-web-def"), paths)
        self.assertNotIn(os.path.join(self.tmp, "important-project"), paths)

    def test_max_age_days_skips_recent_temps(self):
        fresh = os.path.join(self.tmp, "at-tests-fresh")
        old = os.path.join(self.tmp, "at-tests-old")
        os.makedirs(fresh)
        os.makedirs(old)
        past = time.time() - 10 * 86400
        os.utime(old, (past, past))
        plan = clean.plan_cleanup(tmp_roots=[self.tmp], max_age_days=5)
        paths = {i.path for i in plan.items}
        self.assertIn(old, paths)
        self.assertNotIn(fresh, paths)

    def test_control_backups_keep_the_newest(self):
        root = os.path.join(self.tmp, "control-backups", "key1")
        os.makedirs(root)
        for i in range(7):
            p = os.path.join(root, f"2020010{i}-CLAUDE.md")
            open(p, "w").write("x")
            os.utime(p, (1000 + i, 1000 + i))
        plan = clean.plan_cleanup(tmp_roots=[], keep_backups=5, state_dir=self.tmp)
        backups = [i for i in plan.items if i.kind == "control-backup"]
        self.assertEqual(len(backups), 2)  # 7 - 5 kept
        # The oldest two are the ones planned for removal.
        self.assertTrue(all(os.path.basename(i.path).startswith("20200100") or
                            os.path.basename(i.path).startswith("20200101") for i in backups))

    def test_apply_cleanup_removes_only_planned_items(self):
        keep = os.path.join(self.tmp, "important")
        os.makedirs(keep)
        gone = os.path.join(self.tmp, "at-tests-gone")
        os.makedirs(gone)
        plan = clean.plan_cleanup(tmp_roots=[self.tmp])
        result = clean.apply_cleanup(plan)
        self.assertIn(gone, result["removed"])
        self.assertTrue(os.path.isdir(keep))
        self.assertFalse(os.path.exists(gone))
        self.assertEqual(result["failed"], [])

    def test_apply_cleanup_leaves_a_nonempty_dir_in_place(self):
        d = os.path.join(self.tmp, "at-tests-security-sessions")
        os.makedirs(os.path.join(d, "sub"))
        plan = clean.plan_cleanup(tmp_roots=[self.tmp])
        clean.apply_cleanup(plan)
        self.assertTrue(os.path.isdir(os.path.join(d, "sub")))  # not empty: not removed


class CleanCli(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="at-clean-cli-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.env = dict(os.environ, XDG_STATE_HOME=self.home, XDG_RUNTIME_DIR=self.home,
                        PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _run(self, *args: str):
        import subprocess
        import sys

        return subprocess.run([sys.executable, "-m", "crewhall", *args],
                              capture_output=True, text=True, env=self.env, timeout=30)

    def test_dry_run_lists_without_deleting(self):
        left = os.path.join(tempfile.gettempdir(), "at-tests-cli-probe")
        os.makedirs(left, exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(left, ignore_errors=True))
        proc = self._run("clean", "--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = __import__("json").loads(proc.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertTrue(os.path.isdir(left))  # dry run never deletes

    def test_yes_removes_the_planned_items(self):
        left = os.path.join(tempfile.gettempdir(), "at-tests-cli-gone")
        os.makedirs(left, exist_ok=True)
        proc = self._run("clean", "--yes", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(left))


if __name__ == "__main__":
    unittest.main()
