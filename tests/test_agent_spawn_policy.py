from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from crewhall import audit, settings, spawn_policy
from crewhall.controller import Controller
from crewhall.daemon import Server
from crewhall.persistence import StateStore

from .support import FakeHarness

# The round-1 snippet (legacy ``name_patterns``, no profiles): now an invalid
# policy that denies everything (kept for the security review's reproducer).
POLICY = {
    "agent_spawners": ["Heraldo"],
    "agent_spawn_policy": {"name_patterns": ["Ejecutor_Backend_Sonnet_[1-9][0-9]?", "Auditor"],
                           "teams": ["Conciliare", "Otro"], "max_live": 6,
                           "kinds": ["claude", "opencode"]},
}
SKIP = "--dangerously-skip-permissions"
BACKEND_ARGS = ["--agent", "ejecutor-backend", SKIP]


def profiles(cwd: str) -> list[dict]:
    """The dev machine's profiles (cwd = its ``$HOME/Projects``)."""
    sonnet = [{"name_pattern": f"Ejecutor_{role}_Sonnet_[1-9][0-9]?", "kind": "claude",
               "args": ["--agent", agent, SKIP], "cwd": cwd, "backend": "tmux",
               "sandbox": "role"}
              for role, agent in (("Backend", "ejecutor-backend"), ("Frontend", "ejecutor-frontend"),
                                  ("Sonda", "sonda"), ("Redactor", "redactor"))]
    return [*sonnet,
            {"name_pattern": "Ejecutor_DeepSeekV4Flash_[1-9][0-9]?", "kind": "opencode",
             "args": ["--agent", "ejecutor"], "cwd": cwd, "backend": "tmux",
             "sandbox": "role"},
            {"name_pattern": "Auditor", "kind": "claude",
             "args": ["--agent", "auditor", SKIP], "cwd": cwd, "backend": "tmux",
             "sandbox": "role"},
            {"name_pattern": "Investigador_Claude", "kind": "claude",
             "args": ["--agent", "investigador", SKIP], "cwd": cwd, "backend": "tmux",
             "sandbox": "role"},
            {"name_pattern": "Investigador_DeepSeek", "kind": "opencode",
             "args": ["--agent", "investigador_deepseek"], "cwd": cwd, "backend": "tmux",
             "sandbox": "role"}]


def sandbox_section(cwd: str) -> dict:
    """Stage 2: Heraldo and every spawn-profile name are opted in."""
    profile = {"binds": [{"path": cwd, "mode": "rw"}], "env": {},
               "network": "host", "deny": [], "workspace": "rw"}
    return {
        "profiles": {"role": profile, "heraldo": profile},
        "agents": [{"name_pattern": "Heraldo", "profile": "heraldo"},
                   {"name_pattern": "Auditor", "profile": "role"},
                   *[{"name_pattern": p["name_pattern"], "profile": "role"}
                     for p in profiles(cwd)]],
    }


def policy(cwd: str, **body) -> dict:
    return {"agent_spawners": ["Heraldo"],
            "sandbox": sandbox_section(cwd),
            "agent_spawn_policy": {"profiles": profiles(cwd), "teams": ["Conciliare", "Otro"],
                                   "max_live": 6, "kinds": ["claude", "opencode"], **body}}


class AgentSpawnPolicy(unittest.TestCase):
    """``agent_create``/``agent_stop`` from an agent under the daemon-side policy."""

    BASE = ("Heraldo", "Ejecutor_Sonnet_1", "Auditor", "Orquestador")

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-spawn-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.cwd = os.path.join(self.root, "Projects")
        os.makedirs(self.cwd)
        self.store = StateStore(path=os.path.join(self.root, "state.json"))
        self.launched: list[dict] = []
        self.crash_views: list[dict] = []
        self.fail_at: str | None = None
        self.controller = self._controller()
        self.server = Server(self.controller, os.path.join(self.root, "r", "d.sock"))
        c = self.controller
        # Each agent's process tree: a root process per agent (its TUI).
        self.roots: dict[str, int] = {}
        for name in self.BASE:
            c.register_agent(FakeHarness(name))
            self.roots[name] = self.spawn_root()
            c.get_agent(name).session.pid = self.roots[name]
        ids = {n: c.get_agent(n).agent_id for n in ("Heraldo", "Ejecutor_Sonnet_1", "Auditor")}
        self.ids = ids
        c.create_team("Conciliare", list(ids.values()), workspace=self.root)
        c.create_team("Otro", [ids["Auditor"]], workspace=self.root)
        # Requests come from inside Heraldo's process tree unless a test says otherwise.
        self.agent_pid = self.roots["Heraldo"]
        audit.reset_rate_limit()
        self._settings(policy(self.cwd))

    def spawn_root(self) -> int:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc.pid

    def _controller(self) -> Controller:
        c = Controller(adopt=False, persist=False)
        c._store = self.store

        def create_agent(kind, *, name=None, team=None, agent_id=None, **kw):
            if self.fail_at == "before":
                raise RuntimeError("injected failure before launch")
            h = FakeHarness(name)
            if agent_id:
                h.session.session_id = agent_id
            c.register_agent(h)
            if team:
                c.teams.add_member(team, h.agent_id)
            c._persist()
            self.launched.append({"kind": kind, "name": name, **kw})
            # What a daemon crash right after the launch would leave on disk.
            self.crash_views.append(self.store.snapshot(c))
            if self.fail_at == "after":
                raise RuntimeError("injected failure after launch")
            return h

        c.create_agent = create_agent
        return c

    def _settings(self, data: dict | None, restart: bool = True) -> None:
        """Write settings.json; ``restart`` = the daemon (re)starts and reads it."""
        os.makedirs(settings.config_dir(), exist_ok=True)
        if data is None:
            with __import__("contextlib").suppress(FileNotFoundError):
                os.unlink(settings.path())
        else:
            with open(settings.path(), "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        settings._CACHE.update(stamp=None, data=None)
        if restart:
            spawn_policy.load()

    def call(self, op: str, sender: str = "Heraldo", token: str | None = None,
             server: Server | None = None, peer: int | None = 0, **kw) -> dict:
        """``peer`` 0 = the sender's own process tree (the honest caller)."""
        server = server or self.server
        req = {"op": op, **kw}
        if sender:
            h = server.controller.get_agent(sender)
            req["sender"] = sender
            req["token"] = token if token is not None else \
                server.controller._agent_token(h.agent_id)
            if peer == 0:
                peer = self.roots.get(sender) or h.session.pid
        elif peer == 0:
            peer = self.agent_pid
        denied = server._guard_origin(req, peer)
        return denied if denied is not None else server.dispatch(req)

    def create(self, name: str, sender: str = "Heraldo", team: str = "Conciliare",
               kind: str = "claude", **kw) -> dict:
        kw.setdefault("args", BACKEND_ARGS)
        return self.call("agent_create", sender=sender, kind=kind, name=name, team=team, **kw)

    def stop(self, target: str, sender: str = "Heraldo", **kw) -> dict:
        return self.call("agent_stop", sender=sender, target=target, **kw)

    def assertDenied(self, resp: dict, text: str) -> None:
        self.assertFalse(resp.get("ok"), resp)
        self.assertIn(text, resp["error"])

    def audit_rows(self, op: str = "agent_spawn_policy") -> list[dict]:
        try:
            with open(audit.audit_path(), encoding="utf-8") as fh:
                return [r for r in (json.loads(line) for line in fh) if r["op"] == op]
        except FileNotFoundError:
            return []

    # -- policy off ----------------------------------------------------------
    def test_policy_off_keeps_the_control_plane_refusal(self) -> None:
        self.controller.agent_root_pids = lambda: {os.getpid()}
        for data in (None, {**policy(self.cwd), "agent_spawners": []}):
            self._settings(data)
            self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"), "not available to agents")
            self.assertDenied(self.stop("Auditor"), "not available to agents")
            self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1", sender=""),
                              "not available to agents")

    def test_human_control_plane_path_unchanged_with_policy_on(self) -> None:
        req = {"op": "agent_stop", "target": "Auditor",
               "_control_token": self.server._control_token}
        self.assertIsNone(self.server._guard_origin(req, None))
        self.assertNotIn("_spawn_caller", req)

    # -- create --------------------------------------------------------------
    def test_heraldo_creates_an_allowed_agent(self) -> None:
        resp = self.create("Ejecutor_Backend_Sonnet_1")
        self.assertTrue(resp.get("ok"), resp)
        new = self.controller.get_agent("Ejecutor_Backend_Sonnet_1")
        self.assertEqual(self.controller._agent_created_by[new.agent_id], self.ids["Heraldo"])
        self.assertIn(new.agent_id, self.controller.teams.get("Conciliare").agent_ids)

    def test_name_not_matching_patterns_is_denied(self) -> None:
        self.assertDenied(self.create("Evil_1"), "does not match any profile")

    def test_protected_names_are_denied(self) -> None:
        for name in ("Orquestador", "Ejecutor_Sonnet_1", "Heraldo", "heraldo", "HERALDO"):
            self.assertDenied(self.create(name), "is protected")
        # The settings key adds names; the three built-ins survive its edit.
        self._settings({**policy(self.cwd), "agent_spawn_protected": ["Ejecutor_Backend_Sonnet_7"]})
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_7"), "is protected")
        self.assertDenied(self.create("Orquestador"), "is protected")

    def test_patterns_do_not_match_the_permanent_executor(self) -> None:
        import re

        patterns = [p["name_pattern"] for p in profiles("/x")]
        self.assertFalse(any(re.fullmatch(p, "Ejecutor_Sonnet_1") for p in patterns))
        for ok in ("Ejecutor_Backend_Sonnet_1", "Ejecutor_DeepSeekV4Flash_12", "Auditor",
                   "Investigador_Claude"):
            self.assertTrue(any(re.fullmatch(p, ok) for p in patterns), ok)

    def test_agent_not_in_spawners_is_denied(self) -> None:
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_2", sender="Ejecutor_Sonnet_1"),
                          "is not in agent_spawners")
        self.assertDenied(self.stop("Auditor", sender="Ejecutor_Sonnet_1"),
                          "is not in agent_spawners")

    def test_team_and_kind_restrictions(self) -> None:
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_3", team="Otro"),
                          "is not a member of team")
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_3", team="Desconocido"),
                          "is not allowed")
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_3", kind="codex"),
                          "kind 'codex' is not allowed")
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_3", conversation="c-1"),
                          "cannot target a chat")
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_3", host="dev"),
                          "cannot target a chat or a remote host")

    def test_max_live_then_stop_frees_a_slot(self) -> None:
        self._settings(policy(self.cwd, max_live=2))
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_2").get("ok"))
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_3"), "max_live (2) reached")
        self.assertTrue(self.stop("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_3").get("ok"))

    def test_name_variants_are_denied_not_normalized(self) -> None:
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.assertDenied(self.create("ejecutor_backend_sonnet_1"), "does not match")
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_2 "), "surrounding blank")
        self.assertDenied(self.create("Ejecutor_Backend_Sonnеt_2"), "non-ASCII")
        self.assertDenied(self.create("Hеraldo"), "non-ASCII")
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1"), "already taken")

    # -- stop ----------------------------------------------------------------
    def test_stop_only_own_creations(self) -> None:
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.assertTrue(self.stop("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.assertDenied(self.stop("Auditor"), "was not created by Heraldo")
        self.assertDenied(self.stop("Ejecutor_Sonnet_1"), "is protected")
        self.assertDenied(self.stop("Heraldo"), "cannot stop itself")

    def test_created_by_survives_a_daemon_restart(self) -> None:
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_1").get("ok"))
        new_id = self.controller.get_agent("Ejecutor_Backend_Sonnet_1").agent_id
        self.store.save(self.store.snapshot(self.controller))
        c2 = self._controller()
        c2.restore()
        # The restored Heraldo is the same live process tree.
        c2.agent_roots = lambda: {c2.get_agent("Heraldo").agent_id: {self.roots["Heraldo"]}}
        self.assertEqual(c2._agent_created_by.get(new_id), self.ids["Heraldo"])
        server2 = Server(c2, os.path.join(self.root, "r", "d2.sock"))
        self.assertDenied(self.call("agent_stop", server=server2, target="Auditor"),
                          "was not created by")
        self.assertTrue(self.call("agent_stop", server=server2,
                                  target="Ejecutor_Backend_Sonnet_1").get("ok"))

    # -- identity ------------------------------------------------------------
    def test_forged_sender_without_valid_token_is_denied(self) -> None:
        for token in ("", "forged"):
            self.assertDenied(self.create("Ejecutor_Backend_Sonnet_1", token=token),
                              "invalid agent identity/token")
            self.assertDenied(self.stop("Auditor", token=token), "invalid agent identity/token")
        # A caller cannot plant the internal field the gate sets.
        req = {"op": "agent_create", "kind": "claude", "name": "Evil_1", "team": "Conciliare",
               "_spawn_caller": self.ids["Heraldo"], "sender": "Heraldo", "token": "x"}
        self.assertIsNotNone(self.server._guard_origin(req, self.agent_pid))
        self.assertNotIn("_spawn_caller", req)

    def test_every_decision_is_audited(self) -> None:
        self.create("Ejecutor_Backend_Sonnet_1")
        self.create("Evil_1")
        self.stop("Auditor")
        with open(audit.audit_path(), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        rows = [r for r in rows if r["op"] == "agent_spawn_policy"]
        self.assertEqual([(r["caller"], r["target"], r["decision"]) for r in rows], [
            ("Heraldo", "Ejecutor_Backend_Sonnet_1", "allowed"),
            ("Heraldo", "Evil_1", "denied"),
            ("Heraldo", "Auditor", "denied"),
        ])
        self.assertIn("does not match", rows[1]["reason"])

    # -- profiles (F1) -------------------------------------------------------
    def use_profiles(self, **body) -> str:
        self._settings(policy(self.cwd, **{"teams": ["Conciliare"], **body}))
        return self.cwd

    def test_profile_exact_args_launch_the_profile_values(self) -> None:
        cwd = self.use_profiles()
        # The CLI sends --args as one string; the daemon splits it shell-style.
        resp = self.create("Ejecutor_Backend_Sonnet_9",
                           args="--agent ejecutor-backend --dangerously-skip-permissions")
        self.assertTrue(resp.get("ok"), resp)
        self.assertTrue(self.create("Ejecutor_DeepSeekV4Flash_3", kind="opencode",
                                    args=["--agent", "ejecutor"], cwd=cwd,
                                    workspace_mode="worktree").get("ok"))
        self.assertEqual([(x["kind"], x["args"], x["cwd"], x["workspace_mode"], x["backend"])
                          for x in self.launched],
                         [("claude", BACKEND_ARGS, cwd, "shared", "tmux"),
                          ("opencode", ["--agent", "ejecutor"], cwd, "shared", "tmux")])

    def test_profile_args_must_match_exactly(self) -> None:
        self.use_profiles()
        bad = ([*BACKEND_ARGS, SKIP], [*BACKEND_ARGS, "--verbose"],
               ["--agent", SKIP, "ejecutor-backend"], [SKIP, "--agent", "ejecutor-backend"],
               [*BACKEND_ARGS, "--settings", "x"], ["--agent", "ejecutor-frontend", SKIP],
               ["--agent", "ejecutor-backend"], None, "--agent 'ejecutor-backend")
        for args in bad:
            self.assertDenied(self.create("Ejecutor_Backend_Sonnet_9", args=args),
                              "args/cwd/backend not allowed for profile")
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_9", kind="opencode",
                                      args=BACKEND_ARGS), "args/cwd/backend not allowed for profile")
        self.assertEqual(self.launched, [])

    def test_profile_cwd_must_match_exactly(self) -> None:
        cwd = self.use_profiles()
        link = os.path.join(self.root, "link")
        os.symlink(cwd, link)
        for other in ("/tmp", f"{cwd}/sub", f"{cwd}/../Projects", f"{cwd}/", link, ""):
            self.assertDenied(self.create("Ejecutor_Backend_Sonnet_9", args=BACKEND_ARGS,
                                          cwd=other), "args/cwd/backend not allowed for profile")
        self.assertEqual(self.launched, [])

    # -- max_live counts live agents (F2) -------------------------------------
    def test_max_live_counts_only_live_agents(self) -> None:
        from crewhall.types import Status

        self.use_profiles()
        for n in range(1, 7):
            self.assertTrue(self.create(f"Ejecutor_Backend_Sonnet_{n}", args=BACKEND_ARGS)
                            .get("ok"))
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_7", args=BACKEND_ARGS),
                          "max_live (6) reached")
        self.assertTrue(self.stop("Ejecutor_Backend_Sonnet_1").get("ok"))
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_7", args=BACKEND_ARGS).get("ok"))
        # An exited agent is still registered (listed) but does not take a slot.
        self.controller.get_agent("Ejecutor_Backend_Sonnet_2").session.status = Status.EXITED
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_8", args=BACKEND_ARGS).get("ok"))
        self.assertDenied(self.create("Ejecutor_Backend_Sonnet_10", args=BACKEND_ARGS),
                          "max_live (6) reached")

    # -- creator recorded before launch (F3) ----------------------------------
    def test_creator_is_persisted_with_the_first_record(self) -> None:
        self.use_profiles()
        self.assertTrue(self.create("Ejecutor_Backend_Sonnet_9", args=BACKEND_ARGS).get("ok"))
        new_id = self.controller.get_agent("Ejecutor_Backend_Sonnet_9").agent_id
        rows = [a for a in self.crash_views[0]["agents"] if a["agent_id"] == new_id]
        self.assertEqual([r["created_by"] for r in rows], [self.ids["Heraldo"]])

    def test_failed_launch_leaves_no_unstoppable_agent(self) -> None:
        self.use_profiles()
        for when in ("before", "after"):
            self.fail_at = when
            resp = self.create("Ejecutor_Backend_Sonnet_9", args=BACKEND_ARGS)
            self.assertFalse(resp.get("ok"), resp)
            self.assertIn(f"injected failure {when} launch", resp["error"])
            self.assertNotIn("Ejecutor_Backend_Sonnet_9",
                             [h.name for h in self.controller.agents.all()])
            self.assertEqual(self.controller._agent_created_by, {})
        # A crash right after the launch: the restored agent is Heraldo's to stop.
        self.fail_at = None
        self.store.save(self.crash_views[-1])
        c2 = self._controller()
        c2.restore()
        # The restored Heraldo is the same live process tree.
        c2.agent_roots = lambda: {c2.get_agent("Heraldo").agent_id: {self.roots["Heraldo"]}}
        server2 = Server(c2, os.path.join(self.root, "r", "d2.sock"))
        self.assertTrue(self.call("agent_stop", server=server2,
                                  target="Ejecutor_Backend_Sonnet_9").get("ok"))

    def test_denied_profile_create_audits_the_reason(self) -> None:
        self.use_profiles()
        self.create("Ejecutor_Backend_Sonnet_9", args=[*BACKEND_ARGS, "--settings", "x"])
        with open(audit.audit_path(), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        rows = [r for r in rows if r["op"] == "agent_spawn_policy"]
        self.assertEqual([(r["decision"], r["reason"]) for r in rows],
                         [("denied", "args/cwd/backend not allowed for profile")])


class SpawnCliSendsIdentity(unittest.TestCase):
    def _run(self, fn, args) -> dict:
        calls = []

        class Client:
            def call(self, op, **kw):
                calls.append(kw)
                return {"agent": {"agent_id": "x", "name": "x", "kind": "claude",
                                  "state": "ready"}}

        env = {"CREWHALL_AGENT_ID": "Heraldo", "CREWHALL_TOKEN": "tok"}
        with mock.patch.dict(os.environ, env), \
                mock.patch("crewhall.commands.common._client", return_value=Client()), \
                mock.patch("crewhall.commands.agents._print_agent"):
            fn(args)
        return calls[0]

    def test_create_and_stop_send_sender_and_token(self) -> None:
        from crewhall.commands import agents as cmds

        create = argparse.Namespace(
            profile=None, kind="claude", args=None, cwd=None, backend=None, host=None,
            name="Ejecutor_Backend_Sonnet_1", team="Conciliare", cols=120, rows=40,
            wait=False, timeout=30.0)
        with mock.patch("crewhall.specs.load_profiles", return_value={}):
            sent = self._run(cmds.cmd_agent_create, create)
        self.assertEqual((sent["sender"], sent["token"]), ("Heraldo", "tok"))
        sent = self._run(cmds.cmd_agent_stop, argparse.Namespace(target="x", force=False))
        self.assertEqual((sent["sender"], sent["token"]), ("Heraldo", "tok"))


if __name__ == "__main__":
    unittest.main()
