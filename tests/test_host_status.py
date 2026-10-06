from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from crewhall import cli, settings
from crewhall.controller import Controller


class HostStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ati-hs-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start(); self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        settings.patch({"hosts": {"a": {"ssh": "u@a", "tunnel": True}, "b": {"ssh": "u@b"}}})
        self.c = Controller(adopt=False, persist=False)

    def _agent(self, agent_id, host, unreachable=False):
        h = SimpleNamespace(agent_id=agent_id)
        h.session = SimpleNamespace(backend=SimpleNamespace(
            meta=lambda: {"host_unreachable": unreachable}))
        self.c._agent_hosts[agent_id] = host
        return h

    def _with_agents(self, *agents):
        self.c.agents = SimpleNamespace(all=lambda: list(agents), resolve=lambda r: None)

    def test_nothing_observed_is_unknown_not_ok(self):
        by = {h["name"]: h for h in self.c.host_status()}
        self.assertEqual(by["a"]["state"], "unknown")
        self.assertEqual(by["b"]["state"], "unknown")
        self.assertIsNone(by["a"]["checked_at"])
        self.assertTrue(by["a"]["tunnel"] and not by["b"]["tunnel"])

    def test_adoption_probe_drives_state_without_agents(self):
        self.c._host_health["a"] = (True, time.time())
        self.c._host_health["b"] = (False, time.time())
        by = {h["name"]: h for h in self.c.host_status()}
        self.assertEqual(by["a"]["state"], "ok")
        self.assertEqual(by["b"]["state"], "unreachable")
        self.assertIsNotNone(by["b"]["checked_at"])

    def test_unreachable_agent_and_reconnecting_tunnel(self):
        down = self._agent("x1", "a", unreachable=True)
        up = self._agent("x2", "b")
        self._with_agents(down, up)
        by = {h["name"]: h for h in self.c.host_status()}
        self.assertEqual(by["a"]["state"], "unreachable")
        self.assertEqual(by["b"]["state"], "ok")
        self.assertEqual(by["a"]["agents"], 1)
        self.assertEqual(self.c._agent_host_state(down), "unreachable")
        self.assertEqual(self.c._agent_host_state(up), "ok")

        self._with_agents(self._agent("x3", "a"))
        self.c.remote_links._links["a"] = SimpleNamespace(state="reconnecting")
        by = {h["name"]: h for h in self.c.host_status()}
        self.assertEqual(by["a"]["state"], "reconnecting")
        self.assertEqual(by["a"]["tunnel_state"], "reconnecting")

    def test_local_agent_has_no_host_state(self):
        local = SimpleNamespace(agent_id="l1", session=SimpleNamespace(
            backend=SimpleNamespace(meta=lambda: {})))
        self.assertIsNone(self.c._agent_host_state(local))

    def test_cli_host_list_renders_table_and_json(self):
        hosts = [{"name": "a", "ssh": "u@a", "port": 22, "tunnel": True,
                  "tunnel_state": "connected", "state": "ok", "agents": 2, "checked_at": None}]
        client = mock.Mock()
        client.call.return_value = {"hosts": hosts}
        with mock.patch.object(cli, "_client", return_value=client):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_host_list(SimpleNamespace(json=False))
            self.assertIn("connected", buf.getvalue())
            self.assertIn("u@a", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_host_list(SimpleNamespace(json=True))
            self.assertEqual(json.loads(buf.getvalue()), hosts)

    def test_web_exposes_host_list(self):
        from crewhall.web import server

        self.assertIn("host_list", server.ALLOWED_OPS)
        replies = {
            "agent_list": {"agents": []}, "team_list": {"teams": []},
            "message_history": {"messages": []}, "request_list": {"requests": []},
            "host_list": {"hosts": [{"name": "a"}]},
        }
        client = mock.Mock()
        client.call.side_effect = lambda op, **kw: replies[op]
        self.assertEqual(server._snapshot(client)["hosts"], [{"name": "a"}])


class HostManagementTests(unittest.TestCase):
    def setUp(self):
        from crewhall.daemon import Server

        self.root = tempfile.mkdtemp(prefix="ati-hm-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start(); self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.c = Controller(adopt=False, persist=False)
        self.server = Server(self.c, os.path.join(self.root, "r", "d.sock"))

    def _call(self, op, **kw):
        return self.server.dispatch({"op": op, **kw})

    def test_changes_need_typed_confirmation(self):
        for op, kw in (("host_set", {"name": "h", "ssh": "u@h"}), ("host_remove", {"name": "h"}),
                       ("settings_set", {"changes": {"hosts": {"h": {"ssh": "u@h"}}}})):
            res = self._call(op, **kw)
            self.assertFalse(res["ok"], op)
            self.assertIn("confirmation", res["error"])
        self.assertEqual(settings.hosts(), {})

    def test_add_edit_remove_and_validation(self):
        ok = self._call("host_set", name="h", ssh="deploy@10.0.0.5", port=2222, tunnel=True, confirm="CONFIRM")
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(ok["host"]["port"], 2222)
        self.assertTrue(ok["host"]["tunnel"])
        self.assertEqual([h["name"] for h in self._call("host_list")["hosts"]], ["h"])
        for bad in ({"ssh": "-oProxyCommand=evil"}, {"ssh": "a b@c"}, {"ssh": "u@h;id"},
                    {"ssh": "u@h", "port": 99999}, {"ssh": "u@h", "tunnel": "yes"},
                    {"ssh": "u@h", "identity": "/etc/passwd"}):
            res = self._call("host_set", name="x", confirm="CONFIRM", **bad)
            self.assertFalse(res["ok"], bad)
        self.assertEqual(list(settings.hosts()), ["h"])
        self.assertFalse(self._call("host_set", name="bad name", ssh="u@h", confirm="CONFIRM")["ok"])
        res = self._call("host_set", name="h", ssh="deploy@10.0.0.6", confirm="CONFIRM")
        self.assertEqual(res["host"]["ssh"], "deploy@10.0.0.6")
        self.assertTrue(self._call("host_remove", name="h", confirm="CONFIRM")["ok"])
        self.assertEqual(settings.hosts(), {})
        self.assertFalse(self._call("host_remove", name="h", confirm="CONFIRM")["ok"])

    def test_a_host_with_agents_cannot_be_edited_or_removed(self):
        self._call("host_set", name="h", ssh="u@h", confirm="CONFIRM")
        agent = SimpleNamespace(agent_id="a1", name="worker")
        self.c._agent_hosts["a1"] = "h"
        self.c.agents = SimpleNamespace(all=lambda: [agent], resolve=lambda r: agent)
        for op, kw in (("host_set", {"ssh": "u@other"}), ("host_remove", {})):
            res = self._call(op, name="h", confirm="CONFIRM", **kw)
            self.assertFalse(res["ok"], op)
            self.assertIn("worker", res["error"])
        self.assertEqual(settings.host("h")["ssh"], "u@h")

    def test_ssh_failures_get_an_actionable_hint(self):
        from crewhall.controller import _ssh_hint

        cfg = {"ssh": "u@box", "port": 2200, "known_hosts": None}
        self.assertIn("ssh -p 2200 u@box", _ssh_hint(cfg, "Host key verification failed."))
        self.assertIn("ssh-keyscan", _ssh_hint({**cfg, "known_hosts": "/k"}, "Host key verification failed."))
        self.assertIn("not authorized", _ssh_hint(cfg, "u@box: Permission denied (publickey)."))
        self.assertIn("did not answer", _ssh_hint(cfg, "ssh: connection timed out"))
        self.assertIn("refused", _ssh_hint(cfg, "connect to host box port 22: Connection refused"))
        self.assertIn("resolved", _ssh_hint(cfg, "Could not resolve hostname box"))
        self.assertEqual(_ssh_hint(cfg, "weird"), "Could not connect over SSH.")


if __name__ == "__main__":
    unittest.main()
