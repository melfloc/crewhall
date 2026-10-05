from __future__ import annotations

import os
import shutil
import unittest

from crewhall import (
    AgentState,
    ClaudeCodeHarness,
    Controller,
    Harness,
    HarnessError,
    Status,
)
from crewhall.harness import available_harnesses, get_harness

from crewhall.harness.base import HarnessNotReady

from .support import FakeSession

CLAUDE_READY = "\n".join(
    [
        " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
        "\u259d\u259c\u2588\u2588\u2588\u2588\u2588\u2588\u2580  Sonnet 5.5 \u00b7 Claude Pro",
        '\u276f\u00a0Try "write a test for <filepath>"',
        "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents",
    ]
)

CLAUDE_WORKING = "\n".join(
    [
        " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
        "\u259d\u259c\u2588\u2588\u2588\u2588\u2588\u2588\u2580  Sonnet 5.5 \u00b7 Claude Pro",
        "\u273b Seasoning\u2026",
        "\u276f\u00a0",
        "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 esc to interrupt \u00b7 \u2190 for agents",
    ]
)

CLAUDE_DONE = "\n".join(
    [
        " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
        "\u259d\u259c\u2588\u2588\u2588\u2588\u2588\u2588\u2580  Sonnet 5.5 \u00b7 Claude Pro",
        "\u276f What is 17 times 3? Reply with just the number.",
        "\u25cf 51",
        "\u273b Baked for 1s \u00b7 done 4:04 PM",
        "\u276f\u00a0",
        "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents",
    ]
)

CLAUDE_STARTING = "loading\u2026\nstarting up"


class ClaudeHarnessContract(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertTrue(issubclass(ClaudeCodeHarness, Harness))
        self.assertIn("claude", available_harnesses())
        self.assertIs(get_harness("claude"), ClaudeCodeHarness)

    def test_command(self) -> None:
        self.assertEqual(ClaudeCodeHarness.command(), ["claude"])

    def test_required_operations_exist(self) -> None:
        for method in ("start", "send", "capture", "capture_recent", "state", "is_waiting", "stop"):
            self.assertTrue(callable(getattr(ClaudeCodeHarness, method)))


class ClaudeHarnessFakeSession(unittest.TestCase):
    def make(self, screen: str = "", name: str = "c") -> ClaudeCodeHarness:
        self.session = FakeSession(screen=screen, name=name)
        return ClaudeCodeHarness(self.session, name=name)

    def test_starting_then_ready(self) -> None:
        harness = self.make(CLAUDE_STARTING)
        self.assertEqual(harness.state(), AgentState.STARTING)
        self.assertIn("not mounted", harness.evidence())
        harness.session.screen = CLAUDE_READY
        self.assertEqual(harness.start(timeout=2.0), AgentState.READY)
        self.assertTrue(harness.is_waiting())

    def test_untrusted_workspace_is_not_ready(self) -> None:
        trust = "Quick safety check: Is this a project you created or one you trust?"
        harness = self.make(trust + "\n \u276f No, exit")
        self.assertEqual(harness.state(), AgentState.STARTING)

    def test_working_from_esc_to_interrupt(self) -> None:
        harness = self.make(CLAUDE_WORKING)
        self.assertEqual(harness.state(), AgentState.WORKING)
        self.assertIn("esc to interrupt", harness.evidence())

    def test_send_writes_then_enter(self) -> None:
        harness = self.make(CLAUDE_READY)
        harness.send("analiza este archivo")
        self.assertEqual(harness.session.writes, ["analiza este archivo"])
        self.assertEqual(harness.session.keys, ["ENTER"])

    def test_send_requires_usable_state(self) -> None:
        harness = self.make(CLAUDE_STARTING)
        with self.assertRaises(HarnessError):
            harness.send("too early", timeout=0.3)

    def test_waiting_input_after_work(self) -> None:
        harness = self.make(CLAUDE_READY)
        harness.send("hi")
        self.assertEqual(harness.state(), AgentState.UNKNOWN)
        harness.session.screen = CLAUDE_WORKING
        self.assertEqual(harness.state(), AgentState.WORKING)
        harness.session.screen = CLAUDE_DONE
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)
        self.assertTrue(harness.is_waiting())

    def test_unknown_before_completion_evidence(self) -> None:
        harness = self.make(CLAUDE_READY)
        harness.send("hi")
        harness.session.screen = "\n".join(
            [
                " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
                "\u276f\u00a0",
                "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents",
            ]
        )
        self.assertEqual(harness.state(), AgentState.UNKNOWN)
        self.assertIn("no completion evidence", harness.evidence())

    def test_completion_marker_detects_finished_turn(self) -> None:
        harness = self.make(CLAUDE_READY)
        harness.send("hi")
        harness.session.screen = CLAUDE_DONE
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)

    def test_persistent_completion_does_not_fake(self) -> None:
        harness = self.make(CLAUDE_DONE)
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)
        harness.send("again")
        self.assertEqual(harness.state(), AgentState.UNKNOWN)
        harness.session.screen = CLAUDE_WORKING
        self.assertEqual(harness.state(), AgentState.WORKING)
        harness.session.screen = CLAUDE_DONE
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)

    def test_confirm_submit_retries_enter_when_text_remains(self) -> None:
        harness = self.make("\u276f\u00a0hello world this is a prompt")
        harness.session.keys.clear()
        harness._confirm_submit("hello world this is a prompt", window=0.3)
        self.assertEqual(harness.session.keys, ["ENTER"])

    def test_confirm_submit_no_retry_when_input_clears(self) -> None:
        harness = self.make("\u276f\u00a0")
        harness.session.keys.clear()
        harness._confirm_submit("hello world this is a prompt", window=0.3)
        self.assertEqual(harness.session.keys, [])

    def test_capture_and_capture_recent(self) -> None:
        harness = self.make("\n".join(f"line-{i}" for i in range(100)))
        self.assertIn("line-0", harness.capture())
        self.assertEqual(
            harness.capture_recent(max_lines=3).splitlines(),
            ["line-97", "line-98", "line-99"],
        )

    def test_stop_closes_session(self) -> None:
        harness = self.make(CLAUDE_READY)
        harness.stop()
        self.assertTrue(harness.session.closed)
        self.assertFalse(harness.session.status.alive)

    def test_exited_and_error_states(self) -> None:
        harness = self.make(CLAUDE_READY)
        harness.session.status = Status.EXITED
        harness.session.exit_code = 0
        self.assertEqual(harness.state(), AgentState.EXITED)
        harness.session.exit_code = 7
        self.assertEqual(harness.state(), AgentState.ERROR)

    def test_info_semantic_fields(self) -> None:
        harness = self.make(CLAUDE_READY, name="worker")
        info = harness.info().to_dict()
        self.assertEqual(info["kind"], "claude")
        self.assertEqual(info["name"], "worker")
        self.assertIn(info["state"], ("ready", "waiting_input"))

    def test_input_line_placeholder_is_empty(self) -> None:
        harness = self.make(CLAUDE_READY)
        self.assertEqual(harness.input_line(), "")

    def test_transcript_strips_input_box(self) -> None:
        screen = "\n".join(
            [
                "● 51",
                "\u273b Baked for 1s \u00b7 done 4:04 PM",
                "",
                "\u2500" * 40,
                "\u276f\u00a0",
                "\u2500" * 40,
                "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents",
            ]
        )
        harness = self.make(screen)
        out = harness.transcript()
        self.assertIn("51", out)
        self.assertNotIn("\u276f", out)
        self.assertNotIn("shift+tab to cycle", out)

    def test_input_line_detects_residue(self) -> None:
        residue = CLAUDE_READY.replace(
            '\u276f\u00a0Try "write a test for <filepath>"',
            "\u276f\u00a0/model sonnet",
        )
        harness = self.make(residue)
        self.assertEqual(harness.input_line(), "/model sonnet")
        self.assertFalse(harness._input_is_clean())

    def test_ensure_input_clean_clears_residue(self) -> None:
        residue = CLAUDE_READY.replace(
            '\u276f\u00a0Try "write a test for <filepath>"',
            "\u276f\u00a0/model sonnet",
        )
        harness = self.make(residue)
        session = harness.session

        def clear(*_a, **_k):
            session.screen = session.screen.replace("/model sonnet", "")

        session.send_key = clear  # type: ignore[assignment]
        self.assertTrue(harness.ensure_input_clean(timeout=1.0))
        self.assertTrue(harness._input_is_clean())

    def test_two_harnesses_isolated(self) -> None:
        a = ClaudeCodeHarness(FakeSession(screen=CLAUDE_READY, name="a"), name="a")
        b = ClaudeCodeHarness(FakeSession(screen=CLAUDE_READY, name="b"), name="b")
        a.send("prompt-for-a")
        b.send("prompt-for-b")
        self.assertEqual(a.session.writes, ["prompt-for-a"])
        self.assertEqual(b.session.writes, ["prompt-for-b"])
        self.assertNotEqual(a.agent_id, b.agent_id)


RUN = os.environ.get("AT_RUN_CLAUDE") == "1"
HAVE = shutil.which("claude") is not None
BACKEND = "tmux" if shutil.which("tmux") else "pty"


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_CLAUDE=1 with claude installed")
class ClaudeHarnessReal(unittest.TestCase):
    def test_single_agent_semantic_control(self) -> None:
        controller = Controller(adopt=False)
        agent = controller.create_agent(
            "claude", name="c", backend=BACKEND, cwd=os.getcwd()
        )
        try:
            self.assertEqual(agent.start(timeout=40), AgentState.READY)
            agent.send("What is 17 times 3? Reply with just the number.")
            agent.wait_for_state(AgentState.WORKING, timeout=15)
            final = agent.wait_for_state(
                (AgentState.WAITING_INPUT, AgentState.READY), timeout=90
            )
            self.assertIn(final, (AgentState.WAITING_INPUT, AgentState.READY))
            self.assertIn("51", agent.capture())
            self.assertTrue(agent.is_waiting())
        finally:
            agent.stop()
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()


FOOTER = "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents"
REWIND = "\n".join(
    [
        "\u259b" * 5 + " Ctrl+Y to paste deleted text",
        "   Rewind",
        "   Restore the code and/or conversation to the point before\u2026",
        "   \u276f (current)",
        "   Enter to continue \u00b7 Esc to cancel",
    ]
)


class ClaudeKeyedSession(FakeSession):
    """FakeSession that reacts to keys like the real Claude TUI.

    ``CTRL_U`` clears the input line. ``ESC`` on an *empty* input arms the
    double-ESC; the second one opens the Rewind selector (no footer, no input
    box) which swallows any text + ENTER typed into it.
    """

    def __init__(self, input_text: str = "", ghost: str = "") -> None:
        super().__init__(screen="", name="c")
        self.input_text = input_text
        self.ghost = ghost
        self.esc_on_empty = 0
        self.rewind = False
        self.sent: list[str] = []
        self._render()

    def _render(self) -> None:
        if self.rewind:
            self.screen = REWIND
            return
        line = self.input_text or self.ghost
        self.screen = "\n".join(
            [
                " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
                "\u273b Baked for 1s \u00b7 done 4:04 PM",
                "\u276f\u00a0" + line,
                FOOTER,
            ]
        )

    def send_key(self, key: str) -> None:
        super().send_key(key)
        if self.rewind:
            return
        if key == "CTRL_U":
            self.input_text = ""
        elif key == "ESC":
            if self.input_text:
                pass
            else:
                self.esc_on_empty += 1
                if self.esc_on_empty >= 2:
                    self.rewind = True
        elif key == "ENTER" and self.input_text:
            self.sent.append(self.input_text)
            self.input_text = ""
        self._render()

    def write(self, text: str) -> None:
        super().write(text)
        if self.rewind:
            return  # swallowed by the overlay
        self.input_text += text
        self._render()


class ClaudeInputSafety(unittest.TestCase):
    def test_clearing_residue_never_sends_esc_and_message_becomes_a_turn(self):
        session = ClaudeKeyedSession(input_text="\u00bfya respondi\u00f3 el ejecutor?")
        harness = ClaudeCodeHarness(session, name="c")
        harness.send("REPLY_TEST", timeout=3)
        self.assertNotIn("ESC", session.keys)
        self.assertFalse(session.rewind)
        self.assertEqual(session.sent, ["REPLY_TEST"])

    def test_no_keys_sent_when_input_already_clean(self):
        session = ClaudeKeyedSession()
        harness = ClaudeCodeHarness(session, name="c")
        harness.send("hello", timeout=3)
        self.assertEqual(session.keys, ["ENTER"])
        self.assertEqual(session.sent, ["hello"])

    def test_ghost_text_is_not_residue_and_does_not_block_send(self):
        session = ClaudeKeyedSession(ghost="run the tests")  # CTRL_U can't clear it
        harness = ClaudeCodeHarness(session, name="c")
        harness.send("hello", timeout=3)
        self.assertNotIn("ESC", session.keys)
        self.assertEqual(session.writes, ["hello"])

    def test_send_never_types_into_an_overlay(self):
        session = ClaudeKeyedSession(input_text="residue")
        harness = ClaudeCodeHarness(session, name="c")
        session.rewind = True  # TUI is showing the Rewind selector
        session._render()
        with self.assertRaises(HarnessError):
            harness.send("REPLY_TEST", timeout=1)
        self.assertEqual(session.writes, [])
        self.assertNotIn("ENTER", session.keys)

    def test_overlay_opened_while_clearing_aborts_before_writing(self):
        session = ClaudeKeyedSession(input_text="residue")
        harness = ClaudeCodeHarness(session, name="c")
        original = session.send_key

        def key_then_overlay(key):
            original(key)
            if key == "CTRL_U":
                session.rewind = True
                session._render()

        session.send_key = key_then_overlay
        with self.assertRaises(HarnessNotReady):
            harness.send("REPLY_TEST", timeout=1)
        self.assertEqual(session.writes, [])


class ClaudeStateRobustness(unittest.TestCase):
    def test_mounted_without_header_is_not_starting(self):
        screen = "\n".join(["\u25cf some long output", "\u276f\u00a0", FOOTER])
        harness = ClaudeCodeHarness(FakeSession(screen=screen, name="c"), name="c")
        self.assertEqual(harness.state(), AgentState.READY)

    def test_overlay_without_footer_is_not_usable(self):
        harness = ClaudeCodeHarness(FakeSession(screen=REWIND, name="c"), name="c")
        self.assertFalse(harness.state().usable)

    def test_completion_detected_when_old_done_lines_scroll_off(self):
        before = "\n".join(
            [
                "\u276f first", "\u273b Baked for 1s \u00b7 done 4:01 PM",
                "\u276f second", "\u273b Baked for 2s \u00b7 done 4:02 PM",
                "\u276f\u00a0", FOOTER,
            ]
        )
        session = FakeSession(screen=before, name="c")
        harness = ClaudeCodeHarness(session, name="c")
        harness.send("REPLY_TEST")
        # The new turn finished but earlier lines (incl. our echo) scrolled
        # away: the visible `done` count did not grow, yet the turn is over.
        session.screen = "\n".join(
            [
                "\u25cf answer to reply", "\u273b Baked for 3s \u00b7 done 4:07 PM",
                "\u276f\u00a0", FOOTER,
            ]
        )
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)

    def test_no_completion_evidence_stays_unknown(self):
        session = FakeSession(screen=CLAUDE_READY, name="c")
        harness = ClaudeCodeHarness(session, name="c")
        harness.send("hello")
        self.assertEqual(harness.state(), AgentState.UNKNOWN)
