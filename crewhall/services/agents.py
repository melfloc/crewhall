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
        if server_url and kind == "opencode":
            # v2's TUI rejects --model/--agent (and any unknown flag) and exits;
            # the model is chosen with the picker or set through the API instead.
            extra_args = self._drop_flags(extra_args, ("--model", "-m", "--agent"))
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

    def _open_opencode_link(
        self, kind: str, extra_args: list[str], cwd: str,
    ) -> tuple[OpenCodeLink | None, Any]:
        """Create the link (and, for v2, start the private server).

        Returns ``(link, server_process)``. For v1 the server lives inside the
        TUI (``--port``); for v2 crewhall runs ``serve --port`` and the TUI
        connects with ``--server`` (the password is shared through the env).
        """
        if not self.track_conversations or not get_harness(kind).uses_local_server:
            return None, None
        if self._user_manages_opencode_server(extra_args):
            return None, None
        if self._opencode_api(kind) == "v2":
            password = secrets.token_urlsafe(24)
            port = free_port()
            proc = self._start_opencode_server(kind, cwd, port, password)
            if proc is None:
                return None, None  # degrade to screen-only state, honestly
            return OpenCodeLink(port, password, api="v2"), proc
        return OpenCodeLink(free_port(), secrets.token_urlsafe(24)), None

    def _reattach_opencode_link(self, harness: Harness, info: dict[str, Any], cwd: str) -> None:
        """Rebuild the link for an adopted agent after a daemon restart.

        v2: restart a ``serve`` on the same port/password (the surviving TUI
        reconnects to it). v1: the TUI still owns its server on that port.
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
            proc = self._start_opencode_server(harness.kind, cwd, port, password)
            if proc is None:
                return  # could not rebind: stay screen-only, honestly
            self._opencode_servers[harness.agent_id] = proc
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
    _V2_DROP_FLAGS = ("--model", "-m", "--agent")
    # Flags that pick the session themselves: with one of these the caller owns
    # the session, so crewhall does not create or reconfigure it up front.
    _V2_SESSION_FLAGS = ("--session", "-s", "--continue", "-c", "--session-id", "--resume", "-r")

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

    @staticmethod
    def _v2_model_ref(link: Any, model: str | None) -> dict[str, str]:
        """``--model provider/model`` -> a v2 ``Model.Ref`` (``{providerID, id}``).

        The CLI takes ``provider/model``; when only a model is given the server's
        default model supplies the provider. Returns ``{}`` when unusable.
        """
        text = (model or "").strip()
        if not text:
            return {}
        raw_provider, sep, raw_model = text.partition("/")
        provider: str | None = raw_provider if (sep and raw_provider and raw_model) else None
        model_id = raw_model if provider else text
        # The catalog is authoritative: it maps an alias (``opencode``) to the
        # canonical provider id (``opencode-go``) the server actually accepts.
        try:
            raw = link.get_json("/api/model", timeout=3.0)
            catalog = raw.get("data") if isinstance(raw, dict) else raw
        except Exception:  # noqa: BLE001 - the server may be busy or older
            catalog = None
        matches: list[dict[str, str]] = []
        for entry in catalog or []:
            if not isinstance(entry, dict):
                continue
            mid = str(entry.get("modelID") or entry.get("id") or "").strip()
            pid = str(entry.get("providerID") or "").strip()
            if not mid or not pid or model_id not in (mid, str(entry.get("id") or "")):
                continue
            matches.append({"providerID": pid, "id": mid,
                            "canonical": str(entry.get("canonical") or "").strip()})
        if matches:
            if provider:
                for match in matches:
                    if provider in (match["providerID"], match["canonical"]):
                        return {"providerID": match["providerID"], "id": match["id"]}
            return {"providerID": matches[0]["providerID"], "id": matches[0]["id"]}
        if provider:
            return {"providerID": provider, "id": model_id}
        try:
            raw = link.get_json("/api/model/default", timeout=3.0)
            data = raw.get("data") if isinstance(raw, dict) else None
            default_provider = str((data or {}).get("providerID") or "") or None
        except Exception:  # noqa: BLE001 - the server may be busy or older
            default_provider = None
        if not default_provider:
            return {}
        return {"providerID": default_provider, "id": model_id}

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

    def _configure_v2_session(
        self, agent_id: str, kind: str, link: Any, extra_args: list[str],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply ``--model``/``--agent`` to an OpenCode v2 session through the API.

        With ``session_id=None`` a fresh session is created first, so the model
        and agent are set before the TUI can use it; otherwise the switches are
        applied to the resumed session. Never raises: a failure is recorded as a
        notice (modal + desktop) and in the report, never silently.

        Returns the report (also stored in ``self._v2_config[agent_id]``).
        """
        model, agent = self._requested_v2_config(kind, extra_args)
        dropped = self._present_flags(extra_args, self._V2_DROP_FLAGS)
        report: dict[str, Any] = {
            "model": model, "running_model": None, "model_applied": None,
            "agent": agent, "agent_applied": None,
            "session_id": session_id, "ignored_flags": [], "error": None,
        }
        errors: list[str] = []
        if link is None or getattr(link, "api", "v1") != "v2":
            return report
        try:
            ref = self._v2_model_ref(link, model) if model else {}
            if model and not ref:
                raise RuntimeError(f"could not resolve model {model!r}")
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
                if model:
                    report["model_applied"] = True
            elif model:
                link.post_json(f"/api/session/{session_id}/model", {"model": ref})
                report["model_applied"] = True
            if agent:
                link.post_json(f"/api/session/{session_id}/agent", {"agent": agent})
                report["agent_applied"] = True
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
            if not getattr(link, "_api_detected", False):
                # Ask the running server whether it speaks v1 or v2; keep trying
                # until it answers (the port may not be open yet).
                try:
                    link.api = OpenCodeLink.detect_api(link.port, link.password)
                    link._api_detected = True
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
            return
        if kind == "session.created":
            sid = sid or data.get("id")
            if sid and not data.get("parentID"):
                self._adopt_conversation(harness, sid)
            elif sid:
                self._opencode_children.setdefault(harness.agent_id, set()).add(sid)
            return
        if sid and sid != harness.conversation_id and kind == "session.execution.started":
            # Activity in another session (the user switched): follow it.
            self._follow_opencode_session(harness, sid)
        if sid and sid in self._opencode_children.get(harness.agent_id, ()):
            return  # a subagent's turn is not the agent's turn
        if kind == "session.execution.started":
            last = self._hook_events.get(harness.agent_id) or {}
            if last.get("event") != "prompt_submit":
                self._apply_hook(harness, "prompt_submit")
        elif kind in ("session.execution.succeeded", "session.execution.failed",
                      "session.execution.interrupted"):
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
        # v2 drops --model/--agent from the TUI (it rejects them): crewhall creates
        # the session and applies them through the API, before the agent is usable.
        v2_session: str | None = None
        if link is not None and getattr(link, "api", "v1") == "v2" and not host:
            if self._v2_session_flags(extra_args):
                # The caller chose the session; the model/agent are applied when
                # that session is adopted (see ``_apply_pending_v2_config``).
                self._v2_config.setdefault(session_id, {})
            else:
                v2_session = self._configure_v2_session(
                    session_id, kind, link, extra_args, session_id=None,
                ).get("session_id")
                if v2_session:
                    conversation_id = v2_session
        agent_env = self._build_agent_env(session_id, agent_name, [], env, host)
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
        # True when a full conversation can be read for this agent (it may
        # still be empty before the first message).
        summary["history"] = bool(harness.supports_history)
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
        self._kill_opencode_server(self._opencode_servers.pop(agent_id, None))
        self._opencode_links.pop(agent_id, None)
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
