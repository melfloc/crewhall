from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from typing import Any

from . import brand, clean, paths
from .backends import available
from .client import Client, RpcError
from .daemon import Server
from .controller import Controller
from .session import InteractiveSession, SessionTimeout
from .types import SessionSpec


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


def cmd_list(args: argparse.Namespace) -> int:
    resp = _client(args).call("list")
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
    command = list(args.command)
    if args.shell and command:
        command = " ".join(command)
    elif not command:
        command = _default_shell()
    cwd = argparse_cwd(args.cwd)
    resp = _client(args).call(
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


def argparse_cwd(cwd: str | None) -> str | None:
    return os.path.abspath(cwd) if cwd else None


def cmd_send(args: argparse.Namespace) -> int:
    client = _client(args)
    text = args.text
    if args.shell:
        text = shlex.join([text])
    client.call("write", target=args.target, text=text)
    if args.enter:
        client.call("enter", target=args.target)
    return 0


def cmd_key(args: argparse.Namespace) -> int:
    _client(args).call("key", target=args.target, key=args.key)
    return 0


def cmd_enter(args: argparse.Namespace) -> int:
    _client(args).call("enter", target=args.target)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    resp = _client(args).call("capture", target=args.target)
    sys.stdout.write(resp["output"])
    if not resp["output"].endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    resp = _client(args).call("read", target=args.target, timeout=args.timeout)
    sys.stdout.write(resp["output"])
    return 0


def cmd_read_until(args: argparse.Namespace) -> int:
    resp = _client(args).call(
        "read_until", target=args.target, pattern=args.pattern, timeout=args.timeout
    )
    sys.stdout.write(resp["output"])
    if not resp["output"].endswith("\n"):
        sys.stdout.write("\n")
    return 0 if resp.get("matched") else 1


def cmd_resize(args: argparse.Namespace) -> int:
    resp = _client(args).call(
        "resize", target=args.target, cols=args.cols, rows=args.rows
    )
    _print_session(resp["session"])
    return 0


def cmd_interrupt(args: argparse.Namespace) -> int:
    resp = _client(args).call("interrupt", target=args.target)
    _print_session(resp["session"])
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.all:
        for s in client.call("list")["sessions"]:
            if s["status"] in ("running", "starting"):
                client.call("kill", target=s["session_id"])
        return 0
    resp = client.call("kill", target=args.target)
    _print_session(resp["session"])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    resp = _client(args).call("status", target=args.target)
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
    resp = _client(args).call("events", target=args.target)
    for event in resp["events"]:
        print(f"{event['at']:.3f} {event['type']:<12} {json.dumps(event['data'])}")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    client = _client(args)
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
        from . import settings
        from .backends.ssh_tmux import SshTmuxBackend

        cmd = SshTmuxBackend.attach_command(settings.host(host), tmux_name)
    else:
        from .backends.tmux import TmuxBackend

        cmd = TmuxBackend.attach_command(tmux_name)
    return subprocess.call(cmd)


def _terminals_enabled(args: argparse.Namespace) -> bool:
    if _client(args).call("meta_info").get("terminals_enabled"):
        return True
    print("terminals are disabled (enable in Settings)", file=sys.stderr)
    return False


def _print_terminal(info: dict[str, Any]) -> None:
    host = info.get("host") or "local"
    ro = " readonly" if info.get("readonly") else ""
    print(
        f"{info['session_id']}  host={host}  status={info['status']}  "
        f"cwd={info.get('cwd') or '-'}  title={info.get('title') or '-'}{ro}"
    )


def cmd_terminal_new(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    resp = _client(args).call(
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
    terminals = _client(args).call("terminal_list", host=args.host)["terminals"]
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
    _client(args).call(
        "terminal_write", id=args.target, text=args.text, enter=args.enter
    )
    return 0


def cmd_terminal_key(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    _client(args).call("terminal_key", id=args.target, key=args.key)
    return 0


def cmd_terminal_capture(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    out = _client(args).call(
        "terminal_capture", id=args.target, recent=True, max_lines=args.lines,
        escapes=args.escapes,
    )["output"]
    sys.stdout.write(out)
    return 0


def cmd_terminal_close(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    _client(args).call("terminal_close", id=args.target)
    return 0


def cmd_terminal_attach(args: argparse.Namespace) -> int:
    if not _terminals_enabled(args):
        return 2
    return cmd_attach(argparse.Namespace(**{**vars(args), "target": args.target}))


def _print_agent(info: dict[str, Any]) -> None:
    state = info.get("state")
    mark = "●" if state in ("ready", "waiting_input", "working") else "○"
    name = info.get("name") or "-"
    print(
        f"{mark} {info['agent_id']}  name={name}  kind={info['kind']}  "
        f"state={state}  backend={info.get('backend')}  pid={info.get('pid')}"
    )
    print(f"    evidence: {info.get('evidence')}")


def cmd_agent_identity(args: argparse.Namespace) -> int:
    agent_id = brand.env("AGENT_ID")
    token = brand.env("TOKEN")
    if not agent_id:
        print(
            "not inside an crewhall agent (CREWHALL_AGENT_ID unset)",
            file=sys.stderr,
        )
        return 2
    resp = _client(args).call("agent_identity", target=agent_id, token=token)
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


def cmd_message_send(args: argparse.Namespace) -> int:
    sender = brand.env("AGENT_ID")
    token = brand.env("TOKEN")
    if not sender:
        print(
            "not inside an crewhall agent (CREWHALL_AGENT_ID unset)",
            file=sys.stderr,
        )
        return 2
    resp = _client(args).call(
        "team_send", sender=sender, token=token,
        recipient=args.to, body=args.message,
    )
    delivery = resp["delivery"]
    _print_delivery(delivery)
    return 0 if (delivery["delivered"] or delivery.get("queued")) else 1


def _agent_identity_env() -> tuple[str | None, str | None]:
    return brand.env("AGENT_ID"), brand.env("TOKEN")


def cmd_message_request(args: argparse.Namespace) -> int:
    sender, token = _agent_identity_env()
    if not sender:
        print("not inside an crewhall agent (CREWHALL_AGENT_ID unset)", file=sys.stderr)
        return 2
    try:
        out = _client(args).call("request_create", sender=sender, token=token,
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
        out = _client(args).call("request_reply", agent=sender, token=token,
                                 request_id=args.request_id, body=args.message)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"replied to {out['request']['request_id']}")
    return 0


def cmd_message_requests(args: argparse.Namespace) -> int:
    try:
        out = _client(args).call("request_list", open_only=not args.all)
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


def cmd_agent_new_session(args: argparse.Namespace) -> int:
    """Start a clean conversation in an agent (Claude ``/clear``, OpenCode ``/new``).

    From inside an agent it acts as that agent (Team boundary enforced).
    """
    sender = brand.env("AGENT_ID")
    try:
        if sender:
            out = _client(args).call(
                "team_new_session", sender=sender, token=brand.env("TOKEN"),
                target=args.target, timeout=args.timeout,
            )
        else:
            out = _client(args).call("agent_new_session", target=args.target, timeout=args.timeout)
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


def cmd_host_list(args: argparse.Namespace) -> int:
    hosts = _client(args).call("host_list")["hosts"]
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
        host = _client(args).call("host_set", **params)["host"]
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"host {host['name']} saved ({host['ssh']}:{host['port']}); try: crewhall host test {host['name']}")
    return 0


def cmd_host_remove(args: argparse.Namespace) -> int:
    try:
        _client(args).call("host_remove", name=args.name, confirm="CONFIRM")
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"host {args.name} removed")
    return 0


def cmd_host_test(args: argparse.Namespace) -> int:
    try:
        out = _client(args).call("host_test", name=args.name)
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


def cmd_agent_list(args: argparse.Namespace) -> int:
    team = getattr(args, "team", None)
    if team:
        members = _client(args).call("team_members", target=team)["members"]
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
    agents = _client(args).call("agent_list")["agents"]
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


def cmd_frontend(args: argparse.Namespace) -> int:
    mode = args.command
    if mode == "tailscale" and getattr(args, "keep_local", False):
        mode = "both"
    resp = _client(args).call("frontend_set", mode=mode, port=getattr(args, "port", None))
    _print_frontends(resp)
    return 0


def cmd_frontends(args: argparse.Namespace) -> int:
    resp = _client(args).call("frontend_status")
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        _print_frontends(resp)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from . import updater

    log = lambda m: print(f"  {m}")  # noqa: E731
    try:
        if args.status:
            st = updater.status()
            print(f"version {st['version']} ({st['channel']}{', ' + st['commit'] if st['commit'] else ''})"
                  f"  managed install: {'yes' if st['managed'] else 'no'}")
            print(f"daemon: {st['daemon_version'] or 'not running'}"
                  + ("   ← RESTART PENDING (running the old code)" if st["restart_pending"] else ""))
            if st["managed"]:
                print(f"previous: {st.get('previous') or '-'}   rollback available: "
                      f"{'yes' if st.get('rollback_available') else 'no'}")
            print(f"trusted signer configured: {'yes' if st['signers_configured'] else 'no (integrity check only)'}")
            return 0
        if args.rollback:
            out = updater.rollback(restart=args.restart, force=args.force, force_restart=args.force_restart, log=log)
            print(f"rolled back to {out['to']}" + ("" if out["restarted"] else
                  "  (daemon not restarted: run `crewhall update --restart` when idle)"))
            return 0
        if args.restart and not args.source:   # apply a pending update: restart the daemon on the installed code
            out = updater.restart_pending(force_restart=args.force_restart, log=log)
            print("daemon already runs the installed version" if out["status"] == "nothing-pending"
                  else f"daemon restarted: {out['was']} -> {out['version']}")
            return 0
        if not args.source:
            print("error: --from DIR_OR_URL is required (a release directory made by scripts/release.sh)",
                  file=sys.stderr)
            return 2
        if args.check:
            out = updater.check(args.source)
            print(f"installed {out['current']}  available {out['available']}  "
                  f"{'UPDATE AVAILABLE' if out['newer'] else 'up to date'}  [{out['signature']}]")
            return 0 if not out["newer"] else 10
        out = updater.apply(args.source, restart=args.restart, force=args.force,
                            allow_downgrade=args.allow_downgrade, force_restart=args.force_restart, log=log)
    except updater.RestartRefused as exc:
        print(f"refused (nothing was changed): {exc}", file=sys.stderr)
        return 3
    except updater.UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if out.get("restart_refused"):
        print(f"installed, but the daemon was NOT restarted: {out['restart_refused']}", file=sys.stderr)
        return 3
    if out["status"] == "up-to-date":
        print(f"already at {out['version']}")
    else:
        print(f"updated {out['from']} -> {out['to']}")
        if out.get("restart_pending"):
            print("The running daemon still uses the old code (agents were not touched).\n"
                  "Apply it when idle with: "
                  "crewhall update --restart   # (refused while agents run; --force-restart closes them)")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from . import selftest

    print(f"crewhall selftest in {os.path.abspath(args.cwd)}")
    out = selftest.run(os.path.abspath(args.cwd), kinds=args.kinds.split(","), quick=args.quick,
                       log=(lambda m: None) if args.json else print)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print("\nSELFTEST " + ("PASSED" if out["ok"] else "FAILED"))
    return 0 if out["ok"] else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    results = doctor.run_checks()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(doctor.format_report(results))
    return doctor.exit_code(results)


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def cmd_clean(args: argparse.Namespace) -> int:
    from . import clean

    plan = clean.plan_cleanup(keep_backups=args.keep_backups, max_age_days=args.max_age_days)
    if args.json:
        print(json.dumps({**plan.to_dict(), "dry_run": not args.yes}))
        if not args.yes:
            return 0
    if not plan.items:
        if not args.json:
            print("nothing to clean.")
        return 0

    by_kind: dict[str, list] = {}
    for item in plan.items:
        by_kind.setdefault(item.kind, []).append(item)
    if not args.json:
        print(f"{len(plan.items)} item(s), {_human_bytes(plan.total_bytes)} reclaimable:")
        for kind in sorted(by_kind):
            items = by_kind[kind]
            print(f"  {kind}: {len(items)} ({_human_bytes(sum(i.bytes for i in items))})")
        for item in plan.items[:50]:
            print(f"    - {item.path}")
        if len(plan.items) > 50:
            print(f"    … and {len(plan.items) - 50} more")

    if not args.yes:
        if not sys.stdin or not sys.stdin.isatty():
            print("dry run (no terminal to confirm); pass --yes to actually remove.", file=sys.stderr)
            return 0
        answer = input("Remove these? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("aborted; nothing removed.")
            return 0

    result = clean.apply_cleanup(plan)
    if args.json:
        print(json.dumps({**plan.to_dict(), **result, "dry_run": False}))
    else:
        print(f"removed {len(result['removed'])} item(s), {_human_bytes(result['bytes'])}")
        for failure in result["failed"]:
            print(f"  could not remove {failure['path']}: {failure['error']}", file=sys.stderr)
    return 1 if result["failed"] else 0


def cmd_bundle(args: argparse.Namespace) -> int:
    from . import bundle

    try:
        if args.bundle_command == "export":
            if args.with_token:
                print("warning: the bundle will contain your web access token; "
                      "keep the file private.", file=sys.stderr)
            live = None
            if not args.no_live:  # the teams/agents running now, as rebuildable definitions
                try:
                    live = Client(autostart=False).call("bundle_live_teams")["teams"]
                except Exception:  # noqa: BLE001 - no daemon: export the files only
                    print("note: daemon not reachable; running teams/agents not included.", file=sys.stderr)
            out = bundle.export_bundle(args.path, with_state=args.with_state,
                                       with_token=args.with_token, live_teams=live)
            print(f"wrote {out['path']}")
            for name in out["files"]:
                print(f"  + {name}")
            if not out["files"]:
                print("  (nothing to export yet: no profiles.toml or teams/*.toml)")
        else:
            out = bundle.import_bundle(args.path, force=args.force, dry_run=args.dry_run)
            verb = "would write" if out["dry_run"] else "wrote"
            for name in out["written"]:
                print(f"  {verb} {name}")
            for name in out["skipped"]:
                print(f"  skipped {name}")
            for name in out["backed_up"]:
                print(f"  backed up previous {name} -> .bak")
            specs, errors = bundle.bundle_team_specs(bundle.read_bundle(args.path)[1])
            for err in errors:
                print(f"  warning: {err}", file=sys.stderr)
            for team in bundle.plan_specs(specs):
                print(f"  team {team['team']}: " + ", ".join(
                    a["name"] + ("" if a["cwd_ok"] else " (directory missing here)") for a in team["agents"]))
            if args.apply and not out["dry_run"]:
                res = bundle.apply_specs(specs, lambda spec: _client(args).call("team_up", spec=spec))
                for n in res["created"]:
                    print(f"  + created {n}")
                for n in res["existing"]:
                    print(f"  = already exists {n}")
                for sk in res["skipped"]:
                    print(f"  ! skipped {sk['team']}/{sk['agent']}: {sk['reason']}")
                for f in res["failed"]:
                    print(f"  ! failed {f['team']}: {f['error']}")
            elif specs and not args.apply:
                print("  (use --apply to create these teams and agents)")
    except bundle.BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
        out = _client(args).call(
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


def cmd_agent_hook(args: argparse.Namespace) -> int:
    agent = brand.env("AGENT_ID")
    if not agent:
        return 2
    payload = _hook_payload()
    if args.event == "permission_request":
        return _permission_hook(args, agent, payload)
    _client(args).call(
        "agent_hook", agent=agent,
        token=brand.env("TOKEN"), event=args.event,
        conversation_id=payload.get("session_id"),
    )
    return 0


def cmd_agent_create(args: argparse.Namespace) -> int:
    from .specs import SpecError, apply_profile, load_profiles

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
    resp = _client(args).call(
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
    resp = _client(args).call(
        "agent_send", target=args.target, prompt=args.prompt, timeout=args.timeout
    )
    _print_agent(resp["agent"])
    return 0


def cmd_agent_capture(args: argparse.Namespace) -> int:
    resp = _client(args).call(
        "agent_capture", target=args.target, recent=args.recent, max_lines=args.lines
    )
    sys.stdout.write(resp["output"])
    if not resp["output"].endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_agent_state(args: argparse.Namespace) -> int:
    resp = _client(args).call("agent_state", target=args.target)
    if args.json:
        print(json.dumps(resp["agent"], indent=2))
    else:
        _print_agent(resp["agent"])
    return 0


def cmd_agent_wait(args: argparse.Namespace) -> int:
    resp = _client(args).call(
        "agent_wait", target=args.target, state=args.state, timeout=args.timeout
    )
    _print_agent(resp["agent"])
    return 0 if resp.get("reached") else 1


def cmd_agent_stop(args: argparse.Namespace) -> int:
    resp = _client(args).call("agent_stop", target=args.target, force=args.force)
    _print_agent(resp["agent"])
    return 0


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
        print("    queued: recipient busy; will be delivered when it is ready")
    if delivery.get("error"):
        print(f"    error: {delivery['error']}")


def cmd_agent_message(args: argparse.Namespace) -> int:
    resp = _client(args).call(
        "message_send",
        sender=args.sender,
        recipient=args.recipient,
        body=args.body,
    )
    _print_delivery(resp["delivery"])
    return 0 if resp["delivery"]["delivered"] else 1


def cmd_agent_messages(args: argparse.Namespace) -> int:
    resp = _client(args).call(
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


def cmd_team_create(args: argparse.Namespace) -> int:
    resp = _client(args).call(
        "team_create",
        name=args.name,
        agent_ids=args.agents,
        workspace=getattr(args, "workspace", None),
        host=getattr(args, "host", None),
    )
    _print_team(resp["team"])
    return 0


def cmd_team_up(args: argparse.Namespace) -> int:
    from .specs import SpecError, load_team_spec

    try:
        spec = load_team_spec(args.file)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    resp = _client(args).call("team_up", spec=spec)
    for name in resp["created"]:
        print(f"+ created {name}")
    for name in resp["existing"]:
        print(f"= already exists {name}")
    _print_team(resp["team"])
    return 0


def cmd_team_set_workspace(args: argparse.Namespace) -> int:
    extra = {"host": args.host} if getattr(args, "host", None) is not None else {}
    resp = _client(args).call(
        "team_set_workspace", target=args.target, workspace=args.workspace, **extra
    )
    _print_team(resp["team"])
    return 0


def cmd_team_list(args: argparse.Namespace) -> int:
    teams = _client(args).call("team_list")["teams"]
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
    team = _client(args).call("team_info", target=args.target)["team"]
    if args.json:
        print(json.dumps(team, indent=2))
    else:
        _print_team(team)
    return 0


def cmd_team_remove(args: argparse.Namespace) -> int:
    team = _client(args).call("team_remove", target=args.target)["team"]
    print(f"removed {team['team_id']} ({team['name']})  agents untouched")
    return 0


def cmd_team_add_member(args: argparse.Namespace) -> int:
    team = _client(args).call(
        "team_add_member", target=args.target, agent=args.agent
    )["team"]
    _print_team(team)
    return 0


def cmd_team_remove_member(args: argparse.Namespace) -> int:
    team = _client(args).call(
        "team_remove_member", target=args.target, agent=args.agent
    )["team"]
    _print_team(team)
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    if args.action == "status":
        try:
            resp = _client(args).call("ping")
        except RpcError:
            print("daemon: not running")
            return 1
        print(f"daemon: running pid={resp['pid']} sessions={resp['sessions']}")
        print(f"socket: {paths.socket_path()}")
        return 0
    if args.action == "stop":
        try:
            _client(args).call("shutdown")
        except RpcError as exc:
            print(f"daemon: {exc}", file=sys.stderr)
            return 1
        print("daemon: stopping")
        return 0
    if args.action == "logs":
        path = paths.log_path()
        if not os.path.exists(path):
            print(f"no log at {path}")
            return 0
        with open(path) as fh:
            sys.stdout.write(fh.read())
        return 0
    if args.action == "run":
        return _run_foreground(args)
    raise SystemExit(f"unknown daemon action {args.action}")


def _run_foreground(args: argparse.Namespace) -> int:
    controller = Controller(adopt=True)
    server = Server(controller, args.socket or paths.socket_path())
    print(f"crewhall daemon on {server.socket_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui.app import main as ui_main

    argv: list[str] = []
    if args.local:
        argv.append("--local")
    if args.cwd:
        argv += ["--cwd", args.cwd]
    if args.socket:
        argv += ["--socket", args.socket]
    if args.refresh is not None:
        argv += ["--refresh", str(args.refresh)]
    return ui_main(argv)


def cmd_web(args: argparse.Namespace) -> int:
    if getattr(args, "web_action", None) == "token":
        return cmd_web_token(args)
    if getattr(args, "web_action", None) == "status":
        return cmd_web_status(args)

    from .web.server import main as web_main

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
    from .web import auth

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
        token = auth.load_token()
        if not token:
            print("no token configured", file=sys.stderr)
            return 1
        print(token)
        return 0
    raise SystemExit("usage: crewhall web token generate|revoke|show")


def cmd_web_status(args: argparse.Namespace) -> int:
    from .web import auth, tailscale

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


def cmd_service(args: argparse.Namespace) -> int:
    from . import service

    if args.action == "unit":
        sys.stdout.write(service.unit_text())
        return 0
    if args.action == "install":
        result = service.install()
        print(f"installed: {result['unit_path']}")
        print("enable:", "ok" if result["enable"].get("ok") else result["enable"].get("error"))
        return 0
    if args.action == "uninstall":
        result = service.uninstall()
        print(f"removed: {result['unit_path']}")
        return 0
    if args.action == "status":
        result = service.status()
        print(result["status"].get("stdout") or result["status"].get("error"))
        return 0
    if args.action == "web-unit":
        sys.stdout.write(
            service.web_unit_text(
                host=args.web_host, port=args.web_port, tailscale=args.web_tailscale
            )
        )
        return 0
    if args.action == "install-web":
        result = service.install_web(
            host=args.web_host, port=args.web_port, tailscale=args.web_tailscale
        )
        print(f"installed: {result['unit_path']}")
        print("enable:", "ok" if result["enable"].get("ok") else result["enable"].get("error"))
        return 0
    if args.action == "uninstall-web":
        result = service.uninstall_web()
        print(f"removed: {result['unit_path']}")
        return 0
    raise SystemExit(f"unknown service action {args.action}")


def cmd_demo(args: argparse.Namespace) -> int:
    from .backends import get_backend

    backend_name = args.backend
    if backend_name == "all":
        names = [b for b in available() if b != "ssh-tmux"]
    else:
        names = [backend_name]
    for name in names:
        print(f"=== backend: {name} ===")
        session = None
        try:
            spec = SessionSpec(command=["/bin/bash"], cols=100, rows=30)
            session = InteractiveSession(get_backend(name), spec)
            session.start()
            print(f"session {session.session_id} pid={session.pid}")
            session.write("echo HELLO_FROM_$((40+2))")
            session.send_enter()
            out = session.read_until("HELLO_FROM_42", timeout=5)
            tail = [ln for ln in out.splitlines() if "HELLO_FROM" in ln]
            print("captured:", tail[-1] if tail else out[-200:])
            session.resize(120, 40)
            session.write("stty size")
            session.send_enter()
            size = session.read_until("40 120", timeout=5)
            print("resize ok:", "40 120" in size)
        finally:
            if session is not None:
                session.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crewhall",
        description="Control interactive CLI/TUI sessions through a backend-agnostic interface.",
    )
    from .buildinfo import describe

    parser.add_argument("--version", action="version", version=f"crewhall {describe()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("create", help="create and start a session")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.add_argument("-b", "--backend", default="auto")
    p.add_argument("-n", "--name")
    p.add_argument("-c", "--cwd")
    p.add_argument("--cols", type=int, default=80)
    p.add_argument("--rows", type=int, default=24)
    p.add_argument("--shell", action="store_true", help="treat command as a shell string")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("send", help="write text into a session")
    p.add_argument("target")
    p.add_argument("text")
    p.add_argument("--enter", action="store_true", help="also send ENTER")
    p.add_argument("--shell", action="store_true", help="escape text as a shell string")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("sendline", help="write text then ENTER")
    p.add_argument("target")
    p.add_argument("text")
    p.set_defaults(func=cmd_sendline, shell=False, enter=True)

    p = sub.add_parser("key", help="send a named key")
    p.add_argument("target")
    p.add_argument("key")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("enter", help="send ENTER")
    p.add_argument("target")
    p.set_defaults(func=cmd_enter)

    p = sub.add_parser("capture", help="print the current session output")
    p.add_argument("target")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("read", help="read new output since last read")
    p.add_argument("target")
    p.add_argument("--timeout", type=float, default=1.0)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("read-until", help="wait until pattern appears")
    p.add_argument("target")
    p.add_argument("pattern")
    p.add_argument("--timeout", type=float, default=10.0)
    p.set_defaults(func=cmd_read_until)

    p = sub.add_parser("resize", help="resize the session PTY/window")
    p.add_argument("target")
    p.add_argument("cols", type=int)
    p.add_argument("rows", type=int)
    p.set_defaults(func=cmd_resize)

    p = sub.add_parser("interrupt", help="send SIGINT / C-c")
    p.add_argument("target")
    p.set_defaults(func=cmd_interrupt)

    p = sub.add_parser("kill", help="terminate a session")
    p.add_argument("target", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("status", help="show session state")
    p.add_argument("target")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("events", help="show session event log")
    p.add_argument("target")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("attach", help="attach a terminal to a tmux session")
    p.add_argument("target")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("terminal", help="web/CLI terminals (closed by default)")
    tsub = p.add_subparsers(dest="terminal_command", required=True)
    t = tsub.add_parser("new", help="create a terminal")
    t.add_argument("--host", default=None, help="configured SSH host (default: this machine)")
    t.add_argument("--cwd", default=None)
    t.add_argument("--shell", default=None, help="bash, zsh, sh or fish (local only)")
    t.add_argument("--title", default=None)
    t.add_argument("--readonly", action="store_true")
    t.add_argument("--command", default=None, help="text typed into the shell after start")
    t.add_argument("--cols", type=int, default=120)
    t.add_argument("--rows", type=int, default=32)
    t.set_defaults(func=cmd_terminal_new)
    t = tsub.add_parser("ls", help="list terminals")
    t.add_argument("--host", default=None)
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_terminal_ls)
    t = tsub.add_parser("send", help="write literal text into a terminal")
    t.add_argument("target")
    t.add_argument("text")
    t.add_argument("--enter", action="store_true")
    t.set_defaults(func=cmd_terminal_send)
    t = tsub.add_parser("key", help="send a named key")
    t.add_argument("target")
    t.add_argument("key")
    t.set_defaults(func=cmd_terminal_key)
    t = tsub.add_parser("capture", help="print terminal output")
    t.add_argument("target")
    t.add_argument("--lines", type=int, default=500)
    t.add_argument("--escapes", action="store_true")
    t.set_defaults(func=cmd_terminal_capture)
    t = tsub.add_parser("close", help="close a terminal")
    t.add_argument("target")
    t.set_defaults(func=cmd_terminal_close)
    t = tsub.add_parser("attach", help="attach a terminal to its tmux session")
    t.add_argument("target")
    t.set_defaults(func=cmd_terminal_attach)

    p = sub.add_parser("host", help="remote SSH hosts")
    hsub = p.add_subparsers(dest="host_command", required=True)
    h = hsub.add_parser("list", help="configured hosts and what is observed about them")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_host_list)
    h = hsub.add_parser("add", help="add or edit a host (SSH key auth only)")
    h.add_argument("name", help="short name used by --host (letters, digits, _ . -)")
    h.add_argument("destination", help="user@address of the remote machine")
    h.add_argument("--port", type=int, default=22)
    h.add_argument("--identity", help="private key file (0600)")
    h.add_argument("--known-hosts", dest="known_hosts", help="pinned known_hosts file")
    h.add_argument("--tmux-socket", dest="tmux_socket", help="tmux socket name on the host")
    h.add_argument("--tunnel", action="store_true", help="let remote agents message this daemon")
    h.set_defaults(func=cmd_host_add)
    h = hsub.add_parser("remove", help="remove a host (it must have no agents)")
    h.add_argument("name")
    h.set_defaults(func=cmd_host_remove)
    h = hsub.add_parser("test", help="check the SSH connection and what is installed there")
    h.add_argument("name")
    h.set_defaults(func=cmd_host_test)

    p = sub.add_parser("agent", help="control CLI agents through a semantic harness")
    asub = p.add_subparsers(dest="agent_command", required=True)

    a = asub.add_parser("list", help="list agents (or a team's members)")
    a.add_argument("--team")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent_list)

    a = asub.add_parser("hook", help="(internal) report an agent lifecycle event")
    a.add_argument("event", choices=["session_start", "prompt_submit", "stop", "permission_request"])
    a.set_defaults(func=cmd_agent_hook)

    a = asub.add_parser(
        "new-session",
        help="start a clean conversation in an agent (Claude /clear, OpenCode /new)",
    )
    a.add_argument("target", help="agent name or id (a teammate, when run from an agent)")
    a.add_argument("--timeout", type=float, default=20.0)
    a.set_defaults(func=cmd_agent_new_session)

    a = asub.add_parser("identity", help="show your identity, Teams and teammates")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent_identity)

    a = asub.add_parser("create", help="create and launch an agent")
    a.add_argument("-t", "--kind", default=None, help="harness kind (default: opencode)")
    a.add_argument("-n", "--name")
    a.add_argument("-p", "--profile", default=None,
                   help="defaults from ~/.config/crewhall/profiles.toml")
    a.add_argument("-b", "--backend", default=None, help="backend (default: auto)")
    a.add_argument("-c", "--cwd")
    a.add_argument("--host", default=None,
                   help="run the agent in tmux on a configured SSH host (settings.json hosts)")
    a.add_argument("--team", default=None, help="team to join (inherits its workspace)")
    a.add_argument("--cols", type=int, default=120)
    a.add_argument("--rows", type=int, default=40)
    a.add_argument("--wait", action="store_true", help="wait until ready")
    a.add_argument("--timeout", type=float, default=30.0)
    a.add_argument(
        "--args",
        default=None,
        metavar="ARGS",
        help='extra arguments for the agent command, e.g. --args="--agent reviewer" '
        "(use the = form so a leading dash is not parsed as an option)",
    )
    a.set_defaults(func=cmd_agent_create)

    a = asub.add_parser("send", help="send a semantic prompt")
    a.add_argument("target")
    a.add_argument("prompt")
    a.add_argument("--timeout", type=float, default=30.0)
    a.set_defaults(func=cmd_agent_send)

    a = asub.add_parser("capture", help="print agent output")
    a.add_argument("target")
    a.add_argument("--recent", action="store_true")
    a.add_argument("--lines", type=int, default=40)
    a.set_defaults(func=cmd_agent_capture)

    a = asub.add_parser("state", help="show semantic agent state")
    a.add_argument("target")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent_state)

    a = asub.add_parser("wait", help="wait for a semantic state")
    a.add_argument("target")
    a.add_argument(
        "state",
        choices=["starting", "ready", "working", "waiting_input", "exited", "error"],
    )
    a.add_argument("--timeout", type=float, default=30.0)
    a.set_defaults(func=cmd_agent_wait)

    a = asub.add_parser("stop", help="stop an agent")
    a.add_argument("target")
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=cmd_agent_stop)

    a = asub.add_parser("message", help="send a message from one agent to another")
    a.add_argument("sender")
    a.add_argument("recipient")
    a.add_argument("body")
    a.set_defaults(func=cmd_agent_message)

    a = asub.add_parser("messages", help="show agent message history")
    a.add_argument("--agent")
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent_messages)

    t = asub.add_parser("team", help="manage logical teams of agents")
    tsub = t.add_subparsers(dest="team_command", required=True)

    tc = tsub.add_parser("create", help="create a team (optionally with a workspace)")
    tc.add_argument("name")
    tc.add_argument("agents", nargs="*")
    tc.add_argument("-w", "--workspace", default=None,
                    help="shared workspace directory (default cwd for its agents; "
                         "a path on the host with --host)")
    tc.add_argument("--host", default=None,
                    help="configured SSH host: the team's agents run there, the workspace is "
                         "a directory on it")
    tc.set_defaults(func=cmd_team_create)

    tu = tsub.add_parser("up", help="create a team and its agents from a team.toml/json file")
    tu.add_argument("file")
    tu.set_defaults(func=cmd_team_up)

    tw = tsub.add_parser("set-workspace", help="set/replace a team's workspace")
    tw.add_argument("target")
    tw.add_argument("workspace", nargs="?", default=None)
    tw.add_argument("--host", default=None,
                    help="move an empty team to this host ('' = back to this machine)")
    tw.set_defaults(func=cmd_team_set_workspace)

    tl = tsub.add_parser("list", help="list teams")
    tl.add_argument("--json", action="store_true")
    tl.set_defaults(func=cmd_team_list)

    ti = tsub.add_parser("info", help="show a team and its members")
    ti.add_argument("target")
    ti.add_argument("--json", action="store_true")
    ti.set_defaults(func=cmd_team_info)

    tr = tsub.add_parser("remove", help="remove a team (agents keep running)")
    tr.add_argument("target")
    tr.set_defaults(func=cmd_team_remove)

    tam = tsub.add_parser("add-member", help="add an existing agent to a team")
    tam.add_argument("target")
    tam.add_argument("agent")
    tam.set_defaults(func=cmd_team_add_member)

    trm = tsub.add_parser("remove-member", help="remove an agent from a team")
    trm.add_argument("target")
    trm.add_argument("agent")
    trm.set_defaults(func=cmd_team_remove_member)

    p = sub.add_parser("message", help="agent-to-agent messaging (Team-scoped)")
    msub = p.add_subparsers(dest="message_command", required=True)
    ms = msub.add_parser("send", help="send a message to a teammate")
    ms.add_argument("--to", required=True)
    ms.add_argument("--message", required=True)
    ms.set_defaults(func=cmd_message_send)

    mr = msub.add_parser("request", help="ask a teammate and wait for their reply")
    mr.add_argument("--to", required=True)
    mr.add_argument("--task", required=True)
    mr.add_argument("--timeout", type=float, default=None)
    mr.add_argument("--no-wait", action="store_true", help="return the request id without waiting")
    mr.set_defaults(func=cmd_message_request)

    mp = msub.add_parser("reply", help="reply to a request you received")
    mp.add_argument("--request-id", required=True)
    mp.add_argument("--message", required=True)
    mp.set_defaults(func=cmd_message_reply)

    ml = msub.add_parser("requests", help="list open requests")
    ml.add_argument("--all", action="store_true", help="include closed requests")
    ml.add_argument("--json", action="store_true")
    ml.set_defaults(func=cmd_message_requests)

    p = sub.add_parser("daemon", help="manage the controller daemon")
    p.add_argument("action", choices=["run", "status", "stop", "logs"])
    p.add_argument("--socket")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("demo", help="run the built-in end-to-end demo")
    p.add_argument("-b", "--backend", default="all")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("web", help="run the Web UI (client of the engine)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--socket", default=None)
    p.add_argument("--tailscale", action="store_true",
                   help="bind to this machine's Tailscale IPv4 address")
    p.add_argument("--require-auth", action="store_true",
                   help="require an access token even on localhost")
    p.add_argument("--allow-host", action="append", default=None)
    wsub = p.add_subparsers(dest="web_action")
    wt = wsub.add_parser("token", help="manage the Web UI access token")
    wt.add_argument("token_action", choices=["generate", "revoke", "show"])
    wt.add_argument("--rotate", action="store_true")
    wt.set_defaults(func=cmd_web)
    ws = wsub.add_parser("status", help="show Web UI security/tailscale status")
    ws.set_defaults(func=cmd_web)
    p.set_defaults(func=cmd_web)

    for name, helptext in (
        ("local", "serve the Web UI on 127.0.0.1 only (switches off tailscale)"),
        ("tailscale", "serve the Web UI over Tailscale only (token required; switches off local)"),
        ("off", "stop serving every Web interface (agents keep running)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--port", type=int, default=None, help="port (default 8765, remembered)")
        if name == "tailscale":
            p.add_argument("--keep-local", action="store_true", help="also keep the local interface")
        p.set_defaults(func=cmd_frontend)
    p = sub.add_parser("frontends", help="show which Web interfaces are enabled")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_frontends)

    p = sub.add_parser("update", help="update this managed installation to a verified release")
    p.add_argument("--from", dest="source", default=None, metavar="DIR_OR_URL",
                   help="release directory (release.json + tarball + .sig) or an http(s) base URL")
    p.add_argument("--check", action="store_true", help="only report whether an update is available")
    p.add_argument("--status", action="store_true", help="show installed/daemon versions and rollback state")
    p.add_argument("--rollback", action="store_true", help="swap back to the previous version")
    p.add_argument("--restart", action="store_true",
                   help="also restart the daemon (refused while agents run, unless --force-restart)")
    p.add_argument("--force-restart", action="store_true", help="restart even if agents are running (closes them)")
    p.add_argument("--force", action="store_true", help="reinstall the same version / ignore schema warning")
    p.add_argument("--allow-downgrade", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("selftest", help="real end-to-end check with throw-away agents (spends a few tokens)")
    p.add_argument("--cwd", default=os.getcwd(), help="a folder the agent CLIs already trust (default: here)")
    p.add_argument("--kinds", default="claude,opencode", help="comma-separated agent kinds to test")
    p.add_argument("--quick", action="store_true", help="skip the interrupt step")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("doctor", help="check this machine's environment")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("clean", help="remove test leftovers, old backups and old releases")
    p.add_argument("--dry-run", action="store_true", help="only list what would be removed (default)")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p.add_argument("--keep-backups", type=int, default=clean.KEEP_BACKUPS,
                   help="how many recent backups to keep per group (default: %(default)s)")
    p.add_argument("--max-age-days", type=int, default=None,
                   help="only remove temp dirs older than this many days")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("bundle", help="export/import your configuration (profiles, team files)")
    bsub = p.add_subparsers(dest="bundle_command", required=True)
    be = bsub.add_parser("export", help="write a portable config bundle (.tar.gz)")
    be.add_argument("path")
    be.add_argument("--with-state", action="store_true", help="include teams/agents state")
    be.add_argument("--no-live", action="store_true", help="do not include the teams/agents running now")
    be.add_argument("--with-token", action="store_true", help="include the web access token (sensitive)")
    be.set_defaults(func=cmd_bundle)
    bi = bsub.add_parser("import", help="restore a bundle on this machine")
    bi.add_argument("path")
    bi.add_argument("--force", action="store_true", help="overwrite existing files (keeps .bak)")
    bi.add_argument("--dry-run", action="store_true")
    bi.add_argument("--apply", action="store_true", help="also create the bundle's teams and agents (skips those whose directory is missing)")
    bi.set_defaults(func=cmd_bundle)

    p = sub.add_parser("adapter", help="scaffold a new provider adapter")
    asub = p.add_subparsers(dest="adapter_command", required=True)
    an = asub.add_parser("new", help="create the adapter skeleton and contract test")
    an.add_argument("kind", help="short lowercase identifier, e.g. codex")
    an.set_defaults(func=cmd_adapter)

    p = sub.add_parser("service", help="install/manage the systemd user service")
    p.add_argument(
        "action",
        choices=["unit", "install", "uninstall", "status",
                 "web-unit", "install-web", "uninstall-web"],
    )
    p.add_argument("--web-host", default="127.0.0.1")
    p.add_argument("--web-port", type=int, default=8765)
    p.add_argument("--web-tailscale", action="store_true")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("ui", help="launch the interactive TUI")
    p.add_argument("--local", action="store_true", help="embed a Controller (no daemon)")
    p.add_argument("--cwd", default=None, help="working dir for created agents")
    p.add_argument("--socket", default=None, help="daemon socket path")
    p.add_argument("--refresh", type=float, default=0.8)
    p.set_defaults(func=cmd_ui)

    return parser


def cmd_adapter(args: argparse.Namespace) -> int:
    from . import adapter_gen

    try:
        paths = adapter_gen.create(args.kind)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("created:")
    for path in paths:
        print(f"  {path}")
    print("Now implement the adapter, make the contract kit pass and only then "
          "register it in crewhall/harness/__init__.py.")
    return 0


def cmd_sendline(args: argparse.Namespace) -> int:
    return cmd_send(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SessionTimeout as exc:
        print(f"timeout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
