from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from crewhall import settings, specs
from crewhall.backends import ssh_tmux
from crewhall.types import SessionSpec


def _host(**over):
    cfg = {"ssh": "user@example", "port": 22, "identity": None,
           "tmux_socket": "crewhall", "name": "prod"}
    cfg.update(over)
    return cfg


def _completed(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class SshArgvTests(unittest.TestCase):
    def test_fixed_options_and_destination(self):
        argv = ssh_tmux.SshTmuxBackend(_host(port=2222, identity="/tmp/k"))._ssh_local_argv()
        self.assertEqual(argv[0], "ssh")
        joined = " ".join(argv)
        for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no",
                    "ForwardX11=no", "ClearAllForwardings=yes", "ConnectTimeout=5",
                    "ServerAliveInterval=15", "ServerAliveCountMax=3",
                    "ControlMaster=auto", "ControlPersist=600"):
            self.assertIn(opt, joined)
        self.assertIn("-i", argv)
        self.assertIn("/tmp/k", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1], "user@example")
        self.assertEqual(argv[-1], "user@example")

    def test_control_path_lives_in_a_private_dir(self):
        backend = ssh_tmux.SshTmuxBackend(_host())
        argv = backend._ssh_local_argv()
        control = next(a for a in argv if a.startswith("ControlPath="))
        path = control.split("=", 1)[1]
        self.assertTrue(path.endswith("/%C"))
        self.assertEqual(os.path.dirname(path), ssh_tmux.control_dir())
        self.assertEqual(stat.S_IMODE(os.stat(ssh_tmux.control_dir()).st_mode), 0o700)

    def test_control_path_outside_private_dir_is_rejected(self):
        with self.assertRaises(RuntimeError):
            ssh_tmux._check_control_path("/tmp/attacker/xyz")

    def test_no_user_supplied_ssh_option_can_be_configured(self):
        for bad in ("StrictHostKeyChecking", "ForwardAgent", "ProxyCommand",
                    "LocalCommand", "IdentityAgent", "UserKnownHostsFile"):
            with self.assertRaises(settings.SettingsError):
                settings_hosts_patch({"prod": {"ssh": "u@h", bad: "no"}})

    def test_remote_command_is_a_single_quoted_argument(self):
        backend = ssh_tmux.SshTmuxBackend(_host())
        seen: list[list[str]] = []

        def fake_run(argv, **kw):
            seen.append(argv)
            return _completed(argv)

        with mock.patch.object(ssh_tmux.subprocess, "run", fake_run):
            backend._run("send-keys", "-t", "sess_x", "-l", "--", "; touch /tmp/pwned")
        self.assertEqual(len(seen), 1)
        remote = seen[0][-1]
        self.assertEqual(
            remote,
            shlex.join(["tmux", "-L", "crewhall", "-f", "/dev/null",
                        "send-keys", "-t", "sess_x", "-l", "--", "; touch /tmp/pwned"]),
        )
        # The hostile value stays inside the single remote argument.
        self.assertIn("touch /tmp/pwned", remote)

    def test_ssh_failure_255_is_host_unreachable_not_tmux(self):
        backend = ssh_tmux.SshTmuxBackend(_host())
        with mock.patch.object(ssh_tmux.subprocess, "run",
                               lambda argv, **kw: _completed(argv, 255, err="boom")):
            with self.assertRaises(ssh_tmux.HostUnreachable):
                backend._check("has-session", "-t", "s")
            self.assertTrue(backend.meta()["host_unreachable"])

    def test_poll_unknown_when_unreachable(self):
        backend = ssh_tmux.SshTmuxBackend(_host())
        backend.tmux_name = "prod__sess_1"
        with mock.patch.object(ssh_tmux.subprocess, "run",
                               lambda argv, **kw: _completed(argv, 255)):
            self.assertIsNone(backend.poll())
        self.assertTrue(backend.meta()["host_unreachable"])
        self.assertEqual(backend.meta()["host"], "prod")

    def test_start_uses_remote_cwd_and_stdin_env(self):
        backend = ssh_tmux.SshTmuxBackend(_host())
        backend.session = mock.Mock(created_at=1.0)
        backend.session.session_id = "sess_1"
        calls: list[dict] = []

        def fake_ssh_run(remote, *, input=None, timeout=10.0):
            calls.append({"remote": remote, "input": input})
            return _completed(["ssh"], 0)

        backend._ssh_run = fake_ssh_run  # type: ignore[method-assign]
        backend._run = lambda *a, **k: _completed(list(a), 0)  # type: ignore[method-assign]
        backend._first_pane = lambda: "%0"  # type: ignore[method-assign]
        with mock.patch.object(ssh_tmux.threading, "Thread", lambda **kw: mock.Mock()):
            spec = SessionSpec(command=["/bin/bash"], cwd="/remote/work",
                               env={"SECRET_TOKEN": "hunter2"}, cols=80, rows=24)
            backend.start(spec)
        first = calls[0]
        self.assertIn("/remote/work", first["remote"])
        self.assertIn("export SECRET_TOKEN=hunter2", first["input"])
        self.assertNotIn("hunter2", first["remote"])
        self.assertIn("prod__sess_1", first["remote"])
        self.assertNotIn(os.getcwd(), first["remote"])

    def test_remote_session_name_namespacing(self):
        self.assertEqual(ssh_tmux._remote_session_name("prod", "sess_1"), "prod__sess_1")
        self.assertEqual(ssh_tmux.session_id_from_remote_name("prod", "prod__sess_1"), "sess_1")
        self.assertEqual(ssh_tmux.session_id_from_remote_name("prod", "sess_local"), "sess_local")

    def test_attach_command_allocates_a_tty(self):
        cmd = ssh_tmux.SshTmuxBackend.attach_command(_host(), "prod__sess_1")
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("-t", cmd)
        self.assertIn("attach-session", cmd)
        self.assertEqual(cmd[-1], "prod__sess_1")


class HostSettingsTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-sshset-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.d})
        p.start(); self.addCleanup(p.stop)

    def _key(self, mode=0o600):
        path = os.path.join(self.d, "id_ed25519")
        with open(path, "w") as fh:
            fh.write("KEY")
        os.chmod(path, mode)
        return path

    def test_closed_by_default(self):
        self.assertEqual(settings.hosts(), {})

    def test_valid_host_is_persisted_and_reloaded(self):
        key = self._key()
        settings.patch({"hosts": {"prod1": {"ssh": "deploy@prod1", "port": 2222,
                                            "identity": key, "tmux_socket": "crewhall"}}})
        cfg = settings.host("prod1")
        self.assertEqual(cfg["ssh"], "deploy@prod1")
        self.assertEqual(cfg["port"], 2222)
        self.assertEqual(cfg["identity"], key)
        self.assertEqual(stat.S_IMODE(os.stat(settings.path()).st_mode), 0o600)

    def test_rejects_hostile_destinations(self):
        for bad in ("u@h with space", "-u@h", "u@h;rm -rf /", "u@h|cat", "u@h&x",
                    "u@h$X", "u@h`id`", "u@h\nfoo", "u@h\ttab"):
            with self.assertRaises(settings.SettingsError, msg=bad):
                settings.patch({"hosts": {"p": {"ssh": bad}}})

    def test_rejects_unknown_keys_and_bad_values(self):
        for body in ({"ssh": "u@h", "Port": 22}, {"ssh": "u@h", "ProxyCommand": "x"},
                     {"ssh": "u@h", "port": 0}, {"ssh": "u@h", "port": 70000},
                     {"ssh": "u@h", "tmux_socket": "-bad"}, {"ssh": "u@h", "tmux_socket": "a b"}):
            with self.assertRaises(settings.SettingsError, msg=body):
                settings.patch({"hosts": {"p": body}})

    def test_identity_permissions_are_enforced(self):
        with self.assertRaises(settings.SettingsError):
            settings.patch({"hosts": {"p": {"ssh": "u@h", "identity": self._key(0o644)}}})
        with self.assertRaises(settings.SettingsError):
            settings.patch({"hosts": {"p": {"ssh": "u@h", "identity": self.d}}})

    def test_invalid_stored_host_is_dropped_on_load(self):
        os.makedirs(settings.config_dir())
        import json
        with open(settings.path(), "w") as fh:
            json.dump({"hosts": {"good": {"ssh": "u@h"}, "bad": {"ssh": "u@h;x"}}}, fh)
        self.assertEqual(list(settings.hosts()), ["good"])


def settings_hosts_patch(value):
    """Patch ``hosts`` in an isolated config dir (helper for argv tests)."""
    d = tempfile.mkdtemp(prefix="at-sshopt-")
    try:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            return settings.patch({"hosts": value})
    finally:
        shutil.rmtree(d, ignore_errors=True)


class SpecsHostTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-sshtoml-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.d})
        p.start(); self.addCleanup(p.stop)

    def _team(self, agent_extra: str) -> dict:
        data = {
            "team": {"name": "t"},
            "agent": [{"name": "a", "kind": "claude", **agent_extra}],
        }
        return data

    def test_unknown_host_is_rejected(self):
        with self.assertRaises(specs.SpecError):
            specs.parse_team_data(self._team({"host": "nope"}), self.d, profiles={})

    def test_host_forces_ssh_tmux_and_keeps_remote_cwd(self):
        settings.patch({"hosts": {"prod": {"ssh": "u@h"}}})
        out = specs.parse_team_data(
            self._team({"host": "prod", "cwd": "~/remote"}), self.d, profiles={}
        )
        entry = out["agents"][0]
        self.assertEqual(entry["host"], "prod")
        self.assertEqual(entry["backend"], "ssh-tmux")
        self.assertEqual(entry["cwd"], "~/remote")  # not expanded locally

    def test_host_with_pty_backend_is_rejected(self):
        settings.patch({"hosts": {"prod": {"ssh": "u@h"}}})
        with self.assertRaises(specs.SpecError):
            specs.parse_team_data(
                self._team({"host": "prod", "backend": "pty"}), self.d, profiles={}
            )

    def test_profile_host_is_validated(self):
        with self.assertRaises(specs.SpecError):
            specs.parse_profiles_data({"profile": {"p": {"host": "ghost"}}})


class SessionInfoHostTests(unittest.TestCase):
    def test_backend_meta_surfaces_host(self):
        backend = ssh_tmux.SshTmuxBackend(_host())
        from crewhall.session import InteractiveSession
        session = InteractiveSession(backend, SessionSpec(command=["/bin/bash"]))
        info = session.info()
        self.assertEqual(info.host, "prod")
        self.assertEqual(info.backend, "ssh-tmux")
        self.assertEqual(info.to_dict()["host"], "prod")


if __name__ == "__main__":
    unittest.main()
