from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from crewhall import adapter_gen


class AdapterGenerator(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="at-adaptergen-")
        os.makedirs(os.path.join(self.root, "crewhall", "harness"))
        os.makedirs(os.path.join(self.root, "tests", "fixtures", "screens"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_creates_skeleton_fixtures_and_contract_test(self):
        paths = adapter_gen.create("frobnicator", root=self.root)
        harness = os.path.join(self.root, "crewhall", "harness", "frobnicator.py")
        test = os.path.join(self.root, "tests", "test_frobnicator_contract.py")
        self.assertIn(harness, paths)
        self.assertIn(test, paths)
        self.assertTrue(os.path.isfile(harness))
        self.assertTrue(os.path.isfile(test))
        self.assertTrue(os.path.isfile(os.path.join(
            self.root, "tests", "fixtures", "screens", "frobnicator", "expected.json")))
        # It must be syntactically valid Python...
        with open(harness, encoding="utf-8") as fh:
            compile(fh.read(), harness, "exec")
        # ...and must NOT register itself anywhere.
        self.assertNotIn("HARNESSES", open(harness, encoding="utf-8").read())

    def test_refuses_to_overwrite_and_bad_kind(self):
        adapter_gen.create("frobnicator", root=self.root)
        with self.assertRaises(ValueError):
            adapter_gen.create("frobnicator", root=self.root)
        for bad in ("", "../evil", "Bad Kind", "1bad", "a"):
            with self.assertRaises(ValueError):
                adapter_gen.create(bad, root=self.root)


if __name__ == "__main__":
    unittest.main()
