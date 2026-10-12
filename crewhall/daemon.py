from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from typing import Any

from . import paths, settings
from .controller import AgentNotFound, Controller, SessionNotFound
from .chat import ConversationError
from .terminals import TerminalError
from .harness import HarnessError
from .interactions import InteractionError
from .processes import ProcessError
from .messaging import MessagingError
from .session import SessionTimeout
from .team import TeamError, TeamNotFound
import contextlib
from .ops.agents import OpsAgents
from .ops.chat import OpsChat
from .ops.office import OpsOffice
from .ops.system import OpsSystem
from .ops.teams import OpsTeams
from .ops.terminals import OpsTerminals
from .ops.web import OpsWeb

# How long a Claude permission dialog waits for a Web UI answer before Claude
# shows it in the TUI (the hook's own timeout is hooks.PERMISSION_HOOK_TIMEOUT).
PERMISSION_WAIT = 300.0
PERMISSION_WAIT_MAX = 540.0
# Lifetime of a permission/question card when no Web UI is connected right now:
# the card is still created (so a viewer that connects a moment later sees it)
# instead of being dropped entirely.
PERMISSION_GRACE = 30.0
# How recently a Web UI must have polled to count as "watching".
VIEWER_FRESH = 30.0
# How often the daemon appends a health sample to state/health/journal.jsonl.
HEALTH_JOURNAL_EVERY = 600.0

# Ops an agent (or one of its tool subprocesses) is allowed to call. Everything
# else is the *control plane*: it needs the control token and a caller that does
# not descend from an agent session. ``request_cancel`` is special-cased: its
# agent-authenticated form is an agent op, its operator form (no ``agent`` field)
# is control plane.
AGENT_OPS = frozenset({
    "ping",
    "agent_identity",
    "team_send",
    "team_members",
    "team_new_session",
    "agent_new_session",
    "agent_hook",
    "agent_permission_request",
    "request_create",
    "request_reply",
    "request_cancel",
    "conversation_publish",
})

log = logging.getLogger("crewhall.daemon")


def _listener_alive(socket_path: str, timeout: float = 0.3) -> bool:
    """True if something is actually accepting connections on socket_path."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        try:
            probe.connect(socket_path)
            return True
        except OSError:
            return False



MIN_DIM, MAX_DIM = 2, 1000
OWNERSHIP_CHECK_EVERY = 2.0


def lock_is_ours(fd: Any, path: str) -> bool:
    """Whether ``path`` still names the very file ``fd`` holds a lock on."""
    try:
        return os.fstat(fd.fileno()).st_ino == os.stat(path).st_ino
    except (OSError, ValueError, AttributeError):
        return False


def _dim(value: Any, name: str) -> int:
    """Validate a terminal dimension coming from a (possibly remote) client."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not MIN_DIM <= number <= MAX_DIM:
        raise ValueError(f"{name} must be between {MIN_DIM} and {MAX_DIM}")
    return number


def _peer_pid(conn: socket.socket) -> int | None:
    """PID of the process on the other end of a UNIX socket (Linux SO_PEERCRED)."""
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", raw)
    except (OSError, struct.error):
        return None
    return pid or None


class Server(OpsAgents, OpsTeams, OpsChat, OpsOffice, OpsTerminals, OpsWeb, OpsSystem):

    # Office co-editing ops an agent may drive for its *own* chat (or a chat
    # whose crew it belongs to). ``office_collab_list`` is excluded: it spans
    # every conversation.
    AGENT_OFFICE_OPS = frozenset({
        "office_collab_open", "office_collab_read", "office_collab_command",
        "office_collab_insert", "office_collab_save", "office_collab_close",
        "office_collab_users", "office_collab_chat", "office_collab_say",
        "office_collab_comments", "office_collab_comment_add",
    })

    def __init__(self, controller: Controller, socket_path: str) -> None:
        self.controller = controller
        controller.set_gateway_dispatch(self.dispatch)
        self.socket_path = socket_path
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._lock_fd: Any = None
        self._owns_socket = False
        self._cleaned = False
        self.restart_requested = False  # set by a full reset: re-exec after cleanup
        self._orphan = False  # lost ownership: must not touch sessions/files
        # Whether a shutdown stops the agents too (Fase A1). `daemon stop`
        # clears it so tmux-backed agents survive the restart.
        self._kill_agents_on_stop = True
        from .frontends import FrontendManager

        self.frontends = FrontendManager(socket_path)
        # Last time a Web UI was showing the agents (its live loop polls agent_list).
        self._viewer_seen = 0.0
        self._sock_ino: int | None = None
        try:
            self._control_token = paths.ensure_control_token()
        except OSError:
            log.exception("could not create the control token")
            self._control_token = paths.load_control_token() or ""
    def _acquire_singleton_lock(self) -> bool:
        self._lock_fd = open(paths.lock_path(), "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    def _still_owner(self) -> bool:
        """True while our lock file and socket are still the ones on disk.

        If someone removes the runtime files while we run, a second daemon can
        take a fresh lock. We then either re-take the lock (nobody else did) or
        recognise we are the duplicate and step aside.
        """
        if self._sock_ino is not None:
            try:
                if os.stat(self.socket_path).st_ino != self._sock_ino:
                    return False
            except OSError:
                return False
        if lock_is_ours(self._lock_fd, paths.lock_path()):
            return True
        try:  # lock file gone/replaced: try to re-assert it
            fresh = open(paths.lock_path(), "w")
            fcntl.flock(fresh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        old, self._lock_fd = self._lock_fd, fresh
        with contextlib.suppress(OSError):
            old.close()
        return True
    def serve_forever(self) -> None:
        log.info("daemon starting (pid=%s)", os.getpid())
        if not self._acquire_singleton_lock():
            log.info("another daemon already holds the lock; exiting")
            return
        if os.path.exists(self.socket_path) and _listener_alive(self.socket_path):
            log.info("socket %s already served by a live daemon; exiting",
                     self.socket_path)
            return
        if os.path.exists(self.socket_path):
            with contextlib.suppress(OSError):
                os.unlink(self.socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.bind(self.socket_path)
        except OSError as exc:
            log.info("bind failed (%s); another daemon likely won the race", exc)
            return
        os.chmod(self.socket_path, 0o600)
        self._sock_ino = os.stat(self.socket_path).st_ino
        self._sock.listen(64)
        self._owns_socket = True
        with open(paths.pid_path(), "w") as fh:
            fh.write(str(os.getpid()))
        log.info("daemon listening on %s", self.socket_path)
        # Web interfaces chosen with `crewhall local|tailscale` come back up.
        self.frontends.begin_restore()
        self.controller.workers.spawn(
            "frontends-restore", self.frontends.restore, restart=False)
        # Deliver OnlyOffice co-authoring chat messages to the conversation's agent.
        self.controller.workers.spawn("collab-watcher", self._collab_watcher)
        # Append a health sample (memory, threads, queue, failures, versions) every 10 min.
        self.controller.workers.spawn("health-journal", self._health_journal)
        try:
            from . import chat as conversations

            conversations.ensure_global_rules()
        except Exception:  # noqa: BLE001 - the chat rules are best effort
            log.exception("could not write the global chat rules")
        self._sock.settimeout(0.5)
        next_check = time.monotonic() + OWNERSHIP_CHECK_EVERY
        while not self._stop.is_set():
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + OWNERSHIP_CHECK_EVERY
                if not self._still_owner():
                    log.warning("lost ownership of lock/socket; another daemon "
                                "took over: exiting without touching sessions")
                    self._orphan = True
                    self._owns_socket = False
                    break
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()
        log.info("daemon shutting down")
        self._cleanup()
    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self.frontends.stop_all()
        except Exception:  # noqa: BLE001
            log.exception("error stopping frontends")
        try:
            from .office import collab as office_collab

            office_collab.REGISTRY.close_all()
        except Exception:  # noqa: BLE001
            log.exception("error stopping headless co-editors")
        try:
            if not self._orphan:  # a duplicate must not close adopted sessions
                self.controller.shutdown(kill_agents=self._kill_agents_on_stop)
        except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
            log.exception("error during controller shutdown")
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        # Only the process that actually bound the socket may remove it.
        if self._owns_socket:
            for path in (self.socket_path, paths.pid_path()):
                with contextlib.suppress(OSError):
                    os.unlink(path)
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                self._lock_fd.close()
    def _health_journal(self) -> None:
        """Append a health sample every ``HEALTH_JOURNAL_EVERY`` seconds.

        Long-duration diagnosis (``crewhall doctor --trend``); best effort, a
        failure to write the journal must never disturb the daemon.
        """
        from . import health

        while not self._stop.is_set():
            try:
                health.append_sample(controller=self.controller)
            except Exception:  # noqa: BLE001 - health is diagnostics only
                log.debug("health journal sample failed", exc_info=True)
            self._stop.wait(HEALTH_JOURNAL_EVERY)
    def _collab_watcher(self) -> None:
        """Forward new OnlyOffice chat messages into the agent's conversation.

        The user talks to "AI Agent" from inside the document; crewhall polls the
        headless co-editor and injects each message as a normal prompt.
        """
        from .office import collab as office_collab

        office_collab.REGISTRY.set_forward(self._forward_collab)
        while not self._stop.is_set():
            try:
                office_collab.REGISTRY.poll_forward()
            except Exception:  # noqa: BLE001
                log.exception("collab watcher")
            self._stop.wait(2.0)
    def _forward_collab(self, conversation_id: str, path: str, user: str,
                        kind: str, text: str) -> None:
        from . import chat as conversations

        try:
            meta = conversations.get(conversation_id)
        except Exception:  # noqa: BLE001
            return
        # Route to the artifact's producer (a crew worker) when known, else to
        # the conversation's own lead. Owner/group are not part of the segment.
        agent_id = meta.get("agent_id")
        try:
            record = conversations.producers(conversation_id).get(path) or {}
            agent_id = record.get("producer") or agent_id
        except Exception:  # noqa: BLE001
            log.debug("ignored exception in a boundary guard", exc_info=True)
        if not agent_id:
            return
        label = "comment" if kind == "comment" else "chat"
        body = f"[OnlyOffice {label} · {user or 'user'} · {path}] {text}"
        # No content-based idempotency key: the co-editor already forwards each
        # chat message / comment id once (comment ids persist across reopens),
        # and two identical texts ("ok", "sí") are two legitimate messages.
        try:
            harness = self.controller.get_agent(agent_id)
            if not harness.session.status.alive:
                # The chat agent may be EXITED after a daemon restart: revive it
                # so the user's message is not lost.
                self.controller.restart_agent(agent_id)
            # Never inject directly: a WORKING agent, or one whose input holds a
            # human draft, must queue the comment instead of dropping it.
            self.controller.messaging.send_system(
                agent_id, body, origin="office", conversation_id=conversation_id)
        except Exception as exc:  # noqa: BLE001 - a busy/stopped agent must not crash the loop
            log.info("could not forward OnlyOffice %s to %s: %s", kind, agent_id, exc)
    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
    def _handle(self, conn: socket.socket) -> None:
        peer_pid = _peer_pid(conn)
        try:
            f = conn.makefile("rwb")
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    request = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    response = {"ok": False, "error": f"bad json: {exc}"}
                else:
                    denied = self._guard_origin(request, peer_pid)
                    response = denied if denied is not None else self.dispatch(request)
                f.write((json.dumps(response) + "\n").encode("utf-8"))
                f.flush()
        except OSError:
            pass
        finally:
            conn.close()
    def _caller_is_agent(self, peer_pid: int | None) -> bool:
        """Whether the peer process looks like an agent or one of its tools.

        Agents run as the same Unix user as the daemon, so file permissions alone
        cannot separate them. Two signals are combined: the ``/proc`` ancestry
        (the caller descends from a live agent TUI) and the caller's environment
        (it still carries a live agent's ``CREWHALL_TOKEN``, which survives a
        double fork that reparents it away from the agent).

        This is **defense in depth, not an isolation boundary**: a process that
        deliberately both escapes the tree (``setsid``/double fork) and scrubs
        its environment can still read the 0600 control-token file, because it
        runs as the same user. Real isolation needs the agent sandboxed away
        from the state directory (see SECURITY.md).
        """
        if not peer_pid:
            return False
        try:
            roots = self.controller.agent_root_pids()
        except Exception:  # noqa: BLE001 - a broken registry is not proof
            roots = set()
        if roots:
            from .processes import ancestors

            if set(ancestors(peer_pid)) & roots:
                return True
        return self._environ_has_agent_token(peer_pid)
    def _environ_has_agent_token(self, pid: int) -> bool:
        try:
            tokens = set(self.controller.agent_tokens())
        except Exception:  # noqa: BLE001
            return False
        if not tokens:
            return False
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                env = fh.read().split(b"\0")
        except OSError:
            return False
        for entry in env:
            name, _, value = entry.partition(b"=")
            if name in (b"CREWHALL_TOKEN", b"AGENT_TERMINAL_TOKEN") and \
                    value.decode("utf-8", "replace") in tokens:
                return True
        return False
    def _agent_office_allowed(self, request: dict[str, Any]) -> bool:
        """An agent drives OnlyOffice only for a chat it leads or crews."""
        sender = request.get("sender")
        conversation_id = request.get("id")
        if not sender or not conversation_id:
            return False
        try:
            return self.controller.agent_in_conversation(
                str(sender), request.get("token"), str(conversation_id))
        except Exception:  # noqa: BLE001 - bad identity/conversation: deny
            return False
    def _guard_origin(self, request: dict[str, Any], peer_pid: int | None) -> dict[str, Any] | None:
        """Return a refusal, or None to let the request reach the dispatcher."""
        op = request.get("op")
        if not isinstance(op, str):
            return None  # unknown op is reported by the dispatcher
        allowed_for_agent = op in AGENT_OPS and not (
            op == "request_cancel" and not request.get("agent")
        )
        if allowed_for_agent:
            return None
        if op in self.AGENT_OFFICE_OPS and request.get("sender"):
            # Agent-authenticated co-editing (``crewhall office`` inside a chat).
            if self._agent_office_allowed(request):
                return None
            return {"ok": False, "error": "permission denied: not this chat's agent or crew"}
        if self._caller_is_agent(peer_pid):
            return {"ok": False, "error": "the control plane is not available to agents"}
        token = request.get("_control_token")
        if not self._control_token or token != self._control_token:
            return {"ok": False, "error": "control plane requires a valid control token"}
        return None
    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import audit

        op = request.get("op")
        actor = request.get("_actor", "local")
        if isinstance(actor, dict):
            actor = actor.get("label") or "web"
        response = self._dispatch(request, op)
        if op in audit.AUDITED:
            audit.record(
                str(op), actor=str(actor),
                result="ok" if response.get("ok") else "error",
                summary=audit.summarize(str(op), request, response if response.get("ok") else None),
            )
        return response
    def _dispatch(self, request: dict[str, Any], op: Any) -> dict[str, Any]:
        try:
            # The co-editing ops are only registered when OnlyOffice is enabled;
            # with it off they answer a stable "disabled" instead of loading the
            # office stack at all.
            if isinstance(op, str) and op.startswith("office_collab"):
                from . import office

                if not office.enabled():
                    return {"ok": True, "disabled": True}
            handler = getattr(self, f"_op_{op}", None)
            if handler is None:
                return {"ok": False, "error": f"unknown op {op!r}"}
            return {"ok": True, **handler(request)}
        except SessionNotFound as exc:
            return {"ok": False, "error": f"session not found: {exc}"}
        except TerminalError as exc:
            return {"ok": False, "error": str(exc)}
        except AgentNotFound as exc:
            return {"ok": False, "error": f"agent not found: {exc}"}
        except HarnessError as exc:
            return {"ok": False, "error": str(exc)}
        except MessagingError as exc:
            return {"ok": False, "error": str(exc)}
        except TeamNotFound as exc:
            return {"ok": False, "error": f"team not found: {exc}"}
        except TeamError as exc:
            return {"ok": False, "error": str(exc)}
        except (InteractionError, ProcessError, ConversationError) as exc:
            return {"ok": False, "error": str(exc)}
        except SessionTimeout as exc:
            return {"ok": False, "error": str(exc), "timeout": True}
        except Exception as exc:
            log.exception("op %s failed", op)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}



def _start_tmpdir_janitor(workers: Any, interval: float | None = None) -> None:
    """Periodically prune the dedicated tmpdir (contains Bun's leaked .so)."""

    def loop() -> None:
        wait = interval or settings.get("maintenance.janitor_minutes") * 60.0
        while True:
            time.sleep(wait)
            try:
                removed = paths.cleanup_tmpdir(
                    max_age_seconds=settings.get("maintenance.tmp_max_age_minutes") * 60,
                    max_bytes=settings.get("maintenance.tmp_max_mb") * 1024 * 1024)
                if removed:
                    log.info("tmpdir janitor removed %d stale file(s)", removed)
                from . import uploads

                pruned = uploads.cleanup()
                if pruned:
                    log.info("uploads janitor removed %d stale file(s)", pruned)
            except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                log.exception("tmpdir janitor failed")

    workers.spawn("tmpdir-janitor", loop)


def _start_terminals_janitor(controller: Controller, interval: float = 60.0) -> None:
    """Periodically drop terminals whose shell exited (so they do not accumulate)."""

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                removed = controller.prune_terminals(
                    int(settings.get("terminals.keep_exited_seconds")))
                if removed:
                    log.info("terminals janitor removed %d exited terminal(s)", removed)
            except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                log.exception("terminals janitor failed")

    controller.workers.spawn("terminals-janitor", loop)


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(agent_id)s %(message)s"


class _AgentIdFilter(logging.Filter):
    """Give every record an ``agent_id`` field so the log format never breaks."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "agent_id"):
            record.agent_id = "-"
        return True


def build_log_handler(path: str, max_bytes: int, backup_count: int) -> logging.Handler:
    """A rotated file handler with the structured crewhall log format."""
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(
        path, maxBytes=max(1, int(max_bytes)), backupCount=max(0, int(backup_count)),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(_AgentIdFilter())
    return handler


def _setup_logging() -> None:
    max_bytes = int(settings.get("maintenance.log_max_mb")) * 1024 * 1024
    backups = int(settings.get("maintenance.log_backups"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(build_log_handler(paths.log_path(), max_bytes, backups))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crewhalld")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--socket", default=None)
    args = parser.parse_args(argv)

    _setup_logging()
    paths.cleanup_tmpdir()  # prune any leaked agent temp files on startup
    try:
        from . import uploads

        uploads.cleanup()
    except Exception:  # noqa: BLE001 - never block startup on cleanup
        log.exception("upload cleanup failed")
    controller = Controller(adopt=True, persist=True)
    controller.restore()
    _start_tmpdir_janitor(controller.workers)
    _start_terminals_janitor(controller)
    server = Server(controller, args.socket or paths.socket_path())

    def handle_signal(signum: int, _frame: Any) -> None:
        log.info("received signal %s, stopping", signum)
        server.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        server.serve_forever()
    except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
        log.exception("fatal error during daemon startup/serve")
        server._cleanup()
        return 1
    if server.restart_requested:
        log.info("restarting in place (emergency reset)")
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
