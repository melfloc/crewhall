from __future__ import annotations

import time

from .control import ControlPort
from .model import TERMINAL_STATES, AppModel, translate_sequence
from .view import cursor_position, render, session_viewport
import contextlib

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


# Interactive Focus repolls the agent's screen so what was typed shows up now
# instead of on the next full refresh. A local pane is cheap to capture; an
# ``ssh-tmux`` agent pays an SSH round trip, so it keeps the slower cadence.
LIVE_CAPTURE_LOCAL = 0.12
LIVE_CAPTURE_REMOTE = 0.6
# After a keystroke, refresh the echo if the last capture is older than this.
LIVE_CAPTURE_AFTER_INPUT = 0.04


def _live_interval(model: AppModel) -> float | None:
    if model.focus != "interactive":
        return None
    agent = model.selected_agent()
    if agent is None or agent["state"] in TERMINAL_STATES:
        return None
    return LIVE_CAPTURE_REMOTE if agent.get("backend") == "ssh-tmux" else LIVE_CAPTURE_LOCAL


def run(
    control: ControlPort,
    *,
    cwd: str | None = None,
    refresh_interval: float = 0.8,
    async_ops: bool = True,
) -> int:
    import curses

    model = AppModel(control, cwd=cwd, async_ops=async_ops)

    def _draw(stdscr, attrs) -> tuple[int, int]:
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
                with contextlib.suppress(curses.error):
                    stdscr.addstr(index, column, chunk, attrs.get(style, 0))
                column += len(chunk)
        position = cursor_position(model, width, height)
        with contextlib.suppress(curses.error):
            if position is None:
                curses.curs_set(0)
            else:
                stdscr.move(position[0], position[1])
                curses.curs_set(1)
        stdscr.refresh()
        return width, height

    def _handle_key(stdscr, key: int) -> None:
        if key != 27:
            mapped_key = _map_key(key)
            if mapped_key:
                model.handle_key(mapped_key)
            return
        # Read an escape sequence (Alt/Ctrl+Alt+<key>); Ctrl+Alt+B is the exit
        # combination, everything else is forwarded verbatim.
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
        stdscr.timeout(0)  # back to draining whatever else is buffered
        for mapped in translate_sequence(bytes(seq)):
            model.handle_key(mapped)

    def _loop(stdscr) -> None:
        curses.curs_set(0)
        curses.raw()  # keep Ctrl+C/D as keys so the agent can receive them
        stdscr.nodelay(True)
        stdscr.keypad(True)
        attrs = _init_colors()
        model.refresh()
        last_refresh = time.monotonic()
        last_live = 0.0
        live_for: str | None = None
        while not model.should_quit:
            # Apply a snapshot fetched by refresh_async, if one is ready.
            model.poll_refresh()
            now = time.monotonic()
            if now - last_refresh >= refresh_interval:
                if model.focus == "interactive":
                    # Polling every agent/team can take ~100 ms with a few
                    # agents; fetch it off-thread so typing never waits for it.
                    # The live capture below keeps the selected screen current.
                    model.refresh_async()
                else:
                    model.refresh()
                last_refresh = now
            live = _live_interval(model)
            agent = model.selected_agent()
            agent_key = agent["agent_id"] if live is not None and agent else None
            now = time.monotonic()
            if live is not None and (agent_key != live_for or now - last_live >= live):
                model.refresh_capture()
                last_live = time.monotonic()
                live_for = agent_key
            elif live is None:
                live_for = None

            width, height = _draw(stdscr, attrs)

            # Block until a key arrives or the next poll is due. The old loop
            # slept 20 ms between keys and forwarded each one with a blocking
            # RPC: typing could not go faster than the daemon's per-key work.
            now = time.monotonic()
            due = last_refresh + refresh_interval
            if live is not None:
                due = min(due, last_live + live)
            wait = max(0.001, min(0.25, due - now))
            stdscr.timeout(max(1, int(wait * 1000)))
            key = stdscr.getch()
            if key == -1:
                continue
            stdscr.timeout(0)
            while key != -1 and not model.should_quit:
                _handle_key(stdscr, key)
                key = stdscr.getch()

            # Propagate the real session viewport to the focused agent's TUI
            # (also covers KEY_RESIZE: dimensions change -> resize is sent).
            cols, rows = session_viewport(model, width, height)
            if cols >= 10 and rows >= 2:
                model.sync_viewport(cols, rows)

            # A keystroke should be visible as soon as the agent echoes it.
            if model.focus == "interactive":
                local = _live_interval(model) == LIVE_CAPTURE_LOCAL
                if local and time.monotonic() - last_live >= LIVE_CAPTURE_AFTER_INPUT:
                    model.refresh_capture()
                    last_live = time.monotonic()
                    current = model.selected_agent()
                    live_for = current["agent_id"] if current else None

    try:
        curses.wrapper(_loop)
    except KeyboardInterrupt:
        # Ctrl+C is a valid way to quit; exit cleanly (curses already restored).
        return 0
    finally:
        model.close()
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
