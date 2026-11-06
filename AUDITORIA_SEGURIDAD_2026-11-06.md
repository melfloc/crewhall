# Auditoría de seguridad de Crewhall — 2026-11-06

## Dictamen

**Verde para `main` dentro del alcance acordado, con el riesgo residual de mismo UID aceptado y documentado.** El sandbox de sistema operativo no es requisito del spawner ni de esta entrega. El spawner aplica política a las solicitudes que pasan por Crewhall; no contiene código hostil que ya se ejecuta con el usuario del daemon.

Esto no certifica a Crewhall como entorno seguro para prompts, herramientas o agentes deliberadamente maliciosos bajo el mismo UID. Para esa amenaza se necesitaría un límite de sistema operativo separado —otro usuario, contenedor o VM— y su política específica de filesystem, red, sockets y credenciales. Ese frente queda fuera de `main`.

## Alcance revisado

Revisé `main` en `84c6d58` más los cambios locales de creación/parada de agentes, autorización del daemon, identidad de procesos, Teams, perfiles de proyecto, límites, persistencia, auditoría, UI y documentación. No usé los reportes antiguos de ramas como evidencia del código actual. No inicié agentes con modelos reales ni accedí a credenciales del usuario.

## Controles verificados

- `agent_create` y `agent_stop` siguen fuera del conjunto general de operaciones de agente. La ruta acotada valida token, ID exacto, pertenencia al Team, rol actual de spawner y que el socket venga del árbol del proceso de ese agente. El peer se fija mediante `SO_PEERPIDFD`; si el kernel no ofrece esa opción, el RPC de creación/parada del agente falla cerrado. Las operaciones humanas del plano de control siguen disponibles.
- Solo Teams locales con workspace compartido pueden tener un spawner. Un agente puede ser spawner de un solo Team; los hijos no heredan el rol. El spawner puede detener únicamente sus propios hijos, y el cambio de spawner exige transferir o parar y retirar los hijos antes de quitar al anterior.
- El daemon construye el lanzamiento desde un perfil aprobado. El RPC no puede cambiar proveedor, modelo, agente nativo, flags, backend, directorio, host ni Team. El catálogo tiene límites de tamaño y esquema; las opciones desconocidas y las flags de riesgo se rechazan.
- La aprobación del operador fija un hash del manifiesto y de las definiciones nativas de Claude/OpenCode que pueden seleccionarse. Una modificación requiere aprobar el catálogo de nuevo. El hash usado queda en el registro de cada creación.
- `max_live` limita hijos vivos por spawner; un estado de proceso que no se puede determinar cuenta como vivo. `max_created_per_day` limita reservas persistentes en una ventana de 24 horas y se comprueba antes de iniciar el proveedor; los intentos fallidos consumen el límite. Son límites de cantidad, no presupuestos de tokens, CPU ni dinero. La política también se vuelve a comprobar al reiniciar un hijo salido.
- El registro de `created_by`, las pertenencias y los eventos diarios se persiste para sobrevivir a reinicios. Los rechazos de creación/parada de agentes se limitan a 20 líneas por operación cada 60 segundos; el siguiente evento de esa operación deja un resumen de lo suprimido. Las acciones exitosas siguen registrándose.
- `cwh-agent.md` no reemplaza un archivo existente. Crewhall lee `cwh-agents.json`, pero no lo genera ni modifica.

## Hallazgo y decisión de alcance

El proceso del agente comparte UID y servidor tmux con el daemon. Puede lanzar CLIs directamente, usar tmux fuera del RPC, leer o cambiar archivos permitidos al UID, acceder a servicios locales disponibles para esa cuenta y detener el daemon. Las comprobaciones de árbol, token y pidfd reducen la autoridad de las solicitudes enviadas a Crewhall, pero no crean aislamiento del sistema operativo. El dueño aceptó explícitamente este riesgo para el spawner; por eso el aislamiento por OS se documenta como frente opcional e independiente, no como condición de publicación. El DoD histórico de `feat/agent-sandbox-spawn` quedó marcado como supersedido para `main`.

## Verificación

- Suite completa: `python -m unittest discover` — **1.798 pruebas, OK; 182 omitidas** por condiciones/prerrequisitos opcionales.
- Ruff: `uv tool run --from ruff ruff check crewhall tests` — **All checks passed**.
- Pruebas dirigidas de spawner, catálogo, límites, auditoría y pidfd, junto con la expectativa de serialización Team: **27 pruebas, OK**.
- El demo local levanta el daemon y tmux aislados con un proveedor determinista de prueba; el agente coordinador crea al worker y ambos se mandan mensajes. No usa API ni credenciales de modelos. El GIF está en `docs/images/agent-spawn-communication.gif` y el ejecutor en `experiments/demo_agent_spawn.py`.
- `git diff --check` no reportó errores. La suite completa emite `ResourceWarning` de fixtures existentes por archivos/subprocesos sin cerrar; no produjo fallos tras corregir la expectativa de `spawner_catalog_sha256` en `tests/test_teams.py`.

## Límites de publicación

El spawner requiere Linux con soporte del socket option `SO_PEERPIDFD` para las solicitudes iniciadas por agentes; en kernels sin soporte, esas operaciones se rechazan de forma segura. La política no es una barrera contra código no confiable ejecutado como el mismo usuario. Quien necesite esa garantía debe desplegar y auditar aparte una separación de usuario/VM/contenedor con sus sockets, secretos, rutas y red restringidos.
