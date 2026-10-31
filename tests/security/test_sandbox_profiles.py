"""Stage 2: every role profile, executed inside a real bubblewrap sandbox.

Each case runs the same probe as a sandboxed process and checks both halves:
what the role must be able to do (read/write its trees, reach the daemon
socket) and what it must never do (``~/.ssh``, ``~/deploy``, crewhall's config
and state, writes outside its rw set, creating ``Projects/.ok-prod``).

The profiles mirror the snippet documented in
``REPORTE_sandbox_spawn_stage2.md`` for ``$HOME=/home/abraham-olivares``.
Bubblewrap cannot glob: test directories are bound one by one.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from crewhall import paths, sandbox, settings
from crewhall.types import SessionSpec

PY = "/usr/bin/python3"

# The test home must live outside ``/tmp``: the sandbox mounts a private tmpfs
# there (and these profiles bind no workspace), so a fake home under /tmp
# would be invisible. It is still a throwaway directory.
TEST_ROOT = os.path.join(os.path.expanduser("~"), ".cache", "crewhall-sandbox-tests")

PROBE = r'''
import json, os, socket, subprocess
out = os.environ["PROBE_OUT"]
res = {}
def read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")[:40]
    except OSError as exc:
        return "ERR:%s" % exc.errno
def write(path, text="ok"):
    try:
        with open(path, "w") as fh:
            fh.write(text)
        return "ok"
    except OSError as exc:
        return "ERR:%s" % exc.errno
home = os.environ["HOME"]
p = os.environ["PROBE_PROJECTS"]
res["home"] = home
res["read_claude"] = read(os.path.join(home, ".claude", "settings.json"))
res["read_ssh"] = read(os.path.join(home, ".ssh", "id_ed25519"))
res["read_deploy"] = read(os.path.join(home, "deploy", "secret"))
res["read_secrets"] = read(os.path.join(home, ".secrets", "token"))
res["read_config"] = read(os.path.join(home, ".config", "crewhall", "control-token"))
res["read_state"] = read(os.path.join(home, ".local", "state", "crewhall", "state.json"))
res["read_heraldo_bin"] = read(os.path.join(home, ".local", "bin", "heraldo-status"))
res["read_heraldo_notify"] = read(os.path.join(home, ".local", "bin", "heraldo-notify"))
try:
    r = subprocess.run([os.path.join(home, ".local", "bin", "heraldo-notify")],
                       capture_output=True, text=True, timeout=5)
    res["exec_heraldo_notify"] = "ok:%s" % r.stdout.strip()[:40]
except OSError as exc:
    res["exec_heraldo_notify"] = "ERR:%s" % exc.errno
res["daemon_socket"] = "no"
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(os.environ["CREWHALL_SOCKET"])
    s.sendall(b'{"op":"ping"}\n')
    s.recv(64)
    res["daemon_socket"] = "connected"
    s.close()
except OSError as exc:
    res["daemon_socket"] = "ERR:%s" % exc
res["write_allowed"] = write(os.environ["PROBE_ALLOWED"])
res["write_forbidden"] = write(os.environ["PROBE_FORBIDDEN"])
res["create_ok_prod"] = write(os.path.join(p, ".ok-prod"), "1")
res["tmux_env"] = os.environ.get("TMUX")
res["tmux_dir"] = os.path.exists("/tmp/tmux-%d" % os.getuid())
with open(out, "w") as fh:
    json.dump(res, fh)
'''

RECONNECT_PROBE = r'''
import os, socket, time
sock = os.environ["PROBE_SOCKET"]
out = os.environ["PROBE_OUT"]
tamper = os.environ["PROBE_TAMPER"]
try:
    os.unlink(sock)
    with open(tamper, "w") as fh:
        fh.write("UNLINKED")
except OSError as exc:
    with open(tamper, "w") as fh:
        fh.write("ERR:%s" % exc.errno)
deadline = time.time() + 10
while time.time() < deadline:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(sock)
        s.sendall(b'{"op":"ping"}\n')
        data = s.recv(64)
        s.close()
    except OSError:
        time.sleep(0.05)
        continue
    with open(out, "a") as fh:
        fh.write(data.decode("utf-8", "replace").strip() + "\n")
        fh.flush()
    time.sleep(0.2)
'''


def _binds(home: str, *entries: tuple[str, str]) -> list[dict]:
    return [{"path": os.path.join(home, p) if not p.startswith("/") else p, "mode": m}
            for p, m in entries]


def common_binds(home: str) -> list[dict]:
    """What every sandboxed agent may see (part 2, ``common``)."""
    binds = _binds(
        home,
        (".local/share/mise", "ro"),
        (".local/bin/uv", "ro"),
        (".claude/agents", "ro"),
        (".claude/hooks", "ro"),
        (".claude/skills", "ro"),
        (".claude/settings.json", "ro"),
        (".claude/projects", "rw"),
        (".config/opencode", "ro"),
        ("Projects/indexion-project", "ro"),
        ("Projects/indexion-frontend", "ro"),
        ("Projects/indexion-reflex", "ro"),
        ("Projects/session", "rw"),
    )
    # Directories that may not exist on every machine: an optional bind is
    # skipped instead of failing the launch.
    optional = _binds(home, (".claude/agents", "ro"), (".claude/hooks", "ro"),
                      (".claude/skills", "ro"), (".claude/settings.json", "ro"),
                      (".config/opencode", "ro"), (".local/share/mise", "ro"),
                      (".local/bin/uv", "ro"))
    for bind in optional:
        bind["optional"] = True
    return binds


COMMON_DENY = ["$HOME/deploy", "$HOME/.local/bin/heraldo-*",
               "$HOME/Projects/.ok-prod", "$HOME/Projects/.manos-libres"]


def role_profiles(home: str) -> dict[str, dict]:
    """The dev machine's per-role profiles (data, not code)."""
    common = common_binds(home)
    deny = [p.replace("$HOME", home) for p in COMMON_DENY]
    base = {"env": {"HOME": home, "PATH": "/usr/local/bin:/usr/bin:/bin"},
            "network": "host", "deny": deny, "workspace": "none"}

    def profile(binds, **over):
        return {**base, "binds": [*common, *binds], **over}

    backend_extra = _binds(home,
        ("Projects/indexion-project", "rw"),
        ("Projects/indexion-frontend", "ro"),
        ("Projects/indexion-reflex", "ro"),
        ("Projects/session", "rw"),
        # Postgres variant (b), the default: the unix socket directory.
        # Optional so a machine without Postgres still launches the agent.
        ("/var/run/postgresql", "ro"),
    )
    backend_extra[-1]["optional"] = True
    # Heraldo's own CLI must stay visible *only* to the heraldo profile.
    heraldo_deny = [p for p in deny if not p.endswith("/heraldo-*")]
    return {
        "heraldo": profile([], deny=heraldo_deny),
        "backend": profile(backend_extra),
        "frontend": profile(_binds(home,
            ("Projects/indexion-frontend", "rw"),
            ("Projects/indexion-project", "ro"),
            ("Projects/indexion-reflex", "ro"),
            (".npm", "rw"),
        )),
        "sonda": profile(_binds(home,
            # bwrap cannot glob: only these test trees are writable.
            ("Projects/indexion-project/tests", "rw"),
            ("Projects/indexion-frontend/__tests__", "rw"),
            ("Projects/session", "rw"),
        )),
        "redactor": profile(_binds(home,
            ("Projects/SESSION.md", "rw"),
            ("Projects/ROADMAP.md", "rw"),
            ("Projects/CLAUDE.md", "rw"),
            ("Projects/session", "rw"),
        )),
        "auditor": profile(_binds(home, ("Projects/session", "rw"))),
    }


def sandbox_section(home: str) -> dict:
    profiles = role_profiles(home)
    return {
        "profiles": profiles,
        "agents": [
            {"name_pattern": "Heraldo", "profile": "heraldo"},
            {"name_pattern": "Ejecutor_Backend_Sonnet_[0-9]+", "profile": "backend"},
            {"name_pattern": "Ejecutor_DeepSeekV4Flash_[0-9]+", "profile": "backend"},
            {"name_pattern": "Ejecutor_Frontend_Sonnet_[0-9]+", "profile": "frontend"},
            {"name_pattern": "Ejecutor_Sonda_Sonnet_[0-9]+", "profile": "sonda"},
            {"name_pattern": "Ejecutor_Redactor_Sonnet_[0-9]+", "profile": "redactor"},
            {"name_pattern": "Auditor", "profile": "auditor"},
            {"name_pattern": "Investigador_.*", "profile": "auditor"},
        ],
    }


class RoleSandbox(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(TEST_ROOT, exist_ok=True)
        self.home = tempfile.mkdtemp(prefix="role-home-", dir=TEST_ROOT)
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
        files = {
            ".claude/settings.json": "{}",
            ".claude/agents/x.md": "agent",
            ".claude/hooks/x.sh": "hook",
            ".claude/skills/x/SKILL.md": "skill",
            ".claude/projects/keep": "history",
            ".ssh/id_ed25519": "SSH-SECRET",
            "deploy/secret": "DEPLOY-SECRET",
            ".secrets/token": "TOKEN",
            ".local/bin/heraldo-status": "HERALDO",
            ".local/bin/heraldo-notify": "#!/bin/sh\necho HERALDO-NOTIFY-OK\n",
            ".local/bin/uv": "uv",
            ".local/share/mise/installs/x": "mise",
            ".config/opencode/opencode.json": "{}",
            ".config/crewhall/settings.json": "{}",
            ".config/crewhall/control-token": "CONTROL-SECRET",
            ".local/state/crewhall/state.json": '{"secret":"STATE"}',
            "Projects/indexion-project/app.py": "print(1)",
            "Projects/indexion-project/tests/test_app.py": "def test(): pass",
            "Projects/indexion-frontend/src/app.ts": "export {}",
            "Projects/indexion-frontend/__tests__/app.test.ts": "test",
            "Projects/indexion-frontend/node_modules/x/index.js": "x",
            "Projects/indexion-reflex/app.py": "print(2)",
            "Projects/session/notes.md": "notes",
            "Projects/SESSION.md": "session",
            "Projects/ROADMAP.md": "roadmap",
            "Projects/CLAUDE.md": "claude",
            ".npm/cache/x": "npm",
        }
        for name, body in files.items():
            path = os.path.join(self.home, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        os.chmod(os.path.join(self.home, ".local/bin/heraldo-notify"), 0o755)
        os.makedirs(os.path.join(self.home, "Projects/indexion-project/.venv"))
        settings._CACHE.update(stamp=None, data=None)
        self.addCleanup(settings._CACHE.update, stamp=None, data=None)
        self.write_settings({"sandbox": sandbox_section(self.home)})
        self.probe = os.path.join(self.home, "probe.py")
        with open(self.probe, "w", encoding="utf-8") as fh:
            fh.write(PROBE)
        self.out = os.path.join(self.home, "Projects", "session", "probe.json")
        # A daemon socket the sandbox must still reach; the restart test
        # replaces it with a fresh inode, like a real daemon restart.
        self._servers: list[tuple[threading.Event, socket.socket]] = []
        self.start_server()
        self.addCleanup(self._cleanup)

    def start_server(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(paths.socket_path())
        server.listen(4)
        server.settimeout(0.2)
        stop = threading.Event()
        self._servers.append((stop, server))

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except (TimeoutError, OSError):
                    continue
                with conn:
                    conn.recv(64)
                    conn.sendall(b'{"ok":true}\n')

        threading.Thread(target=serve, daemon=True).start()

    def stop_server(self) -> None:
        stop, server = self._servers.pop()
        stop.set()
        with __import__("contextlib").suppress(OSError):
            server.close()
        with __import__("contextlib").suppress(OSError):
            os.unlink(paths.socket_path())

    def restart_server(self) -> None:
        self.stop_server()
        self.start_server()

    def _cleanup(self) -> None:
        while self._servers:
            self.stop_server()

    def write_settings(self, data: dict) -> None:
        os.makedirs(settings.config_dir(), mode=0o700, exist_ok=True)
        with open(settings.path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        settings._CACHE.update(stamp=None, data=None)

    def run_probe(self, name: str, allowed: str, forbidden: str,
                  section: dict | None = None) -> dict:
        if section is not None:
            self.write_settings({"sandbox": section})
        if os.path.exists(self.out):
            os.unlink(self.out)
        spec = SessionSpec(
            command=[PY, self.probe],
            cwd=os.path.join(self.home, "Projects"),
            env={"PROBE_OUT": self.out,
                 "PROBE_PROJECTS": os.path.join(self.home, "Projects"),
                 "PROBE_ALLOWED": allowed, "PROBE_FORBIDDEN": forbidden,
                 "CREWHALL_SOCKET": paths.socket_path()},
            name=name,
            sandboxed=True,
            sandbox_agent_id="sess_test",
            sandbox_profile=None,
        )
        argv = sandbox.command_for_spec(spec)
        proc = subprocess.run(argv, capture_output=True, text=True, check=False,
                              env={**os.environ, **spec.env})
        self.assertTrue(os.path.exists(self.out),
                        f"probe did not run: rc={proc.returncode} {proc.stderr[-800:]}")
        with open(self.out, encoding="utf-8") as fh:
            return json.load(fh)

    def check_common(self, res: dict, heraldo_hidden: bool = True) -> None:
        self.assertEqual(res["home"], self.home)
        self.assertEqual(res["read_claude"], "{}")
        for key in ("read_ssh", "read_deploy", "read_secrets", "read_config", "read_state"):
            self.assertTrue(res[key].startswith("ERR:"), f"{key}={res[key]}")
        if heraldo_hidden:
            self.assertTrue(res["read_heraldo_bin"].startswith("ERR:"),
                            f"heraldo binary visible: {res['read_heraldo_bin']}")
            self.assertTrue(res["read_heraldo_notify"].startswith("ERR:"),
                            f"heraldo-notify visible: {res['read_heraldo_notify']}")
            self.assertTrue(res["exec_heraldo_notify"].startswith("ERR:"),
                            f"heraldo-notify executable: {res['exec_heraldo_notify']}")
        else:
            self.assertEqual(res["read_heraldo_bin"], "HERALDO")
            self.assertIn("HERALDO-NOTIFY-OK", res["read_heraldo_notify"])
            self.assertEqual(res["exec_heraldo_notify"], "ok:HERALDO-NOTIFY-OK")
        self.assertEqual(res["daemon_socket"], "connected")
        self.assertTrue(res["create_ok_prod"].startswith("ERR:"),
                        "Projects/.ok-prod is creatable")
        self.assertIsNone(res["tmux_env"])
        self.assertFalse(res["tmux_dir"])

    def check_role(self, res: dict, heraldo_hidden: bool = True) -> None:
        self.check_common(res, heraldo_hidden=heraldo_hidden)
        self.assertEqual(res["write_allowed"], "ok", res["write_allowed"])
        self.assertTrue(res["write_forbidden"].startswith("ERR:"),
                        f"wrote outside its rw set: {res['write_forbidden']}")

    def test_heraldo_sees_its_own_cli(self) -> None:
        res = self.run_probe(
            "Heraldo",
            os.path.join(self.home, "Projects/session/heraldo.md"),
            os.path.join(self.home, "Projects/indexion-project/new.py"))
        self.check_role(res, heraldo_hidden=False)

    def test_backend(self) -> None:
        res = self.run_probe(
            "Ejecutor_Backend_Sonnet_1",
            os.path.join(self.home, "Projects/indexion-project/new.py"),
            os.path.join(self.home, "Projects/indexion-frontend/new.ts"))
        self.check_role(res)
        self.assertEqual(res["read_state"], "ERR:2")

    def test_frontend(self) -> None:
        res = self.run_probe(
            "Ejecutor_Frontend_Sonnet_1",
            os.path.join(self.home, "Projects/indexion-frontend/src/new.ts"),
            os.path.join(self.home, "Projects/indexion-project/new.py"))
        self.check_role(res)

    def test_sonda(self) -> None:
        res = self.run_probe(
            "Ejecutor_Sonda_Sonnet_1",
            os.path.join(self.home, "Projects/indexion-project/tests/test_new.py"),
            os.path.join(self.home, "Projects/indexion-project/app.py"))
        self.check_role(res)

    def test_redactor(self) -> None:
        res = self.run_probe(
            "Ejecutor_Redactor_Sonnet_1",
            os.path.join(self.home, "Projects/SESSION.md"),
            os.path.join(self.home, "Projects/OTHER.md"))
        self.check_role(res)

    def test_auditor(self) -> None:
        res = self.run_probe(
            "Auditor",
            os.path.join(self.home, "Projects/session/report.md"),
            os.path.join(self.home, "Projects/indexion-project/app.py"))
        self.check_role(res)

    def test_socket_reconnects_after_a_daemon_restart(self) -> None:
        probe = os.path.join(self.home, "reconnect.py")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(RECONNECT_PROBE)
        out = os.path.join(self.home, "Projects", "session", "reconnect.log")
        tamper = os.path.join(self.home, "Projects", "session", "tamper.txt")
        spec = SessionSpec(
            command=[PY, probe],
            cwd=os.path.join(self.home, "Projects"),
            env={"PROBE_SOCKET": paths.socket_path(), "PROBE_OUT": out,
                 "PROBE_TAMPER": tamper},
            name="Heraldo", sandboxed=True,
            sandbox_agent_id="sess_reconnect", sandbox_profile=None,
        )
        proc = subprocess.Popen(
            sandbox.command_for_spec(spec), env={**os.environ, **spec.env},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertTrue(self._wait_lines(out, 1),
                        "the sandboxed client never connected")
        with open(tamper, encoding="utf-8") as fh:
            # EROFS, not merely EBUSY on a mountpoint: the dir is read-only.
            self.assertEqual(fh.read().strip(), f"ERR:{errno.EROFS}",
                             "the agent could replace the daemon socket")
        # Same directory, new inode: what `daemon restart` does.
        self.restart_server()
        self.assertTrue(self._wait_lines(out, 2),
                        "the sandboxed client did not reconnect after the restart")

    def _wait_lines(self, path: str, count: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with open(path, encoding="utf-8") as fh:
                    if len(fh.read().splitlines()) >= count:
                        return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    # -- mutation: remove one rule and the named check fails -----------------
    def test_mutation_without_backend_rw_bind_cannot_write_the_repo(self) -> None:
        section = sandbox_section(self.home)
        section["profiles"]["backend"]["binds"] = [
            b for b in section["profiles"]["backend"]["binds"]
            if not b["path"].endswith("indexion-project") or b["mode"] != "rw"]
        res = self.run_probe(
            "Ejecutor_Backend_Sonnet_1",
            os.path.join(self.home, "Projects/indexion-project/new.py"),
            os.path.join(self.home, "Projects/indexion-frontend/new.ts"),
            section=section)
        self.assertTrue(res["write_allowed"].startswith("ERR:"),
                        "removing the rw bind did not break the allowed write")

    def test_mutation_without_deploy_deny_exposes_deploy(self) -> None:
        section = sandbox_section(self.home)
        for profile in section["profiles"].values():
            profile["deny"] = [d for d in profile["deny"]
                               if not d.endswith("/deploy")]
        res = self.run_probe(
            "Auditor",
            os.path.join(self.home, "Projects/session/report.md"),
            os.path.join(self.home, "Projects/indexion-project/app.py"),
            section=section)
        self.assertEqual(res["read_deploy"], "DEPLOY-SECRET",
                         "removing the deploy deny did not expose it")


if __name__ == "__main__":
    unittest.main()
