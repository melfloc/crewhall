"""Unit tests for the terminal WebSocket protocol and the output hub."""
from __future__ import annotations

import collections
import socket
import threading
import time
import unittest

from crewhall import terminal_stream as ts
from crewhall.web import ws


class WsFrameTest(unittest.TestCase):
    def _masked(self, payload: bytes, opcode: int = ws.OP_BINARY, fin: bool = True) -> bytes:
        mask = b"\x01\x02\x03\x04"
        header = bytearray([(0x80 if fin else 0) | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bytes(header) + mask + masked

    def test_binary_roundtrip(self):
        frame = ws.encode_binary(b"\x02hello")
        self.assertEqual(frame[0], 0x82)
        decoded = ws.decode_frame_full(self._masked(b"\x02hello"))
        self.assertEqual(decoded.payload, b"\x02hello")
        self.assertTrue(decoded.fin)

    def test_ping_pong_encoding(self):
        self.assertEqual(ws.encode_ping(b"")[0], 0x89)
        self.assertEqual(ws.encode_pong(b"")[0], 0x8A)

    def test_unmasked_client_frame_is_protocol_error(self):
        data = b"\x82\x03abc"  # binary, not masked
        with self.assertRaises(ws.FrameError) as ctx:
            ws.decode_frame_full(data)
        self.assertEqual(ctx.exception.code, ws.CLOSE_PROTOCOL)

    def test_oversize_is_1009(self):
        with self.assertRaises(ws.FrameError) as ctx:
            ws.decode_frame_full(self._masked(b"x" * 100), max_size=10)
        self.assertEqual(ctx.exception.code, ws.CLOSE_TOO_BIG)

    def test_reserved_bits_rejected(self):
        data = bytearray(self._masked(b"hi"))
        data[0] |= 0x40
        with self.assertRaises(ws.FrameError):
            ws.decode_frame_full(bytes(data))

    def test_frame_reader_detects_fragmentation(self):
        a, b = socket.socketpair()
        try:
            reader = ws.FrameReader(a, max_size=100, timeout=1.0)
            b.sendall(self._masked(b"part", fin=False))
            frame = reader.read()
            self.assertFalse(frame.fin)
        finally:
            a.close()
            b.close()

    def test_frame_reader_reassembles_partial(self):
        a, b = socket.socketpair()
        try:
            reader = ws.FrameReader(a, max_size=1000, timeout=1.0)
            data = self._masked(b"hello world")
            b.sendall(data[:3])
            threading.Timer(0.05, lambda: b.sendall(data[3:])).start()
            frame = reader.read()
            self.assertEqual(frame.payload, b"hello world")
        finally:
            a.close()
            b.close()

    def test_frame_reader_timeout(self):
        a, b = socket.socketpair()
        try:
            reader = ws.FrameReader(a, max_size=100, timeout=0.1)
            with self.assertRaises(TimeoutError):
                reader.read()
        finally:
            a.close()
            b.close()

    def test_legacy_parse_frame_still_works(self):
        opcode, flen, payload = ws.parse_frame(self._masked(b"x"))
        self.assertEqual((opcode, payload), (ws.OP_BINARY, b"x"))


class FakeSource:
    def __init__(self):
        self.q: collections.deque[bytes] = collections.deque()
        self.closed = False

    def feed(self, data: bytes) -> None:
        self.q.append(data)

    def read(self, n: int) -> bytes:
        while not self.q:
            if self.closed:
                return b""
            time.sleep(0.01)
        return self.q.popleft()

    def snapshot(self) -> bytes:
        return b"SNAP"

    def close(self) -> None:
        self.closed = True


def _hub(readonly=False, **kw):
    hub = ts.TerminalHub("term_deadbeef", pane="%1", tmux_session="s",
                         socket="at_test", **kw)
    hub.readonly = readonly
    src = FakeSource()
    hub._make_source = lambda: src
    return hub, src


class HubTest(unittest.TestCase):
    def test_enqueue_queue_full_drops_that_client(self):
        c = ts.HubClient(lambda d: None, queue_bytes=10, max_bytes_per_sec=1000,
                         autostart=False)
        self.assertTrue(c.enqueue(b"123456"))
        self.assertFalse(c.enqueue(b"123456"))  # 12 > 10

    def test_enqueue_rate_cap(self):
        c = ts.HubClient(lambda d: None, queue_bytes=10 ** 9, max_bytes_per_sec=1000,
                         autostart=False)
        self.assertTrue(c.enqueue(b"x" * 1000))
        self.assertFalse(c.enqueue(b"x"))  # tokens exhausted

    def test_fanout_to_all_clients(self):
        hub, src = _hub()
        got_a, got_b = [], []
        a = ts.HubClient(got_a.append, queue_bytes=10 ** 6, max_bytes_per_sec=10 ** 7)
        b = ts.HubClient(got_b.append, queue_bytes=10 ** 6, max_bytes_per_sec=10 ** 7)
        hub.add_client(a)
        hub.add_client(b)
        src.feed(b"hello")
        deadline = time.monotonic() + 2
        while (not got_a or not got_b) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(got_a, [b"hello"])
        self.assertEqual(got_b, [b"hello"])
        hub.stop()

    def test_slow_client_dropped_others_keep_receiving(self):
        hub, src = _hub()
        slow_got = []

        def slow_send(data):
            slow_got.append(data)
            time.sleep(10)  # never drains

        fast_got = []
        slow = ts.HubClient(slow_send, queue_bytes=8, max_bytes_per_sec=10 ** 9)
        fast = ts.HubClient(fast_got.append, queue_bytes=10 ** 7, max_bytes_per_sec=10 ** 9)
        hub.add_client(slow)
        hub.add_client(fast)
        for _ in range(50):
            src.feed(b"0123456789")
        deadline = time.monotonic() + 3
        while slow.dropped_code is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(slow.dropped_code, ws.CLOSE_SLOW)
        self.assertTrue(fast_got, "the fast client kept receiving")
        self.assertNotIn(slow, hub.clients)
        hub.stop()

    def test_single_writer_and_claim(self):
        hub, _src = _hub()
        a = ts.HubClient(lambda d: None, queue_bytes=1000, max_bytes_per_sec=1000)
        b = ts.HubClient(lambda d: None, queue_bytes=1000, max_bytes_per_sec=1000)
        hub.add_client(a)
        self.assertEqual(hub.mode_for_new_client(a), "write")
        hub.add_client(b)
        self.assertEqual(hub.mode_for_new_client(b), "read")
        notes = []
        a.control_cb = notes.append
        hub.claim(b)
        self.assertEqual(hub.writer, b)
        self.assertEqual(b.mode, "write")
        self.assertEqual(a.mode, "read")
        self.assertTrue(notes and notes[0].get("mode") == "read")
        # The writer leaving frees the keyboard.
        hub.remove_client(b)
        self.assertIsNone(hub.writer)

    def test_readonly_never_grants_write(self):
        hub, _src = _hub(readonly=True)
        a = ts.HubClient(lambda d: None, queue_bytes=1000, max_bytes_per_sec=1000)
        hub.add_client(a)
        self.assertEqual(hub.mode_for_new_client(a), "read")
        hub.claim(a)
        self.assertIsNone(hub.writer)


class InputPumpTest(unittest.TestCase):
    def test_coalesces_within_window(self):
        sent = []
        pump = ts.InputPump(sent.append)
        pump.feed(b"a")
        pump.feed(b"b")
        pump.feed(b"c")
        deadline = time.monotonic() + 2
        while not sent and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(sent, [b"abc"])


if __name__ == "__main__":
    unittest.main()
