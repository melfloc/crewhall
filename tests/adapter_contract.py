"""Contract kit every provider adapter must pass (ADAPTERS.md §9).

An adapter is not finished until this kit passes **and** a real run is documented
in ``ADAPTERS.md`` §8. Subclass it with the adapter class and its fixture dir:

    class ClaudeContract(AdapterContract):
        harness_cls = ClaudeCodeHarness
        kind = "claude"
"""
from __future__ import annotations

import json
import os
import re
import unittest

from agent_terminal.harness import HARNESSES

from .support import FakeSession

SCREENS = os.path.join(os.path.dirname(__file__), "fixtures", "screens")
ALLOWED_KINDS = {"claude", "opencode", "codex"}

# Never a real path/credential/email in a fixture.
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}"
    r"|/home/(?!user\b)[A-Za-z0-9_.-]+"
    r"|ANTHROPIC_API_KEY\s*=\s*\S+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
SHELL_META = re.compile(r"[;&|`$><\n]")
# A dialog that must never be auto-answered or read as ready.
TRUST_MARKERS = (
    "trust this folder", "is this a project you created", "sign in", "log in",
    "select login", "terms of service", "do you trust",
)


class AdapterContract(unittest.TestCase):
    harness_cls = None  # type: ignore[assignment]
    kind: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        if cls.harness_cls is None:
            raise unittest.SkipTest("base contract")
        cls.directory = os.path.join(SCREENS, cls.kind)
        with open(os.path.join(cls.directory, "expected.json"), encoding="utf-8") as fh:
            cls.expected = json.load(fh)

    def _harness(self, screen: str = ""):
        return self.harness_cls(FakeSession(screen=screen, name=self.kind), name=self.kind)

    def _fixture(self, name: str) -> str:
        with open(os.path.join(self.directory, name), encoding="utf-8") as fh:
            return fh.read()

    def test_kind_is_stable_and_registered(self):
        self.assertIn(self.kind, ALLOWED_KINDS)
        self.assertIs(HARNESSES.get(self.kind), self.harness_cls)

    def _detect_fixture(self, name, want):
        """Detect a fixture that may need a prior work cycle (WAITING_INPUT)."""
        harness = self._harness(self._fixture(name))
        if want["state"] == "waiting_input":
            # A finished turn is only observable after work was seen.
            harness._prompt_sent = True
            for other, spec in self.expected.items():
                if spec["state"] == "working":
                    harness._detect(self._fixture(other))
                    break
        text = self._fixture(name)
        return harness._detect(text)

    def test_fixtures_match_expected_state(self):
        for name, want in self.expected.items():
            state, evidence = self._detect_fixture(name, want)
            self.assertEqual(state.value, want["state"], name)
            if want.get("evidence_contains"):
                self.assertIn(want["evidence_contains"].lower(), evidence.lower(), name)

    def test_trust_or_login_is_never_ready(self):
        for name, want in self.expected.items():
            low = self._fixture(name).lower()
            if any(marker in low for marker in TRUST_MARKERS):
                self.assertNotIn(want["state"], ("ready", "waiting_input"), name)

    def test_working_only_with_a_positive_marker(self):
        for text in ("", "just some unrelated text\n", "\x00\x01 broken"):
            self.assertNotEqual(self._harness(text)._detect(text)[0].value, "working", text)

    def test_broken_screen_never_raises(self):
        for text in ("", "\x00\x01 broken", "x" * 5000):
            state, _ = self._harness(text)._detect(text)
            self.assertIn(state.value, ("starting", "unknown", "ready", "waiting_input", "exited"))

    def test_input_line_never_returns_the_placeholder(self):
        for name, want in self.expected.items():
            if want["state"] != "ready":
                continue
            self.assertEqual(self._harness(self._fixture(name)).input_line().strip(), "", name)

    def test_command_is_safe_argv(self):
        argv = self.harness_cls.command()
        self.assertIsInstance(argv, list)
        self.assertTrue(argv and all(isinstance(a, str) and a for a in argv))
        for arg in argv:
            self.assertIsNone(SHELL_META.search(arg), arg)
        self._assert_no_dangerous(argv)

    def test_launch_args_are_safe_and_secret_free(self):
        args = self.harness_cls.launch_args(None, None, None)
        self.assertIsInstance(args, list)
        self._assert_no_dangerous(args)
        blob = " ".join(args)
        self.assertIsNone(SECRET_RE.search(blob), blob)

    def test_history_tolerates_a_malicious_id(self):
        harness = self._harness("")
        for bad in ("../etc/passwd", "a/b", "..", ""):
            harness.conversation_id = bad
            self.assertIsInstance(harness.history(limit=1), dict, bad)

    def test_fixtures_contain_no_secrets(self):
        for root, _dirs, files in os.walk(SCREENS):
            for name in files:
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                match = SECRET_RE.search(text)
                self.assertIsNone(match, f"{name}: {match.group(0) if match else ''}")

    def _assert_no_dangerous(self, argv) -> None:
        joined = " ".join(argv)
        for flag in self.harness_cls.dangerous_flags:
            self.assertNotIn(flag, joined, flag)
