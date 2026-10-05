from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from agent_terminal import settings


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-set-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        # A private executable: the settings check rejects binaries writable by other
        # users, and a CI toolchain's python often is.
        self.exe = os.path.join(self.d, "fake-agent")
        with open(self.exe, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.exe, 0o755)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.d})
        env.start(); self.addCleanup(env.stop)
        for var in ("CREWHALL_HOOKS", "CREWHALL_CONVERSATIONS", "CREWHALL_PERMISSION_WAIT",
                    "CREWHALL_CONTROL_FILES"):
            p = mock.patch.dict(os.environ); p.start(); self.addCleanup(p.stop)
            os.environ.pop(var, None)


class Basics(_Base):
    def test_defaults_without_a_file(self):
        self.assertEqual(settings.get("security.session_ttl_hours"), 12)
        self.assertEqual(settings.enabled_kinds(), ["claude", "codex", "opencode"])
        self.assertFalse(os.path.exists(settings.path()))

    def test_patch_persists_atomically_with_private_mode(self):
        settings.patch({"security.session_ttl_hours": 4, "maintenance.tmp_max_mb": 128})
        self.assertEqual(settings.get("security.session_ttl_hours"), 4)
        self.assertEqual(stat.S_IMODE(os.stat(settings.path()).st_mode), 0o600)
        self.assertFalse(os.path.exists(settings.path() + ".part"))
        self.assertEqual(json.load(open(settings.path()))["security"]["session_ttl_hours"], 4)

    def test_invalid_values_are_rejected_and_nothing_is_written(self):
        for key, bad in (("security.session_ttl_hours", 0), ("security.session_ttl_hours", "9"),
                         ("security.session_ttl_hours", True), ("agents.default_backend", "zzz"),
                         ("agents.hooks", "yes"), ("security.allow_hosts", ["bad host"]),
                         ("nope.key", 1)):
            with self.assertRaises(settings.SettingsError, msg=(key, bad)):
                settings.patch({key: bad})
        self.assertFalse(os.path.exists(settings.path()))

    def test_a_partly_bad_patch_changes_nothing(self):
        with self.assertRaises(settings.SettingsError):
            settings.patch({"security.session_ttl_hours": 5, "maintenance.tmp_max_mb": 1})
        self.assertEqual(settings.get("security.session_ttl_hours"), 12)

    def test_corrupt_or_hostile_file_falls_back_to_defaults(self):
        os.makedirs(settings.config_dir())
        for text in ("{not json", json.dumps({"security": {"session_ttl_hours": "x"}, "providers": {"claude": {"enabled": "no"}}}),
                     json.dumps([1, 2])):
            open(settings.path(), "w").write(text)
            self.assertEqual(settings.get("security.session_ttl_hours"), 12, text)
            self.assertTrue(settings.provider_enabled("claude"))

    def test_environment_wins_and_is_reported(self):
        settings.patch({"agents.permission_wait": 30})
        os.environ["CREWHALL_PERMISSION_WAIT"] = "7"
        self.assertEqual(settings.get("agents.permission_wait"), 7)
        row = next(i for i in settings.describe()["items"] if i["key"] == "agents.permission_wait")
        self.assertEqual(row["env"], "CREWHALL_PERMISSION_WAIT")
        os.environ["CREWHALL_HOOKS"] = "0"
        self.assertFalse(settings.get("agents.hooks"))

    def test_reset_all_or_a_group(self):
        settings.patch({"security.session_ttl_hours": 4, "maintenance.tmp_max_mb": 128})
        settings.reset("security")
        self.assertEqual(settings.get("security.session_ttl_hours"), 12)
        self.assertEqual(settings.get("maintenance.tmp_max_mb"), 128)
        settings.reset()
        self.assertEqual(settings.get("maintenance.tmp_max_mb"), 512)

    def test_allow_hosts_accepts_text_lines(self):
        settings.patch({"security.allow_hosts": "box.local\n\n 10.0.0.5 \n"})
        self.assertEqual(settings.get("security.allow_hosts"), ["box.local", "10.0.0.5"])


class Providers(_Base):
    def test_disable_one_but_never_all_or_the_default(self):
        settings.patch({"providers.claude.enabled": False, "providers.codex.enabled": False,
                        "agents.default_kind": "opencode"})
        self.assertEqual(settings.enabled_kinds(), ["opencode"])
        with self.assertRaises(settings.SettingsError):
            settings.patch({"providers.opencode.enabled": False})  # none left
        # The default provider can never be disabled while it is the default.
        settings.patch({"providers.claude.enabled": True, "providers.codex.enabled": True})
        with self.assertRaises(settings.SettingsError):
            settings.patch({"providers.opencode.enabled": False, "agents.default_kind": "opencode"})
        settings.patch({"providers.codex.enabled": False, "providers.opencode.enabled": False,
                        "agents.default_kind": "claude"})
        self.assertEqual(settings.enabled_kinds(), ["claude"])

    def test_command_override_args_and_model(self):
        self.assertIsNone(settings.provider_command("claude"))
        settings.patch({"providers.claude.command": f"{self.exe} --flag",
                        "providers.claude.default_args": "--verbose",
                        "providers.claude.default_model": "opus"}, confirm=True)
        self.assertEqual(settings.provider_command("claude"), [self.exe, "--flag"])
        self.assertEqual(settings.provider_args("claude"), ["--model", "opus", "--verbose"])
        # an explicit model on the agent wins over the provider default
        self.assertEqual(settings.provider_args("claude", ["--model", "haiku"]), ["--verbose"])
        self.assertEqual(settings.provider_args("claude", ["--model=haiku"]), ["--verbose"])

    def test_bad_provider_values(self):
        for key, bad in (("providers.claude.command", "unclosed 'quote"), ("providers.claude.command", "a\nb"),
                         ("providers.claude.enabled", "yes"), ("providers.claude.env", "NOEQUALS"),
                         ("providers.claude.env", {"1BAD": "x"}), ("providers.claude.env", {"A B": "x"}),
                         ("providers.ghost.enabled", True)):
            with self.assertRaises(settings.SettingsError, msg=(key, bad)):
                settings.patch({key: bad}, confirm=True)

    def test_secret_env_is_masked_in_describe_and_kept_when_masked_value_returns(self):
        settings.patch({"providers.claude.env": "ANTHROPIC_API_KEY=sk-secret\nREGION=eu"}, confirm=True)
        row = next(p for p in settings.describe()["providers"] if p["kind"] == "claude")
        self.assertEqual(row["env"], {"ANTHROPIC_API_KEY": settings.MASK, "REGION": "eu"})
        self.assertNotIn("sk-secret", json.dumps(settings.describe()))
        settings.patch({"providers.claude.env": {"ANTHROPIC_API_KEY": settings.MASK, "REGION": "us"}}, confirm=True)
        self.assertEqual(settings.provider_env("claude"), {"ANTHROPIC_API_KEY": "sk-secret", "REGION": "us"})

    def test_check_provider_finds_or_explains(self):
        ok = settings.check_provider("claude", "python3")
        self.assertTrue(ok["ok"]); self.assertTrue(ok["path"])
        bad = settings.check_provider("claude", "definitely-not-a-binary-xyz")
        self.assertFalse(bad["ok"]); self.assertIn("not found", bad["error"])
        self.assertFalse(settings.check_provider("claude", "'unclosed")["ok"])


class AppliedByTheController(_Base):
    def test_disabled_provider_cannot_start_agents_and_command_is_overridden(self):

        from agent_terminal import Controller

        c = Controller(adopt=False)
        settings.patch({"providers.claude.command": self.exe, "providers.claude.default_model": "opus"},
                       confirm=True)
        cmd = c._launch_command("claude", ["--a"])
        self.assertEqual(cmd[0], self.exe)
        settings.patch({"providers.claude.enabled": False})
        with self.assertRaises(ValueError) as cm:
            c.create_agent("claude", name="x")
        self.assertIn("disabled", str(cm.exception))

    def test_meta_lists_only_enabled_providers_and_the_defaults(self):
        from agent_terminal.ui.control import meta_info

        settings.patch({"providers.claude.enabled": False})
        meta = meta_info()
        self.assertEqual([h["kind"] for h in meta["harnesses"]], ["codex", "opencode"])
        self.assertEqual(meta["disabled"], ["claude"])
        self.assertEqual(meta["defaults"]["kind"], "opencode")


if __name__ == "__main__":
    unittest.main()


class EnvClamp(_Base):
    def test_out_of_range_environment_values_are_clamped_not_ignored(self):
        os.environ["CREWHALL_PERMISSION_WAIT"] = "99999"
        self.assertEqual(settings.get("agents.permission_wait"), 3600)
        os.environ["CREWHALL_PERMISSION_WAIT"] = "-4"
        self.assertEqual(settings.get("agents.permission_wait"), 0)
