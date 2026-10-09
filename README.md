# crewhall

[![ci](https://github.com/melfloc/crewhall/actions/workflows/ci.yml/badge.svg)](https://github.com/melfloc/crewhall/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![status: beta](https://img.shields.io/badge/status-beta-orange)

> Formerly **agent-terminal**. The `agent-terminal` command, the `AGENT_TERMINAL_*`
> variables and the old config/state directories keep working; see
> [Upgrading from agent-terminal](#upgrading-from-agent-terminal).

> The Spanish original is kept at [README.es.md](README.es.md). Deeper design
> notes: [ARCHITECTURE.md](ARCHITECTURE.md) · adapter standard: [ADAPTERS.md](ADAPTERS.md) ·
> install/operations: [INSTALL.md](INSTALL.md) · releases: [RELEASING.md](RELEASING.md).

**Control real, interactive CLI agents from one place — and let them talk to each
other.** crewhall drives the *actual* TUIs (Claude Code, OpenCode, Codex) in
tmux or a native PTY, exposes a semantic state per agent, and adds a peer-to-peer,
team-scoped messaging layer with an authoritative identity — so agents can assign
work, request an answer and hand off tasks, not just run side by side.

## Screenshots

The Web UI: live agent view, and Mission control with every agent and the message flow.

| Dark | Light |
| --- | --- |
| ![Live agent view, dark theme](docs/images/agent-live-dark.png) | ![Live agent view, light theme](docs/images/agent-live-light.png) |

![Mission control](docs/images/mission-control-dark.png)

## The differentiating claim

> **Peer-to-peer agent messaging with authoritative identity, Team limits, reliable
> delivery to busy or stopped agents, and backend-agnostic transport (tmux or PTY).**

Most orchestrators are supervisor↔worker or a shim around one agent. Here any two
agents inside a Team can talk; the sender is derived from a token validated by the
daemon (never a forgeable `--from`), delivery survives a busy recipient (bounded
retry queue) or a stopped one (revive on demand), and the whole thing works over
tmux *or* a plain PTY.

## What you get

- **One control plane**: a resident daemon over a 0600 UNIX socket, a CLI, a curses
  UI and a Web UI.
- **Semantic agent state** per provider: `STARTING` / `READY` / `WORKING` /
  `WAITING_INPUT` / `EXITED` / `ERROR`, always with observed **evidence** — never a
  guess.
- **Providers**: Claude Code, OpenCode and Codex, each an adapter over one small
  capability interface ([ADAPTERS.md](ADAPTERS.md)). Adding one does not touch the
  core.
- **Teams, tasks and correlation**: assign a task (`handoff`) or ask and wait for an
  answer (`request`/`reply`), with timeouts, chain limits and anti-loop rules.
- **A native MCP server** (stdio, stdlib only, off by default) so an agent can call
  `whoami`, `list_teammates`, `send_message`, `request`, `reply`, `handoff` and
  `new_session` as tools.
- **Optional per-agent git worktrees** (`workspace_mode: worktree`) on a branch
  `at/<team>/<agent>`, isolated under the state directory.
- **Mission Control**, a live activity view, conversation history, cost/usage,
  cleanup/reset, bundles and a settings panel — all in the Web UI.
- **File uploads from the Web UI**: the composer accepts files (button, drag & drop
  or paste), stores them on the server — temp, cleaned by the janitor, or a permanent
  directory — and hands the agent their absolute path.
- **Composer tools**: a per-provider **model selector** (Claude applies
  `/model <alias>`; OpenCode's catalog is read from its local server and its picker
  is driven for you; Codex opens its own picker), a **slash-command palette**,
  **quick actions** (interrupt, new session, permission mode), **conversation export**
  (Markdown/JSON), **global search** across every agent, and **`@`-mentions** for
  workspace paths.
- **Web terminals** (off by default): real `xterm.js` shells shown in the sidebar
  like agents and in the main pane, local or over SSH, gated by scoped tokens.
- **Drag & drop** in the sidebar: reorder teams and move an agent to another team.

## Install

Requires Python ≥ 3.11 and tmux for the tmux backend. The core has **no runtime
dependencies**.

```bash
git clone https://github.com/melfloc/crewhall.git && cd crewhall
python -m venv .venv && . .venv/bin/activate
pip install -e .
crewhall doctor          # environment diagnostics
crewhall selftest --cwd ~/Projects/crewhall   # real end-to-end check
crewhall local           # Web UI on 127.0.0.1:8765
```

`crewhall --help` lists everything; `crewhall ui` opens the curses
interface; `crewhall agent list` shows agents.

## Remote agents over SSH

A single local daemon can run agents in `tmux` on another machine reached over
SSH; remote agents appear in the same registry as local ones. Hosts are closed by
default and live in `settings.json` (0600):

```json
{
  "hosts": {
    "prod1": {"ssh": "deploy@prod1", "port": 22,
              "identity": "~/.ssh/id_crewhall",
              "known_hosts": "~/.ssh/known_hosts",
              "tmux_socket": "crewhall"}
  }
}
```

```bash
crewhall agent create --kind opencode --name reviewer --host prod1 \
    --cwd /srv/projects/app --wait
crewhall agent capture reviewer --recent
crewhall attach reviewer          # ssh -t … tmux attach-session
```

The remote server needs only `tmux` plus the agent CLI already logged in; use a
dedicated non-root user, a dedicated key and pin its host key in `known_hosts`
first. A host that is down is reported as `host_unreachable` and the agent is
never marked as exited; it reconnects when the host returns.

## Web terminals

Besides agents, crewhall can expose raw interactive **terminals** (real shells)
in the Web UI and the CLI. They are **off by default** and treated as a privileged
capability: a terminal is arbitrary code execution as your user.

- Terminals appear **in the sidebar like agents** (state, host, `read-only`) and the
  main pane is a **real `xterm.js` terminal** — you type directly into it and key
  combinations work (no separate input box). A **Terminals** panel lists and manages
  all of them, and each can also be opened in its own tab.
- Local or over SSH, reusing the same fixed, argv-only `ssh-tmux` transport as
  agents; the remote output stream uses its own SSH `ControlPath` so it never
  exhausts the host's `MaxSessions`.
- **Scoped access.** The master Web UI token grants **no** terminal access. A browser
  session must *unlock* with a separate **terminal token** carrying `terminal:read`
  or `terminal:write`, restricted to a set of hosts. Tokens are shown **once** and
  stored only as `sha256`:

  ```bash
  crewhall web terminal-token new --scope write --host local --ttl 8h --label laptop
  crewhall web terminal-token list
  crewhall web terminal-token revoke <id>
  ```

  In the Web UI: **Settings → Access & network → Terminal tokens**; the Terminals tab
  offers to create one if you have none. The unlock is remembered for the browser
  session (it is asked again after a daemon restart or when the session expires).
- **Enable it** with `terminals.enabled` (typed `CONFIRM`): Settings → Terminals, or
  `crewhall settings set terminals.enabled true --confirm`. The local CLI
  (`crewhall terminal new|ls|send|key|capture|close|attach`) is trusted: the UNIX
  socket already belongs to your user, so it needs no token.
- Limits per token/host/total, an idle timeout that drops the client (not the
  terminal), a maximum message size and a cap on WebSocket clients.

## Upgrading from agent-terminal

- Command: `crewhall` (`agent-terminal` stays as an alias).
- Environment: `CREWHALL_*`; the old `AGENT_TERMINAL_*` is still read, with a one-time
  deprecation notice.
- Data: on first run `~/.config/agent-terminal` and `~/.local/state/agent-terminal`
  are **copied** (never moved or deleted) to `.../crewhall`, so you can roll back.
- Services: `crewhall service install` retires the old `agent-terminal*.service` units.
- The Python import package is now `crewhall` (it was `agent_terminal`): update any
  `import agent_terminal` in your own code.

## Security

Security is the first constraint, not a feature:

- The daemon socket is `0600`. The Web UI is authenticated (token/session) unless it
  is bound to localhost *and* you leave the local token requirement off; Host/Origin
  allow-lists, a strict CSP (no inline scripts/evals; inline styles only so `xterm.js`
  can size itself), `nosniff`, `frame-ancestors 'none'` and per-IP login throttling
  are on by default.
- Every new capability is **off by default** (MCP, worktrees, web terminals); nothing
  accepts a trust/permission dialog automatically.
- **Web terminals** need a separate scoped token (the master token alone cannot open a
  shell); the WebSocket handshake checks `Origin` and a single-use ticket, and terminal
  auditing records only metadata (never the typed text or the output).
- Access tokens and credentials never appear in logs, bundles, the audit log,
  activity, fixtures or errors; sensitive settings are masked.
- Privileged operations are recorded in `state/audit.jsonl` (0600, append-only,
  rotated), with no secret values.
- Directory suggestions are confined to `security.fs_roots`; worktree git calls are
  argv-only and confined to the state directory.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Providers and MCP

Each provider is an adapter (`crewhall/harness/<kind>.py`) that translates its
TUI into states and the few actions the core needs. It declares its capabilities
(hooks, local server, task files, MCP) and must pass the **contract kit**
(`tests/adapter_contract.py`). Scaffold a new one with
`crewhall adapter new <kind>`.

The native MCP server is enabled per provider in Settings → Providers; it takes its
identity from the agent environment and every call is authorized by the daemon.

## Supported platforms

Linux (tested on Arch/Omarchy and Ubuntu CI). macOS may work with tmux but is not
tested. Python 3.11–3.14.

## Development

- Tests: `python -m unittest discover` **from the repository root**.
- Lint: `ruff check crewhall tests scripts`.
- One atomic commit per change; version in `pyproject.toml`,
  `crewhall/__init__.py` and `CHANGELOG.md`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT © 2026 melfloc — see [LICENSE](LICENSE).
