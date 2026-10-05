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
                    "It is the default backend for agents.")
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


CHECKS: list[Callable[[], Check | list[Check]]] = [
    check_python, check_install, check_tmux, check_agent_clis, check_command_on_path, check_dirs,
    check_tmpdir, check_hooks, check_daemon, check_web, check_security, check_agent_configs,
    check_systemd, check_tailscale,
]


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
