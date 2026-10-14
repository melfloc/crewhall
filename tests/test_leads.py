from __future__ import annotations

import unittest

from crewhall.controller import Controller
from crewhall.messaging import MessagingError
from crewhall.team import TeamError
from tests.support import FakeHarness


class LeadsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)

    def _agent(self, name: str) -> FakeHarness:
        h = FakeHarness(name)
        self.controller.register_agent(h)
        return h


class LeadsModel(LeadsBase):
    def test_create_with_leads_and_info(self) -> None:
        self._agent("a")
        self._agent("b")
        team = self.controller.create_team("crew", ["a", "b"], leads=["a"])
        self.assertEqual(team.leads, (self.controller.agents.resolve("a").agent_id,))
        info = self.controller.team_info(team.team_id)
        self.assertEqual(info["leads"], list(team.leads))

    def test_set_leads_replaces_and_clears(self) -> None:
        a = self._agent("a")
        b = self._agent("b")
        team = self.controller.create_team("crew", ["a", "b"])
        self.assertEqual(team.leads, ())
        updated = self.controller.set_team_leads(team.team_id, ["b"])
        self.assertEqual(updated.leads, (b.agent_id,))
        cleared = self.controller.set_team_leads(team.team_id, [])
        self.assertEqual(cleared.leads, ())
        self.assertNotIn(a.agent_id, cleared.leads)

    def test_lead_must_be_member(self) -> None:
        self._agent("a")
        self._agent("outsider")
        team = self.controller.create_team("crew", ["a"])
        with self.assertRaises(TeamError):
            self.controller.set_team_leads(team.team_id, ["outsider"])

    def test_remove_member_drops_lead(self) -> None:
        a = self._agent("a")
        b = self._agent("b")
        team = self.controller.create_team("crew", ["a", "b"], leads=["a", "b"])
        updated = self.controller.remove_team_member(team.team_id, "a")
        self.assertEqual(updated.leads, (b.agent_id,))

    def test_to_dict_always_has_leads(self) -> None:
        self._agent("a")
        team = self.controller.create_team("crew", ["a"])
        self.assertEqual(team.to_dict()["leads"], [])


class LeadsPersistence(LeadsBase):
    def test_leads_survive_a_restart(self) -> None:
        controller = Controller(adopt=False, persist=True)
        a = FakeHarness("a")
        b = FakeHarness("b")
        controller.register_agent(a)
        controller.register_agent(b)
        team = controller.create_team("crew", ["a", "b"], leads=["b"])
        # A fresh controller restores the same state from disk.
        restored = Controller(adopt=False, persist=True)
        restored.restore()
        got = restored.team_info(team.team_id)
        self.assertEqual(got["leads"], [b.agent_id])

    def test_old_state_without_leads_still_loads(self) -> None:
        from crewhall.persistence import StateStore

        controller = Controller(adopt=False, persist=True)
        a = FakeHarness("a")
        controller.register_agent(a)
        team = controller.create_team("crew", ["a"])
        data = StateStore().load()
        for record in data.get("teams", []):
            record.pop("leads", None)  # simulate state written before this change
        StateStore().save(data)
        restored = Controller(adopt=False, persist=True)
        restored.restore()
        self.assertEqual(restored.team_info(team.team_id)["leads"], [])


class SpecLeads(unittest.TestCase):
    def test_team_spec_parses_leads(self) -> None:
        from crewhall.specs import parse_team_data

        spec = parse_team_data(
            {"team": {"name": "crew", "leads": ["lead"]},
             "agent": [{"name": "lead", "kind": "opencode"},
                       {"name": "worker", "kind": "opencode"}]},
            base="/tmp",
            profiles={},
        )
        self.assertEqual(spec["leads"], ["lead"])

    def test_team_spec_rejects_unknown_lead(self) -> None:
        from crewhall.specs import SpecError, parse_team_data

        with self.assertRaises(SpecError):
            parse_team_data(
                {"team": {"name": "crew", "leads": ["ghost"]},
                 "agent": [{"name": "lead", "kind": "opencode"}]},
                base="/tmp",
                profiles={},
            )


class LeadAuthorization(LeadsBase):
    def test_lead_may_act_on_member(self) -> None:
        lead = self._agent("lead")
        worker = self._agent("worker")
        self.controller.create_team("crew", ["lead", "worker"], leads=["lead"])
        self.controller._authorize_lead(lead, worker)  # no raise

    def test_non_lead_cannot_act(self) -> None:
        self._agent("lead")
        worker = self._agent("worker")
        other = self._agent("other")
        self.controller.create_team("crew", ["lead", "worker"], leads=["lead"])
        self.controller.register_agent(other)
        with self.assertRaises(MessagingError) as ctx:
            self.controller._authorize_lead(other, worker)
        self.assertIn("not a lead of a team that contains", str(ctx.exception))

    def test_lead_cannot_act_on_peer_lead(self) -> None:
        lead = self._agent("lead")
        peer = self._agent("peer")
        self.controller.create_team("crew", ["lead", "peer"], leads=["lead", "peer"])
        with self.assertRaises(MessagingError):
            self.controller._authorize_lead(lead, peer)

    def test_lead_cannot_act_on_itself(self) -> None:
        lead = self._agent("lead")
        self.controller.create_team("crew", ["lead"], leads=["lead"])
        with self.assertRaises(MessagingError):
            self.controller._authorize_lead(lead, lead)

    def test_agent_of_another_team_has_no_authority(self) -> None:
        lead = self._agent("lead")
        worker = self._agent("worker")
        outsider = self._agent("outsider")
        self.controller.create_team("crew", ["worker"], leads=[])
        self.controller.create_team("other", ["outsider"], leads=["outsider"])
        with self.assertRaises(MessagingError):
            self.controller._authorize_lead(outsider, worker)
        self.assertEqual(self.controller._lead_team_ids(lead.agent_id), set())

    def test_conversation_agent_counts_as_a_lead(self) -> None:
        from crewhall import chat as conversations

        lead = self._agent("lead")
        worker = self._agent("worker")
        team = self.controller.create_team("crew", ["lead", "worker"])
        conv = conversations.create(title="chat", agent_id=lead.agent_id)
        conversations.set_team(conv["id"], team.team_id)
        # No explicit leads: the chat's own agent leads its crew.
        self.controller._authorize_lead(lead, worker)
        self.assertIn(team.team_id, self.controller._lead_team_ids(lead.agent_id))


if __name__ == "__main__":
    unittest.main()
