from __future__ import annotations

import os
import unittest
from unittest import mock

from crewhall import doctor
from crewhall.harness import versions

SCREENS = os.path.join(os.path.dirname(__file__), "fixtures", "screens")


class VersionParsing(unittest.TestCase):
    def test_parse_and_range(self):
        self.assertEqual(versions.parse_version("Claude Code v2.1.284"), (2, 1, 284))
        self.assertEqual(versions.parse_version("OpenAI Codex (v0.158.0)"), (0, 158, 0))
        self.assertIsNone(versions.parse_version("no digits here"))
        self.assertTrue(versions.is_supported("claude", "2.1.284"))
        self.assertTrue(versions.is_supported("claude", "2.5.0"))
        self.assertFalse(versions.is_supported("claude", "2.0.0"))
        self.assertFalse(versions.is_supported("claude", "3.0.0"))
        # opencode now has a validated range too (captured 1.18.34).
        self.assertTrue(versions.is_supported("opencode", "1.18.34"))
        self.assertFalse(versions.is_supported("opencode", "9.9.9"))
        self.assertIsNone(versions.is_supported("nope-kind", "9.9.9"))


class FixturesStayInRange(unittest.TestCase):
    def _fixture_text(self, kind: str) -> str:
        directory = os.path.join(SCREENS, kind)
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".txt"):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                text = fh.read()
            if versions.parse_version(text):
                return text
        return ""

    def test_captured_fixture_versions_are_supported(self):
        for kind in ("claude", "codex"):
            text = self._fixture_text(kind)
            self.assertTrue(versions.is_supported(kind, text),
                            f"{kind} fixture version out of range: {text[:40]!r}")


class DoctorWarnsOutOfRange(unittest.TestCase):
    def test_out_of_range_cli_is_a_warning(self):
        with mock.patch("crewhall.doctor.shutil.which", return_value="/usr/bin/claude"), \
                mock.patch("crewhall.doctor._version_of", return_value="2.0.0"):
            checks = doctor.check_agent_version_ranges()
        self.assertTrue(any(c["status"] == "warn" for c in checks), checks)

    def test_supported_cli_is_ok(self):
        with mock.patch("crewhall.doctor.shutil.which", return_value="/usr/bin/claude"), \
                mock.patch("crewhall.doctor._version_of", return_value="2.1.284"):
            checks = doctor.check_agent_version_ranges()
        self.assertTrue(any(c["status"] == "ok" for c in checks), checks)


if __name__ == "__main__":
    unittest.main()
