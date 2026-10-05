from __future__ import annotations

from ..backend import Backend
from .pty import PtyBackend
from .tmux import TmuxBackend, tmux_available

BACKENDS = {
    "pty": PtyBackend,
    "tmux": TmuxBackend,
}


def available() -> list[str]:
    names = ["pty"]
    if tmux_available():
        names.append("tmux")
    return names


def get_backend(name: str | None = None) -> Backend:
    if name in (None, "auto"):
        name = "tmux" if tmux_available() else "pty"
    try:
        cls = BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"unknown backend {name!r}; available: {', '.join(available())}"
        ) from None
    return cls()


__all__ = ["Backend", "PtyBackend", "TmuxBackend", "get_backend", "available", "BACKENDS"]
