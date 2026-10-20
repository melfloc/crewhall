"""Controller services: persistence (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import os
import secrets
import threading
import time
from typing import Any
from .. import hooks, settings, terminals as termlib
from ..backends import HostUnreachable, ssh_tmux as ssh_tmux_backend, tmux as tmux_backend
from ..control_files import ensure_managed_section
from ..harness import Harness, get_harness
from ..session import InteractiveSession
from ..types import SessionSpec, Status, parse_agent_args
from ..controller import SessionNotFound, log


class ServicesPersistence:

    def _persist(self) -> None:
        if self._store is not None:
            try:
                self._store.save(self._store.snapshot(self))
            except Exception:  # noqa: BLE001 - persistence must never break the caller
                log.warning("could not persist state", exc_info=True)
        self._persist_tokens()

    def _persist_tokens(self) -> None:
        """Keep the known agents' tokens on disk (and only theirs)."""
        if self._token_store is None:
            return
        known = {h.agent_id for h in self.agents.all()}
        with self._locks_guard:
            tokens = {a: t for a, t in self._agent_tokens.items() if a in known and t}
        self._token_store.save(tokens)

    def restore(self) -> None:
        """Load persisted Teams and agent definitions (not processes).

        Agents are reconstructed as EXITED logical entities; they are *not*
        started automatically. A later send_message or explicit start activates
        them (same agent_id), preserving identity and membership.
        """
        if self._store is None:
            return
        data = self._store.load()
        if self._token_store is not None:
            # Before the agents: a tmux agent that survived the restart still
            # carries its old token, so it must stay valid (Fase A1).
            with self._locks_guard:
                for agent_id, token in self._token_store.load().items():
                    self._agent_tokens.setdefault(agent_id, token)
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
            except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                continue
        for agent in data.get("agents", []):
            try:
                self._restore_agent(agent)
            except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                continue
        try:
            self.requests.load(data.get("requests", []))
        except Exception:  # noqa: BLE001 - a bad row must not stop the daemon
            log.debug("ignored exception in a boundary guard", exc_info=True)
        # Terminals persist like agents: re-create any that is not already
        # adopted (its tmux session may have survived a crash, or not).
        for term in data.get("terminals", []):
            try:
                self._restore_terminal(term)
            except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                continue
        # Re-apply memberships after all agents exist.
        for team in data.get("teams", []):
            for agent_id in team.get("agent_ids", []):
                try:
                    self.teams.add_member(team["team_id"], agent_id)
                except Exception:  # noqa: BLE001 - a stale membership is skipped
                    log.debug("could not restore membership of %s in %s",
                              agent_id, team.get("team_id"), exc_info=True)
        # Leads reference members, so they can only be restored once membership
        # exists. Old state has no "leads" key: that just means no leads.
        for team in data.get("teams", []):
            leads = team.get("leads") or []
            if not leads:
                continue
            try:
                self.teams.set_leads(team["team_id"], list(leads))
            except Exception:  # noqa: BLE001 - a stale lead is skipped
                log.debug("could not restore leads of %s", team.get("team_id"), exc_info=True)
        # Resume messages that were still queued when the daemon stopped. The
        # recipients exist as EXITED entities; the queue worker revives them.
        if self._pending_store is not None:
            try:
                self.messaging.load(self._pending_store.load())
            except Exception:  # noqa: BLE001 - a bad queue must not stop startup
                log.exception("could not resume the pending message queue")
        # Restore the message history so the crew strip / Message flow survive
        # the restart (Fase A2).
        if self._history_store is not None:
            try:
                self.messaging.load_history()
            except Exception:  # noqa: BLE001 - a bad history must not stop startup
                log.exception("could not restore the message history")

    def _restore_agent(self, agent: dict[str, Any]) -> None:
        agent_id = agent["agent_id"]
        if any(h.agent_id == agent_id for h in self.agents.all()):
            return
        harness_cls = get_harness(agent["kind"])
        extra_args = parse_agent_args(agent.get("args"))
        self._agent_args[agent_id] = extra_args
        host = agent.get("host")
        self._agent_hosts[agent_id] = host
        # A process that survived the restart was already adopted (see
        # ``_adopt_tmux``). Reuse that live session so the agent keeps running
        # instead of being reconstructed as EXITED and losing its work.
        session = self._live_adopted(agent_id)
        if session is not None:
            self._overlay_persisted(session, agent)
        else:
            spec = SessionSpec(
                command=self._launch_command(agent["kind"], extra_args, host=host, enforce=False),
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
        self._attach_version(harness, agent["kind"], host)
        self.agents.add(harness)
        self._base_env[agent_id] = {
            key: os.environ[key] for key in (agent.get("env_keys") or [])
            if key in os.environ
        }
        if isinstance(agent.get("session_config"), dict):
            self._v2_config[agent_id] = dict(agent["session_config"])
        if isinstance(agent.get("launch"), dict):
            self._agent_launch[agent_id] = dict(agent["launch"])
        self._agent_tokens.setdefault(agent_id, secrets.token_hex(16))
        link_info = agent.get("opencode")
        if isinstance(link_info, dict) and link_info:
            self._opencode_links[agent_id] = link_info
            if session is not None:  # a live tmux session survived the restart
                self._reattach_opencode_link(harness, link_info, session.spec.cwd or os.getcwd())
        elif (agent.get("kind") == "opencode" and session is not None
              and getattr(session, "_adopted", False)):
            self._backfill_opencode_legacy(harness, session.spec.cwd)

    def _session_by_id(self, session_id: str) -> InteractiveSession | None:
        """Exact lookup, never a name/prefix match (ids must be unambiguous)."""
        for session in self.registry.all():
            if session.session_id == session_id:
                return session
        return None

    def _agent_by_id(self, agent_id: str) -> Harness | None:
        for harness in self.agents.all():
            if harness.agent_id == agent_id:
                return harness
        return None

    def _live_adopted(self, session_id: str) -> InteractiveSession | None:
        """A live adopted session for ``session_id``, if one survived a restart."""
        session = self._session_by_id(session_id)
        if session is not None and (
            getattr(session, "_adopted", False) or session.status.alive
        ):
            return session
        return None

    def _overlay_persisted(self, session: InteractiveSession, agent: dict[str, Any]) -> None:
        """Keep the persisted control-plane metadata on an adopted live session."""
        spec = session.spec
        spec.cwd = agent.get("cwd") or spec.cwd
        spec.name = agent.get("name")
        if agent.get("cols"):
            spec.cols = int(agent["cols"])
        if agent.get("rows"):
            spec.rows = int(agent["rows"])
        session.name = spec.name

    def _register_adopted(self, session: InteractiveSession) -> None:
        """Register a session adopted from a surviving tmux server.

        When the id already belongs to an agent reconstructed as EXITED (or one
        being restored), the live session replaces the dead placeholder and the
        harness is pointed at it, so a daemon restart — or a remote host coming
        back — never silently loses a running agent.
        """
        existing = self._session_by_id(session.session_id)
        if existing is not None and getattr(existing, "_adopted", False):
            return  # already adopted; leave it alone
        self.registry.add(session)
        harness = self._agent_by_id(session.session_id)
        if harness is None:
            return
        if getattr(harness.session, "_adopted", False) or harness.session.status.alive:
            return
        old = harness.session.spec
        spec = session.spec
        spec.cwd = spec.cwd or old.cwd
        spec.name = old.name
        if old.cols:
            spec.cols = old.cols
        if old.rows:
            spec.rows = old.rows
        session.name = old.name
        harness.session = session

    def _restore_terminal(self, term: dict[str, Any]) -> None:
        """Re-create a persisted terminal (fresh shell) with the same id."""
        sid = term.get("session_id")
        if not isinstance(sid, str) or not termlib.TERMINAL_ID_RE.match(sid):
            return
        try:
            self.registry.resolve(sid)
            return  # already adopted from a surviving tmux session
        except SessionNotFound:
            pass
        host = term.get("host")
        if host and host not in settings.hosts():
            return  # the host was removed
        shell = term.get("shell")
        if host:
            shell = None  # remote terminals use the remote login shell
        elif shell and not os.path.isfile(shell):
            shell = None
        cwd = term.get("cwd")
        if host is None and cwd and not os.path.isdir(cwd):
            cwd = None
        spec = SessionSpec(
            command=[shell] if shell else "",
            cwd=cwd,
            cols=int(term.get("cols") or 120),
            rows=int(term.get("rows") or 32),
            host=host,
            kind=termlib.KIND_TERMINAL,
            readonly=bool(term.get("readonly")),
            title=term.get("title"),
            owner=term.get("owner"),
        )
        session = InteractiveSession(self._backend_for(None, host), spec, session_id=sid)
        session.created_at = float(term.get("created_at") or session.created_at)
        session.start()
        self.registry.add(session)

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
            prior = harness.conversation_id
            # A non-forced revive (Web UI "Start") continues the agent's own
            # conversation instead of silently creating a new one.
            resume_id = prior if (prior and kind in ("claude", "opencode", "codex")
                                  and not self._agent_hosts.get(agent_id)) else None
            conversation_id = resume_id or self._new_conversation_id(
                kind, self._agent_args.get(agent_id, [])
            )
            spec = harness.session.spec
            backend_name = harness.session.backend.name
            host = self._agent_hosts.get(agent_id)
            link, server_proc = (None, None) if host else self._reopen_opencode_link(
                agent_id, kind, self._agent_args.get(agent_id, []), spec.cwd,
                identity=self._build_agent_env(
                    agent_id, harness.name or agent_id,
                    self._team_names_of(agent_id), None, host))
            v2_session: str | None = None
            if link is not None and getattr(link, "api", "v1") == "v2" and not host:
                # Reopen the agent's own session: creating a new one on every
                # revive piles abandoned sessions into the TUI's session list.
                if resume_id and not self._v2_session_exists(link, resume_id):
                    self._record_v2_unresumable(
                        agent_id, resume_id,
                        from_v1=(self._opencode_links.get(agent_id) or {}).get("api") == "v1")
                    resume_id = None
                    conversation_id = None
                report = self._configure_v2_session(
                    agent_id, kind, link, self._agent_args.get(agent_id, []),
                    session_id=resume_id,
                )
                v2_session = report.get("session_id") or conversation_id
                if v2_session:
                    conversation_id = v2_session
            try:
                harness.session.backend.close()
            except Exception:  # noqa: BLE001 - the session is being rebuilt anyway
                log.debug("backend close failed for %s", agent_id, exc_info=True)
            teams = self._team_names_of(agent_id)
            # Rebuild from the user's original env (not the identity-augmented
            # one) to avoid duplicated/derived keys across restarts.
            base_env = dict(self._base_env.get(agent_id) or {})
            env = self._build_agent_env(agent_id, harness.name or agent_id, teams, base_env, host)
            if kind == "opencode" and not host:
                self._isolate_opencode_tabs(
                    env, agent_id, link, api=self._opencode_api(kind))
            if link:
                env["OPENCODE_SERVER_PASSWORD"] = link.password
            if server_proc is not None:
                self._opencode_servers[agent_id] = server_proc
            if link is not None:
                self._opencode_links[agent_id] = {
                    "port": link.port, "password": link.password, "api": link.api,
                }
            new_spec = SessionSpec(
                command=self._launch_command(
                    kind,
                    self._v2_serve_fallback(
                        kind, self._agent_args.get(agent_id, []), link, agent_id),
                    conversation_id,
                    link.port if link else None, host=host, enforce=False,
                    resume=v2_session or resume_id,
                    server_url=(f"http://127.0.0.1:{link.port}"
                                if link is not None and link.api == "v2" else None),
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
            self._attach_version(revived, kind, host)
            self._persist()
            self.agents.add(revived)
            if not host:
                self._ensure_control_file(spec.cwd, kind)
            return revived

    def _adopt_tmux(self) -> None:
        self._adopt_local_tmux()
        self._start_remote_adopter()

    def _adopt_local_tmux(self) -> None:
        for meta in tmux_backend.existing_sessions():
            if meta.get("backend") != "tmux":
                continue
            sid = meta["session_id"]
            existing = self._session_by_id(sid)
            if existing is not None and getattr(existing, "_adopted", False):
                continue  # already adopted; never re-read the same pane twice
            created = meta.get("created_at")
            try:
                created_f = float(created) if created else None
            except ValueError:
                created_f = None
            spec = SessionSpec(
                command=meta.get("command") or sid,
                kind=meta.get("kind") or "session",
                readonly=(meta.get("readonly") == "1"),
                title=meta.get("title") or None,
                owner=meta.get("owner") or None,
            )
            try:
                session = InteractiveSession.adopt(
                    tmux_backend.TmuxBackend(), spec, sid, created_at=created_f
                )
            except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                continue
            self._register_adopted(session)

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
                except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                    failed = True
                    log.exception("could not adopt sessions on ssh host %s", name)
            delay = self.REMOTE_ADOPT_RETRY if failed else self.REMOTE_ADOPT_IDLE

    def _adopt_remote_host(self, name: str, cfg: dict[str, Any]) -> None:
        for meta in ssh_tmux_backend.existing_sessions(cfg):
            if meta.get("backend") != "tmux":
                continue
            sid = meta["session_id"]
            existing = self._session_by_id(sid)
            if existing is not None and getattr(existing, "_adopted", False):
                continue  # already adopted; never re-read the same pane twice
            created = meta.get("created_at")
            try:
                created_f = float(created) if created else None
            except ValueError:
                created_f = None
            spec = SessionSpec(
                command=meta.get("command") or sid,
                host=name,
                kind=meta.get("kind") or "session",
                readonly=(meta.get("readonly") == "1"),
                title=meta.get("title") or None,
                owner=meta.get("owner") or None,
            )
            try:
                session = InteractiveSession.adopt(
                    ssh_tmux_backend.SshTmuxBackend(cfg), spec, sid, created_at=created_f
                )
            except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
                continue
            self._register_adopted(session)

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
        launch = self._agent_launch.get(harness.agent_id)
        if launch is not None:
            launch["session"] = conversation_id
        self._apply_pending_v2_config(harness, conversation_id)
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("conversation", {"conversation_id": conversation_id})
        self._persist()
        return True

    def _wire_harness(self, harness: Harness) -> None:
        harness.on_new_session = lambda previous: self._conversation_left(harness, previous)

    def _ensure_control_file(self, cwd: str | None, kind: str | None = None) -> None:
        if not cwd:
            return
        try:
            ensure_managed_section(cwd, kind=kind)
        except Exception:  # noqa: BLE001 - never block an agent over a doc file
            log.debug("ignored exception in a boundary guard", exc_info=True)

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

    def _apply_hook(self, harness: Harness, event: str) -> dict[str, Any]:
        now = time.time()
        self._hook_events[harness.agent_id] = {"event": event, "at": now}
        harness.on_hook(event, now)
        logger = getattr(harness.session, "_log", None)
        if callable(logger):
            logger("hook", {"event": event})
        return {"agent_id": harness.agent_id, "event": event, "at": now}

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
