from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from crewhall import codex_rollout
from crewhall.harness.codex import CodexHarness

from .support import FakeSession

ACCOUNT_ID = "acct-SUPER-SECRET-0001"


def _rollout(path: str, cwd: str) -> None:
    entries = [
        {"type": "session_meta", "payload": {"id": "abc", "account_id": ACCOUNT_ID}},
        {"type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-5.6-terra"}},
        {"type": "event_msg", "payload": {"type": "task_started", "started_at": 1}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "hi"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "hello"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete", "completed_at": 2}},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


class CodexRollout(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="at-codex-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cwd = os.path.join(self.root, "proj")
        os.makedirs(self.cwd)
        day = os.path.join(self.root, "2026", "10", "05")
        os.makedirs(day)
        self.file = os.path.join(day, "rollout-2026-10-05T00-00-00-abc.jsonl")
        _rollout(self.file, self.cwd)

    def test_read_returns_messages_and_hides_account_ids(self):
        out = codex_rollout.read(cwd=self.cwd, root=self.root)
        self.assertTrue(out["available"])
        self.assertEqual([m["text"] for m in out["messages"]], ["hi", "hello"])
        self.assertNotIn(ACCOUNT_ID, json.dumps(out))

    def test_latest_signal(self):
        snap = codex_rollout.latest_signal(cwd=self.cwd, root=self.root)
        self.assertEqual(snap["model"], "gpt-5.6-terra")
        self.assertEqual(snap["last_event"], "task_complete")

    def test_invalid_id_is_refused(self):
        for bad in ("../../etc/passwd", "a/b", "x" * 300):
            self.assertIsNone(codex_rollout.find_rollout(bad, root=self.root))

    def test_missing_tree_is_empty_not_an_error(self):
        self.assertEqual(codex_rollout.read(cwd="/nope", root="/nope"),
                         {"available": False, "messages": [], "total": 0, "start": 0})


class CodexHarnessUnit(unittest.TestCase):
    def test_model_from_screen(self):
        text = "› Ask Codex to do anything\n  GPT-5.6-Terra medium · /home/user/project\n  ← for agents · ? for shortcuts"
        self.assertEqual(CodexHarness.model_from_screen(text), "GPT-5.6-Terra")

    def test_config_risks_reads_only(self):
        home = tempfile.mkdtemp(prefix="at-codexhome-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        os.makedirs(os.path.join(home, ".codex"))
        with open(os.path.join(home, ".codex", "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('approval_policy = "never"\n')
        prev = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            risks = CodexHarness.__new__(CodexHarness).config_risks()
        finally:
            if prev is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = prev
        self.assertTrue(any("never" in r for r in risks))

    def test_dangerous_flags_are_declared(self):
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", CodexHarness.dangerous_flags)


class _CodexSession(FakeSession):
    """FakeSession that draws Codex's input line with escape sequences.

    ``ghost`` is the rotating suggestion, rendered faint (SGR 2) after a bold
    ``›`` and a reset, exactly like Codex CLI 0.158.0. ``input_text`` is a real
    (non-faint) draft. A ``help`` line keeps the placeholder text on screen so
    ``_detect`` still reports READY.
    """

    def __init__(self, input_text: str = "", ghost: str = "") -> None:
        super().__init__(screen="", name="codex")
        self.input_text = input_text
        self.ghost = ghost
        self._render()

    def _render(self) -> None:
        line = self.input_text or self.ghost
        self.screen = "\n".join([
            "  >_ OpenAI Codex (v0.158.0)",
            "  Ask Codex to do anything",  # help text keeps the TUI "mounted"
            "\u203a " + line,
            "  GPT-5.6-Terra medium \u00b7 /tmp/x",
            "  ? for shortcuts",
        ])

    def capture(self, escapes: bool = False) -> str:
        if not escapes or self.input_text or not self.ghost:
            return self.screen
        return self.screen.replace(
            "\u203a " + self.ghost,
            "\x1b[1m\u203a\x1b[0m \x1b[2m" + self.ghost + "\x1b[0m")


class CodexGhostText(unittest.TestCase):
    def test_faint_suggestion_reads_as_empty(self):
        harness = CodexHarness(_CodexSession(ghost='Shall we turn "what if" into something?'))
        self.assertTrue(harness._input_is_ghost())
        self.assertEqual(harness.input_line(), "")

    def test_typed_draft_is_not_ghost(self):
        harness = CodexHarness(_CodexSession(input_text="borrador"))
        self.assertFalse(harness._input_is_ghost())
        self.assertEqual(harness.input_line(), "borrador")

    def test_suggestion_does_not_block_send(self):
        session = _CodexSession(ghost="Shall we turn this into a test?")
        harness = CodexHarness(session)
        harness.send("hola", timeout=2)
        self.assertEqual(session.writes, ["hola"])
        self.assertEqual(session.keys, ["ENTER"])
        self.assertNotIn("CTRL_U", session.keys)
        self.assertNotIn("ESC", session.keys)

    def test_human_draft_raises_without_clearing(self):
        from crewhall.harness.base import HarnessNotReady

        session = _CodexSession(input_text="borrador humano")
        harness = CodexHarness(session)
        with self.assertRaises(HarnessNotReady):
            harness.send("hola", timeout=0.5)
        self.assertEqual(session.writes, [])
        self.assertNotIn("CTRL_U", session.keys)
        self.assertNotIn("ESC", session.keys)
        self.assertEqual(session.input_text, "borrador humano")


RUN = os.environ.get("AT_RUN_CODEX") == "1"
HAVE = shutil.which("codex") is not None


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_CODEX=1 with codex installed and a trusted cwd")
class CodexHarnessReal(unittest.TestCase):
    """Real end-to-end run (minimal prompt). Never auto-accepts the trust dialog."""

    def test_reply_with_ok(self) -> None:
        from crewhall import AgentState, Controller

        backend = "tmux" if shutil.which("tmux") else "pty"
        controller = Controller(adopt=False)
        self.addCleanup(controller.shutdown)
        agent = controller.create_agent("codex", name="codex-real", backend=backend, cwd=os.getcwd())
        self.assertEqual(agent.start(timeout=60), AgentState.READY)
        agent.send("reply with the word ok")
        agent.wait_for_state(AgentState.WORKING, timeout=20)
        final = agent.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=120)
        self.assertIn(final, (AgentState.WAITING_INPUT, AgentState.READY))
        self.assertIn("ok", agent.capture().lower())
        agent.stop(force=True)


if __name__ == "__main__":
    unittest.main()


class NoEscapeAsClearKey(unittest.TestCase):
    def test_no_adapter_clears_input_with_the_interrupt_key(self):
        # ESC interrupts a turn in every supported CLI; as a "clear" key it ate
        # the first characters of the next injected message.
        from crewhall.harness import HARNESSES

        for kind, cls in HARNESSES.items():
            self.assertNotIn("ESC", cls.clear_input_keys, kind)
            self.assertIn("CTRL_U", cls.clear_input_keys, kind)

