from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crewhall import Controller


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
        print(f"[{time.time() - t0:5.1f}s] A kind={a.info().kind} | B kind={b.info().kind}")

        d1 = controller.send_message("A", "B", "MESSAGE_FROM_A_123")
        print(f"[{time.time() - t0:5.1f}s] A -> B delivered={d1.delivered} id={d1.message.message_id}")

        d2 = controller.send_message("B", "A", "MESSAGE_FROM_B_456")
        print(f"[{time.time() - t0:5.1f}s] B -> A delivered={d2.delivered} id={d2.message.message_id}")

        time.sleep(1.0)
        cap_a, cap_b = a.capture(), b.capture()
        print("A received MESSAGE_FROM_B_456:", "MESSAGE_FROM_B_456" in cap_a)
        print("B received MESSAGE_FROM_A_123:", "MESSAGE_FROM_A_123" in cap_b)
        print("A leaked MESSAGE_FROM_A_123  :", "MESSAGE_FROM_A_123" in cap_a)
        print("B leaked MESSAGE_FROM_B_456  :", "MESSAGE_FROM_B_456" in cap_b)

        print("--- history ---")
        for m in controller.message_history():
            sender = m.get("sender_name") or m["sender"]
            recipient = m.get("recipient_name") or m["recipient"]
            print(f"  {m['message_id']} {sender} -> {recipient} delivered={m['delivered']} {m['body']!r}")
    finally:
        a.stop()
        b.stop()
        controller.shutdown()


if __name__ == "__main__":
    main()
