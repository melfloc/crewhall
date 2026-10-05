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


if __name__ == "__main__":
    unittest.main()
