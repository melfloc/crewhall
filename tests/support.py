from __future__ import annotations

import itertools
import shutil

from agent_terminal import (
    AgentState,
    Harness,
    InteractiveSession,
    SessionSpec,
    Status,
    get_backend,
)
from agent_terminal.types import SessionInfo

BACKENDS = ["pty"] + (["tmux"] if shutil.which("tmux") else [])

_ids = itertools.count(1)


class FakeBackend:
    name = "pty"

    def close(self) -> None:
        pass


class FakeHarness(Harness):
    """Minimal Harness double that is always READY.

    It exercises the real base ``Harness.send`` (write + send_enter over the
    session) while removing agent-specific readiness/turn detection, which is
    exactly what a messaging test needs.
    """

    kind = "opencode"

    @classmethod
    def command(cls) -> list[str]:
        return ["opencode"]

    def __init__(self, name: str | None = None) -> None:
        super().__init__(FakeSession(screen="", name=name), name=name)

    def _detect(self, text: str) -> tuple[AgentState, str]:
        return AgentState.READY, "fake harness is always ready"


class FakeSession:
    """Duck-typed InteractiveSession used to test harnesses in isolation."""

    def __init__(self, screen: str = "", name: str | None = None) -> None:
        self.session_id = f"sess_fake{next(_ids):04d}"
        self.name = name
        self.screen = screen
        self.status = Status.RUNNING
        self.exit_code: int | None = None
        self.pid = 4242
        self.created_at = 1000.0
        self.last_input_at: float | None = None
        self.last_output_at: float | None = None
        self.backend = FakeBackend()
        self.writes: list[str] = []
        self.keys: list[str] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed = False
        self._events: list[dict] = []
        self.spec = SessionSpec(
            command=["fake"], cwd="/tmp", cols=120, rows=40, name=name
        )

    def resize(self, cols: int, rows: int) -> None:
        self.resizes.append((cols, rows))

    def _log(self, type_: str, data: dict | None = None) -> None:
        self._events.append(
            {"type": type_, "session_id": self.session_id, "at": 1002.0, "data": data or {}}
        )

    def events(self) -> list[dict]:
        return list(self._events)

    def write(self, text: str) -> None:
        self.writes.append(text)
        self.last_input_at = 1001.0

    def send_key(self, key: str) -> None:
        self.keys.append(key)
        self.last_input_at = 1001.0

    def send_enter(self) -> None:
        self.send_key("ENTER")

    def capture(self) -> str:
        return self.screen

    def read(self, timeout: float | None = None) -> str:
        return self.screen

    def read_until(self, pattern: str, timeout: float = 10.0, interval: float = 0.05) -> str:
        return self.screen

    def poll(self) -> Status:
        return self.status

    def wait_for_idle(self, quiet_ms: int = 800, timeout: float = 30.0) -> bool:
        return True

    def is_idle(self, quiet_ms: int = 800) -> bool:
        return True

    def terminate(self) -> None:
        self.status = Status.TERMINATED

    def kill(self) -> None:
        self.status = Status.TERMINATED

    def close(self) -> None:
        self.closed = True
        self.status = Status.TERMINATED

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            backend=self.backend.name,
            pid=self.pid,
            command="opencode",
            cwd="/tmp",
            status=self.status,
            created_at=self.created_at,
            cols=120,
            rows=40,
            name=self.name,
            exit_code=self.exit_code,
            last_input_at=self.last_input_at,
            last_output_at=self.last_output_at,
            exited_at=None,
            meta={},
        )


class ContractMixin:
    backend_name = "pty"

    def make_session(self, command=None, **kwargs) -> InteractiveSession:
        spec = SessionSpec(command=command or ["/bin/bash"], **kwargs)
        session = InteractiveSession(get_backend(self.backend_name), spec)
        session.start()
        self.addCleanup(session.close)
        return session

    def contract_flow(self) -> None:
        session = self.make_session()
        self.assertEqual(session.backend.name, self.backend_name)
        self.assertIn(session.status.value, ("running", "starting"))
        self.assertIsInstance(session.pid, int)

        session.write("echo CONTRACT_$((6*7))")
        session.send_enter()
        out = session.read_until("CONTRACT_42", timeout=8)
        self.assertIn("CONTRACT_42", out)

        session.resize(100, 33)
        session.write("stty size")
        session.send_enter()
        self.assertIn("33 100", session.read_until("33 100", timeout=8))

        session.write("exit 0")
        session.send_enter()
        self.assertEqual(session.wait(timeout=5), 0)
        self.assertFalse(session.status.alive)

    def test_create_session(self) -> None:
        session = self.make_session(name="worker")
        info = session.info()
        self.assertEqual(info.backend, self.backend_name)
        self.assertEqual(info.name, "worker")
        self.assertIsInstance(info.pid, int)
        self.assertIsInstance(info.created_at, float)
        self.assertTrue(info.status.alive)

    def test_write_input(self) -> None:
        session = self.make_session()
        session.write("echo TOKEN_$((1+1))")
        session.wait_for_idle(quiet_ms=400, timeout=5)
        self.assertNotIn("TOKEN_2", session.capture())
        session.send_enter()
        self.assertIn("TOKEN_2", session.read_until("TOKEN_2", timeout=8))

    def test_send_enter(self) -> None:
        session = self.make_session()
        session.write("printf 'ENTERED_%s\\n' yes")
        session.wait_for_idle(quiet_ms=400, timeout=5)
        self.assertNotIn("ENTERED_yes", session.capture())
        session.send_enter()
        self.assertIn("ENTERED_yes", session.read_until("ENTERED_yes", timeout=8))

    def test_capture_output(self) -> None:
        session = self.make_session()
        session.write("echo CAPTURED_ABC")
        session.send_enter()
        out = session.read_until("CAPTURED_ABC", timeout=8)
        self.assertIn("CAPTURED_ABC", out)
        self.assertIn("CAPTURED_ABC", session.capture())

    def test_process_exit(self) -> None:
        session = self.make_session(command=["/bin/bash", "-c", "exit 3"])
        self.assertEqual(session.wait(timeout=5), 3)
        self.assertEqual(session.status.value, "exited")
        self.assertEqual(session.info().exit_code, 3)

    def test_resize(self) -> None:
        session = self.make_session()
        session.resize(90, 30)
        self.assertEqual((session.cols, session.rows), (90, 30))
        session.write("stty size")
        session.send_enter()
        self.assertIn("30 90", session.read_until("30 90", timeout=8))

    def test_interrupt(self) -> None:
        session = self.make_session()
        session.write("sleep 30")
        session.send_enter()
        session.wait_for_idle(quiet_ms=400, timeout=5)
        session.interrupt()
        session.write("echo AFTER_INTERRUPT")
        session.send_enter()
        self.assertIn(
            "AFTER_INTERRUPT", session.read_until("AFTER_INTERRUPT", timeout=8)
        )

    def test_two_sessions(self) -> None:
        session_a = self.make_session(name="a")
        session_b = self.make_session(name="b")
        self.assertNotEqual(session_a.session_id, session_b.session_id)

        session_b.write("echo ONLY_IN_B")
        session_b.send_enter()
        self.assertIn("ONLY_IN_B", session_b.read_until("ONLY_IN_B", timeout=8))

        session_a.wait_for_idle(quiet_ms=400, timeout=5)
        self.assertNotIn("ONLY_IN_B", session_a.capture())

    def test_backend_contract(self) -> None:
        self.contract_flow()


class BrokenBackend:
    name = "broken"

    def bind(self, session):
        self.session = session

    def start(self, spec):
        pass

    def write(self, text):
        pass

    def send_key(self, key):
        pass

    def capture(self):
        return ""

    def resize(self, cols, rows):
        pass

    def interrupt(self):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass

    def poll(self):
        return None

    def pid(self):
        return 1234

    def meta(self):
        return {}

    def wait(self, timeout=None):
        return None

    def close(self):
        pass
