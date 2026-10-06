"""End-to-end ssh-tmux tests against an ephemeral sshd on loopback.

No real host is ever contacted: a throwaway ``sshd`` listens on a high port on
127.0.0.1 with its own host key, client key and known_hosts, and the agent is a
plain ``/bin/bash`` (never a real CLI agent), so the run spends no credits.
"""
from __future__ import annotations

import getpass
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from crewhall import settings
from crewhall.backends import ssh_tmux
from crewhall.controller import Controller
from crewhall.remote_link import HostLink
from crewhall.session import InteractiveSession
from crewhall.types import SessionSpec

HAVE = all(shutil.which(b) for b in ("ssh", "sshd", "ssh-keygen", "tmux"))
SSHD = shutil.which("sshd") or "sshd"
HOST_ALIAS = "loop"
REMOTE_SOCKET = f"at_ssh_{os.getpid()}"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _children(pid: int) -> list[int]:
    kids: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as fh:
                stat = fh.read()
            ppid = int(stat[stat.rfind(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid == pid:
            kids.append(int(entry))
    return kids


def _kill_tree(pid: int) -> None:
    for child in _children(pid):
        _kill_tree(child)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


class SshdHarness:
    """A minimal, disposable sshd (current user, key auth, loopback only)."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="ati-sshd-", dir="/tmp")
        self.port = _free_port()
        self.user = getpass.getuser()
        self.hostkey = os.path.join(self.dir, "hostkey")
        self.clientkey = os.path.join(self.dir, "clientkey")
        self.known_hosts = os.path.join(self.dir, "known_hosts")
        self.config = os.path.join(self.dir, "sshd_config")
        self.log = os.path.join(self.dir, "sshd.log")
        self.pidfile = os.path.join(self.dir, "sshd.pid")
        # Remote Claude config dir for transcript tests (never the real ~/.claude).
        self.claude_dir = os.path.join(self.dir, "claude")
        os.makedirs(os.path.join(self.claude_dir, "projects", "proj"))
        self.state_dir = os.path.join(self.dir, "state")  # remote worktree root parent
        self._proc: subprocess.Popen | None = None
        self._build()

    def _run(self, *args: str) -> None:
        subprocess.run(args, check=True, capture_output=True)

    def _build(self) -> None:
        self._run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.hostkey)
        self._run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", self.clientkey)
        os.chmod(self.clientkey, 0o600)
        authorized = os.path.join(self.dir, "authorized_keys")
        shutil.copyfile(self.clientkey + ".pub", authorized)
        os.chmod(authorized, 0o600)
        with open(self.known_hosts, "w", encoding="utf-8") as fh:
            with open(self.hostkey + ".pub", encoding="utf-8") as pub:
                fh.write(f"[127.0.0.1]:{self.port} {pub.read().strip()}\n")
        os.chmod(self.known_hosts, 0o600)
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(
                f"Port {self.port}\n"
                "ListenAddress 127.0.0.1\n"
                f"HostKey {self.hostkey}\n"
                f"PidFile {self.pidfile}\n"
                f"AuthorizedKeysFile {authorized}\n"
                "StrictModes no\n"
                "PasswordAuthentication no\n"
                "KbdInteractiveAuthentication no\n"
                "PubkeyAuthentication yes\n"
                "UsePAM no\n"
                "PermitRootLogin no\n"
                "PrintMotd no\n"
                "LogLevel ERROR\n"
                f"SetEnv CLAUDE_CONFIG_DIR={self.claude_dir} XDG_STATE_HOME={self.state_dir}\n"
            )

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [SSHD, "-f", self.config, "-E", self.log],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_port(self.port):
            raise RuntimeError("ephemeral sshd did not start")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        pid: int | None = None
        if os.path.exists(self.pidfile):
            try:
                with open(self.pidfile, encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
            except (OSError, ValueError):
                pid = None
        if pid is not None:
            # Killing the listener alone leaves established sshd-session
            # children (and their ControlMaster) alive: kill the whole tree so
            # the "host" really goes away.
            _kill_tree(pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    time.sleep(0.1)
            except OSError:
                return

    def cleanup(self) -> None:
        self.stop()
        subprocess.run(["tmux", "-L", REMOTE_SOCKET, "kill-server"],
                       capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def host_config(self) -> dict:
        return {
            "ssh": f"{self.user}@127.0.0.1",
            "port": self.port,
            "identity": self.clientkey,
            "known_hosts": self.known_hosts,
            "tmux_socket": REMOTE_SOCKET,
        }


@unittest.skipUnless(HAVE, "ssh/sshd/ssh-keygen/tmux not installed")
class SshTmuxRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sshd = SshdHarness()
        cls.sshd.start()

    @classmethod
    def tearDownClass(cls):
        cls.sshd.cleanup()

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ati-sshcfg-", dir="/tmp")
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "config"),
            "XDG_STATE_HOME": os.path.join(self.root, "state"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "run"),
        })
        env.start(); self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700, exist_ok=True)
        settings.patch({"hosts": {HOST_ALIAS: self.sshd.host_config()}})
        self.controller = Controller(adopt=False, persist=False)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _create(self, **over) -> InteractiveSession:
        spec = SessionSpec(command=["/bin/bash"], cwd="/tmp", cols=80, rows=24,
                           host=HOST_ALIAS, **over)
        session = self.controller.create(spec)
        self.addCleanup(session.close)
        return session

    def _sh(self, command: str) -> str:
        proc = ssh_tmux.SshTmuxBackend(self.sshd.host_config())._ssh_run(command)
        return proc.stdout

    def test_roundtrip_write_capture_resize_terminate(self):
        session = self._create()
        self.assertEqual(session.info().host, HOST_ALIAS)
        self.assertEqual(session.info().backend, "ssh-tmux")
        session.write("echo MARKER_$((6*7))")
        session.send_enter()
        self.assertIn("MARKER_42", session.read_until("MARKER_42", timeout=15))

        session.resize(100, 33)
        session.write("stty size")
        session.send_enter()
        self.assertIn("33 100", session.read_until("33 100", timeout=15))

        session.terminate()
        deadline = time.monotonic() + 10
        while session.status.alive and time.monotonic() < deadline:
            session.poll(); time.sleep(0.1)
        self.assertFalse(session.status.alive)

    def test_host_down_is_unreachable_not_exited_and_recovers(self):
        session = self._create()
        session.write("echo BEFORE_DOWN")
        session.send_enter()
        self.assertIn("BEFORE_DOWN", session.read_until("BEFORE_DOWN", timeout=15))

        self.sshd.stop()
        session.poll()
        self.assertTrue(session.status.alive, "a down host must not mark the agent exited")
        self.assertTrue(session.info().meta.get("host_unreachable"))
        self.assertEqual(session.info().host, HOST_ALIAS)

        self.sshd.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            session.poll()
            if not session.info().meta.get("host_unreachable"):
                break
            time.sleep(0.3)
        self.assertFalse(session.info().meta.get("host_unreachable"))
        self.assertTrue(session.status.alive)
        session.write("echo AFTER_UP")
        session.send_enter()
        self.assertIn("AFTER_UP", session.read_until("AFTER_UP", timeout=15))

    def test_secret_env_is_applied_but_never_in_argv_or_ps(self):
        secret = "hunter2_TOPSECRET"
        session = self._create(env={"TOPSECRET": secret})
        session.write('echo "APPLIED=$TOPSECRET"')
        session.send_enter()
        self.assertIn(f"APPLIED={secret}", session.read_until(f"APPLIED={secret}", timeout=15))

        remote_ps = self._sh("ps -eo args")
        self.assertNotIn(secret, remote_ps)
        local_ps = self._local_process_table()
        self.assertNotIn(secret, local_ps)

    def test_env_reaches_second_agent_on_running_tmux_server(self):
        first = self._create(env={"AGENT_TAG": "first"})
        first.write("echo T1=$AGENT_TAG")
        first.send_enter()
        first.read_until("T1=first", timeout=15)
        # The remote tmux server is now running: a new pane must still get its env.
        second = self._create(env={"AGENT_TAG": "second_TOPSECRET"})
        second.write("echo T2=$AGENT_TAG")
        second.send_enter()
        self.assertIn("T2=second_TOPSECRET", second.read_until("T2=second_TOPSECRET", timeout=15))
        self.assertNotIn("second_TOPSECRET", self._sh("ps -eo args"))

    def test_hostile_values_are_literal_not_executed(self):
        payload = "$(touch /tmp/ati-pwned-inj)"
        session = self._create(env={"PAYLOAD": payload})
        session.write('printf "VALUE=%s\\n" "$PAYLOAD"')
        session.send_enter()
        self.assertIn(f"VALUE={payload}", session.read_until(f"VALUE={payload}", timeout=15))
        time.sleep(0.3)
        self.assertFalse(os.path.exists("/tmp/ati-pwned-inj"))

    def test_host_test_reports_tools_and_actionable_failures(self):
        settings.patch({"hosts": {HOST_ALIAS: self.sshd.host_config()}})
        res = self.controller.host_test(HOST_ALIAS)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["tmux"])
        self.assertEqual(self.controller.host_status()[0]["state"], "ok")

        # A name the pinned known_hosts does not cover: the key is not trusted.
        other = {**self.sshd.host_config(), "ssh": f"{self.sshd.user}@localhost"}
        settings.patch({"hosts": {HOST_ALIAS: other}})
        res = self.controller.host_test(HOST_ALIAS)
        self.assertFalse(res["ok"], res)
        self.assertIn("Host key not trusted", res["error"])
        self.assertEqual(self.controller.host_status()[0]["state"], "unreachable")

        settings.patch({"hosts": {HOST_ALIAS: self.sshd.host_config()}})
        self.sshd.stop()
        self.addCleanup(self.sshd.start)
        res = self.controller.host_test(HOST_ALIAS)
        self.assertFalse(res["ok"], res)

    def test_remote_sessions_are_readopted(self):
        session = self._create()
        sid = session.session_id
        session.write("echo ADOPT_ME")
        session.send_enter()
        session.read_until("ADOPT_ME", timeout=15)

        second = Controller(adopt=True, persist=False)
        self.addCleanup(second._remote_adopt_stop.set)
        deadline = time.monotonic() + 15
        adopted = None
        while time.monotonic() < deadline:
            try:
                adopted = second.registry.resolve(sid)
                break
            except Exception:  # noqa: BLE001 - not adopted yet
                time.sleep(0.2)
        self.assertIsNotNone(adopted, "remote session was not readopted")
        self.assertEqual(adopted.info().host, HOST_ALIAS)
        self.assertEqual(adopted.backend.name, "ssh-tmux")

    def _local_process_table(self) -> str:
        out = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    out.append(fh.read().replace(b"\0", b" ").decode("utf-8", "replace"))
            except OSError:
                continue
        return "\n".join(out)


@unittest.skipUnless(HAVE, "ssh/sshd/ssh-keygen/tmux not installed")
class SshTunnelRealTests(unittest.TestCase):
    """Reverse tunnel + restricted gateway against an ephemeral sshd."""

    @classmethod
    def setUpClass(cls):
        cls.sshd = SshdHarness()
        cls.sshd.start()

    @classmethod
    def tearDownClass(cls):
        cls.sshd.cleanup()

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ati-sshgw-", dir="/tmp")
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "config"),
            "XDG_STATE_HOME": os.path.join(self.root, "state"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "run"),
        })
        env.start(); self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        settings.patch({"hosts": {HOST_ALIAS: {**self.sshd.host_config(), "tunnel": True}}})
        self.calls: list[dict] = []

        def dispatch(req):
            self.calls.append(req)
            return {"ok": True, "op": req["op"]}

        self.link = HostLink(settings.host(HOST_ALIAS), dispatch,
                             lambda ref: HOST_ALIAS if ref == "agent-1" else None)
        self.addCleanup(self.link.stop)
        self.backend = ssh_tmux.SshTmuxBackend(settings.host(HOST_ALIAS))

    def _remote_call(self, request: dict) -> dict:
        script = (
            "import json,socket,sys\n"
            "s=socket.socket(socket.AF_UNIX);s.settimeout(10);s.connect(sys.argv[1])\n"
            "s.sendall(sys.stdin.buffer.read());d=b''\n"
            "while b'\\n' not in d:\n"
            "    c=s.recv(65536)\n"
            "    if not c: break\n"
            "    d+=c\n"
            "print(d.decode().strip())\n"
        )
        import json, shlex
        cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(self.link.remote_socket)}"
        proc = self.backend._ssh_run(cmd, input=json.dumps(request) + "\n", timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_remote_agent_reaches_gateway_and_nothing_else(self):
        path = self.link.ensure()
        self.assertEqual(self.link.state, "connected")
        # The remote socket sits in a private directory.
        mode = self.backend._ssh_run(f"stat -c %a {os.path.dirname(path)}").stdout.strip()
        self.assertEqual(mode, "700")

        ok = self._remote_call({"op": "agent_identity", "target": "agent-1", "token": "tok"})
        self.assertTrue(ok["ok"])
        self.assertEqual(self.calls[-1]["_actor"], f"ssh:{HOST_ALIAS}")

        sent = len(self.calls)
        for bad in ({"op": "shutdown"},
                    {"op": "agent_create", "kind": "claude"},
                    {"op": "settings_set", "key": "x", "value": 1},
                    {"op": "agent_identity", "target": "local-agent", "token": "tok"}):
            self.assertFalse(self._remote_call(bad)["ok"], bad)
        self.assertEqual(len(self.calls), sent, "a refused op must never reach the daemon")

    def test_tunnel_recovers_after_the_ssh_process_dies(self):
        self.link.ensure()
        first = self.link._proc
        first.kill()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.link._proc is not first and self.link.state == "connected":
                break
            time.sleep(0.2)
        self.assertEqual(self.link.state, "connected")
        self.assertTrue(self._remote_call({"op": "ping"})["ok"])
        self.assertTrue(self._remote_call(
            {"op": "agent_identity", "target": "agent-1", "token": "tok"})["ok"])

    def test_controller_points_tunnelled_agents_at_the_gateway(self):
        controller = Controller(adopt=False, persist=False)
        self.addCleanup(controller.remote_links.stop_all)
        controller.set_gateway_dispatch(lambda req: {"ok": True})
        local = controller._build_agent_env("a1", "a1", [], None)
        self.assertNotIn("CREWHALL_GATEWAY", local)
        env = controller._build_agent_env("a1", "a1", [], None, HOST_ALIAS)
        link = controller.remote_links._links[HOST_ALIAS]
        self.assertEqual(env["CREWHALL_SOCKET"], link.remote_socket)
        self.assertNotEqual(env["CREWHALL_SOCKET"], local["CREWHALL_SOCKET"])
        self.assertEqual(env["CREWHALL_GATEWAY"], "1")
        # Never advertise a socket that cannot work: a down host aborts the launch.
        self.sshd.stop()
        self.addCleanup(self.sshd.start)
        link.stop()
        controller.remote_links._links.clear()
        with mock.patch("crewhall.remote_link.READY_TIMEOUT", 2.0):
            with self.assertRaises(ssh_tmux.HostUnreachable):
                controller.remote_links.ensure(settings.host(HOST_ALIAS))

    def test_real_hook_cli_on_the_remote_side_reaches_the_daemon_via_gateway(self):
        import shlex
        path = self.link.ensure()
        project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {"CREWHALL_AGENT_ID": "agent-1", "CREWHALL_TOKEN": "tok",
               "CREWHALL_SOCKET": path, "CREWHALL_GATEWAY": "1", "PYTHONPATH": project}
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        proc = self.backend._ssh_run(f"{prefix} python3 -P -m crewhall agent hook stop </dev/null",
                                     timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        hooks = [c for c in self.calls if c["op"] == "agent_hook"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual((hooks[0]["agent"], hooks[0]["event"], hooks[0]["token"]),
                         ("agent-1", "stop", "tok"))

    def test_tunnel_argv_ignores_user_ssh_config_and_is_strict(self):
        argv = self.link._tunnel_argv("/remote/gw.sock")
        self.assertEqual(argv[:3], ["ssh", "-F", "/dev/null"])
        joined = " ".join(argv)
        for opt in ("StrictHostKeyChecking=yes", "ForwardAgent=no", "ExitOnForwardFailure=yes",
                    "BatchMode=yes", "ControlPath=none"):
            self.assertIn(opt, joined)
        self.assertEqual(argv[argv.index("-R") + 1], f"/remote/gw.sock:{self.link.gateway_path}")
        self.assertEqual(argv[-2:], ["--", settings.host(HOST_ALIAS)["ssh"]])


@unittest.skipUnless(HAVE, "ssh/sshd/ssh-keygen/tmux not installed")
class SshTranscriptRealTests(unittest.TestCase):
    """Remote Claude transcripts (history + activity) over SSH."""

    UUID = "11111111-2222-3333-4444-555555555555"

    @classmethod
    def setUpClass(cls):
        cls.sshd = SshdHarness()
        cls.sshd.start()

    @classmethod
    def tearDownClass(cls):
        cls.sshd.cleanup()

    def setUp(self):
        from crewhall import remote_files

        remote_files._cache.clear()
        self.host = {**self.sshd.host_config(), "name": HOST_ALIAS}
        self.root = tempfile.mkdtemp(prefix="ati-sshtr-", dir="/tmp")
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": os.path.join(self.root, "run")})
        env.start(); self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.file = os.path.join(self.sshd.claude_dir, "projects", "proj", f"{self.UUID}.jsonl")
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hello remote"}},
            {"type": "assistant", "message": {"role": "assistant", "model": "claude-test-1",
             "content": [{"type": "text", "text": "hi there"},
                         {"type": "tool_use", "id": "t1", "name": "Bash",
                          "input": {"command": "sleep 9"}}]}},
        ]
        import json
        with open(self.file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(x) for x in lines) + "\n")
        self.addCleanup(lambda: os.path.exists(self.file) and os.unlink(self.file))

    def test_history_is_read_from_the_remote_host(self):
        from crewhall import transcripts

        out = transcripts.read_history(self.UUID, host=self.host)
        self.assertTrue(out["available"])
        self.assertTrue(out["remote"])
        self.assertFalse(out["truncated"])
        text = str(out["messages"])
        self.assertIn("hello remote", text)
        self.assertIn("hi there", text)

    def test_missing_invalid_and_symlinked_transcripts_are_unavailable(self):
        from crewhall import transcripts

        other = "99999999-2222-3333-4444-555555555555"
        self.assertFalse(transcripts.read_history(other, host=self.host)["available"])
        with mock.patch.object(ssh_tmux.SshTmuxBackend, "_ssh_run") as run:
            for bad in ("../../etc/passwd", "x; touch /tmp/ati-pwn", ""):
                self.assertFalse(transcripts.read_history(bad, host=self.host)["available"])
            run.assert_not_called()
        link = os.path.join(self.sshd.claude_dir, "projects", "proj", f"{other}.jsonl")
        os.symlink(self.file, link)
        self.addCleanup(os.unlink, link)
        self.assertFalse(transcripts.read_history(other, host=self.host)["available"])

    def test_activity_snapshot_never_blocks_and_fills_from_the_remote_tail(self):
        from crewhall import activity, remote_files

        t0 = time.monotonic()
        first = activity.claude_snapshot(self.UUID, host=self.host)
        self.assertLess(time.monotonic() - t0, 1.0, "the polled path must not wait on SSH")
        self.assertEqual(first, {})  # nothing observed yet: n/d, not a guess
        deadline = time.monotonic() + 15
        snap: dict = {}
        while time.monotonic() < deadline and not snap:
            time.sleep(0.2)
            snap = activity.claude_snapshot(self.UUID, host=self.host)
        self.assertEqual(snap.get("model"), "claude-test-1")
        self.assertEqual(snap["tool"]["name"], "Bash")

    def test_down_host_gives_no_data_without_hanging(self):
        from crewhall import transcripts

        self.sshd.stop()
        self.addCleanup(self.sshd.start)
        t0 = time.monotonic()
        self.assertFalse(transcripts.read_history(self.UUID, host=self.host)["available"])
        self.assertLess(time.monotonic() - t0, 20)


def _git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True)


@unittest.skipUnless(HAVE and shutil.which("git"), "ssh/sshd/tmux/git not installed")
class SshWorktreeRealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sshd = SshdHarness()
        cls.sshd.start()

    @classmethod
    def tearDownClass(cls):
        cls.sshd.cleanup()

    def setUp(self):
        self.host = {**self.sshd.host_config(), "name": HOST_ALIAS}
        self.root = tempfile.mkdtemp(prefix="ati-sshwt-", dir="/tmp")
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": os.path.join(self.root, "run")})
        env.start(); self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.sshd.state_dir, ignore_errors=True)
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:
            fh.write("a\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def test_lifecycle_dirty_unmerged_and_confined_removal(self):
        from crewhall import remote_worktrees as rw

        self.assertTrue(rw.is_git_repo(self.host, self.repo))
        self.assertFalse(rw.is_git_repo(self.host, self.root))
        path = rw.create(self.host, self.repo, "team;x", "agent$(id)")
        self.assertTrue(path.startswith(os.path.join(self.sshd.state_dir, "crewhall", "worktrees")))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(rw.status(self.host, path, repo=self.repo)["branch"], "at/team-x/agent-id")
        self.assertEqual(rw.can_remove(self.host, path, repo=self.repo), (True, "clean"))

        with open(os.path.join(path, "new.txt"), "w") as fh:
            fh.write("x")
        self.assertFalse(rw.can_remove(self.host, path, repo=self.repo)[0])  # dirty
        _git(path, "add", "-A")
        _git(path, "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-q", "-m", "w")
        self.assertIn("not merged", rw.can_remove(self.host, path, repo=self.repo)[1])

        with self.assertRaises(rw.WorktreeError):
            rw.create(self.host, self.repo, "team;x", "agent$(id)")  # already exists
        for outside in ("/tmp", self.repo, os.path.join(path, "..", "..", "..", "..")):
            with self.assertRaises(rw.WorktreeError):
                rw.remove(self.host, outside, force=True)
        self.assertTrue(os.path.isdir(self.repo))
        rw.remove(self.host, path, force=True)
        self.assertFalse(os.path.exists(path))

    def test_hostile_repo_path_is_literal_and_unknown_state_is_conservative(self):
        from crewhall import remote_worktrees as rw

        evil = "/tmp/x $(touch /tmp/ati-wt-pwn) ; `touch /tmp/ati-wt-pwn2`"
        self.assertFalse(rw.is_git_repo(self.host, evil))
        self.assertFalse(os.path.exists("/tmp/ati-wt-pwn"))
        self.assertFalse(os.path.exists("/tmp/ati-wt-pwn2"))
        path = rw.create(self.host, self.repo, "t", "a")
        self.sshd.stop()
        self.addCleanup(self.sshd.start)
        st = rw.status(self.host, path, repo=self.repo)
        self.assertTrue(st["dirty"] and st["unmerged"] and st["unknown"])
        ok, why = rw.can_remove(self.host, path, repo=self.repo)
        self.assertFalse(ok)
        self.assertIn("unknown", why)

    def test_controller_creates_lists_and_cleans_remote_worktrees(self):
        settings.patch({"hosts": {HOST_ALIAS: self.sshd.host_config()}})
        c = Controller(adopt=False, persist=False)
        path = c._make_worktree("a1", "agent", "team", self.repo, HOST_ALIAS)
        self.assertNotEqual(path, self.repo)
        self.assertTrue(os.path.isdir(path))
        listing = [w for w in c.list_worktrees() if w["agent_id"] == "a1"]
        self.assertEqual(listing[0]["host"], HOST_ALIAS)
        self.assertFalse(listing[0]["dirty"])
        c._cleanup_worktree("a1")
        self.assertFalse(os.path.exists(path))
        # not a repo -> shared workspace with a visible warning, never silent
        same = c._make_worktree("a2", "agent2", "team", self.root, HOST_ALIAS)
        self.assertEqual(same, self.root)
        self.assertIn("not a git repository", c._worktree_warnings["a2"])


if __name__ == "__main__":
    unittest.main()
