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

Archivo `agent_terminal/harness/<kind>.py`, clase que hereda de `Harness` (`harness/base.py`):

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
| `clear_input_keys`, `unclearable_input_is_ghost` | Cómo vaciar la entrada sin efectos secundarios. | `Ctrl+U`, `ESC`. |
| `new_session_commands` | `/clear`, `/new`… (Claude/OpenCode). | Sin "New session". |
| `on_hook(event, at)` | Señales exactas de ciclo de vida empujadas por el agente. | — |
| `activity_snapshot()` · `model_from_screen()` · `usage_from_screen()` | Modelo, herramienta en curso, uso. *(Hoy están como `if kind == …` dispersos; mover aquí es la Fase 2 del plan de trabajo.)* | Actividad = solo estado. |
| `mcp_supported` + `mcp_config_file()` + `mcp_launch_args(path)` | Inyectar el servidor MCP del propio agente sin tocar su config (Fase 4). | Proveedor sin MCP nativo. |

Soporte MCP verificado (0.52.0): **Claude** `--mcp-config <archivo 0600>`; **Codex**
`-c mcp_servers.agent_terminal.…`; **OpenCode** sin flag verificado → queda sin MCP. La superficie MCP
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
S=at_probe_$$                       # socket tmux propio: JAMÁS el de producción (agent_terminal)
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
