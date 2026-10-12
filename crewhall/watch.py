"""Long-duration watch: CLI versions, screen captures and the systemd timers.

The adapters read the agent TUIs from the screen, so the real risk of an
upstream release is a moved phrase. This module implements the first layers of
the long-duration plan:

1. ``watch versions`` compares the installed CLI versions with the ones cached
   in ``state/health/versions.json``; a change triggers layer 2 and a desktop
   notification.
2. ``watch screens`` starts each CLI in a *private* tmux socket (no prompt),
   captures the screen with escapes and runs the adapter detectors (READY,
   footer, input box, ghost text), saving the capture under
   ``tests/fixtures/screens/<cli>-<version>.txt``.
7. ``watch install`` installs and enables the user systemd timers (``--remove``
   uninstalls them).

Nothing here ever starts an agent in the production tmux socket or sends a
prompt to a real agent.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, cast

from . import brand, health, paths
from .harness import AgentState, get_harness
from .harness.versions import parse_version

KINDS = ("claude", "codex", "opencode")


def screens_dir() -> str:
    return os.path.join(paths.project_root(), "tests", "fixtures", "screens")


def notify(title: str, body: str, *, sender: str | None = None) -> bool:
    """Desktop notification (best effort); returns whether it was sent."""
    if sender is None:
        sender = brand.NAME
    if shutil.which("notify-send") is None:
        return False
    try:
        subprocess.run(["notify-send", "-a", sender, title, body],
                       capture_output=True, timeout=10, check=False)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# -- layer 2: adapter screen detectors ----------------------------------------

class _StaticSession:
    """Minimal session double: captures a fixed screen, with escapes."""

    def __init__(self, text: str, escapes: str | None = None) -> None:
        self._text = text
        self._escapes = text if escapes is None else escapes

    def capture(self, escapes: bool = False) -> str:
        return self._escapes if escapes else self._text


def _harness_for(kind: str, text: str, escapes: str | None = None) -> Any:
    session = cast(Any, _StaticSession(text, escapes))
    return get_harness(kind)(session, name=kind)


# Ghost/draft probes: a plain suggestion must read as empty input and a real
# draft as content. Renderings mirror what each TUI actually draws.
_draft_probe_text = {
    "claude": "\u276f hello world\n  auto mode on (shift+tab to cycle)\n",
    "codex": "\u203a hello world\n  \u2190 for agents \u00b7 ? for shortcuts\n",
    "opencode": "\u2503 hello world\n\u2503 Build \u00b7 gpt\n\u2579\u2580\u2580\u2580\u2580\n",
}


def screen_report(kind: str, text: str, escapes: str | None = None) -> dict[str, bool]:
    """Run every detector for ``kind`` on a captured screen."""
    harness = _harness_for(kind, text, escapes)
    harness_cls = get_harness(kind)
    try:
        state, _evidence = harness._detect(text)
        ready = state in (AgentState.READY, AgentState.WAITING_INPUT)
    except Exception:  # noqa: BLE001 - a broken adapter must report, not crash
        ready = False
    try:
        input_box = bool(harness_cls.screen_input_box(text))
    except Exception:  # noqa: BLE001
        input_box = False
    try:
        footer = bool(harness_cls.screen_footer(text))
    except Exception:  # noqa: BLE001
        footer = False
    try:
        suggestion_reads_empty = harness.input_line().strip() == ""
        draft = _harness_for(kind, _draft_probe_text[kind]).input_line().strip()
        ghost = suggestion_reads_empty and draft == "hello world"
    except Exception:  # noqa: BLE001
        ghost = False
    return {"ready": ready, "footer": footer, "input_box": input_box, "ghost": ghost}


def failing_detectors(report: dict[str, Any]) -> list[str]:
    return [name for name, ok in report.items() if not ok]


# -- reading versions ---------------------------------------------------------

def cli_version(kind: str, timeout: float = 15.0) -> str | None:
    """First line of ``<kind> --version`` (``None`` when absent/unreadable)."""
    if shutil.which(kind) is None:
        return None
    try:
        out = subprocess.run([kind, "--version"], capture_output=True, text=True,
                             timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (out.stdout or out.stderr).strip().splitlines()
    return lines[0].strip() if lines else None


def current_versions(kinds: tuple[str, ...] = KINDS) -> dict[str, str | None]:
    return {kind: cli_version(kind) for kind in kinds}


# -- layer 1: versions + trigger ----------------------------------------------

def changed_kinds(previous: dict[str, Any], current: dict[str, str | None],
                  kinds: tuple[str, ...] = KINDS) -> list[str]:
    return [k for k in kinds if current.get(k) and previous.get(k) != current.get(k)]


def check_versions(kinds: tuple[str, ...] = KINDS, *, notify_change: bool = True,
                   capture: bool = True) -> dict[str, Any]:
    previous = health.read_versions().get("versions", {})
    current = current_versions(kinds)
    changed = changed_kinds(previous, current, kinds)
    health.write_versions(current)
    report: dict[str, Any] = {
        "changed": changed, "previous": previous, "versions": current,
        "screens": None,
    }
    if changed and capture:
        report["screens"] = capture_screens(tuple(changed))
    if changed and notify_change:
        detail = ", ".join(f"{k}: {previous.get(k) or '?'} -> {current.get(k)}" for k in changed)
        notify("crewhall: agent CLI updated", detail or "a CLI version changed")
    return report


# -- layer 2: real captures ---------------------------------------------------

def _sanitize(text: str) -> str:
    home = os.path.expanduser("~")
    if home and home not in ("/", "/home"):
        text = text.replace(home, "/home/user")
    return text


def _version_label(kind: str, text: str) -> str:
    parsed = parse_version(text)
    if parsed:
        return ".".join(str(p) for p in parsed)
    raw = cli_version(kind) or ""
    parsed = parse_version(raw)
    return ".".join(str(p) for p in parsed) if parsed else "unknown"


def capture_screens(kinds: tuple[str, ...] = KINDS, *, socket: str | None = None,
                    settle: float = 6.0, cwd: str | None = None) -> dict[str, Any]:
    """Capture each CLI's start screen in a private tmux server (no prompt)."""
    results: dict[str, Any] = {}
    if shutil.which("tmux") is None:
        return {kind: {"ok": False, "error": "tmux not found"} for kind in kinds}
    socket = socket or f"crewhall_watch_{os.getpid()}"
    cwd = cwd or os.path.join(health.health_dir(), "screens-work")
    os.makedirs(cwd, mode=0o700, exist_ok=True)
    try:
        for kind in kinds:
            exe = shutil.which(kind)
            if exe is None:
                results[kind] = {"ok": False, "error": f"{kind} not installed"}
                continue
            results[kind] = _capture_one(kind, exe, socket, cwd, settle)
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"],
                       capture_output=True, check=False)
    return results


def _capture_one(kind: str, exe: str, socket: str, cwd: str, settle: float) -> dict[str, Any]:
    name = "watch"
    base = ["tmux", "-L", socket]
    started = subprocess.run(
        [*base, "-f", "/dev/null", "new-session", "-d", "-s", name,
         "-x", "120", "-y", "40", "-c", cwd, exe],
        capture_output=True, text=True, check=False,
    )
    if started.returncode != 0:
        return {"ok": False, "error": f"could not start {kind}: {started.stderr.strip()}"}
    time.sleep(settle)
    plain = subprocess.run([*base, "capture-pane", "-p", "-t", name],
                           capture_output=True, text=True, check=False).stdout
    escaped = subprocess.run([*base, "capture-pane", "-p", "-e", "-t", name],
                             capture_output=True, text=True, check=False).stdout
    plain = _sanitize(plain)
    escaped = _sanitize(escaped)
    report = screen_report(kind, plain, escaped)
    version = _version_label(kind, plain)
    directory = screens_dir()
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{kind}-{version}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(plain)
    return {"ok": not failing_detectors(report), "detectors": report,
            "path": path, "version": version}


def screens_ok(result: dict[str, Any]) -> bool:
    """Whether every capture ran and every detector passed."""
    return bool(result) and all(isinstance(v, dict) and v.get("ok") for v in result.values())


# -- layer 7: systemd user timers ---------------------------------------------

WATCH_UNITS = ("crewhall-watch-versions", "crewhall-watch-smoke", "crewhall-watch-soak")
TIMER_SCHEDULE = {
    "crewhall-watch-versions": "*-*-* 04:00:00",
    "crewhall-watch-smoke": "*-*-* 05:00:00",
    "crewhall-watch-soak": "Sun *-*-* 06:00:00",
}


def _python() -> str:
    return sys.executable or shutil.which("python3") or "python3"


def _watch_command(unit: str) -> list[str]:
    root = paths.project_root()
    if unit == "crewhall-watch-versions":
        return [_python(), "-P", "-m", "crewhall.cli", "watch", "versions"]
    if unit == "crewhall-watch-smoke":
        return [_python(), os.path.join(root, "scripts", "smoke.py"), "--require"]
    return [_python(), os.path.join(root, "scripts", "soak.py")]


def service_text(unit: str) -> str:
    root = paths.project_root()
    return "\n".join([
        "[Unit]",
        f"Description=crewhall watch: {unit.removeprefix('crewhall-watch-')}",
        "Documentation=file://" + os.path.join(root, "RELEASING.md"),
        "After=default.target",
        "",
        "[Service]",
        "Type=oneshot",
        f"Environment=PYTHONPATH={root}",
        "ExecStart=" + " ".join(_watch_command(unit)),
        "Nice=10",
        "",
    ])


def timer_text(unit: str) -> str:
    return "\n".join([
        "[Unit]",
        f"Description=crewhall watch timer: {unit.removeprefix('crewhall-watch-')}",
        "",
        "[Timer]",
        f"OnCalendar={TIMER_SCHEDULE[unit]}",
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])


def unit_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "systemd", "user")


def _write_units() -> list[str]:
    os.makedirs(unit_dir(), exist_ok=True)
    paths_written = []
    for unit in WATCH_UNITS:
        service = os.path.join(unit_dir(), f"{unit}.service")
        timer = os.path.join(unit_dir(), f"{unit}.timer")
        with open(service, "w", encoding="utf-8") as fh:
            fh.write(service_text(unit))
        with open(timer, "w", encoding="utf-8") as fh:
            fh.write(timer_text(unit))
        paths_written += [service, timer]
    return paths_written


def install_timers() -> dict[str, Any]:
    files = _write_units()
    reloaded = _systemctl("daemon-reload")
    enabled = {unit: _systemctl("enable", "--now", f"{unit}.timer") for unit in WATCH_UNITS}
    return {"files": files, "daemon_reload": reloaded, "enable": enabled}


def remove_timers() -> dict[str, Any]:
    for unit in WATCH_UNITS:
        _systemctl("disable", "--now", f"{unit}.timer")
        for ext in ("service", "timer"):
            path = os.path.join(unit_dir(), f"{unit}.{ext}")
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
    _systemctl("daemon-reload")
    return {"removed": list(WATCH_UNITS)}


def _systemctl(*args: str) -> dict[str, Any]:
    if shutil.which("systemctl") is None:
        return {"ok": False, "error": "systemctl not available"}
    try:
        proc = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def format_versions_report(report: dict[str, Any]) -> str:
    lines = []
    for kind, version in report.get("versions", {}).items():
        lines.append(f"  {kind:<10} {version or '(not installed)'}")
    changed = report.get("changed") or []
    lines.append("changed: " + (", ".join(changed) if changed else "none"))
    for screens in (report.get("screens") or {}).values():
        if not isinstance(screens, dict):
            continue
        if screens.get("ok"):
            lines.append(f"  screens ok -> {screens.get('path')}")
        else:
            lines.append(f"  DETECTORS FAILED: {screens.get('error') or failing_detectors(screens.get('detectors', {}))}")
    return "\n".join(lines)


def format_screens_report(result: dict[str, Any]) -> str:
    lines = []
    for kind, info in result.items():
        if not isinstance(info, dict) or not info.get("ok"):
            detail = info.get("error") if isinstance(info, dict) else info
            failed = failing_detectors(info.get("detectors", {})) if isinstance(info, dict) else []
            lines.append(f"  FAIL {kind}: {detail or failed}")
        else:
            lines.append(f"  ok   {kind} -> {info.get('path')}")
    return "\n".join(lines)


def as_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True)
