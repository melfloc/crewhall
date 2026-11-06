from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from crewhall import paths
from crewhall.client import ensure_daemon
from crewhall.controller import Controller
from crewhall.daemon import Server


def _raw_call(request: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(paths.socket_path())
        sock.settimeout(10)
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
    return json.loads(bytes(buf).split(b"\n", 1)[0].decode("utf-8"))


class GuardOriginUnit(unittest.TestCase):
    """The dispatcher's control-plane gate, without a real socket."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-ctrl-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.server = Server(Controller(adopt=False, persist=False),
                             os.path.join(self.root, "r", "d.sock"))

    def test_control_op_without_token_is_refused(self) -> None:
        denied = self.server._guard_origin(
            {"op": "interaction_respond", "id": "x"}, None)
        self.assertIsNotNone(denied)
        self.assertIn("control token", denied["error"])

    def test_control_op_with_token_is_allowed(self) -> None:
        token = self.server._control_token
        self.assertTrue(token)
        self.assertIsNone(self.server._guard_origin(
            {"op": "agent_send", "target": "x", "_control_token": token}, None))
        # A wrong token is refused.
        self.assertIsNotNone(self.server._guard_origin(
            {"op": "agent_send", "target": "x", "_control_token": "nope"}, None))

    def test_agent_ops_need_no_control_token(self) -> None:
        for op in ("ping", "agent_identity", "team_send", "request_create",
                   "request_reply", "agent_hook"):
            self.assertIsNone(
                self.server._guard_origin({"op": op}, None), op)
        # request_cancel is an agent op only when it names the caller.
        self.assertIsNone(self.server._guard_origin(
            {"op": "request_cancel", "agent": "a", "request_id": "r"}, None))
        self.assertIsNotNone(self.server._guard_origin(
            {"op": "request_cancel", "request_id": "r"}, None))

    def test_caller_descending_from_agent_is_locked_out(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        # Pretend the test process is an agent session root: the child descends
        # from it, so it must be locked out of the control plane.
        self.server.controller.agent_root_pids = lambda: {os.getpid()}
        self.assertTrue(self.server._caller_is_agent(proc.pid))
        self.assertFalse(self.server._caller_is_agent(1))
        token = self.server._control_token
        denied = self.server._guard_origin(
            {"op": "agent_send", "target": "x", "_control_token": token}, proc.pid)
        self.assertIsNotNone(denied)
        self.assertIn("not available to agents", denied["error"])

    def test_spawn_authorization_requires_a_stable_peer_pidfd(self) -> None:
        self.server.controller.agent_root_pids_by_id = lambda: {"a": {os.getpid()}}
        self.assertFalse(self.server._peer_bound_to_agent(os.getpid(), "a"))
        pidfd_open = getattr(os, "pidfd_open", None)
        if pidfd_open is None:
            self.skipTest("pidfd_open is unavailable")
        pidfd = pidfd_open(os.getpid())
        try:
            self.assertTrue(self.server._peer_bound_to_agent(os.getpid(), "a", pidfd))
        finally:
            os.close(pidfd)

    def test_peer_pidfd_is_available_on_accepted_unix_socket(self) -> None:
        from crewhall.daemon import _peer_pidfd, _peer_pid, _pidfd_pid

        path = os.path.join(self.root, "pidfd.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        client.connect(path)
        peer, _ = server.accept()
        self.addCleanup(peer.close)
        pidfd = _peer_pidfd(peer)
        if pidfd is None:
            self.skipTest("kernel does not support SO_PEERPIDFD")
        try:
            self.assertEqual(_peer_pid(peer), _pidfd_pid(pidfd))
        finally:
            os.close(pidfd)


    def _spawn_with_env(self, env: dict) -> subprocess.Popen:
        """A child whose /proc environ is its own: wait until it has exec'd.

        Between fork and exec, /proc/<pid>/environ still shows the parent's
        environment, so reading it right after Popen is racy.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('ready', flush=True); import time; time.sleep(30)"],
            env=env, stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        self.addCleanup(proc.stdout.close)
        return proc

    def test_caller_carrying_an_agent_token_in_its_env_is_locked_out(self) -> None:
        # A double-forked child is reparented away from the agent, but it still
        # inherits the agent's CREWHALL_TOKEN.
        self.server.controller.agent_root_pids = lambda: set()
        self.server.controller.agent_tokens = lambda: ["tok-agent-1"]
        proc = self._spawn_with_env({**os.environ, "CREWHALL_TOKEN": "tok-agent-1"})
        self.assertTrue(self.server._caller_is_agent(proc.pid))
        other = self._spawn_with_env({**os.environ, "CREWHALL_TOKEN": "someone-else"})
        self.assertFalse(self.server._caller_is_agent(other.pid))

    def test_agent_may_co_edit_only_its_own_chat(self) -> None:
        calls = []

        def member(sender, token, conversation_id):
            calls.append((sender, token, conversation_id))
            return sender == "lead" and token == "t" and conversation_id == "c-1"

        self.server.controller.agent_in_conversation = member
        # The agent's own chat: allowed without the control token, even from an
        # agent process tree.
        self.server.controller.agent_root_pids = lambda: {os.getpid()}
        ok = {"op": "office_collab_insert", "sender": "lead", "token": "t",
              "id": "c-1", "path": "a.docx", "text": "x"}
        self.assertIsNone(self.server._guard_origin(ok, os.getpid()))
        # Another chat, or a bad token: refused.
        self.assertIsNotNone(self.server._guard_origin({**ok, "id": "c-2"}, os.getpid()))
        self.assertIsNotNone(self.server._guard_origin({**ok, "token": "bad"}, os.getpid()))
        # The cross-conversation listing stays control plane only.
        self.assertIsNotNone(self.server._guard_origin(
            {"op": "office_collab_list", "sender": "lead", "token": "t"}, os.getpid()))

    def test_non_string_op_is_left_to_the_dispatcher(self) -> None:
        self.assertIsNone(self.server._guard_origin({"op": 7}, None))

    def test_spawn_ops_require_an_exact_bound_agent_identity(self) -> None:
        ctrl = self.server.controller
        caller = mock.Mock(agent_id="sess_a")
        ctrl.agents = mock.Mock()
        ctrl.agents.resolve = lambda name: caller if name in ("sess_a", "A") else (
            (_ for _ in ()).throw(KeyError(name)))
        ctrl._check_token = lambda agent_id, token: None if token == "t" else (
            (_ for _ in ()).throw(ValueError("bad token")))
        bound = {"value": False}
        self.server._peer_bound_to_agent = lambda pid, agent_id, pidfd=None: bound["value"]
        for op in ("agent_create", "agent_stop"):
            base = {"op": op, "sender": "sess_a", "token": "t"}
            for bad in ({**base, "token": None}, {**base, "sender": ""},
                        {**base, "sender": 5}, {**base, "sender": "ghost"},
                        {**base, "sender": "A"}, {**base, "token": "wrong"}):
                denied = self.server._guard_origin(bad, os.getpid())
                self.assertIsNotNone(denied, bad)
                self.assertIn("valid agent identity", denied["error"])
            bound["value"] = False
            denied = self.server._guard_origin(base, os.getpid())
            self.assertIn("did not come from that agent's process", denied["error"])
            bound["value"] = True
            self.assertIsNone(self.server._guard_origin(base, os.getpid()))

    def test_spawn_ops_without_identity_are_control_plane_only(self) -> None:
        token = self.server._control_token
        self.server._caller_is_agent = lambda pid, pidfd=None: True
        denied = self.server._guard_origin(
            {"op": "agent_create", "_control_token": token}, os.getpid())
        self.assertIn("not available to agents", denied["error"])
        self.server._caller_is_agent = lambda pid, pidfd=None: False
        self.assertIsNone(self.server._guard_origin(
            {"op": "agent_create", "_control_token": token}, None))
        self.assertIn("control token", self.server._guard_origin(
            {"op": "agent_stop"}, None)["error"])


class ControlPlaneOverSocket(unittest.TestCase):
    """End-to-end: a live daemon refuses/token-gates control ops."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="ati-cp-", dir="/tmp")
        cls._env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(cls.root, "c"),
            "XDG_STATE_HOME": os.path.join(cls.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(cls.root, "r"),
        })
        cls._env.start()
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        ensure_daemon()

    @classmethod
    def tearDownClass(cls) -> None:
        from crewhall.client import Client

        try:
            Client(autostart=False).call("shutdown")
        except Exception:  # noqa: BLE001
            pass
        cls._env.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_ping_is_open(self) -> None:
        self.assertTrue(_raw_call({"op": "ping"})["ok"])

    def test_control_op_without_token_rejected(self) -> None:
        res = _raw_call({"op": "settings_get"})
        self.assertFalse(res["ok"])
        self.assertIn("control token", res["error"])

    def test_control_op_with_token_accepted(self) -> None:
        token = paths.load_control_token()
        self.assertTrue(token)
        res = _raw_call({"op": "settings_get", "_control_token": token})
        self.assertTrue(res["ok"], res)


if __name__ == "__main__":
    unittest.main()
