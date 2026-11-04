"""Daemon RPC protocol: malformed bodies must not kill the connection handler."""
from __future__ import annotations

import json
import socket
import unittest

from crewhall.daemon import Server


class DaemonProtocol(unittest.TestCase):
    def test_non_object_request_is_rejected(self):
        server = object.__new__(Server)
        a, b = socket.socketpair()
        try:
            b.sendall(b"[1]\n")
            b.shutdown(socket.SHUT_WR)
            server._handle(a)
            with b.makefile("rb") as fh:
                reply = json.loads(fh.readline())
        finally:
            a.close()
            b.close()
        self.assertFalse(reply["ok"])
        self.assertIn("JSON object", reply["error"])


if __name__ == "__main__":
    unittest.main()
