"""UI key handling: environment escape sequences and raw bytes -> UI keys."""
from __future__ import annotations

from .. import brand

# UI key name -> backend key name (keys.py canonical) for Interactive Focus.
# Ctrl+B is streamed to the agent (it is tmux's default prefix; crewhall
# must not consume it). The exit key is Ctrl+Alt+B (ESC + Ctrl+B).
KEY_FORWARD = {
    "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
    "home": "HOME", "end": "END", "pgup": "PAGE_UP", "pgdn": "PAGE_DOWN",
    "tab": "TAB", "enter": "ENTER", "backspace": "BACKSPACE", "delete": "DELETE",
    "esc": "ESC", "ctrl_b": "CTRL_B", "ctrl_c": "CTRL_C", "ctrl_d": "CTRL_D",
    "ctrl_p": "CTRL_P",
}

# Ctrl+Alt+B is delivered by terminals/tmux as ESC followed by Ctrl+B (0x02).
DEFAULT_EXIT_SEQUENCE = b"\x1b\x02"
# Ctrl+Alt+J / Ctrl+Alt+K are ESC followed by the letter.
DEFAULT_PREV_SEQUENCE = b"\x1bJ"
DEFAULT_NEXT_SEQUENCE = b"\x1bK"

_CONTROL_CHARS = {2: "ctrl_b", 3: "ctrl_c", 4: "ctrl_d", 16: "ctrl_p"}


def _env_sequence(name: str, default: bytes) -> bytes:
    raw = brand.env(name)
    if not raw:
        return default
    try:
        return raw.encode("utf-8").decode("unicode_escape").encode("latin-1")
    except Exception:  # noqa: BLE001
        return default


def exit_sequence() -> bytes:
    return _env_sequence("EXIT_SEQUENCE", DEFAULT_EXIT_SEQUENCE)


def prev_sequence() -> bytes:
    return _env_sequence("PREV_SEQUENCE", DEFAULT_PREV_SEQUENCE)


def next_sequence() -> bytes:
    return _env_sequence("NEXT_SEQUENCE", DEFAULT_NEXT_SEQUENCE)


def translate_sequence(data: bytes) -> list[str]:
    """Map a raw byte sequence to UI keys.

    Special Ctrl+Alt combinations become named UI actions
    (``exit_interactive`` / ``interactive_prev`` / ``interactive_next``);
    everything else is forwarded verbatim (ESC included).
    """
    if not data:
        return []
    for seq, name in (
        (exit_sequence(), "exit_interactive"),
        (prev_sequence(), "interactive_prev"),
        (next_sequence(), "interactive_next"),
    ):
        if seq and data[: len(seq)] == seq:
            rest = data[len(seq):]
            rest_keys = [k for b in rest if (k := _byte_key(b)) is not None]
            return [name, *rest_keys]
    return [k for k in (_byte_key(b) for b in data) if k]


def _byte_key(byte: int) -> str | None:
    if byte == 27:
        return "esc"
    if byte == 9:
        return "tab"
    if byte in (10, 13):
        return "enter"
    if byte in (8, 127):
        return "backspace"
    if byte in _CONTROL_CHARS:
        return _CONTROL_CHARS[byte]
    if byte == 0:
        return "ctrl_space"
    if 32 <= byte < 127:
        return chr(byte)
    return None
