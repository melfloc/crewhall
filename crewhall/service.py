from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any

from . import brand, paths

UNIT_NAME, WEB_UNIT_NAME = brand.SYSTEMD_UNITS


def _python() -> str:
    return sys.executable or shutil.which("python3") or "python3"


def web_unit_text(*, host: str = "127.0.0.1", port: int = 8765, tailscale: bool = False) -> str:
    root = paths.project_root()
    args = f"--host {host} --port {port}"
    if tailscale:
        args = f"--tailscale --port {port}"
    return "\n".join(
        [
            "[Unit]",
            "Description=crewhall Web UI (client of the control plane)",
            "Documentation=file://" + os.path.join(root, "README.md"),
            "After=crewhall.service",
            "Wants=crewhall.service",
            "",
            "[Service]",
            "Type=simple",
            f'Environment=PYTHONPATH={root}',
            f"ExecStart={_python()} -P -m crewhall.web {args}",
            "Restart=on-failure",
            "RestartSec=1",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def web_unit_path() -> str:
    return os.path.join(unit_dir(), WEB_UNIT_NAME)


def install_web(*, host: str = "127.0.0.1", port: int = 8765, tailscale: bool = False) -> dict[str, Any]:
    os.makedirs(unit_dir(), exist_ok=True)
    with open(web_unit_path(), "w", encoding="utf-8") as fh:
        fh.write(web_unit_text(host=host, port=port, tailscale=tailscale))
    reloaded = _systemctl("daemon-reload")
    enabled = _systemctl("enable", "--now", WEB_UNIT_NAME)
    return {
        "unit_path": web_unit_path(),
        "daemon_reload": reloaded,
        "enable": enabled,
        "content": web_unit_text(host=host, port=port, tailscale=tailscale),
    }


def uninstall_web() -> dict[str, Any]:
    _systemctl("disable", "--now", WEB_UNIT_NAME)
    try:
        os.unlink(web_unit_path())
    except FileNotFoundError:
        pass
    _systemctl("daemon-reload")
    return {"unit_path": web_unit_path(), "removed": True}


def unit_text() -> str:
    root = paths.project_root()
    return "\n".join(
        [
            "[Unit]",
            "Description=crewhall control plane (resident daemon)",
            "Documentation=file://" + os.path.join(root, "README.md"),
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f'Environment=PYTHONPATH={root}',
            f"Environment=TMPDIR={paths.tmpdir_root()}",
            f"ExecStart={_python()} -P -m crewhall.daemon --foreground",
            "Restart=on-failure",
            "RestartSec=1",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def unit_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "systemd", "user")


def unit_path() -> str:
    return os.path.join(unit_dir(), UNIT_NAME)


def _retire_legacy_units() -> None:
    """Stop/remove units installed under the old product name (best effort)."""
    for name in brand.LEGACY_SYSTEMD_UNITS:
        path = os.path.join(unit_dir(), name)
        if os.path.exists(path):
            _systemctl("disable", "--now", name)
            try:
                os.unlink(path)
            except OSError:
                pass


def install() -> dict[str, Any]:
    os.makedirs(unit_dir(), exist_ok=True)
    _retire_legacy_units()
    with open(unit_path(), "w", encoding="utf-8") as fh:
        fh.write(unit_text())
    reloaded = _systemctl("daemon-reload")
    enabled = _systemctl("enable", "--now", UNIT_NAME)
    return {
        "unit_path": unit_path(),
        "daemon_reload": reloaded,
        "enable": enabled,
        "content": unit_text(),
    }


def uninstall() -> dict[str, Any]:
    _systemctl("disable", "--now", UNIT_NAME)
    try:
        os.unlink(unit_path())
    except FileNotFoundError:
        pass
    _systemctl("daemon-reload")
    return {"unit_path": unit_path(), "removed": True}


def status() -> dict[str, Any]:
    return {"unit_path": unit_path(), "status": _systemctl("status", UNIT_NAME)}


def _systemctl(*args: str) -> dict[str, Any]:
    if shutil.which("systemctl") is None:
        return {"ok": False, "error": "systemctl not available"}
    try:
        proc = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }
