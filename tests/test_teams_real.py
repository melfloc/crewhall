from __future__ import annotations

import os
import shutil
import time
import unittest
import uuid

from crewhall import Controller

RUN = (
    os.environ.get("AT_RUN_CLAUDE") == "1"
    and os.environ.get("AT_RUN_OPENCODE") == "1"
)
HAVE = shutil.which("claude") is not None and shutil.which("opencode") is not None
BACKEND = "tmux" if shutil.which("tmux") else "pty"

# Only OpenCode and Claude Code have credentials in this environment, so the
# "third agent" is a third real instance, not a third harness implementation.


def wait_for_text(harness, needle: str, timeout: float = 240.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in harness.capture():
            return True
        time.sleep(0.5)
    return False


class RealFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)
        self.agents: list = []

    def tearDown(self) -> None:
        self.controller.shutdown()

    def spawn(self, kind: str, name: str):
        harness = self.controller.create_agent(
            kind, name=name, backend=BACKEND, cwd=os.getcwd()
        )
        self.agents.append(harness)
        return harness


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_CLAUDE=1 and AT_RUN_OPENCODE=1")
class TeamReal(RealFixture):
    def test_mixed_team_with_real_agents(self) -> None:
        oc = self.spawn("opencode", "oc")
        cc = self.spawn("claude", "cc")
        oc.start(timeout=40)
        cc.start(timeout=40)

        team = self.controller.create_team("mixed", ["oc", "cc"])
        info = self.controller.team_info(team.team_id)
        self.assertEqual({m["name"] for m in info["members"]}, {"oc", "cc"})
        self.assertEqual({m["kind"] for m in info["members"]}, {"opencode", "claude"})
        self.assertEqual(info["missing"], [])

        delivery = self.controller.send_message("oc", "cc", "TEAM_MESSAGE_123")
        self.assertTrue(delivery.delivered, delivery.error)

        self.controller.remove_agent("oc")
        self.assertEqual([m["name"] for m in self.controller.team_members(team.team_id)], ["cc"])
        self.assertEqual(self.controller.teams.missing(team), [oc.agent_id])
        self.assertIs(self.controller.get_team(team.team_id), team)

        self.controller.remove_team(team.team_id)
        self.assertIs(self.controller.get_agent("cc"), cc)
        self.assertTrue(cc.session.status.alive)

    def _send_when_ready(self, sender: str, recipient: str, body: str) -> bool:
        """Deliver once the recipient is able to accept input.

        A busy (WORKING) agent legitimately rejects input — there is no queue by
        design — so the caller waits for it to become ready again. Real agents
        can stay busy for a while under load, hence the generous window.
        """
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            try:
                delivery = self.controller.send_message(sender, recipient, body)
            except Exception as exc:  # noqa: BLE001
                if "cannot send while state" not in str(exc):
                    raise
                time.sleep(1.0)
                continue
            if delivery.delivered:
                return True
            if delivery.error and "cannot send while state" not in delivery.error:
                return False
            time.sleep(1.0)
        return False

    def test_three_agent_team_membership_and_messages(self) -> None:
        a = self.spawn("opencode", "a")
        b = self.spawn("claude", "b")
        c = self.spawn("opencode", "c")
        a.start(timeout=40)
        b.start(timeout=40)
        c.start(timeout=40)

        team = self.controller.create_team("research", ["a", "b", "c"])
        info = self.controller.team_info(team.team_id)
        self.assertEqual(len(info["members"]), 3)
        self.assertEqual(info["missing"], [])
        self.assertEqual({m["name"] for m in info["members"]}, {"a", "b", "c"})
        self.assertEqual(len({x.agent_id for x in (a, b, c)}), 3)

        pairs = [
            ("a", "b", b),
            ("b", "c", c),
            ("c", "a", a),
            ("a", "c", c),
            ("c", "b", b),
            ("b", "a", a),
        ]
        delivered: list[tuple[str, object]] = []
        for sender, recipient, target in pairs:
            body = "MSG_" + uuid.uuid4().hex[:8].upper()
            self.assertTrue(
                self._send_when_ready(sender, recipient, body),
                f"could not deliver {body}",
            )
            self.assertTrue(wait_for_text(target, body), f"{body} not delivered")
            delivered.append((body, target))
        # Isolation is enforced by the control plane; the raw capture may
        # legitimately mention a token if an agent itself ran `tmux
        # capture-pane`, so we assert authorization instead of screen content.
        from crewhall.messaging import MessagingError

        # a and b share fiscal; c is also in it, so use a foreign agent.
        outsider = self.spawn("opencode", "outsider")
        outsider.start(timeout=40)
        self.controller.create_team("other", ["outsider"])
        token_a = self.controller._agent_token(a.agent_id)
        with self.assertRaises(MessagingError):
            self.controller.send_message_as(
                a.agent_id, token_a, "outsider", "SHOULD_NOT_ARRIVE"
            )
        self.assertNotIn("SHOULD_NOT_ARRIVE", outsider.capture())

        updated = self.controller.remove_team_member("research", "b")
        self.assertEqual({m.name for m in self.controller.teams.members(updated)}, {"a", "c"})
        self.assertTrue(b.session.status.alive)

        # After removing b from the team, b remains a live agent; operator
        # sends (no Team restriction on the operator path) must still deliver.
        self.assertTrue(self._send_when_ready("a", "c", "AFTER_REMOVE_A_TO_C"))
        self.assertTrue(wait_for_text(c, "AFTER_REMOVE_A_TO_C"))

        self.assertTrue(self._send_when_ready("a", "b", "B_STILL_AN_AGENT"))
        self.assertTrue(
            wait_for_text(b, "B_STILL_AN_AGENT")
            or "B_STILL_AN_AGENT" in self.controller.get_agent("b").capture()
        )

        restored = self.controller.add_team_member("research", "b")
        self.assertEqual(
            {m.name for m in self.controller.teams.members(restored)}, {"a", "b", "c"}
        )

    def test_team_isolation_real(self) -> None:
        a = self.spawn("opencode", "a")
        b = self.spawn("claude", "b")
        c = self.spawn("opencode", "c")
        a.start(timeout=40)
        b.start(timeout=40)
        c.start(timeout=40)

        research = self.controller.create_team("research", ["a", "b"])
        dev = self.controller.create_team("dev", ["c"])
        self.controller.remove_team_member("research", "b")
        self.controller.add_team_member("dev", "b")

        self.assertEqual({m["name"] for m in self.controller.team_members(research.team_id)}, {"a"})
        self.assertEqual(
            {m["name"] for m in self.controller.team_members(dev.team_id)}, {"c", "b"}
        )
        self.assertIs(self.controller.get_agent("b"), b)
        self.assertTrue(b.session.status.alive)

        delivery = self.controller.send_message("a", "c", "ISOLATION_A_TO_C")
        self.assertTrue(delivery.delivered, delivery.error)
        self.assertTrue(wait_for_text(c, "ISOLATION_A_TO_C"))


if __name__ == "__main__":
    unittest.main()
