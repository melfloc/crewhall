"""Stage 1 security boundary: reproducers from REVISION_SEGURIDAD_agent_spawn_r2.

These are black-box tests against a real ``Controller`` + private tmux socket +
throwaway XDG dirs. They do not need the spawn-policy branch (that code is not on
``main``); they exercise the daemon-side launch path that both branches share.

On ``main`` (before the sandbox/freeze changes) the tests must FAIL:
  * the agent can read the control token, the token store and another process's
    environ, and can reach the docker socket;
  * a plain file write changes providers.* for the next launch (R2b);
  * an agent can use the inherited $TMUX to type into another pane (N1).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from crewhall import paths, settings
from crewhall.controller import Controller

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

PROBE = r'''
import json, os, socket, stat, subprocess, sys
out = os.environ["PROBE_OUT"]
state = os.environ["PROBE_STATE"]
res = {}
def read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace")[:64]
    except OSError as exc:
        return "ERR:%s" % exc.errno
res["control_token"] = read(os.path.join(state, "control-token"))
res["agent_tokens"] = read(os.path.join(state, "agent-tokens.json"))
res["state_json"] = read(os.path.join(state, "state.json"))
res["audit"] = read(os.path.join(state, "audit.jsonl"))
res["state_listing"] = sorted(os.listdir(state))[:20]
res["proc_other"] = read("/proc/%s/environ" % os.environ["PROBE_HOST_PID"])
res["proc_entries"] = sorted(os.listdir("/proc"))[:20]
res["id"] = subprocess.run(["id"], capture_output=True, text=True, check=False).stdout.strip()
res["groups"] = os.getgroups()
try:
    res["docker_sock"] = "socket" if stat.S_ISSOCK(os.stat(os.environ["PROBE_DOCKER"]).st_mode) else "not-socket"
except OSError as exc:
    res["docker_sock"] = "ERR:%s" % exc.errno
res["tmux_dir"] = os.path.exists("/tmp/tmux-%d" % os.getuid())
res["tmux_env"] = os.environ.get("TMUX")
res["tmux_pane_env"] = os.environ.get("TMUX_PANE")
res["docker_host_env"] = os.environ.get("DOCKER_HOST")
res["ssh_auth_env"] = os.environ.get("SSH_AUTH_SOCK")
res["dbus_env"] = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
res["tmp_entries"] = sorted(os.listdir("/tmp"))[:20]
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(os.environ["PROBE_SOCKET"])
    s.sendall(b'{"op":"ping"}\n')
    res["daemon_socket"] = "connected"
    s.close()
except OSError as exc:
    res["daemon_socket"] = "ERR:%s" % exc
try:
    with open(os.path.join(os.environ["PROBE_WORKSPACE"], "probe-write"), "w") as fh:
        fh.write("ok")
    res["workspace_write"] = "ok"
except OSError as exc:
    res["workspace_write"] = "ERR:%s" % exc
res["agent_token_present"] = bool(os.environ.get("CREWHALL_TOKEN"))
with open(out, "w") as fh:
    json.dump(res, fh)
'''


class BoundaryBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="at-boundary-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = os.path.join(self.tmp, "config")
        self.state = os.path.join(self.tmp, "state")
        self.runtime = os.path.join(self.tmp, "runtime")
        self.home = os.path.join(self.tmp, "home")
        self.workspace = os.path.join(self.tmp, "workspace")
        for path in (self.config, self.state, self.runtime, self.home, self.workspace):
            os.makedirs(path, mode=0o700, exist_ok=True)
        self.sockname = f"sec{os.getpid()}{uuid.uuid4().hex[:6]}"
        self.env = {
            "XDG_CONFIG_HOME": self.config,
            "XDG_STATE_HOME": self.state,
            "XDG_RUNTIME_DIR": self.runtime,
            "HOME": self.home,
            "CREWHALL_TMUX_SOCKET": self.sockname,
        }
        self._env = mock.patch.dict(os.environ, self.env)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.makedirs(settings.config_dir(), mode=0o700, exist_ok=True)
        reset = getattr(settings, "reset_provider_snapshot", None)
        if reset:
            reset()
        settings._CACHE.update(stamp=None, data=None)
        self.state_dir = paths.state_dir()
        # A listening daemon socket the agent must still be able to reach.
        self.sock_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock_server.bind(paths.socket_path())
        self.sock_server.listen(8)
        self.sock_stop = threading.Event()

        def serve() -> None:
            self.sock_server.settimeout(0.2)
            while not self.sock_stop.is_set():
                try:
                    conn, _ = self.sock_server.accept()
                except (TimeoutError, OSError):
                    continue
                with conn:
                    try:
                        conn.recv(4096)
                        conn.sendall(b'{"ok":true}\n')
                    except OSError:
                        pass

        threading.Thread(target=serve, daemon=True).start()
        self.controllers: list[Controller] = []
        self.addCleanup(self._cleanup)
        # Secrets an agent must not reach.
        with open(os.path.join(self.state_dir, "control-token"), "w") as fh:
            fh.write("CONTROL-SECRET")
        with open(os.path.join(self.state_dir, "agent-tokens.json"), "w") as fh:
            fh.write('{"tokens":{"victim":"AGENT-SECRET"}}')
        with open(os.path.join(self.state_dir, "state.json"), "w") as fh:
            fh.write('{"secret":"STATE-SECRET"}')

    def _cleanup(self) -> None:
        for controller in self.controllers:
            try:
                controller.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self.sock_stop.set()
        try:
            self.sock_server.close()
        except OSError:
            pass
        subprocess.run(["tmux", "-L", self.sockname, "-f", "/dev/null", "kill-server"],
                       capture_output=True, check=False)
        reset = getattr(settings, "reset_provider_snapshot", None)
        if reset:
            reset()
        settings._CACHE.update(stamp=None, data=None)

    def write_settings(self, command: str, default_args: str = "") -> None:
        data = {"providers": {"claude": {"command": command, "default_args": default_args}}}
        with open(settings.path(), "w") as fh:
            json.dump(data, fh)

    def stub(self, name: str, body: str) -> str:
        path = os.path.join(self.workspace, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\ncase \"$1\" in --version) echo '2.1.300 (Claude Code)'; exit 0;; esac\n")
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def controller(self) -> Controller:
        controller = Controller(adopt=False, persist=True)
        self.controllers.append(controller)
        return controller

    def wait_for(self, path: str, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(path):
                return True
            time.sleep(0.05)
        return False


class OsBoundary(BoundaryBase):
    def _probe_agent(self) -> dict:
        probe = os.path.join(self.workspace, "probe.py")
        with open(probe, "w") as fh:
            fh.write(PROBE)
        out = os.path.join(self.workspace, "probe.json")
        stub = self.stub("claude-probe", f'exec /usr/bin/python3 {probe}\n')
        self.write_settings(stub)
        controller = self.controller()
        controller.create_agent(
            "claude", name="probe", cwd=self.workspace, backend="tmux",
            env={
                "PROBE_OUT": out,
                "PROBE_STATE": self.state_dir,
                "PROBE_HOST_PID": str(os.getpid()),
                "PROBE_DOCKER": "/var/run/docker.sock",
                "PROBE_SOCKET": paths.socket_path(),
                "PROBE_WORKSPACE": self.workspace,
            },
        )
        self.assertTrue(self.wait_for(out), "the agent never ran the probe")
        with open(out) as fh:
            return json.load(fh)

    def test_sandbox_hides_the_control_plane(self) -> None:
        result = self._probe_agent()
        print("\n[BOUNDARY]", json.dumps(result, sort_keys=True))
        self.assertTrue(result["agent_token_present"])
        self.assertTrue(result["control_token"].startswith("ERR:"), "control-token readable")
        self.assertTrue(result["agent_tokens"].startswith("ERR:"), "agent-tokens.json readable")
        self.assertTrue(result["state_json"].startswith("ERR:"), "state.json readable")
        self.assertTrue(result["proc_other"].startswith("ERR:"), "another process's environ readable")
        self.assertNotIn("docker", result["id"])
        self.assertNotIn("wheel", result["id"])
        self.assertFalse({967, 998} & set(result["groups"]))
        self.assertIn(result["docker_sock"], ("not-socket", "ERR:2"))
        self.assertFalse(result["tmux_dir"], "tmux socket dir visible")
        for key in ("tmux_env", "tmux_pane_env", "docker_host_env", "ssh_auth_env", "dbus_env"):
            self.assertIsNone(result[key], key)
        self.assertEqual(result["daemon_socket"], "connected")
        self.assertEqual(result["workspace_write"], "ok")

    def test_an_exited_agent_leaves_no_dead_pane(self) -> None:
        stub = self.stub("claude-exit", "exit 7\n")
        self.write_settings(stub)
        controller = self.controller()
        harness = controller.create_agent(
            "claude", name="exit", cwd=self.workspace, backend="tmux")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and harness.session.status.alive:
            time.sleep(0.05)
        time.sleep(0.5)
        proc = subprocess.run(
            ["tmux", "-L", self.sockname, "-f", "/dev/null",
             "has-session", "-t", harness.agent_id],
            capture_output=True, text=True, check=False)
        print(f"\n[N3-pane] status={harness.session.status} has-session rc={proc.returncode}")
        self.assertFalse(harness.session.status.alive)
        self.assertNotEqual(proc.returncode, 0, "the exited agent left a dead pane")

    def test_provider_settings_are_frozen_after_start(self) -> None:
        marker_a = os.path.join(self.workspace, "a.marker")
        marker_b = os.path.join(self.workspace, "b.marker")
        stub_a = self.stub("claude-a", f'echo A >> {marker_a}\n')
        stub_b = self.stub("claude-b", f'echo B >> {marker_b}\n')
        self.write_settings(stub_a)
        # Same read-once point as daemon.main(): before the Controller exists.
        freeze = getattr(settings, "freeze_providers", None)
        if freeze:
            freeze()
        controller = self.controller()
        # R2b: an agent (or anything same-uid) rewrites settings.json after start.
        self.write_settings(stub_b, "--evil-flag")
        argv = controller._launch_command("claude", [])
        print(f"\n[R2b] launch after settings rewrite: {argv[:3]}")
        self.assertEqual(argv[0], stub_a, "providers.command was live-reloaded")
        self.assertNotIn("--evil-flag", argv)
        # A real create after the rewrite must run the frozen binary, not B.
        controller.create_agent("claude", name="frozen", cwd=self.workspace, backend="tmux")
        self.assertTrue(self.wait_for(marker_a), "the frozen binary was not executed")
        time.sleep(0.5)
        self.assertFalse(os.path.exists(marker_b), "the rewritten provider binary was executed")

    def test_agent_cannot_type_into_another_agents_pane(self) -> None:
        # A victim pane on the same private tmux server, outside the agent.
        subprocess.run(["tmux", "-L", self.sockname, "-f", "/dev/null",
                        "new-session", "-d", "-s", "victim", "-x", "100", "-y", "30",
                        "sh", "-c", "cat"],
                       check=True, capture_output=True)
        subprocess.run(["tmux", "-L", self.sockname, "-f", "/dev/null",
                        "send-keys", "-t", "victim", "-l", "--", "ORIGINAL"],
                       check=True, capture_output=True)
        out = os.path.join(self.workspace, "n1.json")
        attack = (
            "import json,os,subprocess\n"
            "r=subprocess.run(['tmux','send-keys','-t','victim','-l','--','HACKED'],"
            "capture_output=True,text=True)\n"
            "r2=subprocess.run(['tmux','send-keys','-t','victim','Enter'],"
            "capture_output=True,text=True)\n"
            "json.dump({'rc':r.returncode,'rc2':r2.returncode,'err':(r.stderr+r2.stderr)[:200],"
            "'tmux':os.environ.get('TMUX')},open(os.environ['N1_OUT'],'w'))\n"
        )
        attack_py = os.path.join(self.workspace, "attack.py")
        with open(attack_py, "w") as fh:
            fh.write(attack)
        stub = self.stub("claude-attack", f'exec /usr/bin/python3 {attack_py}\n')
        self.write_settings(stub)
        controller = self.controller()
        controller.create_agent(
            "claude", name="attacker", cwd=self.workspace, backend="tmux",
            env={"N1_OUT": out},
        )
        self.assertTrue(self.wait_for(out), "the attacker never ran")
        with open(out) as fh:
            result = json.load(fh)
        captured = subprocess.run(
            ["tmux", "-L", self.sockname, "-f", "/dev/null", "capture-pane", "-p", "-t", "victim"],
            capture_output=True, text=True, check=True).stdout
        print(f"\n[N1] attack={result} victim_has_HACKED={'HACKED' in captured}")
        self.assertNotIn("HACKED", captured, "agent typed into another pane")
        self.assertNotEqual(result["rc"], 0)


class GuardRails(BoundaryBase):
    def test_claude_settings_deny_the_shell_escapes(self) -> None:
        from crewhall import hooks

        path = hooks.ensure_claude_settings()
        with open(path) as fh:
            data = json.load(fh)
        deny = data["permissions"]["deny"]
        for rule in ("Bash(tmux:*)", "Bash(screen:*)", "Bash(gdb:*)", "Bash(strace:*)"):
            self.assertIn(rule, deny)
        self.assertTrue(any(settings.config_dir() in rule for rule in deny))

    def test_opencode_config_carries_the_same_denies(self) -> None:
        controller = self.controller()
        env: dict[str, str] = {"OPENCODE_CLI_CONFIG_CONTENT": '{"theme":"x","permission":{"bash":{"git *":"allow"}}}'}
        controller._apply_opencode_guard(env)
        data = json.loads(env["OPENCODE_CLI_CONFIG_CONTENT"])
        self.assertEqual(data["theme"], "x")
        self.assertEqual(data["permission"]["bash"]["git *"], "allow")
        for pattern in ("tmux *", "screen *", "gdb *", "strace *"):
            self.assertEqual(data["permission"]["bash"][pattern], "deny")

    def test_root_workspace_is_refused(self) -> None:
        from crewhall import sandbox

        stub = self.stub("claude-root", "exit 0\n")
        self.write_settings(stub)
        controller = self.controller()
        with self.assertRaises(sandbox.SandboxError):
            controller.create_agent("claude", name="root-ws", cwd="/", backend="tmux")


if __name__ == "__main__":
    unittest.main(verbosity=2)
