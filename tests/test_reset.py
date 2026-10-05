from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from crewhall import Controller, OpenCodeHarness, paths, reset

from .support import FakeSession

SCREEN = '  Build · model\n  Ask anything… "x"\n  ctrl+p commands'


class FakeFrontends:
    def __init__(self):
        self.calls = []

    def mode(self):
        return "local"

    def set_mode(self, mode, port=None, *, persist=True):
        self.calls.append((mode, persist))


class Reset(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-reset-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.d, "cfg"),
                                           "XDG_STATE_HOME": os.path.join(self.d, "state")})
        env.start(); self.addCleanup(env.stop)
        self.c = Controller(adopt=False)
        self.fe = FakeFrontends()
        tmp = paths.tmpdir_root()
        os.makedirs(tmp, exist_ok=True)
        self.old = os.path.join(tmp, "old.so"); self.new = os.path.join(tmp, "new.so")
        for p in (self.old, self.new):
            open(p, "wb").write(b"x" * 2048)
        past = time.time() - 3600
        os.utime(self.old, (past, past))

    def _agent(self, name):
        h = OpenCodeHarness(FakeSession(screen=SCREEN, name=name), name=name)
        self.c.register_agent(h)
        return h

    def test_plan_describes_each_level_and_only_full_needs_confirmation(self):
        self._agent("a")
        clean = reset.plan(self.c, "clean")
        self.assertFalse(clean["needs_confirm"])
        self.assertNotIn("backup", [s["id"] for s in clean["steps"]])
        full = reset.plan(self.c, "full")
        ids = [s["id"] for s in full["steps"]]
        self.assertEqual((ids[0], ids[-1]), ("backup", "restart"))
        self.assertTrue(full["needs_confirm"]); self.assertEqual(full["confirm_word"], "RESET")
        self.assertEqual(full["live_agents"][0]["name"], "a")
        with self.assertRaises(ValueError):
            reset.plan(self.c, "nuke")

    def test_clean_removes_only_stale_temp_and_keeps_agents_alive(self):
        h = self._agent("a")
        out = reset.apply(self.c, self.fe, "clean")
        self.assertFalse(os.path.exists(self.old)); self.assertTrue(os.path.exists(self.new))
        self.assertFalse(h.session.closed)
        self.assertEqual(out["errors"], []); self.assertFalse(out["restart"])
        self.assertEqual(self.fe.calls, [])

    def test_services_bounces_the_web_ui_and_signs_browsers_out(self):
        from crewhall.web import auth

        auth.issue_session(ip="1.2.3.4")
        out = reset.apply(self.c, self.fe, "services")
        self.assertEqual(self.fe.calls, [("off", False), ("local", False)])
        self.assertEqual(auth.list_sessions(), [])
        self.assertFalse(out["restart"])

    def test_full_backs_up_first_stops_agents_wipes_temp_and_asks_for_a_restart(self):
        os.makedirs(paths.state_dir(), exist_ok=True)
        open(paths.state_path(), "w").write('{"schema": 1, "teams": [], "agents": []}')
        h = self._agent("a")
        out = reset.apply(self.c, self.fe, "full")
        self.assertTrue(out["restart"]); self.assertEqual(out["errors"], [])
        self.assertTrue(out["backup"] and os.path.isfile(out["backup"]))
        self.assertIn("pre-reset-", os.path.basename(out["backup"]))
        self.assertFalse(h.session.status.alive)
        self.assertFalse(os.path.exists(self.new))  # a full reset clears the whole temp dir

    def test_full_can_forget_teams_and_agents_and_restore_default_settings(self):
        from crewhall import settings

        os.makedirs(paths.state_dir(), exist_ok=True)
        open(paths.state_path(), "w").write('{"schema": 1, "teams": [], "agents": []}')
        self._agent("a")
        self.c.create_team("t", [])
        settings.patch({"security.session_ttl_hours": 3})
        reset.apply(self.c, self.fe, "full", forget_state=True, reset_settings=True)
        self.assertEqual(self.c.list_agents(), []); self.assertEqual(self.c.list_teams(), [])
        self.assertEqual(settings.get("security.session_ttl_hours"), 12)

    def test_without_a_backup_nothing_is_destroyed(self):
        h = self._agent("a")
        self.assertTrue(h.session.status.alive)
        with mock.patch.object(reset, "_backup", side_effect=OSError("disk full")):
            out = reset.apply(self.c, self.fe, "full")
        self.assertFalse(out["restart"]); self.assertTrue(out["errors"])
        self.assertTrue(h.session.status.alive); self.assertTrue(os.path.exists(self.new))

    def test_one_failing_step_does_not_stop_the_rest(self):
        with mock.patch.object(reset, "_clear_caches", side_effect=RuntimeError("boom")):
            out = reset.apply(self.c, self.fe, "clean")
        self.assertEqual(len(out["errors"]), 1)
        self.assertFalse(os.path.exists(self.old))


if __name__ == "__main__":
    unittest.main()
