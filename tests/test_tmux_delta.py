"""The tmux reader must emit only appended output, not the whole scrollback."""
from __future__ import annotations

import unittest
from unittest import mock

from crewhall.backends.tmux import TmuxBackend


class Delta(unittest.TestCase):
    def test_append(self):
        self.assertEqual(TmuxBackend._delta("one\n", "one\ntwo\n"), "two\n")

    def test_unchanged(self):
        self.assertEqual(TmuxBackend._delta("one\n", "one\n"), "")

    def test_sliding_window_returns_only_new_lines(self):
        previous = "".join(f"line {i:03d} " + "x" * 80 + "\n" for i in range(200))
        current = "".join(previous.splitlines(keepends=True)[1:]) + "new line\n"
        self.assertEqual(TmuxBackend._delta(previous, current), "new line\n")

    def test_several_lines_dropped(self):
        previous = "".join(f"line {i}\n" for i in range(10))
        current = "".join(f"line {i}\n" for i in range(3, 10)) + "new\n"
        self.assertEqual(TmuxBackend._delta(previous, current), "new\n")

    def test_redraw_without_overlap_returns_the_pane(self):
        self.assertEqual(TmuxBackend._delta("old\n", "totally different\n"),
                         "totally different\n")

    def test_reader_captures_a_bounded_tail(self):
        backend = TmuxBackend(socket="test")
        backend.tmux_name = "sess"
        backend.pane_id = "%1"
        with mock.patch.object(backend, "_run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="tail")
            self.assertEqual(backend._capture_tail(), "tail")
        args = run.call_args.args
        self.assertEqual(args[0], "capture-pane")
        self.assertEqual(args[-2:], ("-S", f"-{TmuxBackend.READ_TAIL_LINES}"))


if __name__ == "__main__":
    unittest.main()
