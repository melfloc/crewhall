"""Dynamic control of the daemon's network-facing interfaces (Web UI).

The daemon hosts the Web UI listeners itself, so they can be switched on and off
at runtime (``crewhall local | tailscale | off``) without starting or
restarting any service or keeping a terminal open. The chosen mode is persisted
and re-applied when the daemon starts.

Modes:
  off        no listener
  local      127.0.0.1 only (no token needed: only local processes can reach it)
  tailscale  this machine's Tailscale address only; ALWAYS requires the token
  both       local + tailscale

Switching is *disable first, then enable*: moving from tailscale to local closes
the tailnet listener and every connection on it before anything else happens.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from . import paths
from .web import auth, tailscale
from .web.server import SecurityPolicy, WebServer

log = logging.getLogger("crewhall.frontends")

MODES = ("off", "local", "tailscale", "both")
DEFAULT_PORT = 8765
# Booting: the daemon can come up before the tailnet does. Keep retrying the *saved* mode for a while.
RESTORE_RETRY_EVERY = 10.0
RESTORE_ATTEMPTS = 60


def _extra_hosts() -> list[str]:
    try:
        from . import settings

        return list(settings.get("security.allow_hosts"))
    except Exception:  # noqa: BLE001
        return []


class FrontendError(RuntimeError):
    pass


def _wanted(mode: str) -> set[str]:
    return {"off": set(), "local": {"local"}, "tailscale": {"tailscale"},
            "both": {"local", "tailscale"}}[mode]


class FrontendManager:
    def __init__(self, socket_path: str, state_file: str | None = None) -> None:
        self.socket_path = socket_path
        self.state_file = state_file or os.path.join(paths.state_dir(), "frontends.json")
        self._lock = threading.RLock()
        self._running: dict[str, tuple[WebServer, threading.Thread, str]] = {}
        self._errors: dict[str, str] = {}
        self.port = DEFAULT_PORT
        # Set unless the daemon is still restoring the persisted mode: a status asked in that
        # window must wait for the real answer, not report "off".
        self._restored = threading.Event()
        self._restored.set()
        self._closing = threading.Event()

    # -- persistence -------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, mode: str) -> None:
        os.makedirs(os.path.dirname(self.state_file), mode=0o700, exist_ok=True)
        tmp = self.state_file + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"mode": mode, "port": self.port}, fh)
        os.replace(tmp, self.state_file)

    # -- building one listener --------------------------------------------
    def _build(self, name: str) -> tuple[WebServer, str, dict[str, Any]]:
        """Create (not start) the listener; returns (server, url, extra info)."""
        extra: dict[str, Any] = {}
        if name == "local":
            from . import settings

            require = bool(settings.get("security.local_requires_token"))
            if require and not auth.token_exists():
                extra["token"] = auth.generate_token()
                extra["token_created"] = True
            policy = SecurityPolicy(require_auth=require,
                                    allowed_hosts=["127.0.0.1", "localhost", *_extra_hosts()])
            host, url = "127.0.0.1", f"http://127.0.0.1:{self.port}/"
        else:
            if not tailscale.available():
                raise FrontendError(
                    "Tailscale is not available: install it and log in (`sudo tailscale up`)."
                )
            ip = tailscale.local_ipv4()
            if not ip:
                raise FrontendError("Tailscale has no IPv4 address yet (is it connected? `tailscale status`).")
            if not auth.token_exists():
                extra["token"] = auth.generate_token()
                extra["token_created"] = True
            allowed = [ip, *_extra_hosts()]
            dns = tailscale.dns_name()
            if dns:
                allowed.append(dns)
            policy = SecurityPolicy(require_auth=True, allowed_hosts=allowed)
            host, url = ip, f"http://{dns or ip}:{self.port}/"
        server = WebServer(host=host, port=self.port, socket_path=self.socket_path, policy=policy)
        return server, url, extra

    def _start(self, name: str) -> dict[str, Any]:
        server, url, extra = self._build(name)
        try:
            server.start()
        except OSError as exc:
            raise FrontendError(
                f"cannot listen on {server.host}:{self.port} for '{name}': {exc.strerror or exc} "
                "(is a standalone `crewhall web` still running?)"
            ) from exc
        thread = threading.Thread(target=server.serve_forever, name=f"frontend-{name}", daemon=True)
        thread.start()
        self._running[name] = (server, thread, url)
        self._errors.pop(name, None)
        log.info("frontend %s enabled on %s", name, url)
        return extra

    def _stop(self, name: str) -> None:
        entry = self._running.pop(name, None)
        if entry:
            server, thread, _ = entry
            server.stop()
            thread.join(timeout=5)
            log.info("frontend %s disabled", name)

    # -- public API ---------------------------------------------------------
    def mode(self) -> str:
        names = set(self._running)
        for mode in ("off", "local", "tailscale", "both"):
            if _wanted(mode) == names:
                return mode
        return "off"

    def begin_restore(self) -> None:
        self._restored.clear()

    def status(self) -> dict[str, Any]:
        self._restored.wait(15)
        return self._status()

    def _status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode(),
                "port": self.port,
                "frontends": {
                    name: {"running": name in self._running,
                           "url": self._running[name][2] if name in self._running else None,
                           "auth": "required" if name == "tailscale" else "off",
                           "error": self._errors.get(name)}
                    for name in ("local", "tailscale")
                },
                "token_configured": auth.token_exists(),
            }

    def set_mode(self, mode: str, port: int | None = None, *, persist: bool = True) -> dict[str, Any]:
        if mode not in MODES:
            raise FrontendError(f"unknown mode {mode!r} (choose from {', '.join(MODES)})")
        if port is not None and not (1 <= int(port) <= 65535):
            raise FrontendError("port must be 1-65535")
        self._restored.wait(15)
        return self._set_mode(mode, port, persist=persist)

    def _set_mode(self, mode: str, port: int | None = None, *, persist: bool) -> dict[str, Any]:
        """``set_mode`` without waiting for the restore (the restore itself calls it)."""
        extras: dict[str, Any] = {}
        with self._lock:
            if port is not None and int(port) != self.port:
                for name in list(self._running):  # a port change restarts what is on
                    self._stop(name)
                self.port = int(port)
            want = _wanted(mode)
            for name in list(self._running):  # disable first
                if name not in want:
                    self._stop(name)
            try:
                for name in sorted(want - set(self._running)):
                    extras.update(self._start(name))
            except FrontendError as exc:
                self._errors["last"] = str(exc)
                if persist:  # a user command that failed: keep the file in line with what is running
                    self._save(self.mode())
                raise
            if persist:
                self._save(self.mode())
            out = self._status()
            out.update(extras)
            return out

    def restore(self) -> None:
        """Re-apply the persisted mode (called at daemon start); never raises."""
        try:
            self._restore()
        finally:
            self._restored.set()

    def _restore(self) -> None:
        """Bring the saved mode back, retrying while the cause (tailnet down, port busy) persists.

        The saved mode is the user's *intent*: a failed attempt must never rewrite it, or a boot
        before Tailscale connects would silently turn the access off for good."""
        data = self._load()
        mode = data.get("mode", "off")
        try:
            self.port = int(data.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            self.port = DEFAULT_PORT
        if mode not in MODES or mode == "off":
            return
        for attempt in range(RESTORE_ATTEMPTS):
            try:
                self._set_mode(mode, persist=False)
                for name in _wanted(mode):
                    self._errors.pop(name, None)
                return
            except Exception as exc:  # noqa: BLE001 - a bad frontend must not stop the daemon
                if attempt == 0:
                    log.warning("could not restore frontends (%s): %s; will keep retrying", mode, exc)
                for name in _wanted(mode):
                    self._errors[name] = str(exc)
                self._restored.set()  # waiters get the (error) status now; retries continue
                if self._closing.wait(RESTORE_RETRY_EVERY):
                    return

    def stop_all(self) -> None:
        self._closing.set()
        with self._lock:
            for name in list(self._running):
                self._stop(name)
