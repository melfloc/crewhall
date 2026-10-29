"""Daemon ops: system (mixins composed by :class:`crewhall.daemon.Server`)."""
from __future__ import annotations

# File-wide F401: each mixin keeps the daemon's name surface so a moved
# handler needs no import surgery; ruff's unused-import check is not useful here.
# ruff: noqa: F401
import os
import threading
import time
from typing import Any
from .. import paths, protocol, settings
from ..session import SessionTimeout
from ..types import SessionSpec
from ..ui.control import meta_info
from .common import OpsBase, _dim, log


class OpsSystem(OpsBase):

    def _op_ping(self, request: dict[str, Any]) -> dict[str, Any]:
        from ..buildinfo import build_info

        info = build_info()
        return {"pid": os.getpid(), "sessions": len(self.controller.registry.all()),
                "version": info["version"], "build": info.get("commit"),
                "channel": info["kind"], "protocol": protocol.PROTOCOL_VERSION}

    def _op_health(self, request: dict[str, Any]) -> dict[str, Any]:
        """Background-worker, queue and memory health (surfaced by `doctor`)."""
        from .. import health

        return {
            "workers": self.controller.workers.snapshot(),
            "queues": self.controller.messaging.queue_depth(),
            "pid": os.getpid(),
            **health.memory_fields(),
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

    def _op_host_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"hosts": self.controller.host_status()}

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

    def _op_interaction_list(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"interactions": self.controller.list_interactions(request.get("target"))}

    def _op_interaction_respond(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"interaction": self.controller.respond_interaction(
            str(request["id"]), request.get("answer"), by=str(request.get("by") or "user")[:40],
        )}

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

    def _op_bundle_export(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import bundle

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
                from .. import bundle

                bundles.append({"name": name, "bytes": st.st_size, "mtime": st.st_mtime,
                                **bundle.describe(full)})
        return {"bundles": bundles, "directory": dest_dir}

    def _op_bundle_delete(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import bundle

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
        from .. import bundle

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

    def _op_settings_get(self, request: dict[str, Any]) -> dict[str, Any]:
        return settings.describe()

    def _op_settings_set(self, request: dict[str, Any]) -> dict[str, Any]:
        if "hosts" in (request.get("changes") or {}):
            self._need_confirm(request, "changing hosts")
        try:
            result = settings.patch(
                request.get("changes") or {},
                confirm=request.get("confirm") == "CONFIRM",
            )
        except settings.SettingsError as exc:
            raise ValueError(str(exc)) from exc
        try:  # a provider command change can flip a CLI in/out of range
            self.controller.check_provider_versions()
        except Exception:  # noqa: BLE001 - a version check must not fail the save
            log.debug("provider version check after settings_set failed", exc_info=True)
        return result

    def _op_settings_reset(self, request: dict[str, Any]) -> dict[str, Any]:
        return settings.reset(request.get("prefix") or None)

    def _op_provider_check(self, request: dict[str, Any]) -> dict[str, Any]:
        kinds = [request["kind"]] if request.get("kind") else settings._kinds()
        return {"providers": [settings.check_provider(k, request.get("command") if len(kinds) == 1 else None)
                              for k in kinds]}

    def _op_provider_notices(self, request: dict[str, Any]) -> dict[str, Any]:
        """Notices crewhall raised on its own (e.g. an automatic CLI switch)."""
        from .. import notices

        return {"notices": notices.list_all()}

    def _op_notice_ack(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import notices

        return {"notices": notices.ack(request.get("id"))}

    def _op_reset_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import reset

        return reset.plan(self.controller, str(request.get("level") or ""))

    def _op_reset_apply(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import reset

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
        from .. import uploads

        return uploads.info()

    def _op_fs_complete(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import fscomplete

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
        from .. import updater

        return updater.status()

    def _op_clean_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import clean

        plan = clean.plan_cleanup(keep_backups=int(request.get("keep_backups", settings.get("maintenance.keep_backups"))),
                                  max_age_days=request.get("max_age_days")).to_dict()
        plan["worktrees"] = clean.list_worktrees()
        return plan

    def _op_clean_apply(self, request: dict[str, Any]) -> dict[str, Any]:
        from .. import clean

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
