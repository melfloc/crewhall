from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

from crewhall import worktrees


def _git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *(["-C", cwd] if cwd else []), *args],
                          capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "git is required")
class Worktrees(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = tempfile.mkdtemp(prefix="at-wtrepo-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.addCleanup(self._remove_all)
        _git("init", "-q", cwd=self.repo)
        with open(os.path.join(self.repo, "README"), "w") as fh:
            fh.write("base\n")
        _git("add", "README", cwd=self.repo)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init",
             cwd=self.repo)

    def _remove_all(self) -> None:
        for path in os.listdir(worktrees.root()) if os.path.isdir(worktrees.root()) else []:
            shutil.rmtree(os.path.join(worktrees.root(), path), ignore_errors=True)

    def test_create_and_isolate_two_agents(self):
        a = worktrees.create(self.repo, "crew", "alpha")
        b = worktrees.create(self.repo, "crew", "beta")
        self.assertTrue(os.path.isdir(a) and os.path.isdir(b))
        self.assertNotEqual(a, b)
        self.assertEqual(worktrees.status(a)["branch"], "at/crew/alpha")
        with open(os.path.join(a, "only-a.txt"), "w") as fh:
            fh.write("x")
        self.assertFalse(os.path.exists(os.path.join(b, "only-a.txt")))

    def test_safe_delete_keeps_dirty_worktrees(self):
        a = worktrees.create(self.repo, "crew", "alpha")
        ok, reason = worktrees.can_remove(a, repo=self.repo)
        self.assertTrue(ok, reason)
        with open(os.path.join(a, "dirty.txt"), "w") as fh:
            fh.write("uncommitted")
        ok, reason = worktrees.can_remove(a, repo=self.repo)
        self.assertFalse(ok)
        self.assertIn("changes", reason)
        worktrees.remove(a, force=True)
        self.assertFalse(os.path.exists(a))

    def test_unmerged_commits_block_removal(self):
        a = worktrees.create(self.repo, "crew", "alpha")
        with open(os.path.join(a, "new.txt"), "w") as fh:
            fh.write("x")
        _git("add", "new.txt", cwd=a)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "work",
             cwd=a)
        ok, reason = worktrees.can_remove(a, repo=self.repo)
        self.assertFalse(ok)
        self.assertIn("not merged", reason)

    def test_hostile_names_are_sanitized(self):
        self.assertEqual(worktrees.sanitize("../../etc/passwd"), "etc-passwd")
        self.assertEqual(worktrees.sanitize("../../x"), "x")
        self.assertNotIn("..", worktrees.worktree_path("../../x", "../../y"))
        path = worktrees.worktree_path("crew", "a;rm -rf /")
        self.assertTrue(path.startswith(worktrees.root()))

    def test_clean_lists_worktrees_and_never_flags_dirty_as_removable(self):
        from crewhall import clean

        a = worktrees.create(self.repo, "crew", "alpha")

        def mine():
            return next(i for i in clean.list_worktrees()
                        if os.path.realpath(i["path"]) == os.path.realpath(a))

        self.assertTrue(mine()["removable"])
        with open(os.path.join(a, "dirty"), "w") as fh:
            fh.write("x")
        self.assertFalse(mine()["removable"])

    def test_non_git_repo_refused(self):
        plain = tempfile.mkdtemp(prefix="at-plain-")
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)
        self.assertFalse(worktrees.is_git_repo(plain))
        with self.assertRaises(worktrees.WorktreeError):
            worktrees.create(plain, "crew", "alpha")


if __name__ == "__main__":
    unittest.main()
