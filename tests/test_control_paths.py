"""End-to-end paths of the lead controls: daemon ops, a real forced restart,
the CLI status printer and the MCP tools."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from crewhall import AgentState, ClaudeCodeHarness, Controller, Status
from crewhall.daemon import Server
from crewhall.messaging import MessagingError
from crewhall.team import TeamError
from tests.support import FakeHarness


def _status_agent() -> dict:
    return {"agent_id": "agent_1", "name": "w", "kind": "opencode", "state": "ready",
            "backend": "pty", "pid": 9, "evidence": ""}


def _status_dict(**over) -> dict:
    st = {
        "name": "w", "agent_id": "agt_1", "kind": "opencode", "state": "ready",
        "state_age": 3.0, "alive": True, "backend": "pty", "host": None,
        "activity": {}, "interactions": [], "human_draft": False,
        "queue": {}, "blocked_by": [], "deliverable_now": True,
        "processes": {"available": True, "shells": [], "others": []},
        "subagents": [],
    }
    st.update(over)
    return st


class _FakeCliClient:
    """Records every ``call``; a value may be a response, an exception or a callable."""

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = dict(responses or {})

    def call(self, op: str, **params) -> dict:
        self.calls.append((op, params))
        resp = self.responses.get(op, {})
        if isinstance(resp, Exception):
            raise resp
        return resp(params) if callable(resp) else resp


class _KnownState(FakeHarness):
    _state = AgentState.READY

    def _detect(self, text: str) -> tuple[AgentState, str]:
        return self._state, f"fake {self._state.value}"


class _Starting(_KnownState):
    _state = AgentState.STARTING


class _Exited(_KnownState):
    _state = AgentState.EXITED


class _Errored(_KnownState):
    _state = AgentState.ERROR


class _Working(_KnownState):
    _state = AgentState.WORKING


class _ServerBase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-ctlpath-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.controller = Controller(adopt=False, persist=False)
        self.server = Server(self.controller, os.path.join(self.root, "r", "d.sock"))
        self.lead = self._agent("lead")
        self.worker = self._agent("worker")
        self.controller.create_team("crew", ["lead", "worker"], leads=["lead"])

    def _agent(self, name: str) -> FakeHarness:
        h = FakeHarness(name)
        self.controller.register_agent(h)
        return h

    def _as(self, who: FakeHarness, op: str, **params) -> dict:
        return self.server.dispatch({
            "op": op, "sender": who.agent_id,
            "token": self.controller._agent_token(who.agent_id), **params})


class AgentOpsThroughTheDaemon(_ServerBase):
    def test_status_from_a_teammate(self) -> None:
        resp = self._as(self.worker, "agent_status", target="lead")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["status"]["name"], "lead")

    def test_status_output_is_for_leads_only(self) -> None:
        resp = self._as(self.worker, "agent_status", target="lead", output=True)
        self.assertFalse(resp["ok"])
        self.assertIn("only a lead", resp["error"])

    def test_status_with_a_wrong_token_is_refused(self) -> None:
        resp = self.server.dispatch({"op": "agent_status", "sender": "worker",
                                     "token": "nope", "target": "lead"})
        self.assertFalse(resp["ok"])

    def test_operator_status(self) -> None:
        resp = self.server.dispatch({"op": "agent_status", "target": "worker"})
        self.assertTrue(resp["ok"], resp)

    def test_lead_interrupts_member_but_member_cannot_interrupt_lead(self) -> None:
        self.worker.interrupt = lambda: True
        ok = self._as(self.lead, "agent_interrupt", target="worker")
        self.assertTrue(ok["ok"], ok)
        self.assertTrue(ok["interrupted"])
        denied = self._as(self.worker, "agent_interrupt", target="lead")
        self.assertFalse(denied["ok"])

    def test_lead_signals_all_shells(self) -> None:
        seen = []
        with mock.patch.object(self.controller, "list_processes",
                               return_value={"shells": [{"pid": 7}], "others": []}), \
                mock.patch.object(self.controller, "signal_process",
                                  side_effect=lambda t, pid, sig, by="user":
                                  seen.append((pid, sig, by)) or {"pid": pid}):
            resp = self._as(self.lead, "agent_signal", target="worker", all_shells=True,
                            signal="KILL")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(seen, [(7, "KILL", self.lead.agent_id)])
        self.assertFalse(self._as(self.worker, "agent_signal", target="lead", pid=1)["ok"])

    def test_restart_from_a_lead_is_forced_and_by_the_lead(self) -> None:
        with mock.patch.object(self.controller, "force_restart_agent",
                               return_value={"state": "ready"}) as forced:
            resp = self._as(self.lead, "agent_restart", target="worker", reason="stuck")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(forced.call_args.kwargs["by"], self.lead.agent_id)
        self.assertEqual(forced.call_args.kwargs["reason"], "stuck")
        self.assertFalse(self._as(self.worker, "agent_restart", target="lead")["ok"])


class RealForcedRestart(unittest.TestCase):
    """A real process tree (pty + ``sleep``) torn down and relaunched."""

    def setUp(self) -> None:
        patcher = mock.patch.object(
            ClaudeCodeHarness, "command", classmethod(lambda cls: ["sleep"]))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="at-frestart-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.controller = Controller(adopt=False, persist=False)
        self.addCleanup(self.controller.shutdown)

    def test_tree_is_killed_and_identity_kept(self) -> None:
        harness = self.controller.create_agent(
            "claude", name="w", backend="pty", cwd=self.tmp, args="120")
        old_pid = harness.session.pid
        self.assertTrue(old_pid)
        result = self.controller.force_restart_agent(
            harness.agent_id, reason="test", timeout=0.3)
        self.assertEqual(result["agent_id"], harness.agent_id)
        self.assertTrue(result["fresh"])  # no tracked conversation to resume here
        self.assertEqual(result["terminated"]["remaining"], [])
        self.assertIn(old_pid, result["terminated"]["signalled"])
        revived = self.controller.get_agent(harness.agent_id)
        self.assertNotEqual(revived.session.pid, old_pid)
        time.sleep(0.2)
        revived.session.poll()
        self.assertTrue(revived.session.status.alive)
        with self.assertRaises(ProcessLookupError):
            os.kill(old_pid, 0)

    def test_lead_restarts_are_braked(self) -> None:
        harness = self.controller.create_agent(
            "claude", name="w", backend="pty", cwd=self.tmp, args="120")
        for _ in range(3):
            self.controller.force_restart_agent(harness.agent_id, by="lead-x", timeout=0.2)
        with self.assertRaisesRegex(MessagingError, "brake"):
            self.controller.force_restart_agent(harness.agent_id, by="lead-x", timeout=0.2)


class StatusPrinter(unittest.TestCase):
    def test_prints_every_section_without_draft_text(self) -> None:
        from crewhall.commands.agents import _print_status

        st = {
            "name": "w", "agent_id": "agt_1", "kind": "opencode", "state": "working",
            "state_age": 12.0, "alive": True, "backend": "tmux", "host": None,
            "activity": {"kind": "shell", "label": "Running command", "detail": "make",
                         "model": "m"},
            "interactions": [{"kind": "permission"}], "human_draft": True,
            "queue": {"count": 2, "oldest_age": 30.0},
            "blocked_by": ["working"], "deliverable_now": False,
            "processes": {"available": True, "shells": [
                {"pid": 5, "age": 3, "producing_output": True, "command": "sleep 9"}],
                "others": [{"pid": 6, "age": 1, "command": "node"}],
                "output": [{"pid": 5, "output": "tick"}]},
            "subagents": ["explore"],
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_status(st)
            _print_status({**st, "processes": {"available": False, "reason": "remote"},
                           "queue": {}, "blocked_by": [], "human_draft": False,
                           "interactions": [], "subagents": [], "activity": {},
                           "state_age": None})
        out = buf.getvalue()
        for needle in ("state=working", "queued messages: 2", "blocked_by=['working']",
                       "shell pid=5", "pid=6", "tick", "(text not shown)",
                       "unavailable (remote)", "active sub-agents: 1"):
            self.assertIn(needle, out)


class McpControlTools(unittest.TestCase):
    def _session(self, reply: dict):
        from crewhall.mcp import server

        session = server.Session()
        session.agent_id, session.token = "agt_lead", "tok"
        calls = []
        session.call = lambda op, **params: (calls.append((op, params)), reply)[1]
        return server, session, calls

    def test_tools_forward_identity_and_validate_arguments(self) -> None:
        server, session, calls = self._session(
            {"interrupted": True, "count": 1, "all_shells": True, "state": "ready",
             "resumed": True, "fresh": False, "status": {"name": "w"}})
        server.call_tool(session, "teammate_status", {"to": "w"})
        server.call_tool(session, "interrupt_teammate", {"to": "w"})
        server.call_tool(session, "stop_teammate_process", {"to": "w", "all_shells": True})
        server.call_tool(session, "restart_teammate", {"to": "w", "reason": "stuck"})
        ops = [op for op, _ in calls]
        self.assertEqual(ops, ["agent_status", "agent_interrupt", "agent_signal",
                               "agent_restart"])
        self.assertTrue(all(p["sender"] == "agt_lead" and p["token"] == "tok"
                            for _, p in calls))
        with self.assertRaises(server.McpError):
            server.call_tool(session, "stop_teammate_process", {"to": "w"})
        with self.assertRaises(server.McpError):
            server.call_tool(session, "stop_teammate_process", {"to": "w", "pid": "1"})
        with self.assertRaises(server.McpError):
            server.call_tool(session, "teammate_status", {"to": ""})


class OperatorRestartThroughTheDaemon(_ServerBase):
    """The human control plane's two restart shapes (legacy revive / forced)."""

    def _operator(self, op: str, **params) -> dict:
        return self.server.dispatch({
            "op": op, "_control_token": self.server._control_token, **params})

    def test_forced_restart_runs_as_the_user(self) -> None:
        with mock.patch.object(self.controller, "force_restart_agent",
                               return_value={"state": "ready"}) as forced:
            resp = self._operator("agent_restart", target="worker", forced=True,
                                  fresh=True, reason="stuck")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(forced.call_args.kwargs["by"], "user")
        self.assertTrue(forced.call_args.kwargs["fresh"])
        self.assertEqual(forced.call_args.kwargs["reason"], "stuck")

    def test_legacy_restart_revives_and_starts_the_agent(self) -> None:
        with mock.patch.object(self.controller, "restart_agent",
                               return_value=self.worker) as revived, \
                mock.patch.object(self.worker, "start",
                                  return_value=AgentState.READY) as started:
            resp = self._operator("agent_restart", target="worker")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["agent"]["name"], "worker")
        revived.assert_called_once_with("worker")
        started.assert_called_once()

    def test_legacy_restart_survives_a_readiness_timeout(self) -> None:
        with mock.patch.object(self.controller, "restart_agent",
                               return_value=self.worker), \
                mock.patch.object(self.worker, "start", side_effect=RuntimeError("slow")):
            resp = self._operator("agent_restart", target="worker")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["agent"]["agent_id"], self.worker.agent_id)


class CliControlCommands(unittest.TestCase):
    """The ``crewhall agent status|interrupt|signal|restart`` handlers end to end."""

    def _run(self, handler, args: argparse.Namespace, responses: dict | None = None,
             env: dict | None = None):
        client = _FakeCliClient(responses)
        out, err = io.StringIO(), io.StringIO()
        patches = [mock.patch("crewhall.commands.common._client", return_value=client)]
        if env:
            patches.append(mock.patch.dict(os.environ, env))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = handler(args)
        return rc, out.getvalue(), err.getvalue(), client

    def test_status_prints_json_and_text(self) -> None:
        from crewhall.commands.agents import cmd_agent_status

        rc, out, _e, client = self._run(
            cmd_agent_status, argparse.Namespace(target="w", output=True, json=True),
            {"agent_status": {"status": _status_dict()}})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["name"], "w")
        self.assertEqual(client.calls[0][1], {"target": "w", "output": True})

        st = _status_dict(state="working", deliverable_now=False, blocked_by=["working"])
        rc, out, _e, _c = self._run(
            cmd_agent_status, argparse.Namespace(target="w", output=False, json=False),
            {"agent_status": {"status": st}})
        self.assertEqual(rc, 0)
        self.assertIn("state=working", out)
        self.assertIn("blocked_by=['working']", out)

    def test_status_forwards_the_agent_identity_when_inside_one(self) -> None:
        from crewhall.commands.agents import cmd_agent_status

        rc, _o, _e, client = self._run(
            cmd_agent_status, argparse.Namespace(target="w", output=False, json=True),
            {"agent_status": {"status": _status_dict()}},
            env={"CREWHALL_AGENT_ID": "agt_sender", "CREWHALL_TOKEN": "tok"})
        self.assertEqual(rc, 0)
        self.assertEqual(client.calls[0][1]["sender"], "agt_sender")
        self.assertEqual(client.calls[0][1]["token"], "tok")

    def test_status_reports_an_rpc_error(self) -> None:
        from crewhall.client import RpcError
        from crewhall.commands.agents import cmd_agent_status

        rc, _o, err, _c = self._run(
            cmd_agent_status, argparse.Namespace(target="w", output=False, json=False),
            {"agent_status": RpcError("down")})
        self.assertEqual(rc, 1)
        self.assertIn("error: down", err)

    def test_interrupt_text_json_and_error(self) -> None:
        from crewhall.client import RpcError
        from crewhall.commands.agents import cmd_agent_interrupt

        rc, out, _e, _c = self._run(
            cmd_agent_interrupt, argparse.Namespace(target="w", json=False),
            {"agent_interrupt": {"name": "w", "agent_id": "a", "interrupted": True}})
        self.assertEqual(rc, 0)
        self.assertIn("interrupted: w", out)
        rc, out, _e, _c = self._run(
            cmd_agent_interrupt, argparse.Namespace(target="w", json=False),
            {"agent_interrupt": {"name": "w", "interrupted": False}})
        self.assertIn("no current turn: w", out)
        rc, out, _e, _c = self._run(
            cmd_agent_interrupt, argparse.Namespace(target="w", json=True),
            {"agent_interrupt": {"name": "w", "interrupted": True}})
        self.assertTrue(json.loads(out)["interrupted"])
        rc, _o, err, _c = self._run(
            cmd_agent_interrupt, argparse.Namespace(target="w", json=False),
            {"agent_interrupt": RpcError("denied")})
        self.assertEqual(rc, 1)
        self.assertIn("error: denied", err)

    def test_signal_requires_a_pid_or_all_shells(self) -> None:
        from crewhall.commands.agents import cmd_agent_signal

        rc, _o, err, client = self._run(
            cmd_agent_signal,
            argparse.Namespace(target="w", pid=None, all_shells=False,
                               signal="TERM", json=False))
        self.assertEqual(rc, 2)
        self.assertIn("--pid or --all-shells", err)
        self.assertEqual(client.calls, [])  # no RPC was attempted

    def test_signal_by_pid_and_all_shells(self) -> None:
        from crewhall.client import RpcError
        from crewhall.commands.agents import cmd_agent_signal

        rc, out, _e, client = self._run(
            cmd_agent_signal,
            argparse.Namespace(target="w", pid=7, all_shells=False,
                               signal="kill", json=False),
            {"agent_signal": {"count": 1}},
            env={"CREWHALL_AGENT_ID": "agt_sender", "CREWHALL_TOKEN": "tok"})
        self.assertEqual(rc, 0)
        self.assertIn("signalled 1 process(es) with KILL", out)
        self.assertEqual(client.calls[0][1]["pid"], 7)
        self.assertEqual(client.calls[0][1]["sender"], "agt_sender")
        self.assertEqual(client.calls[0][1]["token"], "tok")
        rc, out, _e, _c = self._run(
            cmd_agent_signal,
            argparse.Namespace(target="w", pid=None, all_shells=True,
                               signal="TERM", json=True),
            {"agent_signal": {"count": 2, "all_shells": True}})
        self.assertEqual(json.loads(out)["count"], 2)
        rc, _o, err, _c = self._run(
            cmd_agent_signal,
            argparse.Namespace(target="w", pid=1, all_shells=False,
                               signal="TERM", json=False),
            {"agent_signal": RpcError("bad")})
        self.assertEqual(rc, 1)
        self.assertIn("error: bad", err)

    def test_restart_resumed_fresh_json_legacy_and_error(self) -> None:
        from crewhall.client import RpcError
        from crewhall.commands.agents import cmd_agent_restart

        resumed = {"name": "w", "state": "ready", "resumed": True, "duration": 1.5,
                   "terminated": {"signalled": [1, 2], "remaining": []}, "reason": "stuck"}
        rc, out, _e, client = self._run(
            cmd_agent_restart,
            argparse.Namespace(target="w", fresh=False, reason="stuck", json=False),
            {"agent_restart": resumed})
        self.assertEqual(rc, 0)
        self.assertIn("restarted w: state=ready (resumed)", out)
        self.assertIn("reason: stuck", out)
        self.assertTrue(client.calls[0][1]["forced"])

        fresh = {"name": "w", "state": "ready", "resumed": False, "duration": 0.2,
                 "terminated": {"signalled": [], "remaining": []}}
        rc, out, _e, _c = self._run(
            cmd_agent_restart,
            argparse.Namespace(target="w", fresh=True, reason=None, json=False),
            {"agent_restart": fresh})
        self.assertIn("(fresh)", out)

        rc, out, _e, _c = self._run(
            cmd_agent_restart,
            argparse.Namespace(target="w", fresh=False, reason=None, json=True),
            {"agent_restart": resumed})
        self.assertEqual(json.loads(out)["state"], "ready")

        rc, out, _e, _c = self._run(
            cmd_agent_restart,
            argparse.Namespace(target="w", fresh=False, reason=None, json=False),
            {"agent_restart": {"agent": _status_agent()}})
        self.assertIn("agent_1", out)

        rc, _o, err, _c = self._run(
            cmd_agent_restart,
            argparse.Namespace(target="w", fresh=False, reason=None, json=False),
            {"agent_restart": RpcError("nope")})
        self.assertEqual(rc, 1)
        self.assertIn("error: nope", err)


class StatusBranches(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False, persist=False)

    def _agent(self, name: str, cls=FakeHarness) -> FakeHarness:
        h = cls(name)
        self.controller.register_agent(h)
        return h

    def test_starting_exited_and_error_states_are_blocked(self) -> None:
        for cls, reason in [(_Starting, "starting"), (_Exited, "exited"), (_Errored, "error")]:
            h = self._agent(cls.__name__, cls)
            st = self.controller.agent_status(h.agent_id)
            self.assertIn(reason, st["blocked_by"])
            self.assertFalse(st["deliverable_now"])

    def test_an_unreadable_input_is_not_a_draft(self) -> None:
        h = self._agent("a")
        h.input_is_clean = mock.Mock(side_effect=RuntimeError("unreadable"))
        self.assertFalse(self.controller.agent_status(h.agent_id)["human_draft"])

    def test_state_since_uses_the_last_hook_event(self) -> None:
        ready = self._agent("r")
        self.controller._hook_events[ready.agent_id] = {"event": "stop", "at": 123.0}
        self.assertEqual(self.controller.agent_status(ready.agent_id)["state_since"], 123.0)
        working = self._agent("w", _Working)
        self.controller._hook_events[working.agent_id] = {"event": "prompt_submit",
                                                          "at": 200.0}
        self.assertEqual(self.controller.agent_status(working.agent_id)["state_since"], 200.0)
        plain = self._agent("p")  # no matching hook: session creation is the fallback
        self.assertEqual(self.controller.agent_status(plain.agent_id)["state_since"], 1000.0)

    def test_state_since_is_none_when_the_session_has_no_creation_time(self) -> None:
        h = self._agent("a")
        h.session.created_at = None
        self.assertIsNone(self.controller.agent_status(h.agent_id)["state_since"])

    def test_remote_and_dead_agents_report_no_processes(self) -> None:
        h = self._agent("a")
        self.controller._agent_hosts[h.agent_id] = "box"
        h.session.backend.meta = mock.Mock(return_value={})
        st = self.controller.agent_status(h.agent_id)
        self.assertFalse(st["processes"]["available"])
        self.assertIn("remote", st["processes"]["reason"])

        del self.controller._agent_hosts[h.agent_id]
        h.session.status = Status.TERMINATED
        self.controller.list_processes = mock.Mock(side_effect=RuntimeError("boom"))
        st = self.controller.agent_status(h.agent_id)
        self.assertEqual(st["processes"]["reason"], "the agent is not running")

        h.session.status = Status.RUNNING
        st = self.controller.agent_status(h.agent_id)
        self.assertEqual(st["processes"]["reason"], "boom")

    def test_lead_process_output_is_best_effort(self) -> None:
        h = self._agent("a")
        listing = {
            "shells": [
                {"pid": 1, "command": "make", "elapsed": 5, "last_output_ago": 2,
                 "output": {"kind": "file", "task": "t"}},
                {"pid": 2, "command": "node", "elapsed": 1,
                 "output": {"kind": "opencode", "call": "c"}},
                {"pid": 3, "command": "x", "elapsed": 1, "output": {"kind": "other"}},
            ],
            "others": [{"pid": 4, "command": "daemon", "elapsed": 9}],
        }
        self.controller.list_processes = mock.Mock(return_value=listing)
        self.controller.process_output = mock.Mock(
            side_effect=[{"output": "tick"}, RuntimeError("no output")])
        procs = self.controller.agent_status(h.agent_id, include_output=True)["processes"]
        self.assertEqual(len(procs["shells"]), 3)
        self.assertTrue(procs["shells"][0]["producing_output"])
        self.assertFalse(procs["shells"][1]["producing_output"])
        self.assertEqual(procs["others"], [{"pid": 4, "command": "daemon", "age": 9}])
        # The file shell produced output; the opencode one failed and was dropped.
        self.assertEqual(procs["output"], [{"pid": 1, "task": "t", "call": None,
                                            "output": "tick"}])


class ControlBranches(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False, persist=False)

    def _agent(self, name: str) -> FakeHarness:
        h = FakeHarness(name)
        self.controller.register_agent(h)
        return h

    def test_interrupt_by_the_user_skips_the_notice(self) -> None:
        h = self._agent("a")
        h.interrupt = lambda: True
        out = self.controller.interrupt_agent(h.agent_id, by="user")
        self.assertTrue(out["interrupted"])
        self.assertEqual(out["name"], "a")

    def test_signal_by_pid_targets_only_that_process(self) -> None:
        h = self._agent("a")
        seen = []
        self.controller.signal_process = (
            lambda target, pid, sig, by="user": seen.append((pid, sig, by)) or {"pid": pid})
        out = self.controller.signal_agent(h.agent_id, "TERM", pid=99)
        self.assertEqual(out["count"], 1)
        self.assertEqual(seen, [(99, "TERM", "user")])

    def test_capture_tree_skips_bad_pids_and_remote_hosts(self) -> None:
        h = self._agent("a")
        h.session.pid = "not-a-pid"
        self.assertEqual(self.controller._capture_tree(h), [])
        h.session.pid = 0
        self.assertEqual(self.controller._capture_tree(h), [])
        h.session.pid = 4242
        self.controller._agent_hosts[h.agent_id] = "box"
        self.assertEqual(self.controller._capture_tree(h), [])

    def test_terminate_tree_is_best_effort_on_a_broken_backend(self) -> None:
        h = self._agent("a")
        h.session.backend.close = mock.Mock(side_effect=RuntimeError("closed"))
        out = self.controller._terminate_tree(h, [])
        self.assertEqual(out, {"signalled": [], "killed": [], "remaining": []})

    def test_terminate_tree_tolerates_a_dead_pid(self) -> None:
        h = self._agent("a")
        out = self.controller._terminate_tree(h, [999999999])
        self.assertEqual(out["signalled"], [])
        self.assertEqual(out["remaining"], [])


class TeamLeadPaths(unittest.TestCase):
    """``team_up`` assigns leads from a spec; the lead lookups used by the ops."""

    def setUp(self) -> None:
        self.controller = Controller(adopt=False, persist=False)

    def _agent(self, name: str) -> FakeHarness:
        h = FakeHarness(name)
        self.controller.register_agent(h)
        return h

    def test_team_up_assigns_leads_and_is_idempotent(self) -> None:
        lead = self._agent("lead")
        self._agent("worker")
        spec = {"name": "crew",
                "agents": [{"name": "lead"}, {"name": "worker"}],
                "leads": ["lead"]}
        out = self.controller.team_up(spec)
        self.assertTrue(out["team_created"])
        self.assertEqual(sorted(out["existing"]), ["lead", "worker"])
        self.assertEqual(out["team"]["leads"], [lead.agent_id])
        again = self.controller.team_up(spec)
        self.assertFalse(again["team_created"])
        self.assertEqual(again["team"]["leads"], [lead.agent_id])

    def test_team_up_rejects_an_unknown_lead(self) -> None:
        self._agent("lead")
        with self.assertRaises(TeamError):
            self.controller.team_up(
                {"name": "crew", "agents": [{"name": "lead"}], "leads": ["ghost"]})

    def test_team_up_tolerates_a_repeated_agent_entry(self) -> None:
        self._agent("lead")
        out = self.controller.team_up(
            {"name": "crew", "agents": [{"name": "lead"}, {"name": "lead"}]})
        # The second add is "already a member" and is swallowed as existing.
        self.assertEqual(out["existing"], ["lead", "lead"])
        self.assertEqual(out["created"], [])

    def test_team_up_propagates_a_real_membership_error(self) -> None:
        self._agent("lead")
        with mock.patch.object(self.controller, "add_team_member",
                               side_effect=ValueError("wrong host")):
            with self.assertRaisesRegex(ValueError, "wrong host"):
                self.controller.team_up(
                    {"name": "crew", "agents": [{"name": "lead"}]})

    def test_team_up_wraps_a_set_leads_failure(self) -> None:
        self._agent("lead")
        with mock.patch.object(self.controller.teams, "set_leads",
                               side_effect=RuntimeError("nope")):
            with self.assertRaisesRegex(TeamError, "team leads"):
                self.controller.team_up(
                    {"name": "crew", "agents": [{"name": "lead"}], "leads": ["lead"]})

    def test_live_team_defs_and_lead_lookups(self) -> None:
        a = self._agent("a")
        self._agent("b")
        team = self.controller.create_team("crew", ["a", "b"], leads=["a"])
        defs = self.controller.live_team_defs()
        self.assertEqual(defs[0]["name"], "crew")
        self.assertEqual(sorted(x["name"] for x in defs[0]["agents"]), ["a", "b"])
        self.assertEqual(self.controller._team_members("crew"), {a.agent_id,
                                                                team.agent_ids[1]})
        self.assertEqual(self.controller._team_members(None), set())
        self.assertEqual(self.controller._team_members("ghost"), set())
        self.assertIsNone(self.controller._team_or_none(None))
        self.assertIsNone(self.controller._team_or_none("ghost"))
        self.assertEqual(self.controller._team_or_none(team.team_id).team_id,
                         team.team_id)

    def test_identity_reports_a_missing_teammate(self) -> None:
        a = self._agent("a")
        b = self._agent("b")
        self.controller.create_team("crew", ["a", "b"])
        self.controller.agents.remove(b.agent_id)
        token = self.controller._agent_token(a.agent_id)
        info = self.controller.agent_identity(a.agent_id, token)
        self.assertEqual(info["teammates"],
                         [{"agent_id": b.agent_id, "name": b.agent_id,
                           "kind": "?", "state": "missing"}])
        self.assertEqual(self.controller.teammates(a.agent_id), info["teammates"])


class TeamModelEdges(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False, persist=False)

    def _agent(self, name: str) -> FakeHarness:
        h = FakeHarness(name)
        self.controller.register_agent(h)
        return h

    def test_workspace_mode_is_validated(self) -> None:
        self._agent("a")
        with self.assertRaises(TeamError):
            self.controller.teams.create("t", ["a"], workspace_mode="weird")

    def test_duplicate_team_id_is_rejected(self) -> None:
        self._agent("a")
        self.controller.teams.create("one", ["a"], team_id="team_fixed")
        with self.assertRaises(TeamError):
            self.controller.teams.create("two", ["a"], team_id="team_fixed")

    def test_set_leads_by_literal_member_id_after_the_agent_is_gone(self) -> None:
        a = self._agent("a")
        self._agent("b")
        team = self.controller.teams.create("t", ["a", "b"])
        self.controller.agents.remove(a.agent_id)
        updated = self.controller.teams.set_leads(team.team_id, [a.agent_id])
        self.assertEqual(updated.leads, (a.agent_id,))

    def test_set_leads_with_an_unresolvable_target_fails(self) -> None:
        self._agent("a")
        team = self.controller.teams.create("t", ["a"])
        with self.assertRaises(TeamError):
            self.controller.teams.set_leads(team.team_id, ["ghost"])

    def test_removing_an_unknown_id_fails(self) -> None:
        self._agent("a")
        team = self.controller.teams.create("t", ["a"])
        with self.assertRaises(TeamError):
            self.controller.teams.remove_member(team.team_id, "ghost")

    def test_an_inaccessible_workspace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("os.access", return_value=False):
                with self.assertRaises(TeamError):
                    self.controller.teams.create("t", [], workspace=tmp)

    def test_agent_host_of_an_unknown_agent_is_none(self) -> None:
        self.assertIsNone(self.controller.teams._agent_host("ghost"))

    def test_workspace_for_agent_picks_the_first_team_with_one(self) -> None:
        from crewhall.team import Team, workspace_for_agent

        no_ws = Team("t1", "one", ("sess_a",), 1.0)
        with_ws = Team("t2", "two", ("sess_a",), 2.0, workspace="/work")
        self.assertEqual(workspace_for_agent([no_ws, with_ws], "sess_a"), "/work")
        self.assertIsNone(workspace_for_agent([no_ws], "sess_a"))
        self.assertIsNone(workspace_for_agent([with_ws], "other"))


if __name__ == "__main__":
    unittest.main()
