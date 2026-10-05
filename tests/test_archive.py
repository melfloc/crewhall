from __future__ import annotations

import os
import tempfile
import unittest

from agent_terminal import archive


class ArchiveStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-arch-")
        self._prev = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self.tmp
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._prev is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._prev
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_archive_then_list_and_read(self):
        archive.archive_agent(
            agent_id="sess_1", name="old", kind="claude", cwd="/x", teams=["t"],
            conversation_id="c1",
            messages=[{"sender": "sess_1", "sender_name": "old", "recipient": "sess_2",
                       "recipient_name": "new", "body": "bye", "agent_id": "sess_1"}],
        )
        listed = archive.list_archived()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "old")
        full = archive.read_archived("sess_1")
        self.assertEqual(full["conversation_id"], "c1")
        self.assertEqual(full["messages"][0]["body"], "bye")

    def test_invalid_id_is_rejected(self):
        with self.assertRaises(ValueError):
            archive.archive_agent(agent_id="../evil", name=None, kind="claude", cwd=None,
                                  teams=[], conversation_id=None, messages=[])
        self.assertIsNone(archive.read_archived("../evil"))

    def test_files_are_private(self):
        archive.archive_agent(agent_id="sess_2", name=None, kind="claude", cwd=None,
                              teams=[], conversation_id=None, messages=[])
        path = os.path.join(archive.archive_dir(), "sess_2.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
