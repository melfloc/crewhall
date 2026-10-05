from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_terminal import Controller


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-b", "--backend", default="tmux" if shutil.which("tmux") else "pty")
    args = parser.parse_args()

    controller = Controller(adopt=False)
    a = controller.create_agent("opencode", name="a", backend=args.backend, cwd=os.getcwd())
    b = controller.create_agent("claude", name="b", backend=args.backend, cwd=os.getcwd())
    c = controller.create_agent("opencode", name="c", backend=args.backend, cwd=os.getcwd())
    t0 = time.time()

    def show(label: str, team) -> None:
        info = controller.team_info(team.team_id)
        names = [m["name"] for m in info["members"]]
        print(f"[{time.time() - t0:5.1f}s] {label}: members={names} missing={info['missing']}")

    try:
        a.start(timeout=40)
        b.start(timeout=40)
        c.start(timeout=40)

        team = controller.create_team("research", ["a", "b", "c"])
        show("create research [a,b,c]", team)

        team = controller.remove_team_member("research", "b")
        show("remove_member b", team)

        delivery = controller.send_message("a", "c", "TEAM_DEMO_A_TO_C")
        print(f"[{time.time() - t0:5.1f}s] a -> c delivered={delivery.delivered}")

        team = controller.add_team_member("research", "b")
        show("add_member b", team)

        controller.remove_team(team.team_id)
        print(
            f"[{time.time() - t0:5.1f}s] remove_team: agents still alive="
            f"{[controller.get_agent(n).state().value for n in ('a', 'b', 'c')]}"
        )
    finally:
        controller.shutdown()


if __name__ == "__main__":
    main()
