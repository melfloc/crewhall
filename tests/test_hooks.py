from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from agent_terminal import AgentState, ClaudeCodeHarness, Controller, hooks
from agent_terminal.messaging import MessagingError

from .support import FakeSession
from .test_harness_claude import CLAUDE_READY


class HookSignals(unittest.TestCase):
    def test_stop_hook_completes_turn_without_screen_evidence(self):
        session = FakeSession(screen=CLAUDE_READY, name="c")
        harness = ClaudeCodeHarness(session, name="c")
        harness.send("hello")
        # Screen shows nothing conclusive: heuristics alone stay UNKNOWN.
        self.assertEqual(harness.state(), AgentState.UNKNOWN)
        harness.on_hook("stop")
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)

    def test_stale_stop_before_send_does_not_count(self):
        session = FakeSession(screen=CLAUDE_READY, name="c")
        harness = ClaudeCodeHarness(session, name="c")
        harness.on_hook("stop", at=1.0)  # an older turn
        harness.send("hello")
        self.assertEqual(harness.state(), AgentState.UNKNOWN)

    def test_prompt_submit_marks_turn_started(self):
        session = FakeSession(screen=CLAUDE_READY, name="c")
        harness = ClaudeCodeHarness(session, name="c")
        harness.send("hello")
        harness.on_hook("prompt_submit")
        self.assertEqual(harness.state(), AgentState.WAITING_INPUT)


class HookWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="at-hooks-")
        p = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp})
        p.start()
        self.addCleanup(p.stop)

    def test_settings_file_declares_both_hooks(self):
        path = hooks.ensure_claude_settings()
        data = json.load(open(path))
        for name, event in (("SessionStart", "session_start"),
                            ("UserPromptSubmit", "prompt_submit"), ("Stop", "stop")):
            cmd = data["hooks"][name][0]["hooks"][0]["command"]
            self.assertIn(f"agent hook {event}", cmd)
            self.assertTrue(cmd.endswith("|| true"))
        self.assertEqual(hooks.ensure_claude_settings(), path)  # idempotent

    def test_launch_command_includes_settings_only_when_enabled(self):
        c = Controller(adopt=False)
        c.hooks_enabled = True
        cmd = c._launch_command("claude", ["--agent", "x"])
        self.assertEqual(cmd[0], "claude")
        self.assertEqual(cmd[1], "--settings")
        self.assertEqual(cmd[3:], ["--agent", "x"])
        c.hooks_enabled = False
        self.assertEqual(c._launch_command("claude", ["--agent", "x"]),
                         ["claude", "--agent", "x"])
        c.hooks_enabled = True
        self.assertEqual(c._launch_command("opencode", []), ["opencode"])

    def test_record_hook_requires_the_agents_token(self):
        c = Controller(adopt=False)
        harness = ClaudeCodeHarness(FakeSession(screen=CLAUDE_READY, name="c"), name="c")
        c.register_agent(harness)
        token = c._agent_token(harness.agent_id)
        with self.assertRaises(MessagingError):
            c.record_hook(harness.agent_id, "wrong", "stop")
        with self.assertRaises(ValueError):
            c.record_hook(harness.agent_id, token, "bogus")
        out = c.record_hook(harness.agent_id, token, "stop")
        self.assertEqual(out["event"], "stop")
        self.assertGreater(harness._stop_at, 0)

    def test_clear_moves_the_agent_to_the_new_conversation(self):
        c = Controller(adopt=False)
        c.track_conversations = True
        harness = ClaudeCodeHarness(FakeSession(screen=CLAUDE_READY, name="c"), name="c")
        c.register_agent(harness)
        token = c._agent_token(harness.agent_id)
        harness.conversation_id = "11111111-1111-4111-8111-111111111111"
        new = "22222222-2222-4222-8222-222222222222"
        out = c.record_hook(harness.agent_id, token, "session_start", conversation_id=new)
        self.assertEqual(out["conversation_id"], new)
        self.assertEqual(harness.conversation_id, new)
        self.assertEqual(harness._stop_at, 0)  # not a turn signal
        # Garbage (or a path trick) never replaces the conversation.
        c.record_hook(harness.agent_id, token, "stop", conversation_id="../../etc/passwd")
        self.assertEqual(harness.conversation_id, new)
        # Any hook carries the session id: a missed SessionStart is caught later.
        newer = "33333333-3333-4333-8333-333333333333"
        c.record_hook(harness.agent_id, token, "prompt_submit", conversation_id=newer)
        self.assertEqual(harness.conversation_id, newer)


if __name__ == "__main__":
    unittest.main()


class HooksRespectTheProject(unittest.TestCase):
    """crewhall must add its hooks without touching or replacing the project's."""

    def test_launch_adds_settings_additively_and_writes_nothing_into_the_project(self):
        import shutil


        state = tempfile.mkdtemp(prefix="at-hk-state-")
        proj = tempfile.mkdtemp(prefix="at-hk-proj-")
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        self.addCleanup(shutil.rmtree, proj, ignore_errors=True)
        project_settings = os.path.join(proj, ".claude")
        os.makedirs(project_settings)
        mine = os.path.join(project_settings, "settings.json")
        original = json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}})
        open(mine, "w").write(original)
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}):
            c = Controller(adopt=False)
            c.hooks_enabled = True
            cmd = c._launch_command("claude", [], None)
        # Additive: --settings *adds* a settings source; it never replaces the project's own.
        self.assertEqual(cmd[:2], ["claude", "--settings"])
        self.assertNotIn("--setting-sources", cmd)
        self.assertTrue(cmd[2].startswith(state))          # our file lives in crewhall's state dir
        self.assertEqual(open(mine).read(), original)       # the project's settings are untouched
        self.assertEqual(sorted(os.listdir(proj)), [".claude"])

    def test_opencode_launch_does_not_touch_plugin_or_hook_config(self):
        c = Controller(adopt=False)
        cmd = c._launch_command("opencode", [], None, 4242)
        self.assertEqual(cmd, ["opencode", "--port", "4242"])


@unittest.skipUnless(os.environ.get("AT_RUN_CLAUDE") == "1", "set AT_RUN_CLAUDE=1 (needs a real, logged-in claude)")
class RealClaudeKeepsProjectHooks(unittest.TestCase):
    def test_project_and_agent_terminal_hooks_both_fire(self):
        import shutil
        import time

        proj = os.environ.get("AT_TRUSTED_DIR", os.getcwd())  # a folder claude already trusts
        log = tempfile.mktemp(prefix="at-proj-hook-")
        settings = os.path.join(proj, ".claude", "settings.local.json")
        existed = os.path.exists(os.path.dirname(settings))
        self.assertFalse(os.path.exists(settings), "refusing to overwrite an existing settings.local.json")
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"echo PROJECT_STOP >> {log}"}]}]}},
                  open(settings, "w"))
        self.addCleanup(lambda: (os.unlink(settings), None if existed else shutil.rmtree(os.path.dirname(settings), ignore_errors=True)))
        c = Controller(adopt=False)
        c.hooks_enabled = True
        self.addCleanup(c.shutdown)
        h = c.create_agent("claude", name="hk", cwd=proj, wait_ready=True, timeout=60)
        h.send("Responde solo OK")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not os.path.exists(log):
            time.sleep(0.5)
        self.assertIn("PROJECT_STOP", open(log).read())
        # (crewhall's own hook reports to the daemon: covered by the e2e run in the docs)
