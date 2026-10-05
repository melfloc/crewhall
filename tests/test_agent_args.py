from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from agent_terminal import ClaudeCodeHarness, Controller
from agent_terminal.persistence import StateStore
from agent_terminal.types import parse_agent_args


class ParseAgentArgs(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(parse_agent_args(None), [])
        self.assertEqual(parse_agent_args("   "), [])

    def test_shell_style_split(self):
        self.assertEqual(
            parse_agent_args('--agent reviewer --append-system-prompt "be brief"'),
            ["--agent", "reviewer", "--append-system-prompt", "be brief"],
        )

    def test_list_passthrough(self):
        self.assertEqual(parse_agent_args(["--agent", "x y"]), ["--agent", "x y"])

    def test_rejects_bad_input(self):
        for bad in ('--a "unterminated', ["a\nb"], ["a\x00b"], [1], 5,
                    ["x"] * 65, ["x" * 5000]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_agent_args(bad)


class AgentArgsLifecycle(unittest.TestCase):
    def setUp(self):
        # Make the "claude" harness launch a harmless long-lived process so the
        # argv wiring can be checked without the real CLI.
        patcher = mock.patch.object(
            ClaudeCodeHarness, "command", classmethod(lambda cls: ["sleep"])
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="at-args-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = StateStore(os.path.join(self.tmp, "state.json"))
        self.controller = Controller(adopt=False, persist=False)
        self.controller._store = self.store
        self.addCleanup(self.controller.shutdown)

    def _create(self, **kw):
        return self.controller.create_agent(
            "claude", name="a", backend="pty", cwd=self.tmp, **kw
        )

    def test_args_are_appended_to_command_as_argv(self):
        harness = self._create(args="60")
        self.assertEqual(harness.session.spec.command, ["sleep", "60"])
        summary = self.controller.agent_summary(harness)
        self.assertEqual(summary["args"], ["60"])

    def test_default_has_no_extra_args(self):
        harness = self._create()
        self.assertEqual(harness.session.spec.command, ["sleep"])
        self.assertEqual(self.controller.agent_summary(harness)["args"], [])

    def test_invalid_args_do_not_create_an_agent(self):
        with self.assertRaises(ValueError):
            self._create(args='--agent "oops')
        self.assertEqual(self.controller.list_agents(), [])

    def test_args_are_not_interpreted_by_a_shell(self):
        harness = self._create(args="60; touch pwned")
        self.assertEqual(harness.session.spec.command, ["sleep", "60;", "touch", "pwned"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned")))

    def test_args_survive_persistence_and_restart(self):
        harness = self._create(args="61")
        agent_id = harness.agent_id
        self.store.save(self.store.snapshot(self.controller))
        self.assertEqual(self.store.load()["agents"][0]["args"], ["61"])

        # New controller (e.g. daemon restart): agent restored as EXITED, then
        # revived on demand with the same command line.
        c2 = Controller(adopt=False, persist=False)
        c2._store = self.store
        self.addCleanup(c2.shutdown)
        c2.restore()
        restored = c2.get_agent(agent_id)
        self.assertEqual(restored.session.spec.command, ["sleep", "61"])
        revived = c2.restart_agent(agent_id)
        self.assertEqual(revived.session.spec.command, ["sleep", "61"])
        time.sleep(0.2)
        revived.session.poll()
        self.assertTrue(revived.session.status.alive)


if __name__ == "__main__":
    unittest.main()


class TerminalDimensionValidation(unittest.TestCase):
    def test_dim_bounds(self):
        from agent_terminal.daemon import _dim

        self.assertEqual(_dim("120", "cols"), 120)
        for bad in (0, 1, 1001, 10**9, -5, "x", None, [1]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                _dim(bad, "cols")


class ManagedSectionGuidance(unittest.TestCase):
    def test_section_covers_sender_identity_and_queued_semantics(self):
        from agent_terminal.control_files import managed_section

        text = managed_section()
        self.assertIn("[from: <sender>]", text)  # attributed by crewhall
        self.assertIn("queued", text)
        self.assertIn("Do not send it again", text)
        self.assertIn("crewhall message send --to", text)

    def test_stale_section_is_refreshed_in_place_preserving_user_text(self):
        from agent_terminal.control_files import BEGIN, END, ensure_managed_section

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "CLAUDE.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"# mine\n\n{BEGIN}\nold text\n{END}\n\nafter\n")
            result = ensure_managed_section(tmp)
            self.assertTrue(result.updated)
            body = open(path, encoding="utf-8").read()
            self.assertTrue(body.startswith("# mine\n"))
            self.assertTrue(body.rstrip().endswith("after"))
            self.assertNotIn("old text", body)
            self.assertIn("Do not send it again", body)
