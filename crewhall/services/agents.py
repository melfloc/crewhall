"""Controller services: agents (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import os
import secrets
import threading
import time
import uuid
from typing import Any
from .. import activity as act, brand, hooks, notices, paths, processes as procs, settings
from ..backends import get_backend
from ..harness import AgentInfo, Harness, HarnessError, get_harness, versions
from ..harness.versions import UnsupportedVersionError
from ..messaging import MessagingError
from ..opencode_link import OpenCodeLink, free_port
from ..session import InteractiveSession
from ..terminals import is_terminal
from ..types import SessionSpec, new_session_id, parse_agent_args
from ..usage import parse_usage
from ..controller import _OPENCODE_INTERACTION_EVENTS, log


class ServicesAgents:

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
            values["SOCKET"] = self.remote_links.ensure(settings.host(host)).remote_socket or ""
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

    def _new_conversation_id(self, kind: str, extra_args: list[str]) -> str | None:
        """A fresh conversation id, unless disabled or the user chose a session."""
        if not self.track_conversations or not get_harness(kind).uses_hooks:
            return None
        if any(a in self._SESSION_FLAGS or a.startswith("--session-id=") for a in extra_args):
            return None
        return str(uuid.uuid4())

    def _resolve_provider_command(
        self, kind: str, host: str | None
    ) -> tuple[list[str], Any, bool]:
        """The command to launch, with version auto-selection applied.

        A remote agent runs the remote machine's own CLI (bare name, remote
        PATH): a local override path, a local fallback or a local version probe
        means nothing there, so the built-in command is returned unpinned.
        """
        if host:
            return get_harness(kind).command(), None, False
        return settings.provider_effective_command(kind)

    def _enforce_version(self, kind: str, report: Any) -> None:
        """Block launch when the CLI version is outside the validated range."""
        if report is None or not report.blocked:
            return
        if settings.get("agents.allow_unvalidated_versions"):
            log.warning("provider %s: %s (allowed by agents.allow_unvalidated_versions)",
                        kind, report.reason)
            return
        raise UnsupportedVersionError(report)

    def _attach_version(self, harness: Harness, kind: str, host: str | None) -> None:
        """Record the resolved version on the agent (for the UI and the gate)."""
        try:
            _command, report, used_fallback = self._resolve_provider_command(kind, host)
            primary = None if host else versions.evaluate(kind, settings.provider_primary_command(kind))
        except Exception:  # noqa: BLE001 - a version probe must never block a launch
            log.debug("version probe failed for %s", kind, exc_info=True)
            return
        harness.version_report = report
        harness.version_blocked = bool(
            report is not None and report.blocked
            and not settings.get("agents.allow_unvalidated_versions")
        )
        self._agent_versions[harness.agent_id] = report.to_dict() if report else None
        if used_fallback:
            log.warning("provider %s: primary CLI out of range; using compatible %s",
                        kind, " ".join(report.command))
        if primary is not None:
            self._record_version_notice(kind, primary, report, used_fallback)

    def _record_version_notice(self, kind: str, primary: Any, chosen: Any, switched: bool) -> None:
        """Explain an automatic version switch (or block) in a user-facing notice."""
        if primary is None or primary.supported is not False:
            return
        if switched and chosen is not None:
            notice = notices.add(
                key=f"switch:{kind}:{primary.text}->{chosen.text}",
                kind=kind, level="warn",
                title=f"{kind} updated to {primary.text or 'a new version'} — switched automatically",
                body=(
                    f"Your {kind} CLI is now {primary.text or 'unknown'}, outside the range this "
                    f"adapter was verified against (last verified {primary.captured}). crewhall "
                    f"detected it and automatically switched to a compatible install at "
                    f"{chosen.command[0]} ({chosen.text or 'unknown'}). The validated adapter keeps "
                    f"working; the {kind} adapter for the previous major version is now legacy until "
                    "crewhall is updated for the new one."
                ),
            )
        else:
            notice = notices.add(
                key=f"blocked:{kind}:{primary.text}",
                kind=kind, level="err",
                title=f"{kind} CLI {primary.text or 'unknown'} is not supported",
                body=(
                    f"Your {kind} CLI is outside the range this adapter was verified against "
                    f"(last verified {primary.captured}) and no compatible install was found "
                    "automatically. crewhall will not launch or drive it: install a supported "
                    "version, pin one in Settings → Providers, or set "
                    "agents.allow_unvalidated_versions to override."
                ),
            )
        if notice is None:
            return
        try:
            from .. import watch

            watch.notify(notice["title"], notice["body"])
        except Exception:  # noqa: BLE001 - a desktop notification is best effort
            log.debug("desktop notification failed", exc_info=True)

    def check_provider_versions(self) -> list[dict[str, Any]]:
        """Resolve every enabled provider; record a notice on a version change.

        Cheap (version probes are cached). Called at daemon start, after a
        settings change and on every agent launch, so a CLI update is detected
        and explained without the user having to run anything.
        """
        out: list[dict[str, Any]] = []
        for kind in settings._kinds():
            if not settings.provider_enabled(kind):
                continue
            try:
                primary = versions.evaluate(kind, settings.provider_primary_command(kind))
                argv, report, switched = settings.provider_effective_command(kind)
            except Exception:  # noqa: BLE001 - a broken provider must not hide the rest
                log.debug("version check failed for %s", kind, exc_info=True)
                continue
            self._record_version_notice(kind, primary, report, switched)
            out.append({
                "kind": kind, "primary": primary.to_dict(), "effective": report.to_dict(),
                "switched": switched, "command": argv,
            })
        return out

    def _assert_operable(self, harness: Harness) -> None:
        """Refuse to drive an agent whose CLI version is unsupported (version gate)."""
        if not getattr(harness, "version_blocked", False):
            return
        report = getattr(harness, "version_report", None)
        if report is not None:
            raise UnsupportedVersionError(report)
        raise HarnessError(harness.agent_id, "its CLI version is not supported")

    def _launch_command(
        self,
        kind: str,
        extra_args: list[str],
        conversation_id: str | None = None,
        port: int | None = None,
        mcp_args: list[str] | None = None,
        host: str | None = None,
        resume: str | None = None,
        enforce: bool = True,
    ) -> list[str]:
        cls = get_harness(kind)
        # A remote agent runs the remote machine's own CLI (bare name, remote
        # PATH): a local override path or local settings file means nothing there.
        command, report, _used = self._resolve_provider_command(kind, host)
        # ``enforce`` is False for a relaunch/restore of an *existing* agent:
        # restarting must always work (it is what moves the agent onto the
        # compatible binary), so a still-blocked command is launched and the
        # agent is simply marked blocked instead of refusing to start.
        if enforce:
            self._enforce_version(kind, report)
        if resume and not host:
            # Continue an existing conversation instead of starting a new one.
            if kind == "claude":
                base = [*command, *cls.launch_args(self._hooks_settings_path(None), None, port)]
                return [*base, "--resume", resume, *(mcp_args or []), *extra_args]
            if kind == "opencode":
                base = [*command, *cls.launch_args(self._hooks_settings_path(None), None, port)]
                return [*base, "--session", resume, *(mcp_args or []), *extra_args]
            if kind == "codex":
                return [*command, "resume", resume, *(mcp_args or []), *extra_args]
        return [
            *command,
            *cls.launch_args(self._hooks_settings_path(host), conversation_id, port),
            *(mcp_args or []),
            *extra_args,
        ]

    def _new_opencode_link(self, kind: str, extra_args: list[str]) -> OpenCodeLink | None:
        """Port + password for the agent's own server, unless the user set a port."""
        if not self.track_conversations or not get_harness(kind).uses_local_server:
            return None
        if any(a == "--port" or a.startswith("--port=") for a in extra_args):
            return None
        return OpenCodeLink(free_port(), secrets.token_urlsafe(24))

    def _mcp_for(self, kind: str, harness_cls: type[Harness]) -> tuple[list[str], str | None]:
        """MCP launch args + (optional) temp config file, when enabled."""
        if not harness_cls.mcp_supported or not settings.provider(kind).get("mcp"):
            return [], None
        path = harness_cls.mcp_config_file()
        return harness_cls.mcp_launch_args(path), path

    def _mcp_cleanup(self, agent_id: str) -> None:
        from ..mcp import config as mcp_config

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
                log.debug("ignored exception in a boundary guard", exc_info=True)
            if should_stop():
                return
            if not harness.conversation_id:  # the creation event may have been missed
                try:
                    self._discover_opencode_session(harness, link)
                except Exception:  # noqa: BLE001
                    log.debug("ignored exception in a boundary guard", exc_info=True)
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
            except Exception:  # noqa: BLE001 - best-effort: a failure here is non-fatal (B1 audit)
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
        conversation: str | None = None,
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
        conversation_workspace: str | None = None
        if conversation and not host:
            # A chat conversation shares its inputs/outputs with the agent through
            # the environment; a remote agent has no access to this host's disk.
            # Its cwd is forced into the conversation's own workspace so the chat
            # is fully isolated (never the user's project nor the daemon's cwd).
            from .. import chat as _conversations

            try:
                conv_env = _conversations.env(conversation)
                env.update(conv_env)
                conversation_workspace = conv_env["CREWHALL_WORKSPACE"]
            except _conversations.ConversationError:
                pass
        conversation_id = self._new_conversation_id(kind, extra_args)
        # The OpenCode server binds the remote loopback and MCP config is a local
        # file: neither is reachable for a remote agent (state falls back to the
        # screen, honestly).
        link = None if host else self._new_opencode_link(kind, extra_args)
        mcp_args, mcp_file = ([], None) if host else self._mcp_for(kind, harness_cls)
        if name and any(h.name == name for h in self.agents.all()):
            raise ValueError(f'agent name "{name}" already exists')
        cwd = cwd if host else self.resolve_cwd(cwd, team)
        if conversation_workspace:
            # Isolation wins over any cwd/team/worktree: a chat never runs in a
            # project directory and never creates worktrees or stray files.
            cwd = conversation_workspace
            workspace_mode = "shared"
        session_id = agent_id or new_session_id()
        agent_name = name or session_id
        mode = workspace_mode
        if mode is None and team:
            try:
                t = self.teams.get(team)
                mode = getattr(t, "workspace_mode", None)
            except Exception:  # noqa: BLE001
                mode = None
        if mode == "worktree" and not conversation_workspace:
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
        self._attach_version(harness, kind, host)
        self.agents.add(harness)
        if conversation and not host:
            # A chat runs in a crewhall-owned isolated workspace, so accept the
            # agent's folder-trust dialog in the background (Claude shows one).
            def _accept() -> None:
                try:
                    harness.accept_workspace_trust()
                except Exception:  # noqa: BLE001 - best effort
                    log.debug("ignored exception in a boundary guard", exc_info=True)

            threading.Thread(target=_accept, name=f"trust-{session_id}", daemon=True).start()
        self._agent_tokens.setdefault(session_id, agent_env["CREWHALL_TOKEN"])
        if mcp_file:
            self._mcp_files[session_id] = mcp_file
        self._base_env.setdefault(session_id, dict(env or {}))
        self._agent_args[session_id] = extra_args
        self._agent_hosts[session_id] = host
        if not host and not conversation:
            # A chat conversation must not drop a CLAUDE.md/AGENTS.md anywhere:
            # its instructions travel through the environment instead.
            self._ensure_control_file(cwd, kind)
        if team:
            try:
                self.teams.add_member(team, session_id)
            except Exception:  # noqa: BLE001 - an unknown team must not block the agent
                log.debug("could not attach %s to team %s", session_id, team, exc_info=True)
        self._persist()
        if wait_ready:
            harness.start(timeout=timeout)
        return harness

    def _check_token(self, agent_id: str, token: str | None) -> None:
        if not token or self._agent_tokens.get(agent_id) != token:
            raise MessagingError("invalid agent identity/token")

    def agent_root_pids(self) -> set[int]:
        """PIDs of the live agent TUIs (the roots of their process trees).

        The daemon uses this to recognise a caller that descends from an agent
        (via ``SO_PEERCRED`` + the ``/proc`` ancestry) and lock it out of the
        control plane, whatever credentials it presents.
        """
        pids: set[int] = set()
        for harness in self.agents.all():
            try:
                if harness.session.status.alive and harness.session.pid:
                    pids.add(int(harness.session.pid))
            except Exception:  # noqa: BLE001 - a broken session must not hide the rest
                continue
        return pids

    def agent_tokens(self) -> list[str]:
        """Live agent identity tokens (the daemon matches a caller's environ)."""
        return [t for t in self._agent_tokens.values() if t]

    def register_agent(self, harness: Harness) -> None:
        self.agents.add(harness)

    def get_agent(self, target: str) -> Harness:
        return self.agents.resolve(target)

    def list_agents(self) -> list[dict[str, Any]]:
        return [self.agent_summary(harness) for harness in self.agents.all()]

    def agent_summary(self, harness: Harness) -> dict[str, Any]:
        self._maybe_sync_interactions(harness)
        summary = harness.info().to_dict()
        summary["args"] = list(self._agent_args.get(harness.agent_id, []))
        # Version gate: the CLI version the agent runs and whether it is inside
        # the range its adapter was verified against (and thus drivable).
        summary["cli"] = self._agent_versions.get(harness.agent_id)
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

    def agent_models(self, target: str) -> dict[str, Any]:
        """Model catalog the Web UI may offer for this agent, plus the current one."""
        harness = self.agents.resolve(target)
        current = (self.agent_summary(harness).get("activity") or {}).get("model")
        entries: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(value: Any, label: Any = None) -> None:
            name = str(value or "").strip()
            if not name or name in seen:
                return
            seen.add(name)
            entries.append({"value": name, "label": str(label or name)})

        for name in settings.provider_models(harness.kind):
            add(name)
        try:
            for item in harness.available_models():
                if isinstance(item, dict):
                    add(item.get("value"), item.get("label"))
                else:
                    add(item)
        except Exception:  # noqa: BLE001 - discovery is best effort
            log.debug("ignored exception in a boundary guard", exc_info=True)
        # The TUI often shows "<name> <provider>"; map it back to a catalog value
        # so the selector marks the real entry instead of inventing a new one.
        selected = current
        if current:
            for entry in entries:
                if current == entry["value"] or current.startswith(entry["value"] + " "):
                    selected = entry["value"]
                    break
        return {"kind": harness.kind, "current": selected,
                "mode": harness.model_switch_mode, "models": entries}

    def set_agent_model(self, target: str, model: str | None = None) -> dict[str, Any]:
        """Change a running agent's model: a direct command or its own picker."""
        harness = self.agents.resolve(target)
        name = (model or "").strip() or None
        if harness.model_switch_mode == "direct" and not name:
            raise HarnessError(harness.agent_id, "a model name is required")
        harness.set_model(name)
        return self.agent_summary(harness)

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
            log.debug("ignored exception in a boundary guard", exc_info=True)
        harness.stop(force=force)
        self.agents.remove(agent_id)
        try:  # do not leave a dead session behind (it keeps the name ambiguous)
            self.registry.remove(agent_id)
        except Exception:  # noqa: BLE001
            log.debug("ignored exception in a boundary guard", exc_info=True)
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
        from .. import archive

        messages = self.messaging.history(agent=harness.agent_id, limit=200)
        return archive.archive_agent(
            agent_id=harness.agent_id, name=harness.name, kind=harness.kind,
            cwd=harness.session.spec.cwd, teams=teams,
            conversation_id=harness.conversation_id, messages=messages,
        )

    def list_archived_agents(self) -> list[dict[str, Any]]:
        from .. import archive

        return archive.list_archived()

    def read_archived_agent(self, agent_id: str) -> dict[str, Any] | None:
        from .. import archive

        return archive.read_archived(agent_id)

    def list(self, kind: str = "session") -> list[dict[str, Any]]:
        if kind not in ("session", "terminal", "all"):
            kind = "session"
        out = []
        for session in self.registry.all():
            session.poll()
            info = session.info()
            terminal = is_terminal(info)
            if kind == "terminal" and not terminal:
                continue
            if kind == "session" and terminal:
                continue
            out.append(info.to_dict())
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

    @classmethod
    def session_survives_restart(cls, session: InteractiveSession) -> bool:
        """Whether ``session`` keeps running after the daemon process exits."""
        return getattr(session.backend, "name", "") in cls.SURVIVABLE_BACKENDS

    def shutdown(self, *, kill_agents: bool = True) -> None:
        """Stop the control plane.

        With ``kill_agents=True`` (the historical default) every agent process is
        stopped too. With ``kill_agents=False`` agents on a survivable backend
        (tmux / ssh-tmux) are left running so the next daemon adopts them — the
        PTY backend cannot survive its parent and is always stopped. Either way
        the control plane's own resources are released.
        """
        self.remote_links.stop_all()
        for harness in self.agents.all():
            if not kill_agents and self.session_survives_restart(harness.session):
                continue
            try:
                harness.stop()
            except Exception:  # noqa: BLE001 - shutting down must continue
                log.debug("harness stop failed during shutdown", exc_info=True)
        for session in self.registry.all():
            if not kill_agents and self.session_survives_restart(session):
                continue
            try:
                session.close()
            except Exception:  # noqa: BLE001 - shutting down must continue
                log.debug("session close failed during shutdown", exc_info=True)
        if kill_agents:
            for agent_id in list(self._mcp_files):
                self._mcp_cleanup(agent_id)
        try:
            paths.cleanup_tmpdir()
        except Exception:  # noqa: BLE001 - a cleanup failure must not block shutdown
            log.debug("tmpdir cleanup failed during shutdown", exc_info=True)
        try:
            self.workers.stop_all()
        except Exception:  # noqa: BLE001 - never block shutdown on a stuck worker
            log.debug("could not stop all workers", exc_info=True)

    def summary(self, session: InteractiveSession) -> dict[str, Any]:
        session.poll()
        return session.info().to_dict()
