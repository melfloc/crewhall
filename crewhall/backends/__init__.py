from __future__ import annotations

import shutil

from ..backend import Backend
from .pty import PtyBackend
from .ssh_tmux import HostUnreachable, SshTmuxBackend
from .tmux import TmuxBackend, tmux_available

BACKENDS = {
    "pty": PtyBackend,
    "tmux": TmuxBackend,
    "ssh-tmux": SshTmuxBackend,
}


def available() -> list[str]:
    names = ["pty"]
    if tmux_available():
        names.append("tmux")
    if shutil.which("ssh"):
        names.append("ssh-tmux")
    return names


def get_backend(name: str | None = None, host: dict | None = None) -> Backend:
    """Build a backend. ``host`` is a validated remote host config (or None).

    A remote host always uses the ssh-tmux backend: the ``pty`` backend has no
    way to reach another machine and is rejected rather than silently ignored.
    """
    if host is not None:
        if name == "pty":
            raise ValueError("the pty backend cannot be used with a remote host")
        return SshTmuxBackend(host)
    if name == "ssh-tmux":
        raise ValueError("the ssh-tmux backend requires a configured host")
    if name in (None, "auto"):
        name = "tmux" if tmux_available() else "pty"
    try:
        cls = BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"unknown backend {name!r}; available: {', '.join(available())}"
        ) from None
    return cls()


__all__ = [
    "Backend",
    "PtyBackend",
    "TmuxBackend",
    "SshTmuxBackend",
    "HostUnreachable",
    "get_backend",
    "available",
    "BACKENDS",
]
