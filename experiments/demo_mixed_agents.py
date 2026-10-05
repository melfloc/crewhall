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
    parser.add_argument("--kind-a", default="opencode")
    parser.add_argument("--kind-b", default="claude")
    parser.add_argument("-b", "--backend", default="tmux" if shutil.which("tmux") else "pty")
    args = parser.parse_args()

    controller = Controller(adopt=False)
    a = controller.create_agent(args.kind_a, name="A", backend=args.backend, cwd=os.getcwd())
    b = controller.create_agent(args.kind_b, name="B", backend=args.backend, cwd=os.getcwd())
    t0 = time.time()
    try:
        a.start(timeout=40)
        b.start(timeout=40)
        print(
            f"[{time.time() - t0:5.1f}s] A={a.agent_id} kind={a.info().kind} | "
            f"B={b.agent_id} kind={b.info().kind} | backend={a.info().backend}"
        )

        a.send("What is 17 times 3? Reply with just the number.")
        b.send("What is 19 times 4? Reply with just the number.")

        a.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)
        b.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)

        cap_a, cap_b = a.capture(), b.capture()
        print(f"[{time.time() - t0:5.1f}s] {a.info().kind}: has 51={('51' in cap_a)} leaked 76={('76' in cap_a)}")
        print(f"[{time.time() - t0:5.1f}s] {b.info().kind}: has 76={('76' in cap_b)} leaked 51={('51' in cap_b)}")
        print(f"A state={a.state().value} | B state={b.state().value}")
    finally:
        a.stop()
        b.stop()
        controller.shutdown()


if __name__ == "__main__":
    main()
