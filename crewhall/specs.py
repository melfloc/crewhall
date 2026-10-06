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
import posixpath
import tomllib
from typing import Any

from . import brand
from .types import parse_agent_args

AGENT_KEYS = {"name", "kind", "args", "cwd", "backend", "profile", "host"}
PROFILE_KEYS = {"kind", "args", "cwd", "backend", "host"}


class SpecError(ValueError):
    pass


def _check_host(value: Any) -> str | None:
    """Reject an unknown remote host early, client side."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SpecError("host must be a configured host name")
    from .settings import hosts

    if value not in hosts():
        raise SpecError(
            f"unknown host {value!r}; configure it under \"hosts\" in settings.json"
        )
    return value


def _remote_workspace(value: Any) -> str | None:
    if not value:
        return None
    if not isinstance(value, str) or not value.startswith("/") or any(c in value for c in "\0\n\r"):
        raise SpecError("team workspace: a remote workspace must be an absolute path on the host")
    return posixpath.normpath(value)


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
        try:
            _check_host(body.get("host"))
        except SpecError as exc:
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
    try:
        team_host = _check_host(team.get("host"))
    except SpecError as exc:
        raise SpecError(f"team host: {exc}") from exc
    remote_ws = _remote_workspace(team.get("workspace")) if team_host else None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(agents, 1):
        if not isinstance(raw, dict) or set(raw) - AGENT_KEYS:
            raise SpecError(f"agent #{i}: allowed keys are {sorted(AGENT_KEYS)}")
        entry = apply_profile(raw, profiles)
        if team_host and not entry.get("host"):
            entry["host"] = team_host  # a remote team's agents run on its host
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
        try:
            host = _check_host(entry.get("host"))
        except SpecError as exc:
            raise SpecError(f"agent {name!r}: {exc}") from exc
        entry["host"] = host
        if team_host and host != team_host:
            raise SpecError(
                f"agent {name!r}: team host is {team_host!r}, the agent asks for {host!r}"
            )
        if host:
            if entry.get("backend") == "pty":
                raise SpecError(
                    f"agent {name!r}: backend 'pty' cannot reach the remote host {host!r}"
                )
            if entry.get("backend") in (None, "", "auto"):
                entry["backend"] = "ssh-tmux"
        cwd = entry.get("cwd")
        if cwd and host and team_host:
            # Relative to the remote workspace; never resolved on this machine.
            if not str(cwd).startswith("/"):
                if not remote_ws:
                    raise SpecError(f"agent {name!r}: a relative cwd needs the team workspace")
                cwd = posixpath.join(remote_ws, cwd)
            entry["cwd"] = posixpath.normpath(cwd)
        elif cwd and not host:
            cwd = os.path.expanduser(cwd)
            entry["cwd"] = cwd if os.path.isabs(cwd) else os.path.join(base, cwd)
        # With a host, cwd is a path on the remote machine: never expand or
        # resolve it against the local filesystem.
        entry.pop("profile", None)
        out.append(entry)
    workspace = team.get("workspace")
    if team_host:
        workspace = remote_ws  # a path on the host: never expanded locally
    elif workspace:
        workspace = os.path.expanduser(workspace)
        if not os.path.isabs(workspace):
            workspace = os.path.join(base, workspace)
    return {"name": team["name"], "workspace": workspace, "host": team_host, "agents": out}
