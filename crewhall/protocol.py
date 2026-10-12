"""Socket protocol versioning (Fase F3).

The daemon reports a ``protocol`` number in its ``ping`` response. A client that
speaks a different number still connects (the wire format is additive), but it
warns the user so a stale daemon or an updated client is noticed before a
missing op or a changed reply shape bites.

Bump :data:`PROTOCOL_VERSION` when an op is removed or its reply shape changes in
a way an older client cannot read; keep the previous version working for at
least one release and warn on it (see docs/STABLE-SURFACE.md).
"""
from __future__ import annotations

PROTOCOL_VERSION = 1


def warning(daemon_protocol: object) -> str | None:
    """A human warning when the daemon protocol differs from ours, else None."""
    try:
        daemon = int(daemon_protocol)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return ("warning: this daemon predates protocol versioning; some ops may "
                "be missing. Restart it (`crewhall daemon stop && crewhall daemon run`).")
    if daemon < PROTOCOL_VERSION:
        return (f"warning: the daemon speaks protocol {daemon}, this client expects "
                f"{PROTOCOL_VERSION}. Restart the daemon to update it.")
    if daemon > PROTOCOL_VERSION:
        return (f"warning: the daemon speaks a newer protocol ({daemon}) than this "
                f"client ({PROTOCOL_VERSION}); update crewhall.")
    return None
