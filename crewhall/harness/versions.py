"""Validated CLI versions and the version parser shared by doctor and tests.

The adapters read the agent TUIs from the screen, so an upstream reflow can move
the exact phrases they key on. This module records the version each adapter was
last verified against (the captures in ``tests/fixtures/screens/<kind>/``) and a
range we consider compatible. ``crewhall doctor`` warns when the installed CLI
falls outside it, so a surprising UI change is flagged before it bites.
"""
from __future__ import annotations

import re

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

# kind -> {"min": (inclusive), "max_exclusive": ..., "captured": the tested one}.
# A kind absent here has no validated range yet (never warned about).
VALIDATED: dict[str, dict[str, object]] = {
    "claude": {"min": (2, 1, 0), "max_exclusive": (3, 0, 0), "captured": "2.1.284"},
    "codex": {"min": (0, 150, 0), "max_exclusive": (1, 0, 0), "captured": "0.158.0"},
    "opencode": {"min": (1, 0, 0), "max_exclusive": (2, 0, 0), "captured": "1.18.34"},
}


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """First ``X.Y`` / ``X.Y.Z`` in ``text`` as ``(major, minor, patch)``."""
    match = _VERSION_RE.search(text or "")
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def validated_range(kind: str) -> dict[str, object] | None:
    return VALIDATED.get(kind)


def is_supported(kind: str, text: str | None) -> bool | None:
    """True/False against the validated range, or None when undecidable."""
    rng = VALIDATED.get(kind)
    if rng is None:
        return None
    version = parse_version(text)
    if version is None:
        return None
    if version < rng["min"] or version >= rng["max_exclusive"]:
        return False
    return True
