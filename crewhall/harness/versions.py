"""Validated CLI versions and the version gate shared by doctor and tests.

The adapters read the agent TUIs from the screen, so an upstream reflow can move
the exact phrases they key on. This module records the version each adapter was
last verified against (the captures in ``tests/fixtures/screens/<kind>/``) and a
range we consider compatible. ``crewhall doctor`` warns when the installed CLI
falls outside it, so a surprising UI change is flagged before it bites.

It also owns the *gate*: ``evaluate`` turns a concrete command into a
:class:`VersionReport`, and ``select_command`` picks a configured fallback
binary when the primary one is out of range, so crewhall can switch versions by
configuration instead of silently driving an unverified adapter.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypedDict

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


class ValidatedRange(TypedDict):
    min: tuple[int, int, int]
    max_exclusive: tuple[int, int, int]
    captured: str


# kind -> {"min": (inclusive), "max_exclusive": ..., "captured": the tested one}.
# A kind absent here has no validated range yet (never warned about).
VALIDATED: dict[str, ValidatedRange] = {
    "claude": {"min": (2, 1, 0), "max_exclusive": (3, 0, 0), "captured": "2.1.284"},
    "codex": {"min": (0, 150, 0), "max_exclusive": (1, 0, 0), "captured": "0.158.0"},
    "opencode": {"min": (1, 0, 0), "max_exclusive": (3, 0, 0), "captured": "1.18.34 / 2.0.29"},
}


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """First ``X.Y`` / ``X.Y.Z`` in ``text`` as ``(major, minor, patch)``."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def validated_range(kind: str) -> ValidatedRange | None:
    return VALIDATED.get(kind)


def is_supported(kind: str, text: str | None) -> bool | None:
    """True/False against the validated range, or None when undecidable."""
    rng = VALIDATED.get(kind)
    if rng is None:
        return None
    version = parse_version(text)
    if version is None:
        return None
    return not (version < rng["min"] or version >= rng["max_exclusive"])


# -- probing the binary ---------------------------------------------------------
# The version string of a command barely changes, so a short cache keeps the
# gate cheap when the controller resolves the command on every launch.
_PROBE_TTL = 30.0
_PROBE_CACHE: dict[tuple[str, ...], tuple[float, str | None]] = {}


def probe_version(argv: Sequence[str]) -> str | None:
    """First line of ``<command> --version``, or None when it cannot be read."""
    key = tuple(argv)
    hit = _PROBE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _PROBE_TTL:
        return hit[1]
    text: str | None = None
    try:
        proc = subprocess.run(
            [*argv, "--version"], capture_output=True, text=True,
            timeout=5, stdin=subprocess.DEVNULL,
        )
        lines = (proc.stdout or proc.stderr).strip().splitlines()
        text = lines[0][:120] if lines else None
    except (OSError, subprocess.SubprocessError):
        text = None
    _PROBE_CACHE[key] = (time.monotonic(), text)
    return text


def _clear_probe_cache() -> None:
    """Test hook: forget cached ``--version`` output."""
    _PROBE_CACHE.clear()


def is_canonical(kind: str, command: Sequence[str]) -> bool:
    """Whether ``command`` is the provider's own binary (``opencode``, …).

    The version gate only applies to the CLI the adapter was written for. A
    custom command (a wrapper, a test double, another tool) is the operator's
    explicit choice: its ``--version`` says nothing about the TUI crewhall will
    drive, so it is reported but never blocked.
    """
    if not command:
        return False
    return os.path.basename(command[0]) == kind


@dataclass(frozen=True)
class VersionReport:
    """What crewhall knows about the CLI behind a command."""

    kind: str
    command: tuple[str, ...]
    text: str | None
    version: tuple[int, int, int] | None
    supported: bool | None
    captured: str | None
    reason: str
    canonical: bool = True

    @property
    def blocked(self) -> bool:
        """True only when the *provider's own* CLI is known to be out of range.

        An unreadable version (``None``) never blocks: some binaries do not
        answer ``--version`` and refusing them would be a false positive. A
        custom command (``canonical`` False) is trusted instead.
        """
        return self.supported is False and self.canonical

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "command": list(self.command),
            "version": self.text,
            "supported": self.supported,
            "captured": self.captured,
            "reason": self.reason,
            "canonical": self.canonical,
            "blocked": self.blocked,
        }


def evaluate(kind: str, argv: Sequence[str]) -> VersionReport:
    """Probe ``argv`` and compare its version against ``kind``'s range."""
    command = tuple(argv)
    rng = VALIDATED.get(kind)
    captured = rng["captured"] if rng else None
    if not command:
        return VersionReport(kind, command, None, None, None, captured,
                             "no command configured")
    text = probe_version(command)
    version = parse_version(text)
    supported = is_supported(kind, text)
    canonical = is_canonical(kind, command)
    if rng is None:
        reason = "no validated range for this provider"
    elif supported is False:
        reason = f"outside the validated range (last verified {captured})"
    elif supported is True:
        reason = f"within the validated range (last verified {captured})"
    else:
        reason = "version could not be read (run `<binary> --version`)"
    if supported is False and not canonical:
        reason = f"{reason}; custom command, so not enforced"
    return VersionReport(kind, command, text, version, supported, captured, reason, canonical)


def _version_hint(path: str) -> tuple[int, int, int]:
    """Version parsed from the nearest ``X.Y[.Z]`` path component (mise/asdf)."""
    for part in reversed(os.path.dirname(path).split(os.sep)):
        version = parse_version(part)
        if version and version > (0, 0, 0):
            return version
    return (0, 0, 0)


def discovery_roots(kind: str) -> list[str]:
    """Directories that commonly hold several installs of ``kind``.

    Version managers keep every version side by side (mise/asdf: ``<root>/*/kind``
    or ``<root>/*/bin/kind``); a user-pinned directory and the usual local bin
    directories are searched too. ``CREWHALL_DISCOVERY_PATH`` (a ``:``-separated
    list) overrides the search entirely; ``XDG_DATA_HOME`` is honoured so callers
    (and tests, and the sandbox regression script) can point discovery at a
    private tree.
    """
    override = (os.environ.get("CREWHALL_DISCOVERY_PATH")
                or os.environ.get("AGENT_TERMINAL_DISCOVERY_PATH"))
    if override:
        return [os.path.expanduser(part) for part in override.split(os.pathsep) if part]
    home = os.path.expanduser("~")
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return [
        os.path.join(data, "mise", "installs", kind),
        os.path.join(home, ".local", "share", "mise", "installs", kind),
        os.path.join(home, ".asdf", "installs", kind),
        os.path.join(data, "crewhall", "bin"),
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".opencode", "bin"),
    ]


def discover_candidates(kind: str, *, exclude: Sequence[str] = ()) -> list[list[str]]:
    """Other installs of ``kind`` on disk, newest first (never probes them)."""
    import glob

    seen: dict[str, tuple[int, int, int]] = {}
    skip = {os.path.abspath(p) for p in exclude}
    for root in discovery_roots(kind):
        patterns = [
            os.path.join(root, kind),
            os.path.join(root, "*", kind),
            os.path.join(root, "*", "bin", kind),
        ]
        for pattern in patterns:
            for path in glob.glob(pattern):
                real = os.path.realpath(path)
                if real in skip or os.path.basename(real) != kind:
                    continue
                if not (os.path.isfile(real) and os.access(real, os.X_OK)):
                    continue
                seen.setdefault(real, _version_hint(real))
    ordered = sorted(seen.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    return [[path] for path, _version in ordered[:20]]


def select_command(
    kind: str,
    candidates: Sequence[Sequence[str]],
) -> tuple[list[str], VersionReport, bool]:
    """Pick the command to run from an ordered list of candidate argv.

    The first candidate is the configured primary; the rest are fallbacks (a
    configured ``fallback_command`` and/or discovered installs). When the primary
    is *known* to be out of range, the first candidate that is not blocked and is
    in range is used — so an automatic, silent switch keeps a validated adapter
    working after an upstream upgrade. Returns ``(argv, report, used_fallback)``.
    """
    ordered = [tuple(c) for c in candidates if c]
    if not ordered:
        return [], evaluate(kind, ()), False
    primary_report = evaluate(kind, ordered[0])
    if not primary_report.blocked:
        return list(primary_report.command), primary_report, False
    best: VersionReport | None = None
    for candidate in ordered[1:]:
        report = evaluate(kind, candidate)
        if report.blocked or report.supported is not True:
            continue
        if best is None or (report.version or (0, 0, 0)) > (best.version or (0, 0, 0)):
            best = report
    if best is not None:
        return list(best.command), best, True
    return list(primary_report.command), primary_report, False


def blocked_reason(report: VersionReport | None) -> str | None:
    """Human message for a blocked version, or None when it is safe to proceed."""
    if report is None or not report.blocked:
        return None
    detail = report.text or "unknown version"
    return (
        f"{report.kind} CLI ({detail}) is {report.reason}. crewhall refused to "
        "launch or drive it. It looked for a compatible install automatically and "
        "found none: install a supported binary (or pin one in Settings → Providers), "
        "or set agents.allow_unvalidated_versions to override (not recommended)."
    )


class UnsupportedVersionError(RuntimeError):
    """Raised when a CLI version is outside the range the adapter was verified on."""

    def __init__(self, report: VersionReport) -> None:
        self.report = report
        super().__init__(blocked_reason(report) or f"unsupported {report.kind} CLI version")
