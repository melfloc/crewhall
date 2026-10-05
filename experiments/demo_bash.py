from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crewhall import InteractiveSession, SessionSpec, available, get_backend


def main() -> None:
    for name in available():
        session = InteractiveSession(
            get_backend(name),
            SessionSpec(command=["/bin/bash"], cols=100, rows=30),
        )
        session.start()
        try:
            print(f"[{name}] session={session.session_id} pid={session.pid}")
            session.write("echo HELLO_$((40+2))")
            session.send_enter()
            out = session.read_until("HELLO_42", timeout=8)
            line = [l for l in out.splitlines() if "HELLO_42" in l][-1]
            print(f"[{name}] captured: {line!r}")
        finally:
            session.close()


if __name__ == "__main__":
    main()
