"""Layer 1 (versions), layer 2 (screen detectors) and layer 7 (timers)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import health, watch

SCREENS = os.path.join(os.path.dirname(__file__), "fixtures", "screens")
KINDS = ("claude", "codex", "opencode")


def _fixture(kind: str, name: str) -> str:
    with open(os.path.join(SCREENS, kind, name), encoding="utf-8") as fh:
        return fh.read()


class VersionWatch(unittest.TestCase):
    """Layer 1: a changed CLI version triggers layer 2 and a notification."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-watch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(health, "health_dir", return_value=self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_change_triggers_layer_two_and_notifies(self):
        with mock.patch.object(watch, "current_versions",
                               return_value={"claude": "2.1.0", "codex": "0.1.0",
                                             "opencode": "1.0.0"}), \
                mock.patch.object(watch, "capture_screens",
                                  return_value={"claude": {"ok": True}}) as capture, \
                mock.patch.object(watch, "notify", return_value=True) as notify:
            health.write_versions({"claude": "2.0.0", "codex": "0.1.0", "opencode": "1.0.0"})
            report = watch.check_versions()
        self.assertEqual(report["changed"], ["claude"])
        capture.assert_called_once_with(("claude",))
        notify.assert_called_once()
        self.assertEqual(health.read_versions()["versions"]["claude"], "2.1.0")

    def test_no_change_does_not_capture(self):
        with mock.patch.object(watch, "current_versions",
                               return_value={"claude": "2.1.0", "codex": "0.1.0",
                                             "opencode": "1.0.0"}), \
                mock.patch.object(watch, "capture_screens") as capture, \
                mock.patch.object(watch, "notify") as notify:
            health.write_versions({"claude": "2.1.0", "codex": "0.1.0", "opencode": "1.0.0"})
            report = watch.check_versions()
        self.assertEqual(report["changed"], [])
        capture.assert_not_called()
        notify.assert_not_called()

    def test_changed_kinds_ignores_missing_cli(self):
        self.assertEqual(watch.changed_kinds({"claude": "1.0"}, {"claude": None}), [])


class ScreenDetectors(unittest.TestCase):
    """Layer 2: the adapter detectors pass real captures and fail altered ones."""

    def test_existing_captures_pass_every_detector(self):
        for kind in KINDS:
            report = watch.screen_report(kind, _fixture(kind, "ready.txt"))
            self.assertEqual(watch.failing_detectors(report), [], kind)
            self.assertTrue(all(report.values()), (kind, report))

    def test_altered_footer_fails(self):
        ready = _fixture("claude", "ready.txt")
        altered = "\n".join(line for line in ready.splitlines() if "auto mode" not in line)
        report = watch.screen_report("claude", altered)
        self.assertIn("footer", watch.failing_detectors(report))
        self.assertFalse(watch.screens_ok({"claude": {"ok": False, "detectors": report}}))

    def test_altered_input_box_fails(self):
        ready = _fixture("codex", "ready.txt")
        altered = "\n".join(line.replace("\u203a", ">") for line in ready.splitlines())
        report = watch.screen_report("codex", altered)
        self.assertIn("input_box", watch.failing_detectors(report))

    def test_screens_ok_requires_every_kind(self):
        self.assertTrue(watch.screens_ok({"claude": {"ok": True}}))
        self.assertFalse(watch.screens_ok({}))


class WatchInstall(unittest.TestCase):
    """Layer 7: `watch install` writes and removes the user timers."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-timers-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_install_and_remove_timers(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp}), \
                mock.patch.object(watch, "_systemctl", return_value={"ok": True}) as systemctl:
            out = watch.install_timers()
            unit_dir = os.path.join(self.tmp, "systemd", "user")
            for path in out["files"]:
                self.assertTrue(os.path.exists(path), path)
            self.assertTrue(os.path.exists(
                os.path.join(unit_dir, "crewhall-watch-versions.timer")))
            self.assertIn("OnCalendar", watch.timer_text("crewhall-watch-versions"))
            self.assertIn("watch versions", watch.service_text("crewhall-watch-versions"))
            self.assertTrue(systemctl.called)
            watch.remove_timers()
            self.assertFalse(os.path.exists(
                os.path.join(unit_dir, "crewhall-watch-versions.timer")))

    def test_remove_is_idempotent(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp}), \
                mock.patch.object(watch, "_systemctl", return_value={"ok": True}):
            watch.remove_timers()
            watch.remove_timers()


if __name__ == "__main__":
    unittest.main()
