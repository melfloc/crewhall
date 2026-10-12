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

from . import paths, protocol, settings
from .controller import AgentNotFound, Controller, SessionNotFound
from .chat import ConversationError
from .terminals import TerminalError
from .harness import AgentState, HarnessError
from .interactions import InteractionError
from .processes import ProcessError
from .ui.control import meta_info
from .messaging import MessagingError
from .session import SessionTimeout
from .team import TeamError, TeamNotFound
from .types import SessionSpec
import contextlib

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

class Server:
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

    # Office co-editing ops an agent may drive for its *own* chat (or a chat
    # whose crew it belongs to). ``office_collab_list`` is excluded: it spans
    # every conversation.
    AGENT_OFFICE_OPS = frozenset({
        "office_collab_open", "office_collab_read", "office_collab_command",
        "office_collab_insert", "office_collab_save", "office_collab_close",
        "office_collab_users", "office_collab_chat", "office_collab_say",
        "office_collab_comments", "office_collab_comment_add",
    })

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

    def _op_ping(self, request: dict[str, Any]) -> dict[str, Any]:
        from .buildinfo import build_info

        info = build_info()
        return {"pid": os.getpid(), "sessions": len(self.controller.registry.all()),
                "version": info["version"], "build": info.get("commit"),
                "channel": info["kind"], "protocol": protocol.PROTOCOL_VERSION}

    def _op_health(self, request: dict[str, Any]) -> dict[str, Any]:
        """Background-worker and queue health (surfaced by `doctor`)."""
        return {
            "workers": self.controller.workers.snapshot(),
            "queues": self.controller.messaging.queue_depth(),
            "pid": os.getpid(),
        }

    def _op_meta_info(self, request: dict[str, Any]) -> dict[str, Any]:
        return meta_info()

    def _op_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"sessions": self.controller.list(request.get("kind", "session"))}

    def _op_prune(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"removed": self.controller.prune()}

    def _op_create(self, request: dict[str, Any]) -> dict[str, Any]:
        spec = SessionSpec(
            command=request["command"],
            cwd=request.get("cwd"),
            env=request.get("env"),
            cols=_dim(request.get("cols", 80), "cols"),
            rows=_dim(request.get("rows", 24), "rows"),
            shell=bool(request.get("shell", False)),
            name=request.get("name"),
            host=request.get("host"),
        )
        session = self.controller.create(spec, request.get("backend"))
        return {"session": self.controller.summary(session)}

    def _op_status(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"session": self.controller.summary(self.controller.get(request["target"]))}

    def _op_write(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        self.controller.guard_terminal_input(session)
        session.write(request["text"])
        return {"session": self.controller.summary(session)}

    def _op_key(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        self.controller.guard_terminal_input(session)
        session.send_key(request["key"])
        return {"session": self.controller.summary(session)}

    def _op_enter(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        self.controller.guard_terminal_input(session)
        session.send_enter()
        return {"session": self.controller.summary(session)}

    def _op_capture(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        return {"output": session.capture(), "session": self.controller.summary(session)}

    def _op_read(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        return {"output": session.read(request.get("timeout"))}

    def _op_read_until(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        try:
            output = session.read_until(
                request["pattern"], timeout=float(request.get("timeout", 10.0))
            )
            return {"matched": True, "output": output}
        except SessionTimeout as exc:
            return {"matched": False, "output": exc.text, "error": str(exc)}

    def _op_resize(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        session.resize(_dim(request["cols"], "cols"), _dim(request["rows"], "rows"))
        return {"session": self.controller.summary(session)}

    def _op_interrupt(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        self.controller.guard_terminal_input(session)
        session.interrupt()
        return {"session": self.controller.summary(session)}

    def _op_terminate(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        session.terminate()
        return {"session": self.controller.summary(session)}

    def _op_kill(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        session.kill()
        return {"session": self.controller.summary(session)}

    def _op_wait(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        code = session.wait(request.get("timeout"))
        return {"exit_code": code, "session": self.controller.summary(session)}

    def _op_events(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        return {"events": session.events(), "session": self.controller.summary(session)}

    # -- terminals ---------------------------------------------------------
    def _op_terminal_create(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.create_terminal(
            host=request.get("host"),
            cwd=request.get("cwd"),
            shell=request.get("shell"),
            command=request.get("command"),
            title=request.get("title"),
            readonly=request.get("readonly", False),
            cols=request.get("cols", 120),
            rows=request.get("rows", 32),
            owner=request.get("owner"),
        )
        return {"terminal": self.controller.terminal_info(session.session_id)}

    def _op_terminal_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"terminals": self.controller.list_terminals(request.get("host"))}

    def _op_terminal_info(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"terminal": self.controller.terminal_info(request["id"])}

    def _op_terminal_write(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller._resolve_terminal(request["id"])
        self.controller.guard_terminal_input(session)
        text = request.get("text")
        if not isinstance(text, str):
            raise TerminalError("text must be a string")
        limit = int(settings.get("terminals.max_message_bytes"))
        if len(text.encode("utf-8")) > limit:
            raise TerminalError(f"text too large (max {limit} bytes)")
        session.write(text)
        if request.get("enter"):
            session.send_enter()
        return {"written": len(text.encode("utf-8"))}

    def _op_terminal_key(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller._resolve_terminal(request["id"])
        self.controller.guard_terminal_input(session)
        session.send_key(request["key"])
        return {}

    def _op_terminal_capture(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller._resolve_terminal(request["id"])
        output = session.capture(escapes=bool(request.get("escapes", False)))
        if request.get("recent", True):
            max_lines = int(request.get("max_lines", 500) or 0)
            if max_lines > 0:
                output = "\n".join(output.splitlines()[-max_lines:])
        return {"output": output}

    def _op_terminal_resize(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller._resolve_terminal(request["id"])
        self.controller.guard_terminal_input(session)
        cols = self.controller._terminal_dim(request.get("cols"), "cols", 10, 500)
        rows = self.controller._terminal_dim(request.get("rows"), "rows", 2, 200)
        session.resize(cols, rows)
        return {}

    def _op_terminal_close(self, request: dict[str, Any]) -> dict[str, Any]:
        closed = self.controller.close_terminal(request["id"])
        return {"closed": closed}

    def _op_agent_create(self, request: dict[str, Any]) -> dict[str, Any]:
        team = request.get("team")
        if request.get("conversation") and not team:
            # A chat with a crew: its lead joins the Team (isolation of its cwd is
            # preserved by the controller). A remote team is not attached to a
            # chat: its agent must run locally to keep the conversation workspace.
            from . import chat as conversations

            try:
                team = conversations.get(str(request["conversation"])).get("team")
                team_obj = self.controller._team_or_none(team)
                if team_obj is not None and team_obj.host:
                    team = None
            except Exception:  # noqa: BLE001 - unknown conversation/team: no crew
                team = None
        harness = self.controller.create_agent(
            request["kind"],
            name=request.get("name"),
            backend=request.get("backend"),
            cwd=request.get("cwd"),
            team=team,
            cols=_dim(request.get("cols", 120), "cols"),
            rows=_dim(request.get("rows", 40), "rows"),
            wait_ready=bool(request.get("wait_ready", False)),
            timeout=float(request.get("timeout", 30.0)),
            args=request.get("args"),
            workspace_mode=request.get("workspace_mode"),
            host=request.get("host"),
            conversation=request.get("conversation"),
        )
        if request.get("conversation"):
            from . import chat as conversations

            with contextlib.suppress(conversations.ConversationError):
                conversations.set_agent(str(request["conversation"]), harness.agent_id)
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_list(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("viewer"):
            self._viewer_seen = time.monotonic()
        # Chat agents are isolated from Cowork: the default scope hides them, so
        # they never show up in the agents sidebar, teams or mission control.
        from . import chat as conversations

        agents = self.controller.list_agents()
        scope = request.get("scope") or "cowork"
        if scope != "all":
            chat_ids = conversations.agent_ids()
            if scope == "chat":
                agents = [a for a in agents if a.get("agent_id") in chat_ids]
            else:
                agents = [a for a in agents if a.get("agent_id") not in chat_ids]
        return {"agents": agents}

    def _op_host_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"hosts": self.controller.host_status()}

    @staticmethod
    def _need_confirm(request: dict[str, Any], what: str) -> None:
        # A host decides where code runs and which key is used: typed confirmation.
        if request.get("confirm") != "CONFIRM":
            raise ValueError(f"{what} needs typed confirmation")

    def _op_host_set(self, request: dict[str, Any]) -> dict[str, Any]:
        self._need_confirm(request, "changing a host")
        body: dict[str, Any] = {k: request[k] for k in ("ssh", "port", "identity", "known_hosts",
                                        "tmux_socket", "tunnel") if k in request}
        return {"host": self.controller.host_set(str(request.get("name") or ""), body)}

    def _op_host_remove(self, request: dict[str, Any]) -> dict[str, Any]:
        self._need_confirm(request, "removing a host")
        return self.controller.host_remove(str(request.get("name") or ""))

    def _op_host_test(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.controller.host_test(str(request.get("name") or ""))
        except settings.SettingsError as exc:
            raise ValueError(str(exc)) from exc

    def _op_agent_info(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_send(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        harness.send(request["prompt"], timeout=float(request.get("timeout", 30.0)))
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_write(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        harness.write_raw(request["text"])
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_key(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        harness.send_key(request["key"])
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_resize(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        harness.resize(_dim(request["cols"], "cols"), _dim(request["rows"], "rows"))
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_interrupt(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        return {"interrupted": harness.interrupt()}

    def _op_agent_models(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"models": self.controller.agent_models(request["target"])}

    def _op_agent_set_model(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"agent": self.controller.set_agent_model(
            request["target"], request.get("model"))}

    def _op_conversation_search(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"matches": self.controller.search_conversations(
            str(request.get("query") or ""), int(request.get("limit", 50) or 50))}

    # -- chat conversations (inputs/outputs per conversation) ---------------
    def _op_conversation_create(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations

        team = request.get("team") or None
        if team:
            # Store the stable team_id (a name can be renamed) and refuse a
            # remote team: a chat's lead runs locally with its own workspace.
            team_obj = self.controller.teams.get(str(team))
            if team_obj.host:
                raise conversations.ConversationError(
                    "a remote team cannot be a chat's crew")
            team = team_obj.team_id
        meta = conversations.create(
            str(request.get("title") or ""), agent_id=request.get("agent_id"),
            team=team)
        env = conversations.env(meta["id"])
        meta = {**meta, "dir": env["CREWHALL_CONVERSATION_DIR"],
                "outputs": env["CREWHALL_OUTPUTS"], "inputs": env["CREWHALL_INPUTS"],
                "workspace": env["CREWHALL_WORKSPACE"]}
        return {"conversation": meta}

    def _op_conversation_list(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations

        return {"conversations": [self._conversation_row(m) for m in conversations.list_all()]}

    def _conversation_row(self, meta: dict[str, Any]) -> dict[str, Any]:
        """A conversation as list views show it: file counts, live agent, crew name."""
        from . import chat as conversations

        row = dict(meta)
        row["inputs"] = len(conversations.list_files(meta["id"], "inputs"))
        row["outputs"] = len(conversations.list_files(meta["id"], "outputs"))
        agent = None
        if meta.get("agent_id"):
            try:
                agent = self.controller.agent_summary(
                    self.controller.get_agent(meta["agent_id"]))
            except Exception:  # noqa: BLE001 - a gone agent must not hide the chat
                agent = None
        row["agent"] = agent
        team = self.controller._team_or_none(meta.get("team"))
        row["team_name"] = team.name if team is not None else None
        return row

    def _op_conversation_info(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations

        info = conversations.info(str(request["id"]))
        agent = None
        if info.get("agent_id"):
            try:
                agent = self.controller.agent_summary(
                    self.controller.get_agent(info["agent_id"]))
            except Exception:  # noqa: BLE001
                agent = None
        info["agent"] = agent
        return {"conversation": info}

    def _op_conversation_rename(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations

        return {"conversation": conversations.rename(
            str(request["id"]), str(request.get("title") or ""))}

    def _op_conversation_set_agent(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations

        return {"conversation": conversations.set_agent(
            str(request["id"]), request.get("agent_id"))}

    def _op_conversation_set_team(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"conversation": self.controller.set_conversation_team(
            str(request["id"]), request.get("team"))}

    def _op_conversation_activity(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.conversation_activity(
            str(request["id"]), limit=int(request.get("limit", 50) or 50),
            all_team=bool(request.get("all_team", False)))

    def _op_conversation_crew(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations

        team = str(request.get("team") or "")
        if team:
            metas = conversations.crew_conversations(self.controller.teams.get(team).team_id)
        else:
            metas = [m for m in conversations.list_all() if m.get("team")]
        return {"conversations": [self._conversation_row(m) for m in metas]}

    def _op_conversation_publish(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self.controller.publish_artifact(
            request["sender"], request.get("token"), str(request["id"]),
            str(request.get("source") or ""), request.get("name"))
        return result

    def _op_conversation_delete(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations

        return conversations.delete(
            str(request["id"]), delete_files=bool(request.get("delete_files")))

    def _op_conversation_artifact_delete(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations

        return {"artifact": conversations.delete_artifact(
            str(request["id"]), str(request.get("path") or ""))}

    # -- Level 3: the agent as a live co-editor (headless Chromium) ---------
    def _op_office_collab_open(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import chat as conversations, office
        from .office import collab as office_collab

        if not office.enabled():
            raise ValueError("OnlyOffice is not enabled")
        cid = str(request["id"])
        rel = str(request.get("path") or "")
        path = conversations.resolve(cid, "outputs", rel)
        ok, why = office.validate_file(path)
        if not ok:
            raise ValueError(f"the artifact is not a valid {os.path.splitext(path)[1]} file "
                             f"({why}); regenerate it before co-editing it")
        base = office.base_url(str(request.get("base") or ""))
        if not base:
            raise ValueError("office.base_url (or web.public_url) must be set "
                             "so the Document Server can fetch the document")
        config = office.build_config(
            conversation_id=cid, relpath=rel, name=os.path.basename(path), path=path,
            document_base=base, callback_base=base, user_id="crewhall-ai",
            user_name="AI Agent",
        )
        return office_collab.REGISTRY.open(cid, rel, config, office.public_url())

    def _op_office_collab_read(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        text = office_collab.REGISTRY.read(str(request["id"]), str(request.get("path") or ""))
        return {"text": text}

    def _op_office_collab_command(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        result = office_collab.REGISTRY.command(
            str(request["id"]), str(request.get("path") or ""),
            str(request["method"]), request.get("args"))
        return {"result": result}

    def _op_office_collab_insert(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"result": office_collab.REGISTRY.insert(
            str(request["id"]), str(request.get("path") or ""), str(request.get("text") or ""))}

    def _op_office_collab_save(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"result": office_collab.REGISTRY.save(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_close(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"closed": office_collab.REGISTRY.close(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_list(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"editors": office_collab.REGISTRY.list()}

    def _op_office_collab_users(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"users": office_collab.REGISTRY.users(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_chat(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"messages": office_collab.REGISTRY.chat_messages(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_say(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"result": office_collab.REGISTRY.chat_send(
            str(request["id"]), str(request.get("path") or ""), str(request.get("text") or ""))}

    def _op_office_collab_comments(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"comments": office_collab.REGISTRY.comments(
            str(request["id"]), str(request.get("path") or ""))}

    def _op_office_collab_comment_add(self, request: dict[str, Any]) -> dict[str, Any]:
        from .office import collab as office_collab

        return {"result": office_collab.REGISTRY.add_comment(
            str(request["id"]), str(request.get("path") or ""), str(request.get("text") or ""))}

    def _op_office_status(self, request: dict[str, Any]) -> dict[str, Any]:
        """Report the OnlyOffice infrastructure state (for the Web UI button)."""
        from .office import setup as office_setup

        try:
            return {"status": office_setup.status()}
        except Exception as exc:  # noqa: BLE001 - a broken probe must not break the UI
            return {"status": {"docker": False, "compose": False, "healthy": False,
                               "error": str(exc)}}

    def _op_office_start(self, request: dict[str, Any]) -> dict[str, Any]:
        """Start/restart/stop the Document Server from the Web UI.

        ``start``/``restart`` run in the background (they may pull an image and take
        a while); the caller polls ``office_status`` until it turns healthy. ``stop``
        is quick and runs inline.
        """
        from .office import setup as office_setup

        action = str(request.get("action") or "start")
        if action == "stop":
            return {"result": office_setup.stop()}
        if action not in ("start", "restart"):
            raise ValueError(f"unknown office action: {action}")

        def work() -> None:
            try:
                if action == "restart":
                    office_setup.restart()
                office_setup.start(wait=False, log=lambda *a: log.info("office: %s", " ".join(map(str, a))))
            except Exception:  # noqa: BLE001 - a failed background start must not crash the daemon
                log.exception("office %s failed", action)

        threading.Thread(target=work, name=f"office-{action}", daemon=True).start()
        return {"started": True, "action": action}

    def _op_frontend_status(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.frontends.status()

    def _op_frontend_set(self, request: dict[str, Any]) -> dict[str, Any]:
        from .frontends import FrontendError

        try:
            return self.frontends.set_mode(request["mode"], request.get("port"))
        except FrontendError as exc:
            raise ValueError(str(exc)) from exc

    def _op_agent_history(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        before = request.get("before")
        out = harness.history(
            int(request.get("limit", 200)), None if before is None else int(before)
        )
        # Lets a viewer notice /clear or /new even before the new one has messages.
        return {**out, "conversation_id": harness.conversation_id}

    def _op_agent_hook(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.record_hook(
            request["agent"], request.get("token"), request["event"],
            conversation_id=request.get("conversation_id"),
        )

    def _op_agent_permission_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Claude's PermissionRequest/PreToolUse hook: wait for an answer from a front-end.

        The card is always created so it is never lost; while a Web UI is connected
        it waits the full ``agents.permission_wait``; with no viewer it holds a short
        grace (:data:`PERMISSION_GRACE`) so a front-end that reconnects right after
        still sees it. ``CREWHALL_PERMISSION_WAIT`` (seconds, 0 disables) sets the
        full wait.
        """
        try:
            wait = float(settings.get("agents.permission_wait"))
        except ValueError:
            wait = PERMISSION_WAIT
        if time.monotonic() - self._viewer_seen > VIEWER_FRESH:
            wait = min(wait, PERMISSION_GRACE)
        return self.controller.claude_permission_request(
            request["agent"], request.get("token"), request.get("payload"),
            wait=max(0.0, min(wait, PERMISSION_WAIT_MAX)),
        )

    def _op_agent_new_session(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        return self.controller.new_session(harness, timeout=float(request.get("timeout", 20.0)))

    def _op_team_new_session(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.new_session_as(
            request["sender"], request.get("token"), request["target"],
            timeout=float(request.get("timeout", 20.0)),
        )

    def _op_agent_processes(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.list_processes(request["target"])

    def _op_agent_process_output(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.process_output(
            request["target"], task=request.get("task"), call=request.get("call"),
        )

    def _op_agent_process_signal(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.signal_process(
            request["target"], int(request["pid"]), str(request.get("signal", "INT")),
            by=str(request.get("by") or "user")[:40],
        )

    def _op_interaction_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"interactions": self.controller.list_interactions(request.get("target"))}

    def _op_interaction_respond(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"interaction": self.controller.respond_interaction(
            str(request["id"]), request.get("answer"), by=str(request.get("by") or "user")[:40],
        )}

    def _op_agent_identity(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.agent_identity(request["target"], request.get("token"))

    def _op_team_send(self, request: dict[str, Any]) -> dict[str, Any]:
        delivery = self.controller.send_message_as(
            request["sender"], request.get("token"),
            request["recipient"], request["body"],
            idempotency_key=request.get("idempotency_key"),
            message_id=request.get("message_id"),
            conversation_id=request.get("conversation_id"),
        )
        return {"delivery": delivery.to_dict()}

    def _op_team_members(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"members": self.controller.team_members(request["target"])}

    def _op_request_create(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.create_request(
            request["sender"], request.get("token"), request["recipient"], request["task"],
            timeout=request.get("timeout"), wait=bool(request.get("wait", True)),
        )

    def _op_request_reply(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"request": self.controller.reply_to_request(
            request["agent"], request.get("token"), request["request_id"], request["body"])}

    def _op_request_cancel(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("agent"):
            return {"request": self.controller.cancel_request(
                request["agent"], request.get("token"), request["request_id"])}
        # Control plane (authenticated Web UI / local): cancel without a token.
        return {"request": self.controller.requests.cancel_operator(request["request_id"]).to_dict()}

    def _op_request_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"requests": self.controller.list_requests(
            open_only=bool(request.get("open_only", True)),
        )}

    def _op_agent_capture(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        output = harness.capture()
        if request.get("recent"):
            output = harness.capture_recent(int(request.get("max_lines", 40)))
        return {"output": output, "agent": self.controller.agent_summary(harness)}

    def _op_agent_transcript(self, request: dict[str, Any]) -> dict[str, Any]:
        """Capture with the agent's own input area removed (for Web UIs that
        draw their own input)."""
        harness = self.controller.get_agent(request["target"])
        output = harness.transcript()
        max_lines = int(request.get("max_lines", 0) or 0)
        if max_lines > 0:
            output = "\n".join(output.splitlines()[-max_lines:])
        return {"output": output, "agent": self.controller.agent_summary(harness)}

    def _op_agent_state(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_wait(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.get_agent(request["target"])
        target = AgentState(request["state"])
        state = harness.wait_for_state(
            target, timeout=float(request.get("timeout", 30.0))
        )
        info = self.controller.agent_summary(harness)
        return {"reached": state is target or state == target, "agent": info}

    def _op_agent_restart(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.restart_agent(request["target"])
        try:
            harness.start(timeout=float(request.get("timeout", 20.0)))
        except Exception:  # noqa: BLE001 - return the summary even if readiness times out
            log.exception("agent restart: readiness wait failed")
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_stop(self, request: dict[str, Any]) -> dict[str, Any]:
        info = self.controller.remove_agent(
            request["target"], force=bool(request.get("force", False))
        )
        return {"agent": info.to_dict()}

    def _op_message_send(self, request: dict[str, Any]) -> dict[str, Any]:
        delivery = self.controller.send_message(
            request["sender"], request["recipient"], request["body"],
            idempotency_key=request.get("idempotency_key"),
            message_id=request.get("message_id"),
            conversation_id=request.get("conversation_id"),
        )
        return {"delivery": delivery.to_dict()}

    def _op_message_history(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "messages": self.controller.message_history(
                agent=request.get("agent"), limit=request.get("limit")
            )
        }

    def _op_team_create(self, request: dict[str, Any]) -> dict[str, Any]:
        team = self.controller.create_team(
            request["name"],
            request["agent_ids"],
            workspace=request.get("workspace"),
            workspace_mode=request.get("workspace_mode"),
            host=request.get("host"),
        )
        return {"team": self.controller.team_info(team.team_id)}

    def _op_team_up(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.controller.team_up(request["spec"])

    def _op_team_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"teams": self.controller.list_teams()}

    def _op_team_info(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"team": self.controller.team_info(request["target"])}

    def _op_team_add_member(self, request: dict[str, Any]) -> dict[str, Any]:
        team = self.controller.add_team_member(request["target"], request["agent"])
        return {"team": self.controller.team_info(team.team_id)}

    def _op_team_remove_member(self, request: dict[str, Any]) -> dict[str, Any]:
        team = self.controller.remove_team_member(request["target"], request["agent"])
        return {"team": self.controller.team_info(team.team_id)}

    def _op_team_set_workspace(self, request: dict[str, Any]) -> dict[str, Any]:
        extra = {"host": request["host"] or None} if "host" in request else {}
        team = self.controller.set_team_workspace(
            request["target"], request.get("workspace"), **extra
        )
        return {"team": self.controller.team_info(team.team_id)}

    def _op_team_remove(self, request: dict[str, Any]) -> dict[str, Any]:
        team = self.controller.remove_team(request["target"])
        return {"team": team.to_dict()}

    def _op_agent_archive_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"archived": self.controller.list_archived_agents()}

    def _op_agent_archive_get(self, request: dict[str, Any]) -> dict[str, Any]:
        rec = self.controller.read_archived_agent(str(request.get("target") or ""))
        if rec is None:
            return {"found": False}
        return {"found": True, "archive": rec}

    def _op_bundle_export(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import bundle

        # The destination is server-side (no client-chosen paths): a dedicated
        # bundles directory under the state home.
        dest_dir = os.path.join(paths.state_dir(), "bundles")
        os.makedirs(dest_dir, mode=0o700, exist_ok=True)
        name = time.strftime("bundle-%Y%m%d-%H%M%S.tar.gz")
        dest = os.path.join(dest_dir, name)
        out = bundle.export_bundle(dest, with_state=False, with_token=False,
                                   live_teams=self.controller.live_team_defs())
        return {"path": out["path"], "name": name, "files": out["files"]}

    def _op_bundle_live_teams(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"teams": self.controller.live_team_defs()}

    def _op_bundle_list(self, request: dict[str, Any]) -> dict[str, Any]:
        dest_dir = os.path.join(paths.state_dir(), "bundles")
        bundles = []
        if os.path.isdir(dest_dir):
            for name in sorted(os.listdir(dest_dir)):
                if not name.endswith(".tar.gz"):
                    continue
                full = os.path.join(dest_dir, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                from . import bundle

                bundles.append({"name": name, "bytes": st.st_size, "mtime": st.st_mtime,
                                **bundle.describe(full)})
        return {"bundles": bundles, "directory": dest_dir}

    def _op_bundle_delete(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import bundle

        names = request.get("names")
        names = [request["name"]] if request.get("name") else list(names or [])
        deleted, failed = [], []
        for name in names[:200]:
            try:
                bundle.delete_bundle(str(name))
                deleted.append(name)
            except (bundle.BundleError, OSError) as exc:
                failed.append({"name": name, "error": str(exc)})
        return {"deleted": deleted, "failed": failed}

    def _op_bundle_import(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import bundle

        dest_dir = os.path.join(paths.state_dir(), "bundles")
        name = str(request.get("name") or "")
        # Only a bundle name inside the server-side directory: no traversal.
        if not name or "/" in name or "\\" in name or not name.endswith(".tar.gz"):
            raise bundle.BundleError("invalid bundle name")
        src = os.path.realpath(os.path.join(dest_dir, name))
        if not src.startswith(os.path.realpath(dest_dir) + os.sep) or not os.path.isfile(src):
            raise bundle.BundleError("no such bundle")
        dry = bool(request.get("dry_run", True))
        result = bundle.import_bundle(src, force=bool(request.get("force", False)), dry_run=dry)
        # Teams/agents defined in the bundle: preview, or recreate them (idempotent).
        _, files = bundle.read_bundle(src)
        specs, errors = bundle.bundle_team_specs(files)
        result["teams_plan"] = bundle.plan_specs(specs)
        result["team_errors"] = errors
        if not dry and request.get("apply_teams"):
            result["teams_applied"] = bundle.apply_specs(specs, self.controller.team_up)
        return {**result, "by": str(request.get("by") or "user")[:40]}

    # -- settings -----------------------------------------------------------
    def _op_settings_get(self, request: dict[str, Any]) -> dict[str, Any]:
        return settings.describe()

    def _op_settings_set(self, request: dict[str, Any]) -> dict[str, Any]:
        if "hosts" in (request.get("changes") or {}):
            self._need_confirm(request, "changing hosts")
        try:
            return settings.patch(
                request.get("changes") or {},
                confirm=request.get("confirm") == "CONFIRM",
            )
        except settings.SettingsError as exc:
            raise ValueError(str(exc)) from exc

    def _op_settings_reset(self, request: dict[str, Any]) -> dict[str, Any]:
        return settings.reset(request.get("prefix") or None)

    def _op_provider_check(self, request: dict[str, Any]) -> dict[str, Any]:
        kinds = [request["kind"]] if request.get("kind") else settings._kinds()
        return {"providers": [settings.check_provider(k, request.get("command") if len(kinds) == 1 else None)
                              for k in kinds]}

    # -- emergency reset ------------------------------------------------------
    def _op_reset_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import reset

        return reset.plan(self.controller, str(request.get("level") or ""))

    def _op_reset_apply(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import reset

        level = str(request.get("level") or "")
        if level == "full" and request.get("confirm") != reset.CONFIRM_WORD:
            raise ValueError(f"type {reset.CONFIRM_WORD} to confirm a full reset")
        result = reset.apply(self.controller, self.frontends, level,
                             forget_state=bool(request.get("forget_state")),
                             reset_settings=bool(request.get("reset_settings")))
        if result["restart"]:
            self.restart_requested = True
            threading.Thread(target=self._delayed_stop, daemon=True).start()
        return result

    def _op_uploads_info(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import uploads

        return uploads.info()

    def _op_fs_complete(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import fscomplete

        roots = list(settings.get("security.fs_roots") or [])
        try:
            roots += [t["workspace"] for t in self.controller.list_teams() if t.get("workspace")]
        except Exception:  # noqa: BLE001 - missing teams must not break completion
            log.debug("ignored exception in a boundary guard", exc_info=True)
        try:
            roots += [a["cwd"] for a in self.controller.list_agents() if a.get("cwd")]
        except Exception:  # noqa: BLE001
            log.debug("ignored exception in a boundary guard", exc_info=True)
        actor_raw = request.get("_actor")
        actor: dict[str, Any] = actor_raw if isinstance(actor_raw, dict) else {}
        return fscomplete.complete(
            request.get("prefix"), roots=roots, actor=(actor.get("label") or actor.get("ip") or "local"),
        )

    def _op_worktree_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"worktrees": self.controller.list_worktrees()}

    def _op_worktree_discard(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.controller.discard_worktree(
                str(request.get("path") or ""), request.get("confirm"))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def _op_update_status(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import updater

        return updater.status()

    def _op_clean_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import clean

        plan = clean.plan_cleanup(keep_backups=int(request.get("keep_backups", settings.get("maintenance.keep_backups"))),
                                  max_age_days=request.get("max_age_days")).to_dict()
        plan["worktrees"] = clean.list_worktrees()
        return plan

    def _op_clean_apply(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import clean

        plan = clean.plan_cleanup(keep_backups=int(request.get("keep_backups", settings.get("maintenance.keep_backups"))),
                                  max_age_days=request.get("max_age_days"))
        result = clean.apply_cleanup(plan)
        return {**plan.to_dict(), **result, "by": str(request.get("by") or "user")[:40]}

    def _op_attach_info(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        info = session.info().to_dict()
        info["attachable"] = session.backend.name in ("tmux", "ssh-tmux")
        return {"session": info}

    def _op_shutdown(self, request: dict[str, Any]) -> dict[str, Any]:
        # `crewhall daemon stop` keeps tmux-backed agents running (kill_agents
        # False); `daemon stop --kill-agents` (and older callers that omit it)
        # stop everything. The default stays True so an unqualified RPC shutdown
        # is the historical, fully-stopping behaviour.
        kill_agents = bool(request.get("kill_agents", True))
        threading.Thread(
            target=self._delayed_stop, args=(kill_agents,), daemon=True
        ).start()
        return {"stopping": True, "kill_agents": kill_agents}

    def _delayed_stop(self, kill_agents: bool = True) -> None:
        time.sleep(0.1)
        self._kill_agents_on_stop = kill_agents
        self.stop()


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
