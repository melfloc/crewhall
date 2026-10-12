"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from .. import brand
from ..client import RpcError
from . import common


def cmd_chat_publish(args: argparse.Namespace) -> int:
    """Publish a file as an artifact of a chat (lead or crew member only)."""
    sender = brand.env("AGENT_ID")
    token = brand.env("TOKEN")
    if not sender:
        print("not inside an crewhall agent (CREWHALL_AGENT_ID unset)", file=sys.stderr)
        return 2
    cid = args.conversation or os.environ.get("CREWHALL_CONVERSATION")
    if not cid:
        print("error: --conversation is required (no $CREWHALL_CONVERSATION)", file=sys.stderr)
        return 2
    try:
        res = common._client(args).call(
            "conversation_publish", sender=sender, token=token, id=cid,
            source=os.path.abspath(args.path), name=args.name)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    artifact = res.get("artifact") or {}
    print(f"published {artifact.get('relpath')} in {cid}")
    return 0


def cmd_office(args: argparse.Namespace) -> int:
    """Drive the headless OnlyOffice co-editor (Nivel 3) from an agent or shell.

    Typical agent use, with the environment crewhall injects:

        crewhall office open   --conversation "$CREWHALL_CONVERSATION" --path report.docx
        crewhall office insert --conversation "$CREWHALL_CONVERSATION" --path report.docx --text "hola"
        crewhall office read   --conversation "$CREWHALL_CONVERSATION" --path report.docx
        crewhall office close  --conversation "$CREWHALL_CONVERSATION" --path report.docx
    """
    import json as _json

    action = args.office_command
    if action == "setup":
        from ..office import setup as office_setup

        try:
            res = office_setup.setup(port=getattr(args, "port", 8081),
                                     bind=getattr(args, "bind", None),
                                     external=getattr(args, "external", None),
                                     jwt_secret=getattr(args, "jwt_secret", None),
                                     wait=not getattr(args, "no_wait", False))
        except office_setup.SetupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(_json.dumps(res, indent=2))
        return 0
    if action == "status":
        from ..office import setup as office_setup

        print(_json.dumps(office_setup.status(), indent=2))
        return 0
    if action in ("stop", "restart"):
        from ..office import setup as office_setup

        try:
            res = (office_setup.stop if action == "stop" else office_setup.restart)()
        except office_setup.SetupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(_json.dumps(res, indent=2))
        return 0

    cid = getattr(args, "conversation", None) or os.environ.get("CREWHALL_CONVERSATION")
    rel = getattr(args, "path", None)
    client = common._client(args)
    # Inside an agent, identify as it: the daemon lets an agent co-edit only the
    # chat it leads or crews (the control plane itself is closed to agents).
    sender = brand.env("AGENT_ID")
    creds = {"sender": sender, "token": brand.env("TOKEN")} if sender else {}

    def call(op: str, **params: Any) -> dict[str, Any]:
        return client.call(op, **creds, **params)

    if action == "list":
        res = client.call("office_collab_list")
    elif action == "open":
        res = call("office_collab_open", id=cid, path=rel)
    elif action == "read":
        res = call("office_collab_read", id=cid, path=rel)
    elif action == "insert":
        res = call("office_collab_insert", id=cid, path=rel, text=args.text)
    elif action == "command":
        try:
            parsed = _json.loads(args.args) if args.args else []
        except _json.JSONDecodeError as exc:
            print(f"error: --args is not JSON: {exc}", file=sys.stderr)
            return 1
        res = call("office_collab_command", id=cid, path=rel,
                          method=args.method, args=parsed)
    elif action == "close":
        res = call("office_collab_close", id=cid, path=rel)
    elif action == "users":
        res = call("office_collab_users", id=cid, path=rel)
    elif action == "chat":
        res = call("office_collab_chat", id=cid, path=rel)
    elif action == "say":
        res = call("office_collab_say", id=cid, path=rel, text=args.text)
    elif action == "comments":
        res = call("office_collab_comments", id=cid, path=rel)
    elif action == "comment":
        res = call("office_collab_comment_add", id=cid, path=rel, text=args.text)
    elif action == "save":
        res = call("office_collab_save", id=cid, path=rel)
    else:  # pragma: no cover - argparse enforces the choices
        print(f"error: unknown office command {action!r}", file=sys.stderr)
        return 1
    res.pop("ok", None)
    print(_json.dumps(res, ensure_ascii=False))
    return 0
