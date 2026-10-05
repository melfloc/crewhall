from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_terminal import InteractiveSession, SessionSpec, get_backend


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-b", "--backend", default="tmux" if shutil.which("tmux") else "pty")
    parser.add_argument("-c", "--command", default="opencode")
    parser.add_argument("--message", default="Reply with exactly the single token: PONG_TEST")
    args = parser.parse_args()

    session = InteractiveSession(
        get_backend(args.backend),
        SessionSpec(command=[args.command], cols=120, rows=40),
    )
    session.start()
    try:
        print(f"session {session.session_id} pid={session.pid} backend={args.backend}")
        _wait_for_output(session, timeout=12)
        print("--- initial screen ---")
        print(session.capture()[-800:])

        session.write(args.message)
        time.sleep(0.6)
        print("message visible in TUI:", args.message.split()[-1] in session.capture())

        session.send_enter()
        _wait_for_change(session, timeout=45)
        print("--- screen after ENTER ---")
        print(session.capture()[-1000:])
    finally:
        session.close()


def _wait_for_output(session: InteractiveSession, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and not session.capture().strip():
        time.sleep(0.3)


def _wait_for_change(session: InteractiveSession, timeout: float) -> None:
    baseline = len(session.capture())
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        if len(session.capture()) > baseline + 30:
            return


if __name__ == "__main__":
    main()
