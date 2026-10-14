"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import json
import sys

from . import common
from .common import _print_team


def cmd_team_create(args: argparse.Namespace) -> int:
    resp = common._client(args).call(
        "team_create",
        name=args.name,
        agent_ids=args.agents,
        workspace=getattr(args, "workspace", None),
        host=getattr(args, "host", None),
    )
    _print_team(resp["team"])
    return 0


def cmd_team_up(args: argparse.Namespace) -> int:
    from ..specs import SpecError, load_team_spec

    try:
        spec = load_team_spec(args.file)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    resp = common._client(args).call("team_up", spec=spec)
    for name in resp["created"]:
        print(f"+ created {name}")
    for name in resp["existing"]:
        print(f"= already exists {name}")
    _print_team(resp["team"])
    return 0


def cmd_team_set_workspace(args: argparse.Namespace) -> int:
    extra = {"host": args.host} if getattr(args, "host", None) is not None else {}
    resp = common._client(args).call(
        "team_set_workspace", target=args.target, workspace=args.workspace, **extra
    )
    _print_team(resp["team"])
    return 0


def cmd_team_list(args: argparse.Namespace) -> int:
    teams = common._client(args).call("team_list")["teams"]
    if args.json:
        print(json.dumps(teams, indent=2))
        return 0
    if not teams:
        print("no teams")
        return 0
    for team in teams:
        names = ", ".join(
            (m.get("name") or m["agent_id"]) for m in team["members"]
        )
        missing = f"  missing={team['missing']}" if team["missing"] else ""
        where = f"  host={team['host']}" if team.get("host") else ""
        print(f"{team['team_id']}  {team['name']}  members=[{names}]{missing}{where}")
    return 0


def cmd_team_info(args: argparse.Namespace) -> int:
    team = common._client(args).call("team_info", target=args.target)["team"]
    if args.json:
        print(json.dumps(team, indent=2))
    else:
        _print_team(team)
    return 0


def cmd_team_remove(args: argparse.Namespace) -> int:
    team = common._client(args).call("team_remove", target=args.target)["team"]
    print(f"removed {team['team_id']} ({team['name']})  agents untouched")
    return 0


def cmd_team_add_member(args: argparse.Namespace) -> int:
    team = common._client(args).call(
        "team_add_member", target=args.target, agent=args.agent
    )["team"]
    _print_team(team)
    return 0


def cmd_team_remove_member(args: argparse.Namespace) -> int:
    team = common._client(args).call(
        "team_remove_member", target=args.target, agent=args.agent
    )["team"]
    _print_team(team)
    return 0


def cmd_team_set_leads(args: argparse.Namespace) -> int:
    team = common._client(args).call(
        "team_set_leads", target=args.target, agents=args.agents
    )["team"]
    _print_team(team)
    return 0
