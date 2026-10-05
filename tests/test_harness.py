from __future__ import annotations

import unittest

from agent_terminal import AgentState, Harness, HarnessError, OpenCodeHarness, Status
from agent_terminal.harness import available_harnesses, get_harness

from .support import FakeSession

READY_SCREEN = "\n".join(
    [
        "                       ┃",
        '                       ┃  Ask anything… "Fix broken tests"',
        "                       ┃  Build · DeepSeek V4.1 Flash OpenCode Go",
        "  ~/Projects/x    ctrl+p commands",
    ]
)

WORKING_SCREEN = READY_SCREEN + "\n   ■■■■⬝⬝⬝⬝  esc interrupt   tab agents  ctrl+p commands"

IDLE_AFTER_PROMPT = "\n".join(
    [
        "  ┃",
        "  ┃  Reply with PONG",
        "   PONG",
        "  ┃  Build · DeepSeek V4.1 Flash OpenCode Go",
        "  ~/Projects/x    ctrl+p commands",
    ]
)

COMPLETION_SCREEN = IDLE_AFTER_PROMPT + (
    "\n   \u25a3  Build \u00b7 DeepSeek V4.1 Flash \u00b7 4.3s"
    "\n  /home/u/x    12.6K (1%) \u00b7 $0.00  ctrl+p commands"
)

STARTING_SCREEN = "  ▄\n  █▀▀█ █▀▀█\n  loading…"


class HarnessContract(unittest.TestCase):
    def test_opencode_is_registered_harness(self) -> None:
        self.assertTrue(issubclass(OpenCodeHarness, Harness))
        self.assertIn("opencode", available_harnesses())
        self.assertIs(get_harness("opencode"), OpenCodeHarness)

    def test_unknown_harness_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_harness("nope")

    def test_command(self) -> None:
        self.assertEqual(OpenCodeHarness.command(), ["opencode"])

    def test_required_operations_exist(self) -> None:
        for method in ("start", "send", "capture", "state", "is_waiting", "stop"):
            self.assertTrue(callable(getattr(OpenCodeHarness, method)))

    def test_state_enum_values(self) -> None:
        self.assertEqual(
            {s.value for s in AgentState},
            {"starting", "ready", "working", "waiting_input", "exited", "error", "unknown"},
        )


class OpenCodeHarnessFakeSession(unittest.TestCase):
    def make(self, screen: str = "", name: str = "a") -> OpenCodeHarness:
        self.session = FakeSession(screen=screen, name=name)
        return OpenCodeHarness(self.session, name=name)

    def test_starting_then_ready(self) -> None:
        harness = self.make(STARTING_SCREEN)
        self.assertEqual(harness.state(), AgentState.STARTING)
        self.assertIn("not mounted", harness.evidence())
        harness.session.screen = READY_SCREEN
        self.assertEqual(harness.state(), AgentState.READY)
        self.assertTrue(harness.is_waiting())

    def test_working_from_esc_interrupt(self) -> None:
        harness = self.make(WORKING_SCREEN)
        self.assertEqual(harness.state(), AgentState.WORKING)
        self.assertIn("esc interrupt", harness.evidence())

    def test_unknown_when_mounted_without_marker(self) -> None:
        harness = self.make("  Build · model\n  ctrl+p commands")
        self.assertEqual(harness.state(), AgentState.UNKNOWN)

    def test_start_waits_for_ready(self) -> None:
        harness = self.make(READY_SCREEN)
        self.assertEqual(harness.start(timeout=1.0), AgentState.READY)

    def test_send_writes_then_enter(self) -> None:
        harness = self.make(READY_SCREEN)
        harness.send("analiza este archivo")
        self.assertEqual(harness.session.writes, ["analiza este archivo"])
        self.assertEqual(harness.session.keys, ["ENTER"])

    def test_send_requires_usable_state(self) -> None:
        harness = self.make(STARTING_SCREEN)
        with self.assertRaises(HarnessError):
            harness.send("too early", timeout=0.3)

    def test_waiting_input_after_prompt(self) -> None:
        harness = self.make(READY_SCREEN)
        harness.send("hi")
        self.assertEqual(harness.state(), AgentState.UNKNOWN)
        harness.session.screen = WORKING_SCREEN
        self.assertEqual(harness.state(), AgentState.WORKING)
        harness.session.screen = IDLE_AFTER_PROMPT
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)
        self.assertTrue(harness.is_waiting())

    def test_unknown_before_work_cycle_observed(self) -> None:
        harness = self.make(READY_SCREEN)
        harness.send("hi")
        harness.session.screen = IDLE_AFTER_PROMPT
        self.assertEqual(harness.state(), AgentState.UNKNOWN)
        self.assertIn("no completion evidence", harness.evidence())

    def test_completion_footer_detects_finished_turn(self) -> None:
        harness = self.make(READY_SCREEN)
        harness.send("hi")
        harness.session.screen = COMPLETION_SCREEN
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)

    def test_persistent_footer_does_not_fake_completion(self) -> None:
        harness = self.make(COMPLETION_SCREEN)
        harness.send("again")
        self.assertEqual(harness.state(), AgentState.UNKNOWN)
        harness.session.screen = WORKING_SCREEN
        self.assertEqual(harness.state(), AgentState.WORKING)
        harness.session.screen = COMPLETION_SCREEN
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)

    def test_capture_and_capture_recent(self) -> None:
        harness = self.make("\n".join(f"line-{i}" for i in range(100)))
        self.assertIn("line-0", harness.capture())
        recent = harness.capture_recent(max_lines=3)
        self.assertEqual(recent.splitlines(), ["line-97", "line-98", "line-99"])

    def test_transcript_strips_composer_box(self) -> None:
        screen = "\n".join(
            [
                "  Reply",
                "     PONG",
                "",
                "                       \u2503",
                '                       \u2503  Ask anything\u2026 "x"',
                "                       \u2503  Build \u00b7 model",
                "                       \u2579" + "\u2580" * 40,
                "                     tab agents  ctrl+p commands",
                "  ~/Projects/x   1.18.31",
            ]
        )
        harness = self.make(screen)
        out = harness.transcript()
        self.assertIn("PONG", out)
        self.assertNotIn("Ask anything", out)
        self.assertNotIn("ctrl+p commands", out)

    def test_stop_closes_session(self) -> None:
        harness = self.make(READY_SCREEN)
        harness.stop()
        self.assertTrue(harness.session.closed)
        self.assertFalse(harness.session.status.alive)

    def test_exited_and_error_states(self) -> None:
        harness = self.make(READY_SCREEN)
        harness.session.status = Status.EXITED
        harness.session.exit_code = 0
        self.assertEqual(harness.state(), AgentState.EXITED)

        harness.session.exit_code = 7
        self.assertEqual(harness.state(), AgentState.ERROR)

    def test_info_contains_semantic_fields(self) -> None:
        harness = self.make(READY_SCREEN, name="worker")
        info = harness.info().to_dict()
        self.assertEqual(info["kind"], "opencode")
        self.assertEqual(info["name"], "worker")
        self.assertEqual(info["state"], "ready")
        self.assertEqual(info["backend"], "pty")

    def test_two_harnesses_are_isolated(self) -> None:
        a = OpenCodeHarness(FakeSession(screen=READY_SCREEN, name="a"), name="a")
        b = OpenCodeHarness(FakeSession(screen=READY_SCREEN, name="b"), name="b")
        a.send("prompt-for-a")
        b.send("prompt-for-b")
        self.assertEqual(a.session.writes, ["prompt-for-a"])
        self.assertEqual(b.session.writes, ["prompt-for-b"])
        self.assertNotEqual(a.agent_id, b.agent_id)


if __name__ == "__main__":
    unittest.main()
