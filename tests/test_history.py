from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from unittest import mock

from agent_terminal import ClaudeCodeHarness, Controller
from agent_terminal.transcripts import find_session_file, parse_entries, read_history

from .support import FakeSession


def entry(role, content, **extra):
    return json.dumps({"type": role, "message": {"role": role, "content": content},
                       "timestamp": "2026-10-03T00:00:00Z", **extra})


class History(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="at-hist-")
        p = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg})
        p.start()
        self.addCleanup(p.stop)
        self.sid = str(uuid.uuid4())
        proj = os.path.join(self.cfg, "projects", "-some-project")
        os.makedirs(proj)
        self.path = os.path.join(proj, f"{self.sid}.jsonl")

    def write(self, lines):
        open(self.path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    def test_parses_user_assistant_tools_and_skips_noise(self):
        self.write([
            json.dumps({"type": "ai-title"}),
            entry("user", "hola"),
            entry("assistant", [{"type": "thinking", "thinking": "x"},
                                {"type": "text", "text": "respuesta larga\ncon líneas"},
                                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
            entry("user", [{"type": "tool_result", "content": [{"type": "text", "text": "a b"}]}]),
            entry("user", "meta", isMeta=True),
            "not json",
        ])
        msgs = parse_entries(open(self.path).read().splitlines())
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertIn("respuesta larga\ncon líneas", msgs[1]["text"])
        self.assertIn("[tool: Bash]", msgs[1]["text"])
        self.assertNotIn("thinking", msgs[1]["text"])
        self.assertIn("[result] a b", msgs[2]["text"])

    def test_paging_returns_latest_then_earlier(self):
        self.write([entry("user", f"m{i}") for i in range(10)])
        latest = read_history(self.sid, limit=4)
        self.assertEqual([m["text"] for m in latest["messages"]], ["m6", "m7", "m8", "m9"])
        self.assertEqual((latest["total"], latest["start"]), (10, 6))
        older = read_history(self.sid, limit=4, before=latest["start"])
        self.assertEqual([m["text"] for m in older["messages"]], ["m2", "m3", "m4", "m5"])

    def test_session_id_is_validated(self):
        for bad in ("../../etc/passwd", "*", "", "x" * 36):
            self.assertIsNone(find_session_file(bad))
        self.assertFalse(read_history("nope")["available"])

    def test_harness_history_uses_conversation_id(self):
        self.write([entry("user", "hola")])
        h = ClaudeCodeHarness(FakeSession(screen="", name="c"), name="c")
        self.assertFalse(h.history()["available"])
        h.conversation_id = self.sid
        self.assertEqual(h.history()["messages"][0]["text"], "hola")


class ConversationWiring(unittest.TestCase):
    def test_session_id_added_unless_user_chooses_a_session(self):
        c = Controller(adopt=False)
        c.hooks_enabled = False
        c.track_conversations = True
        cid = c._new_conversation_id("claude", [])
        self.assertTrue(cid)
        cmd = c._launch_command("claude", ["--agent", "x"], cid)
        self.assertEqual(cmd, ["claude", "--session-id", cid, "--agent", "x"])
        for flag in (["--resume", "abc"], ["-c"], ["--session-id=zzz"]):
            self.assertIsNone(c._new_conversation_id("claude", flag))
        self.assertIsNone(c._new_conversation_id("opencode", []))
        c.track_conversations = False
        self.assertIsNone(c._new_conversation_id("claude", []))

    def test_each_launch_gets_a_new_id(self):
        c = Controller(adopt=False)
        c.track_conversations = True
        self.assertNotEqual(c._new_conversation_id("claude", []),
                            c._new_conversation_id("claude", []))


if __name__ == "__main__":
    unittest.main()


class StructuredBlocks(History):
    def test_blocks_keep_structure_and_cache_follows_changes(self):
        self.write([
            entry("assistant", [{"type": "text", "text": "# Titulo\n- a\n- b"},
                                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]),
            entry("user", [{"type": "tool_result", "content": "ok"}]),
        ])
        r = read_history(self.sid)
        first = r["messages"][0]["blocks"]
        self.assertEqual([b["type"] for b in first], ["text", "tool_use"])
        self.assertEqual(first[1]["name"], "Bash")
        self.assertEqual(r["messages"][1]["blocks"], [{"type": "tool_result", "text": "ok"}])
        # appending to the file invalidates the cache
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(entry("assistant", "nuevo") + "\n")
        self.assertEqual(read_history(self.sid)["total"], 3)


class StoppedAgentKeepsConversation(unittest.TestCase):
    def setUp(self):
        import shutil

        self.tmp = tempfile.mkdtemp(prefix="at-hist-persist-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp})
        p.start()
        self.addCleanup(p.stop)

    def test_summary_offers_history_by_kind_even_before_any_message(self):
        from agent_terminal import OpenCodeHarness

        c = Controller(adopt=False)
        claude = ClaudeCodeHarness(FakeSession(screen="", name="c"), name="c")
        oc = OpenCodeHarness(FakeSession(screen="", name="o"), name="o")
        for h in (claude, oc):
            c.register_agent(h)
            self.assertIsNone(h.conversation_id)
            self.assertTrue(c.agent_summary(h)["history"], h.kind)

    def test_conversation_id_is_persisted_and_restored_for_a_stopped_agent(self):
        from agent_terminal.persistence import StateStore

        sid = str(uuid.uuid4())
        proj = os.path.join(self.tmp, "projects", "-p")
        os.makedirs(proj)
        open(os.path.join(proj, f"{sid}.jsonl"), "w").write(entry("user", "hola") + "\n")
        store = StateStore(os.path.join(self.tmp, "state.json"))
        c = Controller(adopt=False)
        c._store = store
        h = ClaudeCodeHarness(FakeSession(screen="", name="c"), name="c")
        h.conversation_id = sid
        c.register_agent(h)
        store.save(store.snapshot(c))
        self.assertEqual(store.load()["agents"][0]["conversation_id"], sid)

        c2 = Controller(adopt=False)
        c2._store = store
        c2.restore()
        restored = c2.get_agent("c")
        self.assertEqual(restored.conversation_id, sid)
        self.assertEqual(restored.history()["messages"][0]["text"], "hola")


class InternalWrappersAreHidden(unittest.TestCase):
    def test_pasted_content_and_slash_command_wrappers_read_like_what_the_user_saw(self):
        from agent_terminal.transcripts import clean_user_text, parse_entries

        self.assertEqual(clean_user_text('<pasted_content id="b678"> [from: a] REPORTE largo </pasted_content>').strip(),
                         "[from: a] REPORTE largo")
        self.assertEqual(clean_user_text(
            "<command-name>/model</command-name> <command-message>model</command-message> "
            "<command-args>sonnet</command-args>"), "/model sonnet")
        self.assertEqual(clean_user_text("<local-command-stdout>Set model to Sonnet</local-command-stdout>"),
                         "↳ Set model to Sonnet")
        self.assertEqual(clean_user_text("plain text"), "plain text")
        lines = [entry("user", '<pasted_content id="x">[from: a] hola</pasted_content>'),
                 entry("assistant", "una <pasted_content> literal en la respuesta")]
        msgs = parse_entries(lines)
        self.assertEqual(msgs[0]["text"], "[from: a] hola")            # users' wrappers are stripped
        self.assertIn("<pasted_content>", msgs[1]["text"])              # the agent's own text is never rewritten
