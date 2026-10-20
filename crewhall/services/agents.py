"""Controller services: agents (mixins of the Controller facade)."""
from __future__ import annotations

# ruff: noqa: F401
# mypy: disable-error-code="attr-defined"
import contextlib
import os
import secrets
import signal
import subprocess
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


class _AdoptedServe:
    """A live ``opencode serve`` owned by a previous daemon process.

    Behaves like the ``subprocess.Popen`` crewhall keeps for servers it started
    (``pid``/``poll``), so process trees and shutdown keep working after an
    orphan server is adopted instead of being rebound/killed.
    """

    def __init__(self, pid: int) -> None:
        self.pid = int(pid)

    def poll(self) -> int | None:
        return None if os.path.isdir(f"/proc/{self.pid}") else 0


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
        # Never inherit a parent OpenCode/agent session: if crewhall itself runs
        # inside one, variables like OPENCODE_SESSION_ID or OPENCODE_PASSWORD make
        # the child TUI/server behave as nested (or point at the parent). Only
        # the per-launch OPENCODE_SERVER_PASSWORD is added afterwards.
        for key in os.environ:
            if key.startswith("OPENCODE_"):
                env.setdefault(key, "")
        for key in ("AI_AGENT", "AGENT"):
            if key in os.environ:
                env.setdefault(key, "")
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

    @staticmethod
    def _v2_tabs_off_content(existing: str | None) -> str:
        """Merge ``{"tabs":{"mode":"off"}}`` into an existing CLI config.

        ``OPENCODE_CLI_CONFIG_CONTENT`` is deep-merged by OpenCode over the
        user's ``cli.json``, so this disables the tab strip for *this* agent
        without touching the user's config (theme/keybinds survive).
        """
        import json

        data: dict[str, Any] = {}
        if existing:
            try:
                parsed = json.loads(existing)
                if isinstance(parsed, dict):
                    data = parsed
            except ValueError:
                pass
        tabs = data.get("tabs")
        merged = dict(tabs) if isinstance(tabs, dict) else {}
        merged["mode"] = "off"
        data["tabs"] = merged
        return json.dumps(data, separators=(",", ":"), sort_keys=True)

    def _apply_v2_tabs(self, env: dict[str, str]) -> None:
        """Force the v2 tab strip off for one agent (merge, never replace)."""
        current = env.get("OPENCODE_CLI_CONFIG_CONTENT")
        if current is None:
            current = os.environ.get("OPENCODE_CLI_CONFIG_CONTENT")
        env["OPENCODE_CLI_CONFIG_CONTENT"] = self._v2_tabs_off_content(current)

    def _isolate_opencode_tabs(
        self, env: dict[str, str], agent_id: str, link: Any, api: str | None = None,
    ) -> None:
        """Give an OpenCode v2 TUI its own state dir (and no stray tab strip).

        The v2 TUI keeps the open-session tabs per working directory in
        ``<state>/opencode/latest/tui/tabs.json``, shared by every TUI of the
        user, and never closes them: each restart or sibling agent in the same
        directory added another tab. A private ``XDG_STATE_HOME`` per agent (seeded
        with the user's preferences, tabs reset on every launch) leaves just the
        agent's own session open. Sessions themselves stay in the shared data dir.

        ``prompt-history.jsonl``/``frecency.jsonl`` are deliberately **not**
        copied: they are the user's other projects' prompts, not this agent's.
        """
        is_v2 = api == "v2" or (link is not None and getattr(link, "api", "v1") == "v2")
        if not is_v2:
            return
        import shutil

        real = os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "opencode")
        home = os.path.join(paths.state_dir(), "opencode-state", agent_id)
        mine = os.path.join(home, "opencode")
        private_files = {"tabs.json", "service.json",
                         "prompt-history.jsonl", "frecency.jsonl"}
        try:
            for dirpath, dirs, files in os.walk(real):
                dirs[:] = [d for d in dirs if d != "locks"]
                rel = os.path.relpath(dirpath, real)
                for name in files:
                    if name in private_files:
                        continue
                    target = os.path.join(mine, rel, name)
                    if not os.path.exists(target):
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.copy2(os.path.join(dirpath, name), target)
            os.makedirs(mine, exist_ok=True)
            for dirpath, _dirs, files in os.walk(mine):
                for name in private_files:
                    if name in files:
                        os.remove(os.path.join(dirpath, name))
        except OSError:
            log.warning("could not isolate the OpenCode tabs for %s", agent_id, exc_info=True)
            return
        env["XDG_STATE_HOME"] = home
        self._apply_v2_tabs(env)

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

    @staticmethod
    def _drop_flags(args: list[str], flags: tuple[str, ...]) -> list[str]:
        """Remove ``--flag value`` / ``--flag=value`` / ``-f value`` pairs."""
        out: list[str] = []
        skip = False
        for arg in args:
            if skip:
                skip = False
                continue
            if arg in flags:
                skip = True
                continue
            if any(arg.startswith(f + "=") for f in flags):
                continue
            out.append(arg)
        return out

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
        server_url: str | None = None,
    ) -> list[str]:
        cls = get_harness(kind)
        if kind == "opencode" and self._opencode_api(kind) == "v2":
            # v2's TUI rejects every v1 flag (and exits); the model/agent are set
            # through the API and ``--session`` is the only session selector it
            # accepts. The gate is the *API of the resolved CLI*, not whether a
            # private link happened to start: a failed `serve` must not fall back
            # to v1 argv and the user's managed service.
            extra_args = self._v2_clean_args(extra_args)
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
                base = [*command, *cls.launch_args(self._hooks_settings_path(None), None, port,
                                                   server_url=server_url)]
                return [*base, "--session", resume, *(mcp_args or []), *extra_args]
            if kind == "codex":
                return [*command, "resume", resume, *(mcp_args or []), *extra_args]
        return [
            *command,
            *cls.launch_args(self._hooks_settings_path(host), conversation_id, port,
                             server_url=server_url),
            *(mcp_args or []),
            *extra_args,
        ]

    def _opencode_api(self, kind: str) -> str:
        """Which server contract the resolved OpenCode CLI speaks (v1 or v2)."""
        try:
            _command, report, _used = self._resolve_provider_command(kind, None)
        except Exception:  # noqa: BLE001 - an unknown CLI falls back to v1
            return "v1"
        if report is not None and report.version and report.version[0] >= 2:
            return "v2"
        return "v1"

    def _new_opencode_link(self, kind: str, extra_args: list[str]) -> OpenCodeLink | None:
        """v1: port + password for the TUI's own server, unless the user set a port."""
        if not self.track_conversations or not get_harness(kind).uses_local_server:
            return None
        if self._user_manages_opencode_server(extra_args):
            return None
        if self._opencode_api(kind) == "v2":
            # v2 has no `--port`: the link is created together with a `serve`
            # process (see ``_open_opencode_link``).
            return None
        return OpenCodeLink(free_port(), secrets.token_urlsafe(24))

    @staticmethod
    def _user_manages_opencode_server(extra_args: list[str]) -> bool:
        return any(a in ("--port", "--server") or a.startswith(("--port=", "--server="))
                   for a in extra_args)

    def _start_opencode_server(self, kind: str, cwd: str, port: int, password: str) -> Any:
        """Start an OpenCode v2 ``serve`` for one agent; return the Popen (or None).

        The password travels in the environment (never argv), exactly like v1.
        """
        command, _report, _used = self._resolve_provider_command(kind, None)
        argv = [*command, "serve", "--port", str(port), "--hostname", "127.0.0.1"]
        env = dict(os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = password
        self._ensure_usable_tmpdir(env)
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is a fixed, validated command
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("could not start the OpenCode server for %s: %s", kind, exc)
            return None
        link = OpenCodeLink(port, password, api="v2")
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.warning("the OpenCode server for %s exited early (code %s)",
                            kind, proc.returncode)
                return None
            try:
                link.get_json("/api/info", timeout=1.0)
                return proc
            except Exception:  # noqa: BLE001 - not listening yet
                time.sleep(0.25)
        log.warning("the OpenCode server for %s did not become ready", kind)
        self._kill_opencode_server(proc)
        return None

    @staticmethod
    def _kill_opencode_server(proc: Any) -> None:
        """Terminate a serve process and its group; never leave it orphaned."""
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(OSError):
                proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()

    def _v2_serve_fallback(
        self, kind: str, args: list[str], link: Any, agent_id: str,
    ) -> list[str]:
        """Never let a failed private ``serve`` fall back to the managed service.

        Without ``--server`` the v2 TUI silently connects to the user's shared
        OpenCode service. When crewhall could not start/link its own server (and
        the user did not pass one), launch ``--standalone`` with a visible notice.
        """
        if (kind != "opencode" or link is not None
                or self._opencode_api(kind) != "v2"
                or self._user_manages_opencode_server(args)):
            return args
        self._publish_v2_notice(notices.add(
            key=f"v2serve:{agent_id}", kind="opencode", level="warn",
            title="OpenCode v2 started without its private server",
            body=(
                "The per-agent `opencode serve` did not start, so crewhall launched "
                "this agent in --standalone mode instead of silently using your "
                "managed OpenCode service (which would mix tabs and sessions). "
                "History, activity and API delivery are unavailable for this agent "
                "until it is restarted."
            ),
        ))
        return [*args, "--standalone"]

    def _open_opencode_link(
        self, kind: str, extra_args: list[str], cwd: str,
    ) -> tuple[OpenCodeLink | None, Any]:
        """Create the link (and, for v2, start the private server).

        Returns ``(link, server_process)``. For v1 the server lives inside the
        TUI (``--port``); for v2 crewhall runs ``serve --port`` and the TUI
        connects with ``--server`` (the password is shared through the env).
        """
        if not get_harness(kind).uses_local_server:
            return None, None
        if self._user_manages_opencode_server(extra_args):
            return None, None
        v2 = self._opencode_api(kind) == "v2"
        # v2 always gets its private server, even with conversation tracking off:
        # without --server its TUI falls back to OpenCode's shared managed service
        # on a fixed port and dies when that port is taken.
        if not self.track_conversations and not v2:
            return None, None
        if v2:
            password = secrets.token_urlsafe(24)
            port = free_port()
            proc = self._start_opencode_server(kind, cwd, port, password)
            if proc is None:
                return None, None  # degrade to screen-only state, honestly
            return OpenCodeLink(port, password, api="v2"), proc
        return OpenCodeLink(free_port(), secrets.token_urlsafe(24)), None

    def _reattach_opencode_link(self, harness: Harness, info: dict[str, Any], cwd: str) -> None:
        """Rebuild the link for an adopted agent after a daemon restart.

        v2: first probe ``GET /api/info`` on the persisted port/password. A
        ``serve`` that survived a daemon crash is adopted (its pid becomes the
        process-tree root) instead of starting a new one on an occupied port.
        Only when nothing answers is a fresh ``serve`` started. v1: the TUI
        still owns its server on that port.
        """
        if harness.kind != "opencode" or not self.track_conversations:
            return
        api = str(info.get("api") or "v1")
        try:
            port, password = int(info["port"]), str(info["password"])
        except (KeyError, TypeError, ValueError):
            return
        link = OpenCodeLink(port, password, api=api)
        if api == "v2":
            adopted = False
            try:
                live = link.get_json("/api/info", timeout=2.0)
            except Exception:  # noqa: BLE001 - nobody is listening: start one
                live = None
            if isinstance(live, dict):
                pid = live.get("pid")
                version = live.get("version")
                if isinstance(version, str) and version:
                    self._runtime_versions[harness.agent_id] = f"opencode v{version}"
                if isinstance(pid, int) and pid > 0:
                    self._opencode_servers[harness.agent_id] = _AdoptedServe(pid)
                    adopted = True
                elif live.get("version"):
                    adopted = True  # answering, but no pid: keep the link
            if not adopted:
                proc = self._start_opencode_server(harness.kind, cwd, port, password)
                if proc is None:
                    self._publish_v2_notice(notices.add(
                        key=f"v2reattach:{harness.agent_id}", kind="opencode", level="warn",
                        title="OpenCode v2 server could not be reattached",
                        body=(
                            "Neither the previous per-agent `opencode serve` nor a "
                            "replacement on its persisted port responded, so this "
                            "agent is screen-only (no API history/state) until it is "
                            "restarted. It was not pointed at your managed service."
                        ),
                    ))
                    return
                self._opencode_servers[harness.agent_id] = proc
        else:
            # v1 owns its server inside the TUI: if it no longer answers, do not
            # attach a dead link and pretend history/state are available.
            try:
                OpenCodeLink.detect_api(port, password)
            except Exception:  # noqa: BLE001 - connection refused/401: dead server
                harness.legacy = True
                self._publish_v2_notice(notices.add(
                    key=f"v1reattach:{harness.agent_id}", kind="opencode", level="warn",
                    title="OpenCode v1 server could not be reattached",
                    body=(
                        "The adopted agent's v1 server no longer answers on its "
                        "persisted port/password, so its link was not attached: "
                        "this agent is screen-only (no API history/state) until it "
                        "is restarted. Its previous conversation was not discarded."
                    ),
                ))
                return
        self._opencode_links[harness.agent_id] = {
            "port": port, "password": password, "api": api,
        }
        self._attach_link(harness, link)
        if api == "v2" and harness.conversation_id:
            # An adopted v2 TUI keeps its session: re-apply the requested model and
            # agent so a daemon restart never silently reverts them.
            self._configure_v2_session(
                harness.agent_id, harness.kind, link,
                self._agent_args.get(harness.agent_id, []),
                session_id=harness.conversation_id,
            )

    def _reopen_opencode_link(
        self, agent_id: str, kind: str, extra_args: list[str], cwd: str,
    ) -> tuple[OpenCodeLink | None, Any]:
        """Link for a relaunched agent: reuse its v2 server, or start a fresh one."""
        if not self.track_conversations or not get_harness(kind).uses_local_server:
            return None, None
        if self._user_manages_opencode_server(extra_args):
            return None, None
        if self._opencode_api(kind) == "v2":
            proc = self._opencode_servers.get(agent_id)
            info = self._opencode_links.get(agent_id)
            if proc is not None and proc.poll() is None and info:
                return OpenCodeLink(int(info["port"]), str(info["password"]), api="v2"), proc
            password = secrets.token_urlsafe(24)
            port = free_port()
            proc = self._start_opencode_server(kind, cwd, port, password)
            if proc is None:
                return None, None
            return OpenCodeLink(port, password, api="v2"), proc
        return OpenCodeLink(free_port(), secrets.token_urlsafe(24)), None

    # Flags the v2 TUI rejects: crewhall never passes them; the model/agent are
    # applied through the server API instead (any other dropped flag is reported).
    _V2_DROP_FLAGS = ("--model", "-m", "--agent", "--session-id", "--resume", "-r",
                      "--port", "--hostname", "--variant")
    # Flags that pick the session themselves: crewhall parses and validates them
    # (``--session``) or resolves them to one (``--continue``) before launching.
    _V2_SESSION_FLAGS = ("--session", "-s", "--continue", "-c")
    # v2 accepts these; every other option-like argument is a v1 flag and is
    # dropped with a notice (positional arguments, e.g. a directory, are kept).
    _V2_ACCEPTED_FLAGS = ("--server", "--session", "--standalone", "-s", "-c",
                          "--help", "-h", "--version", "-v", "--print-logs",
                          "--log-level", "--")

    def _v2_clean_args(self, args: list[str]) -> list[str]:
        """Remove v1/unknown options from the argv handed to the v2 TUI."""
        out: list[str] = []
        skip = False
        for arg in args:
            if skip:
                skip = False
                continue
            if arg in self._V2_DROP_FLAGS:
                skip = True
                continue
            if any(arg.startswith(flag + "=") for flag in self._V2_DROP_FLAGS):
                continue
            if arg in ("--session", "-s", "--continue", "-c"):
                skip = True
                continue
            if (arg.startswith("-") and arg not in self._V2_ACCEPTED_FLAGS
                    and not arg.startswith("--log-level=")):
                if "=" not in arg:
                    skip = True  # an unknown ``--flag value`` pair
                continue
            out.append(arg)
        return out

    @staticmethod
    def _flag_value(args: list[str], names: tuple[str, ...]) -> str | None:
        """Value of ``--flag value`` / ``--flag=value`` for the first ``names``."""
        for i, arg in enumerate(args):
            if arg in names and i + 1 < len(args):
                return args[i + 1]
            for name in names:
                if arg.startswith(name + "="):
                    return arg.split("=", 1)[1] or None
        return None

    @staticmethod
    def _present_flags(args: list[str], flags: tuple[str, ...]) -> list[str]:
        """The ``flags`` present in ``args`` (``--f v`` or ``--f=v``), in order."""
        found: list[str] = []
        for arg in args:
            if arg in flags:
                found.append(arg)
            else:
                for flag in flags:
                    if arg.startswith(f"{flag}="):
                        found.append(flag)
                        break
        return found

    def _v2_session_flags(self, args: list[str]) -> list[str]:
        """Session-selecting flags the caller used (they own the session)."""
        return self._present_flags(args, self._V2_SESSION_FLAGS)

    def _v2_latest_session(self, link: Any, cwd: str) -> str | None:
        """Newest root session of ``cwd`` (what v2's ``--continue`` opens)."""
        from urllib.parse import quote

        try:
            raw = link.get_json(
                f"/api/session?directory={quote(cwd)}&order=desc&limit=1", timeout=3.0)
        except Exception:  # noqa: BLE001 - the server may still be starting
            return None
        data = raw.get("data") if isinstance(raw, dict) else raw
        for session in data if isinstance(data, list) else []:
            if isinstance(session, dict) and session.get("id") and not session.get("parentID"):
                return str(session["id"])
        return None

    def _resolve_v2_session(
        self, link: Any, extra_args: list[str], cwd: str, agent_id: str,
    ) -> tuple[str | None, list[str], str | None]:
        """Resolve ``--session``/``--continue`` to a validated session id.

        Returns ``(session_id, cleaned_args, missing_id)``. A missing session is
        *not* opened under the same id (v2 would create an empty one): the caller
        starts a fresh session and records a visible notice. ``--continue`` is
        rewritten to the newest session of the working directory.
        """
        session_id = self._flag_value(extra_args, ("--session", "-s"))
        cleaned = list(extra_args)
        missing: str | None = None
        if self._present_flags(extra_args, ("--continue", "-c")):
            session_id = self._v2_latest_session(link, cwd)
            cleaned = self._drop_flags(cleaned, ("--continue", "-c"))
            if session_id is None:
                missing = "--continue"
        if session_id and not self._v2_session_exists(link, session_id):
            missing = session_id
            session_id = None
        if session_id:
            cleaned = self._drop_flags(cleaned, ("--session", "-s", "--continue", "-c"))
        else:
            # Never keep a stale selector: v2 would create an empty session
            # under the missing id/continue target.
            cleaned = self._drop_flags(cleaned, ("--session", "-s", "--continue", "-c"))
            if missing:
                self._record_v2_unresumable(agent_id, missing, from_v1=False)
        return session_id, cleaned, missing

    @staticmethod
    def _v2_model_ref(link: Any, model: str | None) -> dict[str, str]:
        """``--model provider/model[#variant]`` -> a v2 ``Model.Ref``.

        The CLI accepts ``provider/model``, a model alone (the server's default
        provider supplies the provider), a display name, and a ``#variant``
        suffix. The catalog is authoritative: aliases (``opencode``) resolve to
        the canonical provider id, and an unknown model fails here (with a
        notice from the caller) instead of being POSTed for a silent 204.
        """
        text = (model or "").strip()
        if not text:
            return {}
        variant = ""
        base, sep, suffix = text.rpartition("#")
        if sep and base and suffix:
            text, variant = base.strip(), suffix.strip()
        raw_provider, sep, raw_model = text.partition("/")
        provider: str | None = raw_provider if (sep and raw_provider and raw_model) else None
        model_id = raw_model if provider else text
        try:
            raw = link.get_json("/api/model", timeout=3.0)
            catalog = raw.get("data") if isinstance(raw, dict) else raw
        except Exception:  # noqa: BLE001 - the server may be busy or older
            catalog = None
        entries = [e for e in catalog if isinstance(e, dict)] if isinstance(catalog, list) else []
        matches: list[dict[str, str]] = []
        for entry in entries:
            mid = str(entry.get("modelID") or entry.get("id") or "").strip()
            pid = str(entry.get("providerID") or "").strip()
            name = str(entry.get("name") or "").strip()
            if not mid or not pid:
                continue
            if model_id.casefold() not in (
                mid.casefold(), str(entry.get("id") or "").casefold(),
                name.casefold(),
            ):
                continue
            matches.append({"providerID": pid, "id": mid,
                            "canonical": str(entry.get("canonical") or "").strip()})
        if matches:
            chosen = matches[0]
            if provider:
                for match in matches:
                    ids = (match["providerID"].casefold(), match["canonical"].casefold())
                    if (provider.casefold() in ids
                            or any(provider.casefold() in i or i in provider.casefold()
                                   for i in ids if i)):
                        chosen = match
                        break
                else:
                    return {}  # the model exists, but not under that provider
            ref = {"providerID": chosen["providerID"], "id": chosen["id"]}
            if variant:
                ref["variant"] = variant
            return ref
        if entries:
            return {}  # a catalog exists and does not know this model/display name
        if provider:
            ref = {"providerID": provider, "id": model_id}
            if variant:
                ref["variant"] = variant
            return ref
        try:
            raw = link.get_json("/api/model/default", timeout=3.0)
            data = raw.get("data") if isinstance(raw, dict) else None
            default_provider = str((data or {}).get("providerID") or "") or None
        except Exception:  # noqa: BLE001 - the server may be busy or older
            default_provider = None
        if not default_provider:
            return {}
        ref = {"providerID": default_provider, "id": model_id}
        if variant:
            ref["variant"] = variant
        return ref

    @staticmethod
    def _v2_running_model(link: Any, session_id: str) -> str | None:
        """The model the v2 server reports for ``session_id``, as ``provider/id``."""
        try:
            raw = link.get_json(f"/api/session/{session_id}", timeout=3.0)
        except Exception:  # noqa: BLE001 - best effort: running model is optional
            return None
        data = raw.get("data") if isinstance(raw, dict) else None
        model = (data or {}).get("model") or {}
        model_id = str(model.get("id") or model.get("modelID") or "").strip()
        if not model_id:
            return None
        provider = str(model.get("providerID") or "").strip()
        return f"{provider}/{model_id}" if provider else model_id

    def _requested_v2_config(self, kind: str, extra_args: list[str]) -> tuple[str | None, str | None]:
        """The model (``--model`` else the provider default) and agent asked for."""
        model = (act.model_from_args(extra_args)
                 or settings.provider(kind).get("default_model") or None)
        agent = self._flag_value(extra_args, ("--agent",))
        return (str(model) if model else None, agent)

    def _v2_agent_exists(self, link: Any, agent: str) -> bool:
        try:
            raw = link.get_json("/api/agent", timeout=3.0)
        except Exception:  # noqa: BLE001 - unreachable: let the read-back decide
            return True
        data = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(data, list):
            return True
        return any(isinstance(a, dict) and a.get("id") == agent for a in data)

    @staticmethod
    def _v2_session_value(link: Any, session_id: str, key: str) -> Any:
        raw = link.get_json(f"/api/session/{session_id}", timeout=3.0)
        data = raw.get("data") if isinstance(raw, dict) else raw
        return (data or {}).get(key) if isinstance(data, dict) else None

    @staticmethod
    def _model_matches(value: Any, ref: dict[str, Any]) -> bool:
        if not isinstance(value, dict):
            return False
        model_id = value.get("id") or value.get("modelID")
        provider = value.get("providerID")
        return (model_id == ref.get("id")
                and (not provider or provider == ref.get("providerID")))

    def _configure_v2_session(
        self, agent_id: str, kind: str, link: Any, extra_args: list[str],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply ``--model``/``--agent`` to an OpenCode v2 session through the API.

        With ``session_id=None`` a fresh session is created first, so the model
        and agent are set before the TUI can use it; otherwise the switches are
        applied to the resumed session. v2 answers **204 even for an unknown
        value**, so every switch is validated against the catalog *and* read
        back: only a matching session reports ``applied``. Never raises: a
        failure is a notice (modal + desktop) and a report, never silent.

        Returns the report (also stored in ``self._v2_config[agent_id]``).
        """
        model, agent = self._requested_v2_config(kind, extra_args)
        # A model/agent chosen in the TUI/Web UI at runtime is not in the original
        # args; reapply the last known session_config on a reset/restart.
        saved = self._v2_config.get(agent_id) or {}
        if saved:
            # A runtime choice (picker/Web UI) is not in the original args; only
            # an explicit --model/--agent wins over the last applied config.
            if not act.model_from_args(extra_args) and saved.get("model"):
                model = saved["model"]
            if not self._flag_value(extra_args, ("--agent",)) and saved.get("agent"):
                agent = saved["agent"]
        dropped = self._present_flags(extra_args, self._V2_DROP_FLAGS)
        report: dict[str, Any] = {
            "model": model, "running_model": None, "model_applied": None,
            "agent": agent, "agent_applied": None,
            "session_id": session_id, "ignored_flags": [], "error": None,
            "model_variant": None,
        }
        errors: list[str] = []
        if link is None or getattr(link, "api", "v1") != "v2":
            return report
        try:
            ref = self._v2_model_ref(link, model) if model else {}
            report["model_variant"] = ref.get("variant")
            if model and not ref:
                raise RuntimeError(f"could not resolve model {model!r}")
            if agent and not self._v2_agent_exists(link, agent):
                raise RuntimeError(f"agent {agent!r} is not available on this server")
            if session_id is None:
                # The TUI takes its composer model from the session's *creation*
                # model, so set it in the create call (the switch endpoint alone
                # updates the session but not what the TUI shows).
                created = link.post_json("/api/session", {"model": ref} if ref else {})
                data = created.get("data") if isinstance(created, dict) else None
                session_id = str((data or {}).get("id") or "") or None
                if not session_id:
                    raise RuntimeError("the server did not return a session id")
                report["session_id"] = session_id
            elif model:
                link.post_json(f"/api/session/{session_id}/model", {"model": ref})
            # Read-back: v2 does not reject an unknown model/agent, it ignores it.
            if model:
                report["model_applied"] = self._model_matches(
                    self._v2_session_value(link, session_id, "model"), ref)
                if not report["model_applied"]:
                    errors.append(f"the server did not apply model {model!r}")
            if agent:
                try:
                    link.post_json(
                        f"/api/session/{session_id}/agent", {"agent": agent})
                    report["agent_applied"] = (
                        self._v2_session_value(link, session_id, "agent") == agent)
                except Exception as exc:  # noqa: BLE001 - record it, keep the model result
                    report["agent_applied"] = False
                    errors.append(str(exc))
                if not report["agent_applied"]:
                    errors.append(f"the server did not apply agent {agent!r}")
        except Exception as exc:  # noqa: BLE001 - a bad model/agent must not abort the launch
            errors.append(str(exc))
            if model and report["model_applied"] is None:
                report["model_applied"] = False
            if agent and report["agent_applied"] is None:
                report["agent_applied"] = False
        if report["session_id"]:
            report["running_model"] = self._v2_running_model(link, report["session_id"])
        report["error"] = "; ".join(errors) or None
        # Every dropped flag that was not (re)applied is reported, not silent.
        applied = set()
        if report["model_applied"]:
            applied.update({"--model", "-m"})
        if report["agent_applied"]:
            applied.add("--agent")
        report["ignored_flags"] = [f for f in dropped if f not in applied]
        self._v2_config[agent_id] = report
        self._report_v2_config(agent_id, report)
        return report

    def _v2_session_exists(self, link: Any, session_id: str | None) -> bool:
        """Whether the v2 server already knows this conversation id.

        A conversation created before an upgrade (typically by a v1 server) is
        not present on the v2 server, and v2's ``--session`` then *creates* an
        empty session under that id, silently losing the history. Ask the server
        before resuming instead of trusting the id alone.
        """
        if link is None or getattr(link, "api", "v1") != "v2" or not session_id:
            return False
        try:
            link.get_json(f"/api/session/{session_id}", timeout=3.0)
            return True
        except Exception:  # noqa: BLE001 - 404/unreachable: not resumable here
            return False

    def _record_v2_unresumable(self, agent_id: str, old_id: str, *, from_v1: bool) -> None:
        """Say plainly that a pre-upgrade conversation cannot be resumed on v2."""
        subject = ("an OpenCode v1 conversation is not resumable in v2"
                   if from_v1 else "the conversation was not found on the v2 server")
        self._publish_v2_notice(notices.add(
            key=f"v2migrate:{agent_id}:{old_id}",
            kind="opencode", level="warn",
            title=("OpenCode v1 conversation not resumable in v2 — "
                   "started a new session" if from_v1
                   else "OpenCode v2 conversation not found — started a new session"),
            body=(
                f"{old_id}: {subject}. The v2 server did not have it, so opening it "
                "would have silently shown an empty conversation under the same id. "
                "crewhall started a fresh session instead; the previous history is "
                "still on disk but cannot be continued here."
            ),
        ))

    def _report_v2_config(self, agent_id: str, report: dict[str, Any]) -> None:
        """Explain, once, any OpenCode v2 setting crewhall could not apply."""
        ignored = report.get("ignored_flags") or []
        if ignored:
            self._publish_v2_notice(notices.add(
                key=f"v2flags:{agent_id}:{','.join(sorted(set(ignored)))}",
                kind="opencode", level="warn",
                title="OpenCode v2 ignored some flags",
                body=(
                    "The OpenCode v2 TUI does not accept these flags, so crewhall did "
                    f"not pass them: {', '.join(ignored)}. Choose the model/agent in "
                    "Settings → Providers or with crewhall's own selector instead."
                ),
            ))
        for field, label in (("model", "model"), ("agent", "agent")):
            value = report.get(field)
            if not value or report.get(f"{field}_applied") is not False:
                continue
            if field == "model":
                running = report.get("running_model") or "the provider default"
                detail = f"crewhall asked for model {value!r} but the agent is running {running}."
            else:
                detail = f"crewhall asked for agent {value!r} but it was not applied."
            self._publish_v2_notice(notices.add(
                key=f"v2{field}:{agent_id}:{value}",
                kind="opencode", level="warn",
                title=f"OpenCode v2 could not apply the requested {label}",
                body=f"{detail} {report.get('error') or ''}".strip(),
            ))

    def _publish_v2_notice(self, notice: dict[str, Any] | None) -> None:
        """Mirror a notice to the desktop (best effort), like the version gate."""
        if notice is None:
            return
        try:
            from .. import watch

            watch.notify(notice["title"], notice["body"])
        except Exception:  # noqa: BLE001 - a desktop notification is best effort
            log.debug("desktop notification failed", exc_info=True)

    def _apply_pending_v2_config(self, harness: Harness, session_id: str) -> None:
        """Apply a deferred v2 model/agent once the TUI's session is known.

        Covers sessions the TUI (or the caller) creates itself: the model/agent
        are switched as soon as the session is adopted, never silently skipped.
        """
        link = getattr(harness, "link", None)
        if link is None or getattr(link, "api", "v1") != "v2":
            return
        args = self._agent_args.get(harness.agent_id, [])
        report = self._v2_config.get(harness.agent_id) or {}
        if report.get("session_id") == session_id and report.get("error") is None:
            return  # already applied to this very session
        self._configure_v2_session(
            harness.agent_id, harness.kind, link, args, session_id=session_id,
        )

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
            if not getattr(link, "api_detected", False):
                # Ask the running server whether it speaks v1 or v2; keep trying
                # until it answers (the port may not be open yet).
                try:
                    link.api = OpenCodeLink.detect_api(link.port, link.password)
                    link.api_detected = True
                except Exception:  # noqa: BLE001 - not up yet
                    stop.wait(1.0)
                    continue
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
        link = getattr(harness, "link", None)
        if getattr(link, "api", "v1") == "v2":
            return self._on_opencode_event_v2(harness, event)
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

    def _on_opencode_event_v2(self, harness: Harness, event: dict[str, Any]) -> None:
        """OpenCode v2 events: ``{id, type, location, data}``.

        v2 dropped ``session.status``/``session.idle`` for a turn-lifecycle pair:
        ``session.execution.started`` begins a turn and ``session.execution.
        succeeded|failed|interrupted`` ends it. Questions became ``form.*`` and
        ``permission.rejected`` joined ``permission.asked/replied``.

        Focus rule: a turn in *another* session (a background tab) must never
        change ``conversation_id``; only explicit navigation (``session.viewed``,
        ``session.created``, ``session.forked``) adopts a session.
        """
        kind = event.get("type")
        raw = event.get("data")
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        sid = data.get("sessionID")
        if kind in ("permission.asked", "permission.replied", "permission.rejected"):
            self._on_opencode_interaction(harness, kind, data)
            return
        if kind in ("form.created", "form.replied", "form.cancelled"):
            self._on_opencode_interaction(harness, kind, data)
            return
        if kind == "server.connected" and harness.link is not None:
            self._sync_opencode_interactions(harness, harness.link)
            self._resync_opencode_v2(harness, harness.link)
            return
        if kind == "session.created":
            sid = sid or data.get("id")
            if sid and not data.get("parentID"):
                self._adopt_conversation(harness, sid)
            elif sid:
                self._opencode_children.setdefault(harness.agent_id, set()).add(sid)
            return
        if kind == "session.viewed" and sid:
            # Explicit navigation (opening a session or the visible one going
            # idle): this *is* the user choosing the conversation.
            self._opencode_children.get(harness.agent_id, set()).discard(sid)
            self._adopt_conversation(harness, sid)
            return
        if kind == "session.forked" and sid:
            # A fork the user works in is a root conversation, not a subagent.
            self._opencode_children.get(harness.agent_id, set()).discard(sid)
            self._adopt_conversation(harness, sid)
            return
        if kind == "session.deleted" and sid:
            self._on_opencode_session_deleted(harness, sid)
            return
        if kind in ("session.moved", "session.renamed"):
            self._on_opencode_session_meta(harness, kind, data)
            return
        if kind in ("session.agent.selected", "session.model.selected"):
            self._on_opencode_session_selected(harness, kind, data)
            return
        children = self._opencode_children.get(harness.agent_id, ())
        if sid and sid in children:
            return  # a subagent's turn is not the agent's turn
        if kind == "session.execution.started":
            if sid and sid != harness.conversation_id:
                if harness.conversation_id is None:
                    # Nothing tracked yet (missed created/viewed): the sole
                    # session that starts working is the agent's own.
                    self._follow_opencode_session(harness, sid)
                else:
                    return  # a background tab: never hijack the conversation
            last = self._hook_events.get(harness.agent_id) or {}
            if last.get("event") != "prompt_submit":
                self._apply_hook(harness, "prompt_submit")
        elif kind in ("session.execution.succeeded", "session.execution.failed",
                      "session.execution.interrupted"):
            if sid and sid != harness.conversation_id:
                return
            self._apply_hook(harness, "stop")

    def _on_opencode_session_deleted(self, harness: Harness, sid: str) -> None:
        if sid != harness.conversation_id:
            self._opencode_children.get(harness.agent_id, set()).discard(sid)
            return
        harness.conversation_id = None
        harness.conversation_since = time.time()
        self._publish_v2_notice(notices.add(
            key=f"v2deleted:{harness.agent_id}:{sid}", kind="opencode", level="warn",
            title="The OpenCode session was deleted",
            body=(
                f"{sid} was deleted in the TUI, so crewhall stopped tracking it. "
                "The next prompt creates a new session."
            ),
        ))
        self._persist()

    def _on_opencode_session_meta(self, harness: Harness, kind: str,
                                  data: dict[str, Any]) -> None:
        if data.get("sessionID") != harness.conversation_id:
            return
        if kind == "session.moved":
            location = data.get("location") or {}
            directory = location.get("directory") if isinstance(location, dict) else None
            if isinstance(directory, str) and directory:
                harness.session.spec.cwd = directory
        else:
            title = data.get("title")
            if isinstance(title, str) and title:
                harness.session_title = title
        self._persist()

    def _on_opencode_session_selected(self, harness: Harness, kind: str,
                                      data: dict[str, Any]) -> None:
        if data.get("sessionID") != harness.conversation_id:
            return
        report = self._v2_config.setdefault(harness.agent_id, {})
        if kind == "session.agent.selected":
            report["agent"] = data.get("agent")
            report["agent_applied"] = True
        else:
            model = data.get("model") or {}
            model_id = model.get("id") or model.get("modelID") if isinstance(model, dict) else None
            provider = model.get("providerID") if isinstance(model, dict) else None
            if model_id:
                report["model"] = f"{provider}/{model_id}" if provider else model_id
                report["running_model"] = report["model"]
                report["model_applied"] = True
                if model.get("variant"):
                    report["model_variant"] = model["variant"]
        self._persist()

    def _resync_opencode_v2(self, harness: Harness, link: OpenCodeLink) -> None:
        """Recover state/interactions after an SSE (re)connection.

        Events emitted while the stream was down are lost; ``active`` plus the
        tracked session's own fields are authoritative, so a reconnect never
        leaves the agent stuck in UNKNOWN.
        """
        try:
            raw = link.get_json("/api/session/active", timeout=2.0)
            active = raw.get("data") if isinstance(raw, dict) else raw
        except Exception:  # noqa: BLE001 - the stream will retry
            active = None
        if not harness.conversation_id and isinstance(active, dict) and len(active) == 1:
            only = next(iter(active))
            self._follow_opencode_session(harness, only)
        if harness.conversation_id and isinstance(active, dict):
            if harness.conversation_id in active:
                self._apply_hook(harness, "prompt_submit")
                return
        if not harness.conversation_id:
            return
        try:
            raw = link.get_json(f"/api/session/{harness.conversation_id}", timeout=2.0)
            session = raw.get("data") if isinstance(raw, dict) else raw
        except Exception:  # noqa: BLE001
            return
        if isinstance(session, dict) and session.get("outcome") is not None:
            self._apply_hook(harness, "stop")

    def _backfill_opencode_legacy(self, harness: Harness, cwd: str | None) -> None:
        """Recover the link/conversation of an adopted pre-upgrade agent.

        An agent adopted from a surviving tmux pane has no persisted link. The
        real TUI is a descendant of the pane shell: read its ``/proc`` argv and
        environment for ``--port``/``--server`` and ``OPENCODE_SERVER_PASSWORD``,
        probe the server and adopt the newest session of its directory. If that
        cannot be done, mark it ``legacy`` (visible in status) instead of
        pretending it is a v2 agent with no history.
        """
        if harness.conversation_id and self._opencode_links.get(harness.agent_id):
            return
        pid = harness.session.pid
        if not pid:
            return
        for child in self._proc_descendants(int(pid)):
            try:
                with open(f"/proc/{child}/cmdline", "rb") as fh:
                    argv = [p.decode("utf-8", "replace") for p in fh.read().split(b"\0") if p]
            except OSError:
                continue
            if not argv or os.path.basename(argv[0]).split()[0] != "opencode":
                continue
            port = self._flag_value(argv, ("--port",))
            server = self._flag_value(argv, ("--server",))
            if not port and server:
                from urllib.parse import urlparse

                parsed = urlparse(server)
                port = str(parsed.port or "") or None
            password = ""
            try:
                with open(f"/proc/{child}/environ", "rb") as fh:
                    for item in fh.read().split(b"\0"):
                        key, _, value = item.partition(b"=")
                        if key == b"OPENCODE_SERVER_PASSWORD":
                            password = value.decode("utf-8", "replace")
            except OSError:
                password = ""
            if not port or not password:
                continue
            try:
                api = OpenCodeLink.detect_api(int(port), password)
            except Exception:  # noqa: BLE001 - a dead/old server: legacy
                continue
            link = OpenCodeLink(int(port), password, api=api)
            self._opencode_links[harness.agent_id] = {
                "port": int(port), "password": password, "api": api,
            }
            version = self._opencode_process_version(child, argv)
            if version:
                self._runtime_versions[harness.agent_id] = version
            self._attach_link(harness, link)
            if not harness.conversation_id:
                newest = link.discover(
                    cwd, 0, {h.conversation_id for h in self.agents.all()
                             if h is not harness and h.conversation_id},
                    newest=True)
                if newest:
                    harness.conversation_id = newest
            return
        harness.legacy = True
        self._publish_v2_notice(notices.add(
            key=f"legacy:{harness.agent_id}", kind="opencode", level="warn",
            title="OpenCode agent adopted as legacy",
            body=(
                "This agent was started by an older OpenCode process and its "
                "server/session could not be recovered, so crewhall marked it "
                "legacy: its previous conversation is not available here and a "
                "restart will start a new one. Export what you need from its TUI."
            ),
        ))

    @staticmethod
    def _opencode_process_version(pid: int, argv: list[str]) -> str | None:
        """The version of the *adopted* TUI binary, when it can be read.

        ``_attach_version`` probes the configured CLI; an adopted agent may run
        an older install (v1 while the configured binary is v2). Prefer the
        process' own executable, then its ``argv[0]``.
        """
        candidates: list[str] = []
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
            if os.path.basename(exe).split()[0].startswith("opencode"):
                candidates.append(exe)
        except OSError:
            pass
        if argv:
            candidates.append(argv[0])
        for candidate in candidates:
            if not candidate:
                continue
            text = versions.probe_version([candidate])
            parsed = versions.parse_version(text)
            if parsed:
                return f"opencode v{parsed[0]}.{parsed[1]}.{parsed[2]}"
        return None

    @staticmethod
    def _proc_descendants(root: int) -> list[int]:
        """Descendant pids of ``root`` from ``/proc`` (best effort, Linux)."""
        children: dict[int, list[int]] = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    fields = fh.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()
                ppid = int(fields[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(entry))
        out: list[int] = []
        queue = [int(root)]
        while queue:
            current = queue.pop()
            for child in children.get(current, []):
                if child not in out:
                    out.append(child)
                    queue.append(child)
        return out

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

    def _assert_no_mixed_opencode_db(self, kind: str) -> None:
        """Refuse to run OpenCode v1 and v2 agents against the same database.

        v2 migrates ``opencode.db`` (``session_v2``); a live v1 TUI keeps
        writing/reading the migrated database and can corrupt session tracking.
        """
        if kind != "opencode" or self._opencode_api(kind) != "v2":
            return
        for harness in self.agents.all():
            if harness.kind != "opencode" or not harness.session.status.alive:
                continue
            info = self._opencode_links.get(harness.agent_id) or {}
            if info.get("api") == "v1" or getattr(harness, "legacy", False):
                raise ValueError(
                    f"OpenCode v1 agent {harness.name or harness.agent_id!r} is still "
                    "running against the shared OpenCode database; v1 and v2 must not "
                    "run concurrently. Export/close the v1 agent, then create v2 ones."
                )

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
        self._assert_no_mixed_opencode_db(kind)
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
        # OpenCode link/server: v1 opens the port inside the TUI; v2 needs a
        # private `serve` in the agent's directory (so its location matches).
        link, server_proc = (None, None) if host else self._open_opencode_link(
            kind, extra_args, cwd or os.getcwd())
        if link is not None and getattr(link, "api", "v1") == "v2":
            try:
                live = link.get_json("/api/info", timeout=2.0)
                version = live.get("version") if isinstance(live, dict) else None
                if isinstance(version, str) and version:
                    self._runtime_versions[session_id] = f"opencode v{version}"
            except Exception:  # noqa: BLE001 - informational only
                pass
        if (kind == "opencode" and not host and link is None
                and self._user_manages_opencode_server(extra_args)
                and self._opencode_api(kind) == "v2"):
            self._publish_v2_notice(notices.add(
                key=f"v2userserver:{session_id}", kind="opencode", level="warn",
                title="OpenCode v2 agent is screen-only",
                body=(
                    "You passed --server/--port, so crewhall did not start (or "
                    "link to) a private server: history, activity and API delivery "
                    "are unavailable for this agent. Remove the flag to let "
                    "crewhall manage the server."
                ),
            ))
        # v2 drops --model/--agent from the TUI (it rejects them): crewhall creates
        # the session and applies them through the API, before the agent is usable.
        v2_session: str | None = None
        if link is not None and getattr(link, "api", "v1") == "v2" and not host:
            requested, extra_args, _missing = self._resolve_v2_session(
                link, extra_args, cwd or os.getcwd(), session_id)
            if requested:
                # The caller chose an existing session: apply model/agent to it
                # and open it with ``--session`` instead of creating another.
                v2_session = requested
                conversation_id = requested
                self._configure_v2_session(
                    session_id, kind, link, extra_args, session_id=requested)
            else:
                v2_session = self._configure_v2_session(
                    session_id, kind, link, extra_args, session_id=None,
                ).get("session_id")
                if v2_session:
                    conversation_id = v2_session
        elif (kind == "opencode" and not host and self._opencode_api(kind) == "v2"
              and not self._user_manages_opencode_server(extra_args)):
            extra_args = self._v2_serve_fallback(kind, extra_args, link, session_id)
        agent_env = self._build_agent_env(session_id, agent_name, [], env, host)
        if kind == "opencode" and not host:
            # Isolate even when the link/serve failed: the TUI still must not
            # inherit the user's tab strip or prompt history.
            self._isolate_opencode_tabs(
                agent_env, session_id, link, api=self._opencode_api(kind))
        if link:
            agent_env["OPENCODE_SERVER_PASSWORD"] = link.password
        spec = SessionSpec(
            command=self._launch_command(
                kind, extra_args, conversation_id, link.port if link else None, mcp_args,
                host,
                resume=v2_session,
                server_url=(f"http://127.0.0.1:{link.port}"
                            if link is not None and link.api == "v2" else None),
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
        model, requested_agent = self._requested_v2_config(kind, extra_args)
        self._agent_launch[session_id] = {
            "resolved_command": spec.command[0] if spec.command else None,
            "version": (self._agent_versions.get(session_id) or {}).get("version"),
            "args": list(extra_args),
            "generated_args": [a for a in spec.command[1:] if a not in extra_args],
            "model": model,
            "agent": requested_agent,
            "session": harness.conversation_id,
            "server": ({"port": link.port, "api": link.api} if link else None),
            "env_keys": sorted(agent_env),
        }
        if server_proc is not None:
            self._opencode_servers[session_id] = server_proc
        if link is not None:
            self._opencode_links[session_id] = {
                "port": link.port, "password": link.password, "api": link.api,
            }
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
            # v2 tools descend from the `serve` process: count it as an agent
            # root too, so a tool process is recognised as agent-originated.
            server = self._opencode_servers.get(harness.agent_id)
            if server is not None and server.poll() is None:
                pids.add(int(server.pid))
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
        summary["runtime_version"] = (
            self._runtime_versions.get(harness.agent_id)
            or (self._agent_launch.get(harness.agent_id) or {}).get("version")
            or (summary["cli"] or {}).get("version")
        )
        # True when a full conversation can be read for this agent (it may
        # still be empty before the first message).
        summary["history"] = bool(harness.supports_history)
        launch = self._agent_launch.get(harness.agent_id)
        if launch:
            summary["launch"] = dict(launch)
        if getattr(harness, "session_title", None):
            summary["session_title"] = harness.session_title
        if getattr(harness, "legacy", False):
            summary["legacy"] = True
        summary["interactions"] = [
            i.to_dict() for i in self.interactions.pending(harness.agent_id)
        ]
        # Best-effort usage from what the TUI shows; absent fields stay n/d.
        summary["usage"] = parse_usage(harness)
        summary["activity"] = self._activity(harness, summary)
        # OpenCode v2: which model/agent crewhall asked for and which is running
        # (the TUI footer, when the API could not be read back). Never silent.
        config = dict(self._v2_config.get(harness.agent_id) or {})
        if config:
            if not config.get("running_model"):
                config["running_model"] = (summary["activity"] or {}).get("model")
            summary["session_config"] = config
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
        """Change a running agent's model: the v2 API, or a direct command/picker."""
        harness = self.agents.resolve(target)
        name = (model or "").strip() or None
        link = getattr(harness, "link", None)
        if (link is not None and getattr(link, "api", "v1") == "v2"
                and harness.conversation_id):
            if not name:
                raise HarnessError(harness.agent_id, "a model name is required")
            report = self._configure_v2_session(
                harness.agent_id, harness.kind, link, ["--model", name],
                session_id=harness.conversation_id)
            if not report.get("model_applied"):
                raise HarnessError(
                    harness.agent_id,
                    f"the v2 server did not apply model {name!r} "
                    f"({report.get('error') or 'no read-back match'})")
            return self.agent_summary(harness)
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
        self._kill_opencode_server(self._opencode_servers.pop(agent_id, None))
        self._opencode_links.pop(agent_id, None)
        self._v2_config.pop(agent_id, None)
        self._agent_launch.pop(agent_id, None)
        self._mcp_cleanup(agent_id)
        self._cleanup_worktree(agent_id, force=force)
        self.interactions.forget_agent(agent_id)
        self._retired_conversations.pop(agent_id, None)
        # The per-agent TUI state (tabs/model/prefs) must not outlive the agent:
        # it contained the user's prompt history before the isolation fix.
        import shutil

        state_home = os.path.join(paths.state_dir(), "opencode-state", agent_id)
        shutil.rmtree(state_home, ignore_errors=True)
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
            args=self._agent_args.get(harness.agent_id, []),
            session_config=self._v2_config.get(harness.agent_id),
            launch=self._agent_launch.get(harness.agent_id),
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
        # The OpenCode v2 `serve` processes belong to the control plane, not to
        # tmux: always stop them here (a surviving agent is re-attached to a
        # fresh server on the same port when the daemon comes back, A1).
        for agent_id in list(self._opencode_servers):
            self._kill_opencode_server(self._opencode_servers.pop(agent_id, None))
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
