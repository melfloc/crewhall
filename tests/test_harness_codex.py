from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from agent_terminal import codex_rollout
from agent_terminal.harness.codex import CodexHarness

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


RUN = os.environ.get("AT_RUN_CODEX") == "1"
HAVE = shutil.which("codex") is not None


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_CODEX=1 with codex installed and a trusted cwd")
class CodexHarnessReal(unittest.TestCase):
    """Real end-to-end run (minimal prompt). Never auto-accepts the trust dialog."""

    def test_reply_with_ok(self) -> None:
        from agent_terminal import AgentState, Controller

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
