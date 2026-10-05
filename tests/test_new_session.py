"""/clear (Claude) and /new (OpenCode): the agent leaves its conversation and can be told to."""
from __future__ import annotations

import unittest

from crewhall import ClaudeCodeHarness, Controller, OpenCodeHarness
from crewhall.harness import HarnessError
from crewhall.messaging import MessagingError

from .support import FakeSession
from .test_harness_claude import CLAUDE_READY

# Captured from opencode 1.18.34 at 140 columns.
OC_SESSION = """\
  ┃  responde solo: uno
  uno
  ▣  Build · DeepSeek V4.1 Flash · 1.2s
  ┃
  ┃  Build · DeepSeek V4.1 Flash OpenCode Go
  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
   /tmp/ocnew                                    13.1K (1%) · $0.00   ctrl+p commands
"""
OC_HOME = """\
                       ┃  Ask anything… "Fix broken tests"
                       ┃
                       ┃  Build · DeepSeek V4.1 Flash OpenCode Go
                       ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
                                               tab agents  ctrl+p commands
                                ● Tip Run opencode upgrade to update to the latest version
  /tmp/ocnew                                                              1.18.34
"""
OLD, NEW = "ses_old1", "ses_new2"


class LeavingTheConversation(unittest.TestCase):
    def setUp(self):
        self.c = Controller(adopt=False)
        self.h = OpenCodeHarness(FakeSession(screen=OC_SESSION, name="o"), name="o")
        self.c.register_agent(self.h)
        self.h.conversation_id = OLD

    def test_new_typed_then_enter_leaves_the_conversation_at_once(self):
        self.h.write_raw("/new")
        self.assertEqual(self.h.conversation_id, OLD)  # typing alone does nothing
        self.h.send_key("ENTER")
        self.assertIsNone(self.h.conversation_id)
        self.assertGreater(self.h.conversation_since, 0)

    def test_other_input_and_other_keys_do_not(self):
        self.h.write_raw("/new")
        self.h.send_key("ESC")              # abandoned
        self.h.write_raw("hola")
        self.h.send_key("ENTER")
        self.h.write_raw("/news about x")
        self.h.send_key("ENTER")
        self.assertEqual(self.h.conversation_id, OLD)

    def test_clear_alias_and_send(self):
        self.h.write_raw("/clear")
        self.h.send_key("ENTER")
        self.assertIsNone(self.h.conversation_id)

    def test_the_old_conversation_is_never_picked_again_except_by_creation(self):
        self.h.write_raw("/new")
        self.h.send_key("ENTER")
        self.assertIn(OLD, self.c._retired_conversations[self.h.agent_id])
        # The old session finishing up does not pull the agent back.
        self.c._on_opencode_event(self.h, {"type": "session.status",
                                           "properties": {"sessionID": OLD, "status": {"type": "idle"}}})
        self.assertIsNone(self.h.conversation_id)
        # The first prompt creates the new one, which is adopted.
        self.c._on_opencode_event(self.h, {"type": "session.created",
                                           "properties": {"sessionID": NEW, "info": {"id": NEW}}})
        self.assertEqual(self.h.conversation_id, NEW)

    def test_start_screen_reached_outside_crewhall_is_detected(self):
        self.h.link = object()  # history goes through OpenCode's server
        self.h.conversation_resolver = lambda: None
        self.assertFalse(self.h.on_home_screen())
        self.h.session.screen = OC_HOME
        self.assertTrue(self.h.on_home_screen())
        self.h.history()
        self.assertIsNone(self.h.conversation_id)

    def test_a_new_id_reported_first_is_not_undone(self):
        # Claude: the SessionStart hook can arrive before the harness notices /clear.
        self.c._conversation_left(self.h, "ses_something_else")
        self.assertEqual(self.h.conversation_id, OLD)


class NewSessionCommand(unittest.TestCase):
    def setUp(self):
        self.c = Controller(adopt=False)
        self.orq = ClaudeCodeHarness(FakeSession(screen=CLAUDE_READY, name="orq"), name="orq")
        self.oc = OpenCodeHarness(FakeSession(screen=OC_SESSION, name="ej"), name="ej")
        self.out = ClaudeCodeHarness(FakeSession(screen=CLAUDE_READY, name="out"), name="out")
        for h in (self.orq, self.oc, self.out):
            self.c.register_agent(h)
        self.c.create_team("t", [self.orq.agent_id, self.oc.agent_id])
        self.token = self.c._agent_token(self.orq.agent_id)
        self.oc.conversation_id = OLD

    def test_orchestrator_clears_a_teammate(self):
        self.oc.session.send_key = lambda key, s=self.oc.session: (
            s.keys.append(key), setattr(s, "screen", OC_HOME))
        out = self.c.new_session_as(self.orq.agent_id, self.token, "ej", timeout=2)
        self.assertEqual(self.oc.session.writes[-1], "/new")
        self.assertEqual(out["command"], "/new")
        self.assertEqual(out["previous"], OLD)
        self.assertTrue(out["confirmed"])
        self.assertIsNone(self.oc.conversation_id)
        self.assertFalse(self.oc._prompt_sent)  # idle on the new conversation, not "waiting for a reply"
        self.assertEqual(self.oc.session.events()[-1]["data"], {"by": self.orq.agent_id})

    def test_team_boundary_token_and_self(self):
        with self.assertRaises(MessagingError):
            self.c.new_session_as(self.orq.agent_id, "wrong", "ej", timeout=1)
        with self.assertRaises(MessagingError):
            self.c.new_session_as(self.orq.agent_id, self.token, "out", timeout=1)
        with self.assertRaises(MessagingError):
            self.c.new_session_as(self.orq.agent_id, self.token, "orq", timeout=1)
        self.assertEqual(self.oc.session.writes, [])

    def test_claude_is_confirmed_by_the_new_session_id(self):
        self.c.track_conversations = True
        self.c.create_team("t2", [self.orq.agent_id, self.out.agent_id])
        self.out.conversation_id = "11111111-1111-4111-8111-111111111111"
        new = "22222222-2222-4222-8222-222222222222"
        tok = self.c._agent_token(self.out.agent_id)
        self.out.session.send_key = lambda key, s=self.out.session: (
            s.keys.append(key),
            key == "ENTER" and self.c.record_hook(self.out.agent_id, tok, "session_start",
                                                  conversation_id=new))
        out = self.c.new_session(self.out, timeout=2)
        self.assertEqual(out["command"], "/clear")
        self.assertTrue(out["confirmed"])
        self.assertEqual(self.out.conversation_id, new)

    def test_busy_agent_is_refused(self):
        self.oc.session.screen = OC_SESSION + "\n  ■■■⬝⬝  esc interrupt"
        with self.assertRaises(HarnessError):
            self.c.new_session(self.oc, timeout=0.5)
        self.assertEqual(self.oc.session.writes, [])


if __name__ == "__main__":
    unittest.main()


class CustomAgentComposer(unittest.TestCase):
    """``opencode --agent ejecutor``: the composer footer is not ``Build · …``.

    Regression: the echo of the previous message was taken for residual input,
    so every message and new-session was refused with "input line not clean".
    """

    SCREEN = """\
  ┃  responde solo: hola
  hola
  ▣  Ejecutor · DeepSeek V4.1 Flash · 1.1s
  ┃
  ┃
  ┃
  ┃  Ejecutor · DeepSeek V4.1 Flash OpenCode Go
  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
   ~/Projects                                    13.1K (1%) · $0.00   ctrl+p commands
"""

    def harness(self, screen):
        return OpenCodeHarness(FakeSession(screen=screen, name="o"), name="o")

    def test_previous_message_echo_is_not_input(self):
        self.assertEqual(self.harness(self.SCREEN).input_line(), "")

    def test_typed_text_is_still_seen(self):
        typed = self.SCREEN.replace("  ┃\n  ┃\n  ┃\n  ┃  Ejecutor", "  ┃\n  ┃  texto a medias\n  ┃\n  ┃  Ejecutor")
        self.assertEqual(self.harness(typed).input_line(), "texto a medias")

    def test_new_session_is_not_refused(self):
        c = Controller(adopt=False)
        h = self.harness(self.SCREEN)
        c.register_agent(h)
        h.conversation_id = OLD
        h.session.send_key = lambda key, s=h.session: (s.keys.append(key), setattr(s, "screen", OC_HOME))
        out = c.new_session(h, timeout=2)
        self.assertEqual(h.session.writes[-1], "/new")
        self.assertTrue(out["confirmed"])
