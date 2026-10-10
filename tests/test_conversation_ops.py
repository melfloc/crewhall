from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import conversations
from crewhall.controller import Controller
from crewhall.daemon import Server


class ConversationOps(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ati-conv-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start(); self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.server = Server(Controller(adopt=False, persist=False),
                             os.path.join(self.root, "r", "d.sock"))

    def _call(self, op, **kw):
        return self.server.dispatch({"op": op, **kw})

    def test_create_list_info_rename_delete(self):
        created = self._call("conversation_create", title="First chat")
        self.assertTrue(created["ok"], created)
        cid = created["conversation"]["id"]

        conversations.write_artifact(cid, "notes.md", b"# hi")
        conversations.store_input(cid, "in.txt", b"x")

        listed = self._call("conversation_list")["conversations"]
        self.assertEqual([c["id"] for c in listed], [cid])
        self.assertEqual(listed[0]["outputs"], 1)
        self.assertEqual(listed[0]["inputs"], 1)

        info = self._call("conversation_info", id=cid)["conversation"]
        self.assertEqual(info["title"], "First chat")
        self.assertEqual([f["name"] for f in info["outputs"]], ["notes.md"])

        renamed = self._call("conversation_rename", id=cid, title="Renamed")
        self.assertEqual(renamed["conversation"]["title"], "Renamed")

        deleted = self._call("conversation_artifact_delete", id=cid, path="notes.md")
        self.assertTrue(deleted["artifact"]["deleted"])
        self.assertEqual(conversations.list_files(cid, "outputs"), [])

        gone = self._call("conversation_delete", id=cid)
        self.assertTrue(gone["deleted"])
        self.assertEqual(self._call("conversation_list")["conversations"], [])

    def test_unknown_conversation_is_an_error(self):
        res = self._call("conversation_info", id="c-nope")
        self.assertFalse(res["ok"])

    def test_set_agent_link(self):
        cid = self._call("conversation_create", title="chat")["conversation"]["id"]
        self.assertIsNone(conversations.get(cid)["agent_id"])
        self._call("conversation_set_agent", id=cid, agent_id="agent-1")
        self.assertEqual(conversations.get(cid)["agent_id"], "agent-1")


if __name__ == "__main__":
    unittest.main()
