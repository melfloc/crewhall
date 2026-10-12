"""``crewhall <group>`` command handlers (split from cli.py)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from .. import paths, watch
from ..backends import available
from ..client import Client, RpcError
from ..controller import Controller
from ..daemon import Server
from ..session import InteractiveSession
from ..types import SessionSpec
from . import common
from .common import _human_bytes, _log_matches


def cmd_update(args: argparse.Namespace) -> int:
    from .. import updater

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
                  "crewhall update --restart   # (idle tmux agents survive; refused only if one is working or on PTY)")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from .. import selftest

    print(f"crewhall selftest in {os.path.abspath(args.cwd)}")
    out = selftest.run(os.path.abspath(args.cwd), kinds=args.kinds.split(","), quick=args.quick,
                       log=(lambda m: None) if args.json else print)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print("\nSELFTEST " + ("PASSED" if out["ok"] else "FAILED"))
    return 0 if out["ok"] else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    from .. import doctor

    if getattr(args, "trend", False):
        from .. import health

        report = health.trend_report(getattr(args, "days", 30.0))
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(_format_trend(report))
        return 0
    results = doctor.run_checks()
    if getattr(args, "fix_control_files", False):
        results += doctor.fix_control_files()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(doctor.format_report(results))
    return doctor.exit_code(results)


def _format_trend(report: dict[str, Any]) -> str:
    if not report.get("samples"):
        return "no health journal yet (the daemon writes one sample every 10 minutes)"
    lines = [f"health journal: {report['samples']} sample(s)  {report['since']} -> {report['until']}"]
    for key, label, unit in (
        ("memory_kb", "memory", "KB"), ("threads", "threads", ""),
        ("queue", "queue", ""), ("workers_restarted", "worker restarts", ""),
    ):
        stat = report.get(key)
        if stat:
            lines.append(f"  {label:<16} min={stat['min']:g}{unit} max={stat['max']:g}{unit} "
                         f"last={stat['last']:g}{unit}")
    failures = report.get("failures_last_hour") or {}
    lines.append(f"  {'failures/1h':<16} max={failures.get('max', 0)} sum={failures.get('sum', 0)}")
    lines.append("  versions: " + (", ".join(report.get("versions") or []) or "-"))
    return "\n".join(lines)


def cmd_clean(args: argparse.Namespace) -> int:
    from .. import clean

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
    from .. import bundle

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
                res = bundle.apply_specs(specs, lambda spec: common._client(args).call("team_up", spec=spec))
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


def cmd_daemon(args: argparse.Namespace) -> int:
    if args.action == "status":
        try:
            resp = common._client(args).call("ping")
        except RpcError:
            print("daemon: not running")
            return 1
        print(f"daemon: running pid={resp['pid']} sessions={resp['sessions']}")
        print(f"socket: {paths.socket_path()}")
        return 0
    if args.action == "stop":
        kill_agents = bool(getattr(args, "kill_agents", False))
        try:
            common._client(args).call("shutdown", kill_agents=kill_agents)
        except RpcError as exc:
            print(f"daemon: {exc}", file=sys.stderr)
            return 1
        if kill_agents:
            print("daemon: stopping (agents stopped too)")
        else:
            print("daemon: stopping (agents keep running; use --kill-agents to stop them)")
        return 0
    if args.action == "logs":
        path = paths.log_path()
        if not os.path.exists(path):
            print(f"no log at {path}")
            return 0
        agent = getattr(args, "agent", None)
        if getattr(args, "follow", False):
            return _follow_log(path, agent)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if _log_matches(line, agent):
                    sys.stdout.write(line)
        return 0
    if args.action == "run":
        return _run_foreground(args)
    raise SystemExit(f"unknown daemon action {args.action}")


def _follow_log(path: str, agent: str | None) -> int:
    """`daemon logs --follow`: print new lines as they are written."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.3)
                    continue
                if _log_matches(line, agent):
                    sys.stdout.write(line)
                    sys.stdout.flush()
    except KeyboardInterrupt:
        return 0


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
    from ..ui.app import main as ui_main

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


def cmd_service(args: argparse.Namespace) -> int:
    from .. import service

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


def cmd_watch(args: argparse.Namespace) -> int:
    """Layers 1, 2 and 7: CLI versions, screen captures and the user timers."""
    action = args.watch_command
    if action == "versions":
        report = watch.check_versions(
            notify_change=not getattr(args, "no_notify", False),
            capture=not getattr(args, "no_capture", False),
        )
        if args.json:
            print(watch.as_json(report))
        else:
            print(watch.format_versions_report(report))
        screens = report.get("screens")
        if screens is not None and not watch.screens_ok(screens):
            return 1
        return 0
    if action == "screens":
        kinds = tuple(k for k in (args.kinds or "").split(",") if k)
        result = watch.capture_screens(kinds or watch.KINDS,
                                       save_fixtures=getattr(args, "save_fixtures", False))
        if args.json:
            print(watch.as_json(result))
        else:
            print(watch.format_screens_report(result))
        return 0 if watch.screens_ok(result) else 1
    if action == "install":
        out = watch.remove_timers() if getattr(args, "remove", False) else watch.install_timers()
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    raise SystemExit(f"unknown watch action {action}")


def cmd_demo(args: argparse.Namespace) -> int:
    from ..backends import get_backend

    backend_name = args.backend
    names = (
        [b for b in available() if b != "ssh-tmux"]
        if backend_name == "all" else [backend_name]
    )
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


def cmd_adapter(args: argparse.Namespace) -> int:
    from .. import adapter_gen

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
