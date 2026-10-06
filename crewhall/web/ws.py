from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Close codes used by the terminal WebSocket.
CLOSE_PROTOCOL = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_TOO_BIG = 1009
CLOSE_SLOW = 1013
CLOSE_GOING_AWAY = 1001

# Opcodes.
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def accept_key(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _header(opcode: int, length: int) -> bytearray:
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return header


def encode_text(payload: str) -> bytes:
    data = payload.encode("utf-8")
    return bytes(_header(OP_TEXT, len(data))) + data


def encode_binary(payload: bytes) -> bytes:
    return bytes(_header(OP_BINARY, len(payload))) + payload


def encode_ping(payload: bytes = b"") -> bytes:
    return bytes(_header(OP_PING, len(payload))) + payload


def encode_pong(payload: bytes = b"") -> bytes:
    return bytes(_header(OP_PONG, len(payload))) + payload


def encode_close(code: int | None = None, reason: str = "") -> bytes:
    if code is None:
        return bytes(_header(OP_CLOSE, 0))
    body = struct.pack(">H", int(code)) + reason.encode("utf-8")[:123]
    return bytes(_header(OP_CLOSE, len(body))) + body


class FrameError(Exception):
    """A frame the server refuses; ``code`` is the WebSocket close code."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message or f"websocket frame error {code}")
        self.code = code


@dataclass
class Frame:
    fin: bool
    opcode: int
    masked: bool
    payload: bytes
    frame_len: int


def decode_frame_full(data: bytes, max_size: int = 0) -> Frame:
    """Decode one complete frame, enforcing the Phase-2 rules.

    Raises ``FrameError`` with a close code on protocol violations:
    unmasked client frame / RSV set -> 1002, payload over ``max_size`` -> 1009.
    Fragmentation (FIN=0 or a continuation opcode) is left to the caller.
    """
    if len(data) < 2:
        raise FrameError(CLOSE_PROTOCOL, "short frame")
    b0, b1 = data[0], data[1]
    fin = bool(b0 & 0x80)
    if b0 & 0x70:
        raise FrameError(CLOSE_PROTOCOL, "reserved bits set")
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    idx = 2
    if length == 126:
        if len(data) < 4:
            raise FrameError(CLOSE_PROTOCOL, "short length")
        length = struct.unpack(">H", data[2:4])[0]
        idx = 4
    elif length == 127:
        if len(data) < 10:
            raise FrameError(CLOSE_PROTOCOL, "short length")
        length = struct.unpack(">Q", data[2:10])[0]
        idx = 10
    if length > (1 << 31):
        raise FrameError(CLOSE_TOO_BIG, "length too large")
    if max_size and length > max_size:
        raise FrameError(CLOSE_TOO_BIG, "message too big")
    if not masked:
        raise FrameError(CLOSE_PROTOCOL, "client frame not masked")
    if len(data) < idx + 4:
        raise FrameError(CLOSE_PROTOCOL, "short mask")
    mask = data[idx : idx + 4]
    idx += 4
    frame_len = idx + length
    if len(data) < frame_len:
        raise FrameError(CLOSE_PROTOCOL, "incomplete frame")
    payload = bytearray(data[idx:frame_len])
    for i in range(len(payload)):
        payload[i] ^= mask[i % 4]
    return Frame(fin=fin, opcode=opcode, masked=masked, payload=bytes(payload),
                 frame_len=frame_len)


class FrameReader:
    """Blocking reader that buffers partial frames and enforces the limits.

    Control frames (ping/pong/close) are returned too; fragmentation is
    rejected by ``read`` because the terminal protocol is one frame per message.
    """

    def __init__(self, sock, max_size: int, timeout: float | None = None) -> None:
        self.sock = sock
        self.max_size = max_size
        self.timeout = timeout
        self._buf = bytearray()

    def _fill(self) -> bool:
        if self.timeout is not None:
            self.sock.settimeout(self.timeout)
        chunk = self.sock.recv(65536)  # may raise TimeoutError
        if not chunk:
            return False
        self._buf += chunk
        return True

    def read(self) -> Frame | None:
        """Return the next frame, or None on EOF."""
        while True:
            if len(self._buf) >= 2:
                b1 = self._buf[1]
                length = b1 & 0x7F
                need = 2
                if length == 126:
                    need = 4
                elif length == 127:
                    need = 10
                if len(self._buf) >= need:
                    if length == 126:
                        length = struct.unpack(">H", self._buf[2:4])[0]
                    elif length == 127:
                        length = struct.unpack(">Q", self._buf[2:10])[0]
                    if length > (1 << 31):
                        raise FrameError(CLOSE_TOO_BIG, "length too large")
                    if self.max_size and length > self.max_size:
                        raise FrameError(CLOSE_TOO_BIG, "message too big")
                    masked = bool(b1 & 0x80)
                    total = need + (4 if masked else 0) + length
                    if len(self._buf) >= total:
                        frame = decode_frame_full(bytes(self._buf[:total]), self.max_size)
                        del self._buf[: total]
                        return frame
            if not self._fill():
                return None


# -- backwards-compatible helpers (state WebSocket) --------------------------
def decode_frame(data: bytes) -> tuple[str | None, int, int, bytes]:
    """Legacy decoder used by the state socket. Returns (opcode, len, flen, payload).

    Opcode is returned as ``int`` (or None on malformed input), matching the
    previous contract closely enough for the existing callers.
    """
    try:
        frame = decode_frame_full(data)
    except FrameError:
        return (None, 0, 0, b"")
    return (frame.opcode, len(frame.payload), frame.frame_len, frame.payload)


def parse_frame(data: bytes) -> tuple[int | None, int, bytes]:
    opcode, _length, frame_len, payload = decode_frame(data)
    return (opcode, frame_len, payload)


__all__ = [
    "accept_key", "encode_text", "encode_binary", "encode_ping", "encode_pong",
    "encode_close", "Frame", "FrameError", "FrameReader", "decode_frame",
    "decode_frame_full", "parse_frame",
    "CLOSE_PROTOCOL", "CLOSE_UNSUPPORTED", "CLOSE_TOO_BIG", "CLOSE_SLOW",
    "CLOSE_GOING_AWAY", "OP_CONT", "OP_TEXT", "OP_BINARY", "OP_CLOSE",
    "OP_PING", "OP_PONG",
]
