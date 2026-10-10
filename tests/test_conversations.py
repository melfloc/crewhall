from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from crewhall import conversations


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-conv-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.d,
            "XDG_STATE_HOME": self.d,
        })
        env.start(); self.addCleanup(env.stop)


class Basics(_Base):
    def test_create_defaults_and_listing(self):
        meta = conversations.create("My chat", agent_id="a1")
        self.assertEqual(meta["title"], "My chat")
        self.assertEqual(meta["agent_id"], "a1")
        self.assertTrue(meta["id"].startswith("c-"))
        d = conversations.conversation_dir(meta["id"])
        for section in ("inputs", "outputs"):
            self.assertTrue(os.path.isdir(os.path.join(d, section)))
        items = conversations.list_all()
        self.assertEqual([m["id"] for m in items], [meta["id"]])
        self.assertEqual(conversations.get(meta["id"])["title"], "My chat")

    def test_rename_and_delete(self):
        meta = conversations.create("chat")
        conversations.rename(meta["id"], "Renamed")
        self.assertEqual(conversations.get(meta["id"])["title"], "Renamed")
        conversations.delete(meta["id"])
        self.assertEqual(conversations.list_all(), [])
        with self.assertRaises(conversations.ConversationError):
            conversations.get(meta["id"])

    def test_delete_with_files(self):
        meta = conversations.create("chat")
        conversations.store_input(meta["id"], "note.txt", b"hello")
        conversations.delete(meta["id"], delete_files=True)
        self.assertFalse(os.path.isdir(conversations.conversation_dir(meta["id"])))

    def test_agent_ids_isolate_chat_agents(self):
        a = conversations.create("one", agent_id="agent-1")
        b = conversations.create("two")
        self.assertEqual(conversations.agent_ids(), {"agent-1"})
        conversations.set_agent(b["id"], "agent-2")
        self.assertEqual(conversations.agent_ids(), {"agent-1", "agent-2"})
        conversations.delete(a["id"])
        self.assertEqual(conversations.agent_ids(), {"agent-2"})

    def test_invalid_id_rejected(self):
        for bad in ("../etc", "a/b", "", ".hidden", "x" * 100):
            with self.assertRaises(conversations.ConversationError):
                conversations.get(bad)


class Files(_Base):
    def test_store_input_is_private_and_listed(self):
        meta = conversations.create("chat")
        info = conversations.store_input(meta["id"], "../../evil name.txt", b"data")
        self.assertTrue(info["name"].startswith("evil name"))
        self.assertTrue(info["name"].endswith(".txt"))
        self.assertEqual(info["kind"], "office")
        path = conversations.resolve(meta["id"], "inputs", info["name"])
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        listed = conversations.list_files(meta["id"], "inputs")
        self.assertEqual([f["name"] for f in listed], [info["name"]])

    def test_windows_style_name_is_flattened(self):
        meta = conversations.create("chat")
        info = conversations.store_input(meta["id"], r"C:\Users\x\report.docx", b"x")
        self.assertTrue(info["name"].startswith("report"))
        self.assertTrue(info["name"].endswith(".docx"))

    def test_resolve_refuses_traversal_and_absolute(self):
        meta = conversations.create("chat")
        for bad in ("../meta.json", "/etc/passwd", "a/../../b", ".."):
            with self.assertRaises(conversations.ConversationError):
                conversations.resolve(meta["id"], "outputs", bad)
        with self.assertRaises(conversations.ConversationError):
            conversations.resolve(meta["id"], "nope", "x")

    def test_resolve_refuses_symlink_escape(self):
        meta = conversations.create("chat")
        outside = os.path.join(self.d, "secret.txt")
        with open(outside, "w") as fh:
            fh.write("secret")
        link = os.path.join(conversations.conversation_dir(meta["id"]), "outputs", "leak")
        os.symlink(outside, link)
        with self.assertRaises(conversations.ConversationError):
            conversations.resolve(meta["id"], "outputs", "leak")

    def test_write_artifact_roundtrip(self):
        meta = conversations.create("chat")
        conversations.write_artifact(meta["id"], "sub/doc.md", b"v1")
        path = conversations.resolve(meta["id"], "outputs", "sub/doc.md")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"v1")
        conversations.write_artifact(meta["id"], "sub/doc.md", b"v2")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"v2")

    def test_delete_artifact(self):
        meta = conversations.create("chat")
        conversations.write_artifact(meta["id"], "a.txt", b"x")
        conversations.delete_artifact(meta["id"], "a.txt")
        self.assertEqual(conversations.list_files(meta["id"], "outputs"), [])
        with self.assertRaises(conversations.ConversationError):
            conversations.delete_artifact(meta["id"], "a.txt")

    def test_info_aggregates(self):
        meta = conversations.create("chat")
        conversations.store_input(meta["id"], "in.txt", b"a")
        conversations.write_artifact(meta["id"], "out.docx", b"b")
        info = conversations.info(meta["id"])
        self.assertEqual(len(info["inputs"]), 1)
        self.assertEqual(len(info["outputs"]), 1)
        self.assertTrue(info["inputs"][0]["name"].endswith(".txt"))
        self.assertTrue(info["outputs"][0]["office"])

    def test_env_pointers(self):
        meta = conversations.create("chat")
        env = conversations.env(meta["id"])
        self.assertEqual(env["CREWHALL_CONVERSATION"], meta["id"])
        self.assertTrue(env["CREWHALL_OUTPUTS"].endswith(os.path.join(meta["id"], "outputs")))
        self.assertTrue(env["CREWHALL_WORKSPACE"].endswith(os.path.join(meta["id"], "workspace")))

    def test_workspace_is_isolated_inside_the_conversation(self):
        meta = conversations.create("chat")
        ws = conversations.workspace(meta["id"])
        self.assertTrue(os.path.isdir(ws))
        root = os.path.realpath(conversations.conversation_dir(meta["id"]))
        self.assertTrue(os.path.realpath(ws).startswith(root + os.sep))
        # The workspace is scratch space: it is never listed as an artifact.
        conversations.write_artifact(meta["id"], "real.txt", b"x")
        with open(os.path.join(ws, "scratch.tmp"), "w") as fh:
            fh.write("junk")
        self.assertEqual([f["name"] for f in conversations.list_files(meta["id"], "outputs")],
                         ["real.txt"])
        self.assertEqual(conversations.list_files(meta["id"], "inputs"), [])


if __name__ == "__main__":
    unittest.main()
