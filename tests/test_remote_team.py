"""Teams that work on a remote SSH host: host + a workspace on that host."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from crewhall import bundle, settings, specs
from crewhall.controller import Controller
from crewhall.team import KEEP, Team, TeamError, TeamRegistry, validate_remote_workspace


def _agent(agent_id, host=None):
    return SimpleNamespace(agent_id=agent_id, name=agent_id,
                           session=SimpleNamespace(spec=SimpleNamespace(host=host)))


class _Agents:
    def __init__(self, *agents):
        self._by = {a.agent_id: a for a in agents}

    def resolve(self, target):
        try:
            return self._by[target]
        except KeyError:
            raise KeyError(target) from None


class RemoteTeamRegistryTests(unittest.TestCase):
    def setUp(self):
        self.agents = _Agents(_agent("r1", "prod"), _agent("r2", "prod"),
                              _agent("o1", "stage"), _agent("l1", None))
        self.reg = TeamRegistry(self.agents)
        self.checked: list[tuple[str, str]] = []
        self.reg.remote_validator = lambda host, path: self.checked.append((host, path))

    def test_remote_workspace_is_checked_on_the_host_not_on_this_machine(self):
        team = self.reg.create("t", [], workspace="/srv/definitely/not/local", host="prod")
        self.assertEqual((team.host, team.workspace), ("prod", "/srv/definitely/not/local"))
        self.assertEqual(self.checked, [("prod", "/srv/definitely/not/local")])
        self.assertFalse(os.path.exists("/srv/definitely/not/local"))

    def test_remote_workspace_syntax(self):
        self.assertEqual(validate_remote_workspace("/a//b/../c"), "/a/c")
        for bad in ("relative/dir", "~/x", "", "/a\nb", "/a\0b", "/" + "x" * 5000):
            with self.assertRaises(TeamError):
                validate_remote_workspace(bad)
        with self.assertRaises(TeamError):
            self.reg.create("t", [], workspace="rel", host="prod")

    def test_a_failing_host_check_rejects_the_team(self):
        def deny(host, path):
            raise TeamError("does not exist on prod")
        self.reg.remote_validator = deny
        with self.assertRaises(TeamError):
            self.reg.create("t", [], workspace="/nope", host="prod")
        self.assertEqual(self.reg.list(), [])

    def test_restore_never_needs_the_host(self):
        self.reg.remote_validator = mock.Mock(side_effect=AssertionError("must not be called"))
        team = self.reg.create("t", [], workspace="/srv/x", host="prod", verify_remote=False)
        self.assertEqual(team.host, "prod")

    def test_only_agents_of_the_team_host_may_join(self):
        team = self.reg.create("t", ["r1"], workspace="/srv/x", host="prod")
        self.reg.add_member(team.team_id, "r2")
        for other in ("o1", "l1"):
            with self.assertRaises(TeamError):
                self.reg.add_member(team.team_id, other)
        with self.assertRaises(TeamError):
            self.reg.create("bad", ["r1", "o1"], host="prod")
        # a local team keeps accepting everything (unchanged behaviour)
        local = self.reg.create("loc", ["r1", "l1", "o1"])
        self.assertIsNone(local.host)

    def test_host_change_only_while_empty_and_membership_edits_keep_settings(self):
        team = self.reg.create("t", [], workspace="/srv/x", host="prod", workspace_mode="worktree")
        moved = self.reg.set_workspace(team.team_id, "/srv/y", host="stage")
        self.assertEqual((moved.host, moved.workspace), ("stage", "/srv/y"))
        back = self.reg.set_workspace(team.team_id, None, host=None)
        self.assertIsNone(back.host)
        self.assertIsNone(back.workspace)
        keep = self.reg.create("k", ["r1"], workspace="/srv/z", host="prod", workspace_mode="worktree")
        with self.assertRaises(TeamError):
            self.reg.set_workspace(keep.team_id, "/srv/z", host=None)
        same = self.reg.set_workspace(keep.team_id, "/srv/w")  # KEEP: host unchanged
        self.assertEqual(same.host, "prod")
        # regression: a membership change used to drop workspace_mode (and would drop host)
        after = self.reg.add_member(keep.team_id, "r2")
        self.assertEqual((after.workspace_mode, after.host), ("worktree", "prod"))
        after = self.reg.remove_member(keep.team_id, "r2")
        self.assertEqual((after.workspace_mode, after.host), ("worktree", "prod"))
        self.assertIs(KEEP, KEEP)

    def test_to_dict_exposes_host(self):
        self.assertEqual(Team("t", "n", (), 1.0, host="prod").to_dict()["host"], "prod")


class RemoteTeamSpecTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ati-rts-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.root})
        env.start(); self.addCleanup(env.stop)
        settings.patch({"hosts": {"prod": {"ssh": "u@prod"}, "stage": {"ssh": "u@stage"}}})

    def _parse(self, team, agents):
        return specs.parse_team_data({"team": team, "agent": agents}, "/local/base", {})

    def test_agents_inherit_host_and_resolve_paths_on_the_host(self):
        spec = self._parse({"name": "t", "host": "prod", "workspace": "/srv/app/../app"},
                           [{"name": "a"}, {"name": "b", "cwd": "sub/dir"}, {"name": "c", "cwd": "/abs/x"}])
        self.assertEqual(spec["host"], "prod")
        self.assertEqual(spec["workspace"], "/srv/app")
        self.assertEqual([a["host"] for a in spec["agents"]], ["prod"] * 3)
        self.assertEqual([a.get("cwd") for a in spec["agents"]], [None, "/srv/app/sub/dir", "/abs/x"])
        self.assertEqual({a["backend"] for a in spec["agents"]}, {"ssh-tmux"})

    def test_invalid_remote_team_specs(self):
        bad = [
            ({"name": "t", "host": "nope"}, [{"name": "a"}]),
            ({"name": "t", "host": "prod", "workspace": "relative"}, [{"name": "a"}]),
            ({"name": "t", "host": "prod"}, [{"name": "a", "host": "stage"}]),
            ({"name": "t", "host": "prod"}, [{"name": "a", "cwd": "rel"}]),
            ({"name": "t", "host": "prod"}, [{"name": "a", "backend": "pty"}]),
        ]
        for team, agents in bad:
            with self.assertRaises(specs.SpecError, msg=str((team, agents))):
                self._parse(team, agents)

    def test_local_team_specs_are_unchanged(self):
        spec = self._parse({"name": "t", "workspace": "ws"}, [{"name": "a", "cwd": "x"}])
        self.assertIsNone(spec["host"])
        self.assertEqual(spec["workspace"], "/local/base/ws")
        self.assertEqual(spec["agents"][0]["cwd"], "/local/base/x")


class RemoteTeamBundleTests(unittest.TestCase):
    def test_export_keeps_the_team_host_and_import_does_not_check_remote_dirs_locally(self):
        with mock.patch.object(bundle, "config_dir", return_value=tempfile.mkdtemp(prefix="ati-bd-", dir="/tmp")):
            files = bundle.live_team_files([{"name": "T", "workspace": "/srv/app", "host": "prod",
                                             "agents": [{"name": "a", "kind": "claude", "host": "prod"}]}])
        body = next(iter(files.values())).decode()
        self.assertIn('"host": "prod"', body)
        calls = []
        spec = {"name": "T", "workspace": "/srv/not/local", "host": "prod",
                "agents": [{"name": "a", "host": "prod"}]}
        out = bundle.apply_specs([spec], lambda s: calls.append(s) or {"created": ["a"], "existing": []})
        self.assertEqual(out["skipped"], [])
        self.assertEqual(len(calls), 1)
        plan = bundle.plan_specs([spec])
        self.assertTrue(plan[0]["agents"][0]["cwd_ok"])
        self.assertEqual(plan[0]["agents"][0]["host"], "prod")
        # a *local* missing directory is still skipped
        local = {"name": "L", "workspace": "/nowhere/local", "agents": [{"name": "b"}]}
        self.assertEqual(len(bundle.apply_specs([local], lambda s: {})["skipped"]), 1)


class RemoteTeamControllerTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ati-rtc-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start(); self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        settings.patch({"hosts": {"prod": {"ssh": "u@prod"}}})
        self.c = Controller(adopt=False, persist=False)
        self.c.teams.remote_validator = lambda host, path: None

    def test_unknown_host_is_rejected_up_front(self):
        with self.assertRaises(settings.SettingsError):
            self.c.create_team("t", [], workspace="/srv/x", host="ghost")

    def test_a_remote_team_rejects_an_agent_of_another_host_or_pty(self):
        self.c.create_team("t", [], workspace="/srv/x", host="prod")
        with self.assertRaisesRegex(ValueError, "runs on host 'prod'"):
            self.c.create_agent("claude", name="x", team="t", host="stage")
        with self.assertRaisesRegex(ValueError, "pty"):
            self.c.create_agent("claude", name="x", team="t", backend="pty")

    def test_persistence_roundtrip_keeps_host_without_touching_the_network(self):
        from crewhall import persistence

        team = self.c.create_team("t", [], workspace="/srv/not/local", host="prod",
                                  workspace_mode="worktree")
        snap = persistence.StateStore(os.path.join(self.root, "state.json")).snapshot(self.c)
        row = next(t for t in snap["teams"] if t["team_id"] == team.team_id)
        self.assertEqual((row["host"], row["workspace"], row["workspace_mode"]),
                         ("prod", "/srv/not/local", "worktree"))
        fresh = Controller(adopt=False, persist=False)
        fresh.teams.remote_validator = mock.Mock(side_effect=AssertionError("restore must not use SSH"))
        fresh._store = SimpleNamespace(load=lambda: snap)
        fresh.restore()
        restored = fresh.teams.get("t")
        self.assertEqual((restored.host, restored.workspace, restored.workspace_mode),
                         ("prod", "/srv/not/local", "worktree"))

    def test_live_definitions_and_workspace_op_carry_the_host(self):
        self.c.create_team("t", [], workspace="/srv/x", host="prod")
        self.assertEqual(self.c.live_team_defs()[0]["host"], "prod")
        updated = self.c.set_team_workspace("t", "/srv/y")
        self.assertEqual((updated.host, updated.workspace), ("prod", "/srv/y"))


if __name__ == "__main__":
    unittest.main()
