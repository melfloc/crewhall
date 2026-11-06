# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**: use the repository's
"Report a vulnerability" (GitHub private advisory) at
<https://github.com/melfloc/crewhall/security/advisories/new>. Do not open a public issue for a security bug.
Include: what you did, what happened, the version (`crewhall --version`)
and, if possible, a minimal reproduction.

We aim to acknowledge within a few days and to publish a fix and an advisory
once a patched release exists.

## Scope and threat model

crewhall runs coding-agent TUIs on **your** machine and exposes a small
control plane (a 0600 UNIX socket and an optional Web UI). The invariants we
care about:

- **Local control plane**: the daemon socket is `0600`. Reaching the socket is not
  enough to drive it: every operation that is **not** an agent operation (see below)
  requires a **control token** kept in a `0600` file under the state directory
  (`control-token`), which the CLI, the Web UI and the in-process UI read
  automatically. The token is **never** injected into an agent's environment.
- **The agent is a partial adversary**: an agent runs as the same Unix user as the
  daemon, so filesystem permissions alone cannot separate them. The daemon therefore
  also checks the caller's process credentials (`SO_PEERCRED`), pins the connected
  process with `SO_PEERPIDFD` for agent creation/stopping, and walks `/proc`:
  a process that **descends from an agent session** may only use the agent operations
  (`agent.identity`, `team.send`, `request.*`, `agent.new_session`/`team.new_session`,
  lifecycle/permission hooks, `team.members`, `ping`, publishing to and co-editing
  the chat it leads or crews). `agent_create` and `agent_stop` have a separate,
  narrower route described below. The same applies to a process whose environment still
  carries a live agent's `CREWHALL_TOKEN` (e.g. a double-forked child reparented away
  from the agent). Such a caller is refused the control plane even with a valid
  control token. This stops an agent that merely *uses* `crewhall` (or a careless
  tool it runs) from approving its own permission prompts, writing to an unrelated
  agent, or killing sessions.
- **Accepted same-user risk**: the process-tree/token checks are defense in
  depth, not an OS isolation boundary. An agent that escapes both checks
  (`setsid`/double fork and a scrubbed environment) runs as you and can read
  same-user files such as `control-token`, use the shared tmux server, invoke
  provider CLIs directly, access local services available to your account, or
  stop the daemon. This risk is explicitly accepted for the agent-spawner
  feature. A separate Unix user, container, VM, or OS sandbox is an optional,
  separate deployment workstream for untrusted code; none is required to enable
  or use the spawner.
- **Provider restrictions are extra defense in depth**: Crewhall supplies
  Claude Code with `permissions.deny` rules for its own file tools against the
  control token, state files and runtime directory. These are provider-level
  rules, not an OS boundary; arbitrary subprocesses may bypass them. Provider
  permission/sandbox settings can add protection when configured, but the
  spawner does not depend on them and Crewhall does not promise that every
  provider launch is OS-contained.
- **Web UI**: authenticated (token/session) unless bound to localhost and the
  token requirement is off; Host/Origin allow-lists; strict security headers and
  CSP; per-IP login throttling; no inline scripts/styles.
- **Agents**: a new capability is **off by default** (MCP, worktrees). The
  daemon is authoritative for agent identity and Team membership; a message or
  request is never trusted from a `--from`.
- **Agent-controlled creation**: each local shared-workspace Team may assign one
  current member as its spawner. The daemon accepts creation only from that
  agent's own live process tree, authenticated by its agent token and a stable
  peer pidfd. Agent-initiated create/stop fails closed on kernels without
  `SO_PEERPIDFD` support; operator control-plane operations remain available.
  The daemon checks Team membership, current spawner role, selected project
  profile, and limits on every request. Project profiles come from
  `cwh-agents.json`; an operator explicitly approves the catalog fingerprint
  before it can be used. The fingerprint covers the manifest and project-native
  agent definitions, and any change requires approval again. The request cannot replace the profile's
  provider, model, agent, effort, flags, working directory, or backend. Only
  explicitly supported profile fields and safe flags are accepted. A spawner can
  stop only its own children. The `max_live` limit is per spawner and counts
  uncertain process status as live; the rolling `max_created_per_day` limit is
  per spawner and persists reservations before launch, so failed launch attempts
  also consume the daily allowance. These are creation-count limits, not a
  token, compute, or financial budget. `created_by` and the Team's spawner role
  persist across daemon restarts, and the live-child cap is checked before
  restarting an exited child. Remote and worktree Teams cannot use a spawner. When a spawner is
  removed or replaced, Crewhall requires assigning a replacement and blocks the
  change until its children are transferred to the replacement or explicitly
  stopped and removed. The catalog SHA-256 is recorded for each agent-created
  worker for audit. Denied agent create/stop requests are rate-limited to 20
  audit entries per operation per 60-second window; suppressed denials are
  summarized on the next window.
- **Project guide files**: creating a local Team with a workspace creates
  `cwh-agent.md` only when that path does not already exist. Crewhall reads
  `cwh-agents.json` but does not create or overwrite it. The guide asks an agent
  to preserve an existing profile file and propose targeted edits. These files
  live in a shared workspace, so they are visible to every agent with access to
  that directory; they are instructions and configuration, not a secret or a
  permission boundary.
- **Limit of agent-controlled creation**: this is a rule for requests sent through
  crewhall. It is not an OS boundary. Agent code still runs as the same user and
  can run provider CLIs or use the shared tmux server directly, and can read or
  change same-user files, process information, and local settings where normal
  permissions allow, including stopping or restarting the daemon. This control
  narrows Crewhall's own create/stop path; it does not promise to stop arbitrary
  code from launching another process.
- **Team leads**: within a Team, a member may be marked a *lead*. A lead can
  interrogate a member (`agent status`), interrupt its current turn
  (`agent interrupt`), signal its background processes (`agent signal`) and
  force-restart it (`agent restart`). The authority is derived from the caller's
  token and the stored membership, never from a field the caller chooses, and it
  is checked by a single function (`_authorize_lead`). A lead **cannot** act on a
  peer lead of the same Team, on itself, or on an agent outside its Team; the
  human control plane is unaffected. So a compromised member can, at most,
  disturb the agents of a Team it genuinely leads — the same blast radius it
  already has by messaging them — and cannot escalate to the control plane,
  other Teams or its own lead. A restarted agent is told who restarted it and
  why, the action is logged on the agent, audited, and announced on the desktop.
- **Secrets**: access tokens and provider credentials are never written to logs,
  bundles, the audit log, activity, fixtures or error messages; sensitive values
  are shown masked in the UI.
- **Filesystem**: directory suggestions are confined to `security.fs_roots`
  (`realpath`, no `..`/symlink escape); worktree git calls are argv-only and
  confined to the state directory.
- **Web UI uploads**: a file is stored only in the dedicated temp directory or
  the configured `uploads.dir`; the client filename is reduced to a basename
  (no directory components), written `O_EXCL`/`0600` under a unique name, capped
  by `uploads.max_mb` and audited (`web_upload`, mode and size only). The agent
  receives a path, never the bytes.
- **Audit**: privileged operations are appended to `state/audit.jsonl` (0600),
  with no secret values. Successful spawn actions are always logged; denied
  agent-originated spawn requests are rate-limited so they cannot rapidly rotate
  useful audit entries out of the bounded log.

Out of scope: a host that is already compromised (root/another process running
as you), and the upstream agent CLIs themselves.

## Hardening checklist

`crewhall doctor` reports token/settings/audit file modes, local users,
writable provider binaries and permissive Codex/Claude configs. Settings →
Access lets you require the token on localhost and rotate it.

## Terminales web

Una terminal web es **ejecución arbitraria de código como tu usuario**: es una
shell interactiva real. Por eso está **cerrada por defecto** (`terminals.enabled=false`)
y se trata como una capacidad privilegiada.

- **Qué da acceso**: activar `terminals.enabled` (requiere escribir `CONFIRM`) y
  **desbloquear** una sesión del Web UI con un *token de terminal*. El token maestro
  del Web UI y las sesiones normales **no** tienen alcances de terminal: una sesión
  solo obtiene `terminal:read` / `terminal:write` al hacer
  `POST /api/terminal-unlock` con un token de terminal (límite de intentos como el
  login). `terminal:write` implica `terminal:read`. Los tokens están restringidos a
  hosts (`["local","prod1"]` o `["*"]`).
- **Emisión/rotación/revocación**: `crewhall web terminal-token new --scope read|write
  [--host a,b] [--ttl 8h] [--label X]`, `... list`, `... revoke <id>`; o en
  Ajustes → Access & network → «Terminal tokens» (emitir, listar sin secretos,
  revocar). El token se muestra **una sola vez**; solo se guarda `sha256(token)` en
  `web-terminal-tokens.json` (0600, escritura atómica). Emitir/revocar queda auditado
  (solo id y alcance).
- **Handshake**: antes de aceptar el WebSocket se exige, en orden: función activada
  (si no, 404), `Origin` presente y permitido por `SecurityPolicy.origin_allowed`
  (sin `Origin` se rechaza salvo `terminals.allow_no_origin=true`, por defecto no) —
  defensa anti-CSWSH —, sesión válida con cookie y autorización por alcance/host
  (403). El cliente pide antes un **ticket** de un solo uso (`POST /api/terminal-ticket`,
  vida 30 s, ligado a terminal/modo/sesión/`Origin`); la URL del WS lo lleva en
  `?ticket=`. Un ticket reutilizado, caducado, de otra terminal o de otro `Origin` se
  rechaza. Los campos `_*` que envíe el cliente se descartan y el `host` de la
  terminal se obtiene en el servidor con `terminal_info`, nunca del cliente.
- **Ops por el socket UNIX local (CLI)**: son de confianza (mismo usuario unix); la
  autorización por alcances solo se aplica a la Web UI.
- **Solo lectura**: `readonly` se aplica en el servidor (el WebSocket ignora y cuenta
  las tramas de entrada/resize/claim; las ops `terminal_write/key/resize` y las ops
  crudas se rechazan).
- **Límites**: por token (`terminals.max_per_token`), por host y total; timeout de
  inactividad (`terminals.idle_timeout`, cierra la conexión del cliente, no la
  terminal); tamaño máximo de mensaje (`max_message_bytes`); máximo de clientes WS por
  terminal (8) y por sesión (4).
- **SSH**: las terminales remotas usan solo `SshTmuxBackend`/`_ssh_local_argv` con las
  opciones fijas (`ForwardAgent=no`, `StrictHostKeyChecking=yes`, `BatchMode=yes`, sin
  opciones libres). El stream usa un `ControlPath` propio (`%C-stream`).

### Qué NO se audita y por qué

La auditoría de terminales guarda **solo metadatos**: id, host, owner/token, `bytes`,
`enter`, `key`, y un hash corto `sha256:xxxxxxxxxxxx` del texto. **Nunca** el texto de
las órdenes ni la salida, ni el flujo de pulsaciones del WebSocket: pueden contener
contraseñas, `.env` o tokens. Las altas/bajas de conexión y los cambios de modo se
registran como contadores (bytes de entrada/salida, duración), no como contenido.

### CSP de la página de terminal

xterm.js inyecta estilos en línea para su dimensionado dinámico. Para no relajar el CSP
global del Web UI, la vista de terminal se sirve en una **página propia** del mismo
origen (`/terminal.html`, abierta en pestaña nueva, nunca en iframe) cuya respuesta
envía su propio CSP con `style-src 'self' 'unsafe-inline'` (y `frame-ancestors 'none'`).
El resto de la aplicación mantiene `style-src 'self'` sin `unsafe-inline`.

### Recomendación

Usa terminales solo en **loopback o Tailscale**; **nunca** expongas el Web UI a
Internet. Si activas el modo local sin token, ten en cuenta que las terminales exigen
una sesión autenticada: activa `security.local_requires_token` para que el navegador
inicie sesión antes de desbloquear.

### Nota de CSP (0.70.0)

Para mostrar xterm.js dentro del panel principal (y no en una página aparte), el CSP
global relaja `style-src` a `'self' 'unsafe-inline'` (xterm inyecta una hoja de estilos
para su dimensionado). `script-src` sigue siendo `'self'` **sin** `unsafe-inline` ni
`unsafe-eval`, así que no se permite script en línea; el riesgo añadido se limita a
estilos. La página `/terminal.html` mantiene su propio CSP.

### `terminals.master_grants` (0.71.0)

Si se activa (requiere `CONFIRM`), la **sesión maestra** del Web UI recibe
`terminal:read`/`terminal:write` automáticamente, sin un token de terminal aparte. Es
cómodo para un equipo de un solo usuario, pero **debilita** la separación: un token
maestro filtrado pasaría a abrir shells. Por eso sigue **desactivado por defecto** y la
recomendación es usarlo solo en loopback/Tailscale. Los tokens de terminal con alcance
siguen existiendo para acceso limitado (solo lectura o restringido a hosts).

### `terminals.totp_grants` (0.75.0)

Si se activa (requiere `CONFIRM`), un **código TOTP** desbloquea **todas** las
terminales de una vez con `terminal:read`/`terminal:write` sobre `*` (todos los hosts),
sin tokens de terminal ni un alcance por token:

- **Al iniciar sesión** con `/api/totp/login`, la sesión recién emitida recibe el
  desbloqueo completo automáticamente.
- **En el prompt de desbloqueo** (`POST /api/terminal-unlock`), se acepta `{"code":"123456"}`
  como alternativa a `{"token":"..."}`. Un código de 6 dígitos presenta el mismo límite
  de intentos que el login; un código inválido responde 401.

Igual que `master_grants`, **debilita** la separación de capacidades: quien obtenga el
código TOTP (o un secreto de autenticador) pasa a abrir shells en cualquier host. Por eso
está **desactivado por defecto**, la activación es privilegiada y la recomendación es
usarlo solo en loopback/Tailscale. El desbloqueo por TOTP se audita como
`web_terminal_unlock` (summary `totp`). Los tokens de terminal con alcance siguen
disponibles para acceso limitado (solo lectura o restringido a hosts).

Las terminales cuyo shell ha salido se cierran y eliminan solas tras
`terminals.keep_exited_seconds` (def. 300 s), y los tokens caducados se purgan; así no
se acumulan recursos.

## Passkeys / WebAuthn (0.72.0)

Además del token de acceso, se puede iniciar sesión con un **passkey** (huella, PIN o
Face ID) generado por el teléfono o el equipo.

- **Contexto seguro obligatorio**: WebAuthn solo existe en `https://` o `localhost`. Sobre
  HTTP plano (p. ej. `http://<tailscale>:8765`) el navegador **no** ofrece passkeys; usa el
  token, o sirve el Web UI por HTTPS (p. ej. `tailscale serve`). En localhost funciona.
- **Crypto sin dependencias**: CBOR, COSE, ECDSA P-256 (ES256) y RSA PKCS#1 v1.5 (RS256)
  implementados con la librería estándar. No se verifica la *atestación* (el RP pide
  `attestation: "none"`), solo la **aserción** de login contra la clave pública guardada.
- **Anti-replay**: cada aserción verifica el `signCount` (si no aumenta, se rechaza) y la
  **ceremonia** (challenge aleatorio de 32 bytes, de un solo uso, 5 min, ligada a la sesión
  y al `Origin`).
- **Almacén** `web-credentials.json` (0600, escritura atómica): id, credential id, clave
  pública COSE, algoritmo, contador y etiqueta. Nunca se guarda la clave privada (vive en
  el autenticador). Se puede revocar por passkey desde Ajustes.
- **El token de acceso sigue siendo el respaldo** (break-glass): si pierdes el passkey,
  inicias con el token desde la máquina. Nunca dependas solo del passkey.

### Cookie de sesión persistente

El login emite la cookie `at_session` con `Max-Age` = `security.session_ttl_hours` (def.
12 h): sobrevive a cerrar el navegador. La caducidad real la impone la firma del servidor.

## Códigos de autenticador (TOTP, 0.73.0)

Además del token y los passkeys, se puede iniciar sesión con un **código de 6 dígitos**
(RFC 6238) generado por Authy / Samsung Pass / Google Authenticator / 1Password.

- Alta en Ajustes → **Access & network → Authenticator codes**: se muestra un **QR**
  `otpauth://` (generado por nosotros) y el secreto en base32. Alta = escanear + confirmar
  con un código.
- **Funciona por HTTP** (no necesita contexto seguro), a diferencia de los passkeys.
- **Anti-replay**: cada paso de tiempo (30 s) se registra por alta; un código usado no se
  acepta de nuevo en su ventana. Límite de intentos como el login.
- Almacén `web-totp.json` (0600): secreto compartido en claro (necesario para verificar),
  igual que el token maestro. Revocable por alta.
- El **token de acceso sigue siendo el respaldo**.
