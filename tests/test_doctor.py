from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from crewhall import doctor


class DoctorChecks(unittest.TestCase):
    def test_check_python(self) -> None:
        self.assertEqual(doctor.check_python()["status"], "ok")

    def test_check_tmux_found(self) -> None:
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(doctor, "_version_of", return_value="tmux 3.4"):
            self.assertEqual(doctor.check_tmux()["status"], "ok")

    def test_check_tmux_missing(self) -> None:
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor.check_tmux()["status"], "fail")

    def test_check_agent_clis_missing_and_found(self) -> None:
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            results = doctor.check_agent_clis()
        self.assertTrue(any(r["status"] == "fail" for r in results))
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(doctor, "_version_of", return_value="1.0"):
            results = doctor.check_agent_clis()
        self.assertTrue(all(r["status"] == "ok" for r in results))

    def test_check_command_on_path_missing(self) -> None:
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor.check_command_on_path()["status"], "warn")

    def test_check_dirs_ok(self) -> None:
        results = doctor.check_dirs()
        self.assertEqual(len(results), 2)

    def test_check_dirs_loose_permissions(self) -> None:
        target = tempfile.mkdtemp(prefix="at-doc-")
        os.chmod(target, 0o755)
        try:
            with mock.patch.object(doctor.paths, "state_dir", return_value=target), \
                    mock.patch.object(doctor.paths, "runtime_dir", return_value=target):
                results = doctor.check_dirs()
            self.assertTrue(any(r["status"] == "warn" for r in results))
        finally:
            os.chmod(target, 0o700)

    def test_check_tmpdir_relative(self) -> None:
        with mock.patch.dict(os.environ, {"TMPDIR": "relative/dir"}):
            self.assertEqual(doctor.check_tmpdir()["status"], "warn")

    def test_check_daemon_paths(self) -> None:
        with mock.patch("crewhall.client.ping", return_value=False):
            self.assertEqual(doctor.check_daemon()["status"], "warn")
        with mock.patch("crewhall.client.ping", return_value=True), \
                mock.patch("crewhall.client.Client.call", return_value={"version": "1.0"}):
            self.assertEqual(doctor.check_daemon()["status"], "ok")

    def test_check_workers(self) -> None:
        with mock.patch("crewhall.client.ping", return_value=False):
            self.assertEqual(doctor.check_workers()["status"], "ok")
        health = {"workers": [{"name": "w", "state": "dead", "restarts": 2}]}
        with mock.patch("crewhall.client.ping", return_value=True), \
                mock.patch("crewhall.client.Client.call", return_value=health):
            self.assertEqual(doctor.check_workers()["status"], "warn")

    def test_check_daemon_log(self) -> None:
        self.assertEqual(doctor.check_daemon_log()["name"], "log")

    def test_check_web_off(self) -> None:
        with mock.patch("crewhall.client.ping", return_value=False), \
                mock.patch.object(doctor.socket, "create_connection", side_effect=OSError):
            results = doctor.check_web()
        self.assertTrue(any(r["name"] == "web" for r in results))

    def test_check_web_status_errors(self) -> None:
        status = {"mode": "local", "frontends": {
            "local": {"url": "http://x", "running": False, "error": "boom"}}}
        with mock.patch("crewhall.client.ping", return_value=True), \
                mock.patch("crewhall.client.Client.call", return_value=status):
            results = doctor.check_web()
        self.assertTrue(any(r["status"] == "warn" for r in results))

    def test_check_systemd(self) -> None:
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor.check_systemd()["status"], "warn")
        out = mock.Mock(stdout="running\n", stderr="")
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/systemctl"), \
                mock.patch.object(doctor.subprocess, "run", return_value=out):
            self.assertEqual(doctor.check_systemd()["status"], "ok")

    def test_check_agent_version_ranges_out_of_range(self) -> None:
        from crewhall.harness import versions

        report = versions.VersionReport(
            "claude", ("claude",), "9.9.9", (9, 9, 9), False, "2.1.284", "outside the range")
        with mock.patch("crewhall.settings.provider_effective_command",
                        return_value=(["claude"], report, False)), \
                mock.patch("crewhall.settings.get", return_value=False):
            results = doctor.check_agent_version_ranges()
        self.assertTrue(any(r["status"] == "warn" for r in results))

    def test_check_tailscale(self) -> None:
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor.check_tailscale()["status"], "warn")
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/tailscale"):
            self.assertEqual(doctor.check_tailscale()["status"], "ok")

    def test_check_install_pending(self) -> None:
        st = {"channel": "release", "managed": True, "restart_pending": True,
              "daemon_version": "0.1", "version": "0.2"}
        with mock.patch("crewhall.updater.status", return_value=st):
            results = doctor.check_install()
        self.assertTrue(any(r["name"] == "daemon code" for r in results))

    def test_check_hooks(self) -> None:
        with mock.patch("crewhall.hooks.ensure_claude_settings", return_value="ok"):
            self.assertEqual(doctor.check_hooks()["status"], "ok")
        with mock.patch("crewhall.hooks.ensure_claude_settings", side_effect=OSError(13, "no")):
            self.assertEqual(doctor.check_hooks()["status"], "warn")

    def test_check_agent_configs_risky(self) -> None:
        home = tempfile.mkdtemp(prefix="at-home-")
        os.makedirs(os.path.join(home, ".codex"))
        with open(os.path.join(home, ".codex", "config.toml"), "w") as fh:
            fh.write('approval_policy = "never"')
        os.makedirs(os.path.join(home, ".claude"))
        with open(os.path.join(home, ".claude", "settings.json"), "w") as fh:
            fh.write('{"bypassPermissions": true}')
        with mock.patch.object(doctor.os.path, "expanduser", return_value=home):
            results = doctor.check_agent_configs()
        self.assertTrue(any(r["status"] == "warn" for r in results))

    def test_check_security(self) -> None:
        results = doctor.check_security()
        self.assertIsInstance(results, list)

    def test_check_office_disabled_and_unset(self) -> None:
        with mock.patch("crewhall.settings.get", return_value=False):
            self.assertEqual(doctor.check_office()["status"], "ok")

        def _get(key, default=None):
            return {"office.enabled": True, "office.public_url": ""}.get(key, default)

        with mock.patch("crewhall.settings.get", side_effect=_get):
            self.assertEqual(doctor.check_office()["status"], "warn")

    def test_run_and_format_and_exit(self) -> None:
        # run_checks with a minimal CHECKS set (avoid subprocess/network).
        with mock.patch.object(doctor, "CHECKS", [doctor.check_python, lambda: 1 / 0]):
            out = doctor.run_checks()
        self.assertTrue(any(r["status"] == "warn" for r in out))
        report = doctor.format_report(out)
        self.assertIn("crewhall", report)
        self.assertEqual(doctor.exit_code([{"status": "ok", "name": "x", "detail": "", "hint": ""}]), 0)
        self.assertEqual(doctor.exit_code([{"status": "fail", "name": "x", "detail": "", "hint": ""}]), 1)

    def test_fix_control_files(self) -> None:
        directory = tempfile.mkdtemp(prefix="at-cf-")
        with open(os.path.join(directory, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write("# rules\n")
        results = doctor.fix_control_files([directory])
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()
