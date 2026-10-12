"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import json
import sys

from .. import brand
from ..client import RpcError
from . import common
from .common import _agent_identity_env, _print_delivery


def cmd_message_send(args: argparse.Namespace) -> int:
    sender = brand.env("AGENT_ID")
    token = brand.env("TOKEN")
    if not sender:
        print(
            "not inside an crewhall agent (CREWHALL_AGENT_ID unset)",
            file=sys.stderr,
        )
        return 2
    resp = common._client(args).call(
        "team_send", sender=sender, token=token,
        recipient=args.to, body=args.message,
        conversation_id=brand.env("CONVERSATION"),
    )
    delivery = resp["delivery"]
    _print_delivery(delivery)
    return 0 if (delivery["delivered"] or delivery.get("queued")) else 1


def cmd_message_request(args: argparse.Namespace) -> int:
    sender, token = _agent_identity_env()
    if not sender:
        print("not inside an crewhall agent (CREWHALL_AGENT_ID unset)", file=sys.stderr)
        return 2
    try:
        out = common._client(args).call("request_create", sender=sender, token=token,
                                 recipient=args.to, task=args.task, timeout=args.timeout,
                                 wait=not args.no_wait)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    req = out["request"]
    if args.no_wait:
        print(f"request {req['request_id']} to {req['recipient']} ({req['state']})")
        return 0
    if req["state"] == "replied":
        print(req.get("reply") or "")
        return 0
    print(f"request {req['request_id']} {req['state']}", file=sys.stderr)
    return 1


def cmd_message_reply(args: argparse.Namespace) -> int:
    sender, token = _agent_identity_env()
    if not sender:
        print("not inside an crewhall agent (CREWHALL_AGENT_ID unset)", file=sys.stderr)
        return 2
    try:
        out = common._client(args).call("request_reply", agent=sender, token=token,
                                 request_id=args.request_id, body=args.message)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"replied to {out['request']['request_id']}")
    return 0


def cmd_message_requests(args: argparse.Namespace) -> int:
    try:
        out = common._client(args).call("request_list", open_only=not args.all)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    requests = out["requests"]
    if args.json:
        print(json.dumps(requests, indent=2))
        return 0
    if not requests:
        print("no requests")
        return 0
    for req in requests:
        print(f"{req['request_id']}  {req['sender']} -> {req['recipient']}  "
              f"[{req['state']}]  {req['task'][:60]}")
    return 0
