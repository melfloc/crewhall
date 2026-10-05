from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CliIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runtime = tempfile.mkdtemp(prefix="at-cli-")
        cls.env = dict(os.environ)
        cls.env["XDG_RUNTIME_DIR"] = cls.runtime
        cls.env["PYTHONPATH"] = ROOT + os.pathsep + cls.env.get("PYTHONPATH", "")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.run_cli("daemon", "stop", check=False)
        shutil.rmtree(cls.runtime, ignore_errors=True)

    @classmethod
    def run_cli(cls, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_terminal", *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=cls.env,
            timeout=30,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                f"cli {' '.join(args)} failed: {proc.stderr}\n{proc.stdout}"
            )
        return proc

    def _flow(self, backend: str) -> None:
        self.run_cli("create", "-b", backend, "-n", f"w-{backend}", "bash", "-i")
        self.run_cli("send", f"w-{backend}", "echo CLI_$((2*3))", "--enter")
        out = self.run_cli(
            "read-until", f"w-{backend}", "CLI_6", "--timeout", "8"
        ).stdout
        self.assertIn("CLI_6", out)

        listing = json.loads(self.run_cli("list", "--json").stdout)
        mine = [s for s in listing if s["name"] == f"w-{backend}"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["backend"], backend)
        self.assertEqual(mine[0]["status"], "running")

        self.run_cli("kill", f"w-{backend}")

    def test_cli_pty(self) -> None:
        self._flow("pty")

    @unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
    def test_cli_tmux(self) -> None:
        self._flow("tmux")

    def test_cli_list_empty(self) -> None:
        out = self.run_cli("list", "--json").stdout
        self.assertIsInstance(json.loads(out), list)


if __name__ == "__main__":
    unittest.main()
