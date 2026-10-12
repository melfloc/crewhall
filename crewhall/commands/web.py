"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import json
import sys

from . import common
from .common import _print_frontends


def cmd_frontend(args: argparse.Namespace) -> int:
    mode = args.command
    if mode == "tailscale" and getattr(args, "keep_local", False):
        mode = "both"
    resp = common._client(args).call("frontend_set", mode=mode, port=getattr(args, "port", None))
    _print_frontends(resp)
    return 0


def cmd_frontends(args: argparse.Namespace) -> int:
    resp = common._client(args).call("frontend_status")
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        _print_frontends(resp)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    if getattr(args, "web_action", None) == "token":
        return cmd_web_token(args)
    if getattr(args, "web_action", None) == "status":
        return cmd_web_status(args)

    from ..web.server import main as web_main

    argv = ["--host", args.host, "--port", str(args.port)]
    if args.socket:
        argv += ["--socket", args.socket]
    if getattr(args, "tailscale", False):
        argv.append("--tailscale")
    if getattr(args, "require_auth", False):
        argv.append("--require-auth")
    for extra in getattr(args, "allow_host", None) or []:
        argv += ["--allow-host", extra]
    return web_main(argv)


def cmd_web_token(args: argparse.Namespace) -> int:
    from ..web import auth

    action = getattr(args, "token_action", None)
    if action == "generate":
        if auth.token_exists() and not args.rotate:
            print(
                "a token already exists; use --rotate to replace it",
                file=sys.stderr,
            )
            return 1
        token = auth.generate_token(rotate=args.rotate)
        print(f"token written to {auth.token_path()} (mode 0600)")
        print(f"token: {token}")
        return 0
    if action == "revoke":
        removed = auth.revoke_token()
        print("token revoked" if removed else "no token to revoke")
        return 0
    if action == "show":
        shown = auth.load_token()
        if not shown:
            print("no token configured", file=sys.stderr)
            return 1
        print(shown)
        return 0
    raise SystemExit("usage: crewhall web token generate|revoke|show")


def _parse_ttl(value: str | None) -> int | None:
    if value in (None, "", "0"):
        return None
    text = str(value).strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    return int(float(text))


def cmd_terminal_token(args: argparse.Namespace) -> int:
    from ..web import terminal_tokens

    action = getattr(args, "tt_action", None)
    if action == "new":
        ttl = _parse_ttl(getattr(args, "ttl", None))
        token, rec = terminal_tokens.issue(
            getattr(args, "label", "") or "", scope=getattr(args, "scope", "read"),
            hosts=getattr(args, "host", None), ttl=ttl,
        )
        print(f"terminal token {rec['id']} written to {terminal_tokens.store_path()} (mode 0600)")
        print(f"scopes: {', '.join(rec['scopes'])}  hosts: {', '.join(rec['hosts'])}")
        print(f"token (shown once): {token}")
        return 0
    if action == "list":
        tokens = terminal_tokens.list_tokens()
        if getattr(args, "json", False):
            print(json.dumps(tokens, indent=2))
            return 0
        if not tokens:
            print("no terminal tokens")
            return 0
        for t in tokens:
            state = "expired" if t["expired"] else "active"
            print(f"{t['id']}  {state:<8} scopes={','.join(t['scopes'])}  "
                  f"hosts={','.join(t['hosts'])}  label={t['label'] or '-'}")
        return 0
    if action == "revoke":
        removed = terminal_tokens.revoke(args.id)
        print("token revoked" if removed else "no such token")
        return 0 if removed else 1
    raise SystemExit("usage: crewhall web terminal-token new|list|revoke")


def cmd_web_status(args: argparse.Namespace) -> int:
    from ..web import auth, tailscale

    print(f"token configured: {'yes' if auth.token_exists() else 'no'}")
    fp = auth.token_fingerprint()
    print(f"token fingerprint: {fp or '-'}")
    print(f"token file mode 0600: {'yes' if auth.file_permissions_ok() else 'n/a'}")
    print(f"tailscale installed: {'yes' if tailscale.available() else 'no'}")
    try:
        ip = tailscale.local_ipv4()
        print(f"tailscale ipv4: {ip or '-'}")
        print(f"tailscale dns: {tailscale.dns_name() or '-'}")
    except Exception:  # noqa: BLE001
        print("tailscale ipv4: -")
    return 0
