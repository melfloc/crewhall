"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .. import brand
from ..client import RpcError
from . import common
from .common import _print_agent, _print_delivery, argparse_cwd


def cmd_agent_identity(args: argparse.Namespace) -> int:
    agent_id = brand.env("AGENT_ID")
    token = brand.env("TOKEN")
    if not agent_id:
        print(
            "not inside an crewhall agent (CREWHALL_AGENT_ID unset)",
            file=sys.stderr,
        )
        return 2
    resp = common._client(args).call("agent_identity", target=agent_id, token=token)
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0
    info = resp["agent"]
    print(f"YOU: {info.get('name') or info['agent_id']}  ({info['kind']})")
    print(f"TEAMS: {', '.join(resp['teams']) or '(none)'}")
    print("TEAMMATES:")
    if not resp["teammates"]:
        print("  (none)")
    for member in resp["teammates"]:
        print(f"  - {member['name']} \u00b7 {member['kind']} \u00b7 {member['state']}")
    return 0


def cmd_agent_new_session(args: argparse.Namespace) -> int:
    """Start a clean conversation in an agent (Claude ``/clear``, OpenCode ``/new``).

    From inside an agent it acts as that agent (Team boundary enforced).
    """
    sender = brand.env("AGENT_ID")
    try:
        if sender:
            out = common._client(args).call(
                "team_new_session", sender=sender, token=brand.env("TOKEN"),
                target=args.target, timeout=args.timeout,
            )
        else:
            out = common._client(args).call("agent_new_session", target=args.target, timeout=args.timeout)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    name = out["agent"].get("name") or out["agent"]["agent_id"]
    if out["confirmed"]:
        print(f"new session: {name} ({out['command']})"
              + (f" conversation={out['conversation_id']}" if out.get("conversation_id") else ""))
        return 0
    print(f"sent {out['command']} to {name}, but the new session was not confirmed yet", file=sys.stderr)
    return 3


def cmd_agent_list(args: argparse.Namespace) -> int:
    team = getattr(args, "team", None)
    if team:
        members = common._client(args).call("team_members", target=team)["members"]
        if args.json:
            print(json.dumps(members, indent=2))
            return 0
        if not members:
            print(f"no members in team {team}")
            return 0
        for member in members:
            print(
                f"  {member.get('name') or member['agent_id']} \u00b7 "
                f"{member['kind']} \u00b7 {member['state']}"
            )
        return 0
    agents = common._client(args).call("agent_list")["agents"]
    if args.json:
        print(json.dumps(agents, indent=2))
        return 0
    if not agents:
        print("no agents")
        return 0
    print(f"{'AGENT_ID':<20} {'NAME':<12} {'KIND':<10} {'STATE':<14} BACKEND")
    for a in agents:
        print(
            f"{a['agent_id']:<20} {(a.get('name') or '-'):<12} {a['kind']:<10} "
            f"{a['state']:<14} {a['backend']}"
        )
    return 0


def _hook_payload(timeout: float = 1.0) -> dict:
    """The JSON a Claude hook receives on stdin (``{}`` if absent or not JSON)."""
    import select

    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        if not select.select([sys.stdin], [], [], timeout)[0]:
            return {}
        data = json.loads(sys.stdin.read(4 << 20) or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _permission_hook(args: argparse.Namespace, agent: str, payload: dict) -> int:
    """Wait for an answer from crewhall's front-ends; print Claude's decision.

    Any failure (or no answer in time) prints nothing, so Claude shows its own
    dialog in the TUI: the terminal always remains a way to answer.
    """
    try:
        out = common._client(args).call(
            "agent_permission_request", agent=agent,
            token=brand.env("TOKEN"), payload=payload,
        )
    except Exception:  # noqa: BLE001
        return 0
    decision = out.get("decision")
    if isinstance(decision, dict):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PermissionRequest", "decision": decision,
        }}))
    return 0


def _question_hook(args: argparse.Namespace, agent: str, payload: dict) -> int:
    """``PreToolUse`` for AskUserQuestion: same op, printed as a PreToolUse decision.

    The question (and the user's answer) travel through the identical
    ``agent_permission_request`` op; only the JSON shape Claude expects differs.
    No output means "let the TUI ask on its own".
    """
    try:
        out = common._client(args).call(
            "agent_permission_request", agent=agent,
            token=brand.env("TOKEN"), payload=payload,
        )
    except Exception:  # noqa: BLE001
        return 0
    decision = out.get("decision")
    if not isinstance(decision, dict):
        return 0
    if decision.get("behavior") == "allow":
        hook: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
        if decision.get("updatedInput"):
            hook["updatedInput"] = decision["updatedInput"]
        print(json.dumps({"hookSpecificOutput": hook}))
    elif decision.get("behavior") == "deny":
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": decision.get("message") or "The user dismissed the question.",
        }}))
    return 0


def cmd_agent_hook(args: argparse.Namespace) -> int:
    agent = brand.env("AGENT_ID")
    if not agent:
        return 2
    payload = _hook_payload()
    if args.event == "permission_request":
        return _permission_hook(args, agent, payload)
    if args.event == "question_request":
        return _question_hook(args, agent, payload)
    common._client(args).call(
        "agent_hook", agent=agent,
        token=brand.env("TOKEN"), event=args.event,
        conversation_id=payload.get("session_id"),
    )
    return 0


def cmd_agent_create(args: argparse.Namespace) -> int:
    from ..specs import SpecError, apply_profile, load_profiles

    entry = {
        "profile": args.profile, "kind": args.kind, "args": args.args,
        "cwd": args.cwd, "backend": args.backend, "host": getattr(args, "host", None),
    }
    try:
        entry = apply_profile(entry, load_profiles())
        args.args = entry.get("args")
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    resp = common._client(args).call(
        "agent_create",
        kind=entry.get("kind") or "opencode",
        name=args.name,
        backend=entry.get("backend") or "auto",
        cwd=argparse_cwd(entry.get("cwd")),
        team=getattr(args, "team", None),
        cols=args.cols,
        rows=args.rows,
        wait_ready=args.wait,
        timeout=args.timeout,
        args=args.args,
        host=entry.get("host"),
    )
    _print_agent(resp["agent"])
    return 0


def cmd_agent_send(args: argparse.Namespace) -> int:
    resp = common._client(args).call(
        "agent_send", target=args.target, prompt=args.prompt, timeout=args.timeout
    )
    _print_agent(resp["agent"])
    return 0


def cmd_agent_capture(args: argparse.Namespace) -> int:
    resp = common._client(args).call(
        "agent_capture", target=args.target, recent=args.recent, max_lines=args.lines
    )
    sys.stdout.write(resp["output"])
    if not resp["output"].endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_agent_state(args: argparse.Namespace) -> int:
    resp = common._client(args).call("agent_state", target=args.target)
    if args.json:
        print(json.dumps(resp["agent"], indent=2))
    else:
        _print_agent(resp["agent"])
    return 0


def _print_status(st: dict) -> None:
    since = st.get("state_age")
    since_txt = f" (for {since:g}s)" if since is not None else ""
    host = st.get("host") or "-"
    print(f"● {st['name']}  {st['agent_id']}  kind={st['kind']}  "
          f"state={st['state']}{since_txt}  alive={st['alive']}  "
          f"backend={st['backend']}  host={host}")
    act = st.get("activity") or {}
    detail = f" · {act['detail']}" if act.get("detail") else ""
    print(f"    activity: {act.get('label') or act.get('kind') or '?'}{detail}"
          + (f"  model={act['model']}" if act.get("model") else ""))
    inter = st.get("interactions") or []
    print(f"    interactions pending: {len(inter)}"
          + (f" ({', '.join(i.get('kind','?') for i in inter)})" if inter else ""))
    print(f"    human draft in input: {'yes' if st.get('human_draft') else 'no'}"
          + ("  (text not shown)" if st.get("human_draft") else ""))
    q = st.get("queue") or {}
    if q.get("count"):
        print(f"    queued messages: {q['count']} (oldest {q.get('oldest_age')}s)")
    else:
        print("    queued messages: 0")
    cli = st.get("cli") or {}
    if cli.get("blocked"):
        print(f"    cli: {cli.get('version') or 'unknown'} unsupported — {cli.get('reason')}")
    cfg = st.get("session_config") or {}
    if cfg.get("model"):
        running = cfg.get("running_model")
        if cfg.get("model_applied") is not False and (not running or running == cfg["model"]):
            print(f"    model: {cfg['model']}")
        else:
            print(f"    model requested {cfg['model']}, running {running or 'unknown'}")
    if cfg.get("agent"):
        applied = "applied" if cfg.get("agent_applied") else "not applied"
        print(f"    agent: {cfg['agent']} ({applied})")
    ignored = cfg.get("ignored_flags") or []
    if ignored:
        print(f"    ignored in v2: {', '.join(ignored)}")
    blocked = st.get("blocked_by") or []
    print(f"    deliverable_now: {st.get('deliverable_now')}"
          + (f"  blocked_by={blocked}" if blocked else ""))
    procs = st.get("processes") or {}
    if not procs.get("available"):
        print(f"    processes: unavailable ({procs.get('reason')})")
    else:
        shells = procs.get("shells") or []
        others = procs.get("others") or []
        print(f"    processes: {len(shells)} shell(s), {len(others)} other(s)")
        for shell in shells:
            out = "producing output" if shell.get("producing_output") else "idle"
            print(f"      shell pid={shell['pid']} age={shell.get('age')}s "
                  f"[{out}] {shell.get('command')}")
        for other in others:
            print(f"      pid={other['pid']} age={other.get('age')}s {other.get('command')}")
        for entry in procs.get("output") or []:
            print(f"      --- output pid={entry.get('pid')} ---")
            print(entry.get("output", ""))
    subs = st.get("subagents") or []
    print(f"    active sub-agents: {len(subs)}" + (f" {subs}" if subs else ""))


def _agent_auth_params() -> dict:
    sender = brand.env("AGENT_ID")
    if not sender:
        return {}
    return {"sender": sender, "token": brand.env("TOKEN")}


def cmd_agent_interrupt(args: argparse.Namespace) -> int:
    """Stop a teammate's current turn (lead-only from an agent; control plane otherwise)."""
    try:
        resp = common._client(args).call(
            "agent_interrupt", target=args.target, **_agent_auth_params())
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        name = resp.get("name") or resp.get("agent_id")
        print(f"{'interrupted' if resp.get('interrupted') else 'no current turn'}: {name}")
    return 0


def cmd_agent_effort(args: argparse.Namespace) -> int:
    """Show or set a teammate's reasoning effort (lead-only to set, from an agent)."""
    params: dict[str, Any] = {"target": args.target, "effort": args.effort}
    params.update(_agent_auth_params())
    try:
        resp = common._client(args).call("agent_effort", **params)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0
    name = resp.get("name") or resp.get("agent_id")
    verb = "set to" if resp.get("changed") else "is"
    print(f"{name}: effort {verb} {resp.get('effort')} on {resp.get('model')}"
          f"  (available: {', '.join(resp.get('available') or []) or 'none'}, default)")
    return 0


def cmd_agent_signal(args: argparse.Namespace) -> int:
    """Signal a teammate's background process (--pid) or every shell (--all-shells)."""
    if args.pid is None and not args.all_shells:
        print("error: a --pid or --all-shells is required", file=sys.stderr)
        return 2
    params: dict[str, Any] = {
        "target": args.target, "signal": args.signal,
        "pid": args.pid, "all_shells": args.all_shells,
    }
    params.update(_agent_auth_params())
    try:
        resp = common._client(args).call("agent_signal", **params)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        print(f"signalled {resp['count']} process(es) with {args.signal.upper()}")
    return 0


def cmd_agent_restart(args: argparse.Namespace) -> int:
    """Force-restart a teammate (lead-only from an agent), resuming by default."""
    params: dict[str, Any] = {
        "target": args.target, "fresh": args.fresh, "reason": args.reason, "forced": True,
    }
    params.update(_agent_auth_params())
    try:
        resp = common._client(args).call("agent_restart", **params)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resp, indent=2))
        return 0
    if "agent" in resp and "state" not in resp:
        _print_agent(resp["agent"])
        return 0
    mode = "resumed" if resp.get("resumed") else "fresh"
    term = resp.get("terminated") or {}
    print(f"restarted {resp.get('name')}: state={resp.get('state')} ({mode}) "
          f"in {resp.get('duration')}s; terminated="
          f"{len(term.get('signalled', []))} signalled, {len(term.get('remaining', []))} remaining")
    if resp.get("reason"):
        print(f"    reason: {resp['reason']}")
    return 0


def cmd_agent_status(args: argparse.Namespace) -> int:
    """Status of a teammate: works from an agent (team-scoped) or the human CLI."""
    sender = brand.env("AGENT_ID")
    params: dict[str, Any] = {"target": args.target, "output": args.output}
    if sender:
        params["sender"] = sender
        params["token"] = brand.env("TOKEN")
    try:
        resp = common._client(args).call("agent_status", **params)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resp["status"], indent=2))
    else:
        _print_status(resp["status"])
    return 0


def cmd_agent_wait(args: argparse.Namespace) -> int:
    resp = common._client(args).call(
        "agent_wait", target=args.target, state=args.state, timeout=args.timeout
    )
    _print_agent(resp["agent"])
    return 0 if resp.get("reached") else 1


def cmd_agent_stop(args: argparse.Namespace) -> int:
    resp = common._client(args).call("agent_stop", target=args.target, force=args.force)
    _print_agent(resp["agent"])
    return 0


def cmd_agent_message(args: argparse.Namespace) -> int:
    resp = common._client(args).call(
        "message_send",
        sender=args.sender,
        recipient=args.recipient,
        body=args.body,
    )
    _print_delivery(resp["delivery"])
    return 0 if resp["delivery"]["delivered"] else 1


def cmd_agent_messages(args: argparse.Namespace) -> int:
    resp = common._client(args).call(
        "message_history", agent=args.agent, limit=args.limit
    )
    messages = resp["messages"]
    if args.json:
        print(json.dumps(messages, indent=2))
        return 0
    if not messages:
        print("no messages")
        return 0
    for delivery in messages:
        ts = delivery["timestamp"]
        mark = "→" if delivery["delivered"] else "✗"
        sender = delivery.get("sender_name") or delivery["sender"]
        recipient = delivery.get("recipient_name") or delivery["recipient"]
        print(f"{ts:.3f} {delivery['message_id']} {sender} {mark} {recipient} {delivery['body']!r}")
    return 0
