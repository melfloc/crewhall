from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock

from crewhall import bundle, doctor

ROOT = os.path.join(os.path.dirname(__file__), "..")


class Doctor(unittest.TestCase):
    def test_results_have_the_documented_shape(self):
        results = doctor.run_checks()
        self.assertTrue(results)
        for r in results:
            self.assertEqual(set(r), {"name", "status", "detail", "hint"})
            self.assertIn(r["status"], ("ok", "warn", "fail"))
        names = {r["name"] for r in results}
        self.assertTrue({"python", "tmux", "state dir", "runtime dir"} <= names)

    def test_exit_code_only_fails_on_fail(self):
        warn = [{"name": "x", "status": "warn", "detail": "", "hint": ""}]
        fail = warn + [{"name": "y", "status": "fail", "detail": "", "hint": ""}]
        self.assertEqual(doctor.exit_code(warn), 0)
        self.assertEqual(doctor.exit_code(fail), 1)

    def test_missing_tmux_is_a_failure_with_a_hint(self):
        with mock.patch("shutil.which", return_value=None):
            r = doctor.check_tmux()
        self.assertEqual(r["status"], "fail")
        self.assertIn("tmux", r["hint"])

    def test_without_any_agent_cli_is_a_failure_but_one_is_enough(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIn("fail", [r["status"] for r in doctor.check_agent_clis()])
        only_claude = lambda n: "/x/claude" if n == "claude" else None  # noqa: E731
        with mock.patch("shutil.which", side_effect=only_claude), \
                mock.patch.object(doctor, "_version_of", return_value="v"):
            statuses = [r["status"] for r in doctor.check_agent_clis()]
        self.assertNotIn("fail", statuses)
        self.assertIn("warn", statuses)  # the missing one is only a warning

    def test_relative_tmpdir_is_flagged(self):
        with mock.patch.dict(os.environ, {"TMPDIR": "relative-dir"}):
            self.assertEqual(doctor.check_tmpdir()["status"], "warn")

    def test_report_text_and_cli_json(self):
        text = doctor.format_report(doctor.run_checks())
        self.assertIn("environment check", text)
        out = subprocess.run(["python3", "-m", "crewhall", "doctor", "--json"],
                             capture_output=True, text=True, cwd=ROOT, timeout=60,
                             env={**os.environ, "PYTHONPATH": ROOT})
        self.assertIn(out.returncode, (0, 1))
        self.assertTrue(json.loads(out.stdout))


class BundleRoundtrip(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="at-b-src-")
        self.dst = tempfile.mkdtemp(prefix="at-b-dst-")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.dst, ignore_errors=True)
        self.archive = os.path.join(self.src, "b.tar.gz")

    def home(self, root):
        return mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(root, "cfg"),
                                            "XDG_STATE_HOME": os.path.join(root, "state")})

    def seed(self):
        cfg = os.path.join(self.src, "cfg", "crewhall")
        os.makedirs(os.path.join(cfg, "teams"))
        open(os.path.join(cfg, "profiles.toml"), "w").write("[profile.p]\nkind='claude'\n")
        open(os.path.join(cfg, "teams", "t.toml"), "w").write("[team]\nname='t'\n")
        open(os.path.join(cfg, "web-token"), "w").write("SECRET")
        open(os.path.join(cfg, "teams", "notes.txt"), "w").write("ignored")
        st = os.path.join(self.src, "state", "crewhall")
        os.makedirs(st)
        open(os.path.join(st, "state.json"), "w").write("{}")

    def test_default_export_has_no_secrets_or_state_and_roundtrips(self):
        self.seed()
        with self.home(self.src):
            out = bundle.export_bundle(self.archive)
        self.assertEqual(out["files"], ["config/profiles.toml", "config/teams/t.toml"])
        with self.home(self.dst):
            res = bundle.import_bundle(self.archive)
        self.assertEqual(res["written"], ["config/profiles.toml", "config/teams/t.toml"])
        p = os.path.join(self.dst, "cfg", "crewhall", "profiles.toml")
        self.assertIn("kind='claude'", open(p).read())
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_token_and_state_only_when_asked(self):
        self.seed()
        with self.home(self.src):
            out = bundle.export_bundle(self.archive, with_state=True, with_token=True)
        self.assertIn("config/web-token", out["files"])
        self.assertIn("state/state.json", out["files"])

    def test_import_never_overwrites_without_force_and_keeps_a_backup(self):
        self.seed()
        with self.home(self.src):
            bundle.export_bundle(self.archive)
        target = os.path.join(self.dst, "cfg", "crewhall")
        os.makedirs(target)
        open(os.path.join(target, "profiles.toml"), "w").write("mine")
        with self.home(self.dst):
            res = bundle.import_bundle(self.archive)
            self.assertTrue(any("exists" in s for s in res["skipped"]))
            self.assertEqual(open(os.path.join(target, "profiles.toml")).read(), "mine")
            res = bundle.import_bundle(self.archive, force=True)
        self.assertIn("config/profiles.toml", res["backed_up"])
        self.assertEqual(open(os.path.join(target, "profiles.toml.bak")).read(), "mine")

    def test_dry_run_writes_nothing(self):
        self.seed()
        with self.home(self.src):
            bundle.export_bundle(self.archive)
        with self.home(self.dst):
            res = bundle.import_bundle(self.archive, dry_run=True)
        self.assertTrue(res["written"])
        self.assertFalse(os.path.exists(os.path.join(self.dst, "cfg")))

    def _craft(self, members, manifest_files=None):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            files = {n: d for n, d in members.items()}
            manifest = {"format": 1, "version": "x", "files": manifest_files if manifest_files is not None
                        else {n: bundle._sha(d) for n, d in files.items()}}
            for name, data in [("manifest.json", json.dumps(manifest).encode()), *files.items()]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        path = os.path.join(self.src, "crafted.tar.gz")
        open(path, "wb").write(buf.getvalue())
        return path

    def test_hostile_archives_are_rejected(self):
        for bad in ("../../etc/passwd", "/etc/passwd", "config/../../x", "config/teams/../x.toml",
                    "config/other.toml", "state/evil.json", "config/teams/sub/x.toml"):
            path = self._craft({bad: b"x"})
            with self.home(self.dst), self.assertRaises(bundle.BundleError, msg=bad):
                bundle.import_bundle(path)
        self.assertFalse(os.path.exists("/etc/passwd.bak"))

    def test_tampered_content_and_symlinks_are_rejected(self):
        data = b"[profile.p]\nkind='claude'\n"
        path = self._craft({"config/profiles.toml": b"tampered"},
                           manifest_files={"config/profiles.toml": bundle._sha(data)})
        with self.home(self.dst), self.assertRaises(bundle.BundleError):
            bundle.import_bundle(path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("config/profiles.toml")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
        link = os.path.join(self.src, "link.tar.gz")
        open(link, "wb").write(buf.getvalue())
        with self.home(self.dst), self.assertRaises(bundle.BundleError):
            bundle.import_bundle(link)

    def test_garbage_file_is_a_clean_error(self):
        junk = os.path.join(self.src, "junk")
        open(junk, "w").write("not a tarball")
        with self.assertRaises(bundle.BundleError):
            bundle.read_bundle(junk)


class Packaging(unittest.TestCase):
    def test_pyproject_ships_the_web_ui_and_python_floor_matches_code(self):
        cfg = tomllib.load(open(os.path.join(ROOT, "pyproject.toml"), "rb"))
        data = cfg["tool"]["setuptools"]["package-data"]["crewhall.web"]
        self.assertIn("static/*", data)
        self.assertIn("static/js/*", data)
        # Vendored assets live one level under vendor/ and must all ship.
        self.assertIn("static/vendor/*/*", data)
        self.assertEqual(cfg["project"]["requires-python"], ">=3.11")  # tomllib
        static = os.path.join(ROOT, "crewhall", "web", "static")
        for rel in ("index.html", "app.css", "js/core.js", "js/main.js",
                    "vendor/xterm/xterm.js", "vendor/qrcode/qrcode.js"):
            self.assertTrue(os.path.exists(os.path.join(static, rel)), rel)

    def test_shell_scripts_parse_and_show_help(self):
        for name in ("install.sh", "uninstall.sh", "build-release.sh", "release.sh", "deploy.sh"):
            path = os.path.join(ROOT, "scripts", name)
            self.assertEqual(subprocess.run(["bash", "-n", path]).returncode, 0, name)
            self.assertTrue(os.access(path, os.X_OK), f"{name} is not executable")
        out = subprocess.run(["bash", os.path.join(ROOT, "scripts", "install.sh"), "--help"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn("--service", out.stdout)
        bad = subprocess.run(["bash", os.path.join(ROOT, "scripts", "install.sh"), "--nope"],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)

    def test_deploy_refuses_to_ship_anything_that_is_not_a_signed_tagged_release(self):
        script = os.path.join(ROOT, "scripts", "deploy.sh")
        tmp = tempfile.mkdtemp(prefix="at-dep-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        out = subprocess.run(["bash", script, "somehost", "--version", "99.9.9", "--dry-run"],
                             capture_output=True, text=True, cwd=ROOT, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 1)
        self.assertIn("not a tagged release", out.stderr)  # nothing is deployed from the working tree
        self.assertEqual(subprocess.run(["bash", script, "h", "--bogus"], capture_output=True,
                                        stdin=subprocess.DEVNULL).returncode, 2)

    def test_the_command_is_a_path_based_launcher_not_a_symlink_into_the_venv(self):
        # Regression: `update` swaps the venv dir and console scripts embed their creation path.
        text = open(os.path.join(ROOT, "scripts", "install.sh")).read()
        self.assertIn("managed by crewhall install.sh", text)
        self.assertIn('-P -m crewhall "$@"', text)           # -P: never import from the cwd
        self.assertNotIn("ln -sfn", text)

    def test_the_launcher_style_never_imports_crewhall_from_the_cwd(self):
        # Regression (found in the rehearsal): `python -m crewhall` runs whatever package of that name
        # sits in the current directory (e.g. a dev checkout) instead of the installed release.
        import sys

        shadow = tempfile.mkdtemp(prefix="at-shadow-")
        self.addCleanup(shutil.rmtree, shadow, ignore_errors=True)
        os.makedirs(os.path.join(shadow, "crewhall"))
        open(os.path.join(shadow, "crewhall", "__init__.py"), "w").write("__version__='SHADOW'\n")
        open(os.path.join(shadow, "crewhall", "__main__.py"), "w").write("print('SHADOW')\n")
        env = {**os.environ, "PYTHONPATH": ROOT}
        unsafe = subprocess.run([sys.executable, "-m", "crewhall", "--version"], cwd=shadow,
                                capture_output=True, text=True, env=env)
        safe = subprocess.run([sys.executable, "-P", "-m", "crewhall", "--version"], cwd=shadow,
                              capture_output=True, text=True, env=env)
        self.assertIn("SHADOW", unsafe.stdout)             # documents the hazard
        self.assertIn("crewhall 0.", safe.stdout)    # and that -P avoids it

    def test_release_refuses_to_run_off_master_or_with_a_dirty_tree(self):
        # Gate logic is covered in test_release_mechanism; here: the script is wired to it.
        text = open(os.path.join(ROOT, "scripts", "release.sh")).read()
        self.assertIn("release_check.py gates", text)
        self.assertIn("git archive HEAD", text)          # built from the commit, not the working tree
        self.assertIn("git tag -a", text)

    def test_example_files_load_with_the_real_parsers(self):
        from crewhall.specs import load_profiles, load_team_spec

        profiles = load_profiles(os.path.join(ROOT, "examples", "profiles.toml"))
        spec = load_team_spec(os.path.join(ROOT, "examples", "team.toml"), profiles)
        self.assertEqual(spec["agents"][1]["kind"], "claude")


if __name__ == "__main__":
    unittest.main()


class LiveTeams(unittest.TestCase):
    TEAMS = [{"name": "Frente 1", "workspace": "/tmp", "agents": [
        {"name": "orq", "kind": "claude", "backend": "tmux", "cwd": "/tmp", "args": ["--agent", "o"]},
        {"name": "ex", "kind": "opencode", "backend": "tmux", "cwd": "/nonexistent-xyz", "args": []}]},
        {"name": "vacio", "workspace": None, "agents": []}]

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-b-live-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.d, "cfg"),
                                           "XDG_STATE_HOME": os.path.join(self.d, "state")})
        env.start(); self.addCleanup(env.stop)
        self.archive = os.path.join(self.d, "b.tar.gz")

    def test_export_includes_running_teams_without_ids_or_empty_teams(self):
        out = bundle.export_bundle(self.archive, live_teams=self.TEAMS)
        self.assertEqual(out["files"], ["config/teams/frente-1.json"])
        _, files = bundle.read_bundle(self.archive)
        spec = json.loads(files["config/teams/frente-1.json"])
        self.assertEqual(spec["team"], {"name": "Frente 1", "workspace": "/tmp"})
        self.assertEqual([a["name"] for a in spec["agent"]], ["orq", "ex"])
        self.assertNotIn("agent_id", files["config/teams/frente-1.json"].decode())

    def test_existing_team_file_wins_over_the_live_definition(self):
        os.makedirs(os.path.join(self.d, "cfg", "crewhall", "teams"))
        open(os.path.join(self.d, "cfg", "crewhall", "teams", "frente-1.toml"), "w").write(
            "[team]\nname='Frente 1'\n[[agent]]\nname='x'\n")
        out = bundle.export_bundle(self.archive, live_teams=self.TEAMS)
        self.assertEqual(out["files"], ["config/teams/frente-1.toml"])

    def test_plan_and_apply_skip_agents_whose_directory_is_missing(self):
        bundle.export_bundle(self.archive, live_teams=self.TEAMS)
        specs, errors = bundle.bundle_team_specs(bundle.read_bundle(self.archive)[1])
        self.assertEqual(errors, [])
        plan = bundle.plan_specs(specs)
        self.assertEqual([(a["name"], a["cwd_ok"]) for a in plan[0]["agents"]], [("orq", True), ("ex", False)])
        calls = []
        res = bundle.apply_specs(specs, lambda spec: calls.append(spec) or
                                 {"created": [a["name"] for a in spec["agents"]], "existing": []})
        self.assertEqual(res["created"], ["Frente 1/orq"])
        self.assertEqual([a["name"] for a in calls[0]["agents"]], ["orq"])
        self.assertEqual(res["skipped"][0]["agent"], "ex")
        self.assertIn("not found", res["skipped"][0]["reason"])

    def test_a_failing_team_is_reported_not_raised(self):
        bundle.export_bundle(self.archive, live_teams=self.TEAMS[:1])
        specs, _ = bundle.bundle_team_specs(bundle.read_bundle(self.archive)[1])
        res = bundle.apply_specs(specs, lambda spec: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(res["failed"], [{"team": "Frente 1", "error": "boom"}])

    def test_malformed_team_file_is_an_error_not_a_crash(self):
        specs, errors = bundle.bundle_team_specs({"config/teams/bad.json": b"{not json"})
        self.assertEqual(specs, [])
        self.assertEqual(len(errors), 1)


class StoredBundles(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-b-store-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(self.d, "cfg"),
                                           "XDG_STATE_HOME": os.path.join(self.d, "state")})
        env.start(); self.addCleanup(env.stop)

    def _make(self, **kw):
        os.makedirs(bundle.bundles_dir(), exist_ok=True)
        dest = os.path.join(bundle.bundles_dir(), "b1.tar.gz")
        bundle.export_bundle(dest, **kw)
        return dest

    def test_describe_marks_empty_and_invalid(self):
        dest = self._make()
        self.assertEqual((bundle.describe(dest)["valid"], bundle.describe(dest)["empty"]), (True, True))
        bad = os.path.join(bundle.bundles_dir(), "bad.tar.gz")
        open(bad, "wb").write(b"junk")
        info = bundle.describe(bad)
        self.assertFalse(info["valid"]); self.assertTrue(info["error"])
        full = self._make(live_teams=[{"name": "t", "workspace": None, "agents": [{"name": "a", "kind": "claude"}]}])
        d = bundle.describe(full)
        self.assertEqual((d["files"], d["teams"], d["empty"]), (1, 1, False))

    def test_resolve_and_delete_refuse_traversal_and_foreign_names(self):
        self._make()
        for bad in ("../x.tar.gz", "a/b.tar.gz", "x.txt", "", None, "missing.tar.gz"):
            with self.assertRaises(bundle.BundleError, msg=bad):
                bundle.resolve_name(bad)
        outside = os.path.join(self.d, "outside.tar.gz"); open(outside, "wb").write(b"x")
        os.symlink(outside, os.path.join(bundle.bundles_dir(), "link.tar.gz"))
        with self.assertRaises(bundle.BundleError):
            bundle.resolve_name("link.tar.gz")  # a symlink out of the directory is not served
        bundle.delete_bundle("b1.tar.gz")
        self.assertFalse(os.path.exists(os.path.join(bundle.bundles_dir(), "b1.tar.gz")))

    def test_store_upload_verifies_before_keeping_anything(self):
        for bad in (b"", b"not a tarball"):
            with self.assertRaises(bundle.BundleError):
                bundle.store_upload("x.tar.gz", bad)
        self.assertEqual([n for n in os.listdir(bundle.bundles_dir())], [])  # no .part left behind
        data = open(self._make(), "rb").read()
        name = bundle.store_upload("../../My Setup.tar.gz", data)
        self.assertRegex(name, r"^uploaded-\d{8}-\d{6}-my-setup\.tar\.gz$")
        self.assertEqual(oct(os.stat(os.path.join(bundle.bundles_dir(), name)).st_mode & 0o777), "0o600")
        with self.assertRaises(bundle.BundleError):
            bundle.store_upload("big.tar.gz", b"x" * (bundle.MAX_UPLOAD + 1))
