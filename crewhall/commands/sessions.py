"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from typing import Any

from . import common
from .common import (
    _default_shell,
    _human_time,
    _print_session,
    argparse_cwd,
)


def cmd_list(args: argparse.Namespace) -> int:
    resp = common._client(args).call("list")
    sessions = resp["sessions"]
    if args.json:
        print(json.dumps(sessions, indent=2))
        return 0
    if not sessions:
        print("no sessions")
        return 0
    print(f"{'SESSION_ID':<20} {'NAME':<12} {'BACKEND':<8} {'PID':>7} {'STATUS':<11} COMMAND")
    for s in sessions:
        print(
            f"{s['session_id']:<20} {(s.get('name') or '-'):<12} {s['backend']:<8} "
            f"{str(s.get('pid')):>7} {s['status']:<11} {s['command']}"
        )
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    command: Any = list(args.command)
    if args.shell and command:
        command = " ".join(command)
    elif not command:
        command = _default_shell()
    cwd = argparse_cwd(args.cwd)
    resp = common._client(args).call(
        "create",
        command=command,
        cwd=cwd,
        backend=args.backend,
        name=args.name,
        cols=args.cols,
        rows=args.rows,
        shell=args.shell,
    )
    _print_session(resp["session"])
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    client = common._client(args)
    text = args.text
    if args.shell:
        text = shlex.join([text])
    client.call("write", target=args.target, text=text)
    if args.enter:
        client.call("enter", target=args.target)
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    common._client(args).call("key", target=args.target, key=args.key)
    return 0


def cmd_enter(args: argparse.Namespace) -> int:
    common._client(args).call("enter", target=args.target)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    resp = common._client(args).call("capture", target=args.target)
    sys.stdout.write(resp["output"])
    if not resp["output"].endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    resp = common._client(args).call("read", target=args.target, timeout=args.timeout)
    sys.stdout.write(resp["output"])
    return 0


def cmd_read_until(args: argparse.Namespace) -> int:
    resp = common._client(args).call(
        "read_until", target=args.target, pattern=args.pattern, timeout=args.timeout
    )
    sys.stdout.write(resp["output"])
    if not resp["output"].endswith("\n"):
        sys.stdout.write("\n")
    return 0 if resp.get("matched") else 1


def cmd_resize(args: argparse.Namespace) -> int:
    resp = common._client(args).call(
        "resize", target=args.target, cols=args.cols, rows=args.rows
    )
    _print_session(resp["session"])
    return 0


def cmd_interrupt(args: argparse.Namespace) -> int:
    resp = common._client(args).call("interrupt", target=args.target)
    _print_session(resp["session"])
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    client = common._client(args)
    if args.all:
        for s in client.call("list")["sessions"]:
            if s["status"] in ("running", "starting"):
                client.call("kill", target=s["session_id"])
        return 0
    resp = client.call("kill", target=args.target)
    _print_session(resp["session"])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    resp = common._client(args).call("status", target=args.target)
    info = resp["session"]
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        _print_session(info)
        print(f"    created_at: {_human_time(info.get('created_at'))}")
        print(f"    last_input_at: {_human_time(info.get('last_input_at'))}")
        print(f"    last_output_at: {_human_time(info.get('last_output_at'))}")
        print(f"    exited_at: {_human_time(info.get('exited_at'))}")
        if info.get("meta"):
            print(f"    meta: {info['meta']}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    resp = common._client(args).call("events", target=args.target)
    for event in resp["events"]:
        print(f"{event['at']:.3f} {event['type']:<12} {json.dumps(event['data'])}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    client = common._client(args)
    info = client.call("attach_info", target=args.target)["session"]
    backend = info.get("backend")
    if backend not in ("tmux", "ssh-tmux"):
        print(
            f"session {info['session_id']} uses backend '{backend}', "
            "which cannot be attached. Use 'capture'/'read-until' instead.",
            file=sys.stderr,
        )
        return 2
    tmux_name = (info.get("meta") or {}).get("tmux_session") or info["session_id"]
    host = info.get("host")
    if host:
        from .. import settings
        from ..backends.ssh_tmux import SshTmuxBackend

        cmd = SshTmuxBackend.attach_command(settings.host(host), tmux_name)
    else:
        from ..backends.tmux import TmuxBackend

        cmd = TmuxBackend.attach_command(tmux_name)
    return subprocess.call(cmd)


def cmd_sendline(args: argparse.Namespace) -> int:
    return cmd_send(args)
