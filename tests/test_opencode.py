from __future__ import annotations

import os
import shutil
import time
import unittest

from crewhall import InteractiveSession, SessionSpec, get_backend
from crewhall import paths

RUN = os.environ.get("AT_RUN_OPENCODE") == "1"
HAVE = shutil.which("opencode") is not None

BACKENDS = ["tmux"] if shutil.which("tmux") else ["pty"]


@unittest.skipUnless(RUN and HAVE, "set AT_RUN_OPENCODE=1 with opencode installed")
class OpenCodeInteractive(unittest.TestCase):
    def test_opencode_interactive_session(self) -> None:
        tmpdir = paths.usable_tmpdir()
        env = {"TMPDIR": tmpdir} if tmpdir else None
        spec = SessionSpec(
            command=["opencode"], cols=120, rows=40, cwd=os.getcwd(), env=env
        )
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                session = InteractiveSession(get_backend(backend), spec)
                session.start()
                try:
                    screen = self._wait_for_output(session, timeout=20)
                    self.assertTrue(screen.strip(), "opencode produced no screen output")
                    self.assertTrue(session.status.alive)

                    session.write("hello from crewhall")
                    time.sleep(0.5)
                    session.send_enter()
                    time.sleep(6)
                    after = session.capture()
                    self.assertIsInstance(after, str)
                finally:
                    session.close()

    @staticmethod
    def _wait_for_output(session, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            screen = session.capture()
            if screen.strip():
                return screen
            time.sleep(0.3)
        return session.capture()


if __name__ == "__main__":
    unittest.main()
