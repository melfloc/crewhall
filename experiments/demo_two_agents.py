from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crewhall import AgentState, Controller


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-b", "--backend", default="tmux" if shutil.which("tmux") else "pty")
    args = parser.parse_args()

    controller = Controller(adopt=False)
    a = controller.create_agent("opencode", name="A", backend=args.backend, cwd=os.getcwd())
    b = controller.create_agent("opencode", name="B", backend=args.backend, cwd=os.getcwd())
    t0 = time.time()
    try:
        a.start(timeout=40)
        b.start(timeout=40)
        print(f"[{time.time() - t0:5.1f}s] A={a.agent_id} B={b.agent_id} both READY")

        a.send("What is 17 times 3? Reply with just the number.")
        b.send("What is 19 times 4? Reply with just the number.")

        a.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)
        b.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)

        cap_a = a.capture()
        cap_b = b.capture()
        print(f"[{time.time() - t0:5.1f}s] A answer 51 present: {'51' in cap_a}  (76 leaked: {'76' in cap_a})")
        print(f"[{time.time() - t0:5.1f}s] B answer 76 present: {'76' in cap_b}  (51 leaked: {'51' in cap_b})")
        print(f"A state={a.state().value}  B state={b.state().value}")
        print("--- A tail ---")
        print(a.capture_recent(max_lines=10))
        print("--- B tail ---")
        print(b.capture_recent(max_lines=10))
    finally:
        a.stop()
        b.stop()
        controller.shutdown()


if __name__ == "__main__":
    main()
