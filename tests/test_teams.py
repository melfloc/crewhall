from __future__ import annotations

import unittest

from crewhall import Controller, Team, TeamError, TeamNotFound
from crewhall.team import new_team_id

from .support import FakeHarness


class TeamModel(unittest.TestCase):
    def test_ids_unique_and_prefixed(self) -> None:
        ids = {new_team_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(i.startswith("team_") for i in ids))

    def test_to_dict(self) -> None:
        team = Team("team_x", "research", ("sess_a", "sess_b"), 1234.5)
        self.assertEqual(
            team.to_dict(),
            {
                "team_id": "team_x",
                "name": "research",
                "agent_ids": ["sess_a", "sess_b"],
                "created_at": 1234.5,
                "workspace": None,
                "workspace_mode": None,
            },
        )


class TeamRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)
        self.a = self._add("a")
        self.b = self._add("b")
        self.c = self._add("c")
        self.d = self._add("d")
        self.teams = self.controller.teams

    def _add(self, name: str) -> FakeHarness:
        harness = FakeHarness(name)
        self.controller.register_agent(harness)
        return harness

    def test_create_resolves_names_to_agent_ids(self) -> None:
        team = self.teams.create("research", ["a", "b"])
        self.assertEqual(team.name, "research")
        self.assertEqual(team.agent_ids, (self.a.agent_id, self.b.agent_id))
        self.assertIsInstance(team.created_at, float)
        self.assertTrue(team.team_id.startswith("team_"))

    def test_create_accepts_agent_ids(self) -> None:
        team = self.teams.create("by-id", [self.a.agent_id, self.c.agent_id])
        self.assertEqual(team.agent_ids, (self.a.agent_id, self.c.agent_id))

    def test_empty_team_allowed(self) -> None:
        team = self.teams.create("empty", [])
        self.assertEqual(team.agent_ids, ())
        self.assertEqual(self.teams.members(team), [])

    def test_add_member_updates_membership(self) -> None:
        team = self.teams.create("research", ["a", "b"])
        updated = self.teams.add_member("research", "c")
        self.assertEqual(
            updated.agent_ids, (self.a.agent_id, self.b.agent_id, self.c.agent_id)
        )
        self.assertIs(self.teams.get(team.team_id), updated)

    def test_add_member_unknown_agent(self) -> None:
        team = self.teams.create("t", ["a"])
        with self.assertRaises(TeamError):
            self.teams.add_member("t", "ghost")
        self.assertEqual(self.teams.get("t").agent_ids, team.agent_ids)

    def test_add_member_duplicate(self) -> None:
        team = self.teams.create("t", ["a", "b"])
        with self.assertRaises(TeamError):
            self.teams.add_member("t", "a")
        self.assertEqual(self.teams.get("t").agent_ids, team.agent_ids)

    def test_add_member_unknown_team(self) -> None:
        with self.assertRaises(TeamNotFound):
            self.teams.add_member("ghost-team", "a")

    def test_add_member_does_not_create_agent(self) -> None:
        self.teams.create("t", ["a"])
        self.teams.add_member("t", "c")
        self.assertEqual(len(self.controller.list_agents()), 4)

    def test_remove_member_updates_membership(self) -> None:
        team = self.teams.create("t", ["a", "b", "c"])
        updated = self.teams.remove_member("t", "b")
        self.assertEqual(updated.agent_ids, (self.a.agent_id, self.c.agent_id))
        self.assertIs(self.teams.get(team.team_id), updated)

    def test_remove_member_non_member(self) -> None:
        team = self.teams.create("t", ["a"])
        with self.assertRaises(TeamError):
            self.teams.remove_member("t", "b")
        self.assertEqual(self.teams.get("t").agent_ids, team.agent_ids)

    def test_remove_member_unknown_team(self) -> None:
        with self.assertRaises(TeamNotFound):
            self.teams.remove_member("ghost-team", "a")

    def test_remove_member_keeps_agent_running(self) -> None:
        self.teams.create("t", ["a", "b"])
        self.teams.remove_member("t", "b")
        self.assertFalse(self.b.session.closed)
        self.assertIs(self.controller.get_agent("b"), self.b)

    def test_remove_last_member_leaves_empty_team(self) -> None:
        team = self.teams.create("t", ["a"])
        updated = self.teams.remove_member("t", "a")
        self.assertEqual(updated.agent_ids, ())
        self.assertEqual(self.teams.members(updated), [])
        self.assertIs(self.teams.get(team.team_id), updated)
        self.assertIs(self.controller.get_agent("a"), self.a)

    def test_membership_moves_between_teams(self) -> None:
        self.teams.create("research", ["a", "b"])
        self.teams.create("dev", ["c", "d"])
        self.teams.remove_member("research", "b")
        self.teams.add_member("dev", "b")
        self.assertEqual(
            {m.name for m in self.teams.members(self.teams.get("research"))}, {"a"}
        )
        self.assertEqual(
            {m.name for m in self.teams.members(self.teams.get("dev"))},
            {"c", "d", "b"},
        )
        self.assertIs(self.controller.get_agent("b"), self.b)
        self.assertFalse(self.b.session.closed)

    def test_remove_agent_then_readd_same_target_fails(self) -> None:
        team = self.teams.create("t", ["a", "b"])
        removed_id = self.a.agent_id
        self.controller.remove_agent("a")
        self.assertEqual(self.teams.missing(team), [removed_id])
        with self.assertRaises(TeamError):
            self.teams.add_member("t", "a")

    def test_remove_missing_member_by_agent_id(self) -> None:
        # A missing member (registry cleared without remove_agent) can still be
        # dropped from the Team by its literal agent_id.
        team = self.teams.create("t", ["a", "b"])
        removed_id = self.a.agent_id
        self.controller.agents.remove(removed_id)
        self.assertEqual(self.teams.missing(team), [removed_id])
        updated = self.teams.remove_member("t", removed_id)
        self.assertEqual(updated.agent_ids, (self.b.agent_id,))
        self.assertEqual(self.teams.missing(updated), [])

    def test_membership_unchanged_on_failed_operation(self) -> None:
        self.teams.create("t", ["a", "b"])
        before = self.teams.get("t").agent_ids
        for target in ("ghost", "a"):
            with self.assertRaises(TeamError):
                self.teams.add_member("t", target)
        with self.assertRaises(TeamError):
            self.teams.remove_member("t", "d")
        self.assertEqual(self.teams.get("t").agent_ids, before)

    def test_messaging_intact_after_membership_change(self) -> None:
        self.teams.create("research", ["a", "b"])
        self.teams.remove_member("research", "b")
        self.teams.add_member("research", "b")
        delivery = self.controller.send_message("a", "b", "still works")
        self.assertTrue(delivery.delivered)
        self.assertEqual(self.b.session.writes, ["[from: a] still works"])

    def test_concurrent_membership_changes_consistent(self) -> None:
        import threading

        self.teams.create("pool", [])
        threads = [
            threading.Thread(target=self.teams.add_member, args=("pool", n))
            for n in ("a", "b", "c", "d")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = self.teams.get("pool")
        self.assertEqual(set(final.agent_ids), {h.agent_id for h in (self.a, self.b, self.c, self.d)})

    def test_duplicate_members_rejected(self) -> None:
        with self.assertRaises(TeamError):
            self.teams.create("dup", ["a", "a"])

    def test_canonical_duplicate_rejected(self) -> None:
        with self.assertRaises(TeamError):
            self.teams.create("dup", ["a", self.a.agent_id])

    def test_unknown_agent_rejected_atomically(self) -> None:
        with self.assertRaises(TeamError):
            self.teams.create("bad", ["a", "ghost"])
        self.assertEqual(self.teams.list(), [])

    def test_blank_name_rejected(self) -> None:
        with self.assertRaises(TeamError):
            self.teams.create("   ", ["a"])

    def test_get_by_id_name_and_prefix(self) -> None:
        team = self.teams.create("research", ["a", "b"])
        self.assertIs(self.teams.get(team.team_id), team)
        self.assertIs(self.teams.get("research"), team)
        self.assertIs(self.teams.get(team.team_id[:10]), team)

    def test_get_unknown_raises(self) -> None:
        with self.assertRaises(TeamNotFound):
            self.teams.get("ghost")

    def test_duplicate_names_ambiguous_by_name_but_ok_by_id(self) -> None:
        t1 = self.teams.create("same", ["a"])
        t2 = self.teams.create("same", ["b"])
        with self.assertRaises(TeamNotFound):
            self.teams.get("same")
        self.assertIs(self.teams.get(t1.team_id), t1)
        self.assertIs(self.teams.get(t2.team_id), t2)

    def test_list_and_remove(self) -> None:
        t1 = self.teams.create("t1", ["a"])
        t2 = self.teams.create("t2", ["b"])
        self.assertEqual({t.team_id for t in self.teams.list()}, {t1.team_id, t2.team_id})
        removed = self.teams.remove("t1")
        self.assertIs(removed, t1)
        self.assertEqual([t.team_id for t in self.teams.list()], [t2.team_id])

    def test_remove_does_not_stop_agents(self) -> None:
        team = self.teams.create("t", ["a", "b"])
        self.teams.remove(team.team_id)
        self.assertFalse(self.a.session.closed)
        self.assertFalse(self.b.session.closed)
        self.assertIs(self.controller.get_agent("a"), self.a)
        self.assertEqual(len(self.controller.list_agents()), 4)

    def test_members_return_live_agent_info(self) -> None:
        team = self.teams.create("t", ["a", "b"])
        members = self.teams.members(team)
        self.assertEqual({m.agent_id for m in members}, {self.a.agent_id, self.b.agent_id})
        self.assertEqual({m.name for m in members}, {"a", "b"})
        self.assertEqual({m.kind for m in members}, {"opencode"})

    def test_forget_agent_removes_from_all_teams(self) -> None:
        t1 = self.teams.create("one", ["a", "b"])
        t2 = self.teams.create("two", ["a"])
        self.teams.forget_agent(self.a.agent_id)
        self.assertEqual([m.name for m in self.teams.members(self.teams.get(t1.team_id))], ["b"])
        self.assertEqual(self.teams.members(self.teams.get(t2.team_id)), [])
        self.assertEqual(self.teams.missing(self.teams.get(t1.team_id)), [])
        self.assertEqual(self.teams.missing(self.teams.get(t2.team_id)), [])

    def test_remove_agent_does_not_leave_missing_member(self) -> None:
        self.teams.create("t", ["a", "b"])
        self.controller.remove_agent("a")
        team = self.teams.get("t")
        self.assertEqual([m.name for m in self.teams.members(team)], ["b"])
        self.assertEqual(self.teams.missing(team), [])

    def test_agent_gone_from_registry_marks_member_missing(self) -> None:
        # `missing` describes an agent that vanished without remove_agent
        # (e.g. registry cleared externally); remove_agent cleans Teams instead.
        team = self.teams.create("t", ["a", "b"])
        self.controller.agents.remove(self.a.agent_id)
        self.assertEqual([m.name for m in self.teams.members(team)], ["b"])
        self.assertEqual(self.teams.missing(team), [self.a.agent_id])
        self.assertIs(self.teams.get(team.team_id), team)

    def test_agent_can_belong_to_multiple_teams(self) -> None:
        t1 = self.teams.create("one", ["a", "b"])
        t2 = self.teams.create("two", ["a", "c"])
        self.assertIn(self.a.agent_id, t1.agent_ids)
        self.assertIn(self.a.agent_id, t2.agent_ids)

    def test_teams_are_isolated(self) -> None:
        t_ab = self.teams.create("ab", ["a", "b"])
        t_cd = self.teams.create("cd", ["c", "d"])
        ids_ab = {m.agent_id for m in self.teams.members(t_ab)}
        ids_cd = {m.agent_id for m in self.teams.members(t_cd)}
        self.assertEqual(ids_ab, {self.a.agent_id, self.b.agent_id})
        self.assertEqual(ids_cd, {self.c.agent_id, self.d.agent_id})
        self.assertTrue(ids_ab.isdisjoint(ids_cd))
        self.teams.remove(t_ab.team_id)
        self.assertEqual({m.agent_id for m in self.teams.members(t_cd)}, ids_cd)

    def test_messaging_unaffected_by_teams(self) -> None:
        self.teams.create("ab", ["a", "b"])
        delivery = self.controller.send_message("a", "b", "hello")
        self.assertTrue(delivery.delivered)
        self.assertEqual(self.b.session.writes, ["[from: a] hello"])


if __name__ == "__main__":
    unittest.main()

