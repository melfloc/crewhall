from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from crewhall import activity


class Classify(unittest.TestCase):
    def test_each_state_has_a_distinct_kind(self):
        def c(state, **kw):
            return activity.classify(state, **{"asking": 0, "tool": None, "shells": 0, **kw})["kind"]
        self.assertEqual(c("starting"), "starting")
        self.assertEqual(c("working"), "thinking")
        self.assertEqual(c("working", tool={"name": "Bash", "brief": "npm test"}), "shell")
        self.assertEqual(c("working", tool={"name": "Task", "brief": None}), "subagent")
        self.assertEqual(c("working", tool={"name": "Edit", "brief": "/a.py"}), "tool")
        self.assertEqual(c("waiting_input"), "waiting")
        self.assertEqual(c("waiting_input", asking=1), "asking")
        self.assertEqual(c("ready"), "idle")
        self.assertEqual(c("ready", shells=2), "background")
        self.assertEqual(c("exited"), "exited")
        self.assertEqual(c("error"), "error")

    def test_detail_is_the_tool_brief(self):
        out = activity.classify("working", asking=0, shells=0, tool={"name": "Bash", "brief": "npm test"})
        self.assertEqual((out["label"], out["detail"]), ("Running command", "npm test"))


class ClaudeSnapshot(unittest.TestCase):
    def _write(self, lines):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        path = os.path.join(d, "s.jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(json.dumps(x) for x in lines))
        return path

    def test_model_and_unanswered_tool(self):
        path = self._write([
            {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
            {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "sleep 60"}}]}},
        ])
        with mock.patch.object(activity.transcripts, "find_session_file", return_value=path):
            snap = activity.claude_snapshot("sid")
        self.assertEqual(snap["model"], "claude-opus-5-5")
        self.assertEqual(snap["tool"], {"name": "Bash", "brief": "sleep 60"})

    def test_answered_tool_means_no_running_tool(self):
        path = self._write([
            {"type": "assistant", "message": {"model": "m", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}},
        ])
        with mock.patch.object(activity.transcripts, "find_session_file", return_value=path):
            self.assertIsNone(activity.claude_snapshot("sid")["tool"])

    def test_missing_transcript_is_empty(self):
        with mock.patch.object(activity.transcripts, "find_session_file", return_value=None):
            self.assertEqual(activity.claude_snapshot("sid"), {})


class ModelFallbacks(unittest.TestCase):
    def test_model_found_beyond_the_tail_window(self):
        d = tempfile.mkdtemp(); self.addCleanup(__import__("shutil").rmtree, d, True)
        path = os.path.join(d, "s.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"model": "claude-old", "content": []}}) + "\n")
            fh.write(json.dumps({"type": "user", "message": {"content": "x" * (activity._TAIL * 2)}}) + "\n")
        with mock.patch.object(activity.transcripts, "find_session_file", return_value=path):
            self.assertEqual(activity.claude_snapshot("sid")["model"], "claude-old")

    def test_synthetic_model_is_ignored(self):
        d = tempfile.mkdtemp(); self.addCleanup(__import__("shutil").rmtree, d, True)
        path = os.path.join(d, "s.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"model": "<synthetic>", "content": []}}) + "\n")
        with mock.patch.object(activity.transcripts, "find_session_file", return_value=path):
            self.assertIsNone(activity.claude_snapshot("sid")["model"])

    def test_model_from_agent_args(self):
        self.assertEqual(activity.model_from_args(["--model", "opus"]), "opus")
        self.assertEqual(activity.model_from_args(["--model=sonnet"]), "sonnet")
        self.assertEqual(activity.model_from_args(["-m", "p/x"]), "p/x")
        self.assertIsNone(activity.model_from_args(["--verbose"]))
        self.assertIsNone(activity.model_from_args(None))

    def test_opencode_model_from_prompt_before_any_reply(self):
        link = mock.Mock()
        link.get_json.return_value = [{"info": {"role": "user", "model": {"providerID": "p", "modelID": "gpt-5.6-luna"}}, "parts": []}]
        self.assertEqual(activity.opencode_snapshot(link, "ses_1")["model"], "gpt-5.6-luna")


class ModelFromScreen(unittest.TestCase):
    def test_claude_header(self):
        screen = " ▐▛███▛█   Claude Code v2.1.284\n▝▜██████▀  Sonnet 5.5 · Claude Pro\n ▝▝   ▝▝   ~/p\n"
        self.assertEqual(activity.model_from_screen("claude", screen), "Sonnet 5.5")

    def test_claude_opus_with_context_tag(self):
        self.assertEqual(activity.model_from_screen("claude", "▝▜██████▀  Opus 4.1 (1M context) · Claude Max\n"),
                         "Opus 4.1 (1M context)")

    def test_opencode_footer(self):
        screen = "  ┃\n  ┃  Ask anything…\n  ┃\n  ┃  Build · DeepSeek V4.1 Flash OpenCode Go\n  ╹▀▀▀\n"
        self.assertEqual(activity.model_from_screen("opencode", screen), "DeepSeek V4.1 Flash OpenCode Go")

    def test_no_model_on_screen(self):
        self.assertIsNone(activity.model_from_screen("claude", "just some text\n"))
        self.assertIsNone(activity.model_from_screen("opencode", None))


class OpenCodeSnapshot(unittest.TestCase):
    def test_model_and_running_tool(self):
        link = mock.Mock()
        link.get_json.return_value = [{"info": {"role": "assistant", "modelID": "gpt-x"}, "parts": [
            {"type": "tool", "tool": "bash", "state": {"status": "running", "input": {"command": "make"}}}]}]
        snap = activity.opencode_snapshot(link, "ses_1")
        self.assertEqual(snap, {"model": "gpt-x", "tool": {"name": "bash", "brief": "make"}})

    def test_server_error_is_empty(self):
        link = mock.Mock(); link.get_json.side_effect = OSError("down")
        self.assertEqual(activity.opencode_snapshot(link, "ses_1"), {})


if __name__ == "__main__":
    unittest.main()


class ControllerActivity(unittest.TestCase):
    def _controller(self, args=None):
        from crewhall.controller import Controller
        c = Controller.__new__(Controller)
        c._agent_args = {"a1": args or []}
        return c

    def _harness(self, kind="claude", screen="", snap=None):
        from crewhall.harness import get_harness

        h = mock.Mock(kind=kind, agent_id="a1", conversation_id="sid", link=None)
        h.capture.return_value = screen
        h.activity_snapshot.return_value = snap if snap is not None else {}
        h.model_from_screen.side_effect = lambda text: get_harness(kind).model_from_screen(text)
        return h

    def _summary(self, state="ready"):
        return {"state": state, "interactions": []}

    def test_transcript_model_wins_over_screen(self):
        c = self._controller()
        h = self._harness(screen="▝▜██████▀  Sonnet 5.5 · Claude Pro",
                          snap={"model": "claude-sonnet-5-5"})
        with mock.patch("crewhall.controller.procs.agent_processes", return_value={"shells": []}), \
             mock.patch.object(c, "_agent_root", return_value=1, create=True):
            self.assertEqual(c._activity(h, self._summary())["model"], "claude-sonnet-5-5")

    def test_screen_fallback_and_exited_keeps_last_model(self):
        c, h = self._controller(["--model", "opus"]), self._harness(screen="▝▜██████▀  Sonnet 5.5 · Claude Pro")
        with mock.patch("crewhall.controller.procs.agent_processes", return_value={"shells": []}), \
             mock.patch.object(c, "_agent_root", return_value=1, create=True):
            self.assertEqual(c._activity(h, self._summary())["model"], "Sonnet 5.5")
            h.capture.return_value = ""
            self.assertEqual(c._activity(h, self._summary("exited"))["model"], "Sonnet 5.5")  # last known

    def test_args_are_the_last_resort(self):
        c, h = self._controller(["--model=opus"]), self._harness(screen="")
        with mock.patch("crewhall.controller.procs.agent_processes", return_value={"shells": []}), \
             mock.patch.object(c, "_agent_root", return_value=1, create=True):
            self.assertEqual(c._activity(h, self._summary())["model"], "opus")
