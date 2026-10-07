from __future__ import annotations

import os
import tempfile
import unittest

from crewhall import Controller
from crewhall.persistence import StateStore

from .support import FakeHarness, FakeSession


class PersistenceUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-state-")
        self.store = StateStore(path=os.path.join(self.tmp, "state.json"))

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _controller(self):
        c = Controller(adopt=False, persist=False)
        c._store = self.store
        return c

    def test_roundtrip_teams_and_agents(self):
        c = self._controller()
        a = FakeHarness("a")
        c.register_agent(a)
        c.create_team("fiscal", ["a"], workspace=self.tmp)
        self.store.save(self.store.snapshot(c))

        data = self.store.load()
        self.assertEqual(len(data["teams"]), 1)
        self.assertEqual(data["teams"][0]["workspace"], os.path.abspath(self.tmp))
        self.assertEqual(data["agents"][0]["agent_id"], a.agent_id)
        self.assertEqual(data["agents"][0]["name"], "a")

    def test_no_tokens_persisted(self):
        c = self._controller()
        a = FakeHarness("a")
        c.register_agent(a)
        c._agent_token(a.agent_id)
        self.store.save(self.store.snapshot(c))
        raw = open(self.store.path, encoding="utf-8").read()
        self.assertNotIn("CREWHALL_TOKEN", raw)
        self.assertNotIn(c._agent_token(a.agent_id), raw)

    def test_restore_reconstructs_exited_agents(self):
        c = self._controller()
        a = FakeHarness("a")
        c.register_agent(a)
        c.create_team("fiscal", ["a"], workspace=self.tmp)
        self.store.save(self.store.snapshot(c))

        c2 = self._controller()
        c2.restore()
        self.assertEqual(c2.team_info("fiscal")["workspace"], os.path.abspath(self.tmp))
        restored = c2.get_agent("a")
        self.assertEqual(restored.agent_id, a.agent_id)
        self.assertFalse(restored.session.status.alive)

    def _state_with_agent(self, agent_id: str = "sess_scripts01") -> dict:
        return {
            "schema": 1, "teams": [], "requests": [], "terminals": [],
            "agents": [{
                "agent_id": agent_id, "name": "scripts", "kind": "opencode",
                "backend": "pty", "cwd": "/tmp", "cols": 120, "rows": 40,
                "args": [], "host": None, "conversation_id": None, "env": {},
            }],
        }

    def test_restore_reuses_adopted_live_session(self):
        c = self._controller()
        live = FakeSession(screen="Build · model\n ctrl+p commands", name="scripts")
        live.session_id = "sess_scripts01"
        live._adopted = True
        c.registry.add(live)
        self.store.save(self._state_with_agent())

        c.restore()
        harness = c.get_agent("scripts")
        self.assertIs(harness.session, live)
        self.assertTrue(harness.session.status.alive)

    def test_adoption_upgrades_exited_placeholder(self):
        c = self._controller()
        self.store.save(self._state_with_agent())
        c.restore()
        harness = c.get_agent("scripts")
        self.assertFalse(harness.session.status.alive)

        live = FakeSession(screen="Build · model\n ctrl+p commands", name="scripts")
        live.session_id = "sess_scripts01"
        live._adopted = True
        c._register_adopted(live)
        self.assertIs(harness.session, live)

    def test_load_missing_returns_empty(self):
        data = StateStore(path=os.path.join(self.tmp, "nope.json")).load()
        self.assertEqual(data["teams"], [])
        self.assertEqual(data["agents"], [])

    def test_load_corrupt_returns_empty(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        data = StateStore(path=path).load()
        self.assertEqual(data["teams"], [])


if __name__ == "__main__":
    unittest.main()
