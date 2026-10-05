from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from crewhall import fscomplete


class Complete(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-fsc-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        for sub in ("alpha", "alpha/inner", "Alps", "beta", ".hidden"):
            os.makedirs(os.path.join(self.d, sub))
        open(os.path.join(self.d, "afile.txt"), "w").close()
        os.symlink(os.path.join(self.d, "beta"), os.path.join(self.d, "alink"))
        os.symlink(os.path.join(self.d, "afile.txt"), os.path.join(self.d, "alinkfile"))

    def names(self, prefix):
        return [e["name"] for e in fscomplete.complete(prefix)["entries"]]

    def test_trailing_slash_lists_children_directories_only(self):
        self.assertEqual(self.names(self.d + "/"), ["alink", "alpha", "Alps", "beta"])  # no files, no hidden

    def test_partial_name_filters_case_insensitively_exact_case_first(self):
        self.assertEqual(self.names(self.d + "/al"), ["alink", "alpha", "Alps"])
        self.assertEqual(self.names(self.d + "/Al"), ["Alps", "alink", "alpha"])

    def test_paths_end_in_a_slash_so_the_next_level_can_be_listed(self):
        e = fscomplete.complete(self.d + "/alp")["entries"][0]
        self.assertEqual(e["path"], os.path.join(self.d, "alpha") + "/")
        self.assertEqual(self.names(e["path"]), ["inner"])

    def test_hidden_only_when_asked(self):
        self.assertEqual(self.names(self.d + "/."), [".hidden"])

    def test_tilde_and_empty_mean_home(self):
        home = os.path.expanduser("~") + "/"
        self.assertEqual(fscomplete.complete("~/")["base"], home)
        self.assertEqual(fscomplete.complete("")["base"], home)

    def test_missing_dir_and_bad_input_do_not_raise(self):
        r = fscomplete.complete(self.d + "/nope/x")
        self.assertEqual(r["entries"], []); self.assertTrue(r["error"])
        self.assertTrue(fscomplete.complete("a\0b")["error"])
        self.assertTrue(fscomplete.complete("x" * 2000)["error"])

    def test_dotdot_is_normalised_and_results_are_capped(self):
        self.assertEqual(fscomplete.complete(self.d + "/alpha/../")["base"], self.d + "/")
        for i in range(fscomplete.LIMIT + 5):
            os.makedirs(os.path.join(self.d, f"many{i:03d}"))
        r = fscomplete.complete(self.d + "/many")
        self.assertEqual(len(r["entries"]), fscomplete.LIMIT); self.assertTrue(r["truncated"])


if __name__ == "__main__":
    unittest.main()
