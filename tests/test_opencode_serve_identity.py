"""The OpenCode v2 ``serve`` runs the agent's tools, so it carries the identity.

In v2 the TUI only draws; the shell tool executes inside the per-agent ``serve``.
A serve started without ``CREWHALL_AGENT_ID``/``CREWHALL_TOKEN`` leaves every
``crewhall`` call made by the agent unable to say who it is.
"""
from __future__ import annotations

import unittest
from unittest import mock

from crewhall import Controller
from crewhall.services.agents import _AdoptedServe


class _Proc:
    pid = 4242

    def poll(self):
        return None


class ServeIdentity(unittest.TestCase):
    def setUp(self):
        self.controller = Controller(adopt=False)

    def test_start_passes_the_identity_to_the_serve_environment(self):
        identity = self.controller._build_agent_env("sess_a", "Ejecutor", ["t1"], None)
        seen = {}

        def popen(argv, **kw):
            seen.update(kw["env"])
            return _Proc()

        with mock.patch("crewhall.services.agents.subprocess.Popen", popen), \
                mock.patch("crewhall.services.agents.OpenCodeLink.get_json",
                           return_value={"version": "2.0.29"}), \
                mock.patch.object(self.controller, "_resolve_provider_command",
                                  return_value=(["opencode"], None, None)):
            self.controller._start_opencode_server(
                "opencode", "/tmp", 5555, "pw", identity=identity)
        self.assertEqual(seen["CREWHALL_AGENT_ID"], "sess_a")
        self.assertEqual(seen["CREWHALL_AGENT_NAME"], "Ejecutor")
        self.assertTrue(seen["CREWHALL_TOKEN"])
        self.assertEqual(seen["OPENCODE_SERVER_PASSWORD"], "pw")

    def test_a_live_serve_without_identity_is_replaced_on_reopen(self):
        c = self.controller
        c.track_conversations = True
        c._opencode_servers["a1"] = old = _Proc()
        c._opencode_links["a1"] = {"port": 1111, "password": "x", "api": "v2"}
        new = _Proc()
        with mock.patch.object(c, "_opencode_api", return_value="v2"), \
                mock.patch.object(c, "_serve_has_identity", return_value=False), \
                mock.patch.object(c, "_kill_opencode_server") as kill, \
                mock.patch.object(c, "_start_opencode_server", return_value=new) as start:
            link, proc = c._reopen_opencode_link(
                "a1", "opencode", [], "/tmp", identity={"CREWHALL_AGENT_ID": "a1"})
        kill.assert_called_once_with(old)
        self.assertIs(proc, new)
        self.assertEqual(start.call_args.kwargs["identity"], {"CREWHALL_AGENT_ID": "a1"})

    def test_a_live_serve_with_identity_is_reused_on_reopen(self):
        c = self.controller
        c.track_conversations = True
        c._opencode_servers["a1"] = old = _Proc()
        c._opencode_links["a1"] = {"port": 1111, "password": "x", "api": "v2"}
        with mock.patch.object(c, "_opencode_api", return_value="v2"), \
                mock.patch.object(c, "_serve_has_identity", return_value=True), \
                mock.patch.object(c, "_start_opencode_server") as start:
            link, proc = c._reopen_opencode_link("a1", "opencode", [], "/tmp", identity={})
        start.assert_not_called()
        self.assertIs(proc, old)

    def test_serve_identity_is_read_from_the_process_environment(self):
        import os
        import tempfile
        c = self.controller
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "environ")
            with open(path, "wb") as fh:
                fh.write(b"A=1\0CREWHALL_AGENT_ID=sess_x\0")
            with mock.patch("crewhall.services.agents.os.path.join", lambda *a: path):
                self.assertTrue(c._serve_has_identity(1))
            with open(path, "wb") as fh:
                fh.write(b"A=1\0")
            with mock.patch("crewhall.services.agents.os.path.join", lambda *a: path):
                self.assertFalse(c._serve_has_identity(1))
        self.assertTrue(c._serve_has_identity(2 ** 22 + 12345))  # unreadable: assume ok


class AdoptedServeCanBeStopped(unittest.TestCase):
    def test_has_the_popen_methods_shutdown_calls(self):
        adopted = _AdoptedServe(2 ** 22 + 54321)  # no such process
        for name in ("terminate", "kill", "wait"):
            self.assertTrue(callable(getattr(adopted, name)), name)
        self.assertEqual(adopted.wait(timeout=1), 0)


if __name__ == "__main__":
    unittest.main()
