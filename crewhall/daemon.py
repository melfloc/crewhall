from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import Any

from . import paths, settings
from .controller import AgentNotFound, Controller, SessionNotFound
from .harness import AgentState, HarnessError
from .interactions import InteractionError
from .processes import ProcessError
from .ui.control import meta_info
from .messaging import MessagingError
from .session import SessionTimeout
from .team import TeamError, TeamNotFound
from .types import SessionSpec

# How long a Claude permission dialog waits for a Web UI answer before Claude
# shows it in the TUI (the hook's own timeout is hooks.PERMISSION_HOOK_TIMEOUT).
PERMISSION_WAIT = 90.0
PERMISSION_WAIT_MAX = 540.0
VIEWER_FRESH = 10.0

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

class Server:
    def __init__(self, controller: Controller, socket_path: str) -> None:
        self.controller = controller
        self.socket_path = socket_path
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._lock_fd: Any = None
        self._owns_socket = False
        self._cleaned = False
        self.restart_requested = False  # set by a full reset: re-exec after cleanup
        self._orphan = False  # lost ownership: must not touch sessions/files
        from .frontends import FrontendManager

        self.frontends = FrontendManager(socket_path)
        # Last time a Web UI was showing the agents (its live loop polls agent_list).
        self._viewer_seen = 0.0
        self._sock_ino: int | None = None

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
        try:
            old.close()
        except OSError:
            pass
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
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
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
        threading.Thread(target=self.frontends.restore, name="frontends-restore", daemon=True).start()
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
            if not self._orphan:  # a duplicate must not close adopted sessions
                self.controller.shutdown()
        except Exception:
            log.exception("error during controller shutdown")
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        # Only the process that actually bound the socket may remove it.
        if self._owns_socket:
            for path in (self.socket_path, paths.pid_path()):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if self._lock_fd is not None:
            try:
                self._lock_fd.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _handle(self, conn: socket.socket) -> None:
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
                    response = self.dispatch(request)
                f.write((json.dumps(response) + "\n").encode("utf-8"))
                f.flush()
        except OSError:
            pass
        finally:
            conn.close()

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
            handler = getattr(self, f"_op_{op}", None)
            if handler is None:
                return {"ok": False, "error": f"unknown op {op!r}"}
            return {"ok": True, **handler(request)}
        except SessionNotFound as exc:
            return {"ok": False, "error": f"session not found: {exc}"}
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
        except (InteractionError, ProcessError) as exc:
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
                "version": info["version"], "build": info.get("commit"), "channel": info["kind"]}

    def _op_meta_info(self, request: dict[str, Any]) -> dict[str, Any]:
        return meta_info()

    def _op_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"sessions": self.controller.list()}

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
        session.write(request["text"])
        return {"session": self.controller.summary(session)}

    def _op_key(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
        session.send_key(request["key"])
        return {"session": self.controller.summary(session)}

    def _op_enter(self, request: dict[str, Any]) -> dict[str, Any]:
        session = self.controller.get(request["target"])
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

    def _op_agent_create(self, request: dict[str, Any]) -> dict[str, Any]:
        harness = self.controller.create_agent(
            request["kind"],
            name=request.get("name"),
            backend=request.get("backend"),
            cwd=request.get("cwd"),
            team=request.get("team"),
            cols=_dim(request.get("cols", 120), "cols"),
            rows=_dim(request.get("rows", 40), "rows"),
            wait_ready=bool(request.get("wait_ready", False)),
            timeout=float(request.get("timeout", 30.0)),
            args=request.get("args"),
            workspace_mode=request.get("workspace_mode"),
            host=request.get("host"),
        )
        return {"agent": self.controller.agent_summary(harness)}

    def _op_agent_list(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("viewer"):
            self._viewer_seen = time.monotonic()
        return {"agents": self.controller.list_agents()}

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
        """Claude's PermissionRequest hook: wait for an answer from a front-end.

        Only while someone has the Web UI open (otherwise nobody could answer
        and the TUI would just be delayed); ``CREWHALL_PERMISSION_WAIT``
        (seconds, 0 disables) bounds the wait before Claude shows its own dialog.
        """
        try:
            wait = float(settings.get("agents.permission_wait"))
        except ValueError:
            wait = PERMISSION_WAIT
        if time.monotonic() - self._viewer_seen > VIEWER_FRESH:
            wait = 0.0
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

    def _op_agent_stop(self, request: dict[str, Any]) -> dict[str, Any]:
        info = self.controller.remove_agent(
            request["target"], force=bool(request.get("force", False))
        )
        return {"agent": info.to_dict()}

    def _op_message_send(self, request: dict[str, Any]) -> dict[str, Any]:
        delivery = self.controller.send_message(
            request["sender"], request["recipient"], request["body"]
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
        team = self.controller.set_team_workspace(
            request["target"], request.get("workspace")
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

    def _op_fs_complete(self, request: dict[str, Any]) -> dict[str, Any]:
        from . import fscomplete

        roots = list(settings.get("security.fs_roots") or [])
        try:
            roots += [t["workspace"] for t in self.controller.list_teams() if t.get("workspace")]
        except Exception:  # noqa: BLE001 - missing teams must not break completion
            pass
        try:
            roots += [a["cwd"] for a in self.controller.list_agents() if a.get("cwd")]
        except Exception:  # noqa: BLE001
            pass
        actor = request.get("_actor") if isinstance(request.get("_actor"), dict) else {}
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
        threading.Thread(target=self._delayed_stop, daemon=True).start()
        return {"stopping": True}

    def _delayed_stop(self) -> None:
        time.sleep(0.1)
        self.stop()


def _start_tmpdir_janitor(interval: float | None = None) -> None:
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
            except Exception:
                log.exception("tmpdir janitor failed")

    threading.Thread(target=loop, name="tmpdir-janitor", daemon=True).start()


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        filename=paths.log_path(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crewhalld")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--socket", default=None)
    args = parser.parse_args(argv)

    _setup_logging()
    paths.cleanup_tmpdir()  # prune any leaked agent temp files on startup
    _start_tmpdir_janitor()
    controller = Controller(adopt=True, persist=True)
    controller.restore()
    server = Server(controller, args.socket or paths.socket_path())

    def handle_signal(signum: int, _frame: Any) -> None:
        log.info("received signal %s, stopping", signum)
        server.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        server.serve_forever()
    except Exception:
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
