from __future__ import annotations

import unittest

from crewhall import ClaudeCodeHarness, CodexHarness, OpenCodeHarness
from crewhall.harness import get_harness

from .support import FakeSession

CLAUDE_READY = "\n".join([
    "  \u276f ",
    "  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
    "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents",
])


class ModelSwitch(unittest.TestCase):
    def test_claude_switches_directly_with_an_alias(self):
        self.assertEqual(get_harness("claude"), ClaudeCodeHarness)
        self.assertEqual(ClaudeCodeHarness.model_switch_mode, "direct")
        self.assertIn("sonnet", ClaudeCodeHarness.models())
        inst = ClaudeCodeHarness(FakeSession(screen=CLAUDE_READY))
        self.assertEqual(inst.model_switch("opus"), ["/model opus"])
        self.assertEqual(inst.model_switch(None), ["/model"])

    def test_opencode_drives_its_picker_and_codex_only_opens_its_own(self):
        self.assertEqual(OpenCodeHarness.model_switch_mode, "direct")
        self.assertEqual(OpenCodeHarness(FakeSession()).model_switch("x"), ["/models"])
        self.assertEqual(CodexHarness.model_switch_mode, "picker")
        self.assertEqual(CodexHarness(FakeSession()).model_switch("ignored"), ["/model"])

    def test_command_sends_input_without_marking_a_turn(self):
        session = FakeSession(screen=CLAUDE_READY)
        harness = ClaudeCodeHarness(session)
        harness.send_command("/model sonnet")
        self.assertEqual(session.writes[-1], "/model sonnet")
        self.assertIn("ENTER", session.keys)
        # A slash command is not a work cycle: state detection must not wait for
        # a completion line that will never come.
        self.assertFalse(harness._prompt_sent)


OPENCODE_COMPOSER = "\n".join([
    "  \u2503",
    '  \u2503  Ask anything\u2026 "x"',
    "  \u2503  Build \u00b7 DeepSeek V4.1 Flash OpenCode Go",
    "  ~/x    ctrl+p commands",
])


class _PickerSession(FakeSession):
    """A FakeSession whose screen reacts like OpenCode's /models picker."""

    def __init__(self, variant: bool = False) -> None:
        super().__init__(screen=OPENCODE_COMPOSER)
        self.picker = False
        self.variant = False
        self.variant_models = variant
        self.filter = ""

    def write(self, text: str) -> None:
        if self.picker:
            self.filter = text
        elif text == "/models":
            self.picker = True
            self.filter = ""
        self.writes.append(text)

    def send_enter(self) -> None:
        self.keys.append("ENTER")
        if self.variant:
            self.variant = False
            self.screen = f"Build \u00b7 {self.filter} OpenCode Go"
            return
        if self.picker and self.filter:
            self.picker = False
            if self.variant_models and "GLM" in self.filter:
                self.variant = True
                self.screen = "Select variant\nesc\nSearch\nDefault\nlow\nhigh\nmax"
            else:
                self.screen = f"Build \u00b7 {self.filter} OpenCode Go"

    def capture(self) -> str:
        if self.variant:
            return self.screen
        if self.picker:
            return "Select model\nesc\nSearch\n" + self.filter
        return self.screen


class OpenCodePicker(unittest.TestCase):
    def test_set_model_opens_the_picker_and_selects_the_name(self):
        session = _PickerSession()
        harness = OpenCodeHarness(session)
        self.assertTrue(harness.set_model("GLM-5.3-Flash"))
        self.assertIn("/models", session.writes)
        self.assertIn("GLM-5.3-Flash", session.writes)
        self.assertIn("Build \u00b7 GLM-5.3-Flash", session.screen)

    def test_set_model_without_a_name_only_opens_the_picker(self):
        session = _PickerSession()
        OpenCodeHarness(session).set_model(None)
        self.assertTrue(session.picker)
        self.assertEqual(session.writes, ["/models"])

    def test_set_model_accepts_a_variant_dialog(self):
        session = _PickerSession(variant=True)
        self.assertTrue(OpenCodeHarness(session).set_model("GLM-5.3-Flash"))
        self.assertFalse(session.variant)
        self.assertIn("Build \u00b7 GLM-5.3-Flash", session.screen)
        self.assertEqual(session.keys.count("ENTER"), 3)  # open, model, variant


class ModelRegistry(unittest.TestCase):
    def test_harness_exports_are_stable(self):
        for cls in (ClaudeCodeHarness, CodexHarness, OpenCodeHarness):
            self.assertTrue(hasattr(cls, "model_switch_mode"))
            self.assertTrue(callable(cls.models))
            self.assertTrue(callable(cls.model_switch))
