from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest

from agent_terminal import brand, control_files


class BrandEnv(unittest.TestCase):
    def setUp(self) -> None:
        brand._warned.clear()

    def test_new_prefix_wins_and_legacy_falls_back(self):
        saved = {k: os.environ.get(k) for k in ("CREWHALL_FOO", "AGENT_TERMINAL_FOO")}
        self.addCleanup(self._restore, saved)
        os.environ.pop("CREWHALL_FOO", None)
        os.environ["AGENT_TERMINAL_FOO"] = "legacy"
        self.assertEqual(brand.env("FOO"), "legacy")
        os.environ["CREWHALL_FOO"] = "new"
        self.assertEqual(brand.env("FOO"), "new")

    @staticmethod
    def _restore(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_new_directories_are_active(self):
        self.assertTrue(brand.USE_NEW_PATHS)
        self.assertEqual(brand.NAME, "crewhall")
        self.assertEqual(brand.dir_name("state"), "crewhall")

    def test_first_access_copies_legacy_config_and_keeps_the_original(self):
        root = tempfile.mkdtemp(prefix="at-brand-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        saved = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME",)}
        self.addCleanup(self._restore, saved)
        os.environ["XDG_CONFIG_HOME"] = root
        legacy = os.path.join(root, "agent-terminal")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "web-token"), "w") as fh:
            fh.write("secret")
        new = brand.config_dir()
        self.assertEqual(new, os.path.join(root, "crewhall"))
        with open(os.path.join(new, "web-token")) as fh:
            self.assertEqual(fh.read(), "secret")
        self.assertTrue(os.path.isfile(os.path.join(legacy, "web-token")))
        self.assertEqual(brand.config_dir(), new)  # idempotent


class Migration(unittest.TestCase):
    def test_migrate_copies_and_never_deletes_the_source(self):
        root = tempfile.mkdtemp(prefix="at-brand-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        old = os.path.join(root, "old")
        new = os.path.join(root, "new")
        os.makedirs(os.path.join(old, "state"))
        with open(os.path.join(old, "state", "state.json"), "w") as fh:
            fh.write("{}")
        plan = brand.migrate_tree(old, new, dry_run=True)
        self.assertTrue(any(p["status"] == "would-copy" for p in plan))
        self.assertFalse(os.path.exists(new))
        done = brand.migrate_tree(old, new)
        self.assertTrue(any(p["status"] == "copied" for p in done))
        self.assertTrue(os.path.isfile(os.path.join(new, "state", "state.json")))
        self.assertTrue(os.path.isfile(os.path.join(old, "state", "state.json")))

    def test_tmux_socket_env_has_a_crewhall_fallback(self):
        from agent_terminal.backends import tmux

        saved = {k: os.environ.get(k) for k in ("CREWHALL_TMUX_SOCKET", "AGENT_TERMINAL_TMUX_SOCKET")}
        self.addCleanup(Migration._restore, saved)
        os.environ.pop("CREWHALL_TMUX_SOCKET", None)
        os.environ["AGENT_TERMINAL_TMUX_SOCKET"] = "at_legacy_socket"
        self.assertEqual(tmux.socket_name(), "at_legacy_socket")
        os.environ["CREWHALL_TMUX_SOCKET"] = "at_new_socket"
        self.assertEqual(tmux.socket_name(), "at_new_socket")

    @staticmethod
    def _restore(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ControlMarkers(unittest.TestCase):
    def test_writes_new_markers(self):
        self.assertEqual(control_files.BEGIN, brand.CONTROL_BEGIN)
        self.assertIn("CREWHALL", control_files.BEGIN)

    def test_finds_and_refreshes_a_legacy_block(self):
        data = (f"header\n{brand.LEGACY_CONTROL_BEGIN}\nold body\n"
                f"{brand.LEGACY_CONTROL_END}\ntail\n").encode()
        found = control_files._find_block(data)
        self.assertIsNotNone(found)
        start, stop = found
        self.assertIn(b"old body", data[start:stop])


class RenameCheck(unittest.TestCase):
    def _report(self, name):
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "rename_check.py")
        spec = importlib.util.spec_from_file_location("rename_check", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.report(name)

    def test_dry_run_reports_occurrences_for_a_fictitious_name(self):
        data = self._report("frobnicator")
        self.assertEqual(data["new_name"], "frobnicator")
        self.assertEqual(data["new_env_prefix"], "FROBNICATOR")
        self.assertGreater(data["files"], 0)
        self.assertGreater(data["occurrences"], 0)
        self.assertGreater(data["env_occurrences"], 0)


if __name__ == "__main__":
    unittest.main()
