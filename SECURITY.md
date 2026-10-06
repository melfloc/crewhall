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

- **Local control plane**: the daemon socket is `0600`; anything that can reach
  it can already run code as you.
- **Web UI**: authenticated (token/session) unless bound to localhost and the
  token requirement is off; Host/Origin allow-lists; strict security headers and
  CSP; per-IP login throttling; no inline scripts/styles.
- **Agents**: a new capability is **off by default** (MCP, worktrees). The
  daemon is authoritative for agent identity and Team membership; a message or
  request is never trusted from a `--from`.
- **Secrets**: access tokens and provider credentials are never written to logs,
  bundles, the audit log, activity, fixtures or error messages; sensitive values
  are shown masked in the UI.
- **Filesystem**: directory suggestions are confined to `security.fs_roots`
  (`realpath`, no `..`/symlink escape); worktree git calls are argv-only and
  confined to the state directory.
- **Audit**: privileged operations are appended to `state/audit.jsonl` (0600),
  with no secret values.

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
