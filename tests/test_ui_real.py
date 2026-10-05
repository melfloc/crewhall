from __future__ import annotations

import os
import shutil
import time
import unittest
import uuid

from crewhall import Controller
from crewhall.ui import AppModel, LocalControl, render_text, session_viewport

RUN = (
    os.environ.get("AT_RUN_CLAUDE") == "1"
    and os.environ.get("AT_RUN_OPENCODE") == "1"
)
HAVE = shutil.which("claude") is not None and shutil.which("opencode") is not None
BACKEND = "tmux" if shutil.which("tmux") else "pty"
USABLE = ("ready", "waiting_input")


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_CLAUDE=1 and AT_RUN_OPENCODE=1")
class UiRealFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)
        self.control = LocalControl(self.controller)
        self.model = AppModel(self.control, cwd=os.getcwd(), async_ops=False)
        self.model.refresh()

    def _create_via_modal(self, name: str, kind: str) -> None:
        self.model.focus = "nav"
        self.model.handle_key("n")
        for char in name:
            self.model.handle_key(char)
        self.model.handle_key("tab")  # focus: kind
        kind_field = next(f for f in self.model.modal.fields if f.name == "kind")
        kind_field.index = kind_field.options.index(kind)
        self.model.handle_key("enter")
        self.model.refresh()
        self.assertIn(name, {a["name"] for a in self.model.agents})

    def tearDown(self) -> None:
        self.controller.shutdown()

    def _select(self, name: str) -> None:
        self.model.focus = "nav"
        for index, row in enumerate(self.model.rows()):
            if row["kind"] == "agent" and name in row["label"]:
                self.model.cursor = index
                return
        raise AssertionError(f"agent row {name} not found")

    def _wait_usable(self, name: str, timeout: float = 90.0) -> str:
        deadline = time.monotonic() + timeout
        state = "unknown"
        while time.monotonic() < deadline:
            self._select(name)
            self.model.refresh()
            agent = self.model.selected_agent()
            state = agent["state"] if agent else "unknown"
            if state in USABLE:
                return state
            time.sleep(0.5)
        return state

    def _send_interactive(self, name: str, text: str, timeout: float = 90.0) -> None:
        self._select(name)
        self.assertIn(self._wait_usable(name), USABLE)
        self.model.handle_key("enter")  # enter Interactive Focus
        self.assertEqual(self.model.focus, "interactive")
        for char in text:
            self.model.handle_key(char)
        self.model.handle_key("enter")  # forward ENTER to the agent's TUI

    def _wait_text(self, name: str, needle: str, timeout: float = 90.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._select(name)
            self.model.refresh()
            if needle in self.model.capture_cache:
                return True
            time.sleep(0.5)
        return False

    def _interactive(self, name: str) -> None:
        self._select(name)
        self.assertIn(self._wait_usable(name), USABLE)
        self.model.handle_key("enter")
        self.assertEqual(self.model.focus, "interactive")

    def _wait_exited(self, name: str, timeout: float = 40.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.model.refresh()
            agent = next((a for a in self.model.agents if a["name"] == name), None)
            if self.model.focus == "nav" and agent and agent["state"] in ("exited", "error"):
                return
            time.sleep(0.3)
        self.fail(f"{name} did not exit / return to navigator")

    def _resize_and_exit(self, name: str, kind: str, exit_keys: str) -> None:
        self.model.create_agent(name, kind)
        self.model.refresh()
        self._interactive(name)

        cols, rows = session_viewport(self.model, 100, 30)
        self.model.sync_viewport(cols, rows)
        time.sleep(0.6)
        session = self.controller.get_agent(name).session
        self.assertEqual((session.cols, session.rows), (cols, rows))

        if exit_keys == "ctrl_d":
            self.model.handle_key("ctrl_d")
        else:
            for char in exit_keys:
                self.model.handle_key(char)
            self.model.handle_key("enter")

        self._wait_exited(name)
        self.assertEqual(self.model.focus, "nav")
        self.assertEqual(self.model.selected_agent()["name"], name)

        # Agent stays registered until the user deletes it.
        self._select(name)
        self.model.handle_key("d")
        self.assertEqual(self.model.modal.kind, "confirm")
        self.model.handle_key("y")
        self.model.refresh()
        self.assertNotIn(name, self.model.agent_names())

    def test_resize_and_exit_recovery_opencode(self) -> None:
        self._resize_and_exit("oc", "opencode", "ctrl_d")

    def test_exit_recovery_claude(self) -> None:
        self._resize_and_exit("cc", "claude", "/exit")

    def test_ui_full_flow_with_real_agents(self) -> None:
        self._create_via_modal("a", "opencode")
        self._create_via_modal("b", "claude")
        self._create_via_modal("c", "opencode")
        self.assertEqual({a["name"] for a in self.model.agents}, {"a", "b", "c"})

        self.model.create_team("research", ["a", "b", "c"])
        self.model.refresh()
        team = next(t for t in self.model.teams if t["name"] == "research")
        self.assertEqual({m["name"] for m in team["members"]}, {"a", "b", "c"})
        self.assertEqual(team["missing"], [])

        token_a = "RESULT_A_" + uuid.uuid4().hex[:6].upper()
        token_b = "RESULT_B_" + uuid.uuid4().hex[:6].upper()

        self._send_interactive("a", f"Reply with exactly the single token: {token_a}")
        self.assertTrue(self._wait_text("a", token_a), "token_a not seen in agent A")
        cap_a = self.control.capture("a", True, 200)
        self.assertIn(token_a, cap_a)
        self.assertNotIn(token_b, cap_a)

        self._send_interactive("b", f"Reply with exactly the single token: {token_b}")
        self.assertTrue(self._wait_text("b", token_b), "token_b not seen in agent B")
        cap_b = self.control.capture("b", True, 200)
        self.assertIn(token_b, cap_b)
        self.assertNotIn(token_a, cap_b)

        self.model.send_message("a", "b", "MSG_A_TO_B")
        self.model.send_message("b", "a", "MSG_B_TO_A")
        self.model.refresh()
        bodies = {m["body"] for m in self.model.activity}
        self.assertIn("MSG_A_TO_B", bodies)
        self.assertIn("MSG_B_TO_A", bodies)
        self.assertTrue(all(m["delivered"] for m in self.model.activity))

        self.model.remove_members("research", ["b"])
        self.model.refresh()
        team = next(t for t in self.model.teams if t["name"] == "research")
        self.assertEqual({m["name"] for m in team["members"]}, {"a", "c"})

        self.model.add_members("research", ["b"])
        self.model.refresh()
        team = next(t for t in self.model.teams if t["name"] == "research")
        self.assertEqual({m["name"] for m in team["members"]}, {"a", "b", "c"})

        self.model.remove_team(team["team_id"])
        self.model.refresh()
        self.assertEqual(self.model.teams, [])
        self.assertEqual({a["name"] for a in self.model.agents}, {"a", "b", "c"})

        screen = "\n".join(render_text(self.model, 100, 30))
        self.assertIn("CREWHALL", screen)


if __name__ == "__main__":
    unittest.main()
