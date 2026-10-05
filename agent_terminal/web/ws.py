from __future__ import annotations

import base64
import hashlib
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_text(payload: str) -> bytes:
    data = payload.encode("utf-8")
    header = bytearray([0x81])
    length = len(data)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + data


def encode_close() -> bytes:
    return b"\x88\x00"


def decode_frame(data: bytes) -> tuple[str | None, int, int, bytes]:
    """Return (opcode, payload_len, frame_len, unmasked_payload).

    Only client-to-server frames (masked) are supported, which is all a browser
    sends. Returns opcode 8 (close) on malformed/short data.
    """
    if len(data) < 2:
        return (None, 0, 0, b"")
    b0, b1 = data[0], data[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    idx = 2
    if length == 126:
        if len(data) < 4:
            return (None, 0, 0, b"")
        length = struct.unpack(">H", data[2:4])[0]
        idx = 4
    elif length == 127:
        if len(data) < 10:
            return (None, 0, 0, b"")
        length = struct.unpack(">Q", data[2:10])[0]
        idx = 10
    mask = b""
    if masked:
        if len(data) < idx + 4:
            return (None, 0, 0, b"")
        mask = data[idx : idx + 4]
        idx += 4
    frame_len = idx + length
    if len(data) < frame_len:
        return (None, 0, 0, b"")
    payload = bytearray(data[idx:frame_len])
    if masked:
        for i in range(len(payload)):
            payload[i] ^= mask[i % 4]
    return (opcode, length, frame_len, bytes(payload))


def parse_frame(data: bytes) -> tuple[int | None, int, bytes]:
    opcode, _length, frame_len, payload = decode_frame(data)
    return (opcode, frame_len, payload)


__all__ = ["accept_key", "encode_text", "encode_close", "parse_frame", "decode_frame"]
