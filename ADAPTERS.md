# Estándar para crear un adaptador (Harness)

> **In English (summary):** a provider adapter translates one agent TUI into
> semantic states and a few actions. This is the specification: the mandatory
> contract, optional capabilities, the security rules, the capture recipe, the
> contract kit every adapter must pass, and the verified signals per provider.

Un **adaptador** enseña a crewhall a manejar *una* TUI de agente (Claude Code, OpenCode, Codex…).
Hace una sola cosa: **traducir lo que la TUI muestra a estados semánticos y a las pocas acciones que el
núcleo necesita**. No habla con tmux/PTY (eso es del `backend`), no conoce Teams ni mensajes (eso es del
`controller`) y no abre sockets.

> Prioridad absoluta: **seguridad**. Si una decisión de diseño enfrenta comodidad y seguridad, gana la seguridad.
> Las reglas de la sección 6 son obligatorias y las comprueba el kit de contrato (sección 9).

## 1. Qué obtienes gratis al registrar un adaptador

Settings por provider (activar/desactivar, binario, modelo, args, env, *Test*), selector de New agent, asistente
inicial, mensajería peer-to-peer, actividad en vivo, Mission Control, bundles, reset de emergencia.
**No** hay que tocar la Web UI para un adaptador nuevo.

## 2. Contrato mínimo (obligatorio)

Archivo `crewhall/harness/<kind>.py`, clase que hereda de `Harness` (`harness/base.py`):

| Miembro | Obligación |
|---|---|
| `kind: str` | Identificador estable en minúsculas (`"codex"`). Nunca se renombra: viaja en estado, bundles y archivo. |
| `command() -> list[str]` | argv **sin shell** del binario. Solo el binario; nada de flags peligrosos. |
| `_detect(text) -> (AgentState, evidence)` | Estado a partir de la pantalla capturada, con **evidencia** (frase corta de lo que se vio). |
| registro | `HARNESSES[kind] = Cls` en `harness/__init__.py`. |

### Estados (`AgentState`) y cuándo usar cada uno

| Estado | Regla |
|---|---|
| `STARTING` | Proceso vivo pero la TUI aún no está montada (splash, login, actualización). |
| `READY` | TUI montada, caja de entrada disponible, **ningún turno completado todavía**. |
| `WORKING` | Solo con un **marcador positivo** de trabajo en pantalla (p. ej. `esc to interrupt`). Nunca por "no veo la caja". |
| `WAITING_INPUT` | TUI montada, sin trabajo, y hay evidencia de que un turno terminó. Es el estado "esperando; sin animación". |
| `UNKNOWN` | Se envió un prompt y aún no hay evidencia de nada. Es la respuesta honesta, **no** `READY`. |
| `EXITED` / `ERROR` | Los deriva `Harness.state()` del proceso; el adaptador no los inventa. |

Reglas de oro: la detección es **por evidencia, no por tiempo** (nada de `sleep` como señal); ante duda,
`UNKNOWN`/`STARTING`; un diálogo que bloquea la TUI (confianza de carpeta, login, selector) nunca es `READY`.

## 3. Superficie opcional (cada una desbloquea una capacidad)

| Miembro | Para qué | Si no lo defines |
|---|---|---|
| `launch_args(hooks_settings, conversation_id, port)` | Cablear hooks, id de conversación o puerto al lanzar. | Sin hooks: el estado sale solo de la pantalla. |
| `input_line()` | Texto actual de la caja de entrada (para no concatenar con residuos). | Se asume limpia. |
| `transcript()` | Captura **sin** la caja de entrada propia (la Web UI dibuja la suya). | Captura cruda. |
| `supports_history` + `history(limit, before)` | Pestaña Conversation. Lee de disco/servidor del propio agente. | Sin conversación. |
| `interrupt_keys` | Teclas que detienen un turno (Claude: 1×ESC; OpenCode: 2×ESC). | `("ESC",)`. |
| `clear_input_keys` | Cómo vaciar **solo** el residuo propio (nunca un borrador humano). | `Ctrl+U`, `ESC`. |
| `new_session_commands` | `/clear`, `/new`… (Claude/OpenCode). | Sin "New session". |
| `on_hook(event, at)` | Señales exactas de ciclo de vida empujadas por el agente. | — |
| `activity_snapshot()` · `model_from_screen()` · `usage_from_screen()` | Modelo, herramienta en curso, uso. *(Hoy están como `if kind == …` dispersos; mover aquí es la Fase 2 del plan de trabajo.)* | Actividad = solo estado. |
| `mcp_supported` + `mcp_config_file()` + `mcp_launch_args(path)` | Inyectar el servidor MCP del propio agente sin tocar su config (Fase 4). | Proveedor sin MCP nativo. |

Soporte MCP verificado (0.52.0): **Claude** `--mcp-config <archivo 0600>`; **Codex**
`-c mcp_servers.crewhall.…`; **OpenCode** sin flag verificado → queda sin MCP. La superficie MCP
está desactivada por defecto (`providers.<kind>.mcp`).

## 4. Procedimiento para un adaptador nuevo (checklist)

1. **Investigar la TUI en aislamiento** (sección 5): arranque, trust/login, reposo, escribiendo, trabajando,
   fin de turno, error, diálogo de permiso, actualización disponible, interrupción. Guardar cada pantalla.
2. **Sanear** las capturas (sección 6, regla 5) y guardarlas en `tests/fixtures/screens/<kind>/<estado>.txt`.
3. Escribir `tests/fixtures/screens/<kind>/expected.json` (`{archivo: {state, evidence_contains}}`).
4. Implementar `_detect` (+ lo opcional que la TUI permita). Primero que fallen los fixtures, luego pasar.
5. Registrar en `HARNESSES`. Verificar que aparece en Settings → Providers con su *Test*.
6. Pasar el **kit de contrato** (`tests/adapter_contract.py`, sección 9) y los tests propios.
7. Probar **en real** con un agente de prueba, prompts mínimos y socket tmux aislado (nunca el de producción).
8. Documentar señales y límites conocidos al final de este archivo (sección 8) y en `CHANGELOG.md` (MINOR).
9. Revisar la lista de seguridad (sección 6) marcando cada punto en el PR/commit.

## 5. Receta de captura (aislada y segura)

```bash
S=at_probe_$$                       # socket tmux propio: JAMÁS el de producción (crewhall)
mkdir -p /tmp/ati-probe && cd /tmp/ati-probe
tmux -L $S -f /dev/null new-session -d -s c -x 120 -y 40 -c "$PWD" "<binario>"
sleep 6; tmux -L $S capture-pane -p -t c                 # ver cada pantalla
tmux -L $S send-keys -t c "texto"; sleep 1               # el texto…
tmux -L $S send-keys -t c Enter                          # …y el Enter SEPARADO (ver 6.7)
tmux -L $S kill-server; rm -rf /tmp/ati-probe            # limpiar siempre
```

- Usar un directorio **ya confiable** o aceptar la confianza **a mano** una vez; el adaptador nunca la acepta.
- Prompts mínimos (`reply with the word ok`); cada turno real puede gastar créditos.
- Si la TUI usa pantalla alterna o redibuja, capturar varias veces: los fixtures deben ser estables.

## 6. Reglas de seguridad (obligatorias)

1. **Nunca auto-aceptar** diálogos de confianza de carpeta, login, permisos ni términos. El estado queda
   `STARTING`/`UNKNOWN` hasta que lo resuelva una persona (o la tarjeta de permisos del Web UI).
2. **Nunca inyectar flags peligrosos** (`--dangerously-*`, `--yolo`, bypass de sandbox/aprobaciones,
   `-a never`, modo "full access"). Si el usuario los pone en `default_args`, Settings lo **avisa**; el adaptador no los añade.
3. **No modificar la configuración del usuario** del CLI (`~/.codex/config.toml`, `~/.claude/settings*`…).
   Todo cableado va por flags/archivos temporales 0600 del propio agente, que se borran al cerrar.
4. **Lecturas de disco acotadas**: transcripts/historial solo de rutas derivadas de un id validado
   (regex estricta, sin `..`, sin symlinks fuera del directorio de sesiones), con tope de tamaño y de líneas.
5. **Fixtures y logs sin secretos**: sustituir ids de cuenta/usuario, correos, tokens, rutas bajo `$HOME`
   (`/home/<user>` → `/home/user`), hostnames. Nunca guardar `auth.json` ni capturas con credenciales.
6. **Hooks y señales fallan abiertos**: un error del hook jamás bloquea ni rompe la TUI; el daemon
   valida identidad por token y ignora eventos de agentes ajenos.
7. **Entrada con cuidado**: texto y Enter se envían por separado (muchas TUIs detectan *paste*). Si hace falta
   espera, declarar `submit_delay` (segundos) en el adaptador; no `sleep` ad hoc. Límite de tamaño por mensaje.
8. **Sin shell**: `command()` y los args se pasan como argv; nunca `shell=True` ni interpolación de texto del usuario.
9. **Entorno mínimo**: al agente solo van las variables del provider configuradas + las de identidad
   (`CREWHALL_*`). Los valores sensibles se enmascaran en Settings/logs/archivo.
10. **Degradar con honestidad**: si una señal no es observable, el dato es `n/d`, no una suposición.

## 7. Plantilla

```python
from __future__ import annotations

import re

from .base import AgentState, Harness

_PLACEHOLDER = re.compile(r"^\s*›\s*Ask .* to do anything")      # ejemplo (Codex)
_WORKING = re.compile(r"Working \(\d+s • esc to interrupt\)")


class ExampleHarness(Harness):
    kind = "example"
    interrupt_keys = ("ESC",)

    @classmethod
    def command(cls) -> list[str]:
        return ["example"]

    def input_line(self) -> str:
        for line in reversed(self.session.capture().splitlines()):
            m = re.match(r"^\s*›\s?(.*)$", line)
            if m:
                return "" if _PLACEHOLDER.match(line) else m.group(1)
        return ""

    def _detect(self, text: str) -> tuple[AgentState, str]:
        if "Trust this folder?" in text:
            return AgentState.STARTING, "folder trust dialog (waiting for a person)"
        if _WORKING.search(text):
            return AgentState.WORKING, "'Working … esc to interrupt' on screen"
        if "› " in text:
            return (AgentState.WAITING_INPUT if self._prompt_sent else AgentState.READY), "input box visible"
        return AgentState.STARTING, "TUI not mounted yet"
```

## 8. Estado de los adaptadores

| Kind | Estado | Estado/actividad | Conversación | Hooks / señal exacta | Modelo |
|---|---|---|---|---|---|
| `claude` | implementado | pantalla + hooks | transcript JSONL | `SessionStart`/`UserPromptSubmit`/`Stop`/`PermissionRequest` | transcript / cabecera |
| `opencode` | implementado | pantalla | servidor local HTTP | eventos SSE (`session.status`, `permission.asked`…) | servidor / pie de caja |
| `codex` | implementado | pantalla | `~/.codex/sessions/AAAA/MM/DD/rollout-*.jsonl` (solo lectura) | `task_started` / `task_complete` en el rollout (pantalla de respaldo) | pie: `GPT-5.6-Terra medium · <cwd>` |

### Versiones validadas y de dónde viene cada señal

Cada adaptador prioriza **señales estructuradas** sobre el scraping; la pantalla es
el respaldo. Esta tabla dice qué fuente gana y qué versión se verificó por última vez
(`crewhall doctor` avisa —y el gate **bloquea**— si la CLI instalada queda fuera del
rango, ver `crewhall/harness/versions.py` y la subsección siguiente).

| Kind | Fuente primaria | Respaldo | Versión validada |
|---|---|---|---|
| `claude` | hooks (`SessionStart`/`UserPromptSubmit`/`Stop`/`PermissionRequest`) + transcript JSONL | pantalla | 2.1.284 |
| `opencode` | servidor local HTTP + eventos SSE (`session.execution.*` en v2, `session.status` en v1) | pantalla | 1.18.34 / 2.0.28 |
| `codex` | rollout JSONL (`task_started` / `task_complete`) | pantalla | 0.158.0 |

Las fixtures de `tests/fixtures/screens/<kind>/` son capturas reales por versión; el
kit de contrato (§9) las valida contra `_detect`. `tests/test_adapter_versions.py`
comprueba además que la versión capturada en cada fixture siga dentro del rango
validado.

### Puerta de versión (gate) y pin de binario

Una versión fuera del rango no solo se avisa: **bloquea**. `crewhall/harness/versions.py`
resuelve el comando efectivo de cada provider y lo sondea (`<bin> --version`):

- **Lanzar**: `_launch_command` llama a `_enforce_version`; crear (o reiniciar/restaurar)
  un agente con una CLI fuera del rango lanza `UnsupportedVersionError`.
- **Operar**: el reporte se guarda por agente y `_deliver`/`interrupt`/`signal`/`restart`
  se niegan a tocar un agente con `version_blocked` (así una actualización por debajo
  no deja a crewhall tecleando en una TUI cuyas señales cambiaron).
- **Switch automático**: si el binario primario está fuera del rango, crewhall elige
  solo otra instalación compatible, sin pedir nada al usuario. Candidatos en orden: el
  `providers.<kind>.fallback_command` configurado, y luego las instalaciones que
  descubre en disco (`discover_candidates`: mise/asdf `<root>/*/kind` y
  `<root>/*/bin/kind`, `~/.local/bin`, `~/.opencode/bin`, `~/.local/share/crewhall/bin`;
  `CREWHALL_DISCOVERY_PATH` lo limita). Gana la versión más alta dentro del rango. Se
  desactiva con `agents.auto_select_compatible=false`. Nunca elige un candidato fuera
  de rango, así que uno malo no empeora las cosas; el adaptador validado sigue
  funcionando tras el upgrade y el de la versión anterior queda *legacy*.
- **Aviso (modal)**: el switch (o el bloqueo) se guarda como *notice* y se notifica:
  modal en la Web UI + notificación de escritorio, indicando que crewhall lo detectó y
  aplicó el cambio automáticamente. Ver `crewhall/notices.py` y `web/static/js/notices.js`.
- **Prueba local**: `scripts/opencode_v2_switch_test.py` corre las 3 fases (v1 / v2 con
  v1 disponible / v2 sin nada compatible) en un sandbox, y `--real` reporta el estado
  real de la máquina antes y después del upgrade.
- **Solo aplica al binario del provider**: si `command` apunta a un wrapper u otra
  herramienta (basename ≠ `kind`), el sondeo se reporta pero **no** bloquea; la versión
  de un comando ajeno no dice nada de la TUI que crewhall va a manejar.
- **Override**: `agents.allow_unvalidated_versions=true` (Settings → Agents) degrada el
  bloqueo a aviso. Úsalo solo de forma temporal tras revisar el adaptador.
- `crewhall doctor` informa el estado de cada CLI con el comando efectivo ya resuelto
  (primario o fallback) y avisa del override activo.

### Rollback a OpenCode v1 (volver atrás tras actualizar a v2)

Volver a v1 es solo repuntar el comando del provider: crewhall detecta la versión
del binario en caliente (`_opencode_api` lee `<bin> --version`) y elige el contrato
del servidor (v2 → `serve` + `--server`; v1 → `--port`/`attach`). No hace falta
desinstalar v2 ni tocar el estado: los agentes existentes siguen en su sitio.

1. **Web UI**: Settings → Providers → OpenCode → campo *binario* →
   `~/.opencode/bin/opencode` (la ruta de v1; usa `~`, no la ruta absoluta del home).
2. **API de control** (`settings_set`; cambiar un comando de provider exige
   confirmación):
   `{"providers.opencode.command": "~/.opencode/bin/opencode"}`
3. **A mano**: en `~/.config/crewhall/settings.json`, la clave
   `providers.opencode.command`.

Después, reinicia los agentes afectados (`crewhall agent restart <nombre>`; también
puedes reiniciar el daemon): se relanzan en v1. Si quieres impedir además el
auto-switch a otra instalación compatible, pon `agents.auto_select_compatible=false`;
para fijar un binario concreto sin que el gate lo bloquee, usa
`providers.opencode.fallback_command` o `agents.allow_unvalidated_versions=true`.
Comprobado una vez con el comando de rollback sobre un `settings.json` aislado
(la resolución vuelve a v1 y `_opencode_api("opencode") == "v1"`).

### OpenCode v2 — diferencias con el adaptador v1

OpenCode 2 es un cambio de ruptura profundo; el rango validado de `opencode`
(`>=1.0.0, <2.0.0`) hace que el gate lo **bloquee** hasta migrar el adaptador.
Diferencias a cubrir al portar (verificado contra `https://opencode.ai/v2/openapi.json`):

| crewhall (v1) | OpenCode v2 |
|---|---|
| `GET /session` (lista) | `GET /api/session` — cursor-paginado, responde `{data, cursor}`, filtra por `directory`/`parentID` |
| `GET /session/{id}` | `GET /api/session/{sessionID}` → `{data: Session.Info}` |
| `GET /session/{id}/message` | `GET /api/session/{sessionID}/message` — cursor, *projected messages* (`Session.Message.*`) |
| `POST /session/{id}/abort` | `POST /api/session/{sessionID}/interrupt` |
| `GET /permission` (global) | `GET /api/permission/request` o por sesión `GET /api/session/{sessionID}/permission` |
| `POST /permission/{id}/reply` | `POST /api/session/{sessionID}/permission/{requestID}/reply`, `{reply: once\|always\|reject, message?}` |
| `GET /question` + `reply`/`reject` | *forms*: `GET /api/form`, `POST /api/session/{sessionID}/form/{formID}/reply` |
| `GET /provider` | `GET /api/provider` → `{data: Provider.Info[]}` |
| `GET /event` (SSE) | `GET /api/event` (tipos de evento codificados; por verificar) |
| CLI `opencode --port` + `OPENCODE_SERVER_PASSWORD` | **eliminado**: v2 usa un servicio de fondo compartido (`--standalone` / `--server <url>`) |
| campo de config `server` | ignorado en v2; `provider/agent/command/mcp` → `providers/agents/commands/mcp.servers` |
| id de sesión `^ses_[A-Za-z0-9]+$` | patrón `^ses` (revisar `_SESSION` y `_CONVERSATION_ID`) |

**Estado: implementado (v2 primario, v1 legacy).** Un solo `kind` `opencode`; el
contrato del servidor se detecta en caliente (`OpenCodeLink.detect_api` sondea
`/api/info`) y `VALIDATED["opencode"]` es `>=1.0.0, <3.0.0` con capturas `1.18.34` y
`2.0.28`. Ambos pasan `crewhall watch smoke --require` (6/6) y
`scripts/control_check.py` (5/5).

- **Lanzamiento.** v2 no acepta `--port` en la TUI ni `OPENCODE_SERVER_PASSWORD`
  en `--standalone` (probado: `--standalone` genera una contraseña que no imprime).
  Por eso crewhall arranca un `opencode serve --port N --hostname 127.0.0.1` **por
  agente**, en el cwd del agente (para que su *location* coincida), con la contraseña
  en el entorno (nunca en argv), espera `/api/info` y lanza la TUI con
  `--server http://127.0.0.1:N` (la TUI también recibe la contraseña por entorno, o
  sale al no poder autenticar). v1 sigue con `--port N` dentro de la TUI. El proceso
  `serve` se rastrea por agente y se mata en `remove_agent`, `restart`/`force_restart`
  (se reutiliza) y `shutdown`; no queda huérfano al matar el agente.
- **Reinicio del daemon (A1).** El puerto/contraseña/api se persisten por agente; al
  adoptar la TUI tmux superviviente, crewhall relanza `serve` en el **mismo** puerto
  y la TUI reconecta.
- **Eventos v2** (`{id, type, location, data}`): `session.created` (adoptar),
  `session.execution.started` → `prompt_submit` y `session.execution.
  succeeded|failed|interrupted` → `stop`; `permission.asked|replied|rejected` y
  `form.created|replied|cancelled`. v1 sigue con `session.status`/`session.idle`.
- **Procesos.** En v2 las herramientas corren en el proceso `serve`, no en la TUI:
  el escaneo de procesos y el reconocimiento de descendientes usan el pid del
  `serve` (v1 usa la TUI).
- **Modelos** (`/api/model` en v2) y **permisos** `once|always|reject` (iguales en
  ambos).
- **Pantalla.** La TUI v2 es casi idéntica; la línea de fin de turno usa `ms` en
  turnos rápidos, así que `COMPLETION_RE` acepta `m?s`; fixtures `*-v2.txt` con sus
  tests de `_detect`.
- **Flags v1-only.** La TUI v2 **rechaza** `--model`/`--agent` (y cualquier flag
  desconocido) y sale; crewhall los omite del argv v2 y **los aplica por la API**:
  al arrancar crea la sesión con `POST /api/session` y fija el modelo/agente con
  `POST /api/session/{id}/model` y `/agent` (en restart, sobre la sesión
  reanudada). Si no puede, arranca igual pero avisa (notice + `session_config` en
  `agent_summary`/`agent status`). Cualquier otro flag descartado también se
  reporta; nunca se ignora en silencio.

**Datos reales de v2 (aislado, 2.0.28).** `opencode serve --port N` imprime
`server password <pw>` si no se le da `OPENCODE_SERVER_PASSWORD` (y **respeta** esa
variable de entorno), y usa auth Basic `opencode:<pw>` como v1. `--standalone`
arranca un servidor privado en un puerto **aleatorio**. La pantalla de la TUI es
casi idéntica a v1 (`Ask anything…`, `┃`/`╹▀▀▀`, `ctrl+p commands`, `esc interrupt`,
`Build · <model>`); la diferencia de scraping es que la línea de fin de turno usa
`ms` para turnos rápidos (`Build · Cat Jam Free · 654ms · 40.5 tok/s`) y el pie de
turno muestra `7.4K (3%)` (sin `· $cost` en modelos gratuitos).

### Texto fantasma (placeholder/sugerencia) y borradores humanos

La caja de entrada de cada TUI muestra a veces una **sugerencia** que no es
contenido real: al teclear se reemplaza. No debe confundirse con un borrador
humano, que **nunca** se borra (Fase 0.2). El criterio por adaptador:

- **claude 2.1.x** y **codex 0.158.0**: la sugerencia se dibuja tenue (SGR 2,
  *faint*) tras el prompt (`❯` / `›`); un borrador no. `_input_is_ghost()` usa el
  helper compartido `faint_first_visible()` de `harness/base.py` (y
  `capture(escapes=True)`).
- **opencode 1.18.34**: el placeholder (`Ask anything… "…"`) se dibuja con un gris
  fijo y la parte rotatoria es solo el ejemplo entrecomillado, así que
  `input_line()` lo reconoce por el prefijo `Ask anything` y lo trata como vacío.

Capturas reales usadas para fijar esto: `tmux -L probe capture-pane -p -e` sobre
Codex 0.158.0 y OpenCode 1.18.34.

### Codex CLI 0.158.0 — señales verificadas (captura real, tmux aislado, 120×40)

| Situación | Lo que se ve |
|---|---|
| Carpeta no confiable | `Trust this folder?` + `› 1. Trust and continue` / `2. Back to Agent Command Center`. **No auto-aceptar** (guarda la decisión en `~/.codex/config.toml`). |
| Actualización disponible | Recuadro `✨ Update available! X -> Y` sobre la cabecera. No es estado; ignorar. |
| Reposo (`READY`) | `› Ask Codex to do anything` + pie `<modelo> <esfuerzo> · <cwd>` + `← for agents · ? for shortcuts`. |
| Escribiendo | `› <texto>` en lugar del placeholder. |
| `WORKING` | `• Working (Ns • esc to interrupt)` y spinner braille (`⠼`) en el pie. |
| Fin de turno | `• <respuesta>` + hora (`2:12 AM`); vuelve el placeholder. En el pie aparece un resumen del turno (`· Reply ok`). |
| Herramienta ejecutada | `• Ran <comando>` / `└ (no output)` + hora. |
| Enviar | Texto y Enter **separados**: un Enter pegado al texto se toma como *paste* y no envía. |
| Transcripción | JSONL con `session_meta`, `event_msg/task_started`, `response_item/*`, `event_msg/task_complete`, `token_usage_record`. `session_meta` trae ids de cuenta: **no exponerlos**. |
| Aprobación | *Pendiente de capturar* (la config del usuario era permisiva: `touch` fuera del cwd corrió sin preguntar). Capturar con `codex -a on-request -s read-only`. |
| Interrupción | `esc` según el rótulo `esc to interrupt` (verificar 1× vs 2×). |
| Reanudar | `codex resume <id>`; `codex queue` y `codex app-server` existen (experimentales): posible señal exacta futura. |

> Aviso de seguridad: con la config permisiva del usuario, Codex ejecuta comandos fuera del cwd sin aprobación.
> Settings → Providers debe **avisar** cuando detecte `approval_policy = "never"`/sandbox abierto y recomendarlo en rojo.

### Codex — implementación (0.51.0)

- `harness/codex.py`: `_detect` por evidencia (placeholder, `• Working (Ns • esc to interrupt)`,
  diálogo de confianza → STARTING), `input_line`/`transcript` recortan la caja `›`,
  `interrupt_keys=("ESC",)`, `new_session_commands=("/new",)`, y `config_risks()` lee
  `~/.codex/config.toml` **solo lectura** (avisa de `approval_policy = "never"` / sandbox abierto).
- Historia y señal exacta: `codex_rollout.read()` / `latest_signal()` leen el rollout con `realpath`
  dentro de `~/.codex/sessions`, topes de 8 MB / 4000 líneas, id validado por regex, y **nunca**
  exponen `session_meta` ni ids de cuenta. `turn_context.model` da el modelo y `task_started` /
  `task_complete` la señal de turno (la pantalla es el respaldo).
- **No** se acepta el diálogo de confianza por programa: el agente queda STARTING hasta que una
  persona lo resuelva (regla 1 de §6).
- Prueba real documentada: `AT_RUN_CODEX=1 python -m unittest tests.test_harness_codex` (requiere un
  directorio ya confiado; usa prompts mínimos). El kit de contrato pasa en la suite normal.

## 9. Kit de contrato (a implementar en la Fase 2 del plan)

`tests/adapter_contract.py` define `AdapterContract(unittest.TestCase)`; cada adaptador lo hereda con tres líneas
(`harness_cls`, `kind_dir`) y obtiene gratis:

- cada fixture de `tests/fixtures/screens/<kind>/` produce el estado y la evidencia esperados;
- un diálogo de confianza/login **jamás** da `READY`/`WAITING_INPUT`;
- `WORKING` solo con marcador positivo; pantalla vacía/rota → `STARTING`/`UNKNOWN`, nunca excepción;
- `input_line()` no devuelve el placeholder como contenido;
- `command()` es una lista de strings sin metacaracteres de shell y sin flags de la lista prohibida;
- `launch_args()` no contiene flags peligrosos ni secretos en claro;
- `history()` rechaza ids con `..`, `/` o formato inválido y respeta los topes de tamaño;
- los fixtures no contienen `$HOME` real, correos, tokens ni ids de cuenta (escáner de secretos);
- `kind` es estable (lista blanca en el test) y está registrado en `HARNESSES`.

Un adaptador no se considera terminado hasta que este kit pasa **y** hay una prueba real documentada en la sección 8.
