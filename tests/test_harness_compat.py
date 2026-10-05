from __future__ import annotations

import unittest

from crewhall import (
    AgentState,
    ClaudeCodeHarness,
    Harness,
    OpenCodeHarness,
)
from crewhall.harness import get_harness

from .support import FakeSession

OPENCODE = {
    "starting": "  \u2584\n  \u2588\u2580\u2580\u2588 loading\u2026",
    "ready": "\n".join(
        [
            '  \u2503  Ask anything\u2026 "x"',
            "  \u2503  Build \u00b7 model",
            "  ctrl+p commands",
        ]
    ),
    "working": "\n".join(
        [
            '  \u2503  Ask anything\u2026 "x"',
            "  \u25a0\u25a0\u25a0\u25a1  esc interrupt   tab agents  ctrl+p commands",
        ]
    ),
    "done": "\n".join(
        [
            "  \u2503  Reply",
            "   PONG",
            "  Build \u00b7 model",
            "   \u25a3  Build \u00b7 model \u00b7 4.3s",
            "  12.6K (1%) \u00b7 $0.00  ctrl+p commands",
        ]
    ),
}

CLAUDE = {
    "starting": "loading\u2026\nstarting up",
    "ready": "\n".join(
        [
            " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
            "\u276f\u00a0Try \"write a test\"",
            "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents",
        ]
    ),
    "working": "\n".join(
        [
            " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
            "\u273b Seasoning\u2026",
            "\u276f\u00a0",
            "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 esc to interrupt \u00b7 \u2190 for agents",
        ]
    ),
    "done": "\n".join(
        [
            " \u2590\u259b\u2588\u2588\u2588\u259b\u2588   Claude Code v2.1.284",
            "\u276f What is 17 times 3?",
            "\u25cf 51",
            "\u273b Baked for 1s \u00b7 done 4:04 PM",
            "\u276f\u00a0",
            "  \u23f5\u23f5 auto mode on (shift+tab to cycle) \u00b7 \u2190 for agents",
        ]
    ),
}

CASES = [
    ("opencode", OpenCodeHarness, OPENCODE),
    ("claude", ClaudeCodeHarness, CLAUDE),
]


def drive(harness: Harness, screens: dict[str, str], kind: str) -> None:
    """Upper layer code that only knows the Harness contract."""
    session = harness.session
    session.screen = screens["starting"]
    assert harness.state() == AgentState.STARTING, kind

    session.screen = screens["ready"]
    state = harness.start(timeout=3.0)
    assert state.usable, (kind, state)

    harness.send("do the thing")
    assert session.writes == ["do the thing"], (kind, session.writes)
    assert "ENTER" in session.keys, (kind, session.keys)

    session.screen = screens["working"]
    assert harness.state() == AgentState.WORKING, kind

    session.screen = screens["done"]
    assert harness.state() == AgentState.WAITING_INPUT, kind
    assert harness.is_waiting(), kind

    captured = harness.capture()
    assert captured, kind
    assert harness.capture_recent(max_lines=2), kind

    info = harness.info()
    assert info.kind == kind
    assert isinstance(info.state, AgentState)


class HarnessCompatibility(unittest.TestCase):
    def test_both_registered_and_are_harness(self) -> None:
        for kind, cls, _ in CASES:
            self.assertTrue(issubclass(cls, Harness))
            self.assertIs(get_harness(kind), cls)

    def test_upper_layer_treats_both_identically(self) -> None:
        for kind, cls, screens in CASES:
            with self.subTest(kind=kind):
                harness: Harness = cls(
                    FakeSession(screen=screens["starting"], name=kind), name=kind
                )
                try:
                    drive(harness, screens, kind)
                finally:
                    harness.stop()
                    self.assertTrue(harness.session.closed)

    def test_same_state_sequence_for_both(self) -> None:
        observed = {}
        for kind, cls, screens in CASES:
            with self.subTest(kind=kind):
                harness: Harness = cls(
                    FakeSession(screen=screens["ready"], name=kind), name=kind
                )
                seq = [harness.state()]
                harness.send("go")
                seq.append(harness.state())
                harness.session.screen = screens["working"]
                seq.append(harness.state())
                harness.session.screen = screens["done"]
                seq.append(harness.state())
                observed[kind] = [s.value for s in seq]
                harness.stop()
        self.assertEqual(observed["opencode"], observed["claude"])


if __name__ == "__main__":
    unittest.main()
