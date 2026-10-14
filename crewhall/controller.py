from __future__ import annotations

import collections
import logging
import threading
import re
from typing import Any

from . import settings
from .harness import Harness
from . import interactions as ix
from .messaging import Messaging
from .requests import RequestBoard
from .persistence import AgentTokenStore, HistoryStore, PendingQueueStore, StateStore
from .remote_link import RemoteLinks
from .session import InteractiveSession
from .team import TeamRegistry
from .workers import WorkerRegistry

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



from .services.agents import ServicesAgents  # noqa: E402
from .services.messaging import ServicesMessaging  # noqa: E402
from .services.processes import ServicesProcesses  # noqa: E402
from .services.status import ServicesStatus  # noqa: E402
from .services.control import ServicesControl  # noqa: E402
from .services.terminals import ServicesTerminals  # noqa: E402
from .services.chat_bridge import ServicesChatBridge  # noqa: E402
from .services.persistence import ServicesPersistence  # noqa: E402
from .services.teams import ServicesTeams  # noqa: E402
from .services.worktrees import ServicesWorktrees  # noqa: E402


class Controller(ServicesAgents, ServicesMessaging, ServicesProcesses, ServicesStatus, ServicesControl, ServicesTerminals, ServicesWorktrees, ServicesTeams, ServicesChatBridge, ServicesPersistence):

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
        # Version gate: per-agent CLI version report (kind/version/supported…).
        self._agent_versions: dict[str, dict[str, Any] | None] = {}
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
        # OpenCode v2: the private `serve` process per agent (killed with it).
        self._opencode_servers: dict[str, Any] = {}
        # Persisted OpenCode link per agent: {"port", "password", "api"} so a
        # daemon restart can bring the server back on the same port (A1).
        self._opencode_links: dict[str, dict[str, Any]] = {}
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
        # Throttle for the defensive OpenCode interaction poll (see agent_summary).
        self._interaction_sync: dict[str, float] = {}
        # Launch Claude with a known --session-id so its full history can be read.
        self.track_conversations = settings.get("agents.conversations")
        self._agent_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Per-agent recent send timestamps (anti-loop rate limit, D2).
        self._send_times: dict[str, collections.deque[float]] = {}
        # Recent forced restarts per agent (the brake in ServicesControl).
        self._restart_times: dict[str, collections.deque[float]] = {}
        self._store = StateStore() if persist else None
        self._pending_store = PendingQueueStore() if persist else None
        self._token_store = AgentTokenStore() if persist else None
        history_store = None
        history_limit = 1000
        if persist:
            entries = int(settings.get("messaging.history_entries"))
            days = int(settings.get("messaging.history_days"))
            history_store = HistoryStore(max_entries=entries, max_age_days=days or None)
            history_limit = entries or 1000
        self._history_store = history_store
        # Every background worker (message queues, ack watchers, janitors…) is
        # supervised here so a crash is recorded and the thread is relaunched.
        self.workers = WorkerRegistry()
        self.messaging = Messaging(
            self.agents, revive=self._revive_agent, store=self._pending_store,
            history_limit=history_limit, history_store=history_store,
            workers=self.workers,
        )
        self.requests = RequestBoard(
            max_open_per_agent=int(settings.get("requests.max_open_per_agent")),
            max_depth=int(settings.get("requests.max_depth")),
            default_timeout=float(settings.get("requests.default_timeout_s")),
            max_body=int(settings.get("requests.max_body_chars")),
        )
        if adopt:
            self._adopt_tmux()
    _SESSION_FLAGS = ("--session-id", "--resume", "-r", "--continue", "-c")
    _CONVERSATION_ID = {
        "claude": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
        "opencode": re.compile(r"^ses_[A-Za-z0-9]+$"),
    }
    REMOTE_ADOPT_RETRY = 30.0
    REMOTE_ADOPT_IDLE = 120.0
    REQUEST_WAIT_MAX = 900.0
    _ACTIVITY_TTL = 1.5
    SURVIVABLE_BACKENDS = ("tmux", "ssh-tmux")

