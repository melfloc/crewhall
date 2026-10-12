"""Shared constants and helpers for the daemon ops mixins."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("crewhall.daemon")

MIN_DIM, MAX_DIM = 2, 1000

# How long a Claude permission dialog waits for a Web UI answer before Claude
# shows it in the TUI (the hook's own timeout is hooks.PERMISSION_HOOK_TIMEOUT).
PERMISSION_WAIT = 300.0
PERMISSION_WAIT_MAX = 540.0
# Lifetime of a permission/question card when no Web UI is connected right now.
PERMISSION_GRACE = 30.0
# How recently a Web UI must have polled to count as "watching".
VIEWER_FRESH = 30.0


def _dim(value: Any, name: str) -> int:
    """Validate a terminal dimension coming from a (possibly remote) client."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not MIN_DIM <= number <= MAX_DIM:
        raise ValueError(f"{name} must be between {MIN_DIM} and {MAX_DIM}")
    return number


class OpsBase:
    # Supplied by crewhall.daemon.Server, which composes the ops mixins.
    controller: Any
    frontends: Any

    def stop(self) -> None:
        raise NotImplementedError

    @staticmethod
    def _need_confirm(request: dict[str, Any], what: str) -> None:
        if request.get("confirm") != "CONFIRM":
            raise ValueError(f"{what} needs typed confirmation")
