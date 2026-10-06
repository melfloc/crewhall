"""The shells an agent runs: listed from /proc, their live output, stopped on request."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from crewhall import ClaudeCodeHarness, Controller, OpenCodeHarness
from crewhall import processes as procs
from crewhall.harness import AgentState
from crewhall.types import Status

from .support import FakeSession

# A stand-in agent process: starts a Claude-style wrapped shell writing to a
# tasks/<id>.output file, plus a helper that is not a tool command.
AGENT = r"""
import os, subprocess, sys, time
tasks = sys.argv[1]
out = open(os.path.join(tasks, "btask01.output"), "w")
script = "source /dev/null 2>/dev/null || true && eval 'for i in 1 2 3; do echo tick $i; done; sleep 60' < /dev/null && pwd -P >| /dev/null"
subprocess.Popen(["/usr/bin/bash", "-c", script], stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
time.sleep(60)
"""


class FakeAgentProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="at-procs-")
        self.tasks = os.path.join(self.tmp, f"claude-{os.getuid()}", "-proj", "11111111-2222-3333-4444-555555555555", "tasks")
        os.makedirs(self.tasks)
        self.agent = subprocess.Popen([sys.executable, "-c", AGENT, self.tasks])
        self.addCleanup(self._stop)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            listing = procs.agent_processes(self.agent.pid)
            if listing["shells"] and listing["others"]:
                break
            time.sleep(0.05)
        time.sleep(0.2)

    def _stop(self):
        for pid in procs.descendants(self.agent.pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        self.agent.kill()
        self.agent.wait()

    def test_lists_the_shell_with_its_command_and_output_file(self):
        listing = procs.agent_processes(self.agent.pid)
        self.assertEqual(len(listing["shells"]), 1)
        shell = listing["shells"][0]
        self.assertEqual(shell["command"], "for i in 1 2 3; do echo tick $i; done; sleep 60")
        self.assertEqual(shell["output"], {"kind": "file", "task": "btask01"})
        self.assertEqual(shell["stdin"], "/dev/null")
        self.assertTrue(any(c["command"].startswith("sleep") for c in shell["children"]))
        self.assertLess(shell["last_output_ago"], 5)
        self.assertEqual(len(listing["others"]), 1)  # the helper is not a tool command
        out = procs.tail(shell["stdout"])
        self.assertEqual(out["output"], "tick 1\ntick 2\ntick 3\n")

    def test_signal_stops_the_shell_and_what_it_started(self):
        shell = procs.agent_processes(self.agent.pid)["shells"][0]
        sleeper = next(c["pid"] for c in shell["children"] if c["command"].startswith("sleep"))
        procs.signal_tree(self.agent.pid, shell["pid"], "TERM")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and procs.agent_processes(self.agent.pid)["shells"]:
            time.sleep(0.05)
        self.assertEqual(procs.agent_processes(self.agent.pid)["shells"], [])
        self.assertFalse(os.path.exists(f"/proc/{sleeper}") and
                         procs._stat(sleeper) and procs._stat(sleeper)["state"] != "Z")
        self.assertEqual(self.agent.poll(), None)  # the agent itself is untouched

    def test_only_processes_of_the_agent_can_be_signalled(self):
        for pid in (1, os.getpid(), self.agent.pid):
            with self.assertRaises(procs.ProcessError):
                procs.signal_tree(self.agent.pid, pid, "KILL")
        shell = procs.agent_processes(self.agent.pid)["shells"][0]
        with self.assertRaises(procs.ProcessError):
            procs.signal_tree(self.agent.pid, shell["pid"], "HUP")

    def test_controller_lists_reads_and_signals_through_the_agent(self):
        c = Controller(adopt=False)
        session = FakeSession(screen="", name="c")
        session.pid = self.agent.pid
        harness = ClaudeCodeHarness(session, name="c")
        c.register_agent(harness)
        harness.conversation_id = "11111111-2222-3333-4444-555555555555"
        with mock.patch("crewhall.paths.usable_tmpdir", lambda: self.tmp):
            listing = c.list_processes("c")
            self.assertEqual(listing["agent_pid"], self.agent.pid)
            self.assertEqual(listing["shells"][0]["output"]["task"], "btask01")
            self.assertEqual(c.process_output("c", task="btask01")["output"], "tick 1\ntick 2\ntick 3\n")
            with self.assertRaises(procs.ProcessError):
                c.process_output("c", task="../../etc/passwd")
            out = c.signal_process("c", listing["shells"][0]["pid"], "int")
            self.assertEqual(out["signal"], "INT")
            self.assertEqual(session.events()[-1]["type"], "process_signal")
            # Once finished, its output stays readable among the finished ones.
            # Wait for both: the shell to stop and the task to be reclassified as
            # finished (the machine may be busy under a full test run).
            deadline = time.monotonic() + 15
            finished: list[dict] = []
            while time.monotonic() < deadline:
                listing = c.list_processes("c")
                finished = listing["finished"]
                if not listing["shells"] and any(t["task"] == "btask01" for t in finished):
                    break
                time.sleep(0.1)
            self.assertEqual([t["task"] for t in finished], ["btask01"])

    def test_a_stopped_agent_has_no_processes(self):
        c = Controller(adopt=False)
        session = FakeSession(screen="", name="c")
        session.status = Status.EXITED
        c.register_agent(ClaudeCodeHarness(session, name="c"))
        from crewhall.harness import HarnessError

        with self.assertRaises(HarnessError):
            c.list_processes("c")


class OpenCodeMatching(unittest.TestCase):
    def entry(self, pid, command):
        return {"pid": pid, "argv": ["bash", "-c", command], "command": command}

    def test_exact_contained_and_single_leftover(self):
        tools = {"npm run dev": {"call": "c1"},
                 "bash -c 'for i in 1 2; do echo $i; done'": {"call": "c2"},
                 "make watch": {"call": "c3"}}
        entries = [self.entry(1, "npm run dev"),
                   self.entry(2, "for i in 1 2; do echo $i; done"),   # exec'ed inner bash
                   self.entry(3, "make  watch --quiet-ish")]          # nothing in common
        procs._match_tools(entries, tools)
        self.assertEqual([e.get("output", {}).get("call") for e in entries], ["c1", "c2", "c3"])

    def test_claude_wrapper_is_unwrapped(self):
        argv = ["/usr/bin/bash", "-c",
                "source /x 2>/dev/null || true && eval 'echo '\"'\"'hi'\"'\"' && ls' && pwd -P >| /tmp/c"]
        self.assertEqual(procs.claude_command(argv), "echo 'hi' && ls")


class OpenCodeKilledToolIsUnstuck(unittest.TestCase):
    def test_turn_is_aborted_only_if_the_tool_still_runs(self):
        c = Controller(adopt=False)
        h = OpenCodeHarness(FakeSession(screen="", name="o"), name="o")
        c.register_agent(h)
        h.conversation_id = "ses_abc"
        h.link = mock.Mock()
        h.link.running_tools.return_value = {"sleep 99": {"call": "call_1", "output": ""}}
        gone = 2 ** 22 + 12345  # no such pid
        c._unstick_opencode_tool(h, gone, "call_1", grace=0)
        h.link.abort.assert_called_once_with("ses_abc")
        h.link.reset_mock()
        h.link.running_tools.return_value = {}
        c._unstick_opencode_tool(h, gone, "call_1", grace=0)
        h.link.abort.assert_not_called()


class ClaudeFooterWithBackgroundShells(unittest.TestCase):
    def test_tui_is_mounted_while_a_background_shell_runs(self):
        screen = ("❯ hola\n● Listo\n" + "─" * 40 + "\n❯ \n" + "─" * 40 +
                  "\n  ⏵⏵ auto mode on · 1 shell · ← for agents · ↓ to manage\n")
        h = ClaudeCodeHarness(FakeSession(screen=screen, name="c"), name="c")
        self.assertIn(h.state(), (AgentState.READY, AgentState.WAITING_INPUT))


if __name__ == "__main__":
    unittest.main()
