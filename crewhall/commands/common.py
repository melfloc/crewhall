"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import os
from typing import Any

from .. import brand
from ..client import Client


def _default_shell() -> list[str]:
    return [os.environ.get("SHELL", "/bin/bash"), "-i"]


def _client(args: argparse.Namespace) -> Client:
    return Client(autostart=True)


def _print_session(info: dict[str, Any]) -> None:
    status = info.get("status")
    alive = status in ("running", "starting")
    mark = "●" if alive else "○"
    name = info.get("name") or "-"
    print(
        f"{mark} {info['session_id']}  name={name}  backend={info['backend']}  "
        f"pid={info.get('pid')}  status={status}  exit={info.get('exit_code')}"
    )
    print(f"    command: {info.get('command')}")
    print(f"    cwd: {info.get('cwd')}")


def _human_time(ts: Any) -> str:
    if not ts:
        return "-"
    return f"{ts:.3f}"


def argparse_cwd(cwd: str | None) -> str | None:
    return os.path.abspath(cwd) if cwd else None


def _print_agent(info: dict[str, Any]) -> None:
    state = info.get("state")
    mark = "●" if state in ("ready", "waiting_input", "working") else "○"
    name = info.get("name") or "-"
    print(
        f"{mark} {info['agent_id']}  name={name}  kind={info['kind']}  "
        f"state={state}  backend={info.get('backend')}  pid={info.get('pid')}"
    )
    print(f"    evidence: {info.get('evidence')}")


def _agent_identity_env() -> tuple[str | None, str | None]:
    return brand.env("AGENT_ID"), brand.env("TOKEN")


def _print_delivery(delivery: dict[str, Any]) -> None:
    if delivery.get("status") == "acknowledged":
        mark = "✓✓"
    elif delivery.get("delivered"):
        mark = "✓"
    elif delivery.get("queued"):
        mark = "…"
    else:
        mark = "✗"
    sender = delivery.get("sender_name") or delivery["sender"]
    recipient = delivery.get("recipient_name") or delivery["recipient"]
    print(f"{mark} {delivery['message_id']}  {sender} -> {recipient}: {delivery['body']!r}")
    if delivery.get("queued") and not delivery.get("delivered"):
        reason = delivery.get("queue_reason")
        if reason == "human draft":
            print("    queued: the recipient has a human draft in its input; "
                  "it will be delivered when the input is cleared")
        elif reason == "starting":
            print("    queued: the recipient is still starting; "
                  "it will be delivered when it is ready")
        elif reason == "stopped":
            print("    queued: the recipient is stopped; "
                  "it will be delivered when it is revived")
        elif reason == "busy":
            print("    queued: recipient busy; will be delivered when it is ready")
        else:
            print("    queued: will be delivered when the recipient is ready")
    if delivery.get("error"):
        print(f"    error: {delivery['error']}")


def _print_team(team: dict[str, Any]) -> None:
    members = team.get("members", [])
    missing = team.get("missing", [])
    where = f"  host={team['host']}" if team.get("host") else ""
    ws = f"  workspace={team['workspace']}" if team.get("workspace") else ""
    print(f"● {team['team_id']}  name={team['name']}  members={len(members)}{where}{ws}")
    for member in members:
        name = member.get("name") or "-"
        print(
            f"    ● {member['agent_id']}  name={name}  "
            f"kind={member['kind']}  state={member['state']}"
        )
    for agent_id in missing:
        print(f"    ○ {agent_id}  (missing)")


def _print_terminal(info: dict[str, Any]) -> None:
    host = info.get("host") or "local"
    ro = " readonly" if info.get("readonly") else ""
    print(
        f"{info['session_id']}  host={host}  status={info['status']}  "
        f"cwd={info.get('cwd') or '-'}  title={info.get('title') or '-'}{ro}"
    )


def _print_frontends(resp: dict) -> None:
    print(f"web interfaces: {resp['mode']}")
    for name in ("local", "tailscale"):
        f = resp["frontends"][name]
        if f["running"]:
            auth_note = "token required" if f["auth"] == "required" else "no token (loopback only)"
            print(f"  ● {name:<10} {f['url']}   [{auth_note}]")
        else:
            print(f"  ○ {name:<10} off" + (f"   (error: {f['error']})" if f.get("error") else ""))
    if resp.get("token_created"):
        print(f"\nA web access token was created for remote access:\n  {resp['token']}\n"
              "Keep it private; show it again with: crewhall web token show")
    elif resp["frontends"]["tailscale"]["running"]:
        print("\nLogin token: crewhall web token show")
    print("TUI: on demand, run `crewhall ui` in any terminal.")


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _log_matches(line: str, agent: str | None) -> bool:
    return not agent or agent in line
