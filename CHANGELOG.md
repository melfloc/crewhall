# Changelog

Format: [Keep a Changelog](https://keepachangelog.com), versions follow [SemVer](https://semver.org)
as defined in [RELEASING.md](RELEASING.md). Every release needs a section here: `release.sh` refuses
to run without it and ships these notes with the release.


## [0.60.0] — 2026-10-05

_Fase 2a: mensajería de agentes remotos hacia el daemon local por túnel SSH inverso._

Threat model (túnel de mensajería):

- **Activo**: el socket del daemon es un plano de control completo (crear, matar,
  escribir, ajustes). Exponerlo por el túnel dejaría que cualquier proceso del host
  remoto gobernara la máquina local. **Nunca se reenvía**: cada host con túnel
  tiene su propio *gateway* restringido.
- El gateway solo admite las operaciones de agente autenticadas por token
  (`agent_identity`, `team_send`, `request_create/reply/cancel`) más un `ping`
  mínimo; todo lo demás se rechaza antes de llegar al daemon. Exige token no vacío y
  que el agente que actúa **viva en ese host** (un host comprometido no puede hablar
  como un agente local ni de otro host); el controlador sigue verificando el token.
  Se descartan los campos `_*` del cliente (`_actor` lo pone el gateway) y se acotan
  tamaño, tiempo de lectura y concurrencia.
- El socket del gateway es 0600 en el directorio privado 0700; el remoto vive en un
  directorio 0700, propio y sin symlinks (se verifica antes de reenviar).
- El proceso del túnel ignora `~/.ssh/config` (`-F /dev/null`), conserva las opciones
  estrictas (`StrictHostKeyChecking=yes`, `ForwardAgent=no`, `ExitOnForwardFailure`)
  y se reconecta con retroceso acotado. Con el túnel caído el agente no se lanza con
  un socket inservible (`HostUnreachable`).
- Cerrado por defecto: `hosts.<name>.tunnel` (bool, `false`).

- Nuevo `crewhall/gateway.py` y `crewhall/remote_link.py`; el controlador inyecta
  `CREWHALL_SOCKET` (ruta remota) y `CREWHALL_GATEWAY=1` en los agentes remotos; con
  `CREWHALL_GATEWAY` el cliente nunca autoarranca un daemon en el host remoto.
- Requisito: `crewhall` instalado en el host remoto (el agente usa su CLI).
- Corrección (0.59.0): el entorno del agente llegaba solo al primer agente de cada
  host remoto; con un servidor tmux ya activo el panel no lo heredaba. Ahora el
  comando del panel carga y borra el fichero de entorno, como el backend local.
- Pendiente: hooks/transcripts/worktrees remotos y UI de estado del host.

## [0.59.0] — 2026-10-05

_Agentes en tmux sobre otro equipo, alcanzado por SSH._

Threat model (hosts remotos por SSH):

- Un host configurado **no puede debilitar el transporte**: las opciones SSH son
  fijas (`BatchMode`, `StrictHostKeyChecking=yes`, `ForwardAgent/X11=no`,
  `ClearAllForwardings`, timeouts cortos, `ControlMaster` en un directorio privado
  0700). Nunca se acepta una opción SSH arbitraria del usuario.
- El destino, el `cwd` remoto, el socket de tmux y cada argumento se pasan como
  elementos de `argv` y se re-citan con `shlex` para la shell remota: un nombre de
  host, ruta o agente con metacaracteres no puede inyectar un segundo comando en
  ninguno de los dos lados.
- La clave de identidad debe ser un fichero regular del usuario con permisos
  0600/0400; el `known_hosts` fijado es opcional pero se valida (regular, del
  usuario, no escribible por otros). La verificación de host sigue siendo estricta.
- Los secretos del agente viajan por el **stdin** de SSH a un fichero temporal
  remoto 0600 que se sourcea y se borra antes del `exec`: no aparecen en `ps`
  local ni remoto.
- Un fallo de SSH (código 255) se expone como `host_unreachable` y **nunca** marca
  al agente como `exited`; la sesión se reintenta y se recupera sola.

- **Nuevo backend `ssh-tmux`**: `TmuxBackend` sobre SSH, con las mismas operaciones
  (start/write/capture/resize/terminate/adopt). Una sesión remota se namespacea
  (`<host>__<session_id>`) para no chocar nunca con una local.
- **Nueva tabla `hosts`** en `settings.json` (cerrada por defecto): `ssh`, `port`,
  `identity`, `known_hosts`, `tmux_socket`. Validación estricta que rechaza
  espacios, `-` inicial, metacaracteres de shell, saltos de línea, claves
  desconocidas y valores inseguros.
- Specs y teams aceptan `host`; con host el backend es siempre `ssh-tmux` (`pty` se
  rechaza) y `cwd` es una ruta remota (no se expande en local).
- `SessionInfo`/`to_dict` ganan `host`; el daemon readopta las sesiones remotas por
  cada host configurado en un hilo aparte, tolerando hosts caídos sin bloquear el
  arranque.
- `crewhall agent create --host <name>` y `crewhall attach` operan agentes remotos;
  sin dependencias nuevas (`ssh`/`tmux` del sistema).

## [0.58.0] — 2026-10-05

_The Python package is renamed to match the product._

- **Breaking:** the import package `agent_terminal` is now `crewhall`
  (`python -m crewhall`, `crewhall.cli:main`, `crewhall.harness`, …). Update any
  `import agent_terminal` in your own code or adapters.
- Release-signing namespace stays `agent-terminal`, so signatures keep verifying.
- Fixes found by the first public CI run: `remain-on-exit` is set atomically with
  `new-session` (a command that exits at once no longer loses its exit status), and the
  settings tests no longer depend on a world-writable interpreter.
- The Web UI header, the curses UI title and the control-file heading now say `crewhall`
  (they still read `agent-terminal`); README gains screenshots (`docs/images/`).

## [0.57.0] — 2026-10-05

_Full rename to crewhall; repository prepared for GitHub._

- **Rename activated** (`brand.USE_NEW_PATHS = True`): distribution/command `crewhall`;
  config/state/runtime directories `crewhall`; `CREWHALL_*` environment variables;
  tmux socket `crewhall`; systemd units `crewhall*.service`.
- **Compatibility**: `agent-terminal` command alias; `AGENT_TERMINAL_*` still read (with a
  deprecation notice) and still exported to agents; legacy config/state trees are copied
  (never moved) on first access; `service install` retires the old units; the managed
  control-file section written by earlier versions is recognised and refreshed.
- The import package stays `agent_terminal`; release-signing namespace stays `agent-terminal`
  so already-signed releases keep verifying.
- License holder set (`melfloc`); project URLs point at GitHub.
- Repository: issue/PR templates, dependabot, code of conduct, broader `.gitignore`
  (tool-managed `CLAUDE.md`/`AGENTS.md` are no longer versioned), default branch `main`.

## [0.56.0] — 2026-10-05

Rename groundwork for **crewhall** (the confirmed name): centralized and
reversible, with a compatibility layer. Nothing is published.

### Added
- **`agent_terminal/brand.py`**: the single source of truth for the product name,
  command, environment prefix, directories, socket, control-file markers and
  systemd units.
- **Compatibility layer** (tested): environment variables read `CREWHALL_*` and
  fall back to `AGENT_TERMINAL_*` with a one-time deprecation notice; a `crewhall`
  console command is added and `agent-terminal` kept as an alias; control files are
  written with the new `CREWHALL` markers while legacy `AGENT-TERMINAL` blocks are
  still recognised and refreshed in place; `brand.migrate_tree()` copies (never
  moves) the old config/state/runtime trees, so the change is reversible.
- `scripts/rename_check.py NEW_NAME [--dry-run]` lists every old-name and
  env-var occurrence and prints the plan; tested against a fictitious name.

### Changed
- The filesystem directory default stays on the legacy base
  (`brand.USE_NEW_PATHS = False`) until the migration has run, so an update can
  never break an existing installation. Flipping that one constant (after
  `migrate_tree`) applies the directory rename; the distribution name and docs
  follow in the same dedicated release.

## [0.55.0] — 2026-10-05

Distribution and maturity (nothing is published).

### Added
- `LICENSE` (MIT),
  `SECURITY.md` (how to report, threat model, hardening checklist) and `CONTRIBUTING.md`.
- **README in English** (`README.md`); the Spanish original is kept as `README.es.md`.
  `ARCHITECTURE.md`, `ADAPTERS.md`, `INSTALL.md` and `RELEASING.md` start with an
  English summary.
- `pyproject.toml` metadata: classifiers (Python 3.11–3.14), keywords and
  placeholder URLs.
- **CI** `.github/workflows/ci.yml`: ruff, the full suite on Python 3.11–3.14,
  wheel build + `twine check`, and the privacy scan. A **publish** workflow with
  Trusted Publishing is prepared but only runs on a manual `workflow_dispatch`
  (disabled by default) — it is **not** wired to push/tags.
- `scripts/privacy_scan.py`: scans git-tracked files (and, with `--history`, the
  history) for real paths, emails, hosts, default signing keys and credentials,
  with documented false positives. The working tree is clean.

### Security
- Removed personal references from versioned docs (the host is now `HOST`) and made
  the release/deploy signing key an explicit `AT_RELEASE_KEY` (no hardcoded
  default key). `NOTES.md`, `GAPS.md` and the master prompt are no longer
  versioned.
- `ruff check` is clean across `agent_terminal`, `tests` and `scripts`.

### Verified
- `python -m build` produces a wheel (with `LICENSE`) and `twine check` passes;
  installing it in a clean venv runs `agent-terminal --version` and `doctor`. An
  isolated `selftest` (own XDG dirs + tmux socket) created agents, delivered and
  acknowledged a message and cleaned up; the "turn completes + history" step
  depends on the live model and is noted as residual.

## [0.54.0] — 2026-10-05

Optional per-agent git worktrees, so two agents never step on each other's files.

### Security
- **Threat model (Fase 6).** *Assets*: the user's repository and work in progress. *Adversary*: a
  mistyped name, a non-git workspace, or deletion of unmerged work. *Controls*: worktree mode is
  opt-in (default shared) and only on a real git repo; names are sanitized; every git call is argv
  (no shell); paths are confined to `state/worktrees`; on delete the worktree is removed only if it is
  clean and has no unmerged commits — otherwise it is kept with a warning, and discarding needs a
  typed `DISCARD`.

### Added
- `workspace_mode: shared | worktree` per agent and per team (default `shared`). In worktree mode the
  agent gets a branch `at/<team>/<agent>` in a worktree under `state/worktrees/<team>/<agent>`; a
  non-git workspace falls back to shared with a visible warning (never silently).
- The agent header shows its branch (and the fallback warning); New agent exposes the choice.
- `clean` lists orphan worktrees and flags dirty/unmerged ones (never removes them); deleting an agent
  keeps an unsafe worktree; `agent-terminal`/Web can discard one explicitly. Bundles carry the mode,
  and `fs_complete` already allows the agent's worktree because it is its cwd. Worktree state is not
  touched by a full reset.

## [0.53.0] — 2026-10-05

Request/response between agents (assign vs handoff), built on `Delivery`/`_watch_ack` without ever
inferring completion from terminal quietness.

### Security
- **Threat model (Fase 5).** *Assets*: who may answer whom, and bounded resources. *Adversary*: an
  agent forging a reply, looping requests between two agents, or flooding them. *Controls*: a reply is
  only accepted from the request's original recipient (identity by token), a bounded number of open
  requests per agent, a maximum chain depth, an anti-loop rule (no two agents waiting on each other),
  a default expiry, a body cap and a bounded, secret-free persistence.

### Added
- **Correlated requests**: `Message` gains `kind ∈ {message, request, reply, handoff}`, `request_id`,
  `reply_to` and `deadline`; a request moves `pending → accepted → replied | expired | cancelled |
  failed`. The answer is a matched message, never a guess.
- CLI `agent-terminal message request|reply|requests`; MCP tools `request(to, task, timeout)`,
  `reply(request_id, message)` and `handoff(to, message)` (one-way). `request` returns the reply or the
  terminal state.
- Limits in Settings → Requests (open requests per agent, chain depth, default timeout, max body),
  with conservative defaults. Requests survive a daemon restart (reconstructible state, no secrets).
- Mission Control lists open requests per team (who waits on whom, age, state) and can cancel them;
  creating, replying, cancelling and expiring are all audited.

## [0.52.0] — 2026-10-05

Native MCP surface for agents, off by default.

### Security
- **Threat model (Fase 4).** *Assets*: agent identity and Team boundaries. *Adversary*: a compromised
  agent (prompt injection) trying to impersonate another agent, read outside its Team, flood the
  daemon or run privileged actions. *Controls*: the server takes its identity **from the environment**
  (`AGENT_TERMINAL_AGENT_ID`/`AGENT_TERMINAL_TOKEN`) and never accepts a `from`; every call goes to the
  daemon over the 0600 socket, which is already authoritative for identity and Team membership. Strict
  argument schemas, a body cap, a per-agent rate limit and only four tools; no screen/file/state reads
  and no command execution.

### Added
- **`agent_terminal/mcp/`** — a JSON-RPC 2.0 stdio MCP server with the standard library only
  (`whoami`, `list_teammates`, `send_message` within the Team, `new_session` for itself). Missing or
  wrong identity is refused; a teammate outside your Teams is rejected by the daemon.
- Per-agent MCP injection **without touching the user's CLI config**: Claude gets a temporary 0600
  `--mcp-config` file and Codex gets `-c mcp_servers.…` flags; the file holds no secret (the server
  reads its identity from the inherited environment) and is deleted when the agent closes. OpenCode
  has no verified injection flag, so it stays without MCP (documented).
- `providers.<kind>.mcp` setting, **off by default**, with a Settings → Providers toggle that explains
  exactly what it exposes. Only Claude and Codex are offered it; OpenCode's is disabled. Each tool call
  is audited (operation and result, never message bodies).

### Tests
- Protocol (initialize/tools/list/tools/call/errors), impersonation impossible (no `from`), out-of-Team
  rejected, argument/body validation, rate limit, missing-identity refusal and the per-provider launch
  wiring (0600 file, no token, deleted on close).

## [0.51.0] — 2026-10-05

Codex adapter, built on the Phase 2 capability interface (the core is untouched).

### Added
- **Codex CLI adapter** (`harness/codex.py`, Codex 0.158.0). State by evidence: the input placeholder
  ``› Ask Codex to do anything`` → READY, ``• Working (Ns • esc to interrupt)`` → WORKING, a folder-trust
  dialog → STARTING (never auto-accepted). Model read from the footer (`GPT-5.6-Terra medium · cwd`),
  ``/new`` for a fresh conversation, one ESC to interrupt.
- **Exact turn signal and history** from Codex's rollout JSONL: `codex_rollout.py` reads
  `~/.codex/sessions/**/rollout-*.jsonl` read-only, `realpath`-confined, with size/line caps and a
  validated id, using `task_started`/`task_complete` and `turn_context.model`. `session_meta` and
  account ids are never returned.
- `config_risks()` warns (read-only) when `~/.codex/config.toml` has `approval_policy = "never"` or an
  open sandbox; shown in red in Settings → Providers.
- Codex appears by itself in Settings, New agent and the wizard (registered in `HARNESSES`); the
  contract kit covers it and `AT_RUN_CODEX=1` runs a minimal real check. `ADAPTERS.md` §8 documents the
  signals and limits.

### Security
- The trust dialog is never accepted programmatically; no dangerous Codex flags are injected, and the
  rollout reader only ever reads inside `~/.codex/sessions`.

## [0.50.0] — 2026-10-05

Provider capability interface and a contract kit, so adding an adapter no longer means touching the core.
Refactor with no intended behaviour change; the existing suite is the safety net.

### Added
- **Capabilities on `Harness`** (the controller branches on these, never on `kind`): `uses_hooks`,
  `uses_local_server`, `supports_task_files`, `submit_delay`, `dangerous_flags`, `activity_snapshot()`,
  `model_from_screen()`, `usage_from_screen()`, `running_shells()`, `mcp_launch_args()` and
  `config_risks()`. Claude and OpenCode implement them; a new provider only implements what its TUI
  offers. `send()` now honours `submit_delay` between the text and Enter.
- **Contract kit** `tests/adapter_contract.py`: every adapter inherits it (three lines) and gets, for
  free, fixture-driven state checks, "a trust/login dialog is never READY", "WORKING only with a
  positive marker", "a broken screen never raises", safe `command()`/`launch_args()`, malicious
  history-id tolerance, a fixture secret scanner and the `kind` allow-list. Claude and OpenCode are
  migrated to it (screen fixtures under `tests/fixtures/screens/<kind>/`).
- **Generator** `agent-terminal adapter new <kind>` writes `harness/<kind>.py`, the fixture directory
  and a contract test — and does **not** register it until it passes. Tested.
- Settings → Providers shows each provider's `config_risks()` in red (e.g. permissive Claude
  `bypassPermissions`).

### Changed
- `grep -rn 'kind == "claude"|"opencode"' agent_terminal` is 0 outside `harness/`.

## [0.49.0] — 2026-10-05

Security hardening, applied **before** any new surface is added. Everything here is fail-closed: no
new capability is on by default and nothing accepts a trust/permission dialog automatically.

### Security
- **Threat model (Fase 1).** *Assets*: the access token, the daemon's ability to run processes and
  read the host's filesystem, and the agent identities. *Adversaries*: a hostile web page open in the
  same browser (CSRF/XSS), another local user on the machine, a stolen token over the tailnet, and a
  compromised agent trying to escalate through the daemon's tools. *Controls*: a strict CSP plus
  framing/nosniff/referrer headers on every response; per-IP login throttling with backoff and a
  temporary lockout; an append-only audit log that never stores secrets; `fs_complete` confined to
  configured roots (realpath, symlink/`..` escapes rejected); typed `CONFIRM` for provider commands
  and environment; and read-only warnings when local users exist or a CLI config is permissive.
- **Strict security headers on every Web UI response**: `Content-Security-Policy` with no
  `unsafe-inline`/`unsafe-eval` (inline CSS/JS moved to `/static/login.css` + `/static/login.js`),
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` + `frame-ancestors 'none'`,
  `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy`, and `Cache-Control: no-store` on
  `/api/*`.
- **Login throttling**: five failed attempts per IP trigger an exponential backoff and a temporary
  lockout (bounded, in-memory); the sixth attempt is refused even with the correct token. Token
  comparison stays constant-time and error messages never enumerate anything.
- **Audit log** `state/audit.jsonl` (0600, append-only, rotated by size): logins (ok/fail), token and
  session rotation, `settings_set`/`settings_reset`, `frontend_set`, `reset_*`, `bundle_*`,
  `clean_*`, agent create/stop/send/new-session, messages, team changes, permission answers and
  process signals. Sensitive settings are recorded by key and a short hash, never by value. New
  **Settings → Audit** read-only, filterable tab.
- **`fs_complete` is scoped**: a new `security.fs_roots` setting (default `~`, plus every team
  workspace and running agent's directory) limits directory suggestions; `realpath` rejects `..` and
  symlink escapes, and there is a per-client rate cap.
- **Sensitive provider settings require typed `CONFIRM`** (command or environment) and are audited;
  a configured command must exist, be executable, not be writable by other users, and be absolute
  (bare names are resolved in `PATH`). Warnings show for binaries outside `PATH`/`$HOME`.
- **`security.local_requires_token`** (default off, so current local use is unchanged): when on, the
  localhost listener demands the token like the tailnet one. `doctor` and Settings warn when other
  interactive local users exist.
- **`doctor`** gains checks for token/settings/audit modes, local users, writable provider binaries
  and permissive Codex/Claude configs (read-only).

### Changed
- Changing `providers.*.command` or `providers.*.env` now needs a typed confirmation; the Web UI
  asks for it and sends `confirm=CONFIRM`.
- The login page no longer uses inline CSS/JS (required by the strict CSP).

### Tests
- Header/CSP checks on every route, brute-force lockout, audit rotation/secret-hygiene and
  `fs_complete` escape tests. 686 tests, 17 skipped.

## [0.48.0] — 2026-10-05

Baseline and hygiene phase. Nothing new is exposed; the goal is that running the test suite can never
touch the user's live sessions and leaves nothing behind.

### Security
- **Threat model (Fase 0).** *Asset*: the user's live tmux sessions/agents and the machine's temp
  filesystem. *Adversary*: not an attacker but the test suite itself, importing `agent_terminal` (and
  thus `backends.tmux`) before `tests/__init__` sets the isolated socket. *Vector*: a module-level
  `SOCKET = os.environ.get(...) or "agent_terminal"` froze the production socket for the whole run, so
  test backends, `attach` and `clean` killed/adopted the user's sessions. *Control*: the socket is now
  resolved on every use from `AGENT_TERMINAL_TMUX_SOCKET`; the tests always pick a private socket and a
  regression guard fails if any tmux call reaches `agent_terminal`.

### Fixed
- **Tests could kill the user's live agents.** `unittest discover` imports the `agent_terminal` package
  before `tests/__init__` runs, so `backends.tmux.SOCKET` (and the default arguments of
  `TmuxBackend()`, `attach_command()` and `existing_sessions()`) were bound to the production socket
  `agent_terminal` for the whole run. A full `discover` left `sh`/`opencode` sessions on the user's tmux
  server and made `test_tests_never_use_the_production_tmux_socket` fail depending on import order. The
  socket is now resolved lazily (`socket_name()`) at every use; `tmux.SOCKET` still works for callers.
- **Dialog race in the Web UI.** A native `close` event fires asynchronously; when a dialog was replaced
  by the next one (confirm → result, e.g. *Rotate token*), the stale event closed the new dialog, which
  flashed open and shut. The handler now ignores close events for a dialog that is no longer open.
- Tests no longer leave `/tmp/at-*` directories or orphan `opencode`/`claude` processes behind: the run
  snapshots pre-existing temp dirs, kills processes holding test artifacts and sweeps only what it
  created.

### Tests
- `test_tmux_isolation` gains guards for lazy socket resolution, a "never touch `agent_terminal`"
  interceptor and a sweep test; 680 tests total, 17 skipped.

## [0.47.1] — 2026-10-05

### Fixed
- **Directory autocomplete stopped drawing after the first suggestion.** The native `<datalist>` does not refresh
  its options while its menu is open, so the second and later levels were fetched but never shown. Replaced by an
  ARIA combobox of our own (Down/Up, Enter or Tab accept, Esc closes the menu only, stale answers discarded), with
  regression tests that type like a user: pick a suggestion, keep typing, narrow, go three levels deep.

## [0.47.0] — 2026-10-05

### Added
- **Settings panel (gear icon in the header)** — everything configurable without a terminal:
  - **Providers**: enable/disable Claude Code and OpenCode (disabled ones vanish from New agent, the wizard and
    the palette; running agents are untouched; at least one must stay enabled), point each at another binary or
    wrapper, default model, default arguments and environment variables (secret-looking values are masked and
    kept when left as shown), with a *Test* button that reports the path and version found.
  - **Agents**: default provider and backend, lifecycle hooks, conversation reading, instruction files, permission
    wait. Values forced by an environment variable are shown locked, with the variable's name.
  - **Access & network**: Web UI mode (local / Tailscale / both) and port, **rotate the access token** (shown once,
    other devices signed out, this browser stays in), sign out all devices, session lifetime, extra allowed hosts.
    Turning the Web UI *off* is refused from the Web UI itself so you cannot lock yourself out.
  - **Maintenance**: temp-file age/size caps, janitor interval, backups kept; shortcuts to cleanup and bundles.
  - **Interface**: theme, notifications, setup wizard (this browser only).
  - Stored in `~/.config/agent-terminal/settings.json` (0600, atomic), validated on write and on load: a broken
    file falls back to defaults and can never stop the daemon from starting.
- **Emergency reset** (Settings → Emergency), every level previewed before it runs: *Clean up* (dead sessions, stale
  temp files, orphan tmux sessions, caches; agents untouched), *Restart Web UI* (+ listeners restarted, browsers
  signed out) and *Full reset* (stops every agent, clears the temp directory, optionally forgets saved
  teams/agents and restores settings, then restarts the daemon in place — same pid). A full reset needs you to
  type `RESET`, always takes a `pre-reset-*.tar.gz` backup first and does nothing if the backup fails.
- **Directory autocomplete** on every workspace / working-directory input (new agent, new team, team workspace),
  reading the daemon host's own filesystem: directories and sub-directories only, one level at a time.

## [0.46.0] — 2026-10-05

### Added
- **Download and upload bundles in the Web UI.** Each saved bundle has a *Download* link and the dialog has
  *Upload bundle…*. An upload is verified (manifest, allow-list of paths, checksums) before it is ever listed,
  stored under a fresh `uploaded-<date>-<name>.tar.gz` name with mode 0600, and capped at 8 MB. Downloads and
  deletes only accept names inside the bundles directory (no traversal, symlinks out of it are refused).
- **Delete bundles.** A trash button per bundle and *Delete empty / invalid (N)* to clear junk in one go, always
  after a confirmation. The list now says what each bundle holds (files, teams) and flags empty, invalid and
  token-carrying ones; empty/invalid bundles cannot be imported, only downloaded or deleted.

## [0.45.0] — 2026-10-05

### Added
- **Bundles now carry your running teams and agents.** Before, a bundle only held `profiles.toml` and team files, so
  a setup built in the Web UI exported as an empty bundle. Export now adds each running team as a rebuildable
  definition (team name, workspace, and per agent: name, kind, backend, directory, args) — no ids, pids,
  sessions or conversations. A team file already on disk with the same name wins.
- **Import can recreate them.** The Web UI preview lists the teams and agents and flags those whose directory does
  not exist on this machine (they are skipped, never started elsewhere); *Import and create* runs the idempotent
  `team_up`. CLI: `agent-terminal bundle import FILE --apply` (and `bundle export --no-live` to leave the running
  teams out).

## [0.44.0] — 2026-10-05

### Added
- **Mission control per team.** The dialog has a scope selector (*All agents* or *Team: …*); a team view shows only
  its members, a one-line summary (working / need you / running commands / stopped) and the messages its members
  exchanged. Opened from the team's ⋯ menu ("Mission control") or from the command palette
  ("Mission control: <team>").

## [0.43.2] — 2026-10-05

### Fixed
- Web UI printed the literal text `null` ("nullnull" next to the team counter, and `null` under an agent's header
  when it has no evidence line). Null children are now dropped before they reach the DOM.

## [0.43.1] — 2026-10-05

### Fixed
- **Model detection in the agent header.** Before the first message there was no model to read; now the model the
  TUI itself shows (Claude header, OpenCode prompt footer) is used, then the exact id from the transcript / server
  once there is one. A huge tool result can no longer hide it, the last known model survives the agent exiting, and
  `--model` given at creation is the last resort.

## [0.43.0] — 2026-10-05

### Added
- **Live agent activity in the sidebar and header.** Each agent now reports what it is doing from real signals
  (state, pending questions, the running tool in the Claude transcript / OpenCode server, background shells):
  thinking, using a tool, running a command, running a sub-agent, needs your answer, waiting for input, idle,
  background processes, starting, exited, error. *Waiting for input* is a static state with no animation.
- **Model in the agent header** (observed from the transcript / server; `model n/d` when not known yet).
- **Conversation follows the latest activity**: only the newest tool / sub-agent / result block is expanded; it
  closes when the next one appears. Blocks you open or close by hand are left alone.

### Fixed
- The bottom buttons of the left panel wrap instead of being cut off when the panel is narrow.

## [0.42.0] — 2026-10-04

### Changed
- **Web UI accessibility and sidebar performance.** A visible focus ring everywhere, stronger text contrast on both
  themes, `aria-live` on the summary/sidebar/conversation/toasts, and full keyboard use of the action menus
  (arrows, Home/End, Esc returns focus). The sidebar now updates incrementally: when the structure has not changed
  it refreshes the existing rows instead of rebuilding them on every state push.

## [0.41.0] — 2026-10-04

### Added
- **First-open setup wizard (Web UI).** When the UI is opened for the first time it offers a starting point —
  *Reviewer + executor* (a Claude reviewer and an OpenCode executor in one team) or a single agent — with an
  editable team name and backend, and creates it through `team_up`. Skippable and re-openable from the command
  palette ("Setup wizard"); the choice is remembered in the browser.

## [0.40.0] — 2026-10-04

### Added
- **Archive of closed agents.** When an agent is deleted, its definition and a summary of its messages are written
  to `<state>/archive/<id>.json` (private, no schema change) instead of being forgotten. The Web UI lists archived
  agents and shows their message summary (read-only; they are never restored from there). Ops `agent_archive_list`,
  `agent_archive_get` (additive).

## [0.39.0] — 2026-10-04

### Added
- **Export / import configuration from the Web UI.** A sidebar button exports the profiles and team files to a
  server-side bundle and lists saved bundles to import (with a preview first). The destination is fixed by the
  server (no client paths) and import accepts only a bundle name inside that directory, so it cannot be used for
  path traversal. Ops `bundle_export`, `bundle_list`, `bundle_import` (additive).

## [0.38.0] — 2026-10-04

### Added
- **Session & access (Web UI).** A header button opens a panel listing the connected devices (non-sensitive label:
  IP + a short user-agent, with last-seen), the token status (fingerprint, never the token) and a visible *Sign out*
  that clears the cookie and revokes the session server-side. Ops `web_sessions`, `web_session_revoke`,
  `web_token_status`, `web_token_revoke` (handled in the web layer, additive).

## [0.37.0] — 2026-10-04

### Added
- **Update status in the Web UI.** The sidebar shows a *Restart pending* badge when an update is installed but the
  daemon still runs the old code (page vs daemon version), and a dialog with the installed version, whether a
  rollback is available, and the exact command to apply it — with the warning that restarting closes every agent.
  It only shows and warns; running the restart stays a deliberate terminal action. Daemon op `update_status`.

## [0.36.0] — 2026-10-04

### Added
- **Integrated cleanup.** `agent-terminal clean` removes test leftovers in the temp dir, old control-file backups and
  old update backups (keeping the newest per group) and, in a managed install, old releases. It is a dry run by
  default, asks for confirmation, never touches a live agent, and only deletes clearly-known artifacts. The Web UI
  has the same thing from a header button / the command palette: it lists the plan and removes it after a confirm.
  Daemon ops `clean_plan` / `clean_apply` (additive).

## [0.35.0] — 2026-10-04

### Added
- **Cost & usage (Web UI).** A panel with tokens, estimated cost and active time per agent and per team, plus a
  session total. Values come from what each CLI actually shows (today: OpenCode's session footer); anything not
  reported is shown as *n/d*, never guessed. `agent_summary` gained a `usage` field (additive).

## [0.34.0] — 2026-10-04

### Added
- **Agent timeline (Web UI).** A per-agent bar of the day's work / wait / error periods and the duration of recent
  turns, reachable from the header, the agent menu and the command palette. Built from the state changes the page
  observes while open, and it says so when nothing has been observed yet.

## [0.33.0] — 2026-10-04

### Added
- **Richer Conversation view (Web UI).** Tool calls show a one-line summary and colour-coded diffs (or pretty JSON),
  fenced and indented code is syntax-highlighted with a small built-in highlighter (keywords, strings, numbers,
  comments; no external library), `Ctrl+F` searches within the conversation with match count, and each message has
  a copyable `#msg-N` link that scrolls to it when opened.

## [0.32.0] — 2026-10-04

### Added
- **Colour in the Live view (Web UI).** The raw terminal output now renders basic SGR (bold/dim/italic/underline/
  strike, 16/256/truecolour foreground and background, reset, inverse). It is done with a small local parser and
  the text is always inserted as text nodes, so HTML/`<script>`/`<img onerror>` in the stream is shown literally —
  never executed (covered by an anti-XSS test).

## [0.31.0] — 2026-10-04

### Added
- **Multiline composer (Web UI).** The prompt box grows with the text, `Enter` sends and `Shift+Enter` adds a line,
  `↑`/`↓` walk your recent prompts, and named prompt templates can be saved, inserted and deleted (all kept in the
  browser, never sent anywhere).

## [0.30.0] — 2026-10-04

### Added
- **Command palette (Web UI).** `Ctrl+K` opens a filterable palette: switch to an agent, create an agent or a
  team, send to an agent, stop the working agent, go to a tab, or open the inbox / Mission control. Arrow keys move,
  Enter runs, Esc closes.

## [0.29.0] — 2026-10-04

### Added
- **Mission control (Web UI).** A board with one card per agent (state, last activity, what it is doing now,
  pending answers, messages sent/received) plus a timeline of the message flow between agents (who → who, with
  delivery status and time). Live while open; built only from the existing state snapshot.

## [0.28.0] — 2026-10-04

### Added
- **Browser notifications (Web UI).** A bell in the header asks for the Notifications permission on a user click
  and, once enabled, shows a desktop notification when an agent finishes its turn, errors or exits, and when one
  asks for a permission or a question. The preference is saved; while off nothing is shown.
- A Service Worker at `/sw.js` is registered and already handles `push` and `notificationclick` (focusing the app),
  so Web Push only needs a server-side push subscription and VAPID keys. **Web Push is not implemented yet:** there
  is no server push backend, so notifications only fire while the page is open. No new dependency was added.

## [0.27.0] — 2026-10-04

### Added
- **“Needs your answer” inbox (Web UI).** One place with every pending permission and question from *all* agents,
  answerable with the same cards as the agent view (Allow once / Always allow / Deny with a note, or the question
  form) without opening the agent — plus an *Open agent* shortcut. A counter shows on the header button, in the
  sidebar and in the tab title.

## [0.26.2] — 2026-10-04

### Changed
- **Widened the Web UI browser regression net** (no product change): sidebar filters/search and the team/agent
  action menus, the create-agent / create-team / member-picker / confirm dialogs, the permission and
  `AskUserQuestion` cards, the Processes tab and its signal confirmation, and the mobile 390 px drawer.

## [0.26.1] — 2026-10-04

### Changed
- **Web UI is split into modules** (`app.css` + `js/*.js`) instead of one inline page, served from `/static/*`
  with `no-store` so a reload always gets the current code. No behaviour change: every function, shortcut and
  dialog of 0.26.0 is preserved byte for byte.

### Security
- `/static/*` is resolved against the static directory with a realpath containment check and a flat 404 for
  anything outside it (path traversal covered by tests).

## [0.26.0] — 2026-10-04

### Changed
- **Web UI redesign.** New design system (light/dark theme with a toggle, tokens, one set of buttons, inputs,
  chips, pills, menus, dialogs and toasts) replacing the ad-hoc styles. Sidebar with search, filters (All / Working /
  Needs you), collapsible team cards with an actions menu and per-agent menus; the browser `prompt()`/`confirm()`
  dialogs are now proper dialogs (member picker with checkboxes, workspace and team forms). Header with copyable
  path/pid chips, a Stop button while an agent works, "Jump to latest", a collapsible message log, a responsive
  drawer on phones, keyboard shortcuts (`/` search, Alt+↑/↓ switch agent) and reduced-motion support.

### Added
- **Live "working" indicators**: spinning avatar ring and status dot, elapsed-time counter, an indeterminate bar under
  the header, a typing animation in the Conversation, a toast when an agent finishes or needs you, and the tab title
  and favicon reflect how many agents are working or waiting.

## [0.25.1] — 2026-10-04

### Fixed
- **OpenCode agents with a custom agent (`--agent ejecutor`) refused every message after the first turn.** The
  composer footer then reads `<Agent> · <model>` instead of `Build · <model>`, so the echo of the previous message
  was taken for residual input: messages failed with `input line not clean`, `agent new-session` was refused, and
  the delivery retry kept pressing keys. The input line is now read from the composer's structure (the `┃` block
  closed by `╹▀▀▀`, whose last line is the agent/model footer), whatever the agent is called.
- The Web UI reloads itself when agent-terminal is updated (a page left open kept the old code), and the page is
  no longer cached by the browser.
- Processes: the finished commands listed are those of the agent's current conversation.

## [0.25.0] — 2026-10-04

### Added
- **Web UI *Processes* tab: the commands an agent is running, live.** For each one: command, pid, state, running
  time, CPU, sub-processes and how long ago it last wrote output (flagged after 60 s of silence). Click it to follow
  its output (Claude: its `tasks/<id>.output` file; OpenCode: the running tool's output from its server). *Ctrl-C*,
  *Terminate* and *Kill* stop it with everything it started; only processes under that agent can be signalled, and
  each signal is logged. Claude's finished commands that left an output file (background ones) are listed too.
  Their input is `/dev/null` in both CLIs: they can be watched and stopped, not typed into.
- Daemon ops `agent_processes`, `agent_process_output`, `agent_process_signal` (allowed from the Web UI, logged as
  `by: web`).

### Fixed
- **Claude looked stuck while it had a background shell.** Its footer then reads
  `auto mode on · 1 shell · ← for agents · ↓ to manage` (no "shift+tab to cycle"), which was taken as "TUI not
  mounted": state `starting`, no messages delivered. Every footer variant is recognized now.
- OpenCode does not notice a tool command killed from outside (its turn stays "running" forever): when that
  happens agent-terminal stops the turn through OpenCode's API (as ESC would), so the agent is usable again.

## [0.24.0] — 2026-10-04

### Added
- **`agent-terminal agent new-session <agent>`**: start a clean conversation in an agent (Claude `/clear`,
  OpenCode `/new`) and confirm it. Run from inside an agent it acts as that agent: only teammates, never itself,
  token required (op `team_new_session`; `agent_new_session` for the user and the Web UI). Waits up to 20 s for the
  agent to be idle, otherwise refused; nothing is queued. Documented in the managed CLAUDE.md/AGENTS.md section so
  orchestrators use it instead of sending `/clear` as a (prefixed, never executed) message.
- Web UI: *new session* action in the agent header (asks for confirmation).

### Fixed
- **OpenCode `/new` did not clear the Conversation view.** OpenCode creates the new session only with the first
  prompt, so no event arrived. Submitting `/new` (or `/clear`) through agent-terminal now leaves the conversation
  at once, and the start screen is detected when `/new` is typed straight into the TUI. The left conversation is
  never re-adopted by discovery or by its own late activity; the new one is adopted when OpenCode creates it.
- An agent told to start a new session is idle afterwards (it no longer waits for a reply that never comes).

## [0.23.0] — 2026-10-03

### Added
- **Claude Code permission dialogs and `AskUserQuestion` in the Web UI.** A `PermissionRequest` hook hands the
  request to the daemon, which shows it as the same card used for OpenCode: *Allow once* / *Always allow* (only
  when Claude proposes rules; they are applied as Claude suggested them) / *Deny* with an optional note, and the
  question form (Claude always allows a free-text answer). The answer is returned to Claude as the hook's decision.
- *Answer in terminal* hands the dialog back to Claude's TUI. Claude also shows its own dialog when nobody answers
  within `AGENT_TERMINAL_PERMISSION_WAIT` seconds (default 90, `0` disables the bridge), and immediately when no
  Web UI is open (the wait only happens while someone is viewing it). Any hook failure falls back to the TUI.
- Requires restarting the Claude agents so they load the new hook.

## [0.22.0] — 2026-10-03

### Added
- **Answer an agent's permission prompts and questions from the Web UI (OpenCode).** When OpenCode asks for a
  permission (`permission.asked`) or asks the user a question (`question.asked`), the request shows up as a card
  above the composer of that agent, and as a badge in the agent list. Permissions: *Allow once* / *Always allow* /
  *Deny* (with an optional note). Questions: single or multiple choice, free text when the CLI allows it, or
  *Dismiss*. The answer goes back through OpenCode's own API; answering in the TUI closes the card too, and
  pending requests are re-synced when the bridge reconnects. Subagent requests are included.
- Daemon ops `interaction_list` and `interaction_respond` (also allowed from the Web UI; answers from the Web UI
  are logged as `by: web`). Agent summaries include their pending `interactions`.

### Not included
- Claude Code permission dialogs / `AskUserQuestion` are not bridged yet (needs a blocking `PermissionRequest`
  hook; pending a decision).

## [0.21.7] — 2026-10-03

### Fixed
- **`/clear` (Claude) and `/new` (OpenCode) left the Conversation view on the old session.** The agent now
  follows the session its TUI is in: Claude reports it through a new `SessionStart` hook (fired on start,
  `/clear` and `/resume`) and every hook now carries the `session_id`; OpenCode adopts each new root session
  (`/new`) or the one with activity (`/sessions`). The new id is persisted.
- OpenCode subagent (task) sessions are never taken as the conversation, and their busy/idle events no longer
  count as the agent's own turn.
- The Web UI resets the Conversation view when the conversation changes (`agent_history` now also returns
  `conversation_id`), instead of mixing the old and new sessions.
- Restoring the web-interface mode at daemon start no longer blocks 15 s waiting on itself (the restore
  called the public `set_mode`, which waits for the restore to finish).

## [0.21.6] — 2026-10-03

### Fixed
- **The saved web-interface mode could be lost.** A failed restore (Tailscale not connected yet at boot, a busy
  port) rewrote `frontends.json` to `off`. The saved mode is the user's intent: it is never rewritten by a failed
  restore, which now retries in the background (every 10 s for up to 10 min) until it works.
- A status request right after the daemon starts waits for the restore instead of reporting `off`.
- The test suite is fully isolated from the user's real state/config/runtime (a test daemon had overwritten the
  real saved mode).

## [0.21.5] — 2026-10-03

### Fixed
- Claude's internal wrappers (`<pasted_content>`, `<command-name>`, `<local-command-stdout>`) are no longer
  shown in the Conversation view; the agent's own text is never rewritten.
- An agent sending to a busy recipient is released after 5 s (the bounded retry queue takes over) instead of
  being blocked for up to 30 s inside its turn.
- A client disconnecting mid-request is one log line, not a traceback.
- Repository hygiene: four Bun temp libraries (53 MB) that had been committed by accident were removed from `HEAD`.
- A test no longer inspects the live daemon.

## [0.21.4] — 2026-10-03

### Fixed
- `update --restart` / `--rollback --restart` reported "daemon did not start": the new daemon was launched
  while the old one still held its lock (socket already gone) and exited immediately. The updater now waits
  for the old daemon to release the lock and allows 30 s for the new one.

## [0.21.3] — 2026-10-03

### Fixed
- **A refused daemon restart closed the agents it protected**: `update --restart` with agents running was
  refused, but the refusal was handled like a failure (rollback + forced restart). Refusals are now checked
  *before* anything changes, are a distinct outcome (exit code 3) and never roll back or restart.

### Added
- `agent-terminal update --restart` (without `--from`) applies an already-installed update when idle.

## [0.21.2] — 2026-10-03

### Fixed
- The installed command, the daemon, systemd units and the updater now run Python with `-P`, so a package
  named `agent_terminal` in the current directory (e.g. a dev checkout) can no longer shadow the installed release.

## [0.21.1] — 2026-10-03

Supersedes 0.21.0, whose update path was defective (found in the pre-office rehearsal; 0.21.0 was never
installed on a real machine). The 0.21.0 tag is kept: released versions are immutable.

### Fixed
- `agent-terminal update` left a broken installation: a renamed virtualenv keeps the path it was created in
  inside its console scripts. The command is now a path-based launcher (`python -m agent_terminal`), shebangs
  are repaired after the swap, and the swapped environment is verified — **rolled back automatically** if it fails.
- A failed post-swap check used to lose the recorded version in `install.json` (rollback bookkeeping).
- `signing.sign()` could leave a stale `.sig` in place (ssh-keygen keeps an existing one silently).
- `selftest` created the same team twice and left empty teams behind; it now creates one and cleans up by id.

## [0.21.0] — 2026-10-03 (superseded by 0.21.1)

First release cut with the new release/update mechanism.

### Added
- **Releases and updates**: `scripts/release.sh` (gated, signed, tagged, built from the commit),
  `scripts/deploy.sh`, `agent-terminal update` (verify → build+test new venv → atomic swap → rollback),
  `agent-terminal selftest` (real end-to-end check), build info in `--version`/`ping`/`doctor`.
- **Dynamic interfaces**: `agent-terminal local | tailscale | off | frontends`, hosted by the daemon,
  switched at runtime, persisted, token always required over Tailscale.
- **Install on another machine**: `install.sh`/`uninstall.sh`, `doctor`, `bundle export/import`,
  `INSTALL.md`, example team/profile files.
- **Conversation view** (Web UI) for Claude and OpenCode: structured, scrollable, live, with Ctrl+C to stop;
  OpenCode agents expose their own local server for exact turn events and history.
- Claude lifecycle hooks (`--settings`) for exact turn signals; they add to, never replace, project hooks.
- Message attribution (`[from: name]`), delivery status (queued/injected/acknowledged), de-duplication.
- Declarative teams (`agent team up`), profiles, agent command arguments (`--args`).
- Agent interrupt (`agent_interrupt`), PgUp/PgDn/Home/End/Ctrl+O keys in Live view.

### Changed
- `CLAUDE.md`/`AGENTS.md` injection rewritten to be safe by construction: byte-exact, append-only at the
  end, verified, backed up, concurrency-safe; each agent kind gets the file it actually reads.
- Daemon owns web listeners; `WebServer.stop()` now really closes the socket and open connections.
- Requires Python ≥ 3.11.

### Fixed
- Messages injected into Claude were swallowed when input clearing opened the Rewind dialog.
- Claude/OpenCode state detection no longer depends on scrolled-away text.
- Unknown web paths hung the connection; request bodies are capped; terminal sizes are validated.
- Test runs killed live agents (shared tmux server); a test leaked `TMP` into the environment.
- A second daemon could start when runtime files were removed under a live one.
- The Web UI page was missing from built wheels.

### Security
- Agent tokens no longer appear in `ps` (private env file); relative `TMPDIR` is ignored.

## [0.19.0]
Baseline before the changelog (reliable replies, clean delete, team-bound agents, Tailscale access).
