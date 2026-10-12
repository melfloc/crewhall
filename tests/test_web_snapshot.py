from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall.client import Client, RpcError, ensure_daemon
from crewhall.web.server import _snapshot


class _FakeClient:
    def __init__(self, responses):
        self._responses = responses
        self.calls: list[str] = []

    def call(self, op, **params):
        self.calls.append(op)
        result = self._responses[op]
        if isinstance(result, Exception):
            raise result
        return result


class SnapshotUnit(unittest.TestCase):
    def _base(self, **extra):
        return {
            "agent_list": {"agents": []}, "team_list": {"teams": []},
            "message_history": {"messages": []}, "request_list": {"requests": []},
            "host_list": {"hosts": []}, **extra,
        }

    def test_snapshot_carries_crew_conversations(self):
        conv = [{"id": "c-1", "title": "Report", "team": "crew1"}]
        client = _FakeClient(self._base(conversation_crew={"conversations": conv}))
        snap = _snapshot(client)
        self.assertEqual(snap["conversations"], conv)
        self.assertIn("conversation_crew", client.calls)

    def test_snapshot_tolerates_an_older_daemon(self):
        client = _FakeClient(self._base(conversation_crew=RpcError("unknown op")))
        snap = _snapshot(client)
        self.assertEqual(snap["conversations"], [])


class SnapshotIntegration(unittest.TestCase):
    """A real daemon surfaces a chat-with-crew in the WebSocket snapshot."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="ati-snap-", dir="/tmp")
        cls._env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(cls.root, "c"),
            "XDG_STATE_HOME": os.path.join(cls.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(cls.root, "r"),
        })
        cls._env.start()
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        ensure_daemon()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            Client(autostart=False).call("shutdown")
        except Exception:  # noqa: BLE001
            pass
        cls._env.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_crew_chat_appears_in_the_snapshot(self):
        from crewhall import chat as conversations

        # A conversation that carries a crew (the team is validated by the op;
        # here we only need the stored meta to prove the snapshot wiring).
        crewed = conversations.create("Report", team="t-crew")
        plain = conversations.create("Solo")
        snap = _snapshot(Client())
        ids = [c["id"] for c in snap["conversations"]]
        self.assertIn(crewed["id"], ids)
        self.assertNotIn(plain["id"], ids)


if __name__ == "__main__":
    unittest.main()
