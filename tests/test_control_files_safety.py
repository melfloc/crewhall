"""The CLAUDE.md/AGENTS.md injection must never alter anything but its own block."""
from __future__ import annotations

import os
import random
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

from crewhall.control_files import BEGIN, END, ensure_managed_section, managed_section

B, E = BEGIN.encode(), END.encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="at-cf-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(self.root, "state")})
        p.start()
        self.addCleanup(p.stop)
        self.proj = os.path.join(self.root, "proj")
        os.makedirs(self.proj)

    def write(self, data: bytes, name="CLAUDE.md") -> str:
        path = os.path.join(self.proj, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def read(self, name="CLAUDE.md") -> bytes:
        return open(os.path.join(self.proj, name), "rb").read()


class AppendOnly(Base):
    def test_first_injection_keeps_every_original_byte_as_a_prefix(self):
        rnd = random.Random(1234)
        alphabet = [b"a", b"\xc3\xa9", b"\n", b"\r\n", b" ", b"#", b"<!--", b"`", b"\xe9", b"\t", b"-->"]
        for i in range(300):
            data = b"".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 60)))
            if b"\x00" in data or B in data or E in data:
                continue
            path = self.write(data)
            result = ensure_managed_section(self.proj)
            after = self.read()
            self.assertTrue(after.startswith(data), f"case {i}: original not a prefix: {data!r}")
            self.assertIsNotNone(result)
            self.assertEqual(after.count(B), 1)
            self.assertEqual(after.count(E), 1)
            self.assertTrue(after.rstrip(b"\r\n").endswith(E), "block must be at the very end")
            os.unlink(path)

    def test_line_endings_are_followed_not_rewritten(self):
        data = b"# T\r\nuno\r\ndos\r\n"
        self.write(data)
        ensure_managed_section(self.proj)
        after = self.read()
        self.assertTrue(after.startswith(data))
        self.assertEqual(after.count(b"\r\n"), after.count(b"\n"))  # block is CRLF too
        self.write(b"# T\nuno\n")
        os.unlink(os.path.join(self.proj, "CLAUDE.md"))
        self.write(b"# T\nuno\n")
        ensure_managed_section(self.proj)
        self.assertNotIn(b"\r", self.read())

    def test_bom_non_utf8_and_missing_final_newline(self):
        for data in (b"\xef\xbb\xbf# x\n", b"caf\xe9 \xe8\n", b"sin final"):
            self.write(data)
            ensure_managed_section(self.proj)
            self.assertTrue(self.read().startswith(data), data)
            os.unlink(os.path.join(self.proj, "CLAUDE.md"))

    def test_second_call_does_not_write_at_all(self):
        path = self.write(b"# x\n")
        ensure_managed_section(self.proj)
        before = os.stat(path).st_mtime_ns
        result = ensure_managed_section(self.proj)
        self.assertTrue(result.unchanged)
        self.assertEqual(os.stat(path).st_mtime_ns, before)

    def test_file_mode_and_symlink_are_preserved(self):
        target = os.path.join(self.root, "shared.md")
        open(target, "wb").write(b"# shared\n")
        os.chmod(target, 0o640)
        link = os.path.join(self.proj, "CLAUDE.md")
        os.symlink(target, link)
        ensure_managed_section(self.proj)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), target)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o640)
        self.assertTrue(open(target, "rb").read().startswith(b"# shared\n"))
        # an in-place refresh (atomic replace) must not break the link either
        data = open(target, "rb").read().replace(b"collaboration", b"colaboracion")
        open(target, "wb").write(data)
        ensure_managed_section(self.proj)
        self.assertTrue(os.path.islink(link))
        self.assertIn(b"collaboration", open(target, "rb").read())

    def test_backup_of_the_original_is_kept(self):
        self.write(b"# original\n")
        ensure_managed_section(self.proj)
        backups = []
        for d, _, files in os.walk(os.path.join(self.root, "state")):
            backups += [os.path.join(d, f) for f in files if f.endswith("CLAUDE.md")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(open(backups[0], "rb").read(), b"# original\n")

    def test_unwritable_file_is_left_alone_without_raising(self):
        path = self.write(b"# ro\n")
        os.chmod(path, 0o444)
        if os.access(path, os.W_OK):  # running as root
            self.skipTest("cannot make the file unwritable")
        self.assertIsNone(ensure_managed_section(self.proj))
        self.assertEqual(self.read(), b"# ro\n")


class InPlaceRefresh(Base):
    def test_refresh_changes_only_the_block_even_when_it_is_not_last(self):
        stale = f"{BEGIN}\nold\n{END}".encode()
        data = b"# top\r\n\r\ntexto\xe9\r\n" + stale + b"\r\n\r\ndespues del bloque\r\n"
        self.write(data)
        result = ensure_managed_section(self.proj)
        self.assertTrue(result.updated)
        after = self.read()
        start = data.index(B)
        self.assertEqual(after[:start], data[:start])
        tail = b"\r\n\r\ndespues del bloque\r\n"
        self.assertTrue(after.endswith(tail))
        self.assertNotIn(b"old", after)
        self.assertIn(b"Agent-terminal collaboration", after)

    def test_ambiguous_markers_leave_the_file_byte_identical(self):
        cases = {
            "quoted in prose": f"Mira {BEGIN} y {END} en esta frase\n".encode(),
            "lone begin": f"a\n{BEGIN}\nb\n".encode(),
            "lone end": f"a\n{END}\nb\n".encode(),
            "duplicated": f"{BEGIN}\nx\n{END}\n{BEGIN}\ny\n{END}\n".encode(),
            "end before begin": f"{END}\nuser\n{BEGIN}\n".encode(),
            "text after end": f"{BEGIN}\nx\n{END} trailing prose\n".encode(),
            "binary": b"\x00\x01binary",
        }
        for label, data in cases.items():
            self.write(data)
            result = ensure_managed_section(self.proj)
            self.assertEqual(self.read(), data, label)
            self.assertIsNotNone(result.skipped, label)
            os.unlink(os.path.join(self.proj, "CLAUDE.md"))


class Concurrency(Base):
    def test_many_agents_starting_at_once_inject_exactly_one_block(self):
        self.write(b"# proyecto importante\n")
        errors = []

        def go():
            try:
                ensure_managed_section(self.proj)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go) for _ in range(16)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        after = self.read()
        self.assertEqual(after.count(B), 1)
        self.assertTrue(after.startswith(b"# proyecto importante\n"))


class WhichFile(Base):
    def test_each_kind_gets_the_file_it_actually_reads(self):
        self.write(b"# c\n", "CLAUDE.md")
        self.write(b"# a\n", "AGENTS.md")
        ensure_managed_section(self.proj, kind="claude")
        ensure_managed_section(self.proj, kind="opencode")
        self.assertIn(B, self.read("CLAUDE.md"))
        self.assertIn(B, self.read("AGENTS.md"))

    def test_creates_the_right_file_when_none_exists(self):
        ensure_managed_section(self.proj, kind="opencode")
        self.assertTrue(os.path.exists(os.path.join(self.proj, "AGENTS.md")))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "CLAUDE.md")))

    def test_opencode_falls_back_to_an_existing_claude_md(self):
        self.write(b"# c\n", "CLAUDE.md")
        ensure_managed_section(self.proj, kind="opencode")
        self.assertIn(B, self.read("CLAUDE.md"))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "AGENTS.md")))

    def test_create_false_never_creates_and_env_off_disables_everything(self):
        self.assertIsNone(ensure_managed_section(self.proj, create=False))
        self.assertEqual(os.listdir(self.proj), [])
        self.write(b"# x\n")
        with mock.patch.dict(os.environ, {"CREWHALL_CONTROL_FILES": "off"}):
            self.assertIsNone(ensure_managed_section(self.proj))
        self.assertEqual(self.read(), b"# x\n")

    def test_the_injected_text_documents_the_current_rules(self):
        self.assertIn("[from: <sender>]", managed_section())


if __name__ == "__main__":
    unittest.main()
