# Crewhall: perfiles de agentes de este equipo

Crewhall crea este archivo como guía compartida para el equipo. No lo reemplaces
si ya existe. Aunque todos los agentes puedan leerlo, las instrucciones para
configurar perfiles de ejecutores solo aplican al spawner actual.

## Catálogo de agentes

El catálogo opcional `cwh-agents.json` define los perfiles que el spawner puede
usar. Crewhall solo lee ese archivo; no lo crea ni lo modifica. Si ya existe,
léelo y conserva sus perfiles. Propón cambios puntuales y no lo reemplaces sin
que la persona usuaria lo pida.

Solo si `crewhall agent identity` confirma que eres el spawner actual, y la
persona usuaria te pide configurar los ejecutores, revisa primero el proyecto:
sus tareas, instrucciones y agentes existentes. Si no eres el spawner, no crees
ni edites el JSON; pide al spawner actual o a la persona usuaria que lo haga.
Después crea el JSON con perfiles útiles para este proyecto, usando nombres
descriptivos elegidos para este equipo. No inventes agentes del proveedor: el
campo `agent` debe referirse a uno definido por el proyecto para Claude Code u
OpenCode.

Formato (versión 1):

```json
{
  "version": 1,
  "profiles": {
    "revisor-api": {
      "description": "Revisa cambios de API y sus pruebas.",
      "provider": "claude",
      "model": "sonnet",
      "agent": "api-reviewer",
      "effort": "high",
      "flags": {
        "--max-turns": 8,
        "--append-system-prompt": "Sigue las convenciones de este proyecto."
      }
    },
    "implementador-ui": {
      "description": "Implementa tareas de interfaz.",
      "provider": "opencode",
      "model": "openai/gpt-6#high",
      "agent": "ui-builder",
      "effort": "high",
      "flags": {"--thinking": true}
    }
  }
}
```

Cada perfil usa un nombre corto propio del proyecto. `provider` admite `claude`
u `opencode`; `model` es opcional y usa el nombre que entiende ese proveedor;
`agent` es opcional y selecciona un agente definido en la configuración del
proyecto. `effort` es opcional: Claude lo recibe como opción propia y OpenCode
lo usa como variante del modelo. En OpenCode también puedes escribir la variante
directamente en `model` como `proveedor/modelo#variante`; si defines `effort`,
debe haber un modelo en el perfil o en Settings.

`flags` es opcional y debe usar solo estas opciones permitidas:

- Claude Code: `--max-turns` (entero de 1 a 1000) y
  `--append-system-prompt` (texto breve).
- OpenCode: `--thinking` (`true` o `false`).

No agregues comandos, rutas ejecutables, variables de entorno ni opciones de
permisos, servidor, directorio, modelo, agente o backend dentro de `flags`. El
backend y la carpeta de trabajo los fija Crewhall a partir del spawner y el Team.
Usa los campos específicos para modelo, agente y esfuerzo. Crewhall valida cada
perfil antes de iniciar un agente; las opciones desconocidas se rechazan.

El archivo de perfiles define opciones de trabajo, no permisos. Poder leer esta
guía o el JSON no concede privilegios. Solo el agente que actualmente aparece
como spawner del equipo puede crear ejecutores, y Crewhall vuelve a comprobarlo
al recibir cada solicitud. Consulta
`crewhall agent identity` para ver si tienes ese rol y qué perfiles están
disponibles. Para crear un ejecutor usa:

```text
crewhall agent create --name <nombre> --profile <perfil>
```

No incluyas claves, tokens ni otros secretos en este archivo.
