from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from agent_terminal import ClaudeCodeHarness, Controller
from agent_terminal.specs import SpecError, apply_profile, load_profiles, load_team_spec

TEAM = """
[team]
name = "frente1"
workspace = "ws"

[[agent]]
name = "orq"
kind = "claude"
args = "--agent orq"

[[agent]]
name = "rev"
profile = "revisor"
"""

PROFILES = """
[profile.revisor]
kind = "claude"
args = "--agent reviewer"
"""


class Specs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="at-specs-")
        self.team = os.path.join(self.tmp, "team.toml")
        self.prof = os.path.join(self.tmp, "profiles.toml")
        open(self.team, "w").write(TEAM)
        open(self.prof, "w").write(PROFILES)

    def test_team_file_with_profile(self):
        spec = load_team_spec(self.team, load_profiles(self.prof))
        self.assertEqual(spec["name"], "frente1")
        self.assertEqual(spec["workspace"], os.path.join(self.tmp, "ws"))
        orq, rev = spec["agents"]
        self.assertEqual((orq["kind"], orq["args"]), ("claude", ["--agent", "orq"]))
        self.assertEqual((rev["kind"], rev["args"]), ("claude", ["--agent", "reviewer"]))

    def test_entry_overrides_profile(self):
        merged = apply_profile({"profile": "revisor", "args": "--x"}, load_profiles(self.prof))
        self.assertEqual(merged["args"], "--x")
        self.assertEqual(merged["kind"], "claude")

    def test_json_team_file(self):
        path = os.path.join(self.tmp, "t.json")
        json.dump({"team": {"name": "t"}, "agent": [{"name": "a", "kind": "claude"}]}, open(path, "w"))
        self.assertEqual(load_team_spec(path, {})["agents"][0]["name"], "a")

    def test_invalid_specs(self):
        bad = {
            "no team": "[[agent]]\nname='a'\n",
            "no agents": "[team]\nname='t'\n",
            "unknown key": "[team]\nname='t'\n[[agent]]\nname='a'\nshell='rm -rf /'\n",
            "dup": "[team]\nname='t'\n[[agent]]\nname='a'\n[[agent]]\nname='a'\n",
            "bad args": "[team]\nname='t'\n[[agent]]\nname='a'\nargs='--x \"oops'\n",
            "unknown profile": "[team]\nname='t'\n[[agent]]\nname='a'\nprofile='zzz'\n",
            "not toml": "[[[",
        }
        for label, text in bad.items():
            path = os.path.join(self.tmp, "bad.toml")
            open(path, "w").write(text)
            with self.assertRaises(SpecError, msg=label):
                load_team_spec(path, {})

    def test_profile_with_unknown_key_rejected(self):
        open(self.prof, "w").write("[profile.p]\nkind='claude'\nenv='x'\n")
        with self.assertRaises(SpecError):
            load_profiles(self.prof)


class TeamUp(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(
            ClaudeCodeHarness, "command", classmethod(lambda cls: ["sleep"])
        )
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp(prefix="at-up-")
        self.c = Controller(adopt=False, persist=False)
        self.addCleanup(self.c.shutdown)

    def spec(self):
        return {
            "name": "t", "workspace": self.tmp,
            "agents": [
                {"name": "a", "kind": "claude", "backend": "pty", "args": ["60"]},
                {"name": "b", "kind": "claude", "backend": "pty", "args": ["61"]},
            ],
        }

    def test_creates_team_and_agents_and_is_idempotent(self):
        first = self.c.team_up(self.spec())
        self.assertTrue(first["team_created"])
        self.assertEqual(first["created"], ["a", "b"])
        self.assertEqual(
            sorted(m["name"] for m in self.c.team_members("t")), ["a", "b"]
        )
        again = self.c.team_up(self.spec())
        self.assertFalse(again["team_created"])
        self.assertEqual((again["created"], again["existing"]), ([], ["a", "b"]))
        self.assertEqual(len(self.c.list_agents()), 2)

    def test_duplicate_agent_names_are_rejected(self):
        self.c.team_up(self.spec())
        with self.assertRaises(ValueError):
            self.c.create_agent("claude", name="a", backend="pty", cwd=self.tmp)


if __name__ == "__main__":
    unittest.main()
