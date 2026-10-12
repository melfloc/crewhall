"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import json
import sys

from . import common
from .common import _print_terminal, argparse_cwd
from .sessions import cmd_attach


def _terminals_enabled(args: argparse.Namespace) -> bool:
    if common._client(args).call("meta_info").get("terminals_enabled"):
        return True
    print("terminals are disabled (enable in Settings)", file=sys.stderr)
    return False


def cmd_terminal_new(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    resp = common._client(args).call(
        "terminal_create",
        host=args.host, cwd=argparse_cwd(args.cwd), shell=args.shell,
        command=args.command, title=args.title, readonly=args.readonly,
        cols=args.cols, rows=args.rows,
    )
    _print_terminal(resp["terminal"])
    return 0


def cmd_terminal_ls(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    terminals = common._client(args).call("terminal_list", host=args.host)["terminals"]
    if args.json:
        print(json.dumps(terminals, indent=2))
        return 0
    if not terminals:
        print("no terminals")
        return 0
    for info in terminals:
        _print_terminal(info)
    return 0


def cmd_terminal_send(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    common._client(args).call(
        "terminal_write", id=args.target, text=args.text, enter=args.enter
    )
    return 0


def cmd_terminal_key(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    common._client(args).call("terminal_key", id=args.target, key=args.key)
    return 0


def cmd_terminal_capture(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    out = common._client(args).call(
        "terminal_capture", id=args.target, recent=True, max_lines=args.lines,
        escapes=args.escapes,
    )["output"]
    sys.stdout.write(out)
    return 0


def cmd_terminal_close(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    common._client(args).call("terminal_close", id=args.target)
    return 0


def cmd_terminal_attach(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    return cmd_attach(argparse.Namespace(**{**vars(args), "target": args.target}))
