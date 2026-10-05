from __future__ import annotations

import time
import unittest

from agent_terminal.requests import RequestBoard, RequestError


class BoardUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.board = RequestBoard(max_open_per_agent=2, max_depth=3, default_timeout=60.0)

    def test_happy_cycle_and_states(self):
        req = self.board.create("a", "b", "do it")
        self.assertEqual(req.state, "pending")
        self.board.accept(req.request_id)
        self.assertEqual(self.board.get(req.request_id).state, "accepted")
        replied = self.board.reply(req.request_id, "b", "done")
        self.assertEqual(replied.state, "replied")
        self.assertEqual(replied.reply, "done")
        self.assertEqual(replied.replied_by, "b")

    def test_only_the_recipient_can_reply(self):
        req = self.board.create("a", "b", "do it")
        with self.assertRaises(RequestError):
            self.board.reply(req.request_id, "c", "hi")

    def test_timeout_expires(self):
        req = self.board.create("a", "b", "do it", timeout=0.05)
        time.sleep(0.08)
        self.assertEqual(self.board.get(req.request_id).state, "expired")

    def test_max_open_per_agent(self):
        self.board.create("a", "b", "1")
        self.board.create("a", "c", "2")
        with self.assertRaises(RequestError):
            self.board.create("a", "d", "3")

    def test_depth_limit(self):
        r1 = self.board.create("a", "b", "1")
        r2 = self.board.create("b", "c", "2", parent=r1.request_id)
        r3 = self.board.create("c", "d", "3", parent=r2.request_id)
        self.assertEqual(r3.depth, 3)
        with self.assertRaises(RequestError):
            self.board.create("d", "e", "4", parent=r3.request_id)

    def test_anti_loop_opposite_direction(self):
        self.board.create("a", "b", "1")
        with self.assertRaises(RequestError):
            self.board.create("b", "a", "2")

    def test_operator_cancel_and_snapshot_roundtrip(self):
        req = self.board.create("a", "b", "do it")
        self.board.cancel_operator(req.request_id)
        self.assertEqual(self.board.get(req.request_id).state, "cancelled")
        snap = self.board.snapshot()
        other = RequestBoard()
        other.load(snap)
        self.assertEqual(other.get(req.request_id).state, "cancelled")


class ControllerRequests(unittest.TestCase):
    def setUp(self) -> None:
        from agent_terminal import Controller

        from .support import FakeHarness

        self.c = Controller(adopt=False)
        self.a = FakeHarness(name="alpha")
        self.b = FakeHarness(name="beta")
        self.c.register_agent(self.a)
        self.c.register_agent(self.b)
        self.c.create_team("crew", [self.a.agent_id, self.b.agent_id])

    def _tokens(self):
        return (self.c._agent_token(self.a.agent_id), self.c._agent_token(self.b.agent_id))

    def test_request_reply_happy(self):
        ta, tb = self._tokens()
        out = self.c.create_request(self.a.agent_id, ta, self.b.agent_id, "do it", wait=False)
        rid = out["request"]["request_id"]
        self.assertIn(out["request"]["state"], ("pending", "accepted"))
        self.assertIn(rid, self.b.session.writes[-1])
        replied = self.c.reply_to_request(self.b.agent_id, tb, rid, "done")
        self.assertEqual(replied["state"], "replied")
        self.assertIn("done", self.a.session.writes[-1])

    def test_third_party_reply_rejected(self):
        from .support import FakeHarness

        ta, _tb = self._tokens()
        c = FakeHarness(name="gamma")
        self.c.register_agent(c)
        self.c.teams.add_member("crew", c.agent_id)
        tc = self.c._agent_token(c.agent_id)
        out = self.c.create_request(self.a.agent_id, ta, self.b.agent_id, "x", wait=False)
        rid = out["request"]["request_id"]
        with self.assertRaises(RequestError):
            self.c.reply_to_request(c.agent_id, tc, rid, "nope")

    def test_bad_token_rejected(self):
        from agent_terminal.messaging import MessagingError

        with self.assertRaises(MessagingError):
            self.c.create_request(self.a.agent_id, "wrong", self.b.agent_id, "x", wait=False)

    def test_wait_times_out(self):
        ta, _tb = self._tokens()
        out = self.c.create_request(self.a.agent_id, ta, self.b.agent_id, "x",
                                    timeout=0.1, wait=True)
        self.assertEqual(out["request"]["state"], "expired")

    def test_persistence_keeps_correlation(self):
        self.c._store = __import__("agent_terminal.persistence", fromlist=["StateStore"]).StateStore()
        ta, _tb = self._tokens()
        out = self.c.create_request(self.a.agent_id, ta, self.b.agent_id, "survive", wait=False)
        rid = out["request"]["request_id"]
        snap = self.c._store.snapshot(self.c)
        from agent_terminal import Controller

        other = Controller(adopt=False)
        other.requests.load(snap["requests"])
        self.assertIn(other.requests.get(rid).state, ("pending", "accepted"))


if __name__ == "__main__":
    unittest.main()
