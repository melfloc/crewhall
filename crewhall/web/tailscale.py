from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


class TailscaleUnavailable(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("tailscale") is not None


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tailscale", *args], capture_output=True, text=True, timeout=timeout
    )


def status() -> dict[str, Any]:
    """Return Tailscale status, or raise TailscaleUnavailable."""
    if not available():
        raise TailscaleUnavailable("tailscale is not installed")
    try:
        proc = _run(["status", "--json"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TailscaleUnavailable(f"tailscale status failed: {exc}") from exc
    if proc.returncode != 0:
        raise TailscaleUnavailable(
            f"tailscale is not available/configured: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TailscaleUnavailable(f"tailscale status returned bad json: {exc}") from exc


def local_ipv4() -> str | None:
    """This machine's Tailscale IPv4 address (100.x.y.z), or None."""
    if not available():
        return None
    # Prefer the JSON status (stable) over parsing `tailscale ip`.
    try:
        data = status()
        self_node = data.get("Self", {})
        for addr in self_node.get("TailscaleIPs", []) or []:
            if ":" not in addr:  # IPv4
                return addr
    except TailscaleUnavailable:
        pass
    try:
        proc = _run(["ip", "-4"])
        if proc.returncode == 0:
            first = proc.stdout.strip().splitlines()
            if first:
                return first[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def local_ipv6() -> str | None:
    if not available():
        return None
    try:
        data = status()
        for addr in data.get("Self", {}).get("TailscaleIPs", []) or []:
            if ":" in addr:
                return addr
    except TailscaleUnavailable:
        pass
    return None


def dns_name() -> str | None:
    if not available():
        return None
    try:
        data = status()
        name = data.get("Self", {}).get("DNSName", "")
        return name.rstrip(".") or None
    except TailscaleUnavailable:
        return None
