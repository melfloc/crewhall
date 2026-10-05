from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest

from agent_terminal import audit, settings


class AuditUnit(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_state = os.environ.get("XDG_STATE_HOME")
        self._prev_config = os.environ.get("XDG_CONFIG_HOME")
        self.root = tempfile.mkdtemp(prefix="at-audit-")
        os.environ["XDG_STATE_HOME"] = os.path.join(self.root, "state")
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.root, "config")

    def tearDown(self) -> None:
        for var, prev in (("XDG_STATE_HOME", self._prev_state),
                          ("XDG_CONFIG_HOME", self._prev_config)):
            if prev is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prev
        shutil.rmtree(self.root, ignore_errors=True)

    def test_record_recent_and_filter(self):
        audit.record("settings_set", actor="web:1.2.3.4", summary="keys=agents.hooks")
        audit.record("agent_create", actor="web:1.2.3.4", summary="kind=claude")
        events = audit.recent()
        self.assertEqual(events[0]["op"], "agent_create")
        self.assertEqual(events[0]["actor"], "web:1.2.3.4")
        only_settings = audit.recent(op="settings_set")
        self.assertEqual([e["op"] for e in only_settings], ["settings_set"])
        self.assertEqual(audit.recent(result="error"), [])

    def test_file_is_0600_and_summaries_never_carry_secret_values(self):
        secret = "sk-super-secret-value"
        summary = audit.summarize(
            "settings_set",
            {"changes": {"providers.claude.env": secret}, "confirm": "CONFIRM"},
        )
        audit.record("settings_set", actor="local", summary=summary)
        with open(audit.audit_path(), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(secret, text)
        self.assertIn("providers.claude.env", text)  # the key is recorded, not the value
        self.assertEqual(stat.S_IMODE(os.stat(audit.audit_path()).st_mode), 0o600)
        self.assertTrue(audit.file_permissions_ok())

    def test_rotation_by_size_keeps_a_generation(self):
        old_max = audit.MAX_BYTES
        audit.MAX_BYTES = 200
        self.addCleanup(setattr, audit, "MAX_BYTES", old_max)
        for i in range(40):
            audit.record("op", summary=f"event-{i}-" + "x" * 40)
        self.assertTrue(os.path.exists(audit.audit_path() + ".1"))
        self.assertTrue(audit.recent(limit=1000))

    def test_describe_never_contains_a_seeded_secret(self):
        settings.patch({"providers.claude.env": "ANTHROPIC_API_KEY=sk-super-secret-value"},
                       confirm=True)
        blob = json.dumps(settings.describe(), ensure_ascii=False)
        self.assertNotIn("sk-super-secret-value", blob)
        self.assertIn(settings.MASK, blob)
        # Audit events (created by the daemon in production) are secret-free too.
        audit.record("settings_set", summary=audit.summarize(
            "settings_set", {"changes": {"providers.claude.env": "sk-super-secret-value"}}))
        self.assertNotIn("sk-super-secret-value", json.dumps(audit.recent()))


if __name__ == "__main__":
    unittest.main()
