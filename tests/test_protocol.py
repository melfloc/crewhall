from __future__ import annotations

import contextlib
import io
import types
import unittest
from unittest import mock

from crewhall import client, protocol
from crewhall.daemon import Server


class ProtocolWarning(unittest.TestCase):
    def test_same_version_is_silent(self) -> None:
        self.assertIsNone(protocol.warning(protocol.PROTOCOL_VERSION))

    def test_older_and_newer_warn(self) -> None:
        self.assertIn("speaks protocol 0", protocol.warning(0) or "")
        self.assertIn("newer protocol", protocol.warning(99) or "")

    def test_missing_version_warns(self) -> None:
        self.assertIn("predates", protocol.warning(None) or "")

    def test_client_warns_on_a_simulated_old_daemon(self) -> None:
        client._PROTOCOL_WARNED = False
        with mock.patch.object(client, "_read_line", return_value={"protocol": 0}), \
                mock.patch.object(client.socket, "socket"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                client._warn_protocol("/tmp/does-not-matter")
        self.assertIn("protocol", err.getvalue())

    def test_client_is_silent_for_a_matching_daemon(self) -> None:
        client._PROTOCOL_WARNED = False
        with mock.patch.object(client, "_read_line",
                               return_value={"protocol": protocol.PROTOCOL_VERSION}), \
                mock.patch.object(client.socket, "socket"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                client._warn_protocol("/tmp/does-not-matter")
        self.assertEqual(err.getvalue(), "")


class PingAdvertisesProtocol(unittest.TestCase):
    def test_ping_has_protocol(self) -> None:
        fake = types.SimpleNamespace(
            controller=types.SimpleNamespace(registry=types.SimpleNamespace(all=list)))
        out = Server._op_ping(fake, {})
        self.assertEqual(out["protocol"], protocol.PROTOCOL_VERSION)


if __name__ == "__main__":
    unittest.main()
