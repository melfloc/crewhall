from __future__ import annotations

_CTRL = {
    "CTRL_A": "\x01",
    "CTRL_B": "\x02",
    "CTRL_C": "\x03",
    "CTRL_D": "\x04",
    "CTRL_E": "\x05",
    "CTRL_F": "\x06",
    "CTRL_G": "\x07",
    "CTRL_H": "\x08",
    "CTRL_K": "\x0b",
    "CTRL_L": "\x0c",
    "CTRL_N": "\x0e",
    "CTRL_O": "\x0f",
    "CTRL_P": "\x10",
    "CTRL_Q": "\x11",
    "CTRL_R": "\x12",
    "CTRL_S": "\x13",
    "CTRL_T": "\x14",
    "CTRL_U": "\x15",
    "CTRL_V": "\x16",
    "CTRL_W": "\x17",
    "CTRL_X": "\x18",
    "CTRL_Y": "\x19",
    "CTRL_Z": "\x1a",
}

PTY_KEYS: dict[str, str] = {
    "ENTER": "\r",
    "CR": "\r",
    "RETURN": "\r",
    "LF": "\n",
    "TAB": "\t",
    "SPACE": " ",
    "ESC": "\x1b",
    "ESCAPE": "\x1b",
    "BACKSPACE": "\x7f",
    "BS": "\x7f",
    "DELETE": "\x1b[3~",
    "INSERT": "\x1b[2~",
    "UP": "\x1b[A",
    "DOWN": "\x1b[B",
    "RIGHT": "\x1b[C",
    "LEFT": "\x1b[D",
    "HOME": "\x1b[H",
    "END": "\x1b[F",
    "PAGE_UP": "\x1b[5~",
    "PAGE_DOWN": "\x1b[6~",
    **_CTRL,
}

_TMUX_NAMED = {
    "ENTER": "Enter",
    "CR": "Enter",
    "RETURN": "Enter",
    "LF": "Enter",
    "TAB": "Tab",
    "SPACE": "Space",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "BACKSPACE": "BSpace",
    "BS": "BSpace",
    "DELETE": "DC",
    "INSERT": "IC",
    "UP": "Up",
    "DOWN": "Down",
    "RIGHT": "Right",
    "LEFT": "Left",
    "HOME": "Home",
    "END": "End",
    "PAGE_UP": "PageUp",
    "PAGE_DOWN": "PageDown",
}


def canonical(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("key must be a non-empty string")
    n = name.strip()
    if n.startswith("^") and len(n) == 2:
        return "CTRL_" + n[1].upper()
    if len(n) >= 2 and n[0] in "Cc" and n[1] in "-_":
        return "CTRL_" + n[2:].upper()
    return n.upper().replace("-", "_")


def pty_bytes(name: str) -> bytes:
    raw = name if _is_literal(name) else PTY_KEYS.get(canonical(name))
    if raw is None:
        raise KeyError(f"unknown key for pty backend: {name!r}")
    return raw.encode("utf-8")


def tmux_name(name: str) -> str:
    canon = canonical(name)
    if canon in _TMUX_NAMED:
        return _TMUX_NAMED[canon]
    if canon.startswith("CTRL_") and len(canon) == 6:
        return "C-" + canon[-1].lower()
    raise KeyError(f"unknown key for tmux backend: {name!r}")


def _is_literal(name: str) -> bool:
    return "\x1b" in name or "\x00" in name
