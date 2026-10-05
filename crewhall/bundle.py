"""Portable configuration bundle: move your setup to another machine.

``crewhall bundle export out.tar.gz`` packs *configuration* (profiles and
team files, optionally the persisted teams/agents state and the web token) with
a manifest of SHA-256 sums. ``bundle import`` restores it safely: only a fixed
allow-list of destinations is ever written, nothing is overwritten without
``--force`` (and then a ``.bak`` is kept), and the archive is verified first.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import time
from typing import Any

from . import __version__, brand, paths

MANIFEST = "manifest.json"
FORMAT = 1


class BundleError(ValueError):
    pass


def config_dir() -> str:
    return brand.config_dir()


def _destinations() -> dict[str, str]:
    """Archive member prefix -> directory it is restored into."""
    return {"config": config_dir(), "state": paths.state_dir()}


def _allowed(member: str) -> bool:
    """Fixed allow-list of archive members (no free-form paths)."""
    parts = member.split("/")
    if len(parts) < 2 or any(p in ("", ".", "..") for p in parts) or member.startswith("/"):
        return False
    if member in ("config/profiles.toml", "config/web-token", "state/state.json"):
        return True
    if parts[0] == "config" and parts[1] == "teams" and len(parts) == 3:
        return parts[2].endswith((".toml", ".json"))
    return False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-").lower()[:60] or "team"


def live_team_files(teams: list[dict[str, Any]]) -> dict[str, bytes]:
    """Team definitions (``team_up`` specs) for the teams/agents running now.

    ``teams``: ``[{name, workspace, agents: [{name, kind, backend, cwd, args}]}]``.
    Only what is needed to rebuild the setup: no ids, pids, sessions or
    conversations. Teams without members are skipped. A team file already on
    disk with the same name wins (it is already exported as a file).
    """
    out: dict[str, bytes] = {}
    teams_dir = os.path.join(config_dir(), "teams")
    for team in teams:
        agents = [a for a in team.get("agents", []) if a.get("name")]
        if not agents:
            continue
        slug = _slug(team["name"])
        if any(os.path.exists(os.path.join(teams_dir, slug + ext)) for ext in (".toml", ".json")):
            continue
        spec = {"team": {k: v for k, v in (("name", team["name"]), ("workspace", team.get("workspace"))) if v},
                "agent": [{k: v for k, v in a.items() if v not in (None, "", [])} for a in agents]}
        out[f"config/teams/{slug}.json"] = json.dumps(spec, indent=2, ensure_ascii=False).encode()
    return out


def bundle_team_specs(files: dict[str, bytes]) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse the team files of a bundle into ``team_up`` specs: ``(specs, errors)``."""
    import tomllib

    from . import specs as specmod

    out, errors = [], []
    try:
        profiles = specmod.parse_profiles_data(tomllib.loads(files["config/profiles.toml"].decode())) \
            if "config/profiles.toml" in files else specmod.load_profiles()
    except Exception as exc:  # noqa: BLE001
        profiles, errors = {}, [f"profiles: {exc}"]
    for name in sorted(files):
        if not name.startswith("config/teams/"):
            continue
        try:
            raw = files[name].decode("utf-8")
            data = json.loads(raw) if name.endswith(".json") else tomllib.loads(raw)
            out.append(specmod.parse_team_data(data, os.path.expanduser("~"), profiles))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    return out, errors


def plan_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per team, per agent: whether its directory exists on this machine."""
    plan = []
    for spec in specs:
        agents = []
        for a in spec["agents"]:
            cwd = a.get("cwd") or spec.get("workspace")
            agents.append({"name": a["name"], "kind": a.get("kind"), "cwd": cwd,
                           "cwd_ok": not cwd or os.path.isdir(os.path.expanduser(cwd))})
        plan.append({"team": spec["name"], "workspace": spec.get("workspace"), "agents": agents})
    return plan


def apply_specs(specs: list[dict[str, Any]], team_up) -> dict[str, Any]:
    """Recreate teams/agents through ``team_up(spec)``; agents whose directory is
    missing here are skipped (and reported) instead of being started somewhere else."""
    created, existing, skipped, failed = [], [], [], []
    for spec in specs:
        usable, ws = [], spec.get("workspace")
        for a in spec["agents"]:
            cwd = a.get("cwd") or ws
            if cwd and not os.path.isdir(os.path.expanduser(cwd)):
                skipped.append({"agent": a["name"], "team": spec["name"], "reason": f"directory not found: {cwd}"})
            else:
                usable.append(a)
        if not usable:
            continue
        try:
            res = team_up({**spec, "agents": usable})
        except Exception as exc:  # noqa: BLE001
            failed.append({"team": spec["name"], "error": str(exc)})
            continue
        created += [f"{spec['name']}/{n}" for n in res.get("created", [])]
        existing += [f"{spec['name']}/{n}" for n in res.get("existing", [])]
    return {"created": created, "existing": existing, "skipped": skipped, "failed": failed}


def collect(*, with_state: bool = False, with_token: bool = False,
            live_teams: list[dict[str, Any]] | None = None) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    cfg = config_dir()
    profiles = os.path.join(cfg, "profiles.toml")
    if os.path.isfile(profiles):
        files["config/profiles.toml"] = open(profiles, "rb").read()
    teams = os.path.join(cfg, "teams")
    if os.path.isdir(teams):
        for name in sorted(os.listdir(teams)):
            if name.endswith((".toml", ".json")) and os.path.isfile(os.path.join(teams, name)):
                files[f"config/teams/{name}"] = open(os.path.join(teams, name), "rb").read()
    if live_teams:
        files.update(live_team_files(live_teams))
    if with_state:
        state = os.path.join(paths.state_dir(), "state.json")
        if os.path.isfile(state):
            files["state/state.json"] = open(state, "rb").read()
    if with_token:
        token = os.path.join(cfg, "web-token")
        if os.path.isfile(token):
            files["config/web-token"] = open(token, "rb").read()
    return files


def export_bundle(dest: str, *, with_state: bool = False, with_token: bool = False,
                  live_teams: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    files = collect(with_state=with_state, with_token=with_token, live_teams=live_teams)
    manifest = {
        "format": FORMAT,
        "version": __version__,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": {name: _sha(data) for name, data in files.items()},
    }
    tmp = dest + ".part"
    with tarfile.open(tmp, "w:gz") as tar:
        def add(name: str, data: bytes, mode: int) -> None:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            tar.addfile(info, io.BytesIO(data))

        add(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True).encode(), 0o644)
        for name, data in files.items():
            add(name, data, 0o600)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    return {"path": dest, "files": sorted(files), "with_token": with_token, "with_state": with_state}


def read_bundle(src: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read and fully verify a bundle (manifest, allow-list, checksums)."""
    try:
        tar = tarfile.open(src, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise BundleError(f"cannot read {src}: {exc}") from exc
    files: dict[str, bytes] = {}
    manifest: dict[str, Any] | None = None
    with tar:
        for member in tar.getmembers():
            if not member.isfile():
                raise BundleError(f"unexpected entry type: {member.name}")
            if member.size > 4 * 1024 * 1024:
                raise BundleError(f"entry too large: {member.name}")
            data = tar.extractfile(member).read()  # type: ignore[union-attr]
            if member.name == MANIFEST:
                try:
                    manifest = json.loads(data.decode("utf-8"))
                except ValueError as exc:
                    raise BundleError(f"bad manifest: {exc}") from exc
            elif _allowed(member.name):
                files[member.name] = data
            else:
                raise BundleError(f"refusing unexpected path in bundle: {member.name!r}")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BundleError("missing or unsupported manifest")
    declared = manifest.get("files", {})
    if set(declared) != set(files):
        raise BundleError("manifest does not match archive contents")
    for name, data in files.items():
        if declared[name] != _sha(data):
            raise BundleError(f"checksum mismatch: {name}")
    return manifest, files


def import_bundle(src: str, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    manifest, files = read_bundle(src)
    dests = _destinations()
    written, skipped, backed_up = [], [], []
    for name, data in sorted(files.items()):
        prefix, rest = name.split("/", 1)
        target = os.path.join(dests[prefix], rest)
        exists = os.path.exists(target)
        if exists and not force:
            if open(target, "rb").read() == data:
                skipped.append(f"{name} (identical)")
            else:
                skipped.append(f"{name} (exists; use --force)")
            continue
        if dry_run:
            written.append(name)
            continue
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        if exists:
            shutil.copy2(target, target + ".bak")
            backed_up.append(name)
        fd = os.open(target + ".part", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(target + ".part", target)
        written.append(name)
    return {"written": written, "skipped": skipped, "backed_up": backed_up,
            "dry_run": dry_run, "source_version": manifest.get("version")}


# -- the server-side bundles directory (download / upload / delete) ----------
MAX_UPLOAD = 8 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.tar\.gz$")


def bundles_dir() -> str:
    return os.path.join(paths.state_dir(), "bundles")


def resolve_name(name: str) -> str:
    """Absolute path of an existing bundle in the bundles directory (no traversal)."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise BundleError("invalid bundle name")
    root = os.path.realpath(bundles_dir())
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root or not os.path.isfile(path):
        raise BundleError("no such bundle")
    return path


def describe(path: str) -> dict[str, Any]:
    """What a stored bundle holds, so junk (empty / corrupt) is visible in the list."""
    try:
        manifest, files = read_bundle(path)
    except BundleError as exc:
        return {"valid": False, "error": str(exc), "files": 0, "teams": 0}
    teams = [n for n in files if n.startswith("config/teams/")]
    return {"valid": True, "files": len(files), "teams": len(teams), "empty": not files,
            "has_token": "config/web-token" in files, "version": manifest.get("version"),
            "created": manifest.get("created")}


def delete_bundle(name: str) -> str:
    path = resolve_name(name)
    os.unlink(path)
    return path


def store_upload(filename: str, data: bytes) -> str:
    """Validate an uploaded bundle and keep it under a fresh name; returns that name."""
    if not data or len(data) > MAX_UPLOAD:
        raise BundleError("empty or too large bundle")
    os.makedirs(bundles_dir(), mode=0o700, exist_ok=True)
    tmp = os.path.join(bundles_dir(), f".upload-{os.getpid()}-{int(time.time() * 1000)}.part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        read_bundle(tmp)  # manifest, allow-list and checksums, before it is ever listed
        base = _slug(re.sub(r"\.tar\.gz$", "", os.path.basename(filename or ""))) or "bundle"
        name = f"uploaded-{time.strftime('%Y%m%d-%H%M%S')}-{base}.tar.gz"
        os.replace(tmp, os.path.join(bundles_dir(), name))
        return name
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
