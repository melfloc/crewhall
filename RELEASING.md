# Releases y actualizaciones: criterio y mecanismo

> **In English (summary):** versioning policy (SemVer) and the release/update
> mechanism: main is the single development line; releases are signed,
> immutable artifacts built from a tagged commit; updates are verified and
> reversible, and never restart a daemon with agents running.

## Principios

1. **Una sola línea de desarrollo: `main` en esta máquina.** Todo cambio nace, se prueba y se
   confirma aquí. Nada se edita "en la HOST".
2. **Las otras máquinas solo reciben *releases*.** Una release es un artefacto firmado, construido
   desde un commit etiquetado `vX.Y.Z` de `main` (nunca desde el árbol de trabajo), inmutable.
   `crewhall update` solo actúa sobre instalaciones *gestionadas* (creadas por `install.sh`);
   en un checkout de desarrollo se niega.
3. **Actualizar no puede destruir trabajo en curso.** Instalar una versión nueva no reinicia el daemon
   (reiniciarlo cierra los agentes). El reinicio es una decisión explícita (`--restart`) y se niega
   mientras haya agentes corriendo salvo `--force-restart`.
4. **Todo es reversible.** Se conserva la versión anterior (`venv.prev`) y una copia de config+estado
   previa a cada update; `update --rollback` vuelve atrás; un reinicio que no deja el sistema sano
   revierte solo.
5. **Se verifica lo que se instala.** sha256 + firma SSH (`ssh-keygen -Y`) contra el firmante que la
   máquina destino ya confía; un build nuevo se prueba (versión, imports) *antes* de reemplazar nada.

## Versionado (SemVer, aplicado a este proyecto)

| Cambio | Sube |
|---|---|
| Corrección que no cambia comportamiento documentado | PATCH |
| Funcionalidad nueva compatible (comandos, ops del daemon, claves de config **añadidas**) | MINOR |
| Rompe CLI/ops/config documentados, o cambia `state.json` de forma no legible por la versión anterior | MAJOR |

Reglas de compatibilidad (las comprueban los tests y `release.sh`):

- **`state.json`**: cada versión debe leer los estados de las anteriores. Cada release deja una fixture en
  `tests/fixtures/state-<versión>.json` y la suite las carga todas. Un cambio de `SCHEMA_VERSION` exige
  migración + MAJOR; `update --rollback` se niega si el estado es de un esquema más nuevo que el que
  entiende la versión anterior.
- **Config del usuario** (`profiles.toml`, `teams/*.toml`, `bundle`): solo se *añaden* claves; las
  desconocidas se rechazan con mensaje, las antiguas siguen valiendo.
- **Operaciones del daemon / CLI**: solo se añaden. Quitar algo = MAJOR con aviso previo en el CHANGELOG.
- La versión vive en tres sitios que deben coincidir: `pyproject.toml`, `crewhall/__init__.py`
  y la sección superior de `CHANGELOG.md` (lo exige la compuerta).

## Flujo normal

```bash
# 1. desarrollar en main, con tests; subir versión + CHANGELOG en el commit de release
git commit ...
# 2. (recomendado) prueba real con agentes, antes de liberar
crewhall selftest --cwd ~/Projects/crewhall
# 3. cortar la release: compuertas → tests → build desde el commit → firma → tag
scripts/release.sh
# 4. llevarla a una máquina (primera vez instala; después actualiza)
scripts/deploy.sh HOST                 # no reinstala el daemon en marcha
scripts/deploy.sh HOST --restart       # y lo reinicia si no hay agentes corriendo
# 5. verificar allí
ssh HOST 'crewhall selftest --cwd <carpeta de confianza>'
```

### Compuertas de `release.sh` (todas obligatorias)

1. rama `main` y árbol **limpio**;
2. `pyproject.toml` = `__init__.py` = sección superior de `CHANGELOG.md`, con notas no vacías;
3. el tag `vX.Y.Z` **no existe** y la versión es mayor que la última etiquetada (las versiones no se reescriben);
4. la suite completa pasa;
5. la release se construye desde `git archive` del commit, se firma y la firma se verifica antes de etiquetar.

Salida: `releases/vX.Y.Z/` → `release.json`, `crewhall-X.Y.Z-installer.tar.gz` (+ `.sha256`, `.sig`), `NOTES.md`.

### Qué hace `crewhall update --from <dir|url>` en el destino

1. verifica sha256 y firma (si la máquina tiene `~/.config/crewhall/allowed_signers`, **sin firma válida se rechaza**);
2. rechaza versiones iguales/anteriores (`--force`, `--allow-downgrade`);
3. construye `venv.new` desde el wheel y lo prueba; si falla, no se toca nada;
4. guarda config+estado en `~/.local/state/crewhall/backups/pre-update-*.tar.gz` (últimos 5);
5. intercambia `venv` ↔ `venv.new` y deja `venv.prev`;
6. con `--restart`: reinicia el daemon, comprueba versión y `doctor`; si algo falla, **revierte solo**.

Sin `--restart`, `crewhall update --status` y `doctor` avisan de *"reinicio pendiente"* mientras el
daemon siga con el código viejo. Se aplica cuando no haya agentes corriendo con `crewhall update --restart`
(sin `--from`). **Una negativa por haber agentes activos no cambia nada** (se comprueba antes de instalar y
nunca dispara rollback ni reinicio): sale con código 3; `--force-restart` es la única vía que los cierra.

`--check` solo informa (exit 10 si hay versión nueva). `--rollback [--restart]` vuelve a la anterior.

## Confianza de la firma

La primera vez, `deploy.sh` instala en el destino la clave pública de firma (por el canal SSH que ya
usas). Después, la máquina solo acepta releases firmadas por esa clave: un tarball alterado, de otro
firmante o sin firma se rechaza. Si rotas tu clave: `deploy.sh HOST --rotate-signer`.

## Hotfix en la HOST

No se parchea allí. Se corrige en `main`, se sube PATCH, `release.sh`, `deploy.sh`. Si hay que volver
atrás ya: `ssh HOST 'crewhall update --rollback --restart'`.

## Qué NO hacer

- No `git pull`/`pip install -e` en una máquina destino.
- No reutilizar un número de versión ni mover un tag.
- No liberar con `--no-sign` para producción (`deploy.sh` lo rechaza).
