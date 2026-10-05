from __future__ import annotations

import time

from .control import ControlPort
from .model import AppModel, translate_sequence
from .view import render, session_viewport

_PAIRS = {
    "title": ("yellow", True),
    "border": ("blue", False),
    "muted": ("white", False),
    "primary": ("cyan", False),
    "success": ("green", False),
    "warning": ("yellow", False),
    "error": ("red", False),
    "working": ("magenta", False),
    "selected": ("cyan", True),
    "status": ("white", False),
    "modal": ("white", False),
    "input": ("cyan", False),
    "composer_focus": ("cyan", True),
    "normal": (None, False),
}
_COLORS = {"black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6, "white": 7}


def _map_key(key: int) -> str | None:
    import curses

    mapping = {
        curses.KEY_UP: "up",
        curses.KEY_DOWN: "down",
        curses.KEY_LEFT: "left",
        curses.KEY_RIGHT: "right",
        curses.KEY_PPAGE: "pgup",
        curses.KEY_NPAGE: "pgdn",
        curses.KEY_HOME: "home",
        curses.KEY_END: "end",
        getattr(curses, "KEY_DC", 330): "delete",
        3: "ctrl_c",
        4: "ctrl_d",
        16: "ctrl_p",
        9: "tab",
    }
    if key in mapping:
        return mapping[key]
    if key in (10, 13):
        return "enter"
    if key in (8, 127, getattr(curses, "KEY_BACKSPACE", 263)):
        return "backspace"
    if 0 <= key < 256 and chr(key).isprintable():
        return chr(key)
    return None


def _init_colors() -> dict[str, int]:
    import curses

    attrs = {name: 0 for name in _PAIRS}
    if not curses.has_colors():
        return attrs
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return attrs
    pair = 1
    for name, (color, bold) in _PAIRS.items():
        if color is None:
            attrs[name] = curses.A_BOLD if bold else curses.A_NORMAL
            continue
        try:
            curses.init_pair(pair, _COLORS[color], -1)
            attrs[name] = curses.color_pair(pair) | (curses.A_BOLD if bold else 0)
            pair += 1
        except curses.error:
            attrs[name] = curses.A_BOLD if bold else curses.A_NORMAL
    return attrs


def run(
    control: ControlPort,
    *,
    cwd: str | None = None,
    refresh_interval: float = 0.8,
    async_ops: bool = True,
) -> int:
    import curses

    def _loop(stdscr) -> None:
        curses.curs_set(0)
        curses.raw()  # keep Ctrl+C/D as keys so the agent can receive them
        stdscr.nodelay(True)
        stdscr.keypad(True)
        attrs = _init_colors()
        model = AppModel(control, cwd=cwd, async_ops=async_ops)
        model.refresh()
        last_refresh = time.monotonic()
        while not model.should_quit:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            for index, line in enumerate(render(model, width, height)):
                if index >= height:
                    break
                column = 0
                for text, style in line:
                    if column >= width - 1:
                        break
                    chunk = text[: width - 1 - column]
                    try:
                        stdscr.addstr(index, column, chunk, attrs.get(style, 0))
                    except curses.error:
                        pass
                    column += len(chunk)
            stdscr.refresh()

            key = stdscr.getch()
            if key != -1:
                if key == 27:
                    # Read an escape sequence (Alt/Ctrl+Alt+<key>); Ctrl+Alt+B
                    # is the exit combination, everything else is forwarded.
                    seq = bytearray([27])
                    stdscr.timeout(25)
                    while len(seq) < 6:
                        nxt = stdscr.getch()
                        if nxt == -1:
                            break
                        if 0 <= nxt < 256:
                            seq.append(nxt)
                        else:
                            break
                    stdscr.nodelay(True)
                    for mapped in translate_sequence(bytes(seq)):
                        model.handle_key(mapped)
                else:
                    mapped = _map_key(key)
                    if mapped == "quit":
                        break
                    if mapped:
                        model.handle_key(mapped)

            # Propagate the real session viewport to the focused agent's TUI
            # (also covers KEY_RESIZE: dimensions change -> resize is sent).
            cols, rows = session_viewport(model, width, height)
            if cols >= 10 and rows >= 2:
                model.sync_viewport(cols, rows)

            now = time.monotonic()
            if now - last_refresh >= refresh_interval:
                model.refresh()
                last_refresh = now
            time.sleep(0.02)

    try:
        curses.wrapper(_loop)
    except KeyboardInterrupt:
        # Ctrl+C is a valid way to quit; exit cleanly (curses already restored).
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="crewhall ui", description="Interactive TUI for Crewhall."
    )
    parser.add_argument("--local", action="store_true", help="embed a Controller (no daemon)")
    parser.add_argument("--cwd", default=None, help="working dir for created agents")
    parser.add_argument("--socket", default=None, help="daemon socket path")
    parser.add_argument("--refresh", type=float, default=0.8)
    args = parser.parse_args(argv)

    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()

    if args.local:
        from ..controller import Controller
        from .control import LocalControl

        control: ControlPort = LocalControl(Controller(adopt=False))
    else:
        from .control import DaemonControl

        control = DaemonControl(socket_path=args.socket)

    return run(control, cwd=cwd, refresh_interval=args.refresh, async_ops=True)
