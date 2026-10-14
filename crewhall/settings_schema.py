"""The settings schema (keys, groups, types, defaults).

Kept apart from :mod:`crewhall.settings` so the schema can be imported without
pulling the whole settings machinery; ``settings`` re-exports these names.
"""
from __future__ import annotations

import os
from typing import Any

from . import brand


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
        s("agents.auto_select_compatible", "agents", "bool", True,
          "Auto-select a compatible CLI",
          "On: if a provider's binary is outside the range its adapter was verified against, "
          "crewhall automatically falls back to another installed version (mise/asdf, a pinned "
          "fallback_command, ~/.local/bin…) and notifies you. Off: it only uses fallback_command.",
          restart=True),
        s("agents.allow_unvalidated_versions", "agents", "bool", False,
          "Allow unverified CLI versions",
          "Off: crewhall refuses to launch or drive an agent whose CLI version is outside the "
          "range the adapter was verified against (see `crewhall doctor`). Configure a supported "
          "binary or a fallback_command per provider instead of turning this on.",
          env="CREWHALL_ALLOW_UNVALIDATED_VERSIONS", restart=True),
        s("agents.conversations", "agents", "bool", True, "Read conversations",
          "Enables the Conversation tab (Claude transcripts / OpenCode server).",
          env="CREWHALL_CONVERSATIONS", restart=True),
        s("agents.control_files", "agents", "choice", "append", "Instruction files",
          "append: add the crewhall section to CLAUDE.md/AGENTS.md; off: never touch them.",
          choices=["append", "off"], env="CREWHALL_CONTROL_FILES"),
        s("agents.permission_wait", "agents", "int", 300, "Permission wait (s)",
          "How long a permission request or question waits for your answer in the Web UI before the agent's own prompt takes over (0 = never wait).",
          min=0, max=3600, env="CREWHALL_PERMISSION_WAIT"),
        s("agents.restart_max", "agents", "int", 3, "Forced restarts per agent",
          "How many times an agent may be forcibly restarted (by a lead) inside the window below.",
          min=1, max=100),
        s("agents.restart_window_minutes", "agents", "int", 10, "Forced restart window (min)",
          "The window over which agents.restart_max applies.", min=1, max=1440),
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
        s("uploads.mode", "uploads", "choice", "temp", "Upload storage",
          "temp: uploaded files go to the state temp directory and are cleaned automatically. "
          "permanent: they are kept in the directory below.",
          choices=["temp", "permanent"]),
        s("uploads.dir", "uploads", "path", "~/crewhall-uploads", "Permanent upload directory",
          "Absolute directory used when storage is 'permanent'. Created (0700) if missing."),
        s("uploads.max_mb", "uploads", "int", 25, "Max upload size (MB)",
          "Largest single file the Web UI accepts.", min=1, max=2048),
        s("uploads.keep_days", "uploads", "int", 30, "Keep permanent uploads (days)",
          "Permanent uploads older than this are removed by the janitor (0 = keep forever).",
          min=0, max=3650),
        s("conversations.enabled", "conversations", "bool", True, "Chat mode",
          "Shows the chat interface (conversation sidebar, artifact panel).", restart=True),
        s("chat.language", "conversations", "choice", "es", "Agent language",
          "Language of the rules and guides crewhall writes for chat agents "
          "(CLAUDE.md/AGENTS.md/ONLYOFFICE.md). The Web UI itself is English.",
          choices=["en", "es"]),
        s("conversations.dir", "conversations", "path",
          os.path.join(brand.state_root_dir(), "conversations"), "Conversations directory",
          "Root directory for conversations (inputs/ and outputs/ per conversation). Created 0700."),
        s("conversations.max_mb", "conversations", "int", 200, "Max artifact size (MB)",
          "Largest file a conversation accepts or serves.", min=1, max=4096),
        s("office.enabled", "office", "bool", False, "OnlyOffice editor",
          "Enables the OnlyOffice viewer/editor for artifacts (requires a Document Server)."),
        s("office.public_url", "office", "text", "http://127.0.0.1:8081", "OnlyOffice public URL",
          "Base URL the browser uses to load the editor (e.g. the Document Server or a Tailscale name)."),
        s("office.base_url", "office", "text", "", "crewhall URL seen by OnlyOffice",
          "Base URL the Document Server uses to fetch documents and post callbacks. Empty = web.public_url "
          "(or the Docker bridge gateway for a local container)."),
        s("office.jwt_enabled", "office", "bool", True, "Sign OnlyOffice requests",
          "Sign the editor config and verify callbacks with JWT (recommended; must match the server)."),
        s("office.jwt_secret", "office", "text", "", "OnlyOffice JWT secret", "Shared secret; must match the Document Server.", secret=True),
        s("office.lang", "office", "text", "es", "Editor language", "Two-letter language code for the OnlyOffice UI."),
        s("office.collab_enabled", "office", "bool", False, "Live co-editing (agent)",
          "Lets the agent join the open document as a co-editor with its own cursor (headless Chromium)."),
        s("office.chromium", "office", "text", "", "Chromium binary", "Path to chromium for headless co-editing (auto-detected when empty)."),
        s("web.public_url", "web", "text", "", "Public URL of crewhall",
          "Base URL the browser and the Document Server use to reach this crewhall (e.g. the Tailscale name)."),
        s("requests.max_open_per_agent", "requests", "int", 2, "Open requests per agent",
          "How many requests an agent may have waiting for a reply at once.", min=1, max=10),
        s("requests.max_depth", "requests", "int", 4, "Max request chain depth",
          "Stops A→B→C→… request loops from growing without bound.", min=1, max=10),
        s("requests.default_timeout_s", "requests", "int", 300, "Default request timeout (s)",
          "How long a request waits for its reply before it expires.", min=10, max=86400),
        s("requests.max_body_chars", "requests", "int", 8000, "Max request/reply size (chars)",
          "Requests and replies longer than this are rejected.", min=256, max=65536),
        s("messaging.history_entries", "messaging", "int", 2000, "Message history entries",
          "How many agent messages are kept on disk (the crew strip and Message flow survive "
          "a daemon restart). Older entries are dropped as new ones arrive.", min=0, max=1000000),
        s("messaging.history_days", "messaging", "int", 30, "Message history retention (days)",
          "Message history older than this is pruned (0 = keep forever).", min=0, max=3650),
        s("messaging.max_body_chars", "messaging", "int", 16000, "Max message size (chars)",
          "Agent-to-agent messages longer than this are rejected (anti-loop guard).",
          min=256, max=1000000),
        s("messaging.rate_per_minute", "messaging", "int", 120, "Max messages per minute (per agent)",
          "Agent-to-agent sends above this rate are refused for a moment (anti-loop guard).",
          min=1, max=100000),
        s("maintenance.tmp_max_age_minutes", "maintenance", "int", 60, "Temp files: max age (min)",
          "Agent temp files older than this are removed by the janitor.", min=5, max=10080),
        s("maintenance.tmp_max_mb", "maintenance", "int", 512, "Temp dir: size cap (MB)",
          "The oldest temp files go first when the dedicated temp directory exceeds this.", min=64, max=65536),
        s("maintenance.janitor_minutes", "maintenance", "int", 15, "Janitor interval (min)",
          "How often the temp directory is pruned. Applies after a daemon restart.", min=1, max=1440,
          restart=True),
        s("maintenance.keep_backups", "maintenance", "int", 5, "Backups to keep",
          "Pre-update and pre-reset backups kept in the state directory.", min=1, max=100),
        s("maintenance.log_max_mb", "maintenance", "int", 5, "Daemon log: size cap (MB)",
          "daemon.log rotates once it reaches this size.", min=1, max=1024),
        s("maintenance.log_backups", "maintenance", "int", 3, "Daemon log: rotated files",
          "How many rotated daemon.log.N files are kept.", min=0, max=50),
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
        s("terminals.totp_grants", "terminals", "bool", False, "TOTP unlocks every terminal",
          "On: signing in with a TOTP code grants terminal read+write access on all hosts, and the "
          "unlock prompt also accepts a TOTP code (a leaked code would then open shells). "
          "Enabling is privileged."),
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
