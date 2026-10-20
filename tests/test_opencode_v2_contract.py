"""OpenCode v2 contract: fixture-driven screen detection and safe input.

Every screen fixture under ``fixtures/opencode_v2/<cols>x<rows>/`` is a sanitized
capture from the validated 2.0.29 TUI. The expected state comes from the handoff
(§7.1); a UI change upstream must fail here before it reaches an agent.
"""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from crewhall import Controller, OpenCodeHarness
from crewhall.harness import AgentState
from crewhall.harness.base import HarnessNotReady
from crewhall.harness.opencode import COMPLETION_RE, OpenCodeHarness as Harness
from crewhall.opencode_link import OpenCodeLink

from .fake_opencode_v2 import PASSWORD, FakeOpenCodeV2, start_server
from .support import FakeSession

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "opencode_v2")
EXPECTED = {
    "home-no-tabs": "ready",
    "home-with-tabs": "ready",
    "session-1tab-idle": "waiting_input",
    "session-2tabs-idle": "waiting_input",
    "tabs-off-idle": "waiting_input",
    "working-seconds": "working",
    "working-minutes": "working",
    "tool-running": "working",
    "tool-done": "waiting_input",
    "sessions-picker": "waiting_input",
    "models-picker": "waiting_input",
    "agents-picker": "waiting_input",
    "command-palette": "waiting_input",
    "shell-mode": "waiting_input",
    "permission": "waiting_input",
    "form": "waiting_input",
    "v1-idle-minutes": "waiting_input",
    "v1-home": "ready",
}
OVERLAYS = {
    "sessions-picker": "sessions",
    "models-picker": "models",
    "agents-picker": "agents",
    "command-palette": "palette",
    "shell-mode": "shell",
    "permission": "permission",
    "form": "form",
}


def fixture(size: str, name: str) -> str:
    with open(os.path.join(FIXTURES, size, name + ".txt"), encoding="utf-8") as fh:
        return fh.read()


class FixtureDetection(unittest.TestCase):
    def test_every_fixture_detects_its_expected_state(self):
        for size in ("120x40", "80x24"):
            for name, want in EXPECTED.items():
                text = fixture(size, name)
                harness = OpenCodeHarness(FakeSession(screen=text, name="o"), name="o")
                state, evidence = harness._detect(text)
                self.assertEqual(state.value, want, f"{size}/{name}: {evidence}")

    def test_overlays_are_named(self):
        for name, want in OVERLAYS.items():
            harness = OpenCodeHarness(FakeSession(name="o"), name="o")
            self.assertEqual(harness.overlay(fixture("120x40", name)), want, name)

    def test_permission_and_form_are_asks_not_starting(self):
        for name in ("permission", "form"):
            harness = OpenCodeHarness(FakeSession(name="o"), name="o")
            state, evidence = harness._detect(fixture("120x40", name))
            self.assertNotEqual(state, AgentState.STARTING, name)
            self.assertIn("ask", evidence.lower(), name)

    def test_home_screen_on_both_captures(self):
        for size in ("120x40", "80x24"):
            harness = OpenCodeHarness(FakeSession(name="o"), name="o")
            self.assertTrue(harness.on_home_screen(fixture(size, "home-no-tabs")), size)
            self.assertTrue(harness.on_home_screen(fixture(size, "v1-home")), size)
            self.assertFalse(
                harness.on_home_screen(fixture(size, "session-1tab-idle")), size
            )

    def test_dialog_titles_and_safe_close_cover_the_v2_overlays(self):
        for title in ("sessions for", "commands", "select model", "select agent"):
            self.assertTrue(any(title in t for t in Harness._DIALOG_TITLES), title)
        harness = OpenCodeHarness(FakeSession(name="o"), name="o")
        harness.session.screen = fixture("120x40", "sessions-picker")
        harness.close_safe_overlay()
        self.assertIn("ESC", harness.session.keys)
        # ESC is never sent for an ask.
        for name in ("permission", "form", "shell-mode"):
            harness = OpenCodeHarness(FakeSession(name="o"), name="o")
            harness.session.screen = fixture("120x40", name)
            harness.close_safe_overlay()
            self.assertNotIn("ESC", harness.session.keys, name)

    def test_screen_input_and_footer_recognise_asks(self):
        permission = fixture("120x40", "permission")
        form = fixture("120x40", "form")
        for text in (permission, form):
            self.assertTrue(Harness.screen_input_box(text))
            self.assertFalse(Harness.screen_footer(text))

    def test_input_line_ignores_overlays_and_shell_mode(self):
        for name in OVERLAYS:
            harness = OpenCodeHarness(FakeSession(name="o"), name="o")
            self.assertEqual(harness.input_line(), "", name) if False else None
            harness.session.screen = fixture("120x40", name)
            self.assertEqual(harness.input_line(), "", name)

    def test_transcript_strips_the_v2_tab_bar(self):
        harness = OpenCodeHarness(FakeSession(name="o"), name="o")
        harness.session.screen = fixture("120x40", "session-2tabs-idle")
        transcript = harness.transcript()
        self.assertNotIn("Other session", transcript)
        self.assertIn("Say only the word", transcript)


class CompletionRegex(unittest.TestCase):
    def test_ms_seconds_minutes_hours_and_combinations(self):
        cases = {
            "· 771ms": True,
            "· 12.5s": True,
            "· 2m 9s": True,
            "· 1h 2m": True,
            "· 1h 2m 3s": True,
            "· 1.5m": True,
            "· 45s": True,
        }
        for line, matches in cases.items():
            self.assertEqual(bool(COMPLETION_RE.search(line)), matches, line)

    def test_minutes_turn_reaches_waiting_input(self):
        harness = OpenCodeHarness(
            FakeSession(screen=fixture("120x40", "v1-idle-minutes"), name="o"), name="o")
        harness._prompt_sent = True
        harness._seen_working = True
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)


class SafeInput(unittest.TestCase):
    def setUp(self):
        self.server, self.port = start_server()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        FakeOpenCodeV2.reset()
        FakeOpenCodeV2.sessions["ses_mine"] = {
            "id": "ses_mine", "agent": "build",
            "model": {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"},
            "time": {"created": 1000, "updated": 0}, "location": {"directory": "/tmp/work"},
        }
        self.link = OpenCodeLink(self.port, PASSWORD, api="v2")
        self.harness = OpenCodeHarness(
            FakeSession(screen=fixture("120x40", "tabs-off-idle"), name="o"), name="o")
        self.harness.link = self.link
        self.harness.conversation_id = "ses_mine"

    def test_send_uses_the_api_not_the_active_tab(self):
        self.harness.send("hola desde crewhall")
        self.assertEqual(self.harness.session.writes, [])
        bodies = FakeOpenCodeV2.bodies("/prompt")
        self.assertEqual(bodies[0]["text"], "hola desde crewhall")
        self.assertEqual(FakeOpenCodeV2.paths().count("/api/session/ses_mine/prompt"), 1)

    def test_send_works_with_a_picker_open(self):
        self.harness.session.screen = fixture("120x40", "sessions-picker")
        self.harness.send("MESSAGE_FOR_AGENT")
        self.assertEqual(self.harness.session.writes, [])
        self.assertEqual(FakeOpenCodeV2.bodies("/prompt")[0]["text"], "MESSAGE_FOR_AGENT")

    def test_keyboard_fallback_closes_a_safe_picker_then_retries(self):
        self.harness.link = None
        self.harness.session.screen = fixture("120x40", "sessions-picker")
        # A dumb capture does not redraw after ESC: the guard sends it and the
        # delivery is retried (never typed into the picker's search box).
        with self.assertRaises(HarnessNotReady):
            self.harness.send("hola")
        self.assertIn("ESC", self.harness.session.keys)
        self.assertNotIn("hola", self.harness.session.writes)

    def test_never_escapes_out_of_a_permission_or_form(self):
        self.harness.link = None
        for name in ("permission", "form", "shell-mode"):
            self.harness.session.screen = fixture("120x40", name)
            self.harness.session.keys = []
            with self.assertRaises(HarnessNotReady, msg=name):
                self.harness.send("hola")
            self.assertNotIn("ESC", self.harness.session.keys, name)

    def test_slash_command_still_uses_the_keyboard(self):
        self.harness.send("/new")
        self.assertEqual(FakeOpenCodeV2.bodies("/prompt"), [])
        self.assertEqual(self.harness.session.writes, ["/new"])


class AuthoritativeState(unittest.TestCase):
    def setUp(self):
        self.server, self.port = start_server()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        FakeOpenCodeV2.reset()
        FakeOpenCodeV2.sessions["ses_mine"] = {
            "id": "ses_mine", "time": {"created": 1000, "updated": 0},
            "location": {"directory": "/tmp/work"},
        }
        self.harness = OpenCodeHarness(
            FakeSession(screen=fixture("120x40", "tabs-off-idle"), name="o"), name="o")
        self.harness.link = OpenCodeLink(self.port, PASSWORD, api="v2")
        self.harness.conversation_id = "ses_mine"
        self.harness._prompt_sent = True
        self.harness._sent_at = time.time() - 10

    def test_active_session_is_working_without_screen_evidence(self):
        FakeOpenCodeV2.active = {"ses_mine"}
        self.assertEqual(self.harness.state(), AgentState.WORKING)

    def test_outcome_after_send_is_waiting_input(self):
        FakeOpenCodeV2.active = set()
        FakeOpenCodeV2.sessions["ses_mine"].update({
            "outcome": "succeeded", "time": {"idle": int(time.time() * 1000)},
        })
        self.assertEqual(self.harness.state(), AgentState.WAITING_INPUT)


class DetectApi(unittest.TestCase):
    def setUp(self):
        self.server, self.port = start_server()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        FakeOpenCodeV2.reset()

    def test_404_means_v1(self):
        FakeOpenCodeV2.info_status = 404
        self.assertEqual(OpenCodeLink.detect_api(self.port, PASSWORD), "v1")

    def test_401_and_5xx_are_retried_not_downgraded(self):
        import urllib.error

        for status in (401, 500):
            FakeOpenCodeV2.info_status = status
            with self.assertRaises(urllib.error.HTTPError, msg=status):
                OpenCodeLink.detect_api(self.port, PASSWORD)

    def test_explicit_v2_link_is_not_redetected(self):
        link = OpenCodeLink(self.port, PASSWORD, api="v2")
        self.assertTrue(link.api_detected)
        FakeOpenCodeV2.info_status = 401
        self.assertEqual(link.api, "v2")


if __name__ == "__main__":
    unittest.main()
