"""Stage 2 unit tests: opt-in sandbox profiles (Part 1) and profile data (Part 2).

These do not launch anything: they prove that the daemon resolves a name to a
profile fail-closed and that the launcher argv carries exactly that profile.
The execution boundary itself is tested against bwrap in
``tests/security/test_sandbox_profiles.py``.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import sandbox, settings
from crewhall.sandbox_launcher import LaunchError, build_argv
from crewhall.types import SessionSpec


class SandboxConfigBase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="sandbox-home-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "HOME": self.home,
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            "XDG_STATE_HOME": os.path.join(self.home, ".local", "state"),
            "XDG_RUNTIME_DIR": os.path.join(self.home, "run"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        for name in (".config/crewhall", ".local/state/crewhall", ".claude/projects",
                     ".config/opencode", ".local/share/mise", ".local/bin",
                     "Projects/indexion-project", "Projects/indexion-frontend",
                     "Projects/session"):
            os.makedirs(os.path.join(self.home, name), exist_ok=True)
        settings._CACHE.update(stamp=None, data=None)
        self.addCleanup(settings._CACHE.update, stamp=None, data=None)

    def write(self, data: dict) -> None:
        os.makedirs(settings.config_dir(), mode=0o700, exist_ok=True)
        with open(settings.path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        settings._CACHE.update(stamp=None, data=None)

    def profile(self, **over) -> dict:
        body = {"binds": [], "env": {}, "network": "host", "deny": [],
                "workspace": "rw", **over}
        return {"profiles": {"role": body},
                "agents": [{"name_pattern": "Role_[0-9]+", "profile": "role"}]}

    def homedir(self, *parts: str) -> str:
        return os.path.join(self.home, *parts)


class OptIn(SandboxConfigBase):
    def test_absent_section_launches_the_original_argv(self) -> None:
        self.write({"providers": {"claude": {"command": "/bin/echo"}}})
        self.assertEqual(sandbox.decision_for("Heraldo"), (None, None))
        spec = SessionSpec(command=["/bin/echo", "hola"], cwd=self.home)
        self.assertEqual(sandbox.command_for_spec(spec), ["/bin/echo", "hola"])

    def test_empty_section_also_launches_unchanged(self) -> None:
        self.write({"sandbox": {"profiles": {}, "agents": []}})
        self.assertEqual(sandbox.launch_values("Heraldo"),
                         {"sandboxed": False, "sandbox_profile": None})

    def test_pattern_is_a_full_match(self) -> None:
        self.write({"sandbox": self.profile()})
        self.assertEqual(sandbox.decision_for("Role_1"), ("role", None))
        self.assertEqual(sandbox.decision_for("xRole_1"), (None, None))
        self.assertEqual(sandbox.decision_for("Role_1x"), (None, None))

    def test_name_matched_but_profile_unknown_is_refused(self) -> None:
        cfg = self.profile()
        cfg["agents"][0]["profile"] = "missing"
        self.write({"sandbox": cfg})
        profile, error = sandbox.decision_for("Role_1")
        self.assertIsNone(profile)
        self.assertIn("unknown", error)
        self.assertEqual(sandbox.decision_for("Otro"), (None, None))
        with self.assertRaises(sandbox.SandboxError):
            sandbox.launch_values("Role_1")

    def test_unknown_profile_key_poisons_every_launch(self) -> None:
        cfg = self.profile()
        cfg["profiles"]["role"]["capabilities"] = ["ALL"]
        self.write({"sandbox": cfg})
        self.assertIn("unknown keys", sandbox.decision_for("Role_1")[1])
        # A typo means the daemon cannot know which names were meant to be
        # sandboxed: nothing local is launched silently.
        self.assertIn("unknown keys", sandbox.decision_for("Heraldo")[1])

    def test_bad_regex_poisons_every_launch(self) -> None:
        cfg = self.profile()
        cfg["agents"][0]["name_pattern"] = "Role_["
        self.write({"sandbox": cfg})
        self.assertIn("bad regex", sandbox.decision_for("Role_1")[1])
        self.assertIn("bad regex", sandbox.decision_for("Heraldo")[1])

    def test_unknown_top_level_key_poisons_every_launch(self) -> None:
        self.write({"sandbox": {**self.profile(), "profile": {}}})
        self.assertIn("unknown keys", sandbox.decision_for("Heraldo")[1])


class ProfileValidation(SandboxConfigBase):
    def test_rw_bind_outside_home_or_tmp_is_refused(self) -> None:
        self.write({"sandbox": self.profile(
            binds=[{"path": "/etc", "mode": "rw"}])})
        self.assertIn("refusing", sandbox.decision_for("Role_1")[1])

    def test_rw_bind_of_home_is_refused(self) -> None:
        self.write({"sandbox": self.profile(
            binds=[{"path": self.home, "mode": "rw"}])})
        self.assertIn("refusing", sandbox.decision_for("Role_1")[1])

    def test_control_plane_bind_is_refused_even_read_only(self) -> None:
        self.write({"sandbox": self.profile(
            binds=[{"path": settings.path(), "mode": "ro"}])})
        self.assertIn("overlaps", sandbox.decision_for("Role_1")[1])

    def test_env_cannot_override_identity(self) -> None:
        self.write({"sandbox": self.profile(env={"CREWHALL_TOKEN": "x"})})
        self.assertIn("managed by crewhall", sandbox.decision_for("Role_1")[1])

    def test_missing_required_bind_is_refused_but_optional_is_skipped(self) -> None:
        self.write({"sandbox": self.profile(
            binds=[{"path": self.homedir("nope"), "mode": "ro"}])})
        self.assertIn("does not exist", sandbox.decision_for("Role_1")[1])
        self.write({"sandbox": self.profile(
            binds=[{"path": self.homedir("nope"), "mode": "ro", "optional": True}])})
        self.assertEqual(sandbox.decision_for("Role_1"), ("role", None))

    def test_network_must_be_defined_and_known(self) -> None:
        self.write({"sandbox": self.profile(network="bridge")})
        self.assertIn("network", sandbox.decision_for("Role_1")[1])


class LauncherArgv(SandboxConfigBase):
    def cfg(self, **profile) -> dict:
        body = {"binds": [], "env": {}, "network": "host", "deny": [],
                "workspace": "rw", **profile}
        return {
            "bwrap": "/usr/bin/bwrap", "launcher": "/x",
            "argv": ["/bin/true"], "cwd": self.homedir("Projects", "indexion-project"),
            "workspace": self.homedir("Projects", "indexion-project"),
            "agent_id": "sess_1", "state_dir": self.homedir(".local/state/crewhall"),
            "config_dir": self.homedir(".config/crewhall"),
            "runtime_dir": os.environ["XDG_RUNTIME_DIR"], "socket": None,
            "tmpdir": None, "cache_dir": None, "opencode_state": None,
            "home": self.home, "tmux_tmpdir": None, "container_sockets": [],
            "profile": {"name": "role", **body},
        }

    def test_host_network_is_the_default_and_none_unshares(self) -> None:
        self.assertNotIn("--unshare-net", build_argv(self.cfg()))
        self.assertIn("--unshare-net", build_argv(self.cfg(network="none")))

    def test_workspace_ro_and_none(self) -> None:
        ro = build_argv(self.cfg(workspace="ro"))
        ws = self.cfg()["workspace"]
        self.assertIn(("--ro-bind", ws, ws), _triples(ro))
        self.assertNotIn(("--bind", ws, ws), _triples(ro))
        none = build_argv(self.cfg(workspace="none"))
        self.assertNotIn(ws, [dst for _flag, _src, dst in _triples(none)])

    def test_binds_and_denies_are_applied(self) -> None:
        allowed = self.homedir("Projects", "indexion-project")
        denied = self.homedir("deploy")
        os.makedirs(denied, exist_ok=True)
        argv = build_argv(self.cfg(
            binds=[{"path": allowed, "mode": "ro"}], deny=[denied]))
        self.assertIn(("--ro-bind", allowed, allowed), _triples(argv))
        self.assertIn("--tmpfs", argv)
        self.assertIn(denied, argv)
        # A directory deny is a tmpfs *after* the binds.
        self.assertGreater(argv.index(denied), argv.index(allowed))

    def test_missing_non_optional_bind_fails_closed(self) -> None:
        with self.assertRaises(LaunchError):
            build_argv(self.cfg(
                binds=[{"path": self.homedir("missing"), "mode": "ro"}]))


def _triples(argv: list[str]) -> list[tuple[str, ...]]:
    out = []
    for index, token in enumerate(argv[:-1]):
        if token in ("--bind", "--ro-bind"):
            out.append((token, argv[index + 1], argv[index + 2]))
    return out


if __name__ == "__main__":
    unittest.main()
