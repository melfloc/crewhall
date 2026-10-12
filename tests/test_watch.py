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


class ScreenCapture(unittest.TestCase):
    """Layer 2 against a (scripted) tmux: the bugs the real run exposed."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-watch-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(health, "health_dir", return_value=self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tmux(self, screens):
        """A fake ``subprocess.run`` for tmux: one screen per kind, records calls."""
        calls = []

        def run(args, **_k):
            calls.append(list(args))
            out = ""
            if "capture-pane" in args:
                target = args[args.index("-t") + 1]
                out = screens.get(target.removeprefix("watch-"), "")
            return mock.Mock(returncode=0, stdout=out, stderr="")
        return calls, run

    def test_each_cli_gets_its_own_session_and_captures_stay_out_of_the_repo(self):
        screens = {k: _fixture(k, "ready.txt") for k in ("claude", "codex", "opencode")}
        calls, run = self._tmux(screens)
        with mock.patch.object(watch.subprocess, "run", side_effect=run), \
                mock.patch.object(watch.shutil, "which", side_effect=lambda k: f"/bin/{k}"), \
                mock.patch.object(watch, "cli_version", return_value="9.9.9"), \
                mock.patch.object(watch.time, "sleep"):
            result = watch.capture_screens(settle=0.01)
        sessions = [c[c.index("-s") + 1] for c in calls if "new-session" in c]
        self.assertEqual(sessions, ["watch-claude", "watch-codex", "watch-opencode"])
        for kind, out in result.items():
            self.assertTrue(out["path"].startswith(self.tmp), out)   # state dir, not the repo
            self.assertTrue(out["path"].endswith(f"{kind}-9.9.9.txt"))  # --version, not the screen

    def test_version_label_prefers_the_cli_over_numbers_on_screen(self):
        with mock.patch.object(watch, "cli_version", return_value="opencode 1.19.0"):
            self.assertEqual(watch._version_label("opencode", "DeepSeek V4.1 Flash"), "1.19.0")

    def test_trust_dialog_is_confirmed_only_once_yes_is_selected(self):
        no = " \u276f No, exit\n   Yes, I trust this folder\n Accessing workspace:"
        yes = "   No, exit\n \u276f Yes, I trust this folder\n Accessing workspace:"
        # The first DOWN is swallowed (dialog not interactive yet), the second lands.
        frames = iter([no, no, yes])
        sent = []

        def run(args, **_k):
            if "send-keys" in args:
                sent.append(args[-1])
            screen = next(frames, yes) if "capture-pane" in args else ""
            return mock.Mock(returncode=0, stdout=screen, stderr="")

        with mock.patch.object(watch.subprocess, "run", side_effect=run), \
                mock.patch.object(watch.time, "sleep"):
            self.assertIsNone(watch._accept_trust("claude", ["tmux"], "watch-claude", 1.0))
        self.assertEqual(sent, ["Down", "Down", "Enter"])  # never Enter while "No" is selected

    def test_other_clis_trust_dialog_is_reported_not_guessed(self):
        def run(args, **_k):
            return mock.Mock(returncode=0, stdout="Do you trust the files in this folder?",
                             stderr="")
        with mock.patch.object(watch.subprocess, "run", side_effect=run), \
                mock.patch.object(watch.time, "sleep"):
            err = watch._accept_trust("codex", ["tmux"], "watch-codex", 1.0)
        self.assertIn("folder-trust dialog", err)


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


class WatchUnitsOnAnInstall(unittest.TestCase):
    def test_smoke_timer_ships_in_the_package_soak_stays_checkout_only(self):
        tmp = tempfile.mkdtemp(prefix="at-root-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(watch.paths, "project_root", return_value=tmp):
            units, skipped = watch.available_units()
        self.assertEqual(units, ["crewhall-watch-versions", "crewhall-watch-smoke"])
        self.assertEqual(set(skipped), {"crewhall-watch-soak"})
        # The installed smoke timer must run the module, not a checkout script.
        self.assertEqual(
            watch._watch_command("crewhall-watch-smoke")[1:],
            ["-P", "-m", "crewhall.cli", "watch", "smoke", "--require"])

