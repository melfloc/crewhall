from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from crewhall import chat as conversations
from crewhall.chat import ConversationError


class ConversationsExtra(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="at-conv-")
        self._patch = mock.patch.object(conversations.settings, "get",
                                        side_effect=self._setting)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _setting(self, key, default=None):
        if key == "conversations.dir":
            return self.dir
        if key == "conversations.enabled":
            return True
        if key == "conversations.max_mb":
            return 1
        return default

    def test_valid_id_rejects_bad(self) -> None:
        with self.assertRaises(ConversationError):
            conversations.get("../etc/passwd")

    def test_root_must_be_absolute(self) -> None:
        with mock.patch.object(conversations.settings, "get",
                               side_effect=lambda k, d=None: "relative/dir"
                               if k == "conversations.dir" else d):
            with self.assertRaises(ConversationError):
                conversations.root()

    def test_unknown_section(self) -> None:
        cid = conversations.create("t")["id"]
        with self.assertRaises(ConversationError):
            conversations.resolve(cid, "nope", "x")

    def test_clean_name_fallbacks(self) -> None:
        self.assertEqual(conversations._clean_name(""), "file")
        self.assertEqual(conversations._clean_name("..."), "file")
        self.assertTrue(conversations._clean_name("a" * 500).startswith("a"))

    def test_unique_exhausted(self) -> None:
        with mock.patch.object(conversations.os.path, "exists", return_value=True):
            with self.assertRaises(ConversationError):
                conversations._unique(self.dir, "x.txt")

    def test_load_tolerates_garbage(self) -> None:
        with open(conversations._meta_path(), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(conversations.list_all(), [])

    def test_get_missing(self) -> None:
        with self.assertRaises(ConversationError):
            conversations.get("c-deadbeefdeadbeef")

    def test_set_agent_missing(self) -> None:
        with self.assertRaises(ConversationError):
            conversations.set_agent("c-deadbeefdeadbeef", "a")

    def test_set_team_missing(self) -> None:
        with self.assertRaises(ConversationError):
            conversations.set_team("c-deadbeefdeadbeef", "t")

    def test_rename_missing_and_empty(self) -> None:
        with self.assertRaises(ConversationError):
            conversations.rename("c-deadbeefdeadbeef", "x")
        cid = conversations.create("t")["id"]
        with self.assertRaises(ConversationError):
            conversations.rename(cid, "   ")

    def test_touch_and_delete(self) -> None:
        cid = conversations.create("t")["id"]
        conversations.touch(cid)
        conversations.touch("../bad")  # swallowed
        out = conversations.delete(cid, delete_files=True)
        self.assertTrue(out["deleted"])
        self.assertTrue(out["files_removed"])

    def test_delete_missing(self) -> None:
        with self.assertRaises(ConversationError):
            conversations.delete("c-deadbeefdeadbeef")

    def test_crew_conversations_and_agent_ids(self) -> None:
        conv = conversations.create("t", agent_id="agent-1", team="team-1")
        self.assertEqual([c["id"] for c in conversations.crew_conversations("team-1")],
                         [conv["id"]])
        self.assertIn("agent-1", conversations.agent_ids())

    def test_set_producer_invalid_path(self) -> None:
        cid = conversations.create("t")["id"]
        with self.assertRaises(ConversationError):
            conversations.set_producer(cid, "../evil", "a")
        conversations.set_producer(cid, "ok.txt", "a")
        self.assertEqual(conversations.producers(cid)["ok.txt"]["producer"], "a")

    def test_producers_garbage(self) -> None:
        cid = conversations.create("t")["id"]
        path = conversations._producers_path(cid)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertEqual(conversations.producers(cid), {})

    def test_store_wrong_type_and_too_large(self) -> None:
        cid = conversations.create("t")["id"]
        with self.assertRaises(ConversationError):
            conversations.store_input(cid, "x", "not bytes")  # type: ignore[arg-type]
        with self.assertRaises(ConversationError):
            conversations.store_artifact(cid, "x", b"x" * (2 * 1024 * 1024))
        with self.assertRaises(ConversationError):
            conversations.store_input(cid, "x", b"x" * (2 * 1024 * 1024))

    def test_resolve_errors(self) -> None:
        cid = conversations.create("t")["id"]
        for bad in ("/abs", "a/../b", "trailing/"):
            with self.assertRaises(ConversationError):
                conversations.resolve(cid, "outputs", bad)

    def test_write_artifact(self) -> None:
        cid = conversations.create("t")["id"]
        conversations.store_artifact(cid, "doc.txt", b"one")
        info = conversations.write_artifact(cid, "doc.txt", b"two")
        self.assertEqual(info["size"], 3)
        with self.assertRaises(ConversationError):
            conversations.write_artifact(cid, "doc.txt", b"x" * (2 * 1024 * 1024))

    def test_list_files_missing_section(self) -> None:
        self.assertEqual(conversations.list_files("c-deadbeefdeadbeef", "outputs"), [])

    def test_ensure_global_rules_survives_bad_root(self) -> None:
        with mock.patch.object(conversations, "ensure_rules",
                               side_effect=ConversationError("boom")):
            conversations.ensure_global_rules()  # must not raise

    def test_max_bytes_bad_setting(self) -> None:
        with mock.patch.object(conversations.settings, "get",
                               side_effect=lambda k, d=None: "notanint"
                               if k == "conversations.max_mb" else d):
            self.assertEqual(conversations.max_bytes(), 200 * 1024 * 1024)

    def test_save_error_raises_conversation_error(self) -> None:
        with mock.patch.object(conversations.os, "replace", side_effect=OSError("x")):
            with self.assertRaises(ConversationError):
                conversations.create("t")

    def test_get_ok(self) -> None:
        cid = conversations.create("Title")["id"]
        self.assertEqual(conversations.get(cid)["title"], "Title")


if __name__ == "__main__":
    unittest.main()
