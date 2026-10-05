# Arquitectura

> **In English (summary):** this document describes crewhall's layering — a
> backend-agnostic `InteractiveSession` (tmux or PTY), the `Harness` adapter layer
> that gives an agent semantic state and actions, the `Controller`/daemon control
> plane, and the messaging/Teams layers. It explains the act-control merge, the
> contract tests, and how a new provider plugs in without touching the core.

## La pregunta

¿Cómo controlar sesiones interactivas reales (bash, TUIs, agentes CLI) desde un
programa externo de forma que el programa **no dependa** de cómo está implementada
la sesión?

## La decisión

Separar las responsabilidades que suelen ir mezcladas:

```
UI                   (presentación)         ->  VER y CONTROLAR
Controller           (API pública/daemon)   ->  operaciones del sistema
Teams                (agrupación)           ->  QUÉ agentes juntos
Messaging            (comunicación)         ->  QUIÉN habla con QUIÉN
Harness              (semántica)            ->  QUÉ agente   [OpenCode | Claude | ...]
InteractiveSession   (estable, universal)   ->  QUÉ es una sesión
Backend              (intercambiable)       ->  CÓMO se ejecuta
```

`InteractiveSession` es la abstracción porque representa el **concepto**, no el
mecanismo. `tmux` y `PTY` son backends porque son **mecanismos** concretos de
transporte y ciclo de vida. El `Harness` es la capa semántica de un agente concreto
(OpenCode, Claude Code, Codex…) y se apoya **solo** en la API de `InteractiveSession`.
`Messaging` es la capa de comunicación: solo conoce identidad de agente y
`Harness.send()`.

## Reparto de responsabilidades

### Pertenece a `InteractiveSession` (universal)

- Identidad pública: `session_id` (generado por la capa, no por el backend).
- Estado y transiciones: `starting`, `running`, `exited`, `terminated`, `error`.
- Ciclo de vida observable: `created_at`, `last_input_at`, `last_output_at`,
  `exited_at`, `exit_code`.
- Buffer de salida y eventos (`output`, `exited`, `resized`, `state_changed`).
- La API pública (`write`, `send_key`, `capture`, `read_until`, `resize`,
  `interrupt`, `wait`, ...).
- Semántica de "esperando": `wait_for_idle` / `is_idle`.

### Pertenece al backend (mecanismo)

- Arrancar el proceso y obtener un `pid`.
- Escribir bytes y traducir teclas nombradas al transporte.
- Capturar salida (stream en PTY; pantalla+scrollback en tmux).
- Redimensionar, enviar señales, terminar/matar, `poll`.
- Identificadores propios expuestos como metadata: `tmux_session`, `pane_id`, `pid`.

### Frontera

El backend **nunca** genera ni decide el `session_id`, ni gobierna la máquina de
estados, ni conoce la CLI. La sesión **nunca** llama a `tmux` ni a `os.read`.
El backend reporta hechos (`_emit_output`, `_emit_exit`); la sesión decide el estado.

## Por qué esta frontera y no otra

OpenHands ya validó esta separación: su `TerminalTool` tiene una *factory* que
selecciona `TmuxTerminal` o `SubprocessTerminal` (PTY), y el resto del sistema
(incluida la interfaz que ve el agente) no cambia. Nuestro `get_backend(name)`
es el mismo patrón en miniatura. La diferencia es que aquí el objetivo **no** es
darle una shell a un agente, sino exponer la sesión interactiva como recurso
controlable de primera clase.

## Operaciones mínimas (de la investigación)

De OpenHands / Codex (`exec_command` + `write_stdin`) se retiene:

- un identificador estable de sesión (`session_id`);
- separación estricta entre **escribir** y **enviar ENTER**;
- poder consultar salida sin bloquearse (`capture`), y poder esperar un patrón
  (`read_until`) con timeout;
- un código de salida inequívoco.

De tmux se retiene el modelo "el servidor es la base de datos": las sesiones
sobreviven al controlador y son descubribles. De PTY se retiene que no hace falta
ninguna dependencia externa y se controla byte a byte.

De ccmux se retiene la idea de detectar "necesita input" por quietud del panel — aquí
como `wait_for_idle`, sin heurísticas específicas de agente todavía.

Lo que **no** se adopta en esta fase: registry de agentes, tasks, workflows,
retry/recovery, broker de mensajes, multi-host, LLM orchestration. Nada de eso hace
falta para probar la hipótesis central.

## Input, output, teclas y estado

- **Input**: `write()` envía texto literal sin ENTER (evita el bug clásico de
  `send_keys` añadiendo `\n` solo). `send_key()` envía una tecla nombrada; el backend
  la traduce (`PTY`: `ENTER -> \r`, `CTRL_C -> \x03`, `UP -> \x1b[A`; `tmux`:
  `ENTER -> Enter`, `CTRL_C -> C-c`, `UP -> Up`).
- **Output**: PTY usa `select` + `os.read` (event-driven, sin polling agresivo) y
  acumula en un buffer acotado. tmux usa un hilo que sondea `capture-pane` cada
  200 ms y emite deltas. Ambos entregan a `capture()`/`read()`.
- **Estado**: `poll()` consulta al backend (tmux con `remain-on-exit` conserva
  `pane_dead_status`, por eso el código de salida es exacto en ambos backends).
- **"Esperando"**: si no llega salida durante `quiet_ms`, se considera idle. Es una
  heurística, deliberadamente simple, ampliable con adaptadores por agente.

## Daemon: por qué es parte mínima y no sobre-diseño

Una sesión PTY **solo existe mientras un proceso mantiene el master del PTY**. Si la
CLI de un comando suelto creara la sesión y terminara, la sesión moriría. Por tanto
hace falta *algún* proceso dueño. El daemon local (socket Unix, JSON-lines) es ese
proceso: no es un message broker ni un sistema distribuido, es el controlador
residente. Además permite descubrir y adoptar sesiones tmux que sobrevivieron.

### Ciclo de vida y conexión (corrección de regresión)

La Fase 7B introdujo un `DISCONNECTED` permanente. Causa raíz (tres defectos):

1. **Autostart no serializado**: varios clientes podían lanzar varios daemons.
2. **Bind a ciegas + limpieza sin propiedad**: `serve_forever` hacía `unlink`+`bind`
   aunque otro daemon estuviera vivo, y `_cleanup` borraba el socket/pid **siempre**;
   un daemon viejo borraba el socket del daemon vivo → los clientes no conectaban.
3. **`Client(autostart=False)` sin reintento**: la optimización "ensure una vez y
   reutilizar" dejó sin recuperación; el socket cacheado se volvía inválido al morir
   el daemon y la UI quedaba en `DISCONNECTED` para siempre.

Además, `shutdown` **no terminaba** el daemon: `accept()` bloqueado no se despierta al
cerrar el socket desde otro hilo → daemons zombie acumulados.

Correcciones (todas en la capa conexión/daemon, nunca en el renderer):

- **Instancia única**: el daemon toma un `flock` exclusivo (`daemon.lock`) durante toda
  su vida; si ya hay otro, sale. `ensure_daemon` serializa el arranque con un
  `flock` corto (`spawn.lock`), de modo que N clientes concurrentes → **un** daemon.
- **Detección de socket stale**: antes de borrar/enlazar, se comprueba si hay un
  listener real (`connect()`); si no, se elimina el socket stale y se enlaza. La
  limpieza solo borra el socket/pid si este proceso **lo creó** (`_owns_socket`).
- **Reconexión en `Client.call`**: si la conexión falla (socket ausente/refused/
  cerrado), se hace `ensure_daemon` y **un** reintento, dentro de la capa de conexión.
  La UI no cambió: sigue mostrando `DISCONNECTED` cuando el daemon es realmente
  inalcanzable, pero un daemon caído se recupera de forma transparente.
- **Apagado fiable**: el bucle de `accept()` usa timeout (0.5 s) y respeta el evento de
  parada, así `shutdown`/SIGTERM termina el proceso y limpia su socket/pid.

Tests de regresión en `tests/test_daemon_lifecycle.py` (daemon real, runtime temporal):
fresh start, reuso, socket stale, reconexión tras `SIGKILL`, arranque concurrente.

## Harness: la capa semántica de agente

`InteractiveSession` no debe saber qué es OpenCode. El `Harness` sí: conoce el
comando del agente y cómo interpretar su TUI. La frontera es estricta:

- El Harness **solo** usa la API pública de `InteractiveSession`
  (`write`, `send_enter`, `capture`, `poll`, `status`, `exit_code`, `close`…).
- El Harness **no** abre PTYs, **no** llama a tmux y **no** lanza subprocesos.
- `InteractiveSession` **no** importa ni conoce ningún Harness.

Así, un `ClaudeCodeHarness` o un `CodexHarness` se añaden sin tocar
`InteractiveSession` ni los backends. Eso ya no es una hipótesis: hay **dos**
adapters reales (`OpenCodeHarness`, `ClaudeCodeHarness`) que usan el mismo contrato.

### Qué es universal y qué es del agente

| Concepto | Capa | Nota |
|---|---|---|
| `write` / `send_enter` | InteractiveSession | la separación es deliberada y no se rompe |
| `capture` / `read_until` | InteractiveSession | salida cruda/renderizada |
| `status`, `exit_code` | InteractiveSession | estado del **proceso** |
| `send(prompt)` | Harness | escribir + ENTER, con validación de disponibilidad |
| `state()`, `is_waiting()` | Harness | estado **semántico** del agente |
| `STARTING/READY/WORKING/…` | Harness | derivado de señales observables |
| marcadores de TUI | Adapter concreto | p. ej. `esc interrupt` de OpenCode |

### Estado basado en evidencia (OpenCode)

Estados y señales observadas empíricamente (opencode 1.18.31, 120×40):

- `STARTING`: la TUI no está montada (falta el footer `ctrl+p commands`).
- `READY`: montada y visible el placeholder `Ask anything…` (antes del 1er prompt).
- `WORKING`: la línea de estado casa `esc\s+interrupt`.
- `WAITING_INPUT`: montada, sin `esc interrupt`, y con evidencia de que un ciclo de
  trabajo terminó: se observó `WORKING`, o apareció el footer de tokens/coste
  (`12.6K (1%) · $0.00`) que no estaba en el momento de enviar.
- `EXITED`/`ERROR`: del proceso de la sesión (`status`, `exit_code`).
- `UNKNOWN`: no hay evidencia suficiente. **Se prefiere UNKNOWN a mentir.**

Importante: **terminal idle ≠ agente terminó ≠ esperando input.**
`wait_for_idle()` mide quietud de salida del terminal; no es un detector universal
de "el agente acabó". Por eso el Harness no basa su estado solo en la quietud.
Se implementó una guarda contra el falso positivo de "turno anterior": el footer de
coste solo cuenta como finalización si **no** estaba presente al enviar el prompt,
o si además se observó `WORKING`.

### Segundo adapter: Claude Code (validación de la abstracción)

Señales observadas empíricamente (Claude Code 2.1.284, 120×42):

- `STARTING`: faltan el header `Claude Code v…` o el footer `shift+tab to cycle`.
- `READY`: montada, sin prompt enviado, input `❯` disponible.
- `WORKING`: el footer contiene `esc to interrupt`.
- `WAITING_INPUT`: montada, sin `esc to interrupt`, y ciclo terminado (se observó
  `WORKING`, o apareció la línea `✻ … · done 4:04 PM` que no estaba al enviar).
- `EXITED`/`ERROR`/`UNKNOWN`: igual que en el contrato base.

Diferencias inevitables entre agentes (aisladas en cada adapter, **no** en el
contrato):

| Aspecto | OpenCode | Claude Code |
|---|---|---|
| Marcador WORKING | `esc interrupt` | `esc to interrupt` |
| Marcador de turno terminado | footer de tokens/coste `(N%) · $` | `✻ … · done <hora>` |
| Gate de arranque | ninguno | diálogo de confianza de workspace (no se auto-acepta) |
| Readiness de entrada | suficiente al montar | requiere "asentado" (pantalla estable) y verificación de envío |

**Resultado: el contrato `Harness`/`AgentState`/`AgentInfo` no necesitó cambios.**
Lo único específico por agente es `_detect()` y, como mucho, un override de `send`
(para snapshot de finalización y asentado). El enum de estados, `send`/`capture`/
`state`/`wait_for_state`/`is_waiting`/`stop`/`info` y la capa `InteractiveSession`
se reutilizaron tal cual. Un `CodexHarness` seguiría el mismo patrón.

### Por qué un registro de agentes no es sobre-diseño

La lista de agentes (`Controller.agents`, `AgentInfo`) es el mínimo para que el
controller y la futura UI puedan mostrar "qué agentes existen, estado y actividad".
No es un Task Registry ni un workflow engine: no hay tareas, ni reintentos, ni
orquestación.

## Messaging: por qué no hay broker ni Inbox

El problema de la Fase 4 es *comunicación*, no *orquestación*. Conviene separar:

```
Messaging      = direccionamiento + identidad + entrega + trazabilidad
Orchestration  = decidir qué agente debe hacer qué
```

Solo se implementa lo primero. Un broker (Redis/RabbitMQ/socket propio con colas)
no resuelve nada que hoy no esté resuelto: los agentes viven en el mismo proceso
controlador y ya hay una vía directa (`Harness.send`). Añadir broker sería
infraestructura sin problema que la justifique.

### Inbox: evaluado y descartado en esta fase

| Criterio | `Messaging → Harness.send()` (elegido) | `Agent → Inbox → Harness` |
|---|---|---|
| Simplicidad | mínima | estado de cola + semántica de pull |
| Trazabilidad | `Delivery` + eventos de sesión | igual, más estado que mantener |
| Concurrencia | lock por receptor | colas por receptor |
| Futuro Teams | el `Message`/`Delivery` es reutilizable | un Inbox podrá envolver el mismo modelo |
| Necesidad real hoy | sí (A→B inequívoco) | no |

Decisión: **sin Inbox**. No cierra la puerta: un Inbox futuro envolvería el mismo
`Message`/`Delivery` sin tocar `Harness` ni `InteractiveSession`.

### Semántica de delivery

`delivered=True` significa exactamente: *el `Harness.send()` del receptor aceptó el
body y lo inyectó en su input* (`InteractiveSession.write()` + `send_enter()`). No
significa que el agente terminó de procesarlo ni que el modelo respondió. El envío
es síncrono; si el receptor no está utilizable dentro del timeout, `delivered=False`
con `error` (nunca una promesa más fuerte que la realidad).

### Identidad y trazabilidad

`Message.sender`/`recipient` son `agent_id` del `AgentRegistry` (sin tmux/pid). El
`Messaging` resuelve por nombre/id/prefijo contra el **mismo** registry que usa el
`Controller`; no introduce una segunda identidad. Para trazabilidad reutiliza el
log de eventos de cada `InteractiveSession` (`message_sent`, `message_delivered`,
`message_failed`) — no hay event bus paralelo.

## Teams: agrupación, no orquestación

La pregunta de las Fases 5–6 era: ¿qué estructura mínima agrupa agentes de forma útil,
estable y **dinámica**? La evidencia del código da una respuesta pequeña:

```
Team = team_id + name + {agent_id, …} + created_at
```

Un **conjunto nominal dinámico de agentes**. Ni las pruebas con tres agentes reales
ni la membership dinámica exigieron ninguna primitiva adicional.

### Qué NO es un Team

No es líder, rol, supervisor, coordinator, planner, workflow, scheduler, task
manager, canal, cola, ni status. No decide qué hace cada agente. Todo eso es
**orquestación** y queda deliberadamente fuera: no emerge de `Harness`/`Messaging`
y no hay necesidad demostrada. Introducirlo ahora sería arquitectura imaginada.

### Membership: por qué referencias y no pertenencia única

- El Team guarda `agent_id`s; **no** guarda `Harness`, sesión, `pane_id` ni `pid`.
- La pertenencia vive en el **Team**, no en el agente (sin back-reference). Por eso
  el `AgentRegistry` no se acopla a Teams y **un agente puede pertenecer a varios
  Teams**. Forzar pertenencia única requeriría que el Registry mantuviera el mapeo
  inverso y rompería la independencia de capas, sin aportar nada al modelo actual.
- Validación en `create` y en `add_member`: existencia de cada agente (resuelto a
  `agent_id` canónico), sin duplicados, y operación **atómica** (si falla algo, el
  Team no cambia). `remove_member` rechaza a un no-miembro de forma clara.

### Membership dinámica y Team vacío

`add_member`/`remove_member` son la única forma de cambiar la membership. Ambas se
ejecutan bajo el lock del `TeamRegistry`, así que `add_member`/`remove_member`
concurrentes no corrompen el conjunto. `remove_member` **no** detiene ni elimina al
agente: la existencia del agente y la membership del Team son conceptos distintos.

Se decidió **permitir Teams vacíos** (quitar el último miembro deja `members=[]`; y
se puede crear vacío). Antes se prohibía porque no había forma de poblarlo; con
membership dinámica, "vacío" es un estado transitorio legítimo y prohibirlo añadiría
un caso especial de ciclo de vida.

Los `agent_id` no se reutilizan (un agente nuevo obtiene un id nuevo), así que un
miembro `missing` no se puede "re-añadir" por su id; se añade el id del agente nuevo.
Un miembro `missing` sí se puede quitar pasando su `agent_id` literal.

### Team ↔ AgentRegistry ↔ Messaging

`Team` referencia identidades; `TeamRegistry.members()` resuelve `AgentInfo` **en
vivo** contra el `AgentRegistry` (sin snapshots). El envío sigue siendo
`Messaging.send(agent, agent)`; el Team no altera `Harness` ni participa en la
entrega.

### Eliminación de agentes: una sola regla

`remove_agent(A)` **no** toca los Teams. El Team conserva la referencia; `members()`
omite a los agentes que ya no existen y `missing` los lista. Se descartaron
"borrar el miembro" (acopla Registry→Teams) y "prohibir borrar" (lifecycle complejo):
la resolución en vivo es honesta y sin acoplamiento.

### ¿Team Message?

No. Un `Message` es `sender`/`recipient` entre agentes; no hay campo Team. Un
"historial de un Team" sería derivable filtrando por membership, pero eso obliga a
definir si un mensaje "es del Team" cuando uno solo de sus extremos es miembro, o
solo cuando ambos lo son. Esa decisión no se necesita todavía, así que **no existe
Team Message** ni `team.send()`/`broadcast()` en esta fase.

### Coste en capas inferiores

Cero en el contrato: Teams se construyó **encima** de `AgentRegistry`, `Messaging`,
`Harness` e `InteractiveSession` sin cambiar sus interfaces. El único ajuste fue
interno a los adapters: al validar mensajes con varios turnos sin sondeo continuo,
la detección de "turno terminado" pasó de mirar la *presencia* de un marcador a
*contar* el artefacto por turno (`· <Ns>` en OpenCode, `· done <hora>` en Claude).
Esto es exactamente el tipo de detalle que pertenece al adapter y no al contrato.

## UI: cliente del daemon, sin saltar capas

### Decisión tecnológica

`curses` de la **stdlib**. Razones: cero dependencias nuevas (el proyecto se sostiene
en stdlib), funciona local en Linux, arranca y itera rápido. Se descartó una app web
(no hay razón concreta y añadiría servidor/JS) y una librería TUI externa
(Textual/urwidtrees) porque `curses` cubre el caso y no justifica la dependencia.

### Estructura interna (Fase 7B)

```
ui/control.py   ControlPort + DaemonControl + LocalControl   -> operaciones
ui/model.py     AppModel (estado, modales, navegación)       -> lógica headless
ui/view.py      render(model,w,h) -> [(texto, estilo)]        -> presentación pura
ui/app.py       bucle curses (teclas, color, resize)          -> I/O terminal
```

El `view` no muta estado ni crea agentes; el `model` no importa `curses` ni pinta. Los
modales (`Modal`/`ModalField`: text/select/checklist/confirm) son una infraestructura
única reutilizada por Create Agent/Team, Add/Remove Members, Send Message, Confirm,
Activity, Agent Info, Help y la Command Palette.

### Modelo de navegación (Fase UX)

La UI es un **workspace**, no una lista pegada a un sidebar:

- **Navigator** = `WORKSPACE` con `TEAMS` (contenedores plegables + miembros) y
  `UNGROUPED`; si no hay Teams, directamente `AGENTS` (sin sección vacía).
- **Session workspace** (derecha) = header + transcript + **composer**.
- **Enter contextual**: en Team expande/colapsa; en Agent mueve el foco al composer.
- **Foco** `nav` ↔ `composer` con `Tab`/`Esc`; el composer es un área de entrada real.
- **Footer contextual**: las acciones mostradas dependen de selección/foco/modal.
- **Palette** (`Ctrl+P`) es descubrimiento secundario y filtra por contexto; las
  operaciones básicas tienen tecla directa.
- **Crear = distinto de administrar**: `t` crea un Team (solo nombre, vacío válido);
  `a`/`x` administran membresía con selector múltiple.
- **Responsive**: ancho de sidebar dinámico (clamp 22–38, panel derecho ≥40); a
  `80x24` funciona degradado; por debajo, "terminal too small".

El render devuelve líneas de **segmentos** `(texto, estilo)` para poder estilar el
navigator y el session workspace de forma independiente en la misma fila. Sigue sin
dependencias externas (`curses`) y sin tocar dominio/daemon/IPC.

### Interactive Focus (passthrough, sin doble-input)

En fases previas la UI dibujaba su propio composer además del TUI real del agente: dos
inputs. Ahora **no hay composer**. Con un Agent seleccionado, `Enter` entra en
**Interactive Focus** y el teclado se reenvía al TUI real del agente:

- Texto → `InteractiveSession.write` (sin ENTER añadido).
- Teclas (`Enter`, `Backspace`, `Delete`, flechas, `Home/End`, `Tab`, `PgUp/PgDn`,
  `Esc`, `Ctrl+C`, `Ctrl+D`, `Ctrl+P`, …) → `InteractiveSession.send_key`.
- `Esc` se reenvía (es nativo: interrumpe/cancela); la salida es **`^B`**.

Para no saltar capas, se añadió `Harness.write_raw`/`send_key` (delegan en la sesión) y
las ops de daemon `agent_write`/`agent_key`; la UI sigue usando solo `ControlPort`.
En Interactive Focus la UI **no** interpreta teclas administrativas (esas viven en el
Navigator) y **no** hay buffer de edición paralelo: la edición es la del agente.

### Viewport real y resize

El agente debe creer que su terminal tiene el tamaño correcto. `view.session_viewport`
calcula el tamaño real del panel de sesión (ancho total − sidebar − divisor; alto del
body − header de sesión − separador; el header/estado/footer **no** cuentan). El bucle
de la app, en cada frame y ante `KEY_RESIZE`, llama a `model.sync_viewport(cols, rows)`,
que envía `InteractiveSession.resize` (vía `ControlPort.resize` → daemon `agent_resize`
→ `Harness.resize`), deduplicando por `(agent_id, cols, rows)` para no spamear. Así el
prompt nativo queda abajo y el TUI ocupa todo el área, tanto en PTY (`TIOCSWINSZ`) como
en tmux (`resize-window`). No hay tamaño fijo ni dependencia del tamaño de arranque.

### Recuperación de EXITED/ERROR y Ctrl+C

`model._check_interactive_focus()` corre en cada `refresh()`: si el agente enfocado está
en `EXITED`/`ERROR`, se abandona Interactive Focus, se conserva la selección y el footer
pasa a acciones de Navigator (`d Delete`). El agente **no** se elimina automáticamente.
`_forward` re-guarda contra estados terminales (no se escribe a un proceso muerto).
No hay polling extra: usa el refresh existente.

Ctrl+C se captura como **tecla** (no señal) con `curses.raw()`: en Navigator el modelo
lo trata como salida; en Interactive Focus se reenvía al agente. Además `run()` captura
`KeyboardInterrupt` (SIGINT programático) solo para salir limpio; `curses.wrapper`
restaura el terminal en ambos casos. No se ocultan otras excepciones.

### Crear Agent desde un Team

`n` sobre un Team abre el formulario con `target=team`; al crear, el Agent se registra,
se **añade al Team** y queda **seleccionado en Interactive Focus**. Si un Team no tiene
agentes disponibles, `a` → `Enter` ofrece crear uno (`add_members` vacío → `create_agent`
con el Team como target). Así el flujo crear-Agent / asociar-a-Team / entrar a la sesión
no obliga a salir del contexto.

Los **tipos de agente y backends** se descubren en runtime vía `ControlPort.meta()`
(`available_harnesses()` + `backends.available()`), nunca se hardcodean en el renderer:
si aparece `codex`, la lista del formulario lo incluye sin tocar la UI.

### Frontera: `ControlPort`

La UI no conoce `tmux`, `PTY`, `pane_id`, `pid`, `Harness`, `InteractiveSession` ni
marcadores de TUI. Solo usa `ControlPort` (`ui/control.py`):

```
UI (AppModel + curses)
      │  ControlPort
      ├── DaemonControl ── Client ──socket──▶ daemon == Controller
      └── LocalControl  ──▶ Controller in-process (tests / --local)
```

`DaemonControl` es el uso real: el daemon ya **es** el `Controller` residente, así que
la UI comparte estado con el CLI y los agentes sobreviven a la UI. `LocalControl`
existe para tests/demos sin daemon. La UI no crea una segunda ruta hacia los agentes.

### Reglas arquitectónicas respetadas

- `UI → tmux` sería un fallo: no ocurre.
- `UI → OpenCode internals` sería un fallo: no ocurre.
- `UI → InteractiveSession` directo sería un fallo: no ocurre; pasa por `Controller`.
- La UI **no** detecta estados ni interpreta TUIs: muestra `AgentInfo`/`TeamInfo`/
  `Delivery` ya resueltos.

### Actualización de estado

Polling (~0.8 s) de `list_agents`/`list_teams`/`message_history` + `capture` del
seleccionado. Sin websockets ni brokers. Las operaciones que bloquean (`send`,
`message`, `create_agent`) corren en un hilo para no congelar la UI. El meta
(harnesses/backends) se consulta una vez y al reconectar.

`capture` sigue siendo un **snapshot completo**; la UI hace follow/scroll sobre el
texto ya adquirido. Optimizar con capture incremental sería una mejora de capa
inferior: se documenta como necesidad, no se implementó de forma especulativa.

### View-model, no dominio

La selección, el cursor, el foco, el follow/scroll, los modales y el layout viven en
`ui/model.py`/`ui/view.py` como **estado de presentación**. No se añadió nada al
dominio para la UI: todo se compone de operaciones públicas existentes. El único
añadido es plomería de introspección (`meta_info` en el daemon), que expone lo que ya
existía (`available_harnesses()`/`available()`), no una primitiva de dominio.

### Necesidades detectadas (no implementadas)

Del uso real de la UI emergieron huecos reales, **no** inventados como arquitectura:

1. **"Detener un agente sin eliminarlo"**: `Controller` solo expone `remove_agent`
   (stop + unregister). La UI lo rotula "eliminar (lo detiene)".
2. **Harness de comando arbitrario ("Custom command")**: `create_agent` exige un
   `kind` de harness registrado; no hay harness genérico para un CLI cualquiera.
3. **Capture incremental**: hoy es snapshot completo.

Se documentan como candidatos para una fase futura; no se implementaron aquí.

## Colaboración Team-bound (agent ↔ agent)

crewhall es el **control plane**: los agentes son participantes, Messaging el
canal, y la TUI solo visualiza/administra. La comunicación no depende de la TUI.

### Identidad y discovery

`Controller.create_agent` inyecta en el entorno del proceso
`CREWHALL_AGENT_ID/NAME/TEAMS/TOKEN/SOCKET`. El **token** es la credencial del
agente: `send_message_as(sender, token, ...)` y `agent_identity(target, token)` lo
validan, de modo que el sender se deriva de la identidad registrada y no de un
argumento falsificable. El discovery es **bajo demanda** (`agent identity`,
`agent list --team`); no hay polling/heartbeat/watcher.

### Autorización (autoridad en el control plane)

`Controller._authorize_team` exige `teams(sender) ∩ teams(recipient) ≠ ∅`. Si no hay
Team compartido lanza `MessagingError` **antes** de entregar. Un agente sin Team no
participa. Multi-Team: se permite si la intersección no es vacía. La TUI no es la
autoridad: la mensajería manual de operador reutiliza `Messaging.send` sin esa
restricción (acción explícita del usuario).

### Activación on-demand y reactivación de EXITED

`Messaging` recibe un hook `revive` (provisto por `Controller`). Bajo el lock del
destinatario: si su proceso no está vivo, se llama a `revive` → `Controller.restart_agent`
recrea **la misma entidad lógica** (`agent_id`/`name`/Teams/cwd/harness/backend
preservados; se reutiliza el `session_id`), espera READY (`Harness.start`) y luego
inyecta el mensaje con `Harness.send`. Sin monitor: el mensaje es el activador. Si el
reinicio falla, el `Delivery` queda `failed` con el error. El lock por destinatario
garantiza *at most one activation* y evita input intercalado.

### Control files

`control_files.ensure_managed_section(cwd)` añade/actualiza una sección gestionada en
`CLAUDE.md` (o lo crea si no existe). Nunca reescribe el contenido del usuario: opera
por splicing entre marcadores `BEGIN/END`, es idempotente y **solo** dentro del `cwd`.
Se invoca al crear y al reiniciar agentes.

## Agent-terminal como entorno de comunicación

El agente actúa como cliente del control plane desde su propia TUI (shell normal); no
hay canal directo agente→agente y la TUI no es requisito. El sender se deriva del token
de entorno; la Team authorization y la activación on-demand viven en
`Controller`/`Messaging`. La salida de Interactive Focus usa **Ctrl+Alt+B** para no
capturar Ctrl+B (prefijo de tmux); todo lo demás (`Esc`, `Ctrl+C/D`, flechas, Enter) se
reenvía al agente.

### Robustez de entorno (TMPDIR / runtime)

En máquinas donde el tmpfs por defecto está agotado por cuota (`df` engaña; escribir un
byte da `EDQUOT`), las CLIs de agente (Bun/OpenCode) fallan al desempaquetar su librería
nativa. Además, OpenCode/Bun **solo** honran `TMPDIR` del entorno del proceso padre, no
de un `Popen(env=...)` posterior. Por eso:

- `paths.usable_tmpdir()`/`runtime_dir()` detectan un tmpfs inutilizable y caen a un
  directorio bajo el home cache.
- `ensure_daemon` propaga el `TMPDIR` utilizable al **daemon** (padre de los agentes).
- `TmuxBackend.start` prefija el comando con `env VAR=val` para que el env del spec
  llegue al proceso dentro del pane, con independencia del entorno del servidor tmux.

No cambia el dominio: es entorno de ejecución, no primitivas nuevas.

## Cooperación real (Fase 9)

crewhall es un **entorno de comunicación**, no un orquestador. En la práctica, un
agente `lead` recibe una tarea de alto nivel y **él mismo** decide delegar: descubre su
Team y a sus compañeros por el CLI, redacta las instrucciones y usa
`crewhall message send`. Los compañeros, aunque estén `EXITED`, se activan
on-demand (mismo `agent_id`), reciben el mensaje como input normal, trabajan y responden
por el mismo canal. crewhall no interpreta la tarea, no asigna roles, no mantiene
workflow ni estado de tareas: solo identidad, Team, autorización, transporte y
activación.

### TMPDIR / tmpfs

En máquinas donde el tmpfs por defecto (`/tmp`) está agotado por cuota de usuario
compartida, el sandbox de bash de las CLIs falla (`EDQUOT`). Como crewhall no debe
tocar procesos ajenos, `paths.usable_tmpdir()` prefiere un tmpfs dedicado
(`/dev/shm/crewhall-tmp`) y, si no, el home cache; ese valor se propaga al daemon y
a los agentes (vía `env` en tmux), de modo que el bash de los agentes funciona sin
depender de `/tmp`.

## Input limpio y navegación de vista (Fase 9.1)

Un agente recién (re)arrancado puede tener residuo en su línea de entrada (texto sin
enviar, un `/model …` pendiente). `Harness.ensure_input_clean()` inspecciona la línea
de entrada real —cada adapter implementa `input_line()` con sus marcadores (`❯` en
Claude, rail `┃` en OpenCode)— y espera a que esté vacía antes de inyectar; si el
agente está idle y hay residuo, lo limpia con `Ctrl+U`/`Esc`. Es evidencia, no un
sleep. Evita que un mensaje se concatene con input previo (`/model sonnetHola…`).

En Interactive Focus, `Ctrl+Alt+K`/`Ctrl+Alt+J` cambian **qué agente se visualiza**
(antes/siguiente, con wrap-around) sin abandonar el foco ni tocar a ningún agente;
`Ctrl+Alt+B` vuelve al Navigator. `Ctrl+B` y el resto de teclas se **reenvían** al
agente. La navegación solo actualiza selección, transcript, viewport (resize) y follow.

## Motor independiente de la interfaz (Fase 10)

El daemon es el **control plane**; TUI/CLI/Web UI son clientes. Cerrar una interfaz no
detiene el motor ni los agentes. El socket se resuelve de forma determinista
(`paths.socket_path()`, independiente del cwd), de modo que `crewhall …` funciona
desde cualquier directorio y los agentes/CLI comparten el mismo control plane
(`CREWHALL_SOCKET`).

### Team workspace y precedencia de cwd

`Team.workspace` es un directorio absoluto existente (validado). `Controller.resolve_cwd`
aplica `cwd explícito > workspace del Team > cwd por defecto`. El control file se
escribe en el cwd resuelto del agente.

### Persistencia lógica

`persistence.StateStore` guarda sólo datos no sensibles (Teams, workspace, definiciones
de agentes: id/name/kind/backend/cwd/cols/rows/env de usuario). En el arranque,
`Controller.restore()` recrea los agentes como entidades **EXITED** (mismo `agent_id`);
no arranca procesos. Esto separa *persistencia lógica* de *persistencia de procesos*:
tras un reinicio del daemon o de la máquina, el control plane está disponible y los
agentes se reactivan on-demand. No hay auto-restart de agentes.

### Lifecycles separados

| Evento | Efecto |
|---|---|
| UI/CLI/SSH disconnect | ninguno sobre el daemon ni los agentes |
| daemon restart | agentes quedan lógicos (EXITED); mismo `agent_id` |
| agent exit | entidad lógica persiste; reactivable |
| machine reboot | estado lógico persiste; procesos no; sin autorestart |
| systemd start/stop | arranca/para el control plane, no los agentes |

## Web UI como cliente (Fase 11)

La Web UI es **otra interfaz** del mismo motor. El servidor (`crewhall/web/`,
stdlib) expone una lista blanca de operaciones que traduce a `Client`/control plane;
nunca habla con tmux/PTY/Harness. El estado se empuja por WebSocket mediante una
**adaptación server-side** que sondea el daemon a baja frecuencia y difunde sólo
cambios (el navegador no hace polling). Host local por defecto; la separación
`transport / auth / operations` deja sitio a autenticación futura sin reescribir.

## Acceso remoto (Fase 12): Tailscale = transporte, crewhall = seguridad

```
Browser ─▶ Tailscale (red privada) ─▶ WebServer ─▶ Client/ControlPort ─▶ daemon ─▶ agentes
```

Tailscale no sustituye la autenticación. La frontera de seguridad vive en el
`WebServer` (capa de aplicación), no en la red:

- **Auth**: token de acceso en fichero `0600` (fuera de StateStore/logs/AgentInfo).
  Navegador → cookie de sesión firmada `HttpOnly; SameSite=Strict`; scripts →
  `Bearer`. El handshake WebSocket valida lo mismo.
- **Origin/Host**: allow-list; loopback por defecto; `--tailscale` añade la IPv4 y el
  DNS name. Origin ajeno → 403; Host no permitido → 400.
- **Binding**: default loopback; el modo remoto es explícito (`--tailscale`/`--host`)
  y exige token.
- **Autorización**: hook `authorize(identity)` trivial hoy (autenticado = acceso
  total), listo para RBAC futuro sin reescribir.
- `authentication` y `authorization` están separadas; `transport`, `auth` y
  `operations` también.

## WebSocket: entrega fiable (Fase 12.1)

El canal WebSocket usa lectura con `select()` pero el socket **permanece bloqueante
para el envío**. Un `sendall` sobre un socket no bloqueante lanzaba `BlockingIOError`
cuando el buffer se llenaba (p. ej. en un enlace remoto con transcript grande) y
mataba el bucle WS — en localhost no se veía porque el loopback drena al instante.
Por eso "se veían Teams/agentes pero no el transcript ni el input" en remoto. `_send`
escribe de forma bloqueante con timeout, y el frame `state` incluye `messages`. El
frontend auto-selecciona el primer agente y, si recibe 401, redirige a `/login`.

## Contención de TMPDIR

Bun/OpenCode filtran `.so` en `TMPDIR`. Se aísla a los agentes en un directorio propio
bajo el state home y se poda (arranque, stop de agente, janitor periódico), con cap de
tamaño y edad. Es entorno de ejecución, no una primitiva de dominio.

## Emulación de terminal (pyte): decisión

**No se introduce `pyte` todavía.** Razonamiento:

1. **Qué problema resolvería**: unificar `capture()` entre PTY (stream ANSI crudo) y
   tmux (texto renderizado) bajo un modelo de "pantalla" idéntico.
2. **Qué información falta hoy**: para detectar el estado de OpenCode y Claude Code
   no falta información; los marcadores (`esc interrupt` / `esc to interrupt`,
   footer, `· done <hora>`) están presentes en el texto de tmux. En PTY, los
   adapters no funcionarían igual porque las secuencias ANSI no están interpretadas
   — pero el caso real validado usa tmux.
3. **Qué API cambiaría**: `capture()` devolvería una pantalla normalizada en lugar
   del buffer/scrollback actual. Sería un cambio de `InteractiveSession`, no del
   Harness.
4. **Por qué no se resuelve dentro del Harness**: normalizar ANSI es una
   responsabilidad de la capa de sesión/transporte (aplica a cualquier consumidor),
   no de un adapter de agente concreto.

Conclusión: se resuelve dentro del Harness con las capacidades existentes y se
documenta la limitación. Si aparece la necesidad real de un `OpenCodeHarness`
funcionando sobre PTY con la misma fidelidad que sobre tmux, ése será el disparador
para evaluar `pyte`.

## El contrato como test

La prueba de que la abstracción es correcta es que `ContractMixin` corre **sin
cambios** contra `PtyBackend` y `TmuxBackend`. Si hubiera que bifurcar el test según
el backend, la abstracción estaría mal. `ContractGuard` verifica además que el
contrato **falla** ante un backend roto: los tests no son vacíos.

## Ops expuestas, quién puede invocarlas y su riesgo (Fase 1, 0.49.0)

Toda op del daemon se invoca por el socket UNIX 0600. La Web UI queda limitada a `ALLOWED_OPS`
(`web/server.py`) y cada respuesta pasa por la autenticación (token o cookie de sesión) y la
política de Host/Origin. Las ops marcadas **audit** escriben una línea en `state/audit.jsonl`
(0600, append-only, rotada por tamaño) sin valores secretos.

| Grupo (ops) | Quién puede invocarla | Auditoría | Riesgo |
|---|---|---|---|
| `ping`, `meta_info`, `agent_list`, `agent_info`, `agent_state`, `team_list`, `team_info`, `team_members`, `message_history`, `interaction_list`, `agent_history`, `agent_transcript`, `agent_capture`, `agent_processes`, `agent_process_output`, `agent_archive_list`, `agent_archive_get`, `bundle_list`, `settings_get`, `provider_check`, `frontend_status`, `update_status`, `clean_plan`, `reset_plan` | web (local/tailscale autenticado) y CLI local | — | Bajo: solo lectura del plano de control. |
| `fs_complete` | web/CLI | **sí** (incluye rechazos) | Bajo-medio: revela **nombres** de directorios, acotado a `security.fs_roots` + workspaces/cwd; sin ficheros ni contenido; con tope y límite de frecuencia. |
| `agent_create`, `agent_send`, `agent_write`, `agent_key`, `agent_resize`, `agent_interrupt`, `agent_stop`, `agent_new_session` | web/CLI | `agent_create`, `agent_send`, `agent_stop`, `agent_new_session` | Alto: lanza procesos/inyecta entrada. No acepta diálogos de confianza ni flags peligrosos. |
| `message_send`, `team_send` | web/CLI (y agentes con su token) | **sí** | Medio: mensajería limitada por Team; identidad autoritativa por token. |
| `team_create`, `team_up`, `team_set_workspace`, `team_add_member`, `team_remove_member`, `team_remove` | web/CLI | **sí** | Medio: reorganiza equipos; no borra proyectos. |
| `interaction_respond`, `agent_process_signal` | web/CLI | **sí** | Alto: responde a permisos de agentes / señala procesos; queda auditado con `by`. |
| `settings_set`, `settings_reset` | web/CLI | **sí** (claves; hash corto si es sensible) | Alto: cambia binarios/entorno de proveedor (**exige `CONFIRM`**), política de red y de acceso. |
| `frontend_set` | web/CLI (la Web UI no puede apagar el Web UI) | **sí** | Medio-alto: abre/cierra listeners; reinicia la superficie de red. |
| `bundle_export`, `bundle_import`, `bundle_delete` | web/CLI | **sí** | Medio: import/export de configuración verificada; sin tokens salvo export explícito. |
| `clean_apply`, `reset_apply` | web/CLI | **sí** | Alto: borra temporales/backups/releases; `reset_apply` full exige `RESET` y respaldo. |
| `agent_identity`, `agent_hook`, `agent_permission_request` | agente (token de identidad validado por el daemon) | — | Medio: el daemon valida token y Team; un evento ajeno se ignora (fail-open). |
| `agent_list(viewer=True)`, `agent_transcript`, `agent_capture` desde el bucle WS | web autenticado | — | Bajo: refresco del visor. |

Regla: ninguna op con efecto (escritura, proceso, red o borrado) se expone sin autenticación ni
auditoría. Las ops solo-lectura no se auditan para no inundar el registro.
