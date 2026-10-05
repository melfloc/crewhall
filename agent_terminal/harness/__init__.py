from __future__ import annotations

from .base import AgentInfo, AgentState, Harness, HarnessError
from .claude import ClaudeCodeHarness
from .codex import CodexHarness
from .opencode import OpenCodeHarness

HARNESSES: dict[str, type[Harness]] = {
    OpenCodeHarness.kind: OpenCodeHarness,
    ClaudeCodeHarness.kind: ClaudeCodeHarness,
    CodexHarness.kind: CodexHarness,
}


def available_harnesses() -> list[str]:
    return sorted(HARNESSES)


def get_harness(kind: str) -> type[Harness]:
    try:
        return HARNESSES[kind]
    except KeyError:
        raise ValueError(
            f"unknown harness {kind!r}; available: {', '.join(available_harnesses())}"
        ) from None


__all__ = [
    "Harness",
    "HarnessError",
    "AgentState",
    "AgentInfo",
    "OpenCodeHarness",
    "ClaudeCodeHarness",
    "CodexHarness",
    "HARNESSES",
    "get_harness",
    "available_harnesses",
]
