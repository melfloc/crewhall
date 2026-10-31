"""Stage 2: the round-1 and round-2 security reproducers, inverted.

Each test is the inverse of a finding in ``REVISION_SEGURIDAD_agent_spawn*.md``
or exercises the Stage-2 additions (mandatory sandbox for spawn, N2–N7).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from crewhall import audit, doctor, sandbox, settings, spawn_policy
from crewhall.daemon import _proc_starttime
from crewhall.messaging import MessagingError
from crewhall.types import Status

from tests.test_agent_spawn_policy import BACKEND_ARGS, SKIP, AgentSpawnPolicy, policy


class SecInverted(AgentSpawnPolicy):
    """Round-1 reproducer set (``scratchpad/sec/test_sec_spawn.py``), inverted."""

    def use_profiles(self, **body) -> str:
        self._settings(policy(self.cwd, **body))
        return self.cwd

    # [1] a token taken from another agent's tree never authorises a spawn.
    def test_sec_1_token_from_another_agents_tree_is_denied(self) -> None:
        self.use_profiles()
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        token = self.controller._agent_token(self.ids["Heraldo"])
        req = {"op": "agent_create", "sender": "Heraldo", "token": token,
               "kind": "claude", "name": "Ejecutor_Backend_Sonnet_3",
               "team": "Conciliare", "args": BACKEND_ARGS}
        denied = self.server._guard_origin(dict(req), other.pid)
        self.assertIn("not in Heraldo", denied["error"])
        self.assertIn("no peer pid", self.server._guard_origin(dict(req), None)["error"])

    # [1b] the control config is masked inside an agent (Stage 1 deny targets).
    def test_sec_1b_agent_config_is_in_the_deny_targets(self) -> None:
        from crewhall import hooks, paths

        targets = hooks.claude_deny_targets(
            paths.state_dir(), paths.runtime_dir(), settings.config_dir())
        self.assertTrue(any(settings.config_dir() in t for t in targets))

    # [2] an agent-created agent can never be a spawner (Y1).
    def test_sec_2_spawned_agent_cannot_spawn(self) -> None:
        data = policy(self.cwd)
        data["agent_spawners"] = ["Heraldo", "Auditor"]
        self._settings(data)
        # A spawner name is protected: Heraldo cannot mint a second Auditor.
        self.assertDenied(self.create("Auditor", args=["--agent", "auditor", SKIP]),
                          "protected")

    # [3] max_live counts live agents and reviving over it is refused (R3).
    def test_sec_3_revive_over_max_live_is_refused(self) -> None:
        self.use_profiles(max_live=2)
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_2").get("ok"))
        agent = self.controller.get_agent("Ejecutor_Backend_Sonnet_1")
        agent.session.status = Status.EXITED          # exited gives its slot back
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_3").get("ok"))
        with self.assertRaises(MessagingError) as cm:  # reviving would exceed 2
            with spawn_policy.revive_guard(self.controller, agent):
                pass
        self.assertIn("max_live (2)", str(cm.exception))

    # [4] the final decision is audited once, under the lock (Y6).
    def test_sec_4_final_decision_is_audited_by_the_agent(self) -> None:
        self.use_profiles()
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        rows = [r for r in self.audit_rows() if r["op"] == "agent_spawn_policy"]
        self.assertEqual([r["decision"] for r in rows], ["allowed"])
        self.assertEqual(rows[0]["actor"], "Heraldo")

    # [5] a forged request cannot write an unbounded audit line or flush it.
    def test_sec_5_audit_stays_bounded_and_rate_limited(self) -> None:
        big = "A" * 4_000_000
        req = {"op": "agent_create", "sender": big, "token": "x", "name": big}
        for _ in range(50):
            self.server._guard_origin(dict(req), None)
        size = os.path.getsize(audit.audit_path())
        lines = open(audit.audit_path(), encoding="utf-8").read().splitlines()
        self.assertLessEqual(len(lines), 2, lines)
        self.assertLess(size, 10_000)
        for line in lines:
            self.assertLessEqual(len(line.encode()), audit.MAX_RECORD)
        audit.record("agent_stop", actor="GENUINE-MARKER", summary="real event")
        for _ in range(50):
            self.server._guard_origin(dict(req), None)
        self.assertIn("GENUINE-MARKER", open(audit.audit_path(), encoding="utf-8").read())

    # [6] the legacy request-chosen launch is dead (R4).
    def test_sec_6_legacy_policy_is_invalid(self) -> None:
        self._settings({"agent_spawners": ["Heraldo"],
                        "sandbox": {"profiles": {"role": {"binds": [], "env": {},
                                                          "network": "host", "deny": [],
                                                          "workspace": "none"}},
                                    "agents": [{"name_pattern": "Heraldo", "profile": "role"}]},
                        "agent_spawn_policy": {"name_patterns": ["Ejecutor_.*"],
                                               "teams": ["Conciliare"]}})
        self.assertIn("unknown keys", spawn_policy.config()["invalid"])
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"), "policy invalid")

    # [7] the request cannot choose the backend (Y2).
    def test_sec_7_profile_backend_is_pinned(self) -> None:
        self.use_profiles()
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1", backend="pty"),
                          "args/cwd/backend")

    # [8]/[9] read-once: an agent editing settings.json changes nothing.
    def test_sec_8_and_9_live_reload_cannot_grant_a_spawner(self) -> None:
        self.use_profiles()
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1", sender="Ejecutor_Sonnet_1"),
                          "not in agent_spawners")
        data = json.load(open(settings.path()))
        data["agent_spawners"].append("Ejecutor_Sonnet_1")
        data["sandbox"]["agents"].append(
            {"name_pattern": "Ejecutor_Sonnet_1", "profile": "role"})
        self._settings(data, restart=False)
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1", sender="Ejecutor_Sonnet_1"),
                          "not in agent_spawners")

    # [10] an agent-initiated stop never forwards force.
    def test_sec_10_stop_ignores_force(self) -> None:
        self.use_profiles()
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        seen = {}
        real = self.controller.remove_agent

        def spy(target, force=False):
            seen["force"] = force
            return real(target, force=force)

        self.controller.remove_agent = spy
        self.assertTrue(self.stop("Ejecutor_Backend_Sonnet_1", force=True).get("ok"))
        self.assertFalse(seen["force"])


class Stage2Policy(AgentSpawnPolicy):
    def use_profiles(self, **body) -> str:
        self._settings(policy(self.cwd, **body))
        return self.cwd

    def test_spawn_profile_must_name_a_sandbox_profile(self) -> None:
        data = policy(self.cwd)
        del data["agent_spawn_policy"]["profiles"][0]["sandbox"]
        self._settings(data)
        self.assertIn("sandbox", settings.agent_spawn()["invalid"])
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"), "policy invalid")

    def test_unknown_sandbox_profile_is_fail_closed(self) -> None:
        data = policy(self.cwd)
        for profile in data["agent_spawn_policy"]["profiles"]:
            profile["sandbox"] = "missing"
        self._settings(data)
        self.assertIn("unknown sandbox profile", spawn_policy.load()["invalid"])
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"), "policy invalid")

    def test_unsandboxed_spawner_refuses_to_enable_the_policy(self) -> None:
        data = policy(self.cwd)
        data["sandbox"]["agents"] = [
            a for a in data["sandbox"]["agents"]
            if a["name_pattern"] != "Heraldo"]
        self._settings(data)
        self.assertIn("not sandboxed", spawn_policy.load()["invalid"])
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"), "policy invalid")

    def test_spawn_created_agent_gets_the_profile_sandbox(self) -> None:
        self.use_profiles()
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.assertEqual(self.launched[-1]["sandbox_profile"], "role")

    def test_request_cannot_choose_the_sandbox_profile(self) -> None:
        self.use_profiles()
        launch = spawn_policy.launch_values({
            "name": "Ejecutor_Backend_Sonnet_1", "kind": "claude",
            "args": BACKEND_ARGS, "sandbox": "heraldo",
        })
        self.assertEqual(launch["sandbox"], "role")

    def test_name_whose_sandbox_mapping_differs_is_denied(self) -> None:
        data = policy(self.cwd)
        data["sandbox"]["agents"].insert(
            0, {"name_pattern": "Ejecutor_Backend_Sonnet_1", "profile": "heraldo"})
        self._settings(data)
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"),
                          "resolves to sandbox profile")


class PeerIdentity(AgentSpawnPolicy):
    """N2: a connection is bound to the process that opened it, not to its pid."""

    def test_n2_stale_or_reused_pid_is_denied(self) -> None:
        self._settings(policy(self.cwd))
        caller = self.controller.get_agent("Heraldo")
        start = _proc_starttime(self.agent_pid)
        self.assertIsNotNone(start)
        req = {"op": "agent_create", "sender": "Heraldo",
               "token": self.controller._agent_token(self.ids["Heraldo"]),
               "kind": "claude", "name": "Ejecutor_Backend_Sonnet_1",
               "team": "Conciliare", "args": BACKEND_ARGS}
        self.assertIsNone(self.server._guard_origin(dict(req), (self.agent_pid, start)))
        reused = self.server._guard_origin(dict(req), (self.agent_pid, start + 1))
        self.assertIn("pid was reused", reused["error"])
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        pid, dead_start = gone.pid, _proc_starttime(gone.pid)
        gone.wait()
        self.assertIn("gone or its pid was reused",
                      self.server._peer_binding_error(caller, (pid, dead_start)))


class Adoption(AgentSpawnPolicy):
    """N3: a dead pane respawned by a same-uid process is never adopted."""

    def _persist_agent(self, agent_id: str, name: str, owner: str, status: str) -> None:
        data = self.store.load()
        data.setdefault("agents", []).append({
            "agent_id": agent_id, "name": name, "kind": "claude", "backend": "tmux",
            "cwd": self.cwd, "cols": 120, "rows": 40, "args": [], "host": None,
            "created_by": owner, "status": status,
        })
        self.store.save(data)

    def _adopt(self, agent_id: str, name: str) -> tuple[object, dict]:
        from crewhall.session import InteractiveSession
        from tests.support import FakeBackend

        class Dead:
            alive = True

        spec = SessionSpecFactory(name)
        session = InteractiveSession.__new__(InteractiveSession)
        session.session_id = agent_id
        session.spec = spec
        session._adopted = True
        session._status = Status.RUNNING
        session.backend = FakeBackend()
        session._lock = __import__("threading").RLock()
        session._cv = __import__("threading").Condition(session._lock)
        session._buffer = __import__("collections").deque()
        session._buffer_bytes = 0
        session._buffer_limit = 1000
        session._events = __import__("collections").deque(maxlen=10)
        session.name = name
        killed = {"count": 0}

        def terminate():
            killed["count"] += 1
            session._status = Status.TERMINATED

        session.terminate = terminate
        self.controller.registry.add(session)
        return session, killed

    def test_n3_exited_agent_respawn_is_not_adopted(self) -> None:
        self._settings(policy(self.cwd, max_live=2))
        owner = self.ids["Heraldo"]
        self._persist_agent("sess_owned", "Ejecutor_Backend_Sonnet_1", owner, "exited")
        session, killed = self._adopt("sess_owned", "Ejecutor_Backend_Sonnet_1")
        self.controller.restore()
        self.assertEqual(killed["count"], 1, "respawned pane was adopted")
        self.assertNotEqual(self.controller.get_agent("Ejecutor_Backend_Sonnet_1")
                            .session.status, Status.RUNNING)

    def test_n3_running_agent_over_the_cap_is_not_adopted(self) -> None:
        self._settings(policy(self.cwd, max_live=1))
        owner = self.ids["Heraldo"]
        for n in (1, 2):
            self._persist_agent(f"sess_owned_{n}", f"Ejecutor_Backend_Sonnet_{n}",
                                owner, "running")
        first, _ = self._adopt("sess_owned_1", "Ejecutor_Backend_Sonnet_1")
        second, killed = self._adopt("sess_owned_2", "Ejecutor_Backend_Sonnet_2")
        self.controller.restore()
        self.assertEqual(killed["count"], 1, "over-cap agent was adopted")


class Yellow(AgentSpawnPolicy):
    """N4-N7 from round 2."""

    def use_profiles(self, **body) -> str:
        self._settings(policy(self.cwd, **body))
        return self.cwd

    # N4: with the policy off or invalid the human keeps restart control.
    def test_n4_human_revive_is_not_blocked_by_an_off_or_invalid_policy(self) -> None:
        self.use_profiles(max_live=1)
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        agent = self.controller.get_agent("Ejecutor_Backend_Sonnet_1")
        agent.session.status = Status.EXITED
        for data in ({"agent_spawners": []},
                     {"agent_spawners": ["Heraldo"], "agent_spawn_polcy": {}}):
            self._settings(data)
            with spawn_policy.revive_guard(self.controller, agent):
                pass  # must not raise

    # N5: doctor asks the daemon's memory, never the audit log.
    def test_n5_a_forged_audit_line_does_not_reset_drift(self) -> None:
        self.server._load_spawn_policy()
        data = json.load(open(settings.path()))
        data["agent_spawners"].append("Ejecutor_Sonnet_1")
        data["sandbox"]["agents"].append(
            {"name_pattern": "Ejecutor_Sonnet_1", "profile": "role"})
        self._settings(data, restart=False)
        from crewhall.client import Client

        with mock.patch.object(Client, "call",
                               return_value=self.server._op_spawn_policy_status({})), \
                mock.patch("crewhall.client.ping", return_value=True):
            self.assertIn("warn", doctor.check_spawn_policy()["status"])
            forged = {"ts": 0, "actor": "daemon", "op": "agent_spawn_policy_loaded",
                      "result": "ok",
                      "sha256": spawn_policy.digest(settings.spawn_policy_raw())}
            with open(audit.audit_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(forged) + "\n")
            self.assertIn("warn", doctor.check_spawn_policy()["status"])

    def test_n5b_daemon_refusal_is_reported_not_hidden(self) -> None:
        data = policy(self.cwd)
        data["sandbox"]["agents"] = [
            a for a in data["sandbox"]["agents"] if a["name_pattern"] != "Heraldo"]
        self._settings(data)
        self.server._load_spawn_policy()
        from crewhall.client import Client

        with mock.patch.object(Client, "call",
                               return_value=self.server._op_spawn_policy_status({})), \
                mock.patch("crewhall.client.ping", return_value=True):
            check = doctor.check_spawn_policy()
        self.assertEqual(check["status"], "fail")
        self.assertIn("daemon refused", check["detail"])

    def test_n7_agent_restart_timeout_is_clamped(self) -> None:
        from crewhall.ops.agents import SPAWN_TIMEOUT_MAX, _spawn_timeout

        self.assertEqual(_spawn_timeout({"timeout": 1e9}), SPAWN_TIMEOUT_MAX)
        self.assertEqual(_spawn_timeout({"timeout": -5}), 0.0)
        self.assertEqual(_spawn_timeout({"timeout": "nan"}), SPAWN_TIMEOUT_MAX)

    # N6: denied unauthenticated entries are rate-limited and cannot rotate.
    def test_n6_unauth_flood_cannot_flush_the_log(self) -> None:
        audit.record("agent_stop", actor="GENUINE", summary="keep me")
        req = {"op": "agent_create", "sender": "x" * 5000, "token": "t", "name": "n" * 5000}
        start = time.monotonic()
        for _ in range(200):
            self.server._guard_origin(dict(req), None)
        lines = open(audit.audit_path(), encoding="utf-8").read().splitlines()
        self.assertIn("GENUINE", "\n".join(lines))
        self.assertLessEqual(len(lines), 2)
        self.assertLess(os.path.getsize(audit.audit_path()), 10_000)
        self.assertLess(time.monotonic() - start, 2.0)

    # N7: the policy lock is free while an agent starts.
    def test_n7_revive_does_not_hold_the_lock_during_start(self) -> None:
        self.use_profiles(max_live=2)
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        agent = self.controller.get_agent("Ejecutor_Backend_Sonnet_1")
        agent.session.status = Status.EXITED
        seen: list[bool] = []

        def probe_lock() -> None:
            got = spawn_policy.LOCK.acquire(timeout=0.5)
            seen.append(got)
            if got:
                spawn_policy.LOCK.release()

        with spawn_policy.revive_guard(self.controller, agent):
            worker = __import__("threading").Thread(target=probe_lock, daemon=True)
            worker.start()
            worker.join(1.0)
        self.assertTrue(seen and seen[0], "spawn lock held across the readiness wait")


class SandboxProfileSpec(unittest.TestCase):
    """Part 1 unit checks that need no fixture."""

    def test_unsandboxed_is_the_default(self) -> None:
        with mock.patch.object(settings, "sandbox_config",
                               return_value={"profiles": {}, "agents": [],
                                             "invalid": [], "poison_all": []}):
            self.assertEqual(sandbox.decision_for("Heraldo"), (None, None))


def SessionSpecFactory(name: str):
    from crewhall.types import SessionSpec

    return SessionSpec(command=["/bin/true"], cwd="/tmp", name=name)


if __name__ == "__main__":
    import unittest

    unittest.main()
