"""Best-effort usage extraction from what an agent's TUI actually shows.

This reads the captured screen; it never invents numbers. When a signal is not
present (an agent that does not print tokens, a pre-first-turn screen, …) the
field is ``None`` and the UI shows ``n/d``.
"""
from __future__ import annotations

import re
from typing import Any

from .harness.base import Harness

# OpenCode session footer: ``13.1K (1%) · $0.42`` (tokens, context %, cost).
_OPENCODE_USAGE = re.compile(
    r"(?P<tokens>\d+(?:\.\d+)?)\s*(?P<unit>[KM]?)\s*\((?P<pct>\d+)%\)\s*[·-]\s*\$(?P<cost>\d+(?:\.\d+)?)"
)
_OPENCODE_TOKENS = re.compile(r"(?P<tokens>\d+(?:\.\d+)?)\s*(?P<unit>[KM]?)\s*\((?P<pct>\d+)%\)")
_COST = re.compile(r"\$(?P<cost>\d+(?:\.\d+)?)")


def _count(value: str, unit: str) -> int:
    n = float(value)
    return int(n * 1000) if unit == "K" else int(n * 1_000_000) if unit == "M" else int(n)


def opencode_usage(text: str) -> dict[str, Any]:
    """Parse the OpenCode footer (``13.1K (1%) · $0.42``); never invents values."""
    m = _OPENCODE_USAGE.search(text)
    if m:
        return {
            "available": True,
            "tokens": _count(m.group("tokens"), m.group("unit")),
            "tokens_estimated": True,
            "cost_usd": float(m.group("cost")),
            "source": "opencode footer",
        }
    m = _OPENCODE_TOKENS.search(text)
    if m:
        cost = _COST.search(text)
        return {
            "available": True,
            "tokens": _count(m.group("tokens"), m.group("unit")),
            "tokens_estimated": True,
            "cost_usd": float(cost.group("cost")) if cost else None,
            "source": "opencode footer",
        }
    return {"available": False}


def parse_usage(harness: Harness) -> dict[str, Any]:
    """Return ``{available, tokens, tokens_estimated, cost_usd, source}``.

    Delegates to the adapter's ``usage_from_screen`` capability so the controller
    never branches on the agent kind. ``tokens`` is the context size the TUI
    reports (not a billing counter); ``tokens_estimated`` is True because the
    value is rounded by the CLI itself.
    """
    try:
        text = harness.capture()
    except Exception:  # noqa: BLE001 — usage must never break a summary
        return {"available": False}
    fn = getattr(harness, "usage_from_screen", None)
    if fn is None:
        return {"available": False}
    try:
        return fn(text)
    except Exception:  # noqa: BLE001
        return {"available": False}
