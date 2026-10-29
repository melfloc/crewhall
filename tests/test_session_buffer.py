"""The session ``read`` buffer must stay bounded (daemon memory growth)."""
from __future__ import annotations

import shutil
import time
import unittest
from unittest import mock

from crewhall import InteractiveSession, SessionSpec, get_backend
from crewhall.session import _output_buffer_bytes

from .support import BrokenBackend


class OutputBuffer(unittest.TestCase):
    LIMIT = 64 * 1024

    def _session(self) -> InteractiveSession:
        with mock.patch("crewhall.session._output_buffer_bytes", return_value=self.LIMIT):
            return InteractiveSession(BrokenBackend(), SessionSpec(command=["/bin/true"]))

    def test_appended_output_is_capped_and_keeps_the_tail(self):
        session = self._session()
        for index in range(50):
            session._emit_output(f"chunk-{index:03d} " + "x" * 4000)
        self.assertLessEqual(session.buffered_bytes, self.LIMIT)
        out = session.read()
        self.assertLessEqual(len(out.encode("utf-8")), self.LIMIT)
        self.assertIn("chunk-049", out)

    def test_one_oversized_chunk_keeps_only_its_tail(self):
        session = self._session()
        session._emit_output("HEAD " + "x" * (self.LIMIT * 2) + " THE-END")
        self.assertEqual(session.buffered_bytes, self.LIMIT)
        out = session.read()
        self.assertTrue(out.endswith("THE-END"))
        self.assertNotIn("HEAD", out)

    def test_read_clears_the_buffer(self):
        session = self._session()
        session._emit_output("hello")
        self.assertEqual(session.read(), "hello")
        self.assertEqual(session.buffered_bytes, 0)
        self.assertEqual(session.read(timeout=0), "")

    def test_default_limit_is_one_megabyte(self):
        self.assertGreaterEqual(_output_buffer_bytes(), 64 * 1024)


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class RealTmuxReader(unittest.TestCase):
    def test_continuous_output_stays_bounded(self):
        spec = SessionSpec(
            command=["/bin/bash", "-c",
                     "i=0; while true; do i=$((i+1)); "
                     "printf 'line %08d 0123456789012345678901234567890123456789\\n' \"$i\"; done"],
            cols=80, rows=24,
        )
        with mock.patch("crewhall.session._output_buffer_bytes", return_value=64 * 1024):
            session = InteractiveSession(get_backend("tmux"), spec)
            session.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and session.buffered_bytes < 32 * 1024:
                    time.sleep(0.1)
                self.assertGreater(session.buffered_bytes, 32 * 1024)
                self.assertLessEqual(session.buffered_bytes, 64 * 1024)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
