"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..client import RpcError
from . import common


def cmd_host_list(args: argparse.Namespace) -> int:
    hosts = common._client(args).call("host_list")["hosts"]
    if args.json:
        print(json.dumps(hosts, indent=2))
        return 0
    if not hosts:
        print("no hosts configured (see INSTALL.md, 'Hosts remotos por SSH')")
        return 0
    print(f"{'HOST':<16} {'DESTINATION':<28} {'STATE':<13} {'TUNNEL':<13} AGENTS")
    for h in hosts:
        tunnel = h["tunnel_state"] or ("on" if h["tunnel"] else "off")
        print(f"{h['name']:<16} {h['ssh']:<28} {h['state']:<13} {tunnel:<13} {h['agents']}")
    return 0


def cmd_host_add(args: argparse.Namespace) -> int:
    params: dict[str, Any] = {"name": args.name, "ssh": args.destination, "port": args.port,
                              "tunnel": args.tunnel, "confirm": "CONFIRM"}
    for key in ("identity", "known_hosts", "tmux_socket"):
        if getattr(args, key):
            params[key] = getattr(args, key)
    try:
        host = common._client(args).call("host_set", **params)["host"]
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"host {host['name']} saved ({host['ssh']}:{host['port']}); try: crewhall host test {host['name']}")
    return 0


def cmd_host_remove(args: argparse.Namespace) -> int:
    try:
        common._client(args).call("host_remove", name=args.name, confirm="CONFIRM")
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"host {args.name} removed")
    return 0


def cmd_host_test(args: argparse.Namespace) -> int:
    try:
        out = common._client(args).call("host_test", name=args.name)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not out["ok"]:
        print(f"unreachable: {out['error']}\n  ({out['detail']})", file=sys.stderr)
        return 1
    mark = lambda ok: "yes" if ok else "no"  # noqa: E731
    print(f"connected to {args.name}: tmux={mark(out['tmux'])} crewhall={mark(out['crewhall'])} "
          f"git={mark(out['git'])} claude={mark(out['claude'])} opencode={mark(out['opencode'])}")
    if not out["tmux"]:
        print("  tmux is required on the host", file=sys.stderr)
        return 1
    return 0
