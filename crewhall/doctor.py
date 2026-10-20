"""Environment diagnostics: ``crewhall doctor``.

Every check returns ``{name, status, detail, hint}`` with status ``ok``,
``warn`` or ``fail``. Only ``fail`` makes the command exit non-zero: it means
crewhall cannot work at all on this machine. ``warn`` is something
optional or worth a look (for example only one of the two agent CLIs present).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from typing import Any
from collections.abc import Callable

from . import __version__, brand, paths

Check = dict[str, Any]


def _res(name: str, status: str, detail: str = "", hint: str = "") -> Check:
    return {"name": name, "status": status, "detail": detail, "hint": hint}


def _version_of(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        text = (out.stdout or out.stderr).strip().splitlines()
        return text[0] if text else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 11)
    return _res(
        "python", "ok" if ok else "fail", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
        "" if ok else "Python 3.11 or newer is required (tomllib).",
    )


def check_tmux() -> Check:
    path = shutil.which("tmux")
    if not path:
        return _res("tmux", "fail", "not found",
                    "Install tmux (e.g. `sudo pacman -S tmux` / `sudo apt install tmux`). "
                    "It is the default backend for agents. Without it agents use the "
                    "PTY backend and do NOT survive a `crewhall daemon stop`/restart: "
                    "their conversation is lost.")
    return _res("tmux", "ok", f"{_version_of(['tmux', '-V'])} ({path})")


def check_agent_clis() -> list[Check]:
    results = []
    found = 0
    for name in ("claude", "opencode"):
        path = shutil.which(name)
        if path:
            found += 1
            results.append(_res(f"cli:{name}", "ok", f"{_version_of([name, '--version'])} ({path})"))
        else:
            results.append(_res(f"cli:{name}", "warn", "not found",
                                f"Install the `{name}` CLI to create {name} agents."))
    if not found:
        results.append(_res("cli:any", "fail", "neither claude nor opencode is installed",
                            "Install at least one agent CLI."))
    return results


def check_command_on_path() -> Check:
    path = shutil.which("crewhall")
    if path:
        return _res("path", "ok", f"crewhall -> {path} (v{__version__})")
    return _res("path", "warn", "`crewhall` is not on PATH",
                'Add ~/.local/bin to PATH (e.g. export PATH="$HOME/.local/bin:$PATH").')


def _writable_dir(path: str, create: bool = True) -> tuple[bool, str]:
    try:
        if create:
            os.makedirs(path, mode=0o700, exist_ok=True)
        probe = os.path.join(path, f".doctor-{os.getpid()}")
        with open(probe, "w") as fh:
            fh.write("x")
        os.unlink(probe)
        return True, ""
    except OSError as exc:
        return False, exc.strerror or str(exc)


def check_dirs() -> list[Check]:
    out = []
    for label, getter in (("state dir", paths.state_dir), ("runtime dir", paths.runtime_dir)):
        path = getter()
        ok, why = _writable_dir(path)
        mode = ""
        if ok:
            mode = oct(os.stat(path).st_mode & 0o777)
        loose = ok and (os.stat(path).st_mode & 0o077) != 0
        status = "fail" if not ok else ("warn" if loose else "ok")
        out.append(_res(label, status, f"{path} {mode}".strip() if ok else f"{path}: {why}",
                        "Directory must be writable." if not ok
                        else ("Permissions are looser than 0700." if loose else "")))
    return out


def check_tmpdir() -> Check:
    tmp = os.environ.get("TMPDIR")
    if tmp and not os.path.isabs(tmp):
        return _res("TMPDIR", "warn", f"relative TMPDIR={tmp!r}",
                    "A relative TMPDIR scatters temp dirs in project folders; unset it.")
    return _res("TMPDIR", "ok", tmp or "(default; agents get a dedicated one)")


def check_daemon() -> Check:
    from .client import Client, ping

    try:
        if not ping():
            return _res("daemon", "warn", "not running",
                        "It starts automatically with any command (or `crewhall daemon status`).")
        info = Client(autostart=False).call("ping")
        return _res("daemon", "ok", f"running ({paths.socket_path()}) {info.get('version', '')}".strip())
    except Exception as exc:  # noqa: BLE001
        return _res("daemon", "warn", f"unreachable: {exc}")


def check_workers() -> Check:
    """Report supervised background workers (crashes/restarts)."""
    from .client import Client, ping

    try:
        if not ping():
            return _res("workers", "ok", "daemon not running")
        health = Client(autostart=False).call("health")
    except Exception as exc:  # noqa: BLE001
        return _res("workers", "warn", f"health unavailable: {exc}")
    workers = health.get("workers", [])
    restarts = sum(int(w.get("restarts", 0)) for w in workers)
    dead = [w for w in workers if w.get("state") == "dead"]
    detail = f"{len(workers)} worker(s), {restarts} restart(s)"
    if dead:
        return _res("workers", "warn", detail,
                    "dead: " + ", ".join(str(w.get("name")) for w in dead))
    return _res("workers", "ok", detail)


def check_daemon_log() -> Check:
    """Report the daemon log size and whether rotation is in effect."""
    import glob

    path = paths.log_path()
    try:
        total = os.path.getsize(path)
    except OSError:
        return _res("log", "ok", "no daemon.log yet")
    rotated = glob.glob(path + ".*")
    total += sum(os.path.getsize(p) for p in rotated if os.path.isfile(p))
    size = f"{total / (1024 * 1024):.1f} MiB"
    detail = f"{size} ({len(rotated)} rotated file(s))"
    if len(rotated) == 0 and total > 64 * 1024 * 1024:
        return _res("log", "warn", detail,
                    "daemon.log looks unrotated; it rotates automatically at the configured size.")
    return _res("log", "ok", detail)


def check_web(port: int = 8765) -> list[Check]:
    from .client import Client, ping
    from .web import auth

    out = []
    status = None
    try:
        if ping():
            status = Client(autostart=False).call("frontend_status")
    except Exception:  # noqa: BLE001 - an older daemon without the op, or unreachable
        status = None
    if status is not None:
        active = [f"{n} {f['url']}" for n, f in status["frontends"].items() if f["running"]]
        errors = [f"{n}: {f['error']}" for n, f in status["frontends"].items() if f.get("error")]
        if errors:
            out.append(_res("web", "warn", "; ".join(errors),
                            "Fix the cause and run `crewhall tailscale` / `local` again."))
        elif active:
            out.append(_res("web", "ok", f"mode {status['mode']}: " + ", ".join(active)))
        else:
            out.append(_res("web", "ok", "off (enable with `crewhall local` or `crewhall tailscale`)"))
    else:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                out.append(_res("web", "ok", f"standalone server on 127.0.0.1:{port}"))
        except OSError:
            out.append(_res("web", "warn", "web interface is off",
                            "Enable it with `crewhall local` or `crewhall tailscale`."))
    if auth.token_exists():
        perm_ok = auth.file_permissions_ok()
        out.append(_res("web token", "ok" if perm_ok else "warn",
                        f"configured ({auth.token_fingerprint()})",
                        "" if perm_ok else "Token file must be mode 0600."))
    else:
        out.append(_res("web token", "warn", "not configured",
                        "Needed for remote access: `crewhall web token generate`."))
    return out


def check_systemd() -> Check:
    if not shutil.which("systemctl"):
        return _res("systemd", "warn", "systemctl not found", "Services are optional; run daemon/web manually.")
    try:
        out = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True,
                             text=True, timeout=10)
        state = (out.stdout or out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return _res("systemd", "warn", "user manager unreachable")
    units = [u for u in (*brand.SYSTEMD_UNITS, *brand.LEGACY_SYSTEMD_UNITS)
             if os.path.exists(os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", u))]
    return _res("systemd", "ok" if state in ("running", "degraded") else "warn",
                f"user manager: {state}; installed units: {', '.join(units) or 'none'}")


def check_agent_version_ranges() -> list[Check]:
    """Warn when an installed agent CLI falls outside its validated range.

    Adapters key on phrases from the TUI; an upstream release can move them. We
    cannot test against every version, so we flag the ones we have not verified.
    The command checked is the *effective* one (a configured provider command,
    with the configured fallback already auto-selected).
    """
    from . import settings

    out: list[Check] = []
    allowed = bool(settings.get("agents.allow_unvalidated_versions"))
    for kind in ("claude", "codex", "opencode"):
        try:
            argv, report, used_fallback = settings.provider_effective_command(kind)
        except Exception as exc:  # noqa: BLE001 - a bad provider must not hide the rest
            out.append(_res(f"cli:{kind} version", "warn", f"cannot resolve command: {exc}"))
            continue
        if not argv:
            continue
        detail = report.text or "unknown"
        if report.supported is False:
            if allowed:
                out.append(_res(f"cli:{kind} version", "warn",
                                f"{detail} outside validated range",
                                "allowed by agents.allow_unvalidated_versions"))
            else:
                hint = (f"Using fallback {argv[0]!r} but it is also out of range."
                        if used_fallback
                        else f"Last verified against {report.captured}. "
                             "Configure a supported binary or a fallback_command, "
                             "then re-check the adapter and ADAPTERS.md.")
                out.append(_res(f"cli:{kind} version", "warn",
                                f"{detail} outside validated range", hint))
        elif report.supported is True:
            suffix = f" (fallback {argv[0]})" if used_fallback else ""
            out.append(_res(f"cli:{kind} version", "ok", f"{detail}{suffix}"))
    return out


def check_tailscale() -> Check:
    path = shutil.which("tailscale")
    return _res("tailscale", "ok" if path else "warn",
                path or "not installed", "" if path else "Only needed for remote access over a tailnet.")


def check_install() -> list[Check]:
    from . import updater
    from .buildinfo import describe

    st = updater.status()
    out = [_res("version", "ok" if st["channel"] == "release" or not st["managed"] else "warn",
                f"{describe()}" + (" — managed install" if st["managed"] else " — not a managed install"))]
    if st["restart_pending"]:
        out.append(_res("daemon code", "warn",
                        f"daemon runs {st['daemon_version']} but {st['version']} is installed",
                        "Restart it when no agents are running: crewhall update --restart"))
    return out


def check_hooks() -> Check:
    from . import hooks

    try:
        return _res("hooks", "ok", hooks.ensure_claude_settings())
    except OSError as exc:
        return _res("hooks", "warn", f"cannot write hook settings: {exc.strerror}")


def check_security() -> list[Check]:
    """File modes, local access policy and provider binaries."""
    import stat as _stat

    from . import audit, settings
    from .web import auth

    out: list[Check] = []
    if auth.token_exists():
        ok = auth.file_permissions_ok()
        out.append(_res("token perms", "ok" if ok else "warn",
                        "0600" if ok else "looser than 0600",
                        "" if ok else "chmod 600 the token file."))
    sp = settings.path()
    if os.path.isfile(sp):
        mode = _stat.S_IMODE(os.stat(sp).st_mode)
        out.append(_res("settings perms", "ok" if mode == 0o600 else "warn", oct(mode),
                        "" if mode == 0o600 else "The settings file should be mode 0600."))
    ap = audit.audit_path()
    if os.path.isfile(ap):
        ok = audit.file_permissions_ok()
        out.append(_res("audit perms", "ok" if ok else "warn",
                        "0600" if ok else "looser than 0600",
                        "" if ok else "The audit log should be mode 0600."))
    if settings.get("security.local_requires_token"):
        out.append(_res("local access", "ok", "token required for localhost too"))
    else:
        others = settings.other_interactive_users()
        out.append(_res("local access", "warn" if others else "ok",
                        "other interactive users: " + ", ".join(others[:5]) if others else "only you",
                        "Turn on security.local_requires_token (Settings → Access)." if others else ""))
    for kind in settings._kinds():
        for risk in settings.provider_risks(kind):
            if "writable" in risk or "outside" in risk:
                out.append(_res(f"provider:{kind}", "warn", risk,
                                "Use a binary only you can write, inside PATH or $HOME."))
    return out


def check_agent_configs() -> list[Check]:
    """Read-only look at permissive CLI configs (never modified)."""
    out: list[Check] = []
    home = os.path.expanduser("~")
    codex = os.path.join(home, ".codex", "config.toml")
    if os.path.isfile(codex):
        try:
            with open(codex, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        risky = 'approval_policy = "never"' in text or "approval_policy='never'" in text
        out.append(_res("codex config", "warn" if risky else "ok",
                        "approval_policy = never (commands run without asking)" if risky
                        else "no permissive approval policy detected",
                        "Codex will run commands without approval." if risky else ""))
    claude = os.path.join(home, ".claude", "settings.json")
    if os.path.isfile(claude):
        try:
            with open(claude, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        risky = "bypassPermissions" in text or "dangerously-skip-permissions" in text
        out.append(_res("claude config", "warn" if risky else "ok",
                        "permissive permissions mode detected" if risky else "no permissive mode detected",
                        "Claude may skip permission prompts." if risky else ""))
    return out


def check_office() -> Check:
    """OnlyOffice is optional, but if it is enabled it must be reachable.

    Enabled + unreachable is a warning, not a failure: crewhall works without it
    (chat falls back to plain file artifacts). The hint points at the one command
    that fixes it.
    """
    import urllib.error
    import urllib.request

    from . import office, settings

    if not settings.get("office.enabled"):
        return _res("onlyoffice", "ok", "off (enable with `crewhall office setup`)")
    public = str(settings.get("office.public_url") or "").rstrip("/")
    if not public:
        return _res("onlyoffice", "warn", "enabled but office.public_url is unset",
                    "Run `crewhall office setup` or set office.public_url.")
    try:
        with urllib.request.urlopen(f"{public}/healthcheck", timeout=4) as resp:
            reachable = resp.status == 200
    except (urllib.error.URLError, OSError):
        reachable = False
    if not reachable:
        return _res("onlyoffice", "warn", f"enabled but not answering at {public}",
                    "Start it with `crewhall office setup`, or fix office.public_url.")
    if not str(settings.get("office.base_url") or "").strip():
        return _res("onlyoffice", "warn", f"healthy at {public}, but office.base_url is unset",
                    "The Document Server cannot call back without it: `crewhall office setup`.")
    if office.jwt_enabled() and not office.jwt_active():
        return _res("onlyoffice", "warn", "JWT enabled but office.jwt_secret is empty",
                    "Set office.jwt_secret to the same value as the Document Server.")
    return _res("onlyoffice", "ok", f"healthy at {public}")


def check_agent_runtime_versions() -> list[Check]:
    """Warn when a live agent runs a different CLI than the configured binary.

    An adopted (legacy) agent can keep a v1 TUI alive after the configured
    ``opencode`` moved to v2. Reporting it catches "crewhall says v2 but the TUI
    shows v1" before a restart silently loses the conversation.
    """
    from .client import Client, ping

    try:
        if not ping():
            return [_res("runtime versions", "ok", "daemon not running")]
        agents = Client(autostart=False).call("agent_list").get("agents", [])
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
        return [_res("runtime versions", "warn", f"unavailable: {exc}")]
    out: list[Check] = []
    for agent in agents:
        if agent.get("kind") != "opencode":
            continue
        launch = agent.get("launch") or {}
        cli = agent.get("cli") or {}
        running = agent.get("runtime_version") or launch.get("version")
        configured = cli.get("version")
        if running and configured and running != configured:
            out.append(_res(
                f"runtime:{str(agent.get('name') or agent.get('agent_id'))[:12]}", "warn",
                f"process {running}, configured {configured}",
                "The live TUI/service was started by another version. Restart the "
                "agent to move it onto the configured binary (a v1 conversation is "
                "not resumable in v2; export it first).",
            ))
    if not out:
        out.append(_res("runtime versions", "ok", "live agents match the configured CLIs"))
    return out


CHECKS: list[Callable[[], Check | list[Check]]] = [
    check_python, check_install, check_tmux, check_agent_clis, check_agent_version_ranges,
    check_agent_runtime_versions,
    check_command_on_path, check_dirs,
    check_tmpdir, check_hooks, check_daemon, check_workers, check_daemon_log, check_web, check_office, check_security,
    check_agent_configs, check_systemd, check_tailscale,
]


def fix_control_files(dirs: list[str] | None = None) -> list[Check]:
    """Re-generate stale managed control-file sections under likely roots.

    The managed block is normally refreshed when an agent is launched with a
    directory as its cwd; a directory that is never used as a cwd (e.g. the home
    directory) can keep an old block (still named ``agent-terminal``) forever.
    This scans the home directory, the cwd and its ancestors for existing
    ``CLAUDE.md``/``AGENTS.md`` and refreshes their block in place.
    """
    from . import control_files

    if not control_files.enabled():
        return [_res("control files", "warn", "disabled by CREWHALL_CONTROL_FILES")]

    seen: set[str] = set()
    candidates: list[str] = []

    def add(directory: str) -> None:
        directory = os.path.abspath(directory)
        if directory and directory not in seen and os.path.isdir(directory):
            seen.add(directory)
            candidates.append(directory)

    add(os.path.expanduser("~"))
    candidates_root = list(dirs) if dirs is not None else None
    if candidates_root is None:
        current = os.getcwd()
        while True:
            add(current)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    else:
        for directory in candidates_root:
            add(directory)

    out: list[Check] = []
    changed = 0
    for directory in candidates:
        for name in control_files.CONTROL_FILENAMES:
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            result = control_files.refresh_control_file(path)
            if result is None:
                continue
            if result.created or result.updated:
                changed += 1
                out.append(_res("control file", "ok", f"updated {path}"))
            elif result.skipped:
                out.append(_res("control file", "warn", f"{path}: {result.skipped}"))
    if changed == 0 and not out:
        out.append(_res("control file", "ok", "managed sections already up to date"))
    return out


def run_checks() -> list[Check]:
    results: list[Check] = []
    for fn in CHECKS:
        try:
            r = fn()
        except Exception as exc:  # noqa: BLE001 - a broken check must not hide the others
            r = _res(fn.__name__.removeprefix("check_"), "warn", f"check crashed: {exc}")
        results.extend(r if isinstance(r, list) else [r])
    return results


def format_report(results: list[Check]) -> str:
    mark = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = [f"crewhall {__version__} — environment check", ""]
    for r in results:
        lines.append(f" {mark[r['status']]} {r['name']:<14} {r['detail']}")
        if r["hint"] and r["status"] != "ok":
            lines.append(f"   ↳ {r['hint']}")
    fails = sum(r["status"] == "fail" for r in results)
    warns = sum(r["status"] == "warn" for r in results)
    lines += ["", f"{fails} problem(s), {warns} warning(s)." if fails or warns else "Everything looks good."]
    return "\n".join(lines)


def exit_code(results: list[Check]) -> int:
    return 1 if any(r["status"] == "fail" for r in results) else 0
