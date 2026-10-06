from __future__ import annotations

import glob
import logging
import os
import secrets
import threading
import re
import time
import uuid
from typing import Any

from . import brand, hooks, paths, settings
from .usage import parse_usage
from . import activity as act
from .backends import HostUnreachable, get_backend
from .backends import ssh_tmux as ssh_tmux_backend
from .backends import tmux as tmux_backend
from .control_files import ensure_managed_section
from .harness import AgentInfo, Harness, HarnessError, get_harness
from . import interactions as ix
from . import processes as procs
from .opencode_link import OpenCodeLink, free_port
from .messaging import Delivery, Messaging, MessagingError
from .requests import OPEN_STATES, RequestBoard, RequestError
from .persistence import StateStore
from .remote_link import RemoteLinks
from .session import InteractiveSession
from .team import Team, TeamRegistry
from .types import SessionSpec, Status, new_session_id, parse_agent_args

log = logging.getLogger("crewhall.controller")



# Seconds an agent-originated message waits for a busy recipient before the bounded retry queue takes over.
AGENT_SEND_WAIT = 5.0

class SessionNotFound(KeyError):
    pass


class AgentNotFound(KeyError):
    pass


class AgentRegistry:
    def __init__(self, on_add=None) -> None:
        self._agents: dict[str, Harness] = {}
        self._lock = threading.RLock()
        self._on_add = on_add

    def add(self, harness: Harness) -> None:
        if self._on_add is not None:
            self._on_add(harness)
        with self._lock:
            self._agents[harness.agent_id] = harness

    def all(self) -> list[Harness]:
        with self._lock:
            return list(self._agents.values())

    def resolve(self, target: str) -> Harness:
        with self._lock:
            if target in self._agents:
                return self._agents[target]
            matches = [
                a
                for a in self._agents.values()
                if a.name == target or a.agent_id.startswith(target)
            ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise AgentNotFound(target)
            raise AgentNotFound(
                f"ambiguous agent {target!r}: {', '.join(a.agent_id for a in matches)}"
            )

    def remove(self, target: str) -> None:
        with self._lock:
            self._agents.pop(target, None)


class Registry:
    def __init__(self) -> None:
        self._sessions: dict[str, InteractiveSession] = {}
        self._lock = threading.RLock()

    def add(self, session: InteractiveSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def all(self) -> list[InteractiveSession]:
        with self._lock:
            return list(self._sessions.values())

    def resolve(self, target: str) -> InteractiveSession:
        with self._lock:
            if target in self._sessions:
                return self._sessions[target]
            matches = [
                s
                for s in self._sessions.values()
                if s.name == target or s.session_id.startswith(target)
            ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise SessionNotFound(target)
            raise SessionNotFound(
                f"ambiguous target {target!r}: {', '.join(s.session_id for s in matches)}"
            )

    def remove(self, target: str) -> None:
        with self._lock:
            self._sessions.pop(target, None)


# Seconds to let OpenCode notice a killed tool command before stopping its turn.
OPENCODE_UNSTICK_GRACE = 3.0

_OPENCODE_INTERACTION_EVENTS = (
    "permission.asked", "permission.replied",
    "question.asked", "question.replied", "question.rejected",
)


def _shq(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _ssh_hint(cfg: dict[str, Any], detail: str) -> str:
    low = detail.lower()
    dest, port = cfg["ssh"], cfg.get("port", 22)
    if "host key verification failed" in low or "no matching host key" in low:
        if cfg.get("known_hosts"):
            host = dest.split("@")[-1]
            return (f"Host key not trusted. Add it to {cfg['known_hosts']} "
                    f"(e.g. ssh-keyscan -p {port} {host} >> that file) after checking its fingerprint.")
        return (f"Host key not trusted yet. Run once in a terminal: ssh -p {port} {dest} "
                "(verify the fingerprint, accept it), then test again.")
    if "permission denied" in low:
        return "Authentication failed: the key is not authorized for that user (password login is disabled)."
    if "timed out" in low or "timeout" in low:
        return "The host did not answer in time (check the address, port and network)."
    if "refused" in low:
        return "Connection refused: nothing is listening on that address and port."
    if "could not resolve" in low or "name or service not known" in low:
        return "The host name could not be resolved."
    return "Could not connect over SSH."


class Controller:
    def __init__(self, adopt: bool = True, persist: bool = False) -> None:
        self.registry = Registry()
        self.agents = AgentRegistry(on_add=self._wire_harness)
        self.teams = TeamRegistry(self.agents)
        self.teams.remote_validator = self._verify_remote_dir
        self._agent_tokens: dict[str, str] = {}
        self._base_env: dict[str, dict[str, str]] = {}
        # Extra CLI arguments per agent (e.g. ``--agent reviewer``), kept so a
        # restored/restarted agent is relaunched with the same command line.
        self._agent_args: dict[str, list[str]] = {}
        # Configured SSH host per agent (None = local), kept for restart/restore.
        self._agent_hosts: dict[str, str | None] = {}
        # Last adoption probe per host: (reachable, wall-clock time).
        self._host_health: dict[str, tuple[bool, float]] = {}
        # Reverse SSH tunnels + restricted gateways for hosts with tunnel=true.
        self.remote_links = RemoteLinks()
        self.remote_links.host_of = self._host_of_agent
        # Agent lifecycle hooks (Claude ``--settings``): an exact turn-complete
        # signal pushed to the daemon instead of guessed from the screen.
        self.hooks_enabled = settings.get("agents.hooks")
        self._hook_events: dict[str, dict[str, Any]] = {}
        self._bridges: dict[str, threading.Event] = {}
        # OpenCode subagent sessions per agent: never adopted as the conversation.
        self._opencode_children: dict[str, set[str]] = {}
        # Conversations an agent left with /clear or /new: never picked again by
        # discovery or by activity, only if the TUI explicitly creates/selects them.
        self._retired_conversations: dict[str, set[str]] = {}
        # Claude ``tasks`` directories seen per agent (outputs of its commands).
        self._agent_task_dirs: dict[str, set[str]] = {}
        # Temporary MCP config files (0600) per agent, deleted when it closes.
        self._mcp_files: dict[str, str] = {}
        # Git worktrees created for agents in ``worktree`` mode.
        self._worktrees: dict[str, dict[str, Any]] = {}
        # Why a worktree could not be created (shown to the user, never silent).
        self._worktree_warnings: dict[str, str] = {}
        # Permission prompts / questions the agents' TUIs are waiting on.
        self.interactions = ix.InteractionBoard()
        # Launch Claude with a known --session-id so its full history can be read.
        self.track_conversations = settings.get("agents.conversations")
        self._agent_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._store = StateStore() if persist else None
        self.messaging = Messaging(self.agents, revive=self._revive_agent)
        self.requests = RequestBoard(
            max_open_per_agent=int(settings.get("requests.max_open_per_agent")),
            max_depth=int(settings.get("requests.max_depth")),
            default_timeout=float(settings.get("requests.default_timeout_s")),
            max_body=int(settings.get("requests.max_body_chars")),
        )
        if adopt:
            self._adopt_tmux()

    # ---------------------------------------------------- identity / context
    def _agent_token(self, agent_id: str) -> str:
        with self._locks_guard:
            token = self._agent_tokens.get(agent_id)
            if token is None:
                token = secrets.token_hex(16)
                self._agent_tokens[agent_id] = token
            return token

    def _lock_for_agent(self, agent_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._agent_locks.get(agent_id)
            if lock is None:
                lock = threading.Lock()
                self._agent_locks[agent_id] = lock
            return lock

    def _team_ids_of(self, agent_id: str) -> list[str]:
        return [t.team_id for t in self.teams.list() if agent_id in t.agent_ids]

    def _team_names_of(self, agent_id: str) -> list[str]:
        return [t.name for t in self.teams.list() if agent_id in t.agent_ids]

    def _shared_team(self, a: str, b: str) -> bool:
        return bool(set(self._team_ids_of(a)) & set(self._team_ids_of(b)))

    def set_gateway_dispatch(self, dispatch: Any) -> None:
        """Daemon hook: the op dispatcher the host gateways forward to."""
        self.remote_links.dispatch = dispatch

    def _host_of_agent(self, ref: str) -> str | None:
        return self._agent_hosts.get(self.agents.resolve(ref).agent_id)

    def _build_agent_env(
        self,
        agent_id: str,
        name: str,
        teams: list[str],
        extra: dict[str, str] | None,
        host: str | None = None,
    ) -> dict[str, str]:
        env = dict(extra or {})
        self._ensure_usable_tmpdir(env)
        values = {
            "AGENT_ID": agent_id,
            "AGENT_NAME": name or agent_id,
            "TEAMS": ",".join(teams),
            "TOKEN": self._agent_token(agent_id),
            "SOCKET": paths.socket_path(),
        }
        if host and settings.host(host).get("tunnel"):
            # The remote agent reaches the restricted gateway, never the daemon.
            # A down tunnel raises HostUnreachable: the agent is not launched
            # with a socket path that cannot work.
            values["SOCKET"] = self.remote_links.ensure(settings.host(host)).remote_socket
            values["GATEWAY"] = "1"
        for key, value in values.items():
            env[f"{brand.ENV_PREFIX}_{key}"] = value
            env[f"{brand.LEGACY_ENV_PREFIX}_{key}"] = value  # old clients/scripts
        return env

    @staticmethod
    def _dir_has_space(path: str, need_bytes: int = 256 * 1024 * 1024) -> bool:
        import shutil

        try:
            return shutil.disk_usage(path).free >= need_bytes
        except OSError:
            return False

    @staticmethod
    def _ensure_usable_tmpdir(env: dict[str, str]) -> None:
        """Give agent processes a TMPDIR that is actually usable.

        Agent CLIs (Claude Code, OpenCode) unpack helper libraries and spawn
        services under TMPDIR. This machine's ``/tmp`` is a tmpfs that can be
        exhausted (per-user quota -> EDQUOT) and where OpenCode cannot load its
        native render library, while ``tempfile.gettempdir()`` may report an
        unrelated fallback (``/var/tmp``). We therefore *probe* the real temp
        filesystem and fall back to a directory under the home cache filesystem
        when it is not writable with headroom. An explicit user TMPDIR always
        wins.
        """
        if env.get("TMPDIR"):
            return
        tmpdir = paths.usable_tmpdir()
        if tmpdir:
            env["TMPDIR"] = tmpdir
            env.setdefault("CLAUDE_CODE_TMPDIR", tmpdir)

    def _hooks_settings_path(self, host: str | None = None) -> str | None:
        if not self.hooks_enabled:
            return None
        if host:
            # Hooks need the gateway: without a tunnel nothing could receive them.
            return hooks.remote_claude_settings_json() if settings.host(host).get("tunnel") else None
        try:
            return hooks.ensure_claude_settings()
        except OSError:
            return None

    _SESSION_FLAGS = ("--session-id", "--resume", "-r", "--continue", "-c")

    def _new_conversation_id(self, kind: str, extra_args: list[str]) -> str | None:
        """A fresh conversation id, unless disabled or the user chose a session."""
        if not self.track_conversations or not get_harness(kind).uses_hooks:
            return None
        if any(a in self._SESSION_FLAGS or a.startswith("--session-id=") for a in extra_args):
            return None
        return str(uuid.uuid4())

    def _launch_command(
        self,
        kind: str,
        extra_args: list[str],
        conversation_id: str | None = None,
        port: int | None = None,
        mcp_args: list[str] | None = None,
        host: str | None = None,
    ) -> list[str]:
        cls = get_harness(kind)
        # A remote agent runs the remote machine's own CLI (bare name, remote
        # PATH): a local override path or local settings file means nothing there.
        command = cls.command() if host else (settings.provider_command(kind) or cls.command())
        return [
            *command,
            *cls.launch_args(self._hooks_settings_path(host), conversation_id, port),
            *(mcp_args or []),
            *extra_args,
        ]

    # -- OpenCode local server bridge --------------------------------------
    def _new_opencode_link(self, kind: str, extra_args: list[str]) -> OpenCodeLink | None:
        """Port + password for the agent's own server, unless the user set a port."""
        if not self.track_conversations or not get_harness(kind).uses_local_server:
            return None
        if any(a == "--port" or a.startswith("--port=") for a in extra_args):
            return None
        return OpenCodeLink(free_port(), secrets.token_urlsafe(24))

    def _make_worktree(self, agent_id: str, agent_name: str, team: str | None,
                       cwd: str, host: str | None = None) -> str:
        from . import remote_worktrees, worktrees

        hcfg = settings.host(host) if host else None
        wt = remote_worktrees if hcfg else worktrees
        extra = (hcfg,) if hcfg else ()
        if not cwd:
            self._worktree_warnings[agent_id] = "a worktree needs a repository path"
            return cwd
        if not wt.is_git_repo(*extra, cwd):
            self._worktree_warnings[agent_id] = (
                f"{cwd} is not a git repository; using the shared workspace"
            )
            return cwd
        try:
            path = wt.create(*extra, cwd, team, agent_name)
        except worktrees.WorktreeError as exc:
            self._worktree_warnings[agent_id] = f"worktree not created: {exc}"
            return cwd
        self._worktrees[agent_id] = {
            "path": path, "repo": cwd, "team": team,
            "branch": worktrees.branch_name(team, agent_name),
            "host": host,
        }
        return path

    @staticmethod
    def _wt_host(wt: dict[str, Any]) -> dict[str, Any] | None:
        """Host config of a remote worktree (None = local)."""
        if not wt.get("host"):
            return None
        return settings.host(wt["host"])

    def worktree_info(self, agent_id: str) -> dict[str, Any]:
        return dict(self._worktrees.get(agent_id, {}))

    def _cleanup_worktree(self, agent_id: str, *, force: bool = False) -> None:
        """Remove the worktree on delete only when it is safe; otherwise keep it."""
        from . import remote_worktrees, worktrees

        wt = self._worktrees.get(agent_id)
        if not wt:
            return
        hcfg = self._wt_host(wt)
        mod = remote_worktrees if hcfg else worktrees
        extra = (hcfg,) if hcfg else ()
        if force:
            ok, reason = True, "forced"
        else:
            ok, reason = mod.can_remove(*extra, wt["path"], repo=wt["repo"])
        if ok:
            try:
                mod.remove(*extra, wt["path"], force=force)
                self._worktrees.pop(agent_id, None)
                return
            except worktrees.WorktreeError as exc:
                reason = str(exc)
        self._worktree_warnings[agent_id] = f"worktree kept at {wt['path']}: {reason}"

    def list_worktrees(self) -> list[dict[str, Any]]:
        from . import remote_worktrees, worktrees

        out: list[dict[str, Any]] = []
        for agent_id, wt in self._worktrees.items():
            hcfg = self._wt_host(wt)
            st = (remote_worktrees.status(hcfg, wt["path"], repo=wt["repo"]) if hcfg
                  else worktrees.status(wt["path"], repo=wt["repo"]))
            out.append({"agent_id": agent_id, "path": wt["path"], "branch": wt.get("branch"),
                        "repo": wt["repo"], "dirty": st["dirty"], "unmerged": st["unmerged"],
                        "exists": st["exists"], "orphan": False, "host": wt.get("host"),
                        "unknown": bool(st.get("unknown"))})
        root = worktrees.root()
        known = {os.path.realpath(w["path"]) for w in self._worktrees.values()}
        if os.path.isdir(root):
            for dirpath, dirs, _files in os.walk(root):
                if os.path.isfile(os.path.join(dirpath, ".git")):
                    dirs[:] = []
                    if os.path.realpath(dirpath) not in known:
                        st = worktrees.status(dirpath)
                        out.append({"agent_id": None, "path": dirpath, "branch": st["branch"],
                                    "repo": None, "dirty": st["dirty"], "unmerged": st["unmerged"],
                                    "exists": st["exists"], "orphan": True})
        return out

    def discard_worktree(self, path: str, confirm: str | None) -> dict[str, Any]:
        from . import remote_worktrees, worktrees

        if confirm != "DISCARD":
            raise ValueError("type DISCARD to discard a worktree with changes")
        for agent_id, wt in list(self._worktrees.items()):
            if wt.get("host") and wt["path"] == path:  # a managed remote worktree
                remote_worktrees.remove(self._wt_host(wt), path, force=True)
                self._worktrees.pop(agent_id, None)
                return {"discarded": path, "host": wt["host"]}
        real, base = os.path.realpath(path), os.path.realpath(worktrees.root())
        if not (real == base or real.startswith(base + os.sep)):
            raise ValueError("worktree is outside the managed directory")
        worktrees.remove(path, force=True)
        for agent_id, wt in list(self._worktrees.items()):
            if os.path.realpath(wt["path"]) == real:
                self._worktrees.pop(agent_id, None)
        return {"discarded": path}

    def _mcp_for(self, kind: str, harness_cls: type[Harness]) -> tuple[list[str], str | None]:
        """MCP launch args + (optional) temp config file, when enabled."""
        if not harness_cls.mcp_supported or not settings.provider(kind).get("mcp"):
            return [], None
        path = harness_cls.mcp_config_file()
        return harness_cls.mcp_launch_args(path), path

    def _mcp_cleanup(self, agent_id: str) -> None:
        from .mcp import config as mcp_config

        mcp_config.cleanup(self._mcp_files.pop(agent_id, None))

    def _attach_link(self, harness: Harness, link: OpenCodeLink | None) -> None:
        """Wire the link to the harness and follow its events in the background."""
        old = self._bridges.pop(harness.agent_id, None)
        if old:
            old.set()
        harness.link = link
        if link is not None and hasattr(harness, "conversation_resolver"):
            harness.conversation_resolver = lambda: self._discover_opencode_session(harness, link)
        if link is None:
            return
        stop = threading.Event()
        self._bridges[harness.agent_id] = stop
        threading.Thread(
            target=self._follow_events, args=(harness, link, stop),
            name=f"oc-bridge-{harness.agent_id}", daemon=True,
        ).start()

    def _discover_opencode_session(self, harness: Harness, link: OpenCodeLink) -> str | None:
        claimed = {
            h.conversation_id for h in self.agents.all()
            if h is not harness and h.conversation_id
        } | self._retired_conversations.get(harness.agent_id, set())
        since = max(harness.session.created_at, getattr(harness, "conversation_since", 0.0))
        sid = link.discover(harness.session.spec.cwd, since * 1000, claimed)
        if sid and not harness.conversation_id:
            harness.conversation_id = sid
        return harness.conversation_id

    def _follow_events(self, harness: Harness, link: OpenCodeLink, stop: threading.Event) -> None:
        def should_stop() -> bool:
            return stop.is_set() or not harness.session.status.alive

        # The TUI needs a moment to open its port, so connection errors are
        # simply retried until the agent stops.
        while not should_stop():
            try:
                for event in link.events(should_stop):
                    self._on_opencode_event(harness, event)
            except Exception:  # noqa: BLE001
                pass
            if should_stop():
                return
            if not harness.conversation_id:  # the creation event may have been missed
                try:
                    self._discover_opencode_session(harness, link)
                except Exception:  # noqa: BLE001
                    pass
            stop.wait(1.0)

    def _on_opencode_event(self, harness: Harness, event: dict[str, Any]) -> None:
        kind = event.get("type")
        props = event.get("properties") or {}
        sid = props.get("sessionID")
        if kind in _OPENCODE_INTERACTION_EVENTS:
            self._on_opencode_interaction(harness, kind, props)
            return
        if kind == "server.connected" and harness.link is not None:
            # (Re)connected: requests raised while we were not listening.
            self._sync_opencode_interactions(harness, harness.link)
            return
        if kind == "session.created":
            info = props.get("info") or {}
            sid = sid or info.get("id")
            # ``/new`` creates a root session; subagent (task) sessions have a parent.
            if sid and not info.get("parentID"):
                self._adopt_conversation(harness, sid)
            elif sid:
                self._opencode_children.setdefault(harness.agent_id, set()).add(sid)
            return
        if sid and sid != harness.conversation_id and kind == "session.status":
            # Activity in another session: the user switched to it (/sessions)
            # or the creation event was missed. Follow it unless it is a child.
            self._follow_opencode_session(harness, sid)
        if sid and sid in self._opencode_children.get(harness.agent_id, ()):
            return  # a subagent's turn is not the agent's turn
        if kind == "session.status" and (props.get("status") or {}).get("type") == "busy":
            # `busy` repeats while the turn runs; only the transition counts.
            last = self._hook_events.get(harness.agent_id) or {}
            if last.get("event") != "prompt_submit":
                self._apply_hook(harness, "prompt_submit")
        elif kind == "session.idle":
            self._apply_hook(harness, "stop")

    # -- interactions (permissions / questions) ----------------------------
    def _open_opencode_request(self, harness: Harness, kind: str, req: dict[str, Any]) -> None:
        rid = req.get("id")
        if not isinstance(rid, str) or not rid:
            return
        if kind == "permission":
            data = ix.opencode_permission(req)
        else:
            data = {"questions": ix.normalize_questions(req.get("questions")),
                    "session": req.get("sessionID")}
            if not data["questions"]:
                return
        item, created = self.interactions.open(
            harness.agent_id, kind, f"{harness.agent_id}:{rid}", data,
            ref={"source": "opencode", "request_id": rid},
        )
        if created:
            self._log_interaction(harness, "asked", item)

    def _on_opencode_interaction(self, harness: Harness, kind: str, props: dict[str, Any]) -> None:
        if kind == "permission.asked":
            self._open_opencode_request(harness, "permission", props)
        elif kind == "question.asked":
            self._open_opencode_request(harness, "question", props)
        else:  # replied / rejected, possibly in the TUI itself
            rid = props.get("requestID")
            if isinstance(rid, str):
                answer = (
                    {"reply": props.get("reply")} if kind == "permission.replied"
                    else {"answers": props.get("answers")} if kind == "question.replied"
                    else {"reject": True}
                )
                item = self.interactions.close(f"{harness.agent_id}:{rid}", answer=answer, by="agent")
                if item is not None and item.answered_by == "agent":
                    self._log_interaction(harness, "answered", item)

    def _sync_opencode_interactions(self, harness: Harness, link: OpenCodeLink) -> None:
        try:
            perms, questions = link.pending_permissions(), link.pending_questions()
        except Exception:  # noqa: BLE001 - the server may not be up yet
            return
        live = set()
        for req in perms:
            self._open_opencode_request(harness, "permission", req)
            live.add(f"{harness.agent_id}:{req.get('id')}")
        for req in questions:
            self._open_opencode_request(harness, "question", req)
            live.add(f"{harness.agent_id}:{req.get('id')}")
        for item in self.interactions.pending(harness.agent_id):
            if item.ref.get("source") == "opencode" and item.interaction_id not in live:
                self.interactions.close(item.interaction_id)

    def _log_interaction(self, harness: Harness, what: str, item: ix.Interaction) -> None:
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("interaction", {"what": what, "id": item.interaction_id, "kind": item.kind,
                                   "answer": item.answer, "by": item.answered_by})

    def list_interactions(self, target: str | None = None) -> list[dict[str, Any]]:
        agent_id = self.agents.resolve(target).agent_id if target else None
        return [i.to_dict() for i in self.interactions.pending(agent_id)]

    def respond_interaction(
        self, interaction_id: str, answer: Any, by: str = "user",
    ) -> dict[str, Any]:
        """Answer a pending permission/question through the agent CLI's own API."""
        item = self.interactions.get(interaction_id)
        if item.state != "pending":
            raise ix.InteractionError("this request was already answered")
        clean = ix.validate_answer(item, answer)
        harness = self.agents.resolve(item.agent_id)
        if item.ref.get("source") == "claude":
            # The agent's permission hook is blocked on this; it hands the decision to Claude.
            closed = self.interactions.close(interaction_id, answer=clean, by=by)
            if closed is not None:
                self._log_interaction(harness, "answered", closed)
            return (closed or item).to_dict()
        link = harness.link
        if item.ref.get("source") != "opencode" or link is None:
            raise ix.InteractionError("this agent cannot be answered from here")
        rid = item.ref["request_id"]
        try:
            if item.kind == "permission":
                link.reply_permission(rid, clean["reply"], clean.get("message"))
            elif clean.get("reject"):
                link.reject_question(rid)
            else:
                link.reply_question(rid, clean["answers"])
        except OSError as exc:
            # Gone on the agent's side (answered in the TUI, agent restarted…).
            self._sync_opencode_interactions(harness, link)
            raise ix.InteractionError(f"the agent did not accept the answer: {exc}") from exc
        closed = self.interactions.close(interaction_id, answer=clean, by=by)
        if closed is not None:
            self._log_interaction(harness, "answered", closed)
        return (closed or item).to_dict()

    def claude_permission_request(
        self, agent: str, token: str | None, payload: Any, wait: float,
    ) -> dict[str, Any]:
        """A Claude permission dialog / question, held until answered or ``wait`` passes.

        Returns ``{"decision": {...}}`` for Claude, or ``{"decision": None}``
        to let Claude show its own dialog (no answer in time, answered "in the
        terminal", unusable payload, or the feature disabled with ``wait <= 0``).
        """
        harness = self.agents.resolve(agent)
        self._check_token(harness.agent_id, token)
        if wait <= 0 or not isinstance(payload, dict):
            return {"decision": None}
        parsed = ix.claude_request(payload)
        if parsed is None:
            return {"decision": None}
        kind, data = parsed
        ref_id = payload.get("tool_use_id")
        if not (isinstance(ref_id, str) and ref_id.isascii() and len(ref_id) <= 100):
            ref_id = uuid.uuid4().hex
        item, created = self.interactions.open(
            harness.agent_id, kind, f"{harness.agent_id}:{ref_id}", data,
            ref={"source": "claude"},
        )
        if created:
            self._log_interaction(harness, "asked", item)
        if not item.done.wait(wait):
            # Nobody answered here: the TUI dialog takes over.
            self.interactions.close(item.interaction_id, answer={"terminal": True}, by="timeout")
        return {"decision": ix.claude_decision(item, payload, item.answer)}

    def _follow_opencode_session(self, harness: Harness, sid: str) -> None:
        children = self._opencode_children.setdefault(harness.agent_id, set())
        if sid in children or harness.link is None:
            return
        if sid in self._retired_conversations.get(harness.agent_id, ()):
            return  # the old conversation finishing up after /new
        try:
            info = harness.link.get_json(f"/session/{sid}")
        except Exception:  # noqa: BLE001
            return
        if not isinstance(info, dict):
            return
        if info.get("parentID"):
            children.add(sid)
        else:
            self._adopt_conversation(harness, sid)

    def _wire_harness(self, harness: Harness) -> None:
        harness.on_new_session = lambda previous: self._conversation_left(harness, previous)

    def _conversation_left(self, harness: Harness, previous: str | None) -> None:
        """The TUI dropped ``previous`` (/clear, /new): stop showing it.

        The next conversation is adopted when the CLI reports it (Claude's
        SessionStart hook, OpenCode's ``session.created`` on the first prompt).
        """
        harness.conversation_since = time.time()
        if previous is None or harness.conversation_id != previous:
            return  # nothing to leave, or the new one was already reported
        self._retired_conversations.setdefault(harness.agent_id, set()).add(previous)
        harness.conversation_id = None
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("conversation", {"left": previous})
        self._persist()

    _CONVERSATION_ID = {
        "claude": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
        "opencode": re.compile(r"^ses_[A-Za-z0-9]+$"),
    }

    def _adopt_conversation(self, harness: Harness, conversation_id: Any) -> bool:
        """Point the agent at the conversation its TUI is now in.

        ``/clear`` (Claude) and ``/new`` (OpenCode) start a new session inside
        the same process, so the id chosen at launch goes stale and the
        Conversation view would keep showing the old one.
        """
        pattern = self._CONVERSATION_ID.get(harness.kind)
        if not (pattern and isinstance(conversation_id, str) and pattern.match(conversation_id)):
            return False
        if conversation_id == harness.conversation_id:
            return False
        harness.conversation_id = conversation_id
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("conversation", {"conversation_id": conversation_id})
        self._persist()
        return True

    def _apply_hook(self, harness: Harness, event: str) -> dict[str, Any]:
        now = time.time()
        self._hook_events[harness.agent_id] = {"event": event, "at": now}
        harness.on_hook(event, now)
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("hook", {"event": event})
        return {"agent_id": harness.agent_id, "event": event, "at": now}

    def record_hook(
        self, agent: str, token: str | None, event: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a lifecycle event pushed by an agent's own hook (token-auth).

        ``conversation_id`` is the session the hook fired in; it changes after
        ``/clear`` or ``/resume`` and the agent follows it.
        """
        harness = self.agents.resolve(agent)
        self._check_token(harness.agent_id, token)
        if event not in hooks.EVENTS:
            raise ValueError(f"unknown hook event {event!r}")
        if conversation_id and self.track_conversations:
            self._adopt_conversation(harness, conversation_id)
        if event == "session_start":
            return {"agent_id": harness.agent_id, "event": event,
                    "conversation_id": harness.conversation_id}
        return self._apply_hook(harness, event)

    def _ensure_control_file(self, cwd: str | None, kind: str | None = None) -> None:
        if not cwd:
            return
        try:
            ensure_managed_section(cwd, kind=kind)
        except Exception:  # noqa: BLE001 - never block an agent over a doc file
            pass

    def _adopt_tmux(self) -> None:
        self._adopt_local_tmux()
        self._start_remote_adopter()

    def _adopt_local_tmux(self) -> None:
        for meta in tmux_backend.existing_sessions():
            if meta.get("backend") != "tmux":
                continue
            sid = meta["session_id"]
            try:
                self.registry.resolve(sid)
                continue
            except SessionNotFound:
                pass
            created = meta.get("created_at")
            try:
                created_f = float(created) if created else None
            except ValueError:
                created_f = None
            spec = SessionSpec(command=meta.get("command") or sid)
            try:
                session = InteractiveSession.adopt(
                    tmux_backend.TmuxBackend(), spec, sid, created_at=created_f
                )
            except Exception:
                continue
            self.registry.add(session)

    # -- remote hosts ------------------------------------------------------
    # Adoption runs off the startup path: a host that is down must never delay
    # the daemon. Each host is retried in the background until it answers.
    REMOTE_ADOPT_RETRY = 30.0
    REMOTE_ADOPT_IDLE = 120.0

    def _start_remote_adopter(self) -> None:
        if not settings.hosts():
            return
        stop = threading.Event()
        self._remote_adopt_stop = stop
        threading.Thread(
            target=self._remote_adopt_loop, args=(stop,), name="ssh-adopt", daemon=True
        ).start()

    def _remote_adopt_loop(self, stop: threading.Event) -> None:
        delay = 0.0
        while not stop.is_set():
            if delay:
                stop.wait(delay)
                if stop.is_set():
                    return
            failed = False
            for name in list(settings.hosts()):
                try:
                    self._adopt_remote_host(name, settings.host(name))
                    self._host_health[name] = (True, time.time())
                except HostUnreachable:
                    failed = True
                    self._host_health[name] = (False, time.time())
                    log.warning("ssh host %s unreachable; will retry", name)
                except Exception:
                    failed = True
                    log.exception("could not adopt sessions on ssh host %s", name)
            delay = self.REMOTE_ADOPT_RETRY if failed else self.REMOTE_ADOPT_IDLE

    def _adopt_remote_host(self, name: str, cfg: dict[str, Any]) -> None:
        for meta in ssh_tmux_backend.existing_sessions(cfg):
            if meta.get("backend") != "tmux":
                continue
            sid = meta["session_id"]
            try:
                self.registry.resolve(sid)
                continue
            except SessionNotFound:
                pass
            created = meta.get("created_at")
            try:
                created_f = float(created) if created else None
            except ValueError:
                created_f = None
            spec = SessionSpec(command=meta.get("command") or sid, host=name)
            try:
                session = InteractiveSession.adopt(
                    ssh_tmux_backend.SshTmuxBackend(cfg), spec, sid, created_at=created_f
                )
            except Exception:
                continue
            self.registry.add(session)

    def _backend_for(self, backend_name: str | None, host_name: str | None) -> Any:
        if host_name:
            return get_backend("ssh-tmux", host=settings.host(host_name))
        return get_backend(backend_name)

    def create(self, spec: SessionSpec, backend_name: str | None = None) -> InteractiveSession:
        if not spec.cwd and not spec.host:
            spec.cwd = os.getcwd()
        session = InteractiveSession(self._backend_for(backend_name, spec.host), spec)
        session.start()
        self.registry.add(session)
        return session

    def get(self, target: str) -> InteractiveSession:
        return self.registry.resolve(target)

    def resolve_cwd(self, cwd: str | None, team: str | None) -> str:
        """cwd precedence: explicit agent cwd > team workspace > default (cwd).

        An explicit cwd is always honoured. Otherwise, if a Team is given and it
        has a workspace, that is used. Finally, the process cwd is the default.
        """
        if cwd:
            return os.path.abspath(os.path.expanduser(cwd))
        if team:
            try:
                t = self.teams.get(team)
            except Exception:
                t = None
            if t is not None and t.workspace:
                return t.workspace
        return os.getcwd()

    def create_agent(
        self,
        kind: str,
        *,
        name: str | None = None,
        backend: str | None = None,
        cwd: str | None = None,
        team: str | None = None,
        cols: int = 120,
        rows: int = 40,
        env: dict[str, str] | None = None,
        wait_ready: bool = False,
        timeout: float = 30.0,
        agent_id: str | None = None,
        created_at: float | None = None,
        args: Any = None,
        workspace_mode: str | None = None,
        host: str | None = None,
    ) -> Harness:
        harness_cls = get_harness(kind)
        if not settings.provider_enabled(kind):
            raise ValueError(f"provider {kind!r} is disabled in Settings")
        given = parse_agent_args(args)
        extra_args = [*settings.provider_args(kind, given), *given]  # provider defaults first
        team_obj = self._team_or_none(team)
        if team_obj is not None and team_obj.host:
            # A remote team: its agents run on its host, in its directory.
            if host and host != team_obj.host:
                raise ValueError(
                    f"team {team_obj.name!r} runs on host {team_obj.host!r}; "
                    f"an agent of another host ({host!r}) cannot join it"
                )
            if backend == "pty":
                raise ValueError(f"the pty backend cannot reach the remote host {team_obj.host!r}")
            host = team_obj.host
            cwd = cwd or team_obj.workspace
        if host:
            settings.host(host)  # reject an unknown host before doing any work
            if backend == "pty":
                raise ValueError(f"the pty backend cannot reach the remote host {host!r}")
            backend = "ssh-tmux"
        elif backend is None and settings.get("agents.default_backend") != "auto":
            backend = settings.get("agents.default_backend")
        env = {**settings.provider_env(kind), **(env or {})}
        conversation_id = self._new_conversation_id(kind, extra_args)
        # The OpenCode server binds the remote loopback and MCP config is a local
        # file: neither is reachable for a remote agent (state falls back to the
        # screen, honestly).
        link = None if host else self._new_opencode_link(kind, extra_args)
        mcp_args, mcp_file = ([], None) if host else self._mcp_for(kind, harness_cls)
        if name and any(h.name == name for h in self.agents.all()):
            raise ValueError(f'agent name "{name}" already exists')
        cwd = cwd if host else self.resolve_cwd(cwd, team)
        session_id = agent_id or new_session_id()
        agent_name = name or session_id
        mode = workspace_mode
        if mode is None and team:
            try:
                t = self.teams.get(team)
                mode = getattr(t, "workspace_mode", None)
            except Exception:  # noqa: BLE001
                mode = None
        if mode == "worktree":
            cwd = self._make_worktree(session_id, agent_name, team, cwd, host)
        agent_env = self._build_agent_env(session_id, agent_name, [], env, host)
        if link:
            agent_env["OPENCODE_SERVER_PASSWORD"] = link.password
        spec = SessionSpec(
            command=self._launch_command(
                kind, extra_args, conversation_id, link.port if link else None, mcp_args,
                host,
            ),
            cwd=cwd,
            env=agent_env,
            cols=cols,
            rows=rows,
            name=name,
            host=host,
        )
        session = InteractiveSession(
            self._backend_for(backend, host), spec, session_id=session_id
        )
        if created_at is not None:
            session.created_at = created_at
        session.start()
        self.registry.add(session)
        harness = harness_cls(session, name=name)
        harness.conversation_id = conversation_id
        self._attach_link(harness, link)
        self.agents.add(harness)
        self._agent_tokens.setdefault(session_id, agent_env["CREWHALL_TOKEN"])
        if mcp_file:
            self._mcp_files[session_id] = mcp_file
        self._base_env.setdefault(session_id, dict(env or {}))
        self._agent_args[session_id] = extra_args
        self._agent_hosts[session_id] = host
        if not host:
            self._ensure_control_file(cwd, kind)
        if team:
            try:
                self.teams.add_member(team, session_id)
            except Exception:
                pass
        self._persist()
        if wait_ready:
            harness.start(timeout=timeout)
        return harness

    # ------------------------------------------------------------ persistence
    def _persist(self) -> None:
        if self._store is not None:
            try:
                self._store.save(self._store.snapshot(self))
            except Exception:
                pass

    def restore(self) -> None:
        """Load persisted Teams and agent definitions (not processes).

        Agents are reconstructed as EXITED logical entities; they are *not*
        started automatically. A later send_message or explicit start activates
        them (same agent_id), preserving identity and membership.
        """
        if self._store is None:
            return
        data = self._store.load()
        for team in data.get("teams", []):
            try:
                self.teams.create(
                    team["name"],
                    [],
                    team_id=team["team_id"],
                    workspace=team.get("workspace"),
                    workspace_mode=team.get("workspace_mode"),
                    created_at=team.get("created_at"),
                    host=team.get("host"),
                    verify_remote=False,  # never block a restart on a host that is down
                )
            except Exception:
                continue
        for agent in data.get("agents", []):
            try:
                self._restore_agent(agent)
            except Exception:
                continue
        try:
            self.requests.load(data.get("requests", []))
        except Exception:  # noqa: BLE001 - a bad row must not stop the daemon
            pass
        # Re-apply memberships after all agents exist.
        for team in data.get("teams", []):
            for agent_id in team.get("agent_ids", []):
                try:
                    self.teams.add_member(team["team_id"], agent_id)
                except Exception:
                    pass

    def _restore_agent(self, agent: dict[str, Any]) -> None:
        agent_id = agent["agent_id"]
        if any(h.agent_id == agent_id for h in self.agents.all()):
            return
        harness_cls = get_harness(agent["kind"])
        extra_args = parse_agent_args(agent.get("args"))
        self._agent_args[agent_id] = extra_args
        host = agent.get("host")
        self._agent_hosts[agent_id] = host
        spec = SessionSpec(
            command=self._launch_command(agent["kind"], extra_args, host=host),
            cwd=agent.get("cwd") or (None if host else os.getcwd()),
            env=None,
            cols=int(agent.get("cols", 120)),
            rows=int(agent.get("rows", 40)),
            name=agent.get("name"),
            host=host,
        )
        session = InteractiveSession(
            self._backend_for(agent.get("backend"), host), spec, session_id=agent_id
        )
        session.created_at = float(agent.get("created_at") or session.created_at)
        session._status = Status.EXITED
        self.registry.add(session)
        harness = harness_cls(session, name=agent.get("name"))
        # Keep the last conversation readable for a stopped agent (Claude reads
        # it from its on-disk file; a revived agent gets a new id).
        harness.conversation_id = agent.get("conversation_id")
        self.agents.add(harness)
        self._base_env[agent_id] = dict(agent.get("env") or {})
        self._agent_tokens.setdefault(agent_id, secrets.token_hex(16))

    # ------------------------------------------------- activation (on demand)
    def _revive_agent(self, harness: Harness) -> Harness:
        revived = self.restart_agent(harness.agent_id)
        revived.start(timeout=30.0)
        return revived

    def restart_agent(self, target: str) -> Harness:
        """Restart the process of a registered agent, preserving its identity."""
        harness = self.agents.resolve(target)
        agent_id = harness.agent_id
        with self._lock_for_agent(agent_id):
            if harness.session.status.alive:
                return harness
            kind = harness.kind
            conversation_id = self._new_conversation_id(
                kind, self._agent_args.get(agent_id, [])
            )
            link = (None if self._agent_hosts.get(agent_id)
                    else self._new_opencode_link(kind, self._agent_args.get(agent_id, [])))
            spec = harness.session.spec
            backend_name = harness.session.backend.name
            host = self._agent_hosts.get(agent_id)
            try:
                harness.session.backend.close()
            except Exception:
                pass
            teams = self._team_names_of(agent_id)
            # Rebuild from the user's original env (not the identity-augmented
            # one) to avoid duplicated/derived keys across restarts.
            base_env = dict(self._base_env.get(agent_id) or {})
            env = self._build_agent_env(agent_id, harness.name or agent_id, teams, base_env, host)
            if link:
                env["OPENCODE_SERVER_PASSWORD"] = link.password
            new_spec = SessionSpec(
                command=self._launch_command(
                    kind, self._agent_args.get(agent_id, []), conversation_id,
                    link.port if link else None, host=host,
                ),
                cwd=spec.cwd,
                env=env,
                cols=spec.cols,
                rows=spec.rows,
                name=harness.name,
                host=host,
            )
            session = InteractiveSession(
                self._backend_for(backend_name, host), new_spec, session_id=agent_id
            )
            session.start()
            self.registry.add(session)
            revived = get_harness(kind)(session, name=harness.name)
            revived.conversation_id = conversation_id
            self._attach_link(revived, link)
            self._persist()
            self.agents.add(revived)
            if not host:
                self._ensure_control_file(spec.cwd, kind)
            return revived

    def _authorize_team(self, sender: Harness, recipient: Harness) -> None:
        if not self._shared_team(sender.agent_id, recipient.agent_id):
            raise MessagingError(
                "permission denied: recipient is not a member of a shared Team"
            )

    def _check_token(self, agent_id: str, token: str | None) -> None:
        if not token or self._agent_tokens.get(agent_id) != token:
            raise MessagingError("invalid agent identity/token")

    def agent_identity(self, target: str, token: str | None) -> dict[str, Any]:
        harness = self.agents.resolve(target)
        self._check_token(harness.agent_id, token)
        team_names = self._team_names_of(harness.agent_id)
        teammates = []
        seen = set()
        for team in self.teams.list():
            if harness.agent_id not in team.agent_ids:
                continue
            for other_id in team.agent_ids:
                if other_id == harness.agent_id or other_id in seen:
                    continue
                seen.add(other_id)
                try:
                    info = self.agents.resolve(other_id).info()
                except Exception:
                    teammates.append({"agent_id": other_id, "name": other_id,
                                      "kind": "?", "state": "missing"})
                    continue
                teammates.append({
                    "agent_id": info.agent_id, "name": info.name,
                    "kind": info.kind, "state": info.state.value,
                })
        return {
            "agent": self.agent_summary(harness),
            "teams": team_names,
            "teammates": teammates,
        }

    def teammates(self, target: str) -> list[dict[str, Any]]:
        """Agents that share at least one Team with the target (no token needed)."""
        harness = self.agents.resolve(target)
        return self.agent_identity(harness.agent_id, self._agent_tokens.get(harness.agent_id))[
            "teammates"
        ]

    # -- the shells an agent runs ------------------------------------------
    def _agent_root(self, harness: Harness) -> int:
        if self._agent_hosts.get(harness.agent_id):
            raise HarnessError(
                harness.agent_id, "process inspection is not available for remote agents (n/d)"
            )
        pid = harness.session.pid
        if not pid or not harness.session.status.alive:
            raise HarnessError(harness.agent_id, "the agent is not running")
        return int(pid)

    def _opencode_running(self, harness: Harness) -> dict[str, dict[str, Any]]:
        return harness.running_shells()

    def _task_dirs(self, harness: Harness) -> set[str]:
        dirs = self._agent_task_dirs.setdefault(harness.agent_id, set())
        roots = [paths.usable_tmpdir(), os.environ.get("TMPDIR"), "/tmp"]
        dirs.update(procs.claude_task_files(harness.conversation_id, roots))
        return dirs

    def list_processes(self, target: str) -> dict[str, Any]:
        harness = self.agents.resolve(target)
        root = self._agent_root(harness)
        running = self._opencode_running(harness) or None
        out = procs.agent_processes(root, running_tools=running)
        out["agent_pid"] = root
        if harness.supports_task_files:
            dirs = self._task_dirs(harness)
            for shell in out["shells"]:
                dirs.add(os.path.dirname(shell["stdout"]))
            # Finished commands of the current conversation only (/clear starts another).
            conv = harness.conversation_id
            dirs = {d for d in dirs if conv and os.path.basename(os.path.dirname(d)) == conv}
            live = {s["output"]["task"] for s in out["shells"] if s.get("output", {}).get("kind") == "file"}
            finished = []
            for directory in dirs:
                for path in glob.glob(os.path.join(directory, "*.output")):
                    task = os.path.basename(path)[: -len(".output")]
                    if task in live:
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    finished.append({"task": task, "size": st.st_size, "mtime": st.st_mtime})
            out["finished"] = sorted(finished, key=lambda t: -t["mtime"])[:30]
        return out

    def process_output(self, target: str, task: str | None = None, call: str | None = None) -> dict[str, Any]:
        """Live output of a shell: a Claude task file or an OpenCode running tool."""
        harness = self.agents.resolve(target)
        if task:
            if not re.match(r"^[A-Za-z0-9_-]{1,64}$", task):
                raise procs.ProcessError("invalid task id")
            for directory in self._task_dirs(harness):
                path = os.path.join(directory, f"{task}.output")
                if os.path.isfile(path):
                    return procs.tail(path)
            return {"available": False, "output": "", "size": 0}
        if call:
            for info in self._opencode_running(harness).values():
                if info.get("call") == call:
                    text = info.get("output") or ""
                    return {"available": True, "output": text[-procs.MAX_OUTPUT:],
                            "size": len(text), "truncated": len(text) > procs.MAX_OUTPUT}
            return {"available": False, "output": "", "size": 0, "finished": True}
        raise procs.ProcessError("task or call is required")

    def signal_process(self, target: str, pid: int, signal: str, by: str = "user") -> dict[str, Any]:
        harness = self.agents.resolve(target)
        root = self._agent_root(harness)
        call = None
        if harness.uses_local_server:
            listing = self.list_processes(harness.agent_id)
            shell = next((s for s in listing["shells"] if s["pid"] == int(pid)), None)
            call = ((shell or {}).get("output") or {}).get("call")
        sent = procs.signal_tree(root, int(pid), str(signal).upper())
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("process_signal", {"pid": int(pid), "signal": str(signal).upper(), "by": by})
        if call:
            threading.Thread(
                target=self._unstick_opencode_tool, args=(harness, int(pid), call),
                name=f"oc-unstick-{harness.agent_id}", daemon=True,
            ).start()
        return {"pid": int(pid), "signal": str(signal).upper(), "signalled": sent,
                "abort_if_stuck": bool(call)}

    def _unstick_opencode_tool(self, harness: Harness, pid: int, call: str,
                               grace: float = OPENCODE_UNSTICK_GRACE) -> None:
        """OpenCode does not notice a tool command killed from outside: its turn
        stays "running" forever. Once the process is gone and the tool still runs,
        stop the turn through its API (as ESC would), so the agent is usable again."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and os.path.exists(f"/proc/{pid}"):
            time.sleep(0.2)
        if os.path.exists(f"/proc/{pid}"):
            return  # still alive (e.g. it ignored Ctrl-C): nothing to unstick
        time.sleep(grace)
        running = self._opencode_running(harness)
        if not any(info.get("call") == call for info in running.values()):
            return  # OpenCode noticed it after all
        try:
            harness.link.abort(harness.conversation_id)
        except Exception:  # noqa: BLE001
            return
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("process_signal", {"aborted_turn": call})

    def new_session(self, harness: Harness, timeout: float = 20.0) -> dict[str, Any]:
        """Make the agent's TUI start a clean conversation (/clear, /new).

        Waits up to ``timeout`` for the agent to be idle, then refuses
        (HarnessError): nothing is queued.
        """
        commands = harness.new_session_commands
        if not commands:
            raise HarnessError(harness.agent_id, f"{harness.kind} agents have no new-session command")
        previous = harness.conversation_id
        harness.send(commands[0], timeout=timeout)
        deadline = time.monotonic() + timeout
        confirmed = False
        while time.monotonic() < deadline:
            if harness.uses_hooks:  # the SessionStart hook reports the new id
                confirmed = harness.conversation_id not in (None, previous)
            else:
                on_home = getattr(harness, "on_home_screen", None)
                confirmed = bool(on_home and on_home())
            if confirmed:
                break
            time.sleep(0.2)
        return {
            "agent": self.agent_summary(harness), "command": commands[0],
            "previous": previous, "conversation_id": harness.conversation_id,
            "confirmed": confirmed,
        }

    def new_session_as(
        self, sender: str, token: str | None, target: str, timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Agent-to-agent: clear a teammate's conversation (Team boundary enforced)."""
        sender_harness = self.agents.resolve(sender)
        self._check_token(sender_harness.agent_id, token)
        recipient = self.agents.resolve(target)
        self._authorize_team(sender_harness, recipient)
        if recipient.agent_id == sender_harness.agent_id:
            raise MessagingError("an agent cannot clear its own session this way")
        result = self.new_session(recipient, timeout=timeout)
        logger = getattr(recipient.session, "_log", None)
        if callable(logger):
            logger("new_session", {"by": sender_harness.agent_id})
        return result

    def send_message_as(
        self, sender: str, token: str | None, recipient: str, body: str
    ) -> Delivery:
        """Agent-to-agent send: identity from token + Team boundary enforced."""
        sender_harness = self.agents.resolve(sender)
        self._check_token(sender_harness.agent_id, token)
        return self.messaging.send(
            sender_harness.agent_id, recipient, body,
            authorize=self._authorize_team, prefix_sender=True,
            # Agents are told not to wait for answers: when the recipient is busy, hand over to the
            # bounded retry queue quickly instead of blocking the sender's turn for up to 30 s.
            wait=AGENT_SEND_WAIT,
        )

    def register_agent(self, harness: Harness) -> None:
        self.agents.add(harness)

    def get_agent(self, target: str) -> Harness:
        return self.agents.resolve(target)

    def _team_or_none(self, team: str | None) -> Team | None:
        if not team:
            return None
        try:
            return self.teams.get(team)
        except Exception:  # noqa: BLE001 - unknown team: the caller reports it later
            return None

    def _agent_host_state(self, harness: Harness) -> str | None:
        """``ok`` / ``unreachable`` / ``reconnecting`` for a remote agent (None = local).

        Passive: derived from the last SSH outcome the backend saw and the tunnel
        supervisor, never from a new network call (the listing is polled).
        """
        host = self._agent_hosts.get(harness.agent_id)
        if not host:
            return None
        if harness.session.backend.meta().get("host_unreachable"):
            return "unreachable"
        if self.remote_links.states().get(host) == "reconnecting":
            return "reconnecting"
        return "ok"

    def host_status(self) -> list[dict[str, Any]]:
        """Configured hosts with what is *observed* about them (``unknown`` if nothing)."""
        links = self.remote_links.states()
        out: list[dict[str, Any]] = []
        for name, cfg in settings.hosts().items():
            mine = [h for h in self.agents.all() if self._agent_hosts.get(h.agent_id) == name]
            states = [self._agent_host_state(h) for h in mine]
            health = self._host_health.get(name)
            tunnel = links.get(name) if cfg.get("tunnel") else None
            if "unreachable" in states:
                state = "unreachable"
            elif tunnel == "reconnecting" or "reconnecting" in states:
                state = "reconnecting"
            elif mine:
                state = "ok"
            elif health is not None:
                state = "ok" if health[0] else "unreachable"
            else:
                state = "unknown"
            out.append({
                "name": name, "ssh": cfg["ssh"], "port": cfg["port"],
                "identity": cfg.get("identity"), "known_hosts": cfg.get("known_hosts"),
                "tmux_socket": cfg.get("tmux_socket"),
                "tunnel": bool(cfg.get("tunnel")), "tunnel_state": tunnel,
                "state": state, "agents": len(mine),
                "checked_at": health[1] if health else None,
            })
        return out

    # -- host management (UI / CLI) ------------------------------------------
    def _live_agents_on(self, name: str) -> list[str]:
        return [h.name or h.agent_id for h in self.agents.all()
                if self._agent_hosts.get(h.agent_id) == name]

    def host_set(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Add or edit a host (validated like the settings file)."""
        existing = settings.hosts()
        if name in existing:
            busy = self._live_agents_on(name)
            if busy:
                raise ValueError(
                    f"host {name!r} has agents ({', '.join(busy)}): delete them before editing it"
                )
        table = {**existing, name: {k: v for k, v in body.items() if k != "name"}}
        try:
            settings.patch({"hosts": table})
        except settings.SettingsError as exc:
            raise ValueError(str(exc)) from exc
        self.remote_links.stop(name)  # the next agent re-opens it with the new config
        return next(h for h in self.host_status() if h["name"] == name)

    def host_remove(self, name: str) -> dict[str, Any]:
        existing = settings.hosts()
        if name not in existing:
            raise ValueError(f"unknown host {name!r}")
        busy = self._live_agents_on(name)
        if busy:
            raise ValueError(f"host {name!r} has agents ({', '.join(busy)}): delete them first")
        existing.pop(name)
        settings.patch({"hosts": existing})
        self.remote_links.stop(name)
        self._host_health.pop(name, None)
        return {"removed": name}

    def host_test(self, name: str) -> dict[str, Any]:
        """One short SSH round trip: can we connect, and what is installed there?"""
        cfg = settings.host(name)
        script = ('echo crewhall-ok; command -v tmux >/dev/null 2>&1 && echo tmux=1; '
                  'command -v crewhall >/dev/null 2>&1 && echo crewhall=1; '
                  'command -v git >/dev/null 2>&1 && echo git=1; '
                  'command -v claude >/dev/null 2>&1 && echo claude=1; '
                  'command -v opencode >/dev/null 2>&1 && echo opencode=1; exit 0')
        proc = ssh_tmux_backend.SshTmuxBackend(cfg)._ssh_run(script, timeout=20.0)
        self._host_health[name] = (proc.returncode == 0, time.time())
        if proc.returncode != 0 or "crewhall-ok" not in proc.stdout:
            err = (proc.stderr or "").strip().splitlines()
            detail = err[-1][:200] if err else f"ssh exit {proc.returncode}"
            return {"ok": False, "error": _ssh_hint(cfg, detail), "detail": detail}
        found = {line.split("=")[0] for line in proc.stdout.splitlines() if line.endswith("=1")}
        return {"ok": True, "tmux": "tmux" in found, "crewhall": "crewhall" in found,
                "git": "git" in found, "claude": "claude" in found,
                "opencode": "opencode" in found}

    def list_agents(self) -> list[dict[str, Any]]:
        return [self.agent_summary(harness) for harness in self.agents.all()]

    def send_message(self, sender: str, recipient: str, body: str) -> Delivery:
        return self.messaging.send(sender, recipient, body, prefix_sender=True)

    def message_history(
        self, *, agent: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return self.messaging.history(agent=agent, limit=limit)

    # -- request / reply ---------------------------------------------------
    REQUEST_WAIT_MAX = 900.0

    def create_request(self, sender: str, token: str | None, recipient: str, task: str, *,
                       timeout: float | None = None, wait: bool = True) -> dict[str, Any]:
        """Send a request and (optionally) wait for the correlated reply."""
        sender_h = self.agents.resolve(sender)
        self._check_token(sender_h.agent_id, token)
        recipient_h = self.agents.resolve(recipient)
        self._authorize_team(sender_h, recipient_h)
        req = self.requests.create(sender_h.agent_id, recipient_h.agent_id, task, timeout=timeout)
        body = (
            f"[request {req.request_id} from {sender_h.name or sender_h.agent_id}]\n{req.task}\n"
            f"Reply with: crewhall message reply {req.request_id} \"<your answer>\""
        )
        delivery = self.messaging.send(
            sender_h.agent_id, recipient_h.agent_id, body,
            authorize=self._authorize_team, prefix_sender=False, wait=AGENT_SEND_WAIT,
        )
        if delivery.acknowledged_at or delivery.delivered:
            self.requests.accept(req.request_id)
        if not (delivery.delivered or delivery.queued):
            self.requests.fail(req.request_id, delivery.error or "not delivered")
            return {"request": self.requests.get(req.request_id).to_dict(),
                    "delivery": delivery.to_dict()}
        if not wait:
            return {"request": self.requests.get(req.request_id).to_dict(),
                    "delivery": delivery.to_dict()}
        return {"request": self._await_reply(req.request_id),
                "delivery": delivery.to_dict()}

    def _await_reply(self, request_id: str) -> dict[str, Any]:
        while True:
            req = self.requests.get(request_id)
            if req is None:
                raise RequestError(f"unknown request {request_id!r}")
            if req.state not in OPEN_STATES:
                return req.to_dict()
            remaining = req.deadline - time.time()
            if remaining <= 0:
                return req.to_dict()  # sweep marked it expired
            time.sleep(min(0.25, max(0.02, remaining)))

    def reply_to_request(self, agent: str, token: str | None, request_id: str,
                         body: str) -> dict[str, Any]:
        harness = self.agents.resolve(agent)
        self._check_token(harness.agent_id, token)
        req = self.requests.reply(request_id, harness.agent_id, body)
        self.messaging.send(
            harness.agent_id, req.sender, f"[reply {request_id}] {req.reply}",
            authorize=self._authorize_team, prefix_sender=False, wait=AGENT_SEND_WAIT,
        )
        return req.to_dict()

    def cancel_request(self, agent: str, token: str | None, request_id: str) -> dict[str, Any]:
        harness = self.agents.resolve(agent)
        self._check_token(harness.agent_id, token)
        return self.requests.cancel(request_id, harness.agent_id).to_dict()

    def list_requests(self, *, open_only: bool = False, agent: str | None = None,
                      team_ids: set[str] | None = None) -> list[dict[str, Any]]:
        return self.requests.list(open_only=open_only, agent=agent, team_ids=team_ids)

    def _verify_remote_dir(self, host: str, path: str) -> None:
        """Confirm over SSH that ``path`` is an accessible directory on ``host``."""
        from .team import TeamError

        try:
            cfg = settings.host(host)
        except settings.SettingsError as exc:
            raise TeamError(str(exc)) from exc
        proc = ssh_tmux_backend.SshTmuxBackend(cfg)._ssh_run(
            'sh -c \'[ -d "$1" ] && [ -x "$1" ]\' sh ' + _shq(path), timeout=20.0
        )
        if proc.returncode == 255:
            raise TeamError(f"host {host} is unreachable: cannot confirm the workspace {path}")
        if proc.returncode != 0:
            raise TeamError(f"workspace does not exist or is not accessible on {host}: {path}")

    def create_team(
        self, name: str, agent_ids: list[str] | tuple[str, ...], **kwargs: Any
    ) -> Team:
        if kwargs.get("host"):
            settings.host(kwargs["host"])  # an unknown host is rejected up front
        team = self.teams.create(name, agent_ids, **kwargs)
        self._persist()
        return team

    def team_up(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Idempotently create a team and its agents from a parsed team spec."""
        name = spec["name"]
        try:
            team = self.teams.get(name)
            created_team = False
        except Exception:  # noqa: BLE001 (TeamNotFound)
            team = self.create_team(
                name, [], workspace=spec.get("workspace"),
                workspace_mode=spec.get("workspace_mode"), host=spec.get("host"),
            )
            created_team = True
        created: list[str] = []
        existing: list[str] = []
        for entry in spec["agents"]:
            agent_name = entry["name"]
            if any(h.name == agent_name for h in self.agents.all()):
                existing.append(agent_name)
                try:
                    self.add_team_member(team.team_id, agent_name)
                except Exception as exc:  # noqa: BLE001
                    if "already a member" not in str(exc):
                        raise  # e.g. an existing agent on another host than a remote team
                continue
            self.create_agent(
                entry.get("kind") or "opencode",
                name=agent_name,
                backend=entry.get("backend"),
                cwd=entry.get("cwd"),
                team=team.team_id,
                args=entry.get("args"),
                host=entry.get("host"),
            )
            created.append(agent_name)
        return {
            "team": self.team_info(team.team_id),
            "team_created": created_team,
            "created": created,
            "existing": existing,
        }

    def set_team_workspace(self, team: str, workspace: str | None, **kwargs: Any) -> Team:
        if kwargs.get("host"):
            settings.host(kwargs["host"])
        updated = self.teams.set_workspace(team, workspace, **kwargs)
        self._persist()
        return updated

    def get_team(self, target: str) -> Team:
        return self.teams.get(target)

    def list_teams(self) -> list[dict[str, Any]]:
        return [self.teams.info(team) for team in self.teams.list()]

    def live_team_defs(self) -> list[dict[str, Any]]:
        """Running teams as rebuildable definitions (for configuration bundles)."""
        out = []
        for team in self.teams.list():
            agents = []
            for agent_id in team.agent_ids:
                try:
                    h = self.agents.resolve(agent_id)
                except Exception:  # noqa: BLE001 - a missing member is not exported
                    continue
                info = h.info()
                agents.append({"name": info.name or info.agent_id, "kind": info.kind, "backend": info.backend,
                               "cwd": info.cwd, "args": list(self._agent_args.get(agent_id, [])),
                               "host": self._agent_hosts.get(agent_id)})
            out.append({"name": team.name, "workspace": team.workspace,
                        "workspace_mode": getattr(team, "workspace_mode", None),
                        "host": team.host, "agents": agents})
        return out

    def team_info(self, target: str) -> dict[str, Any]:
        return self.teams.info(self.teams.get(target))

    def team_members(self, target: str) -> list[dict[str, Any]]:
        return [info.to_dict() for info in self.teams.members(self.teams.get(target))]

    def remove_team(self, target: str) -> Team:
        team = self.teams.remove(target)
        self._persist()
        return team

    def add_team_member(self, team: str, agent: str) -> Team:
        result = self.teams.add_member(team, agent)
        self._persist()
        return result

    def remove_team_member(self, team: str, agent: str) -> Team:
        result = self.teams.remove_member(team, agent)
        self._persist()
        return result

    def agent_summary(self, harness: Harness) -> dict[str, Any]:
        summary = harness.info().to_dict()
        summary["args"] = list(self._agent_args.get(harness.agent_id, []))
        # True when a full conversation can be read for this agent (it may
        # still be empty before the first message).
        summary["history"] = bool(harness.supports_history)
        summary["interactions"] = [
            i.to_dict() for i in self.interactions.pending(harness.agent_id)
        ]
        # Best-effort usage from what the TUI shows; absent fields stay n/d.
        summary["usage"] = parse_usage(harness)
        summary["activity"] = self._activity(harness, summary)
        wt = self._worktrees.get(harness.agent_id)
        if wt:
            summary["worktree"] = {"branch": wt.get("branch"), "path": wt.get("path")}
        warning = self._worktree_warnings.get(harness.agent_id)
        if warning:
            summary["worktree_warning"] = warning
        host = self._agent_hosts.get(harness.agent_id)
        if host:
            summary["host"] = host
            summary["host_state"] = self._agent_host_state(harness)
        return summary

    _ACTIVITY_TTL = 1.5

    def _activity(self, harness: Harness, summary: dict[str, Any]) -> dict[str, Any]:
        """What the agent is doing now + its model; cached briefly (polled often)."""
        cache = self.__dict__.setdefault("_activity_cache", {})
        key = (harness.agent_id, summary["state"], len(summary["interactions"]))
        hit = cache.get(harness.agent_id)
        now = time.monotonic()
        if hit and hit[0] == key and now - hit[1] < self._ACTIVITY_TTL:
            return hit[2]
        state = summary["state"]
        snap: dict[str, Any] = {}
        shells = 0
        try:
            if state != "starting":  # the model is still known after the agent exited
                snap = harness.activity_snapshot()
            if state in ("ready", "waiting_input"):
                snap["tool"] = None
                shells = len(procs.agent_processes(self._agent_root(harness))["shells"])
        except Exception:  # noqa: BLE001 - best effort, never break the listing
            snap = snap or {}
        out = act.classify(state, asking=len(summary["interactions"]), tool=snap.get("tool"), shells=shells)
        model = snap.get("model")
        if not model and state in ("ready", "waiting_input", "working"):
            try:  # no message yet: the TUI shows its model on screen
                model = harness.model_from_screen(harness.capture())
            except Exception:  # noqa: BLE001
                model = None
        out["model"] = (model or (hit[2].get("model") if hit else None)
                        or act.model_from_args(self._agent_args.get(harness.agent_id)))
        cache[harness.agent_id] = (key, now, out)
        return out

    def remove_agent(self, target: str, force: bool = False) -> AgentInfo:
        harness = self.agents.resolve(target)
        agent_id = harness.agent_id
        info = harness.info()
        # Archive before forgetting: keep a record with a summary of what it did
        # (the conversation itself is never deleted, only unregistered).
        try:
            teams = [t.name for t in self.teams.list() if agent_id in t.agent_ids]
            self.archive_agent(harness, teams)
        except Exception:  # noqa: BLE001 — archiving must not block removal
            pass
        harness.stop(force=force)
        self.agents.remove(agent_id)
        try:  # do not leave a dead session behind (it keeps the name ambiguous)
            self.registry.remove(agent_id)
        except Exception:  # noqa: BLE001
            pass
        self._agent_args.pop(agent_id, None)
        self._mcp_cleanup(agent_id)
        self._cleanup_worktree(agent_id, force=force)
        self.interactions.forget_agent(agent_id)
        self._retired_conversations.pop(agent_id, None)
        stop = self._bridges.pop(agent_id, None)
        if stop:
            stop.set()
        # Detach from every Team so it does not linger as a "(missing)" member.
        self.teams.forget_agent(agent_id)
        # Contain Bun/OpenCode temp leaks when an agent goes away.
        paths.cleanup_tmpdir()
        self._persist()
        return info

    def archive_agent(self, harness: Harness, teams: list[str]) -> dict[str, Any]:
        from . import archive

        messages = self.messaging.history(agent=harness.agent_id, limit=200)
        return archive.archive_agent(
            agent_id=harness.agent_id, name=harness.name, kind=harness.kind,
            cwd=harness.session.spec.cwd, teams=teams,
            conversation_id=harness.conversation_id, messages=messages,
        )

    def list_archived_agents(self) -> list[dict[str, Any]]:
        from . import archive

        return archive.list_archived()

    def read_archived_agent(self, agent_id: str) -> dict[str, Any] | None:
        from . import archive

        return archive.read_archived(agent_id)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for session in self.registry.all():
            session.poll()
            out.append(session.info().to_dict())
        return out

    def prune(self) -> int:
        removed = 0
        for session in self.registry.all():
            session.poll()
            if not session.status.alive:
                self.registry.remove(session.session_id)
                session.backend.close()
                removed += 1
        return removed

    def shutdown(self) -> None:
        self.remote_links.stop_all()
        for agent_id in list(self._mcp_files):
            self._mcp_cleanup(agent_id)
        for harness in self.agents.all():
            try:
                harness.stop()
            except Exception:
                pass
        for session in self.registry.all():
            try:
                session.close()
            except Exception:
                pass
        try:
            paths.cleanup_tmpdir()
        except Exception:
            pass

    def summary(self, session: InteractiveSession) -> dict[str, Any]:
        session.poll()
        return session.info().to_dict()
