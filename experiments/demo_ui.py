from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_terminal.controller import Controller
from agent_terminal.ui import AppModel, DaemonControl, LocalControl, render_text

USABLE = ("ready", "waiting_input")


def select(model: AppModel, name: str) -> None:
    for index, row in enumerate(model.rows()):
        if row["kind"] == "agent" and name in row["label"]:
            model.cursor = index
            return


def wait_usable(model: AppModel, name: str, timeout: float = 90.0) -> str:
    deadline = time.monotonic() + timeout
    state = "unknown"
    while time.monotonic() < deadline:
        select(model, name)
        model.refresh()
        agent = model.selected_agent()
        state = agent["state"] if agent else "unknown"
        if state in USABLE:
            return state
        time.sleep(0.5)
    return state


def frame(model: AppModel) -> None:
    print("\n".join(render_text(model, 100, 26)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="embed a Controller (no daemon)")
    args = parser.parse_args()

    controller = Controller(adopt=False) if args.local else None
    control = LocalControl(controller) if controller is not None else DaemonControl()
    model = AppModel(control, cwd=os.getcwd(), async_ops=False)
    t0 = time.time()

    try:
        for name, kind in (("a", "opencode"), ("b", "claude"), ("c", "opencode")):
            model.create_agent(name, kind)
        model.refresh()
        print(f"[{time.time() - t0:5.1f}s] agents: {[a['name'] for a in model.agents]}")

        model.create_team("research", ["a", "b", "c"])
        model.refresh()
        print(f"[{time.time() - t0:5.1f}s] teams: {[t['name'] for t in model.teams]}")

        select(model, "a")
        model.send_to_selected("What is 17 times 3? Reply with just the number.")
        print(f"[{time.time() - t0:5.1f}s] a -> {wait_usable(model, 'a')}")

        select(model, "b")
        model.send_to_selected("What is 19 times 4? Reply with just the number.")
        print(f"[{time.time() - t0:5.1f}s] b -> {wait_usable(model, 'b')}")

        model.send_message("a", "b", "MSG_A_TO_B")
        model.send_message("b", "a", "MSG_B_TO_A")
        model.refresh()
        print(f"[{time.time() - t0:5.1f}s] activity messages: {len(model.activity)}")

        model.remove_members("research", ["b"])
        model.refresh()
        print(f"[{time.time() - t0:5.1f}s] after remove_member: "
              f"{[m['name'] for m in model.teams[0]['members']]}")
        model.add_members("research", ["b"])
        model.refresh()
        print(f"[{time.time() - t0:5.1f}s] after add_member: "
              f"{[m['name'] for m in model.teams[0]['members']]}")

        team_id = model.teams[0]["team_id"]
        model.remove_team(team_id)
        model.refresh()
        print(f"[{time.time() - t0:5.1f}s] after remove_team: teams={model.teams} "
              f"agents={[a['name'] for a in model.agents]}")
        frame(model)
    finally:
        if controller is not None:
            controller.shutdown()
        else:
            for name in ("a", "b", "c"):
                try:
                    control.remove_agent(name)
                except Exception:
                    pass
            try:
                control.client.call("shutdown")
            except Exception:
                pass


if __name__ == "__main__":
    main()
