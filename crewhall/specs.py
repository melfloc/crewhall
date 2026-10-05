"""Declarative team files and reusable agent profiles.

``team.toml`` (or ``.json``)::

    [team]
    name = "frente1"
    workspace = "~/Projects/x"

    [[agent]]
    name = "orquestador"
    kind = "claude"
    args = "--agent orq"        # string or list
    profile = "revisor"          # optional defaults, see below

Profiles live in ``~/.config/crewhall/profiles.toml``::

    [profile.revisor]
    kind = "claude"
    args = "--agent reviewer"

Parsing happens client side; the daemon only receives the resulting plain dict.
"""
from __future__ import annotations

import json
import os
import tomllib
from typing import Any

from . import brand
from .types import parse_agent_args

AGENT_KEYS = {"name", "kind", "args", "cwd", "backend", "profile"}
PROFILE_KEYS = {"kind", "args", "cwd", "backend"}


class SpecError(ValueError):
    pass


def profiles_path() -> str:
    return os.path.join(brand.config_dir(), "profiles.toml")


def _load_file(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise SpecError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        if path.endswith(".json"):
            return json.loads(raw.decode("utf-8"))
        return tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SpecError(f"{path}: {exc}") from exc


def load_profiles(path: str | None = None) -> dict[str, dict[str, Any]]:
    path = path or profiles_path()
    if not os.path.exists(path):
        return {}
    return parse_profiles_data(_load_file(path))


def parse_profiles_data(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = data.get("profile", {})
    if not isinstance(profiles, dict):
        raise SpecError("profiles: [profile.<name>] tables expected")
    for name, body in profiles.items():
        if not isinstance(body, dict) or set(body) - PROFILE_KEYS:
            raise SpecError(
                f"profile {name!r}: allowed keys are {sorted(PROFILE_KEYS)}"
            )
        try:
            parse_agent_args(body.get("args"))
        except ValueError as exc:
            raise SpecError(f"profile {name!r}: {exc}") from exc
    return profiles


def apply_profile(
    entry: dict[str, Any], profiles: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Return ``entry`` with the profile's values filled in (entry wins)."""
    name = entry.get("profile")
    if not name:
        return dict(entry)
    if name not in profiles:
        raise SpecError(f"unknown profile {name!r}")
    merged = {**profiles[name], **{k: v for k, v in entry.items() if v is not None}}
    return merged


def load_team_spec(
    path: str, profiles: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Parse and validate a team file into ``{name, workspace, agents: [...]}``."""
    return parse_team_data(_load_file(path), os.path.dirname(os.path.abspath(path)), profiles)


def parse_team_data(
    data: dict[str, Any], base: str, profiles: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    profiles = load_profiles() if profiles is None else profiles
    team = data.get("team")
    if not isinstance(team, dict) or not team.get("name"):
        raise SpecError("[team] with a name is required")
    agents = data.get("agent", [])
    if not isinstance(agents, list) or not agents:
        raise SpecError("at least one [[agent]] is required")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(agents, 1):
        if not isinstance(raw, dict) or set(raw) - AGENT_KEYS:
            raise SpecError(f"agent #{i}: allowed keys are {sorted(AGENT_KEYS)}")
        entry = apply_profile(raw, profiles)
        name = entry.get("name")
        if not name or not isinstance(name, str):
            raise SpecError(f"agent #{i}: name is required")
        if name in seen:
            raise SpecError(f"duplicate agent name {name!r}")
        seen.add(name)
        entry["kind"] = entry.get("kind") or "opencode"
        try:
            entry["args"] = parse_agent_args(entry.get("args"))
        except ValueError as exc:
            raise SpecError(f"agent {name!r}: {exc}") from exc
        cwd = entry.get("cwd")
        if cwd:
            cwd = os.path.expanduser(cwd)
            entry["cwd"] = cwd if os.path.isabs(cwd) else os.path.join(base, cwd)
        entry.pop("profile", None)
        out.append(entry)
    workspace = team.get("workspace")
    if workspace:
        workspace = os.path.expanduser(workspace)
        if not os.path.isabs(workspace):
            workspace = os.path.join(base, workspace)
    return {"name": team["name"], "workspace": workspace, "agents": out}
