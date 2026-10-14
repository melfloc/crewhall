"""Constants and small helpers shared by the web server and its route mixins.

The route handlers live in ``crewhall/web/routes/`` and are composed into
``crewhall.web.server.Handler``. Everything both sides need (limits, the static
asset map, the login page, the allow-lists) lives here so neither has to import
the other.
"""
from __future__ import annotations

import hashlib
import os

# Terminal ops that need a write scope vs a read scope.
TERMINAL_WRITE_OPS = {
    "terminal_create", "terminal_write", "terminal_key", "terminal_resize",
    "terminal_close",
}
TERMINAL_READ_OPS = {"terminal_list", "terminal_info", "terminal_capture"}
MAX_WS_CLIENTS_PER_TERMINAL = 8
MAX_WS_CLIENTS_PER_SESSION = 4
TICKET_TTL = 30.0

# Largest accepted request body (ops are small JSON documents).
MAX_BODY_BYTES = 1024 * 1024
# A configuration bundle upload (it is verified before being stored).
MAX_BUNDLE_UPLOAD = 8 * 1024 * 1024

# Operations a web client may invoke; all map 1:1 to control-plane ops.
ALLOWED_OPS = {
    "ping", "meta_info", "agent_list", "agent_info", "agent_create",
    "agent_send", "agent_write", "agent_key", "agent_resize", "agent_capture",
    "agent_transcript", "agent_history", "agent_interrupt",
    "agent_models", "agent_set_model", "conversation_search",
    "agent_state", "agent_wait", "agent_stop", "agent_restart",
    "team_list", "team_info", "team_create", "team_set_workspace",
    "team_add_member", "team_remove_member", "team_remove", "team_members", "team_up",
    "message_send", "message_history", "agent_identity",
    "interaction_list", "interaction_respond", "agent_new_session",
    "agent_processes", "agent_process_output", "agent_process_signal",
    "clean_plan", "clean_apply", "update_status",
    "bundle_export", "bundle_list", "bundle_import", "bundle_delete", "fs_complete", "uploads_info",
    "settings_get", "settings_set", "settings_reset", "provider_check",
    "provider_notices", "notice_ack",
    "frontend_status", "frontend_set", "reset_plan", "reset_apply",
    "agent_archive_list", "agent_archive_get",
    "request_list", "request_cancel",
    "worktree_list", "worktree_discard", "host_list", "host_set", "host_remove", "host_test",
    "terminal_create", "terminal_list", "terminal_info", "terminal_write",
    "terminal_key", "terminal_capture", "terminal_resize", "terminal_close",
    "conversation_create", "conversation_list", "conversation_info",
    "conversation_rename", "conversation_set_agent", "conversation_delete",
    "conversation_artifact_delete", "conversation_set_team",
    "conversation_activity", "conversation_crew",
    "office_collab_open", "office_collab_read", "office_collab_command",
    "office_collab_insert", "office_collab_close", "office_collab_list",
    "office_collab_users", "office_collab_chat", "office_collab_say",
    "office_collab_comments", "office_collab_comment_add", "office_collab_save",
    "office_status", "office_start",
}


def _session_key(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:16]


HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.realpath(os.path.join(HERE, "static"))
INDEX = os.path.join(STATIC, "index.html")
_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".json": "application/json", ".webmanifest": "application/manifest+json",
}

# The login page keeps its CSS/JS in static files so a strict CSP (no inline
# scripts/styles) holds on every response, including this one.
_LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crewhall — sign in</title>
<link rel="stylesheet" href="/static/login.css"></head><body>
<form id="f"><div>crewhall</div>
<input id="t" type="password" placeholder="access token" autofocus>
<div class="err" id="e"></div><button type="submit">Sign in</button>
<button type="button" id="pk" class="pk init-hidden">Sign in with a passkey</button>
<div class="or">or a 6-digit code (Authy / Samsung Pass…)</div>
<input id="c" inputmode="numeric" autocomplete="one-time-code" maxlength="7" placeholder="000000">
<button type="button" id="cb">Sign in with code</button></form>
<script src="/static/login.js"></script></body></html>"""
