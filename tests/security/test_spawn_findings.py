"""The security review's reproducer (REVISION_SEGURIDAD_agent_spawn.md), inverted.

Each test replays an attack the review demonstrated against the agent spawn
policy and asserts it is now DENIED. Same fixture as the review: in-process
``Server``, ``FakeHarness``, temp XDG dirs; no tmux, no real agents.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading

from crewhall import audit, doctor, settings, spawn_policy
from crewhall.messaging import MessagingError
from crewhall.ops.agents import _spawn_timeout
from crewhall.types import Status
from tests import test_agent_spawn_policy as base
from tests.test_agent_spawn_policy import BACKEND_ARGS, policy


class SpawnFindings(base.AgentSpawnPolicy):
    # The fixture only: the base class's own tests run in their module.
    locals().update({n: None for n in dir(base.AgentSpawnPolicy) if n.startswith("test_")})

    def heraldo_req(self, **kw) -> dict:
        return {"op": "agent_create", "sender": "Heraldo",
                "token": self.controller._agent_token(self.ids["Heraldo"]),
                "kind": "claude", "name": "Ejecutor_Backend_Sonnet_3", "team": "Conciliare",
                "args": BACKEND_ARGS, **kw}

    def live(self) -> int:
        return spawn_policy.live_created(self.controller, self.ids["Heraldo"])

    # R1: Heraldo's token is useless outside Heraldo's own process tree --------
    def test_r1_token_from_a_foreign_process_is_denied(self) -> None:
        self.use_profiles()
        for peer, why in ((self.roots["Ejecutor_Sonnet_1"], "not in Heraldo's process tree"),
                          (self.spawn_root(), "not in Heraldo's process tree"),
                          (1, "no peer pid"), (None, "no peer pid")):
            r = self.server._guard_origin(self.heraldo_req(), peer)
            self.assertDenied(r, why)
            r = self.server._guard_origin(
                {"op": "agent_stop", "sender": "Heraldo", "target": "Auditor",
                 "token": self.controller._agent_token(self.ids["Heraldo"])}, peer)
            self.assertDenied(r, why)
        self.assertEqual(self.launched, [])

    def test_r1_owner_descendant_allowed_other_agents_tree_denied(self) -> None:
        self.use_profiles()
        # Heraldo's root is this test process: every process it starts descends from it.
        self.controller.get_agent("Heraldo").session.pid = os.getpid()
        self.assertIsNone(self.server._guard_origin(self.heraldo_req(), self.spawn_root()))
        # Ejecutor_Sonnet_1's root is also below it, yet that tree belongs to another agent.
        self.assertDenied(self.server._guard_origin(
            self.heraldo_req(), self.roots["Ejecutor_Sonnet_1"]), "another agent's process tree")

    # R2: settings.json is read once, at daemon start --------------------------
    def test_r2_self_added_spawner_after_start_is_denied(self) -> None:
        self.use_profiles()
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1", sender="Ejecutor_Sonnet_1"),
                          "not in agent_spawners")
        data = json.load(open(settings.path()))
        data["agent_spawners"].append("Ejecutor_Sonnet_1")
        data["agent_spawn_policy"]["max_live"] = 99
        self._settings(data, restart=False)   # what the agent's Edit tool would do
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1", sender="Ejecutor_Sonnet_1"),
                          "not in agent_spawners")
        self.assertEqual(spawn_policy.config()["max_live"], 6)
        spawn_policy.load()                   # the owner restarts the daemon
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1", sender="Ejecutor_Sonnet_1")
                        .get("ok"))

    def test_r2_start_logs_the_policy_sha256_and_doctor_prints_it(self) -> None:
        self.server._load_spawn_policy()
        [row] = self.audit_rows("agent_spawn_policy_loaded")
        expected = spawn_policy.digest(settings.spawn_policy_raw())
        self.assertEqual((row["result"], row["sha256"]), ("ok", expected))
        check = doctor.check_spawn_policy()
        self.assertEqual(check["status"], "ok")
        self.assertIn(f"sha256={expected} daemon-loaded={expected}", check["detail"])
        self._settings(policy(self.cwd, max_live=9), restart=False)
        self.assertEqual(doctor.check_spawn_policy()["status"], "warn")

    # R3: max_live holds across restarts of spawn-created agents ---------------
    def _fake_revive(self) -> None:
        def revive(target, **_kw):
            h = self.controller.agents.resolve(target)
            h.session.status = Status.RUNNING
            return h
        self.controller._restart_agent = revive
        self.controller._force_restart_agent = revive

    def test_r3_exited_then_revived_cannot_exceed_max_live(self) -> None:
        self.use_profiles()
        self._fake_revive()
        names = [f"Ejecutor_Backend_Sonnet_{n}" for n in range(1, 13)]
        for n, name in enumerate(names, 1):
            r = self.create(name)
            self.assertTrue(r.get("ok"), r)
            if n <= 6:
                self.controller.get_agent(name).session.status = Status.EXITED
        for name in names[:6]:   # every revive path re-checks the creator's count
            with self.assertRaisesRegex(MessagingError, r"max_live \(6\) reached"):
                self.controller.restart_agent(name)
            with self.assertRaisesRegex(MessagingError, r"max_live \(6\) reached"):
                self.controller.force_restart_agent(name, by="user")
            r = self.server.dispatch({"op": "agent_restart", "target": name})
            self.assertDenied(r, "max_live (6) reached")
        self.assertEqual(self.live(), 6)

    def test_r3_restart_only_into_a_free_slot(self) -> None:
        self.use_profiles()
        self._fake_revive()
        for n in range(1, 7):
            self.assertTrue(self.create(f"Ejecutor_Backend_Sonnet_{n}").get("ok"))
        for n in (1, 2):   # exit without stop
            self.controller.get_agent(f"Ejecutor_Backend_Sonnet_{n}").session.status = Status.EXITED
        for n in (7, 8):
            self.assertTrue(self.create(f"Ejecutor_Backend_Sonnet_{n}").get("ok"))
        for n in (1, 2):
            with self.assertRaisesRegex(MessagingError, "max_live"):
                self.controller.restart_agent(f"Ejecutor_Backend_Sonnet_{n}")
        self.assertTrue(self.stop("Ejecutor_Backend_Sonnet_8").get("ok"))
        self.controller.restart_agent("Ejecutor_Backend_Sonnet_1")
        with self.assertRaisesRegex(MessagingError, "max_live"):
            self.controller.restart_agent("Ejecutor_Backend_Sonnet_2")
        self.assertEqual(self.live(), 6)

    # R4: no legacy mode; a malformed policy denies everything -----------------
    def test_r4_malformed_policy_denies_everything(self) -> None:
        good = policy(self.cwd)
        body = good["agent_spawn_policy"]
        bad_profile = {**body["profiles"][0], "extra_args": ["--settings", "x"]}
        variants = {
            "absent profiles": {**good, "agent_spawn_policy": {k: v for k, v in body.items()
                                                               if k != "profiles"}},
            "Profiles": {**good, "agent_spawn_policy": {
                **{k: v for k, v in body.items() if k != "profiles"}, "Profiles": body["profiles"]}},
            "top-level typo": {"agent_spawners": ["Heraldo"], "agent_spawn_polcy": body},
            "policy unknown key": {**good, "agent_spawn_policy": {**body, "name_patterns": [".*"]}},
            "profile unknown key": {**good, "agent_spawn_policy": {
                **body, "profiles": [bad_profile, *body["profiles"][1:]]}},
            "empty list": {**good, "agent_spawn_policy": {**body, "profiles": []}},
        }
        for label, data in variants.items():
            with self.subTest(label), self.assertLogs("crewhall.spawn_policy", "ERROR") as logs:
                self._settings(data)
                self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"), "policy invalid:")
                self.assertDenied(self.stop("Auditor"), "policy invalid:")
            self.assertEqual(len(logs.output), 1)
            self.assertIn("agent spawn policy invalid", logs.output[0])
        self.assertEqual(self.launched, [])

    def test_r4_round1_legacy_launch_is_denied(self) -> None:
        self._settings(base.POLICY)
        r = self.create("Ejecutor_Backend_Sonnet_4",
                        args=["--settings", "/tmp/x.json", "--add-dir", "/", "--mcp-config",
                              "/tmp/m.json"], cwd="/", workspace_mode="shared", backend="pty")
        self.assertDenied(r, "policy invalid:")
        self.assertEqual(self.launched, [])

    # Y1: spawners are never created; a spawned agent never spawns -------------
    def test_y1_spawned_agent_can_never_be_a_spawner(self) -> None:
        self._settings({**policy(self.cwd), "agent_spawners": ["Heraldo", "Auditor"]})
        self.controller.agents.remove(self.ids["Auditor"])
        self.assertDenied(self.create("Auditor", args=["--agent", "auditor",
                                                       "--dangerously-skip-permissions"]),
                          "is protected")
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.roots["Ejecutor_Backend_Sonnet_1"] = root = self.spawn_root()
        self.controller.get_agent("Ejecutor_Backend_Sonnet_1").session.pid = root
        self._settings({**policy(self.cwd),
                        "agent_spawners": ["Heraldo", "Ejecutor_Backend_Sonnet_1"]})
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_2",
                                      sender="Ejecutor_Backend_Sonnet_1"),
                          "was created by an agent and cannot spawn")

    # Y2/Y3: the profile pins the backend; spawn teams are plain ---------------
    def test_y2_profile_pins_the_backend(self) -> None:
        self.use_profiles()
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_2", backend="pty"),
                          "args/cwd/backend not allowed")
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_2", backend="auto").get("ok"))
        self.assertEqual(self.launched[-1]["backend"], "tmux")
        data = policy(self.cwd)
        del data["agent_spawn_policy"]["profiles"][0]["backend"]
        self._settings(data)
        self.assertEqual(spawn_policy.config()["profiles"][0]["backend"],
                         settings.spawn_default_backend())
        data["agent_spawn_policy"]["profiles"][0]["backend"] = "ssh-tmux"
        self._settings(data)
        self.assertIn("backend must be one of", spawn_policy.config()["invalid"])

    def test_y3_team_with_host_or_worktree_is_denied(self) -> None:
        self.use_profiles()
        team = self.controller.teams.get("Conciliare")
        for change in ({"workspace_mode": "worktree"}, {"host": "dev"}):
            self.controller._team_or_none = lambda _n, c=change: dataclasses.replace(team, **c)
            self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"),
                              "has a remote host or worktree mode")
        self.assertEqual(self.launched, [])

    # Y5: audit lines are bounded ----------------------------------------------
    def test_y5_huge_request_writes_a_bounded_audit_line(self) -> None:
        big = "A" * 4_000_000
        req = {"op": "agent_create", "sender": big + "\n{\"op\":\"forged\"}", "token": "x",
               "kind": "claude", "name": big, "team": "Conciliare", "args": [big]}
        self.assertDenied(self.server._guard_origin(dict(req), None), "invalid agent identity")
        r = self.create(big, args=[big])
        self.assertDenied(r, "does not match any profile")
        lines = open(audit.audit_path(), encoding="utf-8").read().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(len(line.encode()), audit.MAX_RECORD)
            self.assertTrue(json.loads(line)["truncated"])
        audit.record("agent_stop", actor="GENUINE-MARKER", summary="real event")
        for _ in range(12):
            self.server._guard_origin(dict(req), None)
        self.assertIn("GENUINE-MARKER", open(audit.audit_path(), encoding="utf-8").read())

    # Y6: the audit holds the final decision and the real actor ---------------
    def test_y6_audit_is_the_final_decision_by_the_agent(self) -> None:
        self.use_profiles(max_live=1)
        reqs = []
        for n in (1, 2):
            req = self.heraldo_req(name=f"Ejecutor_Backend_Sonnet_{n}")
            self.assertIsNone(self.server._guard_origin(req, self.agent_pid))  # both pass gate
            reqs.append(req)
        out: list = [None, None]
        ts = [threading.Thread(target=lambda i=i: out.__setitem__(i, self.server.dispatch(reqs[i])))
              for i in (0, 1)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(bool(o.get("ok")) for o in out), [False, True])
        self.assertEqual(sorted(r["decision"] for r in self.audit_rows()), ["allowed", "denied"])
        self.assertEqual({(r["actor"], r["result"]) for r in self.audit_rows("agent_create")},
                         {("Heraldo", "ok"), ("Heraldo", "error")})

    # Stop never forces; timeouts are capped -----------------------------------
    def test_stop_ignores_force_and_timeouts_are_capped(self) -> None:
        self.use_profiles()
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        seen = {}
        real = self.controller.remove_agent

        def spy(target, force=False):
            seen["force"] = force
            return real(target, force=force)
        self.controller.remove_agent = spy
        self.assertTrue(self.stop("Ejecutor_Backend_Sonnet_1", force=True).get("ok"))
        self.assertIs(seen["force"], False)
        for given, capped in ((1e9, 30.0), (float("inf"), 30.0), (float("nan"), 30.0),
                              ("x", 30.0), (-5, 0.0), (10, 10.0)):
            self.assertEqual(_spawn_timeout({"timeout": given}), capped, given)
