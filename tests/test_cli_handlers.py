"""In-process tests for the CLI command handlers in ``crewhall.cli``.

The handlers are pure "call the daemon and print" code: each one builds a
``Client`` via ``_client`` and prints fields of the returned dict. These tests
replace ``_client`` with a recording fake and drive the *real* ``main(argv)``
parser, so the dispatch, argument handling and printing branches are exercised
without a daemon, a TTY or any network.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from crewhall import bundle, cli, office_setup, specs, updater


def _session(**over: object) -> dict:
    info = {
        "session_id": "sess_abc123",
        "name": "worker",
        "backend": "pty",
        "pid": 1234,
        "status": "running",
        "exit_code": None,
        "command": "bash -i",
        "cwd": "/tmp",
        "created_at": 1000.0,
        "last_input_at": 1001.0,
        "last_output_at": 1002.0,
        "exited_at": None,
        "meta": {},
    }
    info.update(over)
    return info


def _agent(**over: object) -> dict:
    info = {
        "agent_id": "agent_1",
        "name": "worker",
        "kind": "opencode",
        "state": "ready",
        "backend": "tmux",
        "pid": 42,
        "evidence": "matched",
    }
    info.update(over)
    return info


def _terminal(**over: object) -> dict:
    info = {
        "session_id": "term_1",
        "host": "local",
        "status": "running",
        "cwd": "/tmp",
        "title": "term",
        "readonly": False,
    }
    info.update(over)
    return info


def _delivery(**over: object) -> dict:
    d = {
        "status": "delivered",
        "delivered": True,
        "queued": False,
        "message_id": "msg_1",
        "sender": "alice",
        "recipient": "bob",
        "body": "hi",
    }
    d.update(over)
    return d


def _team(**over: object) -> dict:
    t = {"team_id": "team_1", "name": "core", "members": [], "missing": []}
    t.update(over)
    return t


class FakeClient:
    """Records every ``call`` and returns a canned response per op."""

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict = dict(responses or {})

    def call(self, op: str, **params: object) -> dict:
        self.calls.append((op, params))
        resp = self.responses.get(op, {})
        if isinstance(resp, Exception):
            raise resp
        if callable(resp):
            return resp(params)
        return resp

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


class CliTestCase(unittest.TestCase):
    def run_cli(self, argv: list[str], responses: dict | None = None,
                client: FakeClient | None = None) -> tuple[int, str, str, FakeClient]:
        client = client or FakeClient(responses)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_client", return_value=client):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = cli.main(argv)
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 1
        return rc, out.getvalue(), err.getvalue(), client

    @staticmethod
    def env(**kv: str) -> mock._patch_dict:
        return mock.patch.dict(os.environ, kv)


class SessionsTest(CliTestCase):
    def test_list_empty(self) -> None:
        rc, out, _err, _c = self.run_cli(["list"], {"list": {"sessions": []}})
        self.assertEqual(rc, 0)
        self.assertIn("no sessions", out)

    def test_list_json(self) -> None:
        sess = _session()
        rc, out, _e, _c = self.run_cli(["list", "--json"], {"list": {"sessions": [sess]}})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [sess])

    def test_list_table(self) -> None:
        rc, out, _e, _c = self.run_cli(
            ["list"], {"list": {"sessions": [_session(name=None, pid=None)]}}
        )
        self.assertEqual(rc, 0)
        self.assertIn("SESSION_ID", out)
        self.assertIn("sess_abc123", out)

    def test_create_default_command(self) -> None:
        rc, _o, _e, c = self.run_cli(["create"], {"create": {"session": _session()}})
        self.assertEqual(rc, 0)
        _op, kw = c.calls[0]
        self.assertEqual(kw["command"], cli._default_shell())

    def test_create_command(self) -> None:
        rc, _o, _e, c = self.run_cli(
            ["create", "-c", "/tmp", "bash", "-i"], {"create": {"session": _session()}}
        )
        self.assertEqual(rc, 0)
        _op, kw = c.calls[0]
        self.assertEqual(kw["command"], ["bash", "-i"])
        self.assertEqual(kw["cwd"], "/tmp")

    def test_create_shell_string(self) -> None:
        rc, _o, _e, c = self.run_cli(
            ["create", "--shell", "echo", "hi"], {"create": {"session": _session()}}
        )
        self.assertEqual(rc, 0)
        self.assertEqual(c.calls[0][1]["command"], "echo hi")

    def test_send_and_enter(self) -> None:
        rc, _o, _e, c = self.run_cli(["send", "t1", "hello", "--enter"])
        self.assertEqual(rc, 0)
        self.assertEqual(c.ops(), ["write", "enter"])
        self.assertEqual(c.calls[0][1]["text"], "hello")

    def test_send_shell_escaped(self) -> None:
        rc, _o, _e, c = self.run_cli(["send", "t1", "a b", "--shell"])
        self.assertEqual(rc, 0)
        self.assertEqual(c.calls[0][1]["text"], "'a b'")

    def test_sendline(self) -> None:
        rc, _o, _e, c = self.run_cli(["sendline", "t1", "hello"])
        self.assertEqual(rc, 0)
        self.assertEqual(c.ops(), ["write", "enter"])

    def test_key_and_enter(self) -> None:
        rc, _o, _e, c = self.run_cli(["key", "t1", "C-c"])
        self.assertEqual(rc, 0)
        self.assertEqual(c.calls[0], ("key", {"target": "t1", "key": "C-c"}))
        rc, _o, _e, c = self.run_cli(["enter", "t1"])
        self.assertEqual(rc, 0)
        self.assertEqual(c.ops(), ["enter"])

    def test_capture(self) -> None:
        rc, out, _e, _c = self.run_cli(["capture", "t1"], {"capture": {"output": "hello"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "hello\n")

    def test_capture_trailing_newline(self) -> None:
        rc, out, _e, _c = self.run_cli(["capture", "t1"], {"capture": {"output": "hi\n"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "hi\n")

    def test_read(self) -> None:
        rc, out, _e, _c = self.run_cli(["read", "t1"], {"read": {"output": "data"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "data")

    def test_read_until_matched(self) -> None:
        rc, out, _e, _c = self.run_cli(
            ["read-until", "t1", "PAT"], {"read_until": {"output": "PAT", "matched": True}}
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "PAT\n")

    def test_read_until_unmatched(self) -> None:
        rc, _o, _e, _c = self.run_cli(
            ["read-until", "t1", "PAT"], {"read_until": {"output": "x\n", "matched": False}}
        )
        self.assertEqual(rc, 1)

    def test_resize_interrupt(self) -> None:
        rc, _o, _e, _c = self.run_cli(
            ["resize", "t1", "100", "40"], {"resize": {"session": _session()}}
        )
        self.assertEqual(rc, 0)
        rc, _o, _e, _c = self.run_cli(
            ["interrupt", "t1"], {"interrupt": {"session": _session()}}
        )
        self.assertEqual(rc, 0)

    def test_kill_target(self) -> None:
        rc, _o, _e, c = self.run_cli(["kill", "t1"], {"kill": {"session": _session()}})
        self.assertEqual(rc, 0)
        self.assertEqual(c.ops(), ["kill"])

    def test_kill_all(self) -> None:
        sessions = [
            _session(session_id="s1", status="running"),
            _session(session_id="s2", status="starting"),
            _session(session_id="s3", status="exited"),
        ]
        rc, _o, _e, c = self.run_cli(["kill", "--all"], {"list": {"sessions": sessions}})
        self.assertEqual(rc, 0)
        killed = [kw["target"] for op, kw in c.calls if op == "kill"]
        self.assertEqual(killed, ["s1", "s2"])

    def test_status_json(self) -> None:
        rc, out, _e, _c = self.run_cli(
            ["status", "t1", "--json"], {"status": {"session": _session()}}
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["session_id"], "sess_abc123")

    def test_status_human(self) -> None:
        rc, out, _e, _c = self.run_cli(
            ["status", "t1"],
            {"status": {"session": _session(meta={"k": "v"}, exited_at=1005.0)}},
        )
        self.assertEqual(rc, 0)
        self.assertIn("created_at: 1000.000", out)
        self.assertIn("meta: {'k': 'v'}", out)

    def test_events(self) -> None:
        events = [{"at": 1000.0, "type": "output", "data": {"n": 1}}]
        rc, out, _e, _c = self.run_cli(["events", "t1"], {"events": {"events": events}})
        self.assertEqual(rc, 0)
        self.assertIn("output", out)

    def test_attach_unsupported_backend(self) -> None:
        rc, _o, err, _c = self.run_cli(
            ["attach", "t1"],
            {"attach_info": {"session": _session(backend="pty")}},
        )
        self.assertEqual(rc, 2)
        self.assertIn("cannot be attached", err)

    def test_attach_tmux(self) -> None:
        info = _session(backend="tmux", meta={"tmux_session": "crewhall-x"})
        with mock.patch.object(cli.subprocess, "call", return_value=0) as call:
            rc, _o, _e, _c = self.run_cli(
                ["attach", "t1"], {"attach_info": {"session": info}}
            )
        self.assertEqual(rc, 0)
        self.assertTrue(call.called)

    def test_attach_ssh_tmux(self) -> None:
        info = _session(backend="ssh-tmux", host="box", meta={"tmux_session": "x"})
        with mock.patch("crewhall.settings.host", return_value={"ssh": "u@h"}), \
                mock.patch("crewhall.backends.ssh_tmux.SshTmuxBackend.attach_command",
                           return_value=["ssh"]), \
                mock.patch.object(cli.subprocess, "call", return_value=0) as call:
            rc, _o, _e, _c = self.run_cli(
                ["attach", "t1"], {"attach_info": {"session": info}}
            )
        self.assertEqual(rc, 0)
        self.assertTrue(call.called)


class TerminalTest(CliTestCase):
    def test_disabled(self) -> None:
        for argv in (
            ["terminal", "new"],
            ["terminal", "ls"],
            ["terminal", "send", "t", "hi"],
            ["terminal", "key", "t", "C-c"],
            ["terminal", "capture", "t"],
            ["terminal", "close", "t"],
            ["terminal", "attach", "t"],
        ):
            rc, _o, err, _c = self.run_cli(argv, {"meta_info": {}})
            self.assertEqual(rc, 2, argv)
            self.assertIn("disabled", err)

    def test_new(self) -> None:
        rc, out, _e, _c = self.run_cli(
            ["terminal", "new", "--cwd", "/tmp", "--title", "x"],
            {"meta_info": {"terminals_enabled": True},
             "terminal_create": {"terminal": _terminal()}},
        )
        self.assertEqual(rc, 0)
        self.assertIn("term_1", out)

    def test_ls(self) -> None:
        base = {"meta_info": {"terminals_enabled": True}}
        rc, out, _e, _c = self.run_cli(["terminal", "ls"], {**base, "terminal_list": {"terminals": []}})
        self.assertEqual(rc, 0)
        self.assertIn("no terminals", out)
        rc, out, _e, _c = self.run_cli(
            ["terminal", "ls", "--json"],
            {**base, "terminal_list": {"terminals": [_terminal()]}},
        )
        self.assertEqual(rc, 0)
        self.assertIn("term_1", out)
        rc, out, _e, _c = self.run_cli(
            ["terminal", "ls"], {**base, "terminal_list": {"terminals": [_terminal()]}}
        )
        self.assertEqual(rc, 0)
        self.assertIn("host=local", out)

    def test_send_key_capture_close(self) -> None:
        base = {"meta_info": {"terminals_enabled": True}}
        rc, _o, _e, c = self.run_cli(["terminal", "send", "t", "hi", "--enter"], base)
        self.assertEqual(rc, 0)
        self.assertEqual(c.ops(), ["meta_info", "terminal_write"])
        rc, _o, _e, c = self.run_cli(["terminal", "key", "t", "C-c"], base)
        self.assertEqual(rc, 0)
        rc, out, _e, _c = self.run_cli(
            ["terminal", "capture", "t"],
            {**base, "terminal_capture": {"output": "screen"}},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "screen")
        rc, _o, _e, c = self.run_cli(["terminal", "close", "t"], base)
        self.assertEqual(rc, 0)

    def test_attach_delegates(self) -> None:
        base = {"meta_info": {"terminals_enabled": True}}
        info = _session(backend="tmux", meta={})
        with mock.patch.object(cli.subprocess, "call", return_value=0):
            rc, _o, _e, _c = self.run_cli(
                ["terminal", "attach", "t"], {**base, "attach_info": {"session": info}}
            )
        self.assertEqual(rc, 0)


class AgentIdentityTest(CliTestCase):
    def test_identity_requires_agent(self) -> None:
        rc, _o, err, _c = self.run_cli(["agent", "identity"])
        self.assertEqual(rc, 2)
        self.assertIn("not inside", err)

    def test_identity_human(self) -> None:
        resp = {
            "agent": _agent(),
            "teams": ["core"],
            "teammates": [{"name": "peer", "kind": "claude", "state": "working"}],
        }
        with self.env(CREWHALL_AGENT_ID="agent_1", CREWHALL_TOKEN="tok"):
            rc, out, _e, _c = self.run_cli(["agent", "identity"], {"agent_identity": resp})
        self.assertEqual(rc, 0)
        self.assertIn("YOU: worker", out)
        self.assertIn("TEAMS: core", out)
        self.assertIn("peer", out)

    def test_identity_json_empty_teams(self) -> None:
        resp = {"agent": _agent(), "teams": [], "teammates": []}
        with self.env(CREWHALL_AGENT_ID="agent_1"):
            rc, out, _e, _c = self.run_cli(
                ["agent", "identity", "--json"], {"agent_identity": resp}
            )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), resp)

    def test_identity_no_teammates(self) -> None:
        resp = {"agent": _agent(), "teams": ["solo"], "teammates": []}
        with self.env(CREWHALL_AGENT_ID="agent_1"):
            rc, out, _e, _c = self.run_cli(["agent", "identity"], {"agent_identity": resp})
        self.assertEqual(rc, 0)
        self.assertIn("(none)", out)


class MessageTest(CliTestCase):
    def test_send_requires_sender(self) -> None:
        rc, _o, err, _c = self.run_cli(["message", "send", "--to", "b", "--message", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("not inside", err)

    def test_send_delivered(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, out, _e, c = self.run_cli(
                ["message", "send", "--to", "b", "--message", "x"],
                {"team_send": {"delivery": _delivery()}},
            )
        self.assertEqual(rc, 0)
        self.assertIn("alice -> bob", out)

    def test_send_queued(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, out, _e, _c = self.run_cli(
                ["message", "send", "--to", "b", "--message", "x"],
                {"team_send": {"delivery": _delivery(
                    status="queued", delivered=False, queued=True,
                    queue_reason="human draft")}},
            )
        self.assertEqual(rc, 0)
        self.assertIn("queued", out)

    def test_send_not_delivered(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, _o, _e, _c = self.run_cli(
                ["message", "send", "--to", "b", "--message", "x"],
                {"team_send": {"delivery": _delivery(
                    status="failed", delivered=False, queued=False, error="boom")}},
            )
        self.assertEqual(rc, 1)

    def test_request_requires_sender(self) -> None:
        rc, _o, _e, _c = self.run_cli(
            ["message", "request", "--to", "b", "--task", "t"]
        )
        self.assertEqual(rc, 2)

    def test_request_no_wait(self) -> None:
        req = {"request_id": "r1", "recipient": "b", "state": "open"}
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, out, _e, _c = self.run_cli(
                ["message", "request", "--to", "b", "--task", "t", "--no-wait"],
                {"request_create": {"request": req}},
            )
        self.assertEqual(rc, 0)
        self.assertIn("request r1 to b", out)

    def test_request_replied(self) -> None:
        req = {"request_id": "r1", "recipient": "b", "state": "replied", "reply": "yes"}
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, out, _e, _c = self.run_cli(
                ["message", "request", "--to", "b", "--task", "t"],
                {"request_create": {"request": req}},
            )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "yes\n")

    def test_request_pending(self) -> None:
        req = {"request_id": "r1", "recipient": "b", "state": "pending"}
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, _o, err, _c = self.run_cli(
                ["message", "request", "--to", "b", "--task", "t"],
                {"request_create": {"request": req}},
            )
        self.assertEqual(rc, 1)
        self.assertIn("pending", err)

    def test_request_rpc_error(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, _o, err, _c = self.run_cli(
                ["message", "request", "--to", "b", "--task", "t"],
                {"request_create": cli.RpcError("nope")},
            )
        self.assertEqual(rc, 1)
        self.assertIn("error: nope", err)

    def test_reply(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, out, _e, _c = self.run_cli(
                ["message", "reply", "--request-id", "r1", "--message", "ok"],
                {"request_reply": {"request": {"request_id": "r1"}}},
            )
        self.assertEqual(rc, 0)
        self.assertIn("replied to r1", out)

    def test_reply_requires_sender(self) -> None:
        rc, _o, _e, _c = self.run_cli(
            ["message", "reply", "--request-id", "r1", "--message", "ok"]
        )
        self.assertEqual(rc, 2)

    def test_reply_rpc_error(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, _o, err, _c = self.run_cli(
                ["message", "reply", "--request-id", "r1", "--message", "ok"],
                {"request_reply": cli.RpcError("bad")},
            )
        self.assertEqual(rc, 1)
        self.assertIn("error: bad", err)

    def test_requests(self) -> None:
        reqs = [{"request_id": "r1", "sender": "a", "recipient": "b",
                 "state": "open", "task": "do it"}]
        rc, out, _e, _c = self.run_cli(["message", "requests"], {"request_list": {"requests": reqs}})
        self.assertEqual(rc, 0)
        self.assertIn("r1", out)
        rc, out, _e, _c = self.run_cli(
            ["message", "requests", "--json"], {"request_list": {"requests": reqs}}
        )
        self.assertEqual(json.loads(out), reqs)
        rc, out, _e, _c = self.run_cli(["message", "requests"], {"request_list": {"requests": []}})
        self.assertIn("no requests", out)

    def test_requests_rpc_error(self) -> None:
        rc, _o, err, _c = self.run_cli(
            ["message", "requests"], {"request_list": cli.RpcError("down")}
        )
        self.assertEqual(rc, 1)
        self.assertIn("error: down", err)


class AgentNewSessionTest(CliTestCase):
    def test_confirmed_as_agent(self) -> None:
        out = {"agent": _agent(), "confirmed": True, "command": "/new",
               "conversation_id": "c1"}
        with self.env(CREWHALL_AGENT_ID="a"):
            rc, text, _e, c = self.run_cli(
                ["agent", "new-session", "worker"], {"team_new_session": out}
            )
        self.assertEqual(rc, 0)
        self.assertIn("conversation=c1", text)
        self.assertEqual(c.calls[0][0], "team_new_session")

    def test_confirmed_no_conversation(self) -> None:
        out = {"agent": _agent(), "confirmed": True, "command": "/clear"}
        rc, text, _e, c = self.run_cli(
            ["agent", "new-session", "worker"], {"agent_new_session": out}
        )
        self.assertEqual(rc, 0)
        self.assertEqual(c.calls[0][0], "agent_new_session")

    def test_not_confirmed(self) -> None:
        out = {"agent": _agent(), "confirmed": False, "command": "/new"}
        rc, _o, err, _c = self.run_cli(
            ["agent", "new-session", "worker"], {"agent_new_session": out}
        )
        self.assertEqual(rc, 3)
        self.assertIn("not confirmed", err)

    def test_rpc_error(self) -> None:
        rc, _o, err, _c = self.run_cli(
            ["agent", "new-session", "worker"], {"agent_new_session": cli.RpcError("x")}
        )
        self.assertEqual(rc, 1)
        self.assertIn("error: x", err)


class HostTest(CliTestCase):
    def test_list_empty(self) -> None:
        rc, out, _e, _c = self.run_cli(["host", "list"], {"host_list": {"hosts": []}})
        self.assertEqual(rc, 0)
        self.assertIn("no hosts", out)

    def test_list_json_and_table(self) -> None:
        hosts = [{"name": "box", "ssh": "u@h", "state": "ok", "tunnel": True,
                  "tunnel_state": None, "agents": 2},
                 {"name": "box2", "ssh": "u@h2", "state": "off", "tunnel": False,
                  "tunnel_state": "on", "agents": 0}]
        rc, out, _e, _c = self.run_cli(["host", "list", "--json"], {"host_list": {"hosts": hosts}})
        self.assertEqual(json.loads(out), hosts)
        rc, out, _e, _c = self.run_cli(["host", "list"], {"host_list": {"hosts": hosts}})
        self.assertIn("HOST", out)
        self.assertIn("box", out)

    def test_add(self) -> None:
        host = {"name": "box", "ssh": "u@h", "port": 22}
        rc, out, _e, c = self.run_cli(
            ["host", "add", "box", "u@h", "--identity", "/k", "--known-hosts", "/kh",
             "--tmux-socket", "s", "--tunnel"],
            {"host_set": {"host": host}},
        )
        self.assertEqual(rc, 0)
        self.assertIn("saved", out)
        _op, kw = c.calls[0]
        self.assertEqual(kw["identity"], "/k")
        self.assertEqual(kw["confirm"], "CONFIRM")

    def test_add_rpc_error(self) -> None:
        rc, _o, err, _c = self.run_cli(
            ["host", "add", "box", "u@h"], {"host_set": cli.RpcError("bad")}
        )
        self.assertEqual(rc, 2)
        self.assertIn("error: bad", err)

    def test_remove(self) -> None:
        rc, out, _e, _c = self.run_cli(["host", "remove", "box"])
        self.assertEqual(rc, 0)
        self.assertIn("removed", out)

    def test_remove_rpc_error(self) -> None:
        rc, _o, _e, _c = self.run_cli(["host", "remove", "box"], {"host_remove": cli.RpcError("x")})
        self.assertEqual(rc, 2)

    def test_test_ok(self) -> None:
        out = {"ok": True, "tmux": True, "crewhall": True, "git": True,
               "claude": True, "opencode": False}
        rc, text, _e, _c = self.run_cli(["host", "test", "box"], {"host_test": out})
        self.assertEqual(rc, 0)
        self.assertIn("connected to box", text)

    def test_test_no_tmux(self) -> None:
        out = {"ok": True, "tmux": False, "crewhall": True, "git": False,
               "claude": False, "opencode": False}
        rc, _o, err, _c = self.run_cli(["host", "test", "box"], {"host_test": out})
        self.assertEqual(rc, 1)
        self.assertIn("tmux is required", err)

    def test_test_unreachable(self) -> None:
        out = {"ok": False, "error": "refused", "detail": "timeout"}
        rc, _o, err, _c = self.run_cli(["host", "test", "box"], {"host_test": out})
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", err)

    def test_test_rpc_error(self) -> None:
        rc, _o, _e, _c = self.run_cli(["host", "test", "box"], {"host_test": cli.RpcError("x")})
        self.assertEqual(rc, 2)


class AgentListTest(CliTestCase):
    def test_team_members(self) -> None:
        members = [{"name": None, "agent_id": "a1", "kind": "claude", "state": "ready"}]
        rc, out, _e, _c = self.run_cli(
            ["agent", "list", "--team", "core"], {"team_members": {"members": members}}
        )
        self.assertEqual(rc, 0)
        self.assertIn("a1", out)
        rc, out, _e, _c = self.run_cli(
            ["agent", "list", "--team", "core", "--json"],
            {"team_members": {"members": members}},
        )
        self.assertEqual(json.loads(out), members)
        rc, out, _e, _c = self.run_cli(
            ["agent", "list", "--team", "core"], {"team_members": {"members": []}}
        )
        self.assertIn("no members", out)

    def test_agents(self) -> None:
        agents = [_agent(name=None)]
        rc, out, _e, _c = self.run_cli(["agent", "list"], {"agent_list": {"agents": agents}})
        self.assertEqual(rc, 0)
        self.assertIn("AGENT_ID", out)
        rc, out, _e, _c = self.run_cli(
            ["agent", "list", "--json"], {"agent_list": {"agents": agents}}
        )
        self.assertEqual(json.loads(out), agents)
        rc, out, _e, _c = self.run_cli(["agent", "list"], {"agent_list": {"agents": []}})
        self.assertIn("no agents", out)


class FrontendTest(CliTestCase):
    def _resp(self, **over: object) -> dict:
        resp = {
            "mode": "local",
            "frontends": {
                "local": {"running": True, "auth": "none", "url": "http://127.0.0.1:8765"},
                "tailscale": {"running": False, "auth": "required",
                              "url": "http://ts:8765", "error": "off"},
            },
        }
        resp.update(over)
        return resp

    def test_frontends_human(self) -> None:
        rc, out, _e, _c = self.run_cli(["frontends"], {"frontend_status": self._resp()})
        self.assertEqual(rc, 0)
        self.assertIn("web interfaces: local", out)
        self.assertIn("127.0.0.1", out)

    def test_frontends_token_created(self) -> None:
        resp = self._resp(token_created=True, token="SECRET")
        rc, out, _e, _c = self.run_cli(
            ["frontends"], {"frontend_status": {**resp,
             "frontends": {**resp["frontends"],
                           "tailscale": {"running": True, "auth": "required",
                                         "url": "http://ts:8765"}}}}
        )
        self.assertIn("SECRET", out)

    def test_frontends_tailscale_running(self) -> None:
        resp = self._resp(
            frontends={"local": {"running": False, "auth": "none", "url": "",
                                 "error": "down"},
                       "tailscale": {"running": True, "auth": "required",
                                     "url": "http://ts:8765"}}
        )
        rc, out, _e, _c = self.run_cli(["frontends"], {"frontend_status": resp})
        self.assertIn("Login token", out)
        self.assertIn("error: down", out)

    def test_frontends_json(self) -> None:
        resp = self._resp()
        rc, out, _e, _c = self.run_cli(["frontends", "--json"], {"frontend_status": resp})
        self.assertEqual(json.loads(out), resp)

    def test_frontend_modes(self) -> None:
        resp = self._resp()
        for argv in (["local"], ["off"], ["tailscale"], ["tailscale", "--keep-local"]):
            rc, _o, _e, c = self.run_cli(argv, {"frontend_set": resp})
            self.assertEqual(rc, 0)
            self.assertEqual(c.calls[0][0], "frontend_set")
        # --keep-local rewrites the mode to "both".
        _rc, _o, _e, c = self.run_cli(["tailscale", "--keep-local"], {"frontend_set": resp})
        self.assertEqual(c.calls[0][1]["mode"], "both")


class UpdateTest(CliTestCase):
    def test_status(self) -> None:
        st = {"version": "1.0", "channel": "stable", "commit": "abc", "managed": True,
              "daemon_version": "1.0", "restart_pending": True,
              "previous": "0.9", "rollback_available": True, "signers_configured": True}
        with mock.patch("crewhall.updater.status", return_value=st):
            rc, out, _e, _c = self.run_cli(["update", "--status"])
        self.assertEqual(rc, 0)
        self.assertIn("RESTART PENDING", out)

    def test_status_minimal(self) -> None:
        st = {"version": "1.0", "channel": "stable", "commit": "", "managed": False,
              "daemon_version": None, "restart_pending": False, "signers_configured": False}
        with mock.patch("crewhall.updater.status", return_value=st):
            rc, out, _e, _c = self.run_cli(["update", "--status"])
        self.assertEqual(rc, 0)
        self.assertIn("not running", out)

    def test_rollback(self) -> None:
        with mock.patch("crewhall.updater.rollback", return_value={"to": "0.9", "restarted": True}):
            rc, out, _e, _c = self.run_cli(["update", "--rollback"])
        self.assertEqual(rc, 0)
        self.assertIn("rolled back to 0.9", out)

    def test_rollback_no_restart(self) -> None:
        with mock.patch("crewhall.updater.rollback",
                        return_value={"to": "0.9", "restarted": False}):
            rc, out, _e, _c = self.run_cli(["update", "--rollback"])
        self.assertIn("daemon not restarted", out)

    def test_restart_pending(self) -> None:
        with mock.patch("crewhall.updater.restart_pending",
                        return_value={"status": "nothing-pending"}):
            rc, out, _e, _c = self.run_cli(["update", "--restart"])
        self.assertIn("already runs", out)
        with mock.patch("crewhall.updater.restart_pending",
                        return_value={"status": "restarted", "was": "0.9", "version": "1.0"}):
            rc, out, _e, _c = self.run_cli(["update", "--restart"])
        self.assertIn("restarted: 0.9 -> 1.0", out)

    def test_missing_source(self) -> None:
        rc, _o, err, _c = self.run_cli(["update"])
        self.assertEqual(rc, 2)
        self.assertIn("--from", err)

    def test_check_up_to_date(self) -> None:
        out = {"current": "1.0", "available": "1.0", "newer": False, "signature": "ok"}
        with mock.patch("crewhall.updater.check", return_value=out):
            rc, text, _e, _c = self.run_cli(["update", "--from", "/r", "--check"])
        self.assertEqual(rc, 0)
        self.assertIn("up to date", text)

    def test_check_newer(self) -> None:
        out = {"current": "1.0", "available": "1.1", "newer": True, "signature": "ok"}
        with mock.patch("crewhall.updater.check", return_value=out):
            rc, text, _e, _c = self.run_cli(["update", "--from", "/r", "--check"])
        self.assertEqual(rc, 10)
        self.assertIn("UPDATE AVAILABLE", text)

    def test_apply_up_to_date(self) -> None:
        with mock.patch("crewhall.updater.apply",
                        return_value={"status": "up-to-date", "version": "1.0"}):
            rc, out, _e, _c = self.run_cli(["update", "--from", "/r"])
        self.assertEqual(rc, 0)
        self.assertIn("already at 1.0", out)

    def test_apply_updated(self) -> None:
        with mock.patch("crewhall.updater.apply",
                        return_value={"status": "updated", "from": "0.9", "to": "1.0",
                                      "restart_pending": True}):
            rc, out, _e, _c = self.run_cli(["update", "--from", "/r"])
        self.assertEqual(rc, 0)
        self.assertIn("updated 0.9 -> 1.0", out)

    def test_apply_restart_refused(self) -> None:
        with mock.patch("crewhall.updater.apply",
                        return_value={"status": "updated", "from": "0.9", "to": "1.0",
                                      "restart_refused": "agents running"}):
            rc, _o, err, _c = self.run_cli(["update", "--from", "/r"])
        self.assertEqual(rc, 3)
        self.assertIn("NOT restarted", err)

    def test_restart_refused_exception(self) -> None:
        with mock.patch("crewhall.updater.apply",
                        side_effect=updater.RestartRefused("busy")):
            rc, _o, err, _c = self.run_cli(["update", "--from", "/r"])
        self.assertEqual(rc, 3)
        self.assertIn("refused", err)

    def test_update_error(self) -> None:
        with mock.patch("crewhall.updater.apply", side_effect=updater.UpdateError("bad")):
            rc, _o, err, _c = self.run_cli(["update", "--from", "/r"])
        self.assertEqual(rc, 1)
        self.assertIn("error: bad", err)


class SelftestDoctorCleanTest(CliTestCase):
    def test_selftest(self) -> None:
        with mock.patch("crewhall.selftest.run", return_value={"ok": True}):
            rc, out, _e, _c = self.run_cli(["selftest", "--cwd", "/tmp"])
        self.assertEqual(rc, 0)
        self.assertIn("PASSED", out)

    def test_selftest_json_failed(self) -> None:
        with mock.patch("crewhall.selftest.run", return_value={"ok": False}):
            rc, out, _e, _c = self.run_cli(["selftest", "--json"])
        self.assertEqual(rc, 1)
        self.assertIn('"ok": false', out)

    def test_doctor(self) -> None:
        with mock.patch("crewhall.doctor.run_checks", return_value=[]), \
                mock.patch("crewhall.doctor.fix_control_files", return_value=[]), \
                mock.patch("crewhall.doctor.format_report", return_value="REPORT"), \
                mock.patch("crewhall.doctor.exit_code", return_value=0):
            rc, out, _e, _c = self.run_cli(["doctor", "--fix-control-files"])
        self.assertEqual(rc, 0)
        self.assertIn("REPORT", out)

    def test_doctor_json(self) -> None:
        with mock.patch("crewhall.doctor.run_checks", return_value=[{"name": "x"}]), \
                mock.patch("crewhall.doctor.exit_code", return_value=1):
            rc, out, _e, _c = self.run_cli(["doctor", "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out), [{"name": "x"}])

    def test_clean_nothing(self) -> None:
        with mock.patch("crewhall.clean.plan_cleanup", return_value=cli.clean.Plan()):
            rc, out, _e, _c = self.run_cli(["clean"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing to clean", out)

    def test_clean_json_dry_run(self) -> None:
        plan = cli.clean.Plan(items=[cli.clean.Item("/tmp/x", "tmp", 2048)])
        with mock.patch("crewhall.clean.plan_cleanup", return_value=plan):
            rc, out, _e, _c = self.run_cli(["clean", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["dry_run"])

    def test_clean_no_tty(self) -> None:
        items = [cli.clean.Item(f"/tmp/{i}", "tmp", 2 * 1024 ** 3) for i in range(60)]
        plan = cli.clean.Plan(items=items)
        with mock.patch("crewhall.clean.plan_cleanup", return_value=plan), \
                mock.patch.object(cli.sys, "stdin", io.StringIO()):
            rc, out, err, _c = self.run_cli(["clean"])
        self.assertEqual(rc, 0)
        self.assertIn("dry run", err)
        self.assertIn("and 10 more", out)

    def test_clean_yes_removes(self) -> None:
        plan = cli.clean.Plan(items=[cli.clean.Item("/tmp/x", "tmp", 500)])
        result = {"removed": ["/tmp/x"], "failed": [], "bytes": 500}
        with mock.patch("crewhall.clean.plan_cleanup", return_value=plan), \
                mock.patch("crewhall.clean.apply_cleanup", return_value=result):
            rc, out, _e, _c = self.run_cli(["clean", "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("removed 1 item(s)", out)

    def test_clean_tty_confirm_yes(self) -> None:
        plan = cli.clean.Plan(items=[cli.clean.Item("/tmp/x", "tmp", 500)])
        result = {"removed": ["/tmp/x"], "failed": [], "bytes": 500}
        stdin = mock.MagicMock(isatty=lambda: True)
        with mock.patch("crewhall.clean.plan_cleanup", return_value=plan), \
                mock.patch("crewhall.clean.apply_cleanup", return_value=result), \
                mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch("builtins.input", return_value="y"):
            rc, out, _e, _c = self.run_cli(["clean"])
        self.assertEqual(rc, 0)
        self.assertIn("removed 1 item(s)", out)

    def test_clean_tty_confirm_no(self) -> None:
        plan = cli.clean.Plan(items=[cli.clean.Item("/tmp/x", "tmp", 500)])
        stdin = mock.MagicMock(isatty=lambda: True)
        with mock.patch("crewhall.clean.plan_cleanup", return_value=plan), \
                mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch("builtins.input", return_value="n"):
            rc, out, _e, _c = self.run_cli(["clean"])
        self.assertEqual(rc, 0)
        self.assertIn("aborted", out)

    def test_clean_json_yes(self) -> None:
        plan = cli.clean.Plan(items=[cli.clean.Item("/tmp/x", "tmp", 500)])
        result = {"removed": ["/tmp/x"], "failed": [], "bytes": 500}
        with mock.patch("crewhall.clean.plan_cleanup", return_value=plan), \
                mock.patch("crewhall.clean.apply_cleanup", return_value=result):
            rc, out, _e, _c = self.run_cli(["clean", "--json", "--yes"])
        self.assertEqual(rc, 0)
        last = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(last["dry_run"])
        self.assertEqual(last["removed"], ["/tmp/x"])

    def test_clean_yes_failures(self) -> None:
        plan = cli.clean.Plan(items=[cli.clean.Item("/tmp/x", "tmp", 500)])
        result = {"removed": [], "failed": [{"path": "/tmp/x", "error": "denied"}],
                  "bytes": 0}
        with mock.patch("crewhall.clean.plan_cleanup", return_value=plan), \
                mock.patch("crewhall.clean.apply_cleanup", return_value=result):
            rc, _o, err, _c = self.run_cli(["clean", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("could not remove", err)


class BundleTest(CliTestCase):
    def test_export(self) -> None:
        with mock.patch("crewhall.bundle.export_bundle",
                        return_value={"path": "/tmp/b.tar.gz", "files": ["profiles.toml"]}), \
                mock.patch.object(cli, "Client",
                                  return_value=mock.MagicMock(
                                      call=mock.MagicMock(return_value={"teams": []}))):
            rc, out, _e, _c = self.run_cli(["bundle", "export", "/tmp/b.tar.gz",
                                            "--with-token"])
        self.assertEqual(rc, 0)
        self.assertIn("wrote /tmp/b.tar.gz", out)
        self.assertIn("+ profiles.toml", out)

    def test_export_no_live_empty(self) -> None:
        with mock.patch("crewhall.bundle.export_bundle",
                        return_value={"path": "/tmp/b.tar.gz", "files": []}):
            rc, out, _e, _c = self.run_cli(["bundle", "export", "/tmp/b.tar.gz", "--no-live"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing to export", out)

    def test_export_daemon_down(self) -> None:
        with mock.patch("crewhall.bundle.export_bundle",
                        return_value={"path": "/tmp/b.tar.gz", "files": []}), \
                mock.patch.object(cli, "Client",
                                  side_effect=RuntimeError("no daemon")):
            rc, _o, err, _c = self.run_cli(["bundle", "export", "/tmp/b.tar.gz"])
        self.assertEqual(rc, 0)
        self.assertIn("daemon not reachable", err)

    def test_import(self) -> None:
        out = {"written": ["profiles.toml"], "skipped": ["old.toml"],
               "backed_up": ["teams/x.toml"], "dry_run": False}
        with mock.patch("crewhall.bundle.import_bundle", return_value=out), \
                mock.patch("crewhall.bundle.read_bundle", return_value=({}, {})), \
                mock.patch("crewhall.bundle.bundle_team_specs",
                           return_value=([{"team": "core"}], ["warn"])), \
                mock.patch("crewhall.bundle.plan_specs",
                           return_value=[{"team": "core", "agents": []}]), \
                mock.patch("crewhall.bundle.apply_specs",
                           return_value={"created": ["a"], "existing": ["b"],
                                         "skipped": [{"team": "t", "agent": "a",
                                                      "reason": "missing"}],
                                         "failed": [{"team": "t", "error": "e"}]}):
            rc, out_text, err, c = self.run_cli(["bundle", "import", "/tmp/b.tar.gz", "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("wrote profiles.toml", out_text)
        self.assertIn("+ created a", out_text)
        self.assertIn("warning: warn", err)

    def test_import_dry_run(self) -> None:
        out = {"written": [], "skipped": [], "backed_up": [], "dry_run": True}
        with mock.patch("crewhall.bundle.import_bundle", return_value=out), \
                mock.patch("crewhall.bundle.read_bundle", return_value=({}, {})), \
                mock.patch("crewhall.bundle.bundle_team_specs",
                           return_value=([{"team": "core"}], [])), \
                mock.patch("crewhall.bundle.plan_specs",
                           return_value=[{"team": "core", "agents": []}]):
            rc, out_text, _e, _c = self.run_cli(["bundle", "import", "/tmp/b.tar.gz"])
        self.assertEqual(rc, 0)
        self.assertIn("use --apply", out_text)

    def test_bundle_error(self) -> None:
        with mock.patch("crewhall.bundle.import_bundle",
                        side_effect=bundle.BundleError("bad bundle")):
            rc, _o, err, _c = self.run_cli(["bundle", "import", "/tmp/b.tar.gz"])
        self.assertEqual(rc, 2)
        self.assertIn("error: bad bundle", err)


class AgentHookTest(CliTestCase):
    def test_requires_agent(self) -> None:
        with mock.patch.object(cli, "_hook_payload", return_value={}):
            rc, _o, _e, _c = self.run_cli(["agent", "hook", "session_start"])
        self.assertEqual(rc, 2)

    def test_lifecycle_event(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a", CREWHALL_TOKEN="t"), \
                mock.patch.object(cli, "_hook_payload", return_value={"session_id": "c1"}):
            rc, _o, _e, c = self.run_cli(["agent", "hook", "session_start"])
        self.assertEqual(rc, 0)
        _op, kw = c.calls[0]
        self.assertEqual(kw["conversation_id"], "c1")

    def test_permission_request_decision(self) -> None:
        decision = {"behavior": "allow"}
        with self.env(CREWHALL_AGENT_ID="a"), \
                mock.patch.object(cli, "_hook_payload", return_value={}):
            rc, out, _e, _c = self.run_cli(
                ["agent", "hook", "permission_request"],
                {"agent_permission_request": {"decision": decision}},
            )
        self.assertEqual(rc, 0)
        self.assertIn("PermissionRequest", out)

    def test_permission_request_exception(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"), \
                mock.patch.object(cli, "_hook_payload", return_value={}):
            rc, out, _e, _c = self.run_cli(
                ["agent", "hook", "permission_request"],
                {"agent_permission_request": RuntimeError("boom")},
            )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_question_allow(self) -> None:
        decision = {"behavior": "allow", "updatedInput": {"x": 1}}
        with self.env(CREWHALL_AGENT_ID="a"), \
                mock.patch.object(cli, "_hook_payload", return_value={}):
            rc, out, _e, _c = self.run_cli(
                ["agent", "hook", "question_request"],
                {"agent_permission_request": {"decision": decision}},
            )
        self.assertEqual(rc, 0)
        self.assertIn("PreToolUse", out)

    def test_question_deny(self) -> None:
        decision = {"behavior": "deny", "message": "no"}
        with self.env(CREWHALL_AGENT_ID="a"), \
                mock.patch.object(cli, "_hook_payload", return_value={}):
            rc, out, _e, _c = self.run_cli(
                ["agent", "hook", "question_request"],
                {"agent_permission_request": {"decision": decision}},
            )
        self.assertIn("deny", out)

    def test_question_exception(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"), \
                mock.patch.object(cli, "_hook_payload", return_value={}):
            rc, out, _e, _c = self.run_cli(
                ["agent", "hook", "question_request"],
                {"agent_permission_request": RuntimeError("boom")},
            )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_question_no_decision(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"), \
                mock.patch.object(cli, "_hook_payload", return_value={}):
            rc, out, _e, _c = self.run_cli(
                ["agent", "hook", "question_request"],
                {"agent_permission_request": {}},
            )
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_hook_payload_variants(self) -> None:
        # isatty -> empty
        with mock.patch.object(cli.sys, "stdin", mock.MagicMock(isatty=lambda: True)):
            self.assertEqual(cli._hook_payload(), {})
        # no data ready -> empty
        fake = mock.MagicMock(isatty=lambda: False)
        with mock.patch.object(cli.sys, "stdin", fake), \
                mock.patch("select.select", return_value=([], [], [])):
            self.assertEqual(cli._hook_payload(), {})
        # valid JSON dict
        stdin = io.StringIO('{"session_id": "x"}')
        with mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch("select.select", return_value=([stdin], [], [])):
            self.assertEqual(cli._hook_payload(), {"session_id": "x"})
        # invalid JSON
        stdin = io.StringIO("nope")
        with mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch("select.select", return_value=([stdin], [], [])):
            self.assertEqual(cli._hook_payload(), {})
        # non-dict JSON
        stdin = io.StringIO("[1, 2]")
        with mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch("select.select", return_value=([stdin], [], [])):
            self.assertEqual(cli._hook_payload(), {})


class AgentCommandsTest(CliTestCase):
    def test_create(self) -> None:
        with mock.patch("crewhall.specs.load_profiles", return_value={}), \
                mock.patch("crewhall.specs.apply_profile",
                           return_value={"kind": "claude", "args": "--x"}):
            rc, _o, _e, c = self.run_cli(
                ["agent", "create", "-n", "w", "--args=--x"],
                {"agent_create": {"agent": _agent()}},
            )
        self.assertEqual(rc, 0)
        _op, kw = c.calls[0]
        self.assertEqual(kw["kind"], "claude")
        self.assertEqual(kw["args"], "--x")

    def test_create_spec_error(self) -> None:
        with mock.patch("crewhall.specs.load_profiles", return_value={}), \
                mock.patch("crewhall.specs.apply_profile",
                           side_effect=specs.SpecError("bad")):
            rc, _o, err, _c = self.run_cli(["agent", "create", "-n", "w"])
        self.assertEqual(rc, 2)
        self.assertIn("error: bad", err)

    def test_send_capture_state_wait_stop(self) -> None:
        rc, _o, _e, c = self.run_cli(
            ["agent", "send", "w", "hi"], {"agent_send": {"agent": _agent()}}
        )
        self.assertEqual(rc, 0)
        rc, out, _e, _c = self.run_cli(
            ["agent", "capture", "w"], {"agent_capture": {"output": "text"}}
        )
        self.assertEqual(out, "text\n")
        rc, out, _e, _c = self.run_cli(
            ["agent", "state", "w", "--json"], {"agent_state": {"agent": _agent()}}
        )
        self.assertEqual(json.loads(out)["agent_id"], "agent_1")
        rc, out, _e, _c = self.run_cli(
            ["agent", "state", "w"], {"agent_state": {"agent": _agent()}}
        )
        self.assertIn("agent_1", out)
        rc, _o, _e, _c = self.run_cli(
            ["agent", "wait", "w", "ready"],
            {"agent_wait": {"agent": _agent(), "reached": True}},
        )
        self.assertEqual(rc, 0)
        rc, _o, _e, _c = self.run_cli(
            ["agent", "wait", "w", "ready"],
            {"agent_wait": {"agent": _agent(), "reached": False}},
        )
        self.assertEqual(rc, 1)
        rc, _o, _e, _c = self.run_cli(
            ["agent", "stop", "w", "--force"], {"agent_stop": {"agent": _agent()}}
        )
        self.assertEqual(rc, 0)

    def test_agent_message(self) -> None:
        rc, out, _e, _c = self.run_cli(
            ["agent", "message", "a", "b", "hi"], {"message_send": {"delivery": _delivery()}}
        )
        self.assertEqual(rc, 0)
        self.assertIn("alice -> bob", out)
        rc, _o, _e, _c = self.run_cli(
            ["agent", "message", "a", "b", "hi"],
            {"message_send": {"delivery": _delivery(delivered=False)}},
        )
        self.assertEqual(rc, 1)

    def test_delivery_marks(self) -> None:
        cases = [
            _delivery(status="acknowledged"),
            _delivery(status="queued", delivered=False, queued=True,
                      queue_reason="starting"),
            _delivery(status="queued", delivered=False, queued=True,
                      queue_reason="stopped"),
            _delivery(status="queued", delivered=False, queued=True, queue_reason="busy"),
            _delivery(status="queued", delivered=False, queued=True, queue_reason="other"),
            _delivery(status="failed", delivered=False, queued=False, error="boom"),
        ]
        for delivery in cases:
            rc, out, _e, _c = self.run_cli(
                ["agent", "message", "a", "b", "hi"],
                {"message_send": {"delivery": delivery}},
            )
            self.assertIn(delivery["message_id"], out)

    def test_agent_messages(self) -> None:
        msgs = [{"timestamp": 1000.0, "delivered": True, "message_id": "m1",
                 "sender": "a", "recipient": "b", "body": "hi"}]
        rc, out, _e, _c = self.run_cli(
            ["agent", "messages", "--agent", "a", "--limit", "5"],
            {"message_history": {"messages": msgs}},
        )
        self.assertEqual(rc, 0)
        self.assertIn("m1", out)
        rc, out, _e, _c = self.run_cli(
            ["agent", "messages", "--json"], {"message_history": {"messages": msgs}}
        )
        self.assertEqual(json.loads(out), msgs)
        rc, out, _e, _c = self.run_cli(
            ["agent", "messages"], {"message_history": {"messages": []}}
        )
        self.assertIn("no messages", out)


class TeamTest(CliTestCase):
    def test_create(self) -> None:
        team = _team(members=[{"agent_id": "a1", "name": None, "kind": "claude",
                               "state": "ready"}], missing=["a2"])
        rc, out, _e, c = self.run_cli(
            ["agent", "team", "create", "core", "a1", "--workspace", "/w"],
            {"team_create": {"team": team}},
        )
        self.assertEqual(rc, 0)
        self.assertIn("missing", out)
        self.assertIn("a2", out)

    def test_up(self) -> None:
        resp = {"created": ["a1"], "existing": ["a2"], "team": _team()}
        with mock.patch("crewhall.specs.load_team_spec", return_value={"team": "core"}):
            rc, out, _e, _c = self.run_cli(
                ["agent", "team", "up", "/tmp/t.toml"], {"team_up": resp}
            )
        self.assertEqual(rc, 0)
        self.assertIn("+ created a1", out)

    def test_up_spec_error(self) -> None:
        with mock.patch("crewhall.specs.load_team_spec",
                        side_effect=specs.SpecError("bad spec")):
            rc, _o, err, _c = self.run_cli(["agent", "team", "up", "/tmp/t.toml"])
        self.assertEqual(rc, 2)
        self.assertIn("error: bad spec", err)

    def test_set_workspace(self) -> None:
        rc, _o, _e, c = self.run_cli(
            ["agent", "team", "set-workspace", "core", "/w", "--host", "box"],
            {"team_set_workspace": {"team": _team()}},
        )
        self.assertEqual(rc, 0)
        self.assertEqual(c.calls[0][1]["host"], "box")

    def test_list(self) -> None:
        teams = [_team(members=[{"agent_id": "a1", "name": "n"}], missing=["a2"],
                       host="box")]
        rc, out, _e, _c = self.run_cli(["agent", "team", "list"], {"team_list": {"teams": teams}})
        self.assertEqual(rc, 0)
        self.assertIn("core", out)
        rc, out, _e, _c = self.run_cli(
            ["agent", "team", "list", "--json"], {"team_list": {"teams": teams}}
        )
        self.assertEqual(json.loads(out), teams)
        rc, out, _e, _c = self.run_cli(["agent", "team", "list"], {"team_list": {"teams": []}})
        self.assertIn("no teams", out)

    def test_info_remove_members(self) -> None:
        rc, out, _e, _c = self.run_cli(
            ["agent", "team", "info", "core"], {"team_info": {"team": _team()}}
        )
        self.assertEqual(rc, 0)
        rc, out, _e, _c = self.run_cli(
            ["agent", "team", "info", "core", "--json"], {"team_info": {"team": _team()}}
        )
        self.assertEqual(json.loads(out)["team_id"], "team_1")
        rc, out, _e, _c = self.run_cli(
            ["agent", "team", "remove", "core"], {"team_remove": {"team": _team()}}
        )
        self.assertIn("removed team_1", out)
        rc, _o, _e, _c = self.run_cli(
            ["agent", "team", "add-member", "core", "a1"],
            {"team_add_member": {"team": _team()}},
        )
        self.assertEqual(rc, 0)
        rc, _o, _e, _c = self.run_cli(
            ["agent", "team", "remove-member", "core", "a1"],
            {"team_remove_member": {"team": _team()}},
        )
        self.assertEqual(rc, 0)


class DaemonTest(CliTestCase):
    def test_status_running(self) -> None:
        rc, out, _e, _c = self.run_cli(
            ["daemon", "status"], {"ping": {"pid": 9, "sessions": 3}}
        )
        self.assertEqual(rc, 0)
        self.assertIn("pid=9", out)

    def test_status_down(self) -> None:
        rc, out, _e, _c = self.run_cli(["daemon", "status"], {"ping": cli.RpcError("no")})
        self.assertEqual(rc, 1)
        self.assertIn("not running", out)

    def test_stop(self) -> None:
        rc, out, _e, _c = self.run_cli(["daemon", "stop", "--kill-agents"])
        self.assertEqual(rc, 0)
        self.assertIn("agents stopped too", out)
        rc, out, _e, _c = self.run_cli(["daemon", "stop"])
        self.assertIn("agents keep running", out)

    def test_stop_error(self) -> None:
        rc, _o, err, _c = self.run_cli(
            ["daemon", "stop"], {"shutdown": cli.RpcError("nope")}
        )
        self.assertEqual(rc, 1)
        self.assertIn("daemon: nope", err)

    def test_logs_missing(self) -> None:
        with mock.patch.object(cli.paths, "log_path", return_value="/no/such/log"):
            rc, out, _e, _c = self.run_cli(["daemon", "logs"])
        self.assertEqual(rc, 0)
        self.assertIn("no log", out)

    def test_logs_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("agent a1 did something\nother line\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        with mock.patch.object(cli.paths, "log_path", return_value=path):
            rc, out, _e, _c = self.run_cli(["daemon", "logs", "--agent", "a1"])
        self.assertEqual(rc, 0)
        self.assertIn("agent a1", out)
        self.assertNotIn("other line", out)

    def test_logs_follow(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write("line\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        with mock.patch.object(cli.paths, "log_path", return_value=path), \
                mock.patch.object(cli, "_follow_log", return_value=0) as follow:
            rc, _o, _e, _c = self.run_cli(["daemon", "logs", "--follow"])
        self.assertEqual(rc, 0)
        self.assertTrue(follow.called)

    def test_follow_log_keyboard_interrupt(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            path = fh.name
        self.addCleanup(os.unlink, path)
        with mock.patch.object(cli.time, "sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(cli._follow_log(path, None), 0)

    def test_run_foreground(self) -> None:
        server = mock.MagicMock()
        server.socket_path = "/tmp/sock"
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(cli, "Controller", return_value=mock.MagicMock()), \
                mock.patch.object(cli, "Server", return_value=server):
            rc, out, _e, _c = self.run_cli(["daemon", "run"])
        self.assertEqual(rc, 0)
        self.assertIn("crewhall daemon on", out)
        self.assertTrue(server.stop.called)


class WebTokenTest(CliTestCase):
    def test_generate(self) -> None:
        with mock.patch("crewhall.web.auth.token_exists", return_value=False), \
                mock.patch("crewhall.web.auth.generate_token", return_value="T"), \
                mock.patch("crewhall.web.auth.token_path", return_value="/p"):
            rc, out, _e, _c = self.run_cli(["web", "token", "generate"])
        self.assertEqual(rc, 0)
        self.assertIn("T", out)

    def test_generate_existing(self) -> None:
        with mock.patch("crewhall.web.auth.token_exists", return_value=True):
            rc, _o, err, _c = self.run_cli(["web", "token", "generate"])
        self.assertEqual(rc, 1)
        self.assertIn("already exists", err)

    def test_generate_rotate(self) -> None:
        with mock.patch("crewhall.web.auth.token_exists", return_value=True), \
                mock.patch("crewhall.web.auth.generate_token", return_value="T2"), \
                mock.patch("crewhall.web.auth.token_path", return_value="/p"):
            rc, out, _e, _c = self.run_cli(["web", "token", "generate", "--rotate"])
        self.assertEqual(rc, 0)
        self.assertIn("T2", out)

    def test_revoke(self) -> None:
        with mock.patch("crewhall.web.auth.revoke_token", return_value=True):
            rc, out, _e, _c = self.run_cli(["web", "token", "revoke"])
        self.assertIn("revoked", out)
        with mock.patch("crewhall.web.auth.revoke_token", return_value=False):
            rc, out, _e, _c = self.run_cli(["web", "token", "revoke"])
        self.assertIn("no token", out)

    def test_show(self) -> None:
        with mock.patch("crewhall.web.auth.load_token", return_value="S"):
            rc, out, _e, _c = self.run_cli(["web", "token", "show"])
        self.assertEqual(out, "S\n")
        with mock.patch("crewhall.web.auth.load_token", return_value=None):
            rc, _o, err, _c = self.run_cli(["web", "token", "show"])
        self.assertEqual(rc, 1)
        self.assertIn("no token configured", err)


class TerminalTokenTest(CliTestCase):
    def test_parse_ttl(self) -> None:
        self.assertIsNone(cli._parse_ttl(None))
        self.assertIsNone(cli._parse_ttl("0"))
        self.assertEqual(cli._parse_ttl("45s"), 45)
        self.assertEqual(cli._parse_ttl("30m"), 1800)
        self.assertEqual(cli._parse_ttl("8h"), 28800)
        self.assertEqual(cli._parse_ttl("2d"), 172800)
        self.assertEqual(cli._parse_ttl("100"), 100)

    def test_new(self) -> None:
        rec = {"id": "t1", "scopes": ["read"], "hosts": ["*"]}
        with mock.patch("crewhall.web.terminal_tokens.issue", return_value=("TOK", rec)), \
                mock.patch("crewhall.web.terminal_tokens.store_path", return_value="/p"):
            rc, out, _e, _c = self.run_cli(
                ["web", "terminal-token", "new", "--scope", "read", "--ttl", "1h"]
            )
        self.assertEqual(rc, 0)
        self.assertIn("TOK", out)

    def test_list(self) -> None:
        toks = [{"id": "t1", "expired": False, "scopes": ["read"], "hosts": ["*"],
                 "label": "x"}]
        with mock.patch("crewhall.web.terminal_tokens.list_tokens", return_value=toks):
            rc, out, _e, _c = self.run_cli(["web", "terminal-token", "list"])
        self.assertIn("t1", out)
        with mock.patch("crewhall.web.terminal_tokens.list_tokens", return_value=toks):
            rc, out, _e, _c = self.run_cli(["web", "terminal-token", "list", "--json"])
        self.assertEqual(json.loads(out), toks)
        with mock.patch("crewhall.web.terminal_tokens.list_tokens", return_value=[]):
            rc, out, _e, _c = self.run_cli(["web", "terminal-token", "list"])
        self.assertIn("no terminal tokens", out)

    def test_revoke(self) -> None:
        with mock.patch("crewhall.web.terminal_tokens.revoke", return_value=True):
            rc, out, _e, _c = self.run_cli(["web", "terminal-token", "revoke", "t1"])
        self.assertEqual(rc, 0)
        with mock.patch("crewhall.web.terminal_tokens.revoke", return_value=False):
            rc, _o, _e, _c = self.run_cli(["web", "terminal-token", "revoke", "t1"])
        self.assertEqual(rc, 1)


class WebStatusTest(CliTestCase):
    def test_status(self) -> None:
        with mock.patch("crewhall.web.auth.token_exists", return_value=True), \
                mock.patch("crewhall.web.auth.token_fingerprint", return_value="fp"), \
                mock.patch("crewhall.web.auth.file_permissions_ok", return_value=True), \
                mock.patch("crewhall.web.tailscale.available", return_value=True), \
                mock.patch("crewhall.web.tailscale.local_ipv4", return_value="100.0.0.1"), \
                mock.patch("crewhall.web.tailscale.dns_name", return_value="box.ts"):
            rc, out, _e, _c = self.run_cli(["web", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("token configured: yes", out)
        self.assertIn("100.0.0.1", out)

    def test_status_error(self) -> None:
        with mock.patch("crewhall.web.auth.token_exists", return_value=False), \
                mock.patch("crewhall.web.auth.token_fingerprint", return_value=None), \
                mock.patch("crewhall.web.auth.file_permissions_ok", return_value=False), \
                mock.patch("crewhall.web.tailscale.available", return_value=False), \
                mock.patch("crewhall.web.tailscale.local_ipv4",
                           side_effect=RuntimeError("no ts")):
            rc, out, _e, _c = self.run_cli(["web", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("tailscale ipv4: -", out)

    def test_web_run(self) -> None:
        with mock.patch("crewhall.web.server.main", return_value=0) as m:
            rc, _o, _e, _c = self.run_cli(
                ["web", "--tailscale", "--require-auth", "--allow-host", "x",
                 "--socket", "/s"]
            )
        self.assertEqual(rc, 0)
        self.assertTrue(m.called)


class ServiceTest(CliTestCase):
    def test_unit(self) -> None:
        with mock.patch("crewhall.service.unit_text", return_value="UNIT"):
            rc, out, _e, _c = self.run_cli(["service", "unit"])
        self.assertEqual(out, "UNIT")

    def test_install_uninstall(self) -> None:
        with mock.patch("crewhall.service.install",
                        return_value={"unit_path": "/u", "enable": {"ok": True}}):
            rc, out, _e, _c = self.run_cli(["service", "install"])
        self.assertIn("installed: /u", out)
        with mock.patch("crewhall.service.uninstall", return_value={"unit_path": "/u"}):
            rc, out, _e, _c = self.run_cli(["service", "uninstall"])
        self.assertIn("removed: /u", out)

    def test_install_enable_error(self) -> None:
        with mock.patch("crewhall.service.install",
                        return_value={"unit_path": "/u", "enable": {"error": "denied"}}):
            rc, out, _e, _c = self.run_cli(["service", "install"])
        self.assertIn("denied", out)

    def test_status(self) -> None:
        with mock.patch("crewhall.service.status",
                        return_value={"status": {"stdout": "active"}}):
            rc, out, _e, _c = self.run_cli(["service", "status"])
        self.assertIn("active", out)
        with mock.patch("crewhall.service.status",
                        return_value={"status": {"error": "inactive"}}):
            rc, out, _e, _c = self.run_cli(["service", "status"])
        self.assertIn("inactive", out)

    def test_web_variants(self) -> None:
        with mock.patch("crewhall.service.web_unit_text", return_value="WUNIT"):
            rc, out, _e, _c = self.run_cli(["service", "web-unit"])
        self.assertEqual(out, "WUNIT")
        with mock.patch("crewhall.service.install_web",
                        return_value={"unit_path": "/w", "enable": {"ok": True}}):
            rc, out, _e, _c = self.run_cli(["service", "install-web"])
        self.assertIn("installed: /w", out)
        with mock.patch("crewhall.service.uninstall_web", return_value={"unit_path": "/w"}):
            rc, out, _e, _c = self.run_cli(["service", "uninstall-web"])
        self.assertIn("removed: /w", out)


class ChatAndOfficeTest(CliTestCase):
    def test_chat_publish_requires_agent(self) -> None:
        rc, _o, err, _c = self.run_cli(["chat", "publish", "/tmp/f"])
        self.assertEqual(rc, 2)
        self.assertIn("not inside", err)

    def test_chat_publish_requires_conversation(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a"), \
                mock.patch.dict(os.environ, {"CREWHALL_CONVERSATION": ""}, clear=False):
            os.environ.pop("CREWHALL_CONVERSATION", None)
            rc, _o, err, _c = self.run_cli(["chat", "publish", "/tmp/f"])
        self.assertEqual(rc, 2)
        self.assertIn("--conversation", err)

    def test_chat_publish_ok(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a", CREWHALL_CONVERSATION="c1"):
            rc, out, _e, _c = self.run_cli(
                ["chat", "publish", "/tmp/f", "--name", "out.txt"],
                {"conversation_publish": {"artifact": {"relpath": "out.txt"}}},
            )
        self.assertEqual(rc, 0)
        self.assertIn("out.txt", out)

    def test_chat_publish_error(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a", CREWHALL_CONVERSATION="c1"):
            rc, _o, err, _c = self.run_cli(
                ["chat", "publish", "/tmp/f"],
                {"conversation_publish": cli.RpcError("nope")},
            )
        self.assertEqual(rc, 1)
        self.assertIn("error: nope", err)

    def test_office_rpc_actions(self) -> None:
        with self.env(CREWHALL_AGENT_ID="a", CREWHALL_CONVERSATION="c1"):
            for argv in (
                ["office", "list"],
                ["office", "open", "--path", "p.docx"],
                ["office", "read", "--path", "p.docx"],
                ["office", "insert", "--path", "p.docx", "--text", "hi"],
                ["office", "command", "--path", "p.docx", "--method", "M",
                 "--args", "[1]"],
                ["office", "close", "--path", "p.docx"],
                ["office", "users", "--path", "p.docx"],
                ["office", "chat", "--path", "p.docx"],
                ["office", "say", "--path", "p.docx", "--text", "hi"],
                ["office", "comments", "--path", "p.docx"],
                ["office", "comment", "--path", "p.docx", "--text", "hi"],
                ["office", "save", "--path", "p.docx"],
            ):
                rc, out, _e, _c = self.run_cli(argv, {"office_collab_list": {"ok": True, "x": 1}})
                self.assertEqual(rc, 0, argv)

    def test_office_command_bad_json(self) -> None:
        rc, _o, err, _c = self.run_cli(
            ["office", "command", "--path", "p.docx", "--method", "M", "--args", "nope"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("not JSON", err)

    def test_office_setup_and_lifecycle(self) -> None:
        with mock.patch("crewhall.office_setup.setup", return_value={"ok": True}):
            rc, out, _e, _c = self.run_cli(["office", "setup"])
        self.assertEqual(rc, 0)
        with mock.patch("crewhall.office_setup.setup",
                        side_effect=office_setup.SetupError("bad")):
            rc, _o, err, _c = self.run_cli(["office", "setup"])
        self.assertEqual(rc, 1)
        self.assertIn("error: bad", err)
        with mock.patch("crewhall.office_setup.status", return_value={"ok": True}):
            rc, out, _e, _c = self.run_cli(["office", "status"])
        self.assertEqual(rc, 0)
        with mock.patch("crewhall.office_setup.stop", return_value={"ok": True}):
            rc, _o, _e, _c = self.run_cli(["office", "stop"])
        self.assertEqual(rc, 0)
        with mock.patch("crewhall.office_setup.restart", return_value={"ok": True}):
            rc, _o, _e, _c = self.run_cli(["office", "restart"])
        self.assertEqual(rc, 0)
        with mock.patch("crewhall.office_setup.stop",
                        side_effect=office_setup.SetupError("nope")):
            rc, _o, _e, _c = self.run_cli(["office", "stop"])
        self.assertEqual(rc, 1)


class MiscTest(CliTestCase):
    def test_adapter(self) -> None:
        with mock.patch("crewhall.adapter_gen.create", return_value=["a.py"]):
            rc, out, _e, _c = self.run_cli(["adapter", "new", "codex"])
        self.assertEqual(rc, 0)
        self.assertIn("a.py", out)

    def test_adapter_error(self) -> None:
        with mock.patch("crewhall.adapter_gen.create", side_effect=ValueError("nope")):
            rc, _o, err, _c = self.run_cli(["adapter", "new", "codex"])
        self.assertEqual(rc, 1)
        self.assertIn("error: nope", err)

    def test_ui(self) -> None:
        with mock.patch("crewhall.ui.app.main", return_value=0) as m:
            rc, _o, _e, _c = self.run_cli(
                ["ui", "--local", "--cwd", "/tmp", "--socket", "/s", "--refresh", "0.5"]
            )
        self.assertEqual(rc, 0)
        self.assertTrue(m.called)

    def test_demo(self) -> None:
        class FakeDemoSession:
            def __init__(self, backend, spec):
                self.session_id = "s1"
                self.pid = 1

            def start(self):
                pass

            def write(self, text):
                pass

            def send_enter(self):
                pass

            def read_until(self, pattern, timeout=5):
                return "HELLO_FROM_42\n40 120"

            def resize(self, cols, rows):
                pass

            def close(self):
                pass

        with mock.patch.object(cli, "available", return_value=["pty"]), \
                mock.patch("crewhall.backends.get_backend", return_value=object()), \
                mock.patch.object(cli, "InteractiveSession", FakeDemoSession):
            rc, out, _e, _c = self.run_cli(["demo"])
        self.assertEqual(rc, 0)
        self.assertIn("captured: HELLO_FROM_42", out)

    def test_default_client(self) -> None:
        with mock.patch.object(cli, "Client") as client_cls:
            cli._client(__import__("argparse").Namespace())
        self.assertTrue(client_cls.called)

    def test_helpers(self) -> None:
        self.assertEqual(cli.argparse_cwd(None), None)
        self.assertEqual(cli.argparse_cwd(""), None)
        self.assertTrue(cli.argparse_cwd(".").startswith("/"))
        self.assertEqual(cli._human_time(None), "-")
        self.assertEqual(cli._human_time(1.5), "1.500")
        self.assertEqual(cli._human_bytes(500), "500 B")
        self.assertEqual(cli._human_bytes(2048), "2.0 KB")
        self.assertEqual(cli._human_bytes(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(cli._human_bytes(2 * 1024 ** 3), "2.0 GB")
        self.assertTrue(cli._log_matches("anything", None))
        self.assertTrue(cli._log_matches("agent a1", "a1"))
        self.assertFalse(cli._log_matches("other", "a1"))

    def test_main_rpc_error(self) -> None:
        rc, _o, err, _c = self.run_cli(["list"], {"list": cli.RpcError("down")})
        self.assertEqual(rc, 1)
        self.assertIn("error: down", err)

    def test_main_session_timeout(self) -> None:
        rc, _o, err, _c = self.run_cli(
            ["capture", "t"], {"capture": cli.SessionTimeout("t", 1.0, "partial")}
        )
        self.assertEqual(rc, 1)
        self.assertIn("timeout: session t", err)


if __name__ == "__main__":
    unittest.main()
