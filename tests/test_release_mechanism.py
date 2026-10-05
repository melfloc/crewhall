from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from agent_terminal import __version__, signing, updater
from agent_terminal.updater import UpdateError

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_spec = importlib.util.spec_from_file_location("release_check", os.path.join(ROOT, "scripts", "release_check.py"))
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

HAVE_KEYGEN = shutil.which("ssh-keygen") is not None
GIT = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]


def sh(cwd, *cmd):
    subprocess.run(list(cmd), cwd=cwd, check=True, capture_output=True)


class VersionsAreCoherent(unittest.TestCase):
    def test_pyproject_package_and_changelog_agree_for_this_commit(self):
        self.assertEqual(rc.gate_versions(ROOT), __version__)


class ReleaseGates(unittest.TestCase):
    def repo(self, version="1.0.0", branch="main", changelog=None):
        d = tempfile.mkdtemp(prefix="at-gate-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        os.makedirs(os.path.join(d, "agent_terminal"))
        open(os.path.join(d, "pyproject.toml"), "w").write(f'[project]\nname="x"\nversion="{version}"\n')
        open(os.path.join(d, "agent_terminal", "__init__.py"), "w").write(f'__version__ = "{version}"\n')
        open(os.path.join(d, "CHANGELOG.md"), "w").write(
            changelog if changelog is not None else f"# Changelog\n\n## [{version}] — x\n\n- something\n")
        sh(d, "git", "init", "-q", "-b", branch)
        sh(d, "git", "add", "-A")
        sh(d, *GIT, "commit", "-qm", "init")
        return d

    def test_clean_main_with_coherent_versions_passes(self):
        self.assertEqual(rc.run_gates(self.repo()), "1.0.0")

    def test_wrong_branch_and_dirty_tree_are_refused(self):
        with self.assertRaisesRegex(rc.GateError, "only from 'main'"):
            rc.run_gates(self.repo(branch="feature"))
        d = self.repo()
        open(os.path.join(d, "stray.txt"), "w").write("x")
        with self.assertRaisesRegex(rc.GateError, "not clean"):
            rc.run_gates(d)

    def test_version_drift_and_missing_notes_are_refused(self):
        d = self.repo()
        open(os.path.join(d, "agent_terminal", "__init__.py"), "w").write('__version__ = "1.0.1"\n')
        sh(d, *GIT, "commit", "-qam", "drift")
        with self.assertRaisesRegex(rc.GateError, "mismatch"):
            rc.run_gates(d)
        with self.assertRaisesRegex(rc.GateError, "no notes|no '## \\[X"):
            rc.run_gates(self.repo(changelog="# Changelog\n\n## [1.0.0]\n\n## [0.9.0]\n- old\n"))

    def test_a_released_version_is_immutable_and_must_increase(self):
        d = self.repo()
        sh(d, *GIT, "tag", "-a", "v1.0.0", "-m", "r")
        with self.assertRaisesRegex(rc.GateError, "already exists"):
            rc.run_gates(d)
        # a lower version than the latest tag is refused too
        sh(d, *GIT, "tag", "-a", "v2.0.0", "-m", "r")
        sh(d, "git", "tag", "-d", "v1.0.0")
        with self.assertRaisesRegex(rc.GateError, "not newer"):
            rc.run_gates(d)

    def test_notes_extraction(self):
        d = self.repo()
        self.assertEqual(rc.changelog_section(d, "1.0.0"), "- something")


@unittest.skipUnless(HAVE_KEYGEN, "needs ssh-keygen")
class Signatures(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-sig-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.key = os.path.join(self.d, "k")
        sh(self.d, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.key)
        self.file = os.path.join(self.d, "payload")
        open(self.file, "wb").write(b"release bytes")
        self.signers = os.path.join(self.d, "allowed_signers")
        open(self.signers, "w").write(signing.allowed_signers_line(open(self.key + ".pub").read()) + "\n")

    def test_valid_signature_verifies_and_tampering_or_other_keys_do_not(self):
        sig = signing.sign(self.file, self.key)
        signing.verify(self.file, sig, self.signers)
        open(self.file, "wb").write(b"tampered")
        with self.assertRaises(signing.SignatureError):
            signing.verify(self.file, sig, self.signers)
        open(self.file, "wb").write(b"release bytes")
        other = os.path.join(self.d, "other")
        sh(self.d, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other)
        bad_sig = signing.sign(self.file, other)
        with self.assertRaises(signing.SignatureError):
            signing.verify(self.file, bad_sig, self.signers)

    def test_missing_signature_is_refused(self):
        with self.assertRaisesRegex(signing.SignatureError, "missing signature"):
            signing.verify(self.file, self.file + ".sig", self.signers)


def make_release(root, version="9.9.9", files=None, schema=1):
    """A release directory shaped like scripts/build-release.sh output (fake wheel inside)."""
    d = os.path.join(root, f"rel-{version}")
    os.makedirs(d)
    name = f"crewhall-{version}-installer.tar.gz"
    stage = os.path.join(root, f"stage-{version}")
    os.makedirs(os.path.join(stage, "inner"))
    open(os.path.join(stage, "inner", f"agent_terminal-{version}-py3-none-any.whl"), "wb").write(b"wheel")
    with tarfile.open(os.path.join(d, name), "w:gz") as tar:
        tar.add(os.path.join(stage, "inner"), arcname="inner")
    sha = hashlib.sha256(open(os.path.join(d, name), "rb").read()).hexdigest()
    json.dump({"version": version, "tarball": name, "sha256": sha, "commit": "abc1234",
               "min_python": "3.11", "state_schema": schema}, open(os.path.join(d, "release.json"), "w"))
    return d


class UpdaterVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="at-upd-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.tmp, "cfg"),
                                         "XDG_STATE_HOME": os.path.join(self.tmp, "state")})
        p.start()
        self.addCleanup(p.stop)

    def test_versions(self):
        self.assertLess(updater.parse_version("0.9.0"), updater.parse_version("0.21.0"))
        self.assertEqual(updater.parse_version("v1.2.3"), (1, 2, 3))
        with self.assertRaises(UpdateError):
            updater.parse_version("banana")

    def test_integrity_check_detects_corruption_and_missing_files(self):
        rel = make_release(self.tmp)
        self.assertIn("unsigned", updater.verify_release(rel)["_signature"])
        tarball = os.path.join(rel, json.load(open(os.path.join(rel, "release.json")))["tarball"])
        with open(tarball, "ab") as fh:
            fh.write(b"x")
        with self.assertRaisesRegex(UpdateError, "sha256 mismatch"):
            updater.verify_release(rel)
        os.unlink(tarball)
        with self.assertRaisesRegex(UpdateError, "tarball missing"):
            updater.verify_release(rel)
        with self.assertRaisesRegex(UpdateError, "not a release directory"):
            updater.fetch(os.path.join(self.tmp, "nope"), self.tmp)

    @unittest.skipUnless(HAVE_KEYGEN, "needs ssh-keygen")
    def test_once_a_signer_is_trusted_unsigned_releases_are_refused(self):
        rel = make_release(self.tmp)
        key = os.path.join(self.tmp, "key")
        sh(self.tmp, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key)
        cfg = os.path.join(self.tmp, "cfg", "crewhall")
        os.makedirs(cfg)
        open(os.path.join(cfg, "allowed_signers"), "w").write(
            signing.allowed_signers_line(open(key + ".pub").read()) + "\n")
        with self.assertRaisesRegex(UpdateError, "missing signature"):
            updater.verify_release(rel)
        tarball = os.path.join(rel, json.load(open(os.path.join(rel, "release.json")))["tarball"])
        signing.sign(tarball, key)
        self.assertEqual(updater.verify_release(rel)["_signature"], "verified")

    def test_only_managed_installs_can_be_updated(self):
        with mock.patch.object(updater, "install_root", return_value=None):
            with self.assertRaisesRegex(UpdateError, "not a managed installation"):
                updater.apply(make_release(self.tmp))
            with self.assertRaisesRegex(UpdateError, "not a managed installation"):
                updater.rollback()
        # the real interpreter here is not a managed venv either (dev checkout / test run)
        self.assertIsNone(updater.install_root())


class UpdaterApply(unittest.TestCase):
    """apply()/rollback() over a fake managed prefix; building the venv is stubbed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="at-upd2-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.tmp, "cfg"),
                                           "XDG_STATE_HOME": os.path.join(self.tmp, "state")})
        env.start()
        self.addCleanup(env.stop)
        self.prefix = os.path.join(self.tmp, "prefix")
        os.makedirs(os.path.join(self.prefix, "venv"))
        open(os.path.join(self.prefix, "venv", "MARK"), "w").write("old")
        json.dump({"version": __version__, "state_schema": 1}, open(os.path.join(self.prefix, "install.json"), "w"))
        for target, kw in (("install_root", {"return_value": self.prefix}),
                           ("backup_before_update", {"return_value": None}),
                           ("daemon_version", {"return_value": None}),
                           ("_running_agents", {"return_value": []})):  # never look at the live daemon
            p = mock.patch.object(updater, target, **kw)
            p.start()
            self.addCleanup(p.stop)

        def fake_build(prefix, wheel, version):
            new = os.path.join(prefix, "venv.new")
            os.makedirs(new, exist_ok=True)
            open(os.path.join(new, "MARK"), "w").write("new")
            return new

        p = mock.patch.object(updater, "_build_env", side_effect=fake_build)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(updater, "_post_swap_check")  # the fake venvs here have no interpreter
        p.start()
        self.addCleanup(p.stop)

    def mark(self, name="venv"):
        return open(os.path.join(self.prefix, name, "MARK")).read()

    def test_same_version_and_downgrades_are_refused_and_nothing_changes(self):
        self.assertEqual(updater.apply(make_release(self.tmp, __version__))["status"], "up-to-date")
        with self.assertRaisesRegex(UpdateError, "older than the installed"):
            updater.apply(make_release(self.tmp, "0.0.1"))
        self.assertEqual(self.mark(), "old")

    def test_update_swaps_keeps_previous_and_rollback_toggles_back(self):
        out = updater.apply(make_release(self.tmp, "99.0.0"))
        self.assertEqual((out["status"], out["to"], out["restarted"]), ("updated", "99.0.0", False))
        self.assertEqual((self.mark(), self.mark("venv.prev")), ("new", "old"))
        rec = json.load(open(os.path.join(self.prefix, "install.json")))
        self.assertEqual((rec["version"], rec["previous_version"]), ("99.0.0", __version__))
        self.assertEqual(updater.rollback()["status"], "rolled-back")
        self.assertEqual((self.mark(), self.mark("venv.prev")), ("old", "new"))
        rec = json.load(open(os.path.join(self.prefix, "install.json")))
        self.assertEqual(rec["version"], __version__)

    def test_a_failing_smoke_test_leaves_the_running_install_untouched(self):
        with mock.patch.object(updater, "_build_env", side_effect=UpdateError("smoke test failed")):
            with self.assertRaisesRegex(UpdateError, "smoke test"):
                updater.apply(make_release(self.tmp, "99.0.0"))
        self.assertEqual(self.mark(), "old")
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "venv.prev")))

    def test_failed_restart_rolls_back_automatically(self):
        calls = []

        def restart(prefix, version, *, force, log):
            calls.append(version)
            if version == "99.0.0":
                raise UpdateError("doctor reported problems")

        with mock.patch.object(updater, "restart_daemon", side_effect=restart):
            with self.assertRaisesRegex(UpdateError, "rolled back"):
                updater.apply(make_release(self.tmp, "99.0.0"), restart=True)
        self.assertEqual(self.mark(), "old")                       # back on the old code
        self.assertEqual(calls, ["99.0.0", __version__])           # and the daemon was restarted on it

    def test_rollback_without_previous_and_with_newer_state_schema(self):
        with self.assertRaisesRegex(UpdateError, "no previous version"):
            updater.rollback()
        updater.apply(make_release(self.tmp, "99.0.0", schema=2))
        state = os.path.join(self.tmp, "state", "crewhall")
        os.makedirs(state)
        json.dump({"schema": 2}, open(os.path.join(state, "state.json"), "w"))
        with self.assertRaisesRegex(UpdateError, "newer schema"):
            updater.rollback()
        self.assertEqual(updater.rollback(force=True)["status"], "rolled-back")

    def test_swapped_environment_that_fails_its_check_is_rolled_back(self):
        # Regression: a renamed venv kept the staging path in its scripts' shebangs and was broken.
        with mock.patch.object(updater, "_post_swap_check", side_effect=UpdateError("bad interpreter")):
            with self.assertRaisesRegex(UpdateError, "rolled back"):
                updater.apply(make_release(self.tmp, "99.0.0"))
        self.assertEqual(self.mark(), "old")
        rec = json.load(open(os.path.join(self.prefix, "install.json")))
        self.assertEqual(rec["version"], __version__)

    def test_shebangs_of_the_staged_venv_are_repaired_after_the_rename(self):
        venv = os.path.join(self.tmp, "v")
        os.makedirs(os.path.join(venv, "bin"))
        script = os.path.join(venv, "bin", "crewhall")
        open(script, "w").write("#!/opt/prefix/venv.new/bin/python\nimport sys\nprint('/venv.new/ stays in body')\n")
        os.symlink("python3", os.path.join(venv, "bin", "python"))
        open(os.path.join(venv, "bin", "data.bin"), "wb").write(b"\x00\x01 /venv.new/ binary")
        updater.fix_shebangs(venv, "venv.new")
        text = open(script).read()
        self.assertTrue(text.startswith("#!/opt/prefix/venv/bin/python\n"))
        self.assertIn("/venv.new/ stays in body", text)               # only the shebang line changes
        self.assertTrue(os.path.islink(os.path.join(venv, "bin", "python")))
        self.assertIn(b"/venv.new/", open(os.path.join(venv, "bin", "data.bin"), "rb").read())

    def test_concurrent_updates_are_refused(self):
        with updater._Lock(self.prefix):
            with self.assertRaisesRegex(UpdateError, "in progress"):
                updater.apply(make_release(self.tmp, "99.0.0"))

    def test_restart_refuses_while_agents_run_unless_forced(self):
        with mock.patch.object(updater, "_running_agents", return_value=["orq", "eje"]):
            with self.assertRaisesRegex(UpdateError, "would close them"):
                updater.restart_daemon(self.prefix, "1.0.0", force=False, log=lambda m: None)

    def test_a_refused_restart_changes_nothing_and_never_restarts_the_daemon(self):
        # Regression (found in the rehearsal): the refusal was treated as a failure -> rollback -> forced
        # restart, which closed the very agents it was protecting.
        with mock.patch.object(updater, "_running_agents", return_value=["keepalive"]), \
                mock.patch.object(updater, "restart_daemon") as restart, \
                mock.patch.object(updater, "_build_env") as build:
            with self.assertRaises(updater.RestartRefused):
                updater.apply(make_release(self.tmp, "99.0.0"), restart=True)
            with self.assertRaises(updater.RestartRefused):
                updater.rollback(restart=True)
        build.assert_not_called()                 # refused before doing any work
        restart.assert_not_called()
        self.assertEqual(self.mark(), "old")
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "venv.prev")))

    def test_restart_only_applies_a_pending_update_and_respects_agents(self):
        with mock.patch.object(updater, "daemon_version", return_value="0.0.1"), \
                mock.patch.object(updater, "check_restart_allowed"), \
                mock.patch.object(updater, "restart_daemon") as restart:
            out = updater.restart_pending()
        self.assertEqual((out["status"], out["was"]), ("restarted", "0.0.1"))
        restart.assert_called_once()
        with mock.patch.object(updater, "daemon_version", return_value=__version__), \
                mock.patch.object(updater, "check_restart_allowed"), \
                mock.patch.object(updater, "restart_daemon") as restart:
            self.assertEqual(updater.restart_pending()["status"], "nothing-pending")
        restart.assert_not_called()
        with mock.patch.object(updater, "_running_agents", return_value=["a"]):
            with self.assertRaises(updater.RestartRefused):
                updater.restart_pending()

    def test_agents_appearing_between_the_check_and_the_restart_keep_the_install_without_restarting(self):
        refused = updater.RestartRefused("1 agent(s) are running")
        with mock.patch.object(updater, "check_restart_allowed"), \
                mock.patch.object(updater, "restart_daemon", side_effect=[refused]) as restart:
            out = updater.apply(make_release(self.tmp, "99.0.0"), restart=True)
        self.assertEqual(restart.call_count, 1)    # no rollback-restart afterwards
        self.assertEqual(self.mark(), "new")       # the new version stays installed
        self.assertIn("restart_refused", out)
        self.assertTrue(out["restart_pending"])


class DaemonHandover(unittest.TestCase):
    def test_wait_for_the_old_daemon_to_release_its_lock(self):
        # Regression (found in the rehearsal): the new daemon was started while the old one still held
        # the lock (socket already gone) and exited at once -> "daemon did not start".
        import fcntl
        import threading
        import time

        tmp = tempfile.mkdtemp(prefix="at-lock-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lock = os.path.join(tmp, "daemon.lock")
        holder = open(lock, "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        with mock.patch("agent_terminal.paths.lock_path", return_value=lock):
            with self.assertRaisesRegex(UpdateError, "still shutting down"):
                updater.wait_daemon_gone(timeout=0.5)
            threading.Timer(0.6, holder.close).start()
            t0 = time.monotonic()
            updater.wait_daemon_gone(timeout=10)
            self.assertGreaterEqual(time.monotonic() - t0, 0.4)  # it really waited for the release


class StateCompatibility(unittest.TestCase):
    """Every released state format must stay loadable (RELEASING.md)."""

    def test_all_fixtures_restore_cleanly(self):
        from agent_terminal import Controller
        from agent_terminal.persistence import StateStore

        fixtures = sorted(f for f in os.listdir(os.path.join(ROOT, "tests", "fixtures")) if f.startswith("state-"))
        self.assertTrue(fixtures)
        for name in fixtures:
            tmp = tempfile.mkdtemp(prefix="at-fx-")
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            shutil.copy(os.path.join(ROOT, "tests", "fixtures", name), os.path.join(tmp, "state.json"))
            c = Controller(adopt=False, persist=False)
            c._store = StateStore(os.path.join(tmp, "state.json"))
            c.restore()
            agents = {a["name"]: a for a in c.list_agents()}
            self.assertEqual(set(agents), {"orquestador", "ejecutor"}, name)
            self.assertEqual(agents["orquestador"]["args"], [], name)               # field added later: defaults
            self.assertEqual(c.team_members("frente1")[0]["name"] in agents, True, name)


class BuildInfo(unittest.TestCase):
    def test_dev_vs_release_description(self):
        from agent_terminal import buildinfo

        with mock.patch.object(buildinfo, "_FILE", "/nonexistent"):
            self.assertEqual(buildinfo.build_info()["kind"], "dev")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        f = os.path.join(tmp, "b.json")
        json.dump({"commit": "abc1234", "built": "x"}, open(f, "w"))
        with mock.patch.object(buildinfo, "_FILE", f):
            self.assertEqual(buildinfo.describe(), f"{__version__} (release abc1234)")


if __name__ == "__main__":
    unittest.main()
