from __future__ import annotations

import unittest

from agent_terminal.usage import parse_usage


class _FakeHarness:
    def __init__(self, text: str, kind: str = "opencode") -> None:
        self._text = text
        self.kind = kind

    def capture(self) -> str:
        return self._text

    def usage_from_screen(self, text: str) -> dict:
        from agent_terminal.usage import opencode_usage

        return opencode_usage(text) if self.kind == "opencode" else {"available": False}


class ParseUsage(unittest.TestCase):
    def test_opencode_footer_yields_tokens_and_cost(self):
        text = "some transcript\n  Build · gpt-5 · 13.1K (1%) · $0.42\nesc interrupt"
        usage = parse_usage(_FakeHarness(text))
        self.assertTrue(usage["available"])
        self.assertEqual(usage["tokens"], 13100)
        self.assertTrue(usage["tokens_estimated"])
        self.assertEqual(usage["cost_usd"], 0.42)
        self.assertEqual(usage["source"], "opencode footer")

    def test_tokens_without_cost(self):
        usage = parse_usage(_FakeHarness("2.5M (12%) · processing"))
        self.assertTrue(usage["available"])
        self.assertEqual(usage["tokens"], 2_500_000)
        self.assertIsNone(usage["cost_usd"])

    def test_no_signal_is_unavailable_never_invented(self):
        self.assertEqual(parse_usage(_FakeHarness("just a screen")), {"available": False})

    def test_other_harness_kinds_are_unavailable(self):
        text = "13.1K (1%) · $0.42"
        self.assertEqual(parse_usage(_FakeHarness(text, kind="claude")), {"available": False})

    def test_capture_failure_does_not_break_the_summary(self):
        class Boom:
            kind = "opencode"

            def capture(self):
                raise RuntimeError("no screen")

        self.assertEqual(parse_usage(Boom()), {"available": False})


if __name__ == "__main__":
    unittest.main()
