from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_terminal import AgentState, Controller


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", "--kind", default="opencode")
    parser.add_argument("-b", "--backend", default="tmux" if shutil.which("tmux") else "pty")
    parser.add_argument("-n", "--name", default="solo")
    args = parser.parse_args()

    controller = Controller(adopt=False)
    agent = controller.create_agent(
        args.kind, name=args.name, backend=args.backend, cwd=os.getcwd()
    )
    t0 = time.time()

    def show(prefix: str) -> AgentState:
        state = agent.state()
        print(
            f"[{time.time() - t0:5.1f}s] {prefix:<22} state={state.value:<13} "
            f"evidence={agent.evidence()}"
        )
        return state

    try:
        show("after create")
        agent.start(timeout=40)
        show("after start()")
        print(f"[{time.time() - t0:5.1f}s] kind={agent.info().kind} id={agent.agent_id}")

        agent.send("What is 17 times 3? Reply with just the number.")
        print(f"[{time.time() - t0:5.1f}s] sent prompt; waiting for WORKING…")
        working = agent.wait_for_state(AgentState.WORKING, timeout=15)
        print(f"[{time.time() - t0:5.1f}s] observed               state={working.value}")

        agent.wait_for_state((AgentState.WAITING_INPUT, AgentState.READY), timeout=90)
        show("after work done")
        print("answer '51' present:", "51" in agent.capture())
    finally:
        agent.stop()
        controller.shutdown()


if __name__ == "__main__":
    main()
