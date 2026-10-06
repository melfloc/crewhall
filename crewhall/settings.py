"""User settings: one validated JSON file the daemon applies live.

``~/.config/crewhall/settings.json`` (mode 0600, written atomically). Every
key has a default, a type and bounds; unknown or invalid values are ignored on
load and rejected on write, so a bad edit can never stop the daemon from
starting. Environment variables that already existed (``CREWHALL_HOOKS``…)
still win over the file and the UI says so.

The schema below drives both validation and the Settings panel of the Web UI.
Providers are the harness adapters this build can drive (Claude Code,
OpenCode): each can be enabled/disabled, pointed at another binary, given
default arguments / model and extra environment variables.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
from typing import Any

from . import brand

log = logging.getLogger("crewhall.settings")

FILENAME = "settings.json"
SENSITIVE = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
MASK = "••••••••"


class SettingsError(ValueError):
    pass


def config_dir() -> str:
    return brand.config_dir()


def path() -> str:
    return os.path.join(config_dir(), FILENAME)


def _kinds() -> list[str]:
    from .harness import available_harnesses

    return available_harnesses()


def _backends() -> list[str]:
    from .backends import available

    # ssh-tmux is only valid together with a configured host, so it is not a
    # selectable *default* backend.
    return ["auto", *(b for b in available() if b != "ssh-tmux")]


# (key, group, type, default, label, help, extras)
def _static_schema() -> list[dict[str, Any]]:
    def s(key, group, typ, default, label, help_="", **extra):
        return {"key": key, "group": group, "type": typ, "default": default, "label": label,
                "help": help_, **extra}

    return [
        s("agents.default_kind", "agents", "choice", "opencode", "Default provider",
          "Preselected when you create an agent.", choices=_kinds),
        s("agents.default_backend", "agents", "choice", "auto", "Default backend",
          "tmux keeps agents alive and attachable; pty is lighter.", choices=_backends),
        s("agents.hooks", "agents", "bool", True, "Lifecycle hooks",
          "Lets agents report state and permission requests. Applies to agents created afterwards.",
          env="CREWHALL_HOOKS", restart=True),
        s("agents.conversations", "agents", "bool", True, "Read conversations",
          "Enables the Conversation tab (Claude transcripts / OpenCode server).",
          env="CREWHALL_CONVERSATIONS", restart=True),
        s("agents.control_files", "agents", "choice", "append", "Instruction files",
          "append: add the crewhall section to CLAUDE.md/AGENTS.md; off: never touch them.",
          choices=["append", "off"], env="CREWHALL_CONTROL_FILES"),
        s("agents.permission_wait", "agents", "int", 90, "Permission wait (s)",
          "How long a permission request waits for your answer before the agent's own prompt takes over (0 = never wait).",
          min=0, max=3600, env="CREWHALL_PERMISSION_WAIT"),
        s("security.session_ttl_hours", "security", "int", 12, "Web session lifetime (hours)",
          "After this, a browser must sign in with the token again. Applies to new sessions.",
          min=1, max=720),
        s("security.allow_hosts", "security", "list", [], "Extra allowed hostnames",
          "Hostnames/IPs the Web UI answers to besides localhost and the Tailscale address "
          "(one per line). Takes effect when the Web UI mode is applied again.", restart=True),
        s("security.local_requires_token", "security", "bool", False, "Require the token locally too",
          "When on, even 127.0.0.1 must sign in with the access token. Recommended if other people "
          "use this machine.", restart=True),
        s("security.fs_roots", "security", "paths", ["~"], "Directory suggestion roots",
          "Only directories under these roots are offered by the workspace autocomplete "
          "(one absolute path or ~ per line). Team workspaces and running agents' directories "
          "are always allowed.", restart=False),
        s("requests.max_open_per_agent", "requests", "int", 2, "Open requests per agent",
          "How many requests an agent may have waiting for a reply at once.", min=1, max=10),
        s("requests.max_depth", "requests", "int", 4, "Max request chain depth",
          "Stops A→B→C→… request loops from growing without bound.", min=1, max=10),
        s("requests.default_timeout_s", "requests", "int", 300, "Default request timeout (s)",
          "How long a request waits for its reply before it expires.", min=10, max=86400),
        s("requests.max_body_chars", "requests", "int", 8000, "Max request/reply size (chars)",
          "Requests and replies longer than this are rejected.", min=256, max=65536),
        s("maintenance.tmp_max_age_minutes", "maintenance", "int", 60, "Temp files: max age (min)",
          "Agent temp files older than this are removed by the janitor.", min=5, max=10080),
        s("maintenance.tmp_max_mb", "maintenance", "int", 512, "Temp dir: size cap (MB)",
          "The oldest temp files go first when the dedicated temp directory exceeds this.", min=64, max=65536),
        s("maintenance.janitor_minutes", "maintenance", "int", 15, "Janitor interval (min)",
          "How often the temp directory is pruned. Applies after a daemon restart.", min=1, max=1440,
          restart=True),
        s("maintenance.keep_backups", "maintenance", "int", 5, "Backups to keep",
          "Pre-update and pre-reset backups kept in the state directory.", min=1, max=100),
        s("audit.retention_days", "audit", "int", 90, "Audit retention (days)",
          "Audit entries older than this are pruned as new ones are written (0 = keep forever).",
          min=0, max=3650),
        s("terminals.enabled", "terminals", "bool", False, "Web terminals",
          "Exposes raw interactive shells in the Web UI and CLI. Off by default; "
          "enabling it is privileged and needs typed confirmation."),
        s("terminals.max_total", "terminals", "int", 8, "Max terminals (total)",
          "Hard cap on terminals that may exist at once.", min=1, max=32),
        s("terminals.max_per_host", "terminals", "int", 4, "Max terminals per host",
          "Cap per host; local counts as one host.", min=1, max=16),
        s("terminals.max_per_token", "terminals", "int", 4, "Max terminals per token",
          "Cap for terminals created with the same terminal token.", min=1, max=16),
        s("terminals.master_grants", "terminals", "bool", False, "Master session can use terminals",
          "On: the master Web UI session gets terminal access without a separate terminal token "
          "(a leaked master token would then open shells). Enabling is privileged."),
        s("terminals.keep_exited_seconds", "terminals", "int", 300, "Keep exited terminals (s)",
          "Terminals whose shell has exited are closed and removed after this (0 = keep forever).",
          min=0, max=86400),
        s("terminals.allow_no_origin", "terminals", "bool", False, "Allow missing Origin",
          "Off: a terminal WebSocket without an Origin header is rejected (anti-CSWSH)."),
        s("terminals.idle_timeout", "terminals", "int", 900, "Idle timeout (s)",
          "Disconnect an idle client after this many seconds (0 = never). Does not close the terminal.",
          min=0, max=86400, min_nonzero=30),
        s("terminals.max_message_bytes", "terminals", "int", 65536, "Max message (bytes)",
          "Largest keyboard/message frame accepted.", min=1024, max=1048576),
        s("terminals.max_bytes_per_sec", "terminals", "int", 4194304, "Max output (bytes/s)",
          "Per-client output rate cap; a client above it is dropped.", min=65536, max=67108864),
        s("terminals.client_queue_bytes", "terminals", "int", 524288, "Client queue (bytes)",
          "Per-client output buffer; when full that client is dropped.", min=65536, max=8388608),
    ]


def schema() -> list[dict[str, Any]]:
    """Static settings + one block per provider (resolved lazily: no import cycles)."""
    out = []
    for item in _static_schema():
        item = dict(item)
        if callable(item.get("choices")):
            item["choices"] = item["choices"]()
        out.append(item)
    return out


def provider_defaults(kind: str) -> dict[str, Any]:
    return {"enabled": True, "command": "", "default_args": "", "default_model": "", "env": {},
            "mcp": False}


_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {"stamp": None, "data": None}


def _validate(item: dict[str, Any], value: Any) -> Any:
    typ = item["type"]
    if typ == "bool":
        if isinstance(value, bool):
            return value
        raise SettingsError(f"{item['key']}: true or false expected")
    if typ == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise SettingsError(f"{item['key']}: a whole number expected")
        value = int(value)
        if not item["min"] <= value <= item["max"]:
            raise SettingsError(f"{item['key']}: must be between {item['min']} and {item['max']}")
        if item.get("min_nonzero") and 0 < value < item["min_nonzero"]:
            raise SettingsError(
                f"{item['key']}: must be 0 or between {item['min_nonzero']} and {item['max']}"
            )
        return value
    if typ == "choice":
        if value not in item["choices"]:
            raise SettingsError(f"{item['key']}: one of {', '.join(map(str, item['choices']))}")
        return value
    if typ == "list":
        if isinstance(value, str):
            value = [v.strip() for v in value.splitlines()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{item['key']}: a list of text lines expected")
        value = [v.strip() for v in value if v.strip()]
        if len(value) > 50 or any(len(v) > 253 or any(c in v for c in " /\\@") for v in value):
            raise SettingsError(f"{item['key']}: up to 50 plain hostnames")
        return value
    if typ == "paths":
        if isinstance(value, str):
            value = [v.strip() for v in value.splitlines()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{item['key']}: a list of paths expected")
        value = [v.strip() for v in value if v.strip()]
        if len(value) > 50:
            raise SettingsError(f"{item['key']}: up to 50 paths")
        for entry in value:
            if len(entry) > 4096 or "\0" in entry or not os.path.isabs(os.path.expanduser(entry)):
                raise SettingsError(f"{item['key']}: {entry!r} is not an absolute path")
        return value
    raise SettingsError(f"{item['key']}: unsupported type")


def _provider_value(field: str, value: Any, kind: str) -> Any:
    key = f"providers.{kind}.{field}"
    if field in ("enabled", "mcp"):
        if not isinstance(value, bool):
            raise SettingsError(f"{key}: true or false expected")
        return value
    if field == "env":
        if isinstance(value, str):
            lines = [ln for ln in value.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            value = {}
            for ln in lines:
                k, sep, v = ln.partition("=")
                if not sep:
                    raise SettingsError(f"{key}: use NAME=value on each line")
                value[k.strip()] = v.strip()
        if not isinstance(value, dict) or len(value) > 50:
            raise SettingsError(f"{key}: up to 50 NAME=value pairs")
        for k, v in value.items():
            if not (isinstance(k, str) and k.replace("_", "A").isalnum() and not k[0].isdigit()):
                raise SettingsError(f"{key}: invalid variable name {k!r}")
            if not isinstance(v, str) or len(v) > 4096 or "\0" in v:
                raise SettingsError(f"{key}: invalid value for {k}")
        return dict(value)
    if field in ("command", "default_args", "default_model"):
        if not isinstance(value, str) or len(value) > 1024 or "\0" in value or "\n" in value:
            raise SettingsError(f"{key}: a single line of text expected")
        value = value.strip()
        if field != "command":
            try:
                shlex.split(value)
            except ValueError as exc:
                raise SettingsError(f"{key}: {exc}") from exc
        else:
            try:
                parts = shlex.split(value)
            except ValueError as exc:
                raise SettingsError(f"{key}: {exc}") from exc
            if value and not parts:
                raise SettingsError(f"{key}: empty command")
        return value
    raise SettingsError(f"unknown provider setting {key!r}")


# -- remote SSH hosts -----------------------------------------------------------
# A deliberately tiny, fixed schema: crewhall builds the ``ssh`` argv itself and
# never accepts user-supplied SSH options, so a host entry cannot weaken host-key
# checking, enable agent forwarding or inject shell metacharacters.
HOST_KEYS = {"ssh", "port", "identity", "tmux_socket", "known_hosts", "tunnel"}
_HOST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TMUX_SOCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SSH_BAD_CHARS = set(" \t\r\n\0;|&$`\"'\\")


def _validate_ssh_destination(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise SettingsError("hosts.*.ssh: a destination like user@host is required")
    if value != value.strip():
        raise SettingsError("hosts.*.ssh: no leading or trailing spaces")
    if value.startswith("-"):
        raise SettingsError("hosts.*.ssh: must not start with '-'")
    if any(c in _SSH_BAD_CHARS for c in value):
        raise SettingsError("hosts.*.ssh: invalid characters in destination")
    return value


def _validate_identity(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or "\0" in value or "\n" in value or len(value) > 4096:
        raise SettingsError("hosts.*.identity: a single path expected")
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise SettingsError("hosts.*.identity: use an absolute path or ~/...")
    if os.path.islink(path):
        raise SettingsError("hosts.*.identity: must not be a symlink")
    try:
        st = os.stat(path)
    except OSError:
        raise SettingsError(f"hosts.*.identity: {path!r} does not exist") from None
    if not stat.S_ISREG(st.st_mode):
        raise SettingsError("hosts.*.identity: must be a regular file")
    if st.st_uid != os.getuid():
        raise SettingsError("hosts.*.identity: must be owned by the current user")
    if stat.S_IMODE(st.st_mode) not in (0o600, 0o400):
        raise SettingsError("hosts.*.identity: permissions must be 0600 or 0400")
    return path


def _validate_known_hosts(value: Any) -> str | None:
    """An optional per-host known_hosts file (host-key checking stays strict)."""
    if value in (None, ""):
        return None
    if not isinstance(value, str) or "\0" in value or "\n" in value or len(value) > 4096:
        raise SettingsError("hosts.*.known_hosts: a single path expected")
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise SettingsError("hosts.*.known_hosts: use an absolute path or ~/...")
    if os.path.islink(path):
        raise SettingsError("hosts.*.known_hosts: must not be a symlink")
    try:
        st = os.stat(path)
    except OSError:
        raise SettingsError(f"hosts.*.known_hosts: {path!r} does not exist") from None
    if not stat.S_ISREG(st.st_mode):
        raise SettingsError("hosts.*.known_hosts: must be a regular file")
    if st.st_uid != os.getuid():
        raise SettingsError("hosts.*.known_hosts: must be owned by the current user")
    if stat.S_IMODE(st.st_mode) & 0o022:
        raise SettingsError("hosts.*.known_hosts: must not be writable by others")
    return path


def _validate_host(name: Any, body: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not _HOST_NAME_RE.match(name):
        raise SettingsError(f"hosts: invalid host name {name!r} (use [A-Za-z0-9_.-])")
    if not isinstance(body, dict):
        raise SettingsError(f"hosts.{name}: a table is required")
    unknown = set(body) - HOST_KEYS
    if unknown:
        raise SettingsError(f"hosts.{name}: unknown keys {sorted(unknown)}")
    destination = _validate_ssh_destination(body.get("ssh"))
    port = body.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise SettingsError(f"hosts.{name}.port: a port between 1 and 65535 is required")
    identity = _validate_identity(body.get("identity"))
    known_hosts = _validate_known_hosts(body.get("known_hosts"))
    tmux_socket = body.get("tmux_socket") or "crewhall"
    if not isinstance(tmux_socket, str) or not _TMUX_SOCKET_RE.match(tmux_socket):
        raise SettingsError(f"hosts.{name}.tmux_socket: an invalid tmux socket name")
    # Opt-in (closed by default): lets remote agents reach this daemon's
    # restricted agent gateway over a reverse SSH tunnel.
    tunnel = body.get("tunnel", False)
    if not isinstance(tunnel, bool):
        raise SettingsError(f"hosts.{name}.tunnel: true or false expected")
    return {"ssh": destination, "port": port, "identity": identity,
            "known_hosts": known_hosts, "tmux_socket": tmux_socket, "tunnel": tunnel}


def hosts() -> dict[str, dict[str, Any]]:
    """Validated remote hosts (empty unless explicitly configured)."""
    return {name: dict(cfg) for name, cfg in load().get("hosts", {}).items()}


def host(name: str) -> dict[str, Any]:
    cfg = hosts().get(name)
    if cfg is None:
        raise SettingsError(f"unknown host {name!r}")
    return {**cfg, "name": name}


def _normalize(raw: Any) -> dict[str, Any]:
    """Valid values only; anything unknown or out of range falls back to its default."""
    data: dict[str, Any] = {}
    raw = raw if isinstance(raw, dict) else {}
    for item in schema():
        group, _, name = item["key"].partition(".")
        node = raw.get(group)
        if isinstance(node, dict) and name in node:
            try:
                data.setdefault(group, {})[name] = _validate(item, node[name])
            except SettingsError:
                pass
    providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    for kind in _kinds():
        node = providers.get(kind)
        if not isinstance(node, dict):
            continue
        for field in provider_defaults(kind):
            if field in node:
                try:
                    data.setdefault("providers", {}).setdefault(kind, {})[field] = _provider_value(field, node[field], kind)
                except SettingsError:
                    pass
    hosts_raw = raw.get("hosts")
    if isinstance(hosts_raw, dict):
        for name, body in hosts_raw.items():
            try:
                data.setdefault("hosts", {})[name] = _validate_host(name, body)
            except SettingsError as exc:
                log.warning("ignoring invalid host setting: %s", exc)
    return data


def load() -> dict[str, Any]:
    """The stored (non-default) values, cached by file mtime."""
    p = path()
    try:
        st = os.stat(p)
        stamp = (p, st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (p, None)
    with _LOCK:
        if _CACHE["stamp"] == stamp and _CACHE["data"] is not None:
            return _CACHE["data"]
        data: dict[str, Any] = {}
        if stamp[1] is not None:
            try:
                with open(p, encoding="utf-8") as fh:
                    data = _normalize(json.load(fh))
            except (OSError, ValueError):
                data = {}
        _CACHE.update(stamp=stamp, data=data)
        return data


def _write(data: dict[str, Any]) -> None:
    os.makedirs(config_dir(), mode=0o700, exist_ok=True)
    tmp = path() + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path())
    with _LOCK:
        _CACHE.update(stamp=None, data=None)


def _find(key: str) -> dict[str, Any] | None:
    return next((i for i in schema() if i["key"] == key), None)


def env_override(item: dict[str, Any]) -> str | None:
    name = item.get("env")
    if not name:
        return None
    legacy = brand.LEGACY_ENV_PREFIX + name[len(brand.ENV_PREFIX):]
    for candidate in (name, legacy):
        if os.environ.get(candidate) not in (None, ""):
            return candidate
    return None


def get(key: str, default: Any = None) -> Any:
    """Effective value: environment override > settings file > schema default."""
    item = _find(key)
    if item is None:
        raise KeyError(key)
    env = env_override(item)
    if env:
        raw = os.environ[env]
        try:
            if item["type"] == "bool":
                return raw.lower() not in ("0", "off", "false", "no")
            if item["type"] == "int":  # an environment value is clamped, as it always was, not rejected
                return max(item["min"], min(item["max"], int(float(raw))))
            if item["type"] == "choice":
                return _validate(item, raw.lower())
        except (SettingsError, ValueError):
            pass
    group, _, name = key.partition(".")
    value = load().get(group, {}).get(name)
    return item["default"] if value is None else value


def provider(kind: str) -> dict[str, Any]:
    return {**provider_defaults(kind), **load().get("providers", {}).get(kind, {})}


def provider_enabled(kind: str) -> bool:
    return bool(provider(kind)["enabled"])


def enabled_kinds() -> list[str]:
    return [k for k in _kinds() if provider_enabled(k)]


def provider_command(kind: str) -> list[str] | None:
    """The configured command (split like a shell would), or None for the built-in one."""
    cmd = provider(kind)["command"]
    return shlex.split(cmd) if cmd else None


def provider_args(kind: str, given: list[str] | None = None) -> list[str]:
    """Default args/model ahead of the agent's own; an explicit ``--model`` wins."""
    cfg = provider(kind)
    args = shlex.split(cfg["default_args"]) if cfg["default_args"] else []
    given = list(given or [])
    has_model = any(a in ("--model", "-m") or a.startswith("--model=") for a in args + given)
    if cfg["default_model"] and not has_model:
        args = ["--model", cfg["default_model"], *args]
    return args


def provider_env(kind: str) -> dict[str, str]:
    return dict(provider(kind)["env"])


_STANDARD_BIN_PREFIXES = ("/usr/", "/opt/", "/bin/", "/sbin/", "/snap/", "/nix/")


def validate_provider_command(kind: str, command: str) -> None:
    """Reject a provider command that could not be run safely (raises SettingsError)."""
    argv = shlex.split(command) if command else []
    if not argv:
        return
    first = argv[0]
    key = f"providers.{kind}.command"
    if "/" in first:
        path = os.path.expanduser(first)
        if not os.path.isabs(path):
            raise SettingsError(f"{key}: use an absolute path or a bare name from PATH")
    else:
        path = shutil.which(first) or ""
        if not path:
            raise SettingsError(f"{key}: {first!r} is not on PATH")
    try:
        st = os.stat(path)
    except OSError:
        raise SettingsError(f"{key}: {first!r} does not exist") from None
    if not os.path.isfile(path):
        raise SettingsError(f"{key}: {path!r} is not a file")
    if not os.access(path, os.X_OK):
        raise SettingsError(f"{key}: {path!r} is not executable")
    if st.st_mode & 0o022:
        raise SettingsError(f"{key}: {path!r} is writable by other users")


def provider_risks(kind: str) -> list[str]:
    """Warnings (red in the UI) about a provider's configuration."""
    risks: list[str] = []
    cfg = provider(kind)
    argv = shlex.split(cfg["command"]) if cfg["command"] else []
    if argv:
        first = argv[0]
        path = os.path.expanduser(first) if "/" in first else (shutil.which(first) or "")
        if path and os.path.isabs(path) and not path.startswith(_STANDARD_BIN_PREFIXES + (os.path.expanduser("~"),)):
            risks.append(f"binary outside PATH/home: {path}")
        if path:
            try:
                if os.stat(path).st_mode & 0o022:
                    risks.append(f"binary writable by other users: {path}")
            except OSError:
                pass
    from .harness import get_harness

    try:
        risks.extend(get_harness(kind).config_risks())
    except Exception:  # noqa: BLE001 - a broken provider must not hide the settings page
        pass
    return risks


def is_sensitive(name: str) -> bool:
    return any(w in name.upper() for w in SENSITIVE)


def other_interactive_users() -> list[str]:
    """Other local accounts with an interactive shell (read-only, best effort)."""
    try:
        import pwd

        current = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        current = ""
    names: list[str] = []
    try:
        with open("/etc/passwd", encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split(":")
                if len(parts) < 7:
                    continue
                name, shell, home = parts[0], parts[6], parts[5]
                if name in (current, "root", "nobody") or shell in ("", "/bin/false", "/usr/bin/false"):
                    continue
                if shell.rsplit("/", 1)[-1] in ("bash", "zsh", "fish", "sh", "ksh") and os.path.isdir(home):
                    names.append(name)
    except OSError:
        return []
    return sorted(names)


def warnings() -> list[str]:
    out: list[str] = []
    if not get("security.local_requires_token"):
        others = other_interactive_users()
        if others:
            out.append(
                "Other local users exist (" + ", ".join(others[:5]) + "); consider turning on "
                "\"Require the token locally too\" (security.local_requires_token)."
            )
    for kind in _kinds():
        for risk in provider_risks(kind):
            out.append(f"Provider {kind}: {risk}")
    return out


def describe() -> dict[str, Any]:
    """Everything the Settings panel needs: schema, current values, provenance."""
    stored = load()
    items = []
    for item in schema():
        group, _, name = item["key"].partition(".")
        row = {k: v for k, v in item.items() if k != "env"}
        row["value"] = get(item["key"])
        row["stored"] = name in stored.get(group, {})
        row["env"] = env_override(item)
        items.append(row)
    providers = []
    from .harness import get_harness

    for kind in _kinds():
        cfg = provider(kind)
        env = {k: (MASK if is_sensitive(k) and v else v) for k, v in cfg["env"].items()}
        providers.append({"kind": kind, **cfg, "env": env,
                          "stored": bool(stored.get("providers", {}).get(kind)),
                          "risks": provider_risks(kind),
                          "mcp_supported": get_harness(kind).mcp_supported})
    return {"items": items, "providers": providers, "path": path(),
            "exists": os.path.exists(path()), "warnings": warnings(),
            "hosts": hosts()}


def patch(changes: dict[str, Any], *, confirm: bool = False) -> dict[str, Any]:
    """Apply ``{"group.name": value, "providers.<kind>.<field>": value}`` atomically.

    Changing a provider ``command`` or ``env`` is privileged (it decides which
    binary runs and with which credentials), so it requires ``confirm=True``.
    """
    if not isinstance(changes, dict) or not changes:
        raise SettingsError("nothing to change")
    touched = set()
    for key in changes:
        parts = str(key).split(".")
        if len(parts) == 3 and parts[0] == "providers" and parts[2] in ("command", "env"):
            touched.add(parts[2])
    if touched and not confirm:
        raise SettingsError(
            "changing a provider " + " or ".join(sorted(touched)) + " needs typed confirmation"
        )
    if changes.get("terminals.enabled") is True and not confirm:
        raise SettingsError("enabling terminals needs typed confirmation")
    if changes.get("terminals.master_grants") is True and not confirm:
        raise SettingsError("letting the master session use terminals needs typed confirmation")
    with _LOCK:
        data = json.loads(json.dumps(load()))
        for key, value in changes.items():
            if str(key) == "hosts":
                if not isinstance(value, dict):
                    raise SettingsError("hosts: a table of host entries is required")
                data["hosts"] = {
                    name: _validate_host(name, body) for name, body in value.items()
                }
                continue
            parts = str(key).split(".")
            if parts[0] == "providers" and len(parts) == 3 and parts[1] in _kinds():
                _, kind, field = parts
                if field == "env" and isinstance(value, dict):
                    # A masked value means "unchanged": keep the stored secret.
                    old = provider(kind)["env"]
                    value = {k: (old.get(k, "") if v == MASK else v) for k, v in value.items()}
                clean = _provider_value(field, value, kind)
                if field == "command":
                    validate_provider_command(kind, clean)
                data.setdefault("providers", {}).setdefault(kind, {})[field] = clean
                continue
            item = _find(str(key))
            if item is None:
                raise SettingsError(f"unknown setting {key!r}")
            group, _, name = item["key"].partition(".")
            data.setdefault(group, {})[name] = _validate(item, value)
        enabled = [k for k in _kinds() if data.get("providers", {}).get(k, {}).get("enabled", True)]
        if not enabled:
            raise SettingsError("at least one provider must stay enabled")
        default_kind = data.get("agents", {}).get("default_kind")
        if default_kind and default_kind not in enabled:
            raise SettingsError(f"the default provider {default_kind!r} is disabled")
        _write(data)
    return describe()


def reset(prefix: str | None = None) -> dict[str, Any]:
    """Drop stored values (all, or a group such as ``agents`` / ``providers.claude``)."""
    with _LOCK:
        data = json.loads(json.dumps(load()))
        if not prefix:
            data = {}
        else:
            parts = prefix.split(".")
            node = data
            for p in parts[:-1]:
                node = node.get(p, {})
            node.pop(parts[-1], None)
        _write(data)
    return describe()


# -- provider detection ----------------------------------------------------------
_VERSION_CACHE: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}


def check_provider(kind: str, command: str | None = None) -> dict[str, Any]:
    """Is the binary there, and what version does it report? (never raises)"""
    from .harness import get_harness

    try:
        argv = shlex.split(command) if command else (provider_command(kind) or get_harness(kind).command())
    except ValueError as exc:
        return {"kind": kind, "ok": False, "error": str(exc)}
    if not argv:
        return {"kind": kind, "ok": False, "error": "empty command"}
    key = tuple(argv)
    hit = _VERSION_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    found = shutil.which(argv[0])
    out: dict[str, Any] = {"kind": kind, "command": argv, "path": found, "ok": bool(found)}
    if not found:
        out["error"] = f"{argv[0]!r} not found in PATH"
    else:
        try:
            r = subprocess.run([*argv, "--version"], capture_output=True, text=True, timeout=5,
                               stdin=subprocess.DEVNULL)
            text = (r.stdout or r.stderr).strip().splitlines()
            out["version"] = text[0][:120] if text else None
            if r.returncode != 0:
                out.update(ok=False, error=f"--version exited with {r.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            out.update(ok=False, error=str(exc))
    _VERSION_CACHE[key] = (time.monotonic(), out)
    return out
