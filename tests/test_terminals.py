"""Unit tests for the terminal layer: validators, limits, readonly, labels."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import settings, terminals
from crewhall.backends import tmux as tmux_backend
from crewhall.controller import Controller
from crewhall.session import InteractiveSession
from crewhall.types import SessionSpec, Status


class FakeBackend:
    name = "tmux"

    def __init__(self):
        self.session = None
        self.text = ""
        self.keys = []
        self.resizes = []

    def bind(self, session):
        self.session = session

    def start(self, spec):
        pass

    def write(self, text):
        self.text += text

    def send_key(self, key):
        self.keys.append(key)

    def capture(self, escapes=False):
        return self.text

    def resize(self, cols, rows):
        self.resizes.append((cols, rows))

    def interrupt(self):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass

    def poll(self):
        return None

    def pid(self):
        return None

    def meta(self):
        return {}

    def close(self):
        pass


def _controller() -> Controller:
    ctrl = Controller(adopt=False, persist=False)
    ctrl._backend_for = lambda backend_name, host_name: FakeBackend()
    return ctrl


class ValidatorsTest(unittest.TestCase):
    def test_new_id_format(self):
        tid = terminals.new_terminal_id()
        self.assertTrue(terminals.TERMINAL_ID_RE.match(tid), tid)
        self.assertEqual(len(tid), len("term_") + 8)

    def test_is_terminal(self):
        self.assertTrue(terminals.is_terminal({"kind": "terminal"}))
        self.assertFalse(terminals.is_terminal({"kind": "session"}))
        self.assertTrue(terminals.is_terminal(SessionSpec(command="x", kind="terminal")))
        self.assertFalse(terminals.is_terminal(SessionSpec(command="x")))
        s = InteractiveSession(FakeBackend(), SessionSpec(command="x", kind="terminal"))
        self.assertTrue(terminals.is_terminal(s))
        self.assertFalse(terminals.is_terminal(None))

    def test_validate_host(self):
        self.assertIsNone(terminals.validate_host(None))
        self.assertIsNone(terminals.validate_host("local"))
        with self.assertRaises(ValueError):
            terminals.validate_host("does-not-exist")
        with self.assertRaises(ValueError):
            terminals.validate_host(5)

    def test_validate_cwd_local(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(terminals.validate_cwd(d, None), d)
        with self.assertRaises(ValueError):
            terminals.validate_cwd("/no/such/dir-ati", None)
        with self.assertRaises(ValueError):
            terminals.validate_cwd("relative/path", None)

    def test_validate_cwd_remote_is_syntax_only(self):
        self.assertEqual(
            terminals.validate_cwd("/srv/app", "somehost"), "/srv/app"
        )
        with self.assertRaises(Exception):
            terminals.validate_cwd("relative", "somehost")
        with self.assertRaises(Exception):
            terminals.validate_cwd("/a\nb", "somehost")

    def test_validate_shell(self):
        self.assertIsNone(terminals.validate_shell(None, None))
        path = terminals.validate_shell("bash", None)
        self.assertTrue(os.path.isabs(path))
        with self.assertRaises(ValueError):
            terminals.validate_shell("nc", None)
        with self.assertRaises(ValueError):
            terminals.validate_shell("bash", "host")
        # $SHELL falls back to bash when not in the whitelist.
        with mock.patch.dict(os.environ, {"SHELL": "/usr/bin/nc"}):
            self.assertTrue(terminals.validate_shell("$SHELL", None).endswith("bash"))

    def test_validate_title(self):
        self.assertIsNone(terminals.validate_title(None))
        self.assertEqual(terminals.validate_title("build 1"), "build 1")
        for bad in ("-leading", "bad$char", "x" * 41, "with\nnewline"):
            with self.assertRaises(ValueError):
                terminals.validate_title(bad)

    def test_validate_command(self):
        self.assertIsNone(terminals.validate_command(None))
        self.assertEqual(terminals.validate_command("echo hi"), "echo hi")
        with self.assertRaises(ValueError):
            terminals.validate_command("x" * 5000)
        with self.assertRaises(ValueError):
            terminals.validate_command("a\0b")

    def test_validate_readonly(self):
        self.assertTrue(terminals.validate_readonly(True))
        self.assertFalse(terminals.validate_readonly(False))
        with self.assertRaises(ValueError):
            terminals.validate_readonly("yes")

    def test_validate_terminal_id(self):
        self.assertEqual(
            terminals.validate_terminal_id("term_deadbeef"), "term_deadbeef"
        )
        for bad in ("sess_1", "term_XYZ", "term_123", "../term_deadbeef"):
            with self.assertRaises(ValueError):
                terminals.validate_terminal_id(bad)


class ControllerTerminalTest(unittest.TestCase):
    def setUp(self):
        settings.patch({"terminals.enabled": True}, confirm=True)
        self.ctrl = _controller()

    def tearDown(self):
        settings.patch({"terminals.enabled": False})

    def test_disabled_blocks_create(self):
        settings.patch({"terminals.enabled": False})
        with self.assertRaises(terminals.TerminalError):
            self.ctrl.create_terminal()

    def test_create_and_limits(self):
        first = self.ctrl.create_terminal()
        self.assertEqual(terminals.KIND_TERMINAL, first.spec.kind)
        self.assertEqual(len(self.ctrl.list_terminals()), 1)
        settings.patch({"terminals.max_total": 2})
        self.ctrl.create_terminal()
        with self.assertRaises(ValueError):
            self.ctrl.create_terminal()
        settings.patch({"terminals.max_total": 8})

    def test_max_per_host(self):
        settings.patch({"terminals.max_per_host": 1})
        self.ctrl.create_terminal()
        with self.assertRaises(ValueError):
            self.ctrl.create_terminal()
        settings.patch({"terminals.max_per_host": 4})

    def test_readonly_guard(self):
        t = self.ctrl.create_terminal(readonly=True)
        with self.assertRaises(terminals.TerminalError):
            self.ctrl.guard_terminal_input(t)
        t2 = self.ctrl.create_terminal()
        self.ctrl.guard_terminal_input(t2)  # no raise

    def test_not_a_terminal(self):
        spec = SessionSpec(command="sleep 5")
        session = InteractiveSession(FakeBackend(), spec, session_id="sess_abcdef")
        self.ctrl.registry.add(session)
        with self.assertRaises(terminals.TerminalError):
            self.ctrl.terminal_info("sess_abcdef")

    def test_raw_list_excludes_terminals(self):
        self.ctrl.create_terminal()
        self.assertEqual(self.ctrl.list("session"), [])
        self.assertEqual(len(self.ctrl.list("all")), 1)
        self.assertEqual(len(self.ctrl.list("terminal")), 1)

    def test_terminal_absent_from_agent_list(self):
        self.ctrl.create_terminal()
        ids = {a["agent_id"] for a in self.ctrl.list_agents()}
        self.assertNotIn(self.ctrl.list_terminals()[0]["session_id"], ids)

    def test_close(self):
        t = self.ctrl.create_terminal()
        closed = self.ctrl.close_terminal(t.session_id)
        self.assertEqual(closed, t.session_id)
        self.assertEqual(self.ctrl.list_terminals(), [])

    def test_prune_exited_terminals(self):
        import time as _t

        t = self.ctrl.create_terminal()
        session = self.ctrl.get(t.session_id)
        session._status = Status.EXITED
        session._exited_at = _t.time() - 100
        # keep=0 keeps forever; a positive window past the exit removes it.
        self.assertEqual(self.ctrl.prune_terminals(0), 0)
        self.assertEqual(self.ctrl.prune_terminals(10), 1)
        self.assertEqual(self.ctrl.list_terminals(), [])

    def test_prune_ignores_live_and_agents(self):
        self.ctrl.create_terminal()
        spec = SessionSpec(command="sleep 5")
        raw = InteractiveSession(FakeBackend(), spec, session_id="sess_abcdef")
        self.ctrl.registry.add(raw)
        self.assertEqual(self.ctrl.prune_terminals(1), 0)  # terminal alive, agent untouched
        self.assertIsNotNone(self.ctrl.registry.resolve("sess_abcdef"))


class TerminalPersistenceTest(unittest.TestCase):
    def setUp(self):
        settings.patch({"terminals.enabled": True}, confirm=True)
        self.dir = tempfile.mkdtemp(prefix="at-term-persist-")

    def tearDown(self):
        settings.patch({"terminals.enabled": False})
        shutil.rmtree(self.dir, ignore_errors=True)

    def _store(self):
        from crewhall.persistence import StateStore

        return StateStore(path=os.path.join(self.dir, "state.json"))

    def test_terminals_persist_and_restore(self):
        store = self._store()
        ctrl = _controller()
        ctrl._store = store
        t = ctrl.create_terminal(title="keepme", readonly=True)
        ctrl._persist()
        saved = store.load()["terminals"]
        self.assertEqual([x["session_id"] for x in saved], [t.session_id])
        self.assertEqual(saved[0]["title"], "keepme")
        self.assertTrue(saved[0]["readonly"])

        # A fresh controller restores it (same id, title, readonly).
        ctrl2 = _controller()
        ctrl2._store = store
        ctrl2.restore()
        restored = ctrl2.list_terminals()
        self.assertEqual([x["session_id"] for x in restored], [t.session_id])
        self.assertEqual(restored[0]["title"], "keepme")
        self.assertTrue(restored[0]["readonly"])

    def test_closing_removes_it_from_persistence(self):
        store = self._store()
        ctrl = _controller()
        ctrl._store = store
        t = ctrl.create_terminal()
        ctrl.close_terminal(t.session_id)
        self.assertEqual(store.load()["terminals"], [])


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        settings.patch({"terminals.enabled": True}, confirm=True)

    def tearDown(self):
        settings.patch({"terminals.enabled": False})

    def test_expired_tokens_are_purged(self):
        from crewhall.web import terminal_tokens

        live, _live_rec = terminal_tokens.issue("live", scope="read", ttl=3600)
        stale, stale_rec = terminal_tokens.issue("stale", scope="read", ttl=-1)
        ids = {t["id"] for t in terminal_tokens.list_tokens()}
        self.assertIn(_live_rec["id"], ids)
        self.assertNotIn(stale_rec["id"], ids)          # expired: purged from the list
        self.assertTrue(terminal_tokens.verify(live))
        self.assertIsNone(terminal_tokens.verify(stale))
        stored = open(terminal_tokens.store_path(), encoding="utf-8").read()
        self.assertNotIn(stale_rec["id"], stored)        # and from the file
        terminal_tokens.revoke(_live_rec["id"])


class BackendLabelTest(unittest.TestCase):
    def test_tmux_writes_labels(self):
        backend = tmux_backend.TmuxBackend(socket="at_labels")
        spec = SessionSpec(
            command="", kind="terminal", readonly=True, title="build", owner="tok1"
        )
        with mock.patch.object(backend, "_run") as run:
            backend._set_terminal_labels(spec, "sess_1")
        calls = [c.args for c in run.call_args_list]
        self.assertIn(("set-option", "-t", "sess_1", "@at_kind", "terminal"), calls)
        self.assertIn(("set-option", "-t", "sess_1", "@at_readonly", "1"), calls)
        self.assertIn(("set-option", "-t", "sess_1", "@at_title", "build"), calls)
        self.assertIn(("set-option", "-t", "sess_1", "@at_owner", "tok1"), calls)

    def test_existing_sessions_reads_labels(self):
        def fake_run(args, **kwargs):
            class P:
                returncode = 0
                stdout = "term_deadbeef\n"
                stderr = ""

            return P()

        # list-sessions returns the name; show-options returns each label.
        values = {
            "@at_command": "bash",
            "@at_created": "1.0",
            "@at_backend": "tmux",
            "@at_kind": "terminal",
            "@at_readonly": "0",
            "@at_title": "t1",
            "@at_owner": "tok",
        }

        def fake_subprocess_run(argv, **kwargs):
            class P:
                returncode = 0
                stdout = ""
                stderr = ""

            p = P()
            if "list-sessions" in argv:
                p.stdout = "term_deadbeef\n"
            else:
                opt = argv[-1]
                p.stdout = values.get(opt, "")
            return p

        with mock.patch.object(tmux_backend, "tmux_available", return_value=True), \
             mock.patch("subprocess.run", side_effect=fake_subprocess_run):
            metas = tmux_backend.existing_sessions(socket="at_labels")
        self.assertEqual(metas[0]["kind"], "terminal")
        self.assertEqual(metas[0]["title"], "t1")
        self.assertEqual(metas[0]["owner"], "tok")


class NegativeInjectionTest(unittest.TestCase):
    """Client-controlled values must error, never execute anything."""

    MARK = "/tmp/ati-pwn"

    def tearDown(self):
        if os.path.exists(self.MARK):
            os.unlink(self.MARK)

    def test_bad_ids_never_reach_tmux(self):
        ctrl = _controller()
        settings.patch({"terminals.enabled": True}, confirm=True)
        try:
            for bad in ("$(touch /tmp/ati-pwn)", "; id", "../../x", "term_deadbeef;id",
                        "term_ZZZZZZZZ"):
                with self.assertRaises(Exception):
                    ctrl._resolve_terminal(bad)
                with self.assertRaises(ValueError):
                    terminals.validate_terminal_id(bad)
            self.assertFalse(os.path.exists(self.MARK))
        finally:
            settings.patch({"terminals.enabled": False})

    def test_shell_host_cwd_title_injection_rejected(self):
        with self.assertRaises(ValueError):
            terminals.validate_shell("bash; touch /tmp/ati-pwn", None)
        with self.assertRaises(ValueError):
            terminals.validate_host("-oProxyCommand=x")
        with self.assertRaises(ValueError):
            terminals.validate_host("$(touch /tmp/ati-pwn)")
        with self.assertRaises(ValueError):
            terminals.validate_cwd("$(touch /tmp/ati-pwn)", None)
        with self.assertRaises(ValueError):
            terminals.validate_cwd("../etc", None)
        with self.assertRaises(ValueError):
            terminals.validate_title("x$(touch /tmp/ati-pwn)")
        with self.assertRaises(ValueError):
            terminals.validate_title("a\nb")
        self.assertFalse(os.path.exists(self.MARK))

    def test_shell_with_host_rejected(self):
        with self.assertRaises(ValueError):
            terminals.validate_shell("bash", "somehost")

    def test_command_is_literal_not_executed_by_daemon(self):
        settings.patch({"terminals.enabled": True}, confirm=True)
        try:
            ctrl = _controller()
            t = ctrl.create_terminal(command=f"touch {self.MARK}")
            backend = ctrl.get(t.session_id).backend
            self.assertEqual(backend.text, f"touch {self.MARK}")
            self.assertFalse(os.path.exists(self.MARK))
        finally:
            settings.patch({"terminals.enabled": False})


class DaemonOpTest(unittest.TestCase):
    def setUp(self):
        settings.patch({"terminals.enabled": True}, confirm=True)
        self.ctrl = _controller()
        from crewhall.daemon import Server

        self.server = Server(self.ctrl, "/tmp/ati-not-a-socket")

    def tearDown(self):
        settings.patch({"terminals.enabled": False})

    def test_ops_disabled(self):
        settings.patch({"terminals.enabled": False})
        for op, extra in (("terminal_create", {}), ("terminal_list", {}),
                          ("terminal_info", {"id": "term_deadbeef"}),
                          ("terminal_write", {"id": "term_deadbeef", "text": "x"}),
                          ("terminal_close", {"id": "term_deadbeef"})):
            r = self.server.dispatch({"op": op, **extra})
            self.assertFalse(r["ok"], op)
            self.assertIn("disabled", r["error"])

    def test_readonly_blocks_ops_and_raw(self):
        t = self.ctrl.create_terminal(readonly=True)
        tid = t.session_id
        for op, extra in (("terminal_write", {"id": tid, "text": "x"}),
                          ("terminal_key", {"id": tid, "key": "ENTER"}),
                          ("terminal_resize", {"id": tid, "cols": 80, "rows": 24})):
            r = self.server.dispatch({"op": op, **extra})
            self.assertFalse(r["ok"], op)
        for op in ("write", "key", "enter", "interrupt"):
            r = self.server.dispatch({"op": op, "target": tid, "text": "x", "key": "ENTER"})
            self.assertFalse(r["ok"], op)

    def test_write_literal_and_capture(self):
        t = self.ctrl.create_terminal()
        r = self.server.dispatch({"op": "terminal_write", "id": t.session_id,
                                  "text": "echo $((6*7))", "enter": True})
        self.assertTrue(r["ok"])
        self.assertEqual(r["written"], len("echo $((6*7))"))
        backend = self.ctrl.get(t.session_id).backend
        self.assertEqual(backend.text, "echo $((6*7))")
        self.assertIn("ENTER", backend.keys)
        cap = self.server.dispatch({"op": "terminal_capture", "id": t.session_id})
        self.assertTrue(cap["ok"])

    def test_oversized_write_rejected(self):
        t = self.ctrl.create_terminal()
        r = self.server.dispatch({"op": "terminal_write", "id": t.session_id,
                                  "text": "x" * 70000})
        self.assertFalse(r["ok"])
        self.assertIn("too large", r["error"])

    def test_audit_summary_has_no_text(self):
        from crewhall import audit

        t = self.ctrl.create_terminal()
        secret = "S3CR3T_abc123"
        summary = audit.summarize("terminal_write",
                                  {"id": t.session_id, "text": secret, "enter": True}, {})
        self.assertNotIn(secret, summary)
        self.assertIn("sha256:", summary)
        self.assertIn("bytes=", summary)


if __name__ == "__main__":
    unittest.main()
