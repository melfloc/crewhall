from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

from crewhall import audit


class Summarize(unittest.TestCase):
    def test_all_branches(self) -> None:
        cases = [
            ("settings_set", {"changes": {"a.env": "x", "b": 1}, "confirm": "CONFIRM"}),
            ("settings_reset", {"changes": {}}),
            ("provider_check", {"kind": "claude"}),
            ("agent_create", {"kind": "opencode", "name": "n", "team": "t"}),
            ("agent_stop", {"target": "a"}),
            ("agent_set_model", {"target": "a", "model": "m"}),
            ("conversation_publish", {"id": "c", "sender": "a", "name": "n"}),
            ("conversation_set_team", {"id": "c", "team": "t"}),
            ("message_send", {"sender": "a", "recipient": "b"}),
            ("team_create", {"name": "t", "agent": "a"}),
            ("request_create", {"request_id": "r"}),
            ("worktree_discard", {"path": "/p"}),
            ("interaction_respond", {"id": "i", "by": "u"}),
            ("agent_process_signal", {"target": "a", "pid": 1, "signal": "TERM"}),
            ("frontend_set", {"mode": "local", "port": 1}),
            ("bundle_create", {"name": "b"}),
            ("reset_apply", {"level": "full"}),
            ("fs_complete", {}),
            ("clean_plan", {}),
            ("conversation_create", {"id": "c", "title": "t", "agent_id": "a"}),
            ("conversation_rename", {"id": "c"}),
            ("conversation_delete", {"id": "c", "delete_files": True}),
            ("conversation_artifact_delete", {"id": "c", "path": "p"}),
            ("conversation_artifact_download", {"id": "c", "path": "p"}),
            ("office_collab_open", {"id": "c", "path": "p"}),
            ("office_collab_command", {"id": "c", "method": "m"}),
            ("office_collab_insert", {"id": "c", "path": "p", "text": "hello"}),
            ("office_start", {"action": "start"}),
            ("web_token_generate", {}),
            ("terminal_create", {"id": "t", "host": "h", "owner": "o"}),
            ("terminal_write", {"id": "t", "text": "ls", "enter": True}),
            ("terminal_key", {"id": "t", "key": "ENTER"}),
            ("terminal_ws_open", {"terminal_id": "t", "host": "h", "mode": "read", "token_id": "k"}),
            ("terminal_ws_close", {"terminal_id": "t", "duration": 1, "in_bytes": 2,
                                   "out_bytes": 3, "reason": "bye"}),
            ("web_terminal_token_issue", {"id": "i", "scope": "read"}),
            ("web_terminal_token_revoke", {"id": "i"}),
            ("web_terminal_unlock", {}),
            ("unknown_op", {}),
        ]
        for op, request in cases:
            text = audit.summarize(op, request, {"terminal": {"session_id": "s"}})
            self.assertIsInstance(text, str)
        self.assertNotIn("x", audit.summarize("settings_set", {"changes": {"a.env": "x"}}))


class RecordAndRead(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="at-audit-")
        self._patch = mock.patch.object(audit.paths, "state_dir", return_value=self.dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_record_recent_rotate(self) -> None:
        for i in range(5):
            audit.record("agent_create", actor="local", result="ok", summary=f"n{i}")
        entries = audit.recent(limit=10)
        self.assertEqual(len(entries), 5)
        self.assertTrue(audit.file_permissions_ok())
        self.assertIsInstance(audit._short_hash("secret"), str)

    def test_rotate_and_prune(self) -> None:
        with mock.patch.object(audit, "MAX_BYTES", 1), \
                mock.patch.object(audit, "KEEP", 1):
            audit.record("agent_create", summary="one")
            audit.record("agent_create", summary="two")
        self.assertTrue(os.path.exists(audit.audit_path() + ".1"))
        with mock.patch("crewhall.settings.get", return_value=0):
            audit.record("agent_create", summary="three")  # retention 0 = keep forever

    def test_recent_filters(self) -> None:
        audit.record("agent_create", summary="a")
        audit.record("team_create", summary="b")
        self.assertTrue(all(e["op"] == "agent_create" for e in audit.recent(op="agent_create")))
        self.assertTrue(all(e["result"] == "ok" for e in audit.recent(result="ok")))

    def test_file_permissions_false_when_loose(self) -> None:
        audit.record("agent_create", summary="x")
        os.chmod(audit.audit_path(), stat.S_IRWXU | stat.S_IRGRP)
        self.assertFalse(audit.file_permissions_ok())


if __name__ == "__main__":
    unittest.main()
