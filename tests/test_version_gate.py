from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import ClaudeCodeHarness, Controller, OpenCodeHarness, settings
from crewhall.harness import versions
from crewhall.harness.versions import UnsupportedVersionError
from crewhall.messaging import MessagingError

from .support import FakeSession

SCREEN = '  Build · model\n  Ask anything… "x"\n  ctrl+p commands'


def _probe(mapping: dict[str, str]):
    def probe(argv) -> str | None:
        return mapping.get(tuple(argv), mapping.get(argv[0]))
    return probe


def _report(supported: bool, text: str) -> versions.VersionReport:
    return versions.VersionReport(
        "opencode", ("opencode",), text, versions.parse_version(text), supported,
        "1.18.34", "outside the validated range" if supported is False else "in range",
    )


class SelectCommand(unittest.TestCase):
    def test_fallback_used_when_primary_out_of_range(self):
        mapping = {"/opt/v2/opencode": "2.0.0", "/opt/v1/opencode": "1.18.34"}
        with mock.patch.object(versions, "probe_version", _probe(mapping)):
            argv, report, used = versions.select_command(
                "opencode", [["/opt/v2/opencode"], ["/opt/v1/opencode"]])
        self.assertTrue(used)
        self.assertEqual(argv, ["/opt/v1/opencode"])
        self.assertTrue(report.supported)

    def test_primary_kept_when_in_range(self):
        mapping = {"/opt/a/opencode": "1.18.34", "/opt/b/opencode": "1.19.0"}
        with mock.patch.object(versions, "probe_version", _probe(mapping)):
            argv, report, used = versions.select_command(
                "opencode", [["/opt/a/opencode"], ["/opt/b/opencode"]])
        self.assertFalse(used)
        self.assertEqual(argv, ["/opt/a/opencode"])
        self.assertTrue(report.supported)

    def test_fallback_out_of_range_is_not_used(self):
        mapping = {"/opt/a/opencode": "2.0.0", "/opt/b/opencode": "2.5.0"}
        with mock.patch.object(versions, "probe_version", _probe(mapping)):
            argv, report, used = versions.select_command(
                "opencode", [["/opt/a/opencode"], ["/opt/b/opencode"]])
        self.assertFalse(used)
        self.assertEqual(argv, ["/opt/a/opencode"])
        self.assertTrue(report.blocked)

    def test_best_in_range_candidate_wins(self):
        mapping = {"/opt/a/opencode": "2.0.0", "/opt/b/opencode": "1.17.0",
                   "/opt/c/opencode": "1.19.0"}
        with mock.patch.object(versions, "probe_version", _probe(mapping)):
            argv, report, used = versions.select_command(
                "opencode",
                [["/opt/a/opencode"], ["/opt/b/opencode"], ["/opt/c/opencode"]])
        self.assertTrue(used)
        self.assertEqual(argv, ["/opt/c/opencode"])

    def test_unreadable_version_never_blocks(self):
        with mock.patch.object(versions, "probe_version", lambda argv: None):
            report = versions.evaluate("opencode", ["/opt/x/opencode"])
        self.assertIsNone(report.supported)
        self.assertFalse(report.blocked)
        self.assertIsNone(versions.blocked_reason(report))

    def test_custom_command_is_not_gated(self):
        with mock.patch.object(versions, "probe_version", lambda argv: "2.0.0"):
            report = versions.evaluate("opencode", ["/opt/wrapper-but-not-opencode"])
        self.assertIs(report.supported, False)
        self.assertFalse(report.canonical)
        self.assertFalse(report.blocked)  # a custom command is trusted


class SettingsAutoSelect(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-gate-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        # The binary must be named after the provider for the gate to apply.
        self.primary = self._exe("v2", "opencode")
        self.fallback = self._exe("v1", "opencode")
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.d})
        env.start()
        self.addCleanup(env.stop)
        # Keep discovery deterministic (no real ~/.local/share/mise on the test host).
        discover = mock.patch("crewhall.harness.versions.discover_candidates", return_value=[])
        discover.start()
        self.addCleanup(discover.stop)

    def _exe(self, subdir: str, name: str) -> str:
        directory = os.path.join(self.d, subdir)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return path

    def test_effective_command_switches_to_fallback(self):
        settings.patch({"providers.opencode.command": self.primary,
                        "providers.opencode.fallback_command": self.fallback}, confirm=True)
        mapping = {self.primary: "2.0.0", self.fallback: "1.18.34"}
        with mock.patch.object(versions, "probe_version", _probe(mapping)):
            argv, report, used = settings.provider_effective_command("opencode")
        self.assertTrue(used)
        self.assertEqual(argv, [self.fallback])
        self.assertTrue(report.supported)

    def test_check_provider_flags_blocked(self):
        settings.patch({"providers.opencode.command": self.primary}, confirm=True)
        with mock.patch.object(versions, "probe_version", lambda argv: "2.0.0"):
            out = settings.check_provider("opencode")
        self.assertTrue(out["blocked"])
        self.assertIs(out["supported"], False)
        self.assertFalse(out["fallback_used"])


class GateBlocks(unittest.TestCase):
    def setUp(self):
        self.controller = Controller(adopt=False)

    def _harness(self, name: str) -> OpenCodeHarness:
        return OpenCodeHarness(FakeSession(screen=SCREEN, name=name), name=name)

    def test_enforce_version_raises(self):
        with mock.patch("crewhall.settings.get", return_value=False):
            with self.assertRaises(UnsupportedVersionError):
                self.controller._enforce_version("opencode", _report(False, "2.0.0"))

    def test_enforce_version_allowed_by_setting(self):
        with mock.patch("crewhall.settings.get", return_value=True):
            self.controller._enforce_version("opencode", _report(False, "2.0.0"))

    def test_launch_command_blocks_out_of_range(self):
        with mock.patch.object(versions, "probe_version", lambda argv: "2.0.0"), \
                mock.patch("crewhall.settings.get", return_value=False):
            with self.assertRaises(UnsupportedVersionError):
                self.controller._launch_command("opencode", [])

    def test_assert_operable_blocks_marked_harness(self):
        harness = self._harness("x")
        harness.version_blocked = True
        harness.version_report = _report(False, "2.0.0")
        with self.assertRaises(UnsupportedVersionError):
            self.controller._assert_operable(harness)

    def test_assert_operable_allows_clean_harness(self):
        self.controller._assert_operable(self._harness("x"))  # no raise

    def test_send_message_refuses_blocked_recipient(self):
        sender = self._harness("a")
        recipient = self._harness("b")
        recipient.version_blocked = True
        recipient.version_report = _report(False, "2.0.0")
        self.controller.register_agent(sender)
        self.controller.register_agent(recipient)
        with self.assertRaises(MessagingError):
            self.controller.send_message("a", "b", "hi")

    def _blocked(self, name: str) -> OpenCodeHarness:
        harness = self._harness(name)
        harness.version_blocked = True
        harness.version_report = _report(False, "2.0.0")
        self.controller.register_agent(harness)
        return harness

    def test_interrupt_refuses_blocked_agent(self):
        harness = self._blocked("blocked")
        with self.assertRaises(UnsupportedVersionError):
            self.controller.interrupt_agent(harness.agent_id)

    def test_new_session_refuses_blocked_agent(self):
        harness = self._blocked("blocked")
        with self.assertRaises(UnsupportedVersionError):
            self.controller.new_session(harness)

    def test_signal_is_allowed_on_a_blocked_agent(self):
        """Containment must always work: killing processes is not gated."""
        harness = self._blocked("blocked")
        self.controller.list_processes = lambda target: {"shells": [{"pid": 11}], "others": []}
        seen: list[int] = []
        self.controller.signal_process = (
            lambda target, pid, sig, by="user": seen.append(pid) or {"pid": pid})
        out = self.controller.signal_agent(harness.agent_id, "KILL", all_shells=True)
        self.assertEqual(out["count"], 1)
        self.assertEqual(seen, [11])

    def test_relaunch_without_enforce_does_not_raise(self):
        """A restart/restore of a blocked agent must still produce a command."""
        with mock.patch.object(versions, "probe_version", lambda argv: "2.0.0"), \
                mock.patch("crewhall.settings.get", return_value=False):
            with self.assertRaises(UnsupportedVersionError):
                self.controller._launch_command("opencode", [])  # a fresh launch blocks
            argv = self.controller._launch_command("opencode", [], enforce=False)
        self.assertEqual(argv[0], "opencode")


class RestartNotGated(unittest.TestCase):
    """Forcing a restart must work even when the agent's CLI is unsupported."""

    def setUp(self):
        patcher = mock.patch.object(
            ClaudeCodeHarness, "command", classmethod(lambda cls: ["sleep"]))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="at-gate-restart-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.controller = Controller(adopt=False, persist=False)
        self.addCleanup(self.controller.shutdown)

    def test_restart_of_blocked_agent_succeeds_and_recomputes(self):
        harness = self.controller.create_agent(
            "claude", name="w", backend="pty", cwd=self.tmp)
        harness.version_blocked = True
        harness.version_report = _report(False, "2.0.0")
        result = self.controller.force_restart_agent(
            harness.agent_id, reason="unsupported", timeout=0.3)
        self.assertEqual(result["agent_id"], harness.agent_id)
        revived = self.controller.get_agent(harness.agent_id)
        # Recalculated from the real command: ["sleep"] is not the claude binary,
        # so it is reported but never blocked.
        self.assertFalse(revived.version_blocked)
        self.assertIsNotNone(revived.session.status)


class AutoDiscovery(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-disc-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def _exe(self, subdir: str, name: str = "opencode") -> str:
        directory = os.path.join(self.d, subdir)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
        return path

    def test_discover_sorts_newest_first(self):
        old = self._exe("1.17.0")
        new = self._exe("1.18.33")
        with mock.patch("crewhall.harness.versions.discovery_roots", return_value=[self.d]):
            found = [c[0] for c in versions.discover_candidates("opencode")]
        self.assertEqual(found, [new, old])

    def test_auto_switch_without_configured_fallback(self):
        good = self._exe("1.18.33")
        mapping = {good: "1.18.33", "/usr/bin/opencode": "2.0.0"}
        with mock.patch("crewhall.harness.versions.discovery_roots", return_value=[self.d]), \
                mock.patch.object(versions, "probe_version", _probe(mapping)):
            discovered = versions.discover_candidates("opencode")
            argv, report, switched = versions.select_command(
                "opencode", [["/usr/bin/opencode"], *discovered])
        self.assertTrue(switched)
        self.assertEqual(argv, [good])
        self.assertTrue(report.supported)


class NoticesStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-note-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.d})
        env.start()
        self.addCleanup(env.stop)
        from crewhall import notices

        self.notices = notices
        self.addCleanup(notices.clear)

    def test_add_dedup_and_ack(self):
        self.assertIsNotNone(self.notices.add(key="k1", title="t", body="b"))
        self.assertIsNone(self.notices.add(key="k1", title="t", body="b"))  # deduped
        self.assertEqual(len(self.notices.pending()), 1)
        self.notices.ack()
        self.assertEqual(self.notices.pending(), [])

    def test_switch_notice_recorded_once(self):
        controller = Controller(adopt=False)
        primary = _report(False, "2.0.0")
        chosen = _report(True, "1.18.33")
        with mock.patch("crewhall.watch.notify", return_value=True):
            controller._record_version_notice("opencode", primary, chosen, True)
            controller._record_version_notice("opencode", primary, chosen, True)
        pending = self.notices.pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("switched automatically", pending[0]["title"])


if __name__ == "__main__":
    unittest.main()
