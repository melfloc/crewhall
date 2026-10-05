from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crewhall import InteractiveSession, SessionSpec, available, get_backend


def run(name: str) -> None:
    a = InteractiveSession(get_backend(name), SessionSpec(command=["/bin/bash"], name="A"))
    b = InteractiveSession(get_backend(name), SessionSpec(command=["/bin/bash"], name="B"))
    a.start()
    b.start()
    try:
        a.write('echo "ping-from-A"')
        a.send_enter()
        msg = _extract(a.read_until("ping-from-A", timeout=8), "ping-from-A")

        b.write(f'echo "B-received: {msg}"')
        b.send_enter()
        echoed = _extract(b.read_until("B-received", timeout=8), "B-received")

        b.write('echo "pong-from-B"')
        b.send_enter()
        pong = _extract(b.read_until("pong-from-B", timeout=8), "pong-from-B")

        a.write(f'echo "A-received: {pong}"')
        a.send_enter()
        back = _extract(a.read_until("A-received", timeout=8), "A-received")

        print(f"[{name}] A -> controller -> B : {b.session_id} got {msg!r}")
        print(f"[{name}] B -> controller -> A : {a.session_id} got {back!r}")
        assert "B-received" in echoed and "A-received" in back
    finally:
        a.close()
        b.close()


def _extract(text: str, marker: str) -> str:
    for line in text.splitlines():
        if marker in line and "echo" not in line:
            return line.strip()
    return marker


if __name__ == "__main__":
    for backend in available():
        run(backend)
