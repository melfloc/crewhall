"""Daemon-side policy that lets named agents create/stop agents (default OFF).

``settings.json`` holds three top-level keys (edited by hand; never through the
settings API, which is control plane):

* ``agent_spawners`` — agent names allowed to create/stop agents (default ``[]``:
  the policy is off and agents get the usual control-plane refusal);
* ``agent_spawn_policy`` — ``{"profiles": [profile], "teams": [name],
  "max_live": int, "kinds": [kind]}``. A profile is ``{"name_pattern": regex,
  "kind": kind, "args": [argv token], "cwd": absolute path}``: a request must
  send exactly that kind, argv list and cwd (or omit cwd), and the agent is
  launched with the profile's values. Legacy ``name_patterns: [regex]`` (the
  request chooses args/cwd) is used only when ``profiles`` is absent;
* ``agent_spawn_protected`` — names no spawner may create or stop. The names in
  :data:`ALWAYS_PROTECTED` are protected even if this key is edited.

The caller is the agent authenticated by its token (never a request field). A
spawner may create only a pattern-matching name, into a listed team it belongs
to, up to ``max_live`` live agents of its own (exited ones do not count); it may stop only an agent it
created itself (``created_by``, persisted with the agent record).
"""
from __future__ import annotations

import re
import threading
import unicodedata
from typing import Any

from . import settings
from .types import parse_agent_args

ALWAYS_PROTECTED = ("Heraldo", "Orquestador", "Ejecutor_Sonnet_1")

# Serializes check+create/stop of agent-initiated requests, so two concurrent
# creates cannot both see a free ``max_live`` slot or the same free name.
LOCK = threading.Lock()


def _fold(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold().strip()


def config() -> dict[str, Any]:
    """The effective policy (validated; defaults when absent)."""
    raw = settings.agent_spawn()
    protected = {_fold(n) for n in (*ALWAYS_PROTECTED, *raw["protected"])}
    return {**raw, "protected": protected}


def enabled() -> bool:
    return bool(settings.agent_spawn()["spawners"])


def _is_spawner(cfg: dict[str, Any], caller: Any) -> bool:
    return caller.name in cfg["spawners"]


def _created_by(controller: Any) -> dict[str, str]:
    return getattr(controller, "_agent_created_by", {})


def _alive(harness: Any) -> bool:
    status = getattr(getattr(harness, "session", None), "status", None)
    return bool(getattr(status, "alive", False))


def live_created(controller: Any, caller_id: str) -> int:
    """Agents ``caller_id`` created whose process is still alive."""
    owners = _created_by(controller)
    return sum(1 for h in controller.agents.all()
               if owners.get(h.agent_id) == caller_id and _alive(h))


def _profile_for(cfg: dict[str, Any], request: dict[str, Any]) -> tuple[dict | None, str | None]:
    """The profile this request matches exactly, or the reason it matches none."""
    name = request["name"]
    matching = [p for p in cfg["profiles"] if re.fullmatch(p["name_pattern"], name)]
    if not matching:
        return None, f"name {name!r} does not match any profile"
    try:
        args = parse_agent_args(request.get("args"))
    except ValueError:
        return None, "args/cwd not allowed for profile"
    cwd = request.get("cwd")
    for profile in matching:
        if (profile["kind"] == request.get("kind") and args == profile["args"]
                and (cwd is None or cwd == profile["cwd"])):
            return profile, None
    return None, "args/cwd not allowed for profile"


def launch_values(request: dict[str, Any]) -> dict[str, Any]:
    """kind/args/cwd/workspace_mode to launch an allowed request with.

    With profiles they come from the matching profile, never the request.
    """
    cfg = config()
    if cfg["profiles"] is None:
        return {k: request.get(k) for k in ("kind", "args", "cwd", "workspace_mode")}
    profile, reason = _profile_for(cfg, request)
    if profile is None:
        raise ValueError(reason)
    return {"kind": profile["kind"], "args": list(profile["args"]), "cwd": profile["cwd"],
            "workspace_mode": None}


def check_create(controller: Any, caller: Any, request: dict[str, Any]) -> str | None:
    """None if ``caller`` may create this agent, else the reason it may not."""
    cfg = config()
    if not _is_spawner(cfg, caller):
        return f"{caller.name or caller.agent_id} is not in agent_spawners"
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
    if cfg["profiles"] is not None:
        reason = _profile_for(cfg, request)[1]
        if reason:
            return reason
    elif not any(re.fullmatch(p, name) for p in cfg["name_patterns"]):
        return f"name {name!r} does not match name_patterns"
    if any(_fold(h.name or "") == _fold(name) for h in controller.agents.all()):
        return f"name {name!r} is already taken"
    team = controller._team_or_none(request.get("team"))
    if team is None or team.name not in cfg["teams"]:
        return f"team {request.get('team')!r} is not allowed"
    if caller.agent_id not in team.agent_ids:
        return f"{caller.name} is not a member of team {team.name!r}"
    if live_created(controller, caller.agent_id) >= cfg["max_live"]:
        return f"max_live ({cfg['max_live']}) reached"
    return None


def check_stop(controller: Any, caller: Any, request: dict[str, Any]) -> str | None:
    """None if ``caller`` may stop this agent, else the reason it may not."""
    cfg = config()
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
