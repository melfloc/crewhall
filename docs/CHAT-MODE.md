# Modo Chat y artefactos (conversaciones)

> Estado: diseño + implementación por fases. Este documento es la referencia de
> arquitectura de la feature. Cuando algo se implemente, se ajusta aquí.

## Objetivo

Añadir a crewhall una segunda interfaz de primer nivel, **Modo Chat**, al estilo
de Claude web / ChatGPT, que:

- conserva toda la infraestructura actual (agentes, daemon, WebSocket, auth,
  tailscale) y **solo cambia la presentación**;
- muestra un **sidebar con las conversaciones** y, en la pantalla principal, el
  **full view de la conversación** (el viewport del agente del panel
  Conversation, sin el resto del chrome de coding);
- tiene un **panel derecho de artefactos** donde se previsualizan/editan los
  archivos que produce la conversación;
- guarda cada conversación en el **host donde corre el proceso**, con
  `inputs/` (lo que sube el usuario) y `outputs/` (los artefactos), de forma
  segura y privada;
- sirve esos archivos por HTTP **autenticado**, de modo que estén descargables
  aunque se acceda por Tailscale en remoto;
- integra **OnlyOffice** como visor/editor de artefactos y permite que el
  **agente sea un colaborador en vivo** (mismo documento, cursor propio).

## Decisiones fijadas

- Cada conversación se respalda con un **agente crewhall real** (harness:
  Claude Code / OpenCode / Codex). No hay motor de chat paralelo: se reutiliza
  `harness.history()` y el render de `conversation.js`.
- OnlyOffice se despliega como **contenedor por host** (Document Server 8.1,
  JWT). El acceso es configurable (localhost / IP de bridge Docker / Tailscale).
- La colaboración apunta al **Nivel 3**: el agente entra como **co-editor
  headless** con su propio cursor, no como simple snapshot.

## Capas (sin romper la arquitectura)

```
Web UI (modo chat)  ── HTTP/WS ──▶  WebServer (stdlib)
                                       │  ops 1:1
                                       ▼
                                   daemon/Controller  ──▶ agentes (harness)
                                       │
                                       └── conversations (dirs en disco)
Office (visor/co-edición)  ◀── DS (contenedor) ──▶ document.url / callbackUrl
```

`conversations.py` es un módulo de almacenamiento (como `uploads.py`): lo usan
tanto el daemon (ops) como la capa web (subida/servido directo), porque ambos
corren en el mismo host y comparten `state_dir()`.

## Modelo de conversación

```
<conversations.dir>/<id>/
├── inputs/       0700   archivos subidos por el usuario (upload -> ruta)
├── outputs/      0700   artefactos que produce la conversación
└── workspace/    0700   cwd aislado del agente (scratch; nunca es artefacto)
```

La metadata (id, título, agente, host, timestamps) vive en un store central
`state/conversations.json`, no en un `meta.json` por conversación.

**Aislamiento (obligatorio).** Un chat es conversacional: **no** trabaja en un
directorio de proyecto. El `cwd` del agente se **fuerza** a `workspace/` (no lo
elige el usuario, ignora `cwd`/team/worktree), nunca es el cwd del daemon ni el
del usuario, y **no** se escribe `CLAUDE.md`/`AGENTS.md` (los control files se
omiten para chats). Así toda la conversación vive dentro de su propia carpeta y
no deja archivos ni carpetas por otros directorios. El `workspace/` es scratch:
no se lista como artefacto (solo `inputs/` y `outputs/`).

**Aislado de Cowork.** Los agentes de chat **no** aparecen en la interfaz
Cowork: `agent_list` usa por defecto el ámbito `cowork` y excluye a todo agente
ligado a una conversación (`conversations.agent_ids()`); el ámbito `chat` es el
complementario. La interfaz de chat obtiene sus agentes de `conversation_list`/
`conversation_info`, no de la lista de Cowork. Como el workspace es propiedad de
crewhall, el **diálogo de confianza** del agente (Claude) se acepta
automáticamente para chats (`Harness.accept_workspace_trust`), de modo que el
chat arranca sin interacción.

**Reglas de interacción.** El directorio global (`<conversations.dir>/CLAUDE.md`
y `AGENTS.md`) y el `workspace/` de cada conversación llevan un fichero de reglas
que explica al agente las carpetas (`inputs/outputs/workspace`) y cómo usar
`crewhall office open|insert|read|comment|say|save` para editar en vivo, leer
comentarios y responder en el chat del documento.

**El chat revive al agente.** Si el agente está `exited`/`error` (p. ej. tras
reiniciar el daemon), enviar un mensaje lo reinicia antes de escribir
(`chatEnsureAgent`); el reenvío desde OnlyOffice también revive
(`Controller.restart_agent`). Al abrir un artefacto se levanta además el
co-editor del agente en la misma sesión (mismo `document.key`) para que sus
cambios aparezcan en vivo.

- `id`: slug opaco (`c-<hex>`); **no** es el `agent_id` (van desacoplados: una
  conversación puede sobrevivir a un agente recreado).
- El hostname del host se guarda en `meta.json` para que la UI sepa dónde vive.
- Metadatos en un store propio `ConversationStore` bajo
  `state_dir()/conversations.json` (no se mete en `persistence.StateStore`, que
  está deliberadamente acotado a teams/agents/requests/terminals).
- **Confinamiento de rutas**: `resolve(id, section, relpath)` rechaza `..`,
  rutas absolutas y cualquier escape por symlink (`realpath` debe quedar dentro
  de la sección). Solo se sirven archivos regulares; nunca se ejecutan.

## Agente ↔ conversación

- Al crear una conversación con agente, se inyecta en su entorno:
  `CREWHALL_CONVERSATION`, `CREWHALL_INPUTS`, `CREWHALL_OUTPUTS`.
- Flujo de subida: el usuario sube un archivo → `conversations.store_input()`
  lo escribe en `inputs/` → el mensaje al agente incluye la **ruta absoluta**
  (igual que el upload actual). El agente lee con sus herramientas.
- Flujo de artefacto: el agente escribe en `outputs/` (la UI/instrucciones le
  dan la ruta). La lista de artefactos se **calcula del sistema de archivos**
  (no hay metadata que se pueda desincronizar).

## Ops del plano de control (nuevas)

| Op | Efecto | Auditoría |
|---|---|---|
| `conversation_create` | crea dirs + metadata (+ agente opcional) | sí |
| `conversation_list` | lista conversaciones (con nº de artefactos y estado del agente) | — |
| `conversation_info` | metadata + listado de inputs/outputs | — |
| `conversation_rename` | cambia el título | sí |
| `conversation_delete` | borra metadata; `delete_files` opcional | sí |
| `conversation_artifact_delete` | borra un artefacto concreto | sí |

La **subida** y el **servido/descarga** de archivos viven en la capa web
(como `uploads`), no en ops, para no meter bytes por JSON.

## Servido de archivos (web)

- `GET /api/conversation/artifact?id=<id>&path=<rel>&download=1`
  - autenticado por sesión **o** por un **token de descarga temporal** (HMAC),
    necesario porque el Document Server no tiene cookie de sesión.
  - `Content-Disposition: attachment` cuando `download=1`; si no, `inline`.
- `POST /api/conversation/upload?id=<id>` cuerpo crudo, nombre en
  `X-Filename` (igual que `/api/upload`).
- `GET /api/conversation/export?id=<id>` (solo sesión) → un **ZIP** con
  `transcript.md` (el historial del agente), `meta.json` y `inputs/` + `outputs/`.
  Es el botón **Export** de la cabecera del chat.
- Guardas: Host/Origin/auth, `nosniff`, sin traversal, tamaño máximo.

## OnlyOffice (contenedor por host)

URLs distintas (el error histórico del NAS); en crewhall:

```
DocumentServerUrl  = office.public_url + /web-apps/apps/api/documents/api.js  (navegador)
document.url       = office.base_url  + /api/conversation/artifact?...&dt=<token>  (lo baja el DS)
callbackUrl        = office.base_url  + /api/office/callback?id=..&path=..          (guarda el DS)
```

- El **navegador** carga el SDK desde `office.public_url` (origen permitido en
  CSP `script-src`/`frame-src`/`connect-src`).
- El **DS** necesita alcanzar `office.base_url` (la base de crewhall vista desde
  el contenedor). Casos típicos:
  - DS y crewhall en el mismo host con DS en Docker → `http://172.17.0.1:<puerto>`
    (gateway del bridge; ver `ONLYOFFICE_INTERNAL_BASE_URL` de nas-system).
  - DS en otro host / Tailscale → la URL del tailnet de crewhall.
- `document.key = md5(path:mtime:size)` para invalidar caché al guardar.
- Config firmada con JWT HS256 (`office.jwt_secret`).
- Callback: status 2/6 → descargar `body.url` → escribir en la ruta del
  artefacto → 200 `{"error":0}`.
- Composición: `scripts/onlyoffice/docker-compose.yml` + documentación.

## Colaboración Nivel 3 (agente co-editor)

Se levanta un **editor OnlyOffice headless** (Chromium + `DocsAPI`) para el
agente, con su propia identidad de usuario ("AI Agent"), unido a la misma sesión
colaborativa del documento (mismo `document.key`). El bootstrap del editor se
sirve por HTTP local (OnlyOffice no arranca desde `file://`) y **no** lleva
`editorConfig.events` con funciones (rompe la inicialización: el documento nunca
carga). La API de automatización vive **dentro del iframe del editor**
(`Asc.editor`); se ejecuta por **CDP** en el contexto de ese frame:
`read()` = `Asc.editor.ContentToHTML()` y `insert()` = `Asc.editor.Add_Text()`.
(En el DS 8.1.3 el `DocsAPI.DocEditor` del padre **no** expone `createConnector`.)
El agente aplica cambios en vivo:

```
crewhall (agente) ── tool/MCP ──▶ office_collab (bridge)
                                     │ CDP
                                     ▼
                              Chromium headless + DocsAPI (usuario "AI")
                                     │ co-edición DS
                                     ▼
                              usuario humano (editor normal)
```

- Comandos (ops y CLI `crewhall office …`): `open`, `read`, `insert`, `command`,
  `users`, `chat`, `say`, `comments`, `comment`, `close`.
- **Canales desde OnlyOffice hacia el agente** (lo que hace la integración):
  - **Chat de co-edición**: `customization.chat=true`; el usuario escribe al
    co-editor **"AI Agent"** dentro del documento. crewhall registra
    `asc_onCoAuthoringChatReceiveMessage`, y el **watcher del daemon** reenvía
    cada mensaje nuevo como un prompt normal al agente (`[OnlyOffice · user] …`).
    El agente responde con `office_collab_say`.
  - **Comentarios**: el usuario selecciona texto y comenta "@AI …". El watcher
    reenvía los comentarios nuevos al agente con su **texto anclado**
    (`[OnlyOffice comentario · user] sobre "…": …`); además el agente puede
    leerlos con `pluginMethod_GetAllComments` y añadir los suyos con
    `pluginMethod_AddComment`.
  - **Co-editores**: `asc_coAuthoringGetUsers` (quién está editando).
  - **Guardado**: `insert` llama a `Asc.editor.asc_Save()` tras editar, así que
    los cambios del agente llegan al artefacto en disco por el callback **sin**
    esperar a cerrar la sesión.
- Requiere `chromium`/`chromium-browser` en el host; se activa con
  `office.collab_enabled`. Sin navegador, degrada a Nivel 1 (forcesave).
- Nivel 1 (fallback, ya útil): el agente edita el archivo por FS y crewhall
  fuerza recarga del editor abierto (`forcesave` / bump de `key`).

## Dos interfaces de primer nivel: Cowork y Chat

crewhall tiene **dos interfaces separadas** (como chat/codex de ChatGPT),
conmutables desde el selector **Cowork | Chat** de la barra superior (se
recuerda la elección):

- **Cowork** = la interfaz de agentes de siempre: equipos, terminales, live,
  mensajería, mission control, timeline, usage. La topbar muestra sus controles.
- **Chat** = la interfaz conversacional: sidebar de conversaciones, transcript y
  artefactos (OnlyOffice). La topbar oculta los controles de agentes.

Dentro de **Chat** hay un sub-conmutador de disposición **Chat | Document**
(deliberadamente **no** se llama "cowork", que es la interfaz de primer nivel):

- **Chat**: la conversación es el área principal; artefactos en columna compacta.
- **Document**: el **documento (OnlyOffice) es el área principal**; la
  conversación queda como columna estrecha a la izquierda. **Parallel** reparte
  50/50 en disposición Chat.

## Ajustes (settings)

| Clave | Tipo | Defecto | Notas |
|---|---|---|---|
| `conversations.enabled` | bool | true | muestra el modo chat |
| `conversations.dir` | path | `<state>/conversations` | raíz de las conversaciones |
| `conversations.max_mb` | int | 200 | tamaño máximo de un artefacto |
| `office.enabled` | bool | false | activa el visor/editor |
| `office.public_url` | text | `http://127.0.0.1:8081` | base del DS para el navegador |
| `office.base_url` | text | `""` | base de crewhall vista por el DS |
| `office.jwt_enabled` | bool | true | firma del config |
| `office.jwt_secret` | text (sensible) | `""` | igual que en el DS |
| `office.lang` | choice | es | idioma del editor |
| `office.collab_enabled` | bool | false | co-editor headless (Nivel 3) |
| `office.chromium` | text | `""` | binario de Chromium (auto si vacío) |
| `web.public_url` | text | `""` | base pública de crewhall (fallback de `office.base_url`) |

`text` es un tipo nuevo de settings (línea libre, sin interpretarse como path).

## Fases

1. `conversations.py` (almacén + confinamiento) + tests.
2. Ops del daemon + settings + auditoría.
3. Rutas web: subida, servido, descarga.
4. Frontend: modo chat, sidebar, full view, panel de artefactos.
5. OnlyOffice por host: compose, settings, config firmado, callback.
6. Nivel 3: editor headless + bridge de comandos.
7. Tests de integración, README/CHANGELOG.

## Limitaciones conocidas (documentadas, no ocultas)

- El almacén vive en el host del proceso crewhall. Un agente en un host remoto
  (ssh-tmux) no comparte ese disco: los artefactos remotos no se sirven aún.
- La co-edición Nivel 3 necesita Chromium y un DS alcanzable; sin ellos se usa
  el Nivel 1.
- El servido de artefactos es de solo lectura desde la web salvo el editor
  OnlyOffice (que guarda por callback).
- En algunas instalaciones el conversor de `.txt` del DS se atasca y el editor no
  carga ese tipo; reiniciar el contenedor (`docker restart crewhall-onlyoffice`)
  lo recupera. Los formatos Office (`.docx/.xlsx/.pptx`) son el caso de uso y
  funcionan con normalidad.
