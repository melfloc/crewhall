#!/usr/bin/env python3
"""Run an isolated, deterministic agent-spawn + messaging demo and render a GIF.

The demo uses Crewhall's real daemon, tmux backend, spawn policy, and team
messaging path. A tiny local Claude-shaped fixture replaces the provider, so it
does not use a model, network access, or the user's credentials.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "images" / "agent-spawn-communication.gif"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import os
import subprocess
import sys

if "--version" in sys.argv:
    print("Claude Code v2.1.284")
    raise SystemExit(0)

name = os.environ.get("CREWHALL_AGENT_NAME", "Agent")
ready = "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"

def ready_screen():
    print("· done 1:00 PM")
    print("❯ ")
    print(ready, flush=True)

def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "crewhall", *args],
        text=True, capture_output=True, timeout=45,
    )
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip())
    return proc.returncode

print("Claude Code v2.1.284 · local demo provider")
print("❯ ")
print(ready, flush=True)

for raw in sys.stdin:
    prompt = raw.strip()
    if not prompt:
        continue
    low = prompt.lower()
    if name == "Coordinator" and "create_helper_demo" in low:
        print("Coordinator: creating Worker from approved profile 'worker'…")
        print("working on request", flush=True)
        code = run_cli("agent", "create", "--name", "Worker", "--profile", "worker")
        if code == 0:
            ready_screen()
            run_cli("message", "send", "--to", "Worker",
                    "--message", "Please send the coordinator a short greeting.")
        else:
            ready_screen()
    elif name == "Worker" and "short greeting" in low:
        print("Worker: received the coordinator's request; replying…")
        print("working on request", flush=True)
        run_cli("message", "send", "--to", "Coordinator",
                "--message", "Worker ready: greeting delivered.")
        ready_screen()
    else:
        print(f"{name}: received {prompt[:180]}")
        ready_screen()
'''


def _clean_text(value: str, width: int = 62) -> list[str]:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = value.replace("⏵⏵", "AUTO")
    return [line[:width] for line in value.replace("\r", "").splitlines() if line.strip()]


def _render_gif(
    events: list[str], team_name: str, parent_screen: str, worker_screen: str,
    worker_state: str, messages: list[dict[str, Any]],
) -> None:
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1440, 900
    bg, panel, line, accent = "#101820", "#182630", "#2B3B47", "#67D5B5"
    white, muted, gold = "#ECF2F5", "#A8BBC7", "#E9C46A"
    font_dir = Path("/usr/share/fonts/truetype/dejavu")
    try:
        regular = ImageFont.truetype(str(font_dir / "DejaVuSansMono.ttf"), 19)
        bold = ImageFont.truetype(str(font_dir / "DejaVuSansMono-Bold.ttf"), 23)
        small = ImageFont.truetype(str(font_dir / "DejaVuSansMono.ttf"), 15)
    except OSError:
        regular = bold = small = ImageFont.load_default()

    history_lines = []
    for item in messages:
        sender = item.get("sender_name") or item.get("sender") or "agent"
        recipient = item.get("recipient_name") or item.get("recipient") or "agent"
        history_lines.append(f"{sender} -> {recipient}: {item.get('body', '')}")
    parent_lines = _clean_text(parent_screen)[-9:]
    worker_lines = _clean_text(worker_screen)[-9:]

    frames = []
    for step in range(1, len(events) + 1):
        im = Image.new("RGB", (width, height), bg)
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((32, 28, width - 32, 106), 18, fill=panel, outline=line, width=2)
        d.text((58, 48), "CREWHALL  /  AGENT SPAWN + TEAM MESSAGING", font=bold, fill=white)
        d.text((58, 116), "Local end-to-end demo · provider simulated · no model/API credentials", font=small, fill=muted)

        # Event timeline.
        d.rounded_rectangle((32, 156, 690, 540), 16, fill=panel, outline=line, width=2)
        d.text((56, 176), "CONTROL FLOW", font=bold, fill=accent)
        for index, event in enumerate(events[:step]):
            y = 224 + index * 48
            d.ellipse((58, y + 5, 70, y + 17), fill=accent if index < step - 1 else gold)
            d.text((88, y), event[:48], font=regular, fill=white)

        # Team state.
        d.rounded_rectangle((714, 156, width - 32, 360), 16, fill=panel, outline=line, width=2)
        d.text((738, 176), f"TEAM  /  {team_name}", font=bold, fill=accent)
        d.text((744, 236), "● Coordinator   [SPAWNER]" if step >= 2 else "○ No members yet",
               font=regular, fill=white if step >= 2 else muted)
        worker_label = f"● Worker        [{worker_state.upper()} · created by Coordinator]"
        d.text((744, 276), worker_label if step >= 4 else "○ Worker        [not created yet]",
               font=small if step >= 4 else regular, fill=white if step >= 4 else muted)
        d.text((744, 320), "Profile: worker   ·   live children: 1 / 6" if step >= 4 else "Profile: worker   ·   live children: 0 / 6",
               font=small, fill=muted)

        # Actual terminal captures from both fixture-backed agents.
        d.rounded_rectangle((32, 564, 690, 852), 16, fill=panel, outline=line, width=2)
        d.text((56, 582), "COORDINATOR TERMINAL", font=bold, fill=accent)
        for i, text in enumerate(parent_lines[-6:] if step >= 3 else []):
            d.text((56, 626 + i * 32), text[:62], font=small, fill=white)
        d.rounded_rectangle((714, 384, width - 32, 852), 16, fill=panel, outline=line, width=2)
        d.text((738, 404), "WORKER TERMINAL + MESSAGE HISTORY", font=bold, fill=accent)
        if step < 4:
            shown = []
        elif step < 5:
            shown = worker_lines[-5:]
        elif step == 5:
            shown = worker_lines[-5:] + history_lines[:1]
        else:
            shown = worker_lines[-5:] + history_lines[-3:]
        for i, text in enumerate(shown[-10:]):
            d.text((738, 450 + i * 34), text[:66], font=small, fill=white)

        # Progress indicator.
        d.rounded_rectangle((32, 870, width - 32, 880), 5, fill=line)
        d.rounded_rectangle((32, 870, 32 + int((width - 64) * step / len(events)), 880), 5, fill=accent)
        frames.append(im)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        OUTPUT, save_all=True, append_images=frames[1:], duration=1500,
        loop=0, optimize=True, disposal=2,
    )


def main() -> int:
    # Start from a clean environment so no user settings, state, or credentials
    # can leak into the fixture agents. Only the private paths below are used.
    root = Path(tempfile.mkdtemp(prefix="crewhall-agent-demo-"))
    home = root / "home"
    config = root / "config"
    state = root / "state"
    runtime = root / "runtime"
    cache = root / "cache"
    tmp = root / "tmp"
    workspace = root / "workspace"
    bindir = root / "bin"
    for directory in (home, config / "crewhall", state, runtime, cache, tmp,
                      workspace, bindir):
        directory.mkdir(parents=True, exist_ok=True)
    runtime.chmod(0o700)
    (workspace / ".git").mkdir()
    socket_name = f"crewhall_demo_{os.getpid()}"
    python_bin = str(Path(sys.executable).resolve())
    safe_path = os.pathsep.join((str(bindir), str(Path(python_bin).parent), "/usr/bin", "/bin"))
    env = {
        "HOME": str(home), "PATH": safe_path, "PYTHONPATH": str(ROOT),
        "XDG_CONFIG_HOME": str(config), "XDG_STATE_HOME": str(state),
        "XDG_RUNTIME_DIR": str(runtime), "XDG_CACHE_HOME": str(cache), "TMPDIR": str(tmp),
        "CREWHALL_TMUX_SOCKET": socket_name,
        "AGENT_TERMINAL_TMUX_SOCKET": socket_name,
        "CREWHALL_HOOKS": "0", "CREWHALL_CONVERSATIONS": "0",
        "LANG": "C.UTF-8", "SHELL": "/bin/sh",
    }
    fake_provider = bindir / "claude"
    fake_provider.write_text(FAKE_CLAUDE, encoding="utf-8")
    fake_provider.chmod(0o700)
    (config / "crewhall" / "settings.json").write_text(
        json.dumps({"providers": {"claude": {"command": str(fake_provider)}}}),
        encoding="utf-8",
    )
    (workspace / "cwh-agents.json").write_text(json.dumps({
        "version": 1,
        "profiles": {
            "worker": {
                "description": "Deterministic helper for the local demo.",
                "provider": "claude",
                "flags": {"--max-turns": 5},
            },
        },
    }, indent=2), encoding="utf-8")

    client = None
    socket_cleanup = subprocess.run(
        ["tmux", "-L", socket_name, "kill-server"], env=env,
        capture_output=True, text=True,
    )
    del socket_cleanup
    try:
        os.environ.clear()
        os.environ.update(env)
        sys.path.insert(0, str(ROOT))
        from crewhall.client import Client

        client = Client()
        team = client.call(
            "team_create", name="Spawn Demo", agent_ids=[], workspace=str(workspace),
        )["team"]
        team_id = team["team_id"]
        coordinator = client.call(
            "agent_create", kind="claude", name="Coordinator", backend="tmux",
            cwd=str(workspace), team=team_id,
        )["agent"]
        client.call("team_set_spawner", target=team_id, agent=coordinator["agent_id"])
        events = [
            "Human creates the shared-workspace team",
            "Coordinator joins and becomes its single spawner",
            "Coordinator requests Worker / profile worker",
            "Crewhall validates the request and starts Worker",
            "Coordinator sends a team message to Worker",
            "Worker replies; both team messages are delivered",
        ]
        client.call(
            "agent_send", target=coordinator["agent_id"],
            prompt="CREATE_HELPER_DEMO: create Worker and ask it for a greeting.",
            timeout=30,
        )

        deadline = time.monotonic() + 30
        worker = None
        messages: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            agents = client.call("agent_list").get("agents", [])
            worker = next((agent for agent in agents if agent.get("name") == "Worker"), None)
            messages = client.call("message_history", limit=20).get("messages", [])
            worker_reply = next((m for m in messages
                                 if "Worker ready: greeting delivered." in m.get("body", "")), None)
            if (worker and worker.get("state") in ("ready", "waiting_input")
                    and worker_reply and worker_reply.get("delivered")
                    and not worker_reply.get("queued")):
                break
            time.sleep(0.2)
        worker_reply = next((m for m in messages
                             if "Worker ready: greeting delivered." in m.get("body", "")), None)
        if (not worker or worker.get("state") not in ("ready", "waiting_input")
                or not worker_reply or not worker_reply.get("delivered")
                or worker_reply.get("queued")):
            raise RuntimeError(f"demo did not complete; worker={worker}, messages={messages}")

        team_info = client.call("team_info", target=team_id)["team"]
        parent_screen = client.call("agent_capture", target=coordinator["agent_id"])["output"]
        worker_screen = client.call("agent_capture", target=worker["agent_id"])["output"]
        _render_gif(events, team_info["name"], parent_screen, worker_screen,
                    worker["state"], messages)
        print(f"GIF: {OUTPUT}")
        print("Flow completed: Coordinator created Worker and exchanged both team messages.")
        return 0
    finally:
        if client is not None:
            try:
                client.call("shutdown", kill_agents=True)
            except Exception:
                pass
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"], env=env,
            capture_output=True, text=True,
        )
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
