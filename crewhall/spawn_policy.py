"""Daemon-side policy that lets named agents create/stop agents (default OFF).

``settings.json`` holds three top-level keys (edited by hand; never through the
settings API, which is control plane):

* ``agent_spawners`` — agent names allowed to create/stop agents (default ``[]``:
  the policy is off and agents get the usual control-plane refusal);
* ``agent_spawn_policy`` — ``{"profiles": [profile], "teams": [name],
  "max_live": int, "kinds": [kind]}``. A profile is ``{"name_pattern": regex,
  "kind": kind, "args": [argv token], "cwd": absolute path, "backend":
  tmux|pty}``: a request must send exactly that kind, argv list and cwd (or
  omit cwd) and no other backend, and the agent is launched with the
  profile's values. Anything malformed (no ``profiles``, an unknown key)
  makes the policy deny everything;
* ``agent_spawn_protected`` — names no spawner may create or stop. The names in
  :data:`ALWAYS_PROTECTED` and every spawner name are protected even if this
  key is edited.

The keys are read ONCE, when the daemon starts (:func:`load`): editing
settings.json afterwards changes nothing until the next daemon start.

The caller is the agent authenticated by its token *and* by the calling
process descending from that agent's own process tree. A spawner may create
only a profile-matching name, into a listed plain (local, non-worktree) team it
belongs to, up to ``max_live`` live agents of its own; it may stop only an
agent it created itself (``created_by``, persisted with the agent record). An
agent created by an agent never spawns, and reviving an exited one re-checks
its creator's ``max_live``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import unicodedata
from collections.abc import Iterator
from typing import Any

from . import settings
from .types import parse_agent_args

log = logging.getLogger(__name__)

ALWAYS_PROTECTED = ("Heraldo", "Orquestador", "Ejecutor_Sonnet_1")

# Serializes check+create/stop of agent-initiated requests, so two concurrent
# creates cannot both see a free ``max_live`` slot or the same free name.
LOCK = threading.RLock()

# The policy as read at daemon start (see :func:`load`).
_SNAPSHOT: dict[str, Any] | None = None
# Revives in flight, by creator id: a slot is reserved (not the lock held)
# while the agent starts, so a slow readiness wait cannot block every spawn.
_RESERVED: dict[str, int] = {}


def _fold(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold().strip()


def digest(raw: dict[str, Any]) -> str:
    """SHA-256 of the ``agent_spawn*`` keys as canonical JSON."""
    blob = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load() -> dict[str, Any]:
    """Read the policy from settings.json; the daemon calls this once at start."""
    global _SNAPSHOT
    raw = settings.agent_spawn()
    from . import sandbox

    sandbox_cfg = settings.sandbox_config()
    invalid = raw["invalid"]
    # Stage 2: a spawner must itself be sandboxed, or the token/tmux/proc
    # boundary the policy relies on does not exist for it.
    if not invalid:
        for name in raw["spawners"]:
            profile, error = sandbox.decision_from(sandbox_cfg, name)
            if error:
                invalid = error
                break
            if profile is None:
                invalid = f"spawner {name!r} is not sandboxed (opt it in under sandbox.agents)"
                break
    if not invalid:
        known = set(sandbox_cfg["profiles"])
        for profile in raw["profiles"]:
            if profile["sandbox"] not in known:
                invalid = (f"spawn profile {profile['name_pattern']!r} names unknown "
                           f"sandbox profile {profile['sandbox']!r}")
                break
    _SNAPSHOT = {**raw, "invalid": invalid, "sandbox_config": sandbox_cfg,
                 "sha256": digest(settings.spawn_policy_raw())}
    if invalid:
        log.error("agent spawn policy invalid: %s", invalid)
    return _SNAPSHOT


def _snapshot() -> dict[str, Any]:
    return _SNAPSHOT if _SNAPSHOT is not None else load()


def config() -> dict[str, Any]:
    """The effective policy (validated; defaults when absent)."""
    raw = _snapshot()
    protected = {_fold(n) for n in (*ALWAYS_PROTECTED, *raw["protected"], *raw["spawners"])}
    return {**raw, "protected": protected}


def enabled() -> bool:
    return bool(_snapshot()["spawners"])


def status() -> dict[str, Any]:
    """The snapshot the daemon is enforcing (in memory, never the audit log)."""
    cfg = _snapshot()
    return {"ok": True, "enabled": bool(cfg["spawners"]), "sha256": cfg["sha256"],
            "invalid": cfg["invalid"], "spawners": list(cfg["spawners"]),
            "max_live": cfg["max_live"]}


def audit_decision(op: str, who: str, target: Any, reason: str | None,
                   *, authenticated: bool = True) -> None:
    """The one ``agent_spawn_policy`` audit line of a request (its final decision)."""
    from . import audit

    decision = "denied" if reason else "allowed"
    # The name is capped inside the reason (N8) but the raw target goes to the
    # bounded ``extra``: audit.record clips it and marks the line ``truncated``.
    audit.record(
        "agent_spawn_policy", actor=who, result=decision,
        summary=f"{op} {str(target)[:64]!r}: {(reason or 'allowed')[:audit.MAX_FIELD]}",
        extra={"caller": who, "request_op": op, "target": str(target),
               "decision": decision, "reason": reason or ""},
        unauthenticated=not authenticated,
    )


def _is_spawner(cfg: dict[str, Any], caller: Any) -> bool:
    return caller.name in cfg["spawners"]


def _created_by(controller: Any) -> dict[str, str]:
    return getattr(controller, "_agent_created_by", {})


def _alive(harness: Any) -> bool:
    status = getattr(getattr(harness, "session", None), "status", None)
    return bool(getattr(status, "alive", False))


def live_created(controller: Any, caller_id: str, *, include_reserved: bool = True,
                 exclude: str | None = None) -> int:
    """Agents ``caller_id`` created whose process is still alive (+ reservations)."""
    owners = _created_by(controller)
    count = sum(1 for h in controller.agents.all()
                if h.agent_id != exclude and owners.get(h.agent_id) == caller_id
                and _alive(h))
    if include_reserved:
        with LOCK:
            count += _RESERVED.get(caller_id, 0)
    return count


def adoption_error(controller: Any, owner_id: str | None, agent_id: str,
                   persisted_status: str | None) -> str | None:
    """N3: may a live tmux pane for ``agent_id`` be adopted after a restart?

    A persisted agent that was already EXITED must not come back because some
    same-uid process ran ``tmux respawn-pane`` on its dead pane. A spawn-created
    agent that *was* alive is adopted only under its creator's ``max_live``.
    """
    if persisted_status is not None and persisted_status not in ("starting", "running"):
        return (f"persisted status was {persisted_status!r}; a live pane is a "
                "respawn, not the agent it claims to be")
    if owner_id is None:
        return None
    cfg = config()
    if not cfg["spawners"] or cfg["invalid"]:
        return None
    limit = cfg["max_live"]
    if live_created(controller, owner_id, exclude=agent_id) >= limit:
        return f"max_live ({limit}) reached for its creator"
    return None


def _sandbox_decision(cfg: dict[str, Any], name: str | None) -> tuple[str | None, str | None]:
    from . import sandbox

    sandbox_cfg = cfg.get("sandbox_config")
    if sandbox_cfg is None:
        from . import settings

        sandbox_cfg = settings.sandbox_config()
    return sandbox.decision_from(sandbox_cfg, name)


def _profile_for(cfg: dict[str, Any], request: dict[str, Any]) -> tuple[dict | None, str | None]:
    """The profile this request matches exactly, or the reason it matches none."""
    name = request["name"]
    matching = [p for p in cfg["profiles"] if re.fullmatch(p["name_pattern"], name)]
    if not matching:
        return None, f"name {name!r} does not match any profile"
    try:
        args = parse_agent_args(request.get("args"))
    except ValueError:
        return None, "args/cwd/backend not allowed for profile"
    cwd = request.get("cwd")
    backend = request.get("backend")
    sandbox_profile, sandbox_error = _sandbox_decision(cfg, name)
    for profile in matching:
        if (profile["kind"] == request.get("kind") and args == profile["args"]
                and (cwd is None or cwd == profile["cwd"])
                and backend in (None, "auto", profile["backend"])):
            if sandbox_error:
                return None, sandbox_error
            if sandbox_profile != profile["sandbox"]:
                return None, (
                    f"name {name!r} resolves to sandbox profile {sandbox_profile!r}, "
                    f"not {profile['sandbox']!r}")
            return profile, None
    return None, "args/cwd/backend not allowed for profile"


def launch_values(request: dict[str, Any]) -> dict[str, Any]:
    """kind/args/cwd/backend/workspace_mode/sandbox to launch an allowed request with.

    They come from the matching profile, never the request.
    """
    profile, reason = _profile_for(config(), request)
    if profile is None:
        raise ValueError(reason)
    return {"kind": profile["kind"], "args": list(profile["args"]), "cwd": profile["cwd"],
            "backend": profile["backend"], "workspace_mode": "shared",
            "sandbox": profile["sandbox"]}


def check_create(controller: Any, caller: Any, request: dict[str, Any]) -> str | None:
    """None if ``caller`` may create this agent, else the reason it may not."""
    cfg = config()
    if cfg["invalid"]:
        return f"policy invalid: {cfg['invalid']}"
    if not _is_spawner(cfg, caller):
        return f"{caller.name or caller.agent_id} is not in agent_spawners"
    sandbox_profile, sandbox_error = _sandbox_decision(cfg, caller.name)
    if sandbox_error:
        return sandbox_error
    if sandbox_profile is None:
        return f"{caller.name} is not sandboxed"
    if _created_by(controller).get(caller.agent_id):
        return f"{caller.name} was created by an agent and cannot spawn"
    kind = request.get("kind")
    if kind not in cfg["kinds"]:
        return f"kind {kind!r} is not allowed"
    if request.get("conversation") or request.get("host"):
        return "agents created by an agent cannot target a chat or a remote host"
    name = request.get("name")
    if not isinstance(name, str) or not name:
        return "a name is required"
    if not name.isascii() or not name.isprintable() or name != name.strip():
        return f"name {name!r} has non-ASCII, non-printable or surrounding blank characters"
    if _fold(name) in cfg["protected"]:
        return f"name {name!r} is protected"
    reason = _profile_for(cfg, request)[1]
    if reason:
        return reason
    if any(_fold(h.name or "") == _fold(name) for h in controller.agents.all()):
        return f"name {name!r} is already taken"
    team = controller._team_or_none(request.get("team"))
    if team is None or team.name not in cfg["teams"]:
        return f"team {request.get('team')!r} is not allowed"
    if caller.agent_id not in team.agent_ids:
        return f"{caller.name} is not a member of team {team.name!r}"
    if team.host or team.workspace_mode == "worktree":
        return f"team {team.name!r} has a remote host or worktree mode"
    if live_created(controller, caller.agent_id) >= cfg["max_live"]:
        return f"max_live ({cfg['max_live']}) reached"
    return None


def check_stop(controller: Any, caller: Any, request: dict[str, Any]) -> str | None:
    """None if ``caller`` may stop this agent, else the reason it may not."""
    cfg = config()
    if cfg["invalid"]:
        return f"policy invalid: {cfg['invalid']}"
    if not _is_spawner(cfg, caller):
        return f"{caller.name or caller.agent_id} is not in agent_spawners"
    try:
        target = controller.agents.resolve(str(request.get("target") or ""))
    except Exception as exc:  # noqa: BLE001 - unknown/ambiguous target: deny
        return f"unknown agent: {exc}"
    if target.agent_id == caller.agent_id:
        return "an agent cannot stop itself"
    if _fold(target.name or "") in cfg["protected"]:
        return f"{target.name} is protected"
    if _created_by(controller).get(target.agent_id) != caller.agent_id:
        return f"{target.name or target.agent_id} was not created by {caller.name}"
    return None


@contextlib.contextmanager
def revive_guard(controller: Any, harness: Any) -> Iterator[None]:
    """An exited agent another agent created comes back only under ``max_live``.

    The policy lock is held only for the check and the reservation; the
    readiness wait runs after it, with the slot counted in ``_RESERVED`` (N7).
    With the policy off or invalid the human keeps full restart control (N4).
    """
    owner = _created_by(controller).get(harness.agent_id)
    if owner is None or _alive(harness):
        yield
        return
    from .messaging import MessagingError

    cfg = config()
    if not cfg["spawners"] or cfg["invalid"]:
        yield
        return
    limit = cfg["max_live"]
    with LOCK:
        if live_created(controller, owner) >= limit:
            raise MessagingError(
                f"agent spawn policy: max_live ({limit}) reached for the creator of "
                f"{harness.name or harness.agent_id}")
        _RESERVED[owner] = _RESERVED.get(owner, 0) + 1
    try:
        yield
    finally:
        with LOCK:
            left = _RESERVED.get(owner, 0) - 1
            if left > 0:
                _RESERVED[owner] = left
            else:
                _RESERVED.pop(owner, None)
