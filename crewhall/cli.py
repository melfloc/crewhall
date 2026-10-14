"""``crewhall`` command-line interface.

``main`` and ``build_parser`` stay here; each command group's handlers live in
``crewhall.commands.<group>`` and are re-exported so ``crewhall.cli.cmd_x`` keeps
working (tests and any external caller use those names).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from . import clean, paths, watch
from .client import RpcError
from .session import SessionTimeout
from .commands.agents import (
    _hook_payload,
    cmd_agent_capture, cmd_agent_create, cmd_agent_hook, cmd_agent_identity,
    cmd_agent_list, cmd_agent_message, cmd_agent_messages, cmd_agent_new_session,
    cmd_agent_send, cmd_agent_state, cmd_agent_stop, cmd_agent_wait,
)
from .commands.common import (
    _agent_identity_env, _client, _default_shell, _human_bytes, _human_time, _log_matches,
    _print_agent, _print_delivery, _print_frontends, _print_session, _print_team,
    _print_terminal, argparse_cwd,
)
from .commands.hosts import cmd_host_add, cmd_host_list, cmd_host_remove, cmd_host_test
from .commands.messaging import (
    cmd_message_reply, cmd_message_request, cmd_message_requests, cmd_message_send,
)
from .commands.office import cmd_chat_publish, cmd_office
from .commands.sessions import (
    cmd_attach, cmd_capture, cmd_create, cmd_enter, cmd_events, cmd_interrupt, cmd_key,
    cmd_kill, cmd_list, cmd_read, cmd_read_until, cmd_resize, cmd_send, cmd_sendline,
    cmd_status,
)
from .commands.system import (
    _follow_log,
    cmd_adapter, cmd_bundle, cmd_clean, cmd_daemon, cmd_demo, cmd_doctor, cmd_selftest,
    cmd_service, cmd_update, cmd_ui, cmd_watch,
)
from .commands.teams import (
    cmd_team_add_member, cmd_team_create, cmd_team_info, cmd_team_list, cmd_team_remove,
    cmd_team_remove_member, cmd_team_set_leads, cmd_team_set_workspace, cmd_team_up,
)
from .commands.terminals import (
    cmd_terminal_attach, cmd_terminal_capture, cmd_terminal_close, cmd_terminal_key,
    cmd_terminal_ls, cmd_terminal_new, cmd_terminal_send,
)
from .commands.web import (
    _parse_ttl,
    cmd_frontend, cmd_frontends, cmd_terminal_token, cmd_web, cmd_web_status,
    cmd_web_token,
)

__all__ = [
    "main", "build_parser",
    "_default_shell", "_client", "_print_session", "_human_time", "argparse_cwd",
    "_print_agent", "_agent_identity_env", "_print_delivery", "_print_team",
    "_print_terminal", "_print_frontends", "_human_bytes", "_log_matches",
    "_hook_payload", "_follow_log", "_parse_ttl",
    "cmd_web_token", "cmd_web_status",
    "clean", "paths", "subprocess", "time", "watch",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crewhall",
        description="Control interactive CLI/TUI sessions through a backend-agnostic interface.",
    )
    from .buildinfo import describe

    parser.add_argument("--version", action="version", version=f"crewhall {describe()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("create", help="create and start a session")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.add_argument("-b", "--backend", default="auto")
    p.add_argument("-n", "--name")
    p.add_argument("-c", "--cwd")
    p.add_argument("--cols", type=int, default=80)
    p.add_argument("--rows", type=int, default=24)
    p.add_argument("--shell", action="store_true", help="treat command as a shell string")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("send", help="write text into a session")
    p.add_argument("target")
    p.add_argument("text")
    p.add_argument("--enter", action="store_true", help="also send ENTER")
    p.add_argument("--shell", action="store_true", help="escape text as a shell string")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("sendline", help="write text then ENTER")
    p.add_argument("target")
    p.add_argument("text")
    p.set_defaults(func=cmd_sendline, shell=False, enter=True)

    p = sub.add_parser("key", help="send a named key")
    p.add_argument("target")
    p.add_argument("key")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("enter", help="send ENTER")
    p.add_argument("target")
    p.set_defaults(func=cmd_enter)

    p = sub.add_parser("capture", help="print the current session output")
    p.add_argument("target")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("read", help="read new output since last read")
    p.add_argument("target")
    p.add_argument("--timeout", type=float, default=1.0)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("read-until", help="wait until pattern appears")
    p.add_argument("target")
    p.add_argument("pattern")
    p.add_argument("--timeout", type=float, default=10.0)
    p.set_defaults(func=cmd_read_until)

    p = sub.add_parser("resize", help="resize the session PTY/window")
    p.add_argument("target")
    p.add_argument("cols", type=int)
    p.add_argument("rows", type=int)
    p.set_defaults(func=cmd_resize)

    p = sub.add_parser("interrupt", help="send SIGINT / C-c")
    p.add_argument("target")
    p.set_defaults(func=cmd_interrupt)

    p = sub.add_parser("kill", help="terminate a session")
    p.add_argument("target", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("status", help="show session state")
    p.add_argument("target")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("events", help="show session event log")
    p.add_argument("target")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("attach", help="attach a terminal to a tmux session")
    p.add_argument("target")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("terminal", help="web/CLI terminals (closed by default)")
    tsub = p.add_subparsers(dest="terminal_command", required=True)
    t = tsub.add_parser("new", help="create a terminal")
    t.add_argument("--host", default=None, help="configured SSH host (default: this machine)")
    t.add_argument("--cwd", default=None)
    t.add_argument("--shell", default=None, help="bash, zsh, sh or fish (local only)")
    t.add_argument("--title", default=None)
    t.add_argument("--readonly", action="store_true")
    t.add_argument("--command", default=None, help="text typed into the shell after start")
    t.add_argument("--cols", type=int, default=120)
    t.add_argument("--rows", type=int, default=32)
    t.set_defaults(func=cmd_terminal_new)
    t = tsub.add_parser("ls", help="list terminals")
    t.add_argument("--host", default=None)
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_terminal_ls)
    t = tsub.add_parser("send", help="write literal text into a terminal")
    t.add_argument("target")
    t.add_argument("text")
    t.add_argument("--enter", action="store_true")
    t.set_defaults(func=cmd_terminal_send)
    t = tsub.add_parser("key", help="send a named key")
    t.add_argument("target")
    t.add_argument("key")
    t.set_defaults(func=cmd_terminal_key)
    t = tsub.add_parser("capture", help="print terminal output")
    t.add_argument("target")
    t.add_argument("--lines", type=int, default=500)
    t.add_argument("--escapes", action="store_true")
    t.set_defaults(func=cmd_terminal_capture)
    t = tsub.add_parser("close", help="close a terminal")
    t.add_argument("target")
    t.set_defaults(func=cmd_terminal_close)
    t = tsub.add_parser("attach", help="attach a terminal to its tmux session")
    t.add_argument("target")
    t.set_defaults(func=cmd_terminal_attach)

    p = sub.add_parser("chat", help="chat conversations (agent side)")
    csub = p.add_subparsers(dest="chat_command", required=True)
    c = csub.add_parser("publish", help="publish a file you produced as an artifact of a chat "
                        "(you must lead it or be in its crew)")
    c.add_argument("path", help="file to publish (copied, never moved)")
    c.add_argument("--conversation", default=None,
                   help="conversation id (default: $CREWHALL_CONVERSATION)")
    c.add_argument("--name", default=None, help="artifact file name (default: the file's)")
    c.set_defaults(func=cmd_chat_publish)

    p = sub.add_parser("office", help="OnlyOffice: install the Document Server and co-edit artifacts")
    osub = p.add_subparsers(dest="office_command", required=True)
    o = osub.add_parser("setup", help="install + run OnlyOffice and wire it to crewhall")
    o.add_argument("--port", type=int, default=8081, help="Document Server port (default 8081)")
    o.add_argument("--bind", default=None,
                   help="address the browser reaches the DS on (default: Tailscale IP, else 127.0.0.1)")
    o.add_argument("--external", default=None, metavar="URL",
                   help="use an existing Document Server instead of running Docker here")
    o.add_argument("--jwt-secret", dest="jwt_secret", default=None,
                   help="JWT secret of the external Document Server (with --external)")
    o.add_argument("--no-wait", action="store_true", help="do not wait for the healthcheck")
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("status", help="show the OnlyOffice setup and container status")
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("stop", help="stop the Document Server container (keeps its data)")
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("restart", help="restart the Document Server container")
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("list", help="documents the agent is co-editing")
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("open", help="open the headless co-editor for an artifact")
    o.add_argument("--conversation", required=False, help="conversation id (defaults to $CREWHALL_CONVERSATION)")
    o.add_argument("--path", required=True, help="artifact path inside outputs/")
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("read", help="read the document text")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("insert", help="insert content at the cursor")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.add_argument("--text", required=True)
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("command", help="call any OnlyOffice Automation API method")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.add_argument("--method", required=True)
    o.add_argument("--args", default="[]", help="JSON array of arguments")
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("close", help="close the co-editor")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("users", help="who is editing the document (co-editors)")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("chat", help="read the messages the user sent in the editor's chat")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("say", help="send a message to the editor's co-authoring chat")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.add_argument("--text", required=True)
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("comments", help="list the document comments (anchored to text)")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("comment", help="add a comment to the document")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.add_argument("--text", required=True)
    o.set_defaults(func=cmd_office)
    o = osub.add_parser("save", help="force-save the document (fires the callback)")
    o.add_argument("--conversation", required=False)
    o.add_argument("--path", required=True)
    o.set_defaults(func=cmd_office)

    p = sub.add_parser("host", help="remote SSH hosts")
    hsub = p.add_subparsers(dest="host_command", required=True)
    h = hsub.add_parser("list", help="configured hosts and what is observed about them")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_host_list)
    h = hsub.add_parser("add", help="add or edit a host (SSH key auth only)")
    h.add_argument("name", help="short name used by --host (letters, digits, _ . -)")
    h.add_argument("destination", help="user@address of the remote machine")
    h.add_argument("--port", type=int, default=22)
    h.add_argument("--identity", help="private key file (0600)")
    h.add_argument("--known-hosts", dest="known_hosts", help="pinned known_hosts file")
    h.add_argument("--tmux-socket", dest="tmux_socket", help="tmux socket name on the host")
    h.add_argument("--tunnel", action="store_true", help="let remote agents message this daemon")
    h.set_defaults(func=cmd_host_add)
    h = hsub.add_parser("remove", help="remove a host (it must have no agents)")
    h.add_argument("name")
    h.set_defaults(func=cmd_host_remove)
    h = hsub.add_parser("test", help="check the SSH connection and what is installed there")
    h.add_argument("name")
    h.set_defaults(func=cmd_host_test)

    p = sub.add_parser("agent", help="control CLI agents through a semantic harness")
    asub = p.add_subparsers(dest="agent_command", required=True)

    a = asub.add_parser("list", help="list agents (or a team's members)")
    a.add_argument("--team")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent_list)

    a = asub.add_parser("hook", help="(internal) report an agent lifecycle event")
    a.add_argument("event", choices=["session_start", "prompt_submit", "stop",
                                     "permission_request", "question_request"])
    a.set_defaults(func=cmd_agent_hook)

    a = asub.add_parser(
        "new-session",
        help="start a clean conversation in an agent (Claude /clear, OpenCode /new)",
    )
    a.add_argument("target", help="agent name or id (a teammate, when run from an agent)")
    a.add_argument("--timeout", type=float, default=20.0)
    a.set_defaults(func=cmd_agent_new_session)

    a = asub.add_parser("identity", help="show your identity, Teams and teammates")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent_identity)

    a = asub.add_parser("create", help="create and launch an agent")
    a.add_argument("-t", "--kind", default=None, help="harness kind (default: opencode)")
    a.add_argument("-n", "--name")
    a.add_argument("-p", "--profile", default=None,
                   help="defaults from ~/.config/crewhall/profiles.toml")
    a.add_argument("-b", "--backend", default=None, help="backend (default: auto)")
    a.add_argument("-c", "--cwd")
    a.add_argument("--host", default=None,
                   help="run the agent in tmux on a configured SSH host (settings.json hosts)")
    a.add_argument("--team", default=None, help="team to join (inherits its workspace)")
    a.add_argument("--cols", type=int, default=120)
    a.add_argument("--rows", type=int, default=40)
    a.add_argument("--wait", action="store_true", help="wait until ready")
    a.add_argument("--timeout", type=float, default=30.0)
    a.add_argument(
        "--args",
        default=None,
        metavar="ARGS",
        help='extra arguments for the agent command, e.g. --args="--agent reviewer" '
        "(use the = form so a leading dash is not parsed as an option)",
    )
    a.set_defaults(func=cmd_agent_create)

    a = asub.add_parser("send", help="send a semantic prompt")
    a.add_argument("target")
    a.add_argument("prompt")
    a.add_argument("--timeout", type=float, default=30.0)
    a.set_defaults(func=cmd_agent_send)

    a = asub.add_parser("capture", help="print agent output")
    a.add_argument("target")
    a.add_argument("--recent", action="store_true")
    a.add_argument("--lines", type=int, default=40)
    a.set_defaults(func=cmd_agent_capture)

    a = asub.add_parser("state", help="show semantic agent state")
    a.add_argument("target")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent_state)

    a = asub.add_parser("wait", help="wait for a semantic state")
    a.add_argument("target")
    a.add_argument(
        "state",
        choices=["starting", "ready", "working", "waiting_input", "exited", "error"],
    )
    a.add_argument("--timeout", type=float, default=30.0)
    a.set_defaults(func=cmd_agent_wait)

    a = asub.add_parser("stop", help="stop an agent")
    a.add_argument("target")
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=cmd_agent_stop)

    a = asub.add_parser("message", help="send a message from one agent to another")
    a.add_argument("sender")
    a.add_argument("recipient")
    a.add_argument("body")
    a.set_defaults(func=cmd_agent_message)

    a = asub.add_parser("messages", help="show agent message history")
    a.add_argument("--agent")
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent_messages)

    t = asub.add_parser("team", help="manage logical teams of agents")
    tsub = t.add_subparsers(dest="team_command", required=True)

    tc = tsub.add_parser("create", help="create a team (optionally with a workspace)")
    tc.add_argument("name")
    tc.add_argument("agents", nargs="*")
    tc.add_argument("-w", "--workspace", default=None,
                    help="shared workspace directory (default cwd for its agents; "
                         "a path on the host with --host)")
    tc.add_argument("--host", default=None,
                    help="configured SSH host: the team's agents run there, the workspace is "
                         "a directory on it")
    tc.set_defaults(func=cmd_team_create)

    tu = tsub.add_parser("up", help="create a team and its agents from a team.toml/json file")
    tu.add_argument("file")
    tu.set_defaults(func=cmd_team_up)

    tw = tsub.add_parser("set-workspace", help="set/replace a team's workspace")
    tw.add_argument("target")
    tw.add_argument("workspace", nargs="?", default=None)
    tw.add_argument("--host", default=None,
                    help="move an empty team to this host ('' = back to this machine)")
    tw.set_defaults(func=cmd_team_set_workspace)

    tl = tsub.add_parser("list", help="list teams")
    tl.add_argument("--json", action="store_true")
    tl.set_defaults(func=cmd_team_list)

    ti = tsub.add_parser("info", help="show a team and its members")
    ti.add_argument("target")
    ti.add_argument("--json", action="store_true")
    ti.set_defaults(func=cmd_team_info)

    tr = tsub.add_parser("remove", help="remove a team (agents keep running)")
    tr.add_argument("target")
    tr.set_defaults(func=cmd_team_remove)

    tam = tsub.add_parser("add-member", help="add an existing agent to a team")
    tam.add_argument("target")
    tam.add_argument("agent")
    tam.set_defaults(func=cmd_team_add_member)

    trm = tsub.add_parser("remove-member", help="remove an agent from a team")
    trm.add_argument("target")
    trm.add_argument("agent")
    trm.set_defaults(func=cmd_team_remove_member)

    tsl = tsub.add_parser("set-leads", help="set the team's leads (no agents = clear)")
    tsl.add_argument("target")
    tsl.add_argument("agents", nargs="*", help="member names or ids that become leads")
    tsl.set_defaults(func=cmd_team_set_leads)

    p = sub.add_parser("message", help="agent-to-agent messaging (Team-scoped)")
    msub = p.add_subparsers(dest="message_command", required=True)
    ms = msub.add_parser("send", help="send a message to a teammate")
    ms.add_argument("--to", required=True)
    ms.add_argument("--message", required=True)
    ms.set_defaults(func=cmd_message_send)

    mr = msub.add_parser("request", help="ask a teammate and wait for their reply")
    mr.add_argument("--to", required=True)
    mr.add_argument("--task", required=True)
    mr.add_argument("--timeout", type=float, default=None)
    mr.add_argument("--no-wait", action="store_true", help="return the request id without waiting")
    mr.set_defaults(func=cmd_message_request)

    mp = msub.add_parser("reply", help="reply to a request you received")
    mp.add_argument("--request-id", required=True)
    mp.add_argument("--message", required=True)
    mp.set_defaults(func=cmd_message_reply)

    ml = msub.add_parser("requests", help="list open requests")
    ml.add_argument("--all", action="store_true", help="include closed requests")
    ml.add_argument("--json", action="store_true")
    ml.set_defaults(func=cmd_message_requests)

    p = sub.add_parser("daemon", help="manage the controller daemon")
    p.add_argument("action", choices=["run", "status", "stop", "logs"])
    p.add_argument("--socket")
    p.add_argument(
        "--kill-agents",
        action="store_true",
        help="also stop running agents (tmux agents otherwise survive a restart)",
    )
    p.add_argument("--follow", action="store_true", help="with `logs`: stream new lines")
    p.add_argument("--agent", default=None, help="with `logs`: only lines for this agent id")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("demo", help="run the built-in end-to-end demo")
    p.add_argument("-b", "--backend", default="all")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("web", help="run the Web UI (client of the engine)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--socket", default=None)
    p.add_argument("--tailscale", action="store_true",
                   help="bind to this machine's Tailscale IPv4 address")
    p.add_argument("--require-auth", action="store_true",
                   help="require an access token even on localhost")
    p.add_argument("--allow-host", action="append", default=None)
    wsub = p.add_subparsers(dest="web_action")
    wt = wsub.add_parser("token", help="manage the Web UI access token")
    wt.add_argument("token_action", choices=["generate", "revoke", "show"])
    wt.add_argument("--rotate", action="store_true")
    wt.set_defaults(func=cmd_web)
    wtt = wsub.add_parser("terminal-token", help="manage scoped terminal tokens")
    ttsub = wtt.add_subparsers(dest="tt_action", required=True)
    tt = ttsub.add_parser("new", help="issue a terminal token (shown once)")
    tt.add_argument("--scope", choices=["read", "write"], default="read")
    tt.add_argument("--host", default=None, help="comma-separated hosts (default: all)")
    tt.add_argument("--ttl", default="8h", help="e.g. 8h, 30m, 0 (no expiry)")
    tt.add_argument("--label", default="")
    tt.set_defaults(func=cmd_terminal_token)
    tt = ttsub.add_parser("list", help="list terminal tokens (no secrets)")
    tt.add_argument("--json", action="store_true")
    tt.set_defaults(func=cmd_terminal_token)
    tt = ttsub.add_parser("revoke", help="revoke a terminal token by id")
    tt.add_argument("id")
    tt.set_defaults(func=cmd_terminal_token)
    ws = wsub.add_parser("status", help="show Web UI security/tailscale status")
    ws.set_defaults(func=cmd_web)
    p.set_defaults(func=cmd_web)

    for name, helptext in (
        ("local", "serve the Web UI on 127.0.0.1 only (switches off tailscale)"),
        ("tailscale", "serve the Web UI over Tailscale only (token required; switches off local)"),
        ("off", "stop serving every Web interface (agents keep running)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--port", type=int, default=None, help="port (default 8765, remembered)")
        if name == "tailscale":
            p.add_argument("--keep-local", action="store_true", help="also keep the local interface")
        p.set_defaults(func=cmd_frontend)
    p = sub.add_parser("frontends", help="show which Web interfaces are enabled")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_frontends)

    p = sub.add_parser("update", help="update this managed installation to a verified release")
    p.add_argument("--from", dest="source", default=None, metavar="DIR_OR_URL",
                   help="release directory (release.json + tarball + .sig) or an http(s) base URL")
    p.add_argument("--check", action="store_true", help="only report whether an update is available")
    p.add_argument("--status", action="store_true", help="show installed/daemon versions and rollback state")
    p.add_argument("--rollback", action="store_true", help="swap back to the previous version")
    p.add_argument("--restart", action="store_true",
                   help="also restart the daemon (idle tmux agents survive it; refused if an agent is working or on PTY, unless --force-restart)")
    p.add_argument("--force-restart", action="store_true", help="restart even if an agent is working or on PTY (a PTY agent is closed; a working one loses its turn's hooks)")
    p.add_argument("--force", action="store_true", help="reinstall the same version / ignore schema warning")
    p.add_argument("--allow-downgrade", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("selftest", help="real end-to-end check with throw-away agents (spends a few tokens)")
    p.add_argument("--cwd", default=os.getcwd(), help="a folder the agent CLIs already trust (default: here)")
    p.add_argument("--kinds", default="claude,opencode", help="comma-separated agent kinds to test")
    p.add_argument("--quick", action="store_true", help="skip the interrupt step")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("doctor", help="check this machine's environment")
    p.add_argument("--json", action="store_true")
    p.add_argument("--trend", action="store_true",
                   help="summarise the health journal (memory, threads, queue, failures)")
    p.add_argument("--days", type=float, default=30.0,
                   help="with --trend: how many days back to summarise (default 30)")
    p.add_argument("--fix-control-files", action="store_true",
                   help="refresh stale CLAUDE.md/AGENTS.md managed sections")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("clean", help="remove test leftovers, old backups and old releases")
    p.add_argument("--dry-run", action="store_true", help="only list what would be removed (default)")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p.add_argument("--keep-backups", type=int, default=clean.KEEP_BACKUPS,
                   help="how many recent backups to keep per group (default: %(default)s)")
    p.add_argument("--max-age-days", type=int, default=None,
                   help="only remove temp dirs older than this many days")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("bundle", help="export/import your configuration (profiles, team files)")
    bsub = p.add_subparsers(dest="bundle_command", required=True)
    be = bsub.add_parser("export", help="write a portable config bundle (.tar.gz)")
    be.add_argument("path")
    be.add_argument("--with-state", action="store_true", help="include teams/agents state")
    be.add_argument("--no-live", action="store_true", help="do not include the teams/agents running now")
    be.add_argument("--with-token", action="store_true", help="include the web access token (sensitive)")
    be.set_defaults(func=cmd_bundle)
    bi = bsub.add_parser("import", help="restore a bundle on this machine")
    bi.add_argument("path")
    bi.add_argument("--force", action="store_true", help="overwrite existing files (keeps .bak)")
    bi.add_argument("--dry-run", action="store_true")
    bi.add_argument("--apply", action="store_true", help="also create the bundle's teams and agents (skips those whose directory is missing)")
    bi.set_defaults(func=cmd_bundle)

    p = sub.add_parser("adapter", help="scaffold a new provider adapter")
    asub = p.add_subparsers(dest="adapter_command", required=True)
    an = asub.add_parser("new", help="create the adapter skeleton and contract test")
    an.add_argument("kind", help="short lowercase identifier, e.g. codex")
    an.set_defaults(func=cmd_adapter)

    p = sub.add_parser("service", help="install/manage the systemd user service")
    p.add_argument(
        "action",
        choices=["unit", "install", "uninstall", "status",
                 "web-unit", "install-web", "uninstall-web"],
    )
    p.add_argument("--web-host", default="127.0.0.1")
    p.add_argument("--web-port", type=int, default=8765)
    p.add_argument("--web-tailscale", action="store_true")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("watch", help="long-duration watch: CLI versions, screens and timers")
    wsub = p.add_subparsers(dest="watch_command", required=True)
    w = wsub.add_parser("versions", help="compare CLI versions; on change run `watch screens`")
    w.add_argument("--json", action="store_true")
    w.add_argument("--no-notify", action="store_true", help="do not send a desktop notification")
    w.add_argument("--no-capture", action="store_true", help="only compare, never capture screens")
    w.set_defaults(func=cmd_watch)
    w = wsub.add_parser("screens", help="capture each CLI start screen and run the adapter detectors")
    w.add_argument("--json", action="store_true")
    w.add_argument("--kinds", default=",".join(watch.KINDS),
                   help="comma-separated kinds (default: all)")
    w.add_argument("--save-fixtures", action="store_true",
                   help="also write the captures to tests/fixtures/screens (git checkout only)")
    w.set_defaults(func=cmd_watch)
    w = wsub.add_parser("smoke", help="run the end-to-end smoke test with real agents")
    w.add_argument("--require", action="store_true",
                   help="fail instead of skipping when the real CLIs are missing")
    w.add_argument("--result", default=None, metavar="PATH",
                   help="write the outcome JSON here (default: state/health/smoke-<date>.json)")
    w.add_argument("--no-notify", action="store_true",
                   help="do not send a desktop notification when a step fails")
    w.set_defaults(func=cmd_watch)
    w = wsub.add_parser("install", help="install and enable the user systemd timers")
    w.add_argument("--remove", action="store_true", help="disable and remove the timers")
    w.set_defaults(func=cmd_watch)

    p = sub.add_parser("ui", help="launch the interactive TUI")
    p.add_argument("--local", action="store_true", help="embed a Controller (no daemon)")
    p.add_argument("--cwd", default=None, help="working dir for created agents")
    p.add_argument("--socket", default=None, help="daemon socket path")
    p.add_argument("--refresh", type=float, default=0.8)
    p.set_defaults(func=cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SessionTimeout as exc:
        print(f"timeout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
