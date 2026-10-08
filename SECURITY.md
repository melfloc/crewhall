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
