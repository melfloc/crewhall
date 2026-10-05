# Instalar crewhall en otro equipo

> **In English (summary):** how to install crewhall on another machine:
> build a signed release, deploy it over SSH (never edit on the target), install
> the systemd user service, verify with `doctor`/`selftest`, and manage updates.

Linux (probado en Arch/Omarchy); macOS debería funcionar (tmux + Python), sin servicios systemd.

## Requisitos

| Requisito | Notas |
|---|---|
| Python ≥ 3.11 | con `venv` (Debian/Ubuntu: `sudo apt install python3-venv`) |
| tmux | backend por defecto de los agentes (`--install-deps` lo instala con sudo) |
| `claude` y/o `opencode` | al menos una CLI de agente; cada una con su login/credenciales ya hechos |
| systemd de usuario | opcional, solo para `--service` / `--web` |

## Opción A — release firmada (recomendada; es la única vía para máquinas destino)

Todo lo que llega a otra máquina sale de una **release** (ver [RELEASING.md](RELEASING.md)). En el equipo de desarrollo:

```bash
scripts/release.sh                 # compuertas + tests + build desde el commit + firma + tag
scripts/deploy.sh HOST          # primera vez: instala; después: actualiza (sin reiniciar el daemon)
```

`deploy.sh` instala en el destino la clave pública de firma, sube la release y ejecuta allí
`install.sh` (primera vez) o `crewhall update` (siguientes). El destino no necesita red ni el código fuente.

Instalación manual de una release (sin `deploy.sh`):

```bash
cd releases/vX.Y.Z && scp release.json crewhall-*-installer.tar.gz* otro-equipo:
# en el destino
sha256sum -c crewhall-*-installer.tar.gz.sha256 && tar xzf crewhall-*-installer.tar.gz
cd crewhall-*-installer && ./install.sh --service
```

## Opción B — desde el repositorio

```bash
git clone <repo> crewhall && cd crewhall
scripts/install.sh --service --web
```

(Compila el wheel al instalar: necesita red para obtener `setuptools`.)

## Opciones del instalador

```
--prefix DIR       venv privado             (def: ~/.local/share/crewhall)
--bin-dir DIR      dónde enlazar el comando (def: ~/.local/bin)
--python PATH      Python ≥ 3.11 a usar
--install-deps     instalar tmux con sudo si falta
--service          servicio systemd de usuario para el daemon (arranca con el equipo)
--web              habilitar ya el Web UI local (127.0.0.1:8765)
--tailscale        habilitar ya el Web UI por Tailscale (exige token)
--web-port N       puerto del Web UI
--no-doctor        no ejecutar el diagnóstico final
```

Es idempotente: volver a ejecutarlo **actualiza** en el sitio (conserva config y estado).
Para que el daemon sobreviva al cierre de sesión (imprescindible en un servidor):
`sudo loginctl enable-linger $USER`.

## Qué instala y dónde

| Qué | Dónde |
|---|---|
| Código (venv aislado) | `~/.local/share/crewhall/venv` |
| Comando | `~/.local/bin/crewhall` (symlink) |
| Config (perfiles, equipos, token web) | `~/.config/crewhall/` |
| Estado (equipos/agentes persistidos, hooks) | `~/.local/state/crewhall/` |
| Runtime (socket, lock, log) | `$XDG_RUNTIME_DIR/crewhall/` |
| Servicios | `~/.config/systemd/user/crewhall*.service` |

## Llevarte tu configuración

```bash
# origen
crewhall bundle export mi-config.tar.gz                 # perfiles + equipos (team files)
crewhall bundle export mi-config.tar.gz --with-state    # + equipos/agentes persistidos
crewhall bundle export mi-config.tar.gz --with-token    # + token web (sensible)

# destino
crewhall bundle import mi-config.tar.gz --dry-run       # ver qué haría
crewhall bundle import mi-config.tar.gz                 # no pisa lo existente (--force lo hace, con .bak)
```

El bundle lleva un manifiesto con SHA-256, solo restaura rutas de una lista fija y rechaza
cualquier otra. Los equipos declarativos viven en `~/.config/crewhall/teams/*.toml`:
`crewhall agent team up ~/.config/crewhall/teams/mi-equipo.toml`.
Hay ejemplos en `examples/` (el instalador los copia como `*.example`).

## Verificar

```bash
crewhall doctor          # diagnóstico: python, tmux, CLIs, permisos, daemon, web, systemd…
crewhall doctor --json   # para automatizar; exit 1 solo si algo impide funcionar
crewhall --version
```

## Controlar las interfaces en caliente (sin servicios extra ni terminales abiertas)

El **daemon** aloja las interfaces web, así que se encienden y apagan con comandos, sin
levantar ni reiniciar nada y sin dejar una terminal ocupada:

```bash
crewhall tailscale            # solo por Tailscale (exige token); cierra la local
crewhall local                # solo 127.0.0.1; cierra la de Tailscale
crewhall tailscale --keep-local   # ambas a la vez
crewhall off                  # ninguna (los agentes siguen corriendo)
crewhall frontends            # qué está habilitado ahora (--json disponible)
crewhall local --port 9000    # cambia el puerto (se recuerda)
```

- Cambiar de modo **primero cierra** lo que sobra (listener y conexiones abiertas) y luego abre lo nuevo:
  al pasar de `tailscale` a `local`, el acceso por la tailnet se corta al instante.
- El modo se **recuerda** y el daemon lo restaura al arrancar (reinicio del equipo incluido). Si la
  tailnet no está disponible en ese momento, el daemon arranca igual y `frontends`/`doctor` lo avisan.
- `tailscale` genera el token la primera vez y lo muestra una vez; después: `crewhall web token show`
  (`web token generate --rotate` lo cambia). Sin token válido la API responde 401.
- La TUI no es un servicio: `crewhall ui` en cualquier terminal (por SSH también).
- Estas operaciones **no** están expuestas por la web: solo se controlan desde el socket local del daemon.
- Si ya hay un `crewhall web` suelto ocupando el puerto, el comando lo indica; ciérralo.

## Caso: servidor de HOST por SSH + Tailscale

Requisitos en la HOST que **no** instala crewhall: tailscale instalado y con sesión
(`sudo tailscale up`), y `claude` y/o `opencode` instalados **con login hecho** (una vez, a mano, en ese equipo).

```bash
# 1) en tu equipo (línea principal): liberar y desplegar
scripts/release.sh
scripts/deploy.sh HOST --install-args "--service --install-deps"   # primera vez: instala (+ confía en tu firma)

# 2) en la HOST (por SSH), una sola vez
sudo loginctl enable-linger "$USER"      # el daemon sigue vivo al cerrar la sesión SSH
crewhall tailscale                 # imprime la URL de la tailnet y crea el token
crewhall frontends

# 3) verificación real (usa unos pocos tokens)
crewhall selftest --cwd <carpeta que claude/opencode ya confían>

# 4) opcional: llevar tu configuración
#    (en tu equipo)  crewhall bundle export cfg.tar.gz && scp cfg.tar.gz HOST:
#    (en la HOST) crewhall bundle import cfg.tar.gz
```

Siguientes cambios: `scripts/release.sh && scripts/deploy.sh HOST` (nunca se edita ni se hace `git pull` allí).

Desde tu equipo abre la URL que imprimió (`http://<nombre>.<tailnet>.ts.net:8765/`) e introduce el token.
Antes del primer uso en una carpeta nueva, abre `claude` una vez ahí y acepta el diálogo de confianza
(crewhall no lo acepta por ti a propósito).

**Seguridad:** quien tenga el token puede crear y operar agentes en esa máquina (ejecutan código con tu
usuario). Trátalo como una contraseña; rótalo si se filtra y usa `crewhall off` cuando no lo necesites.

## Actualizar y desinstalar

```bash
# actualizar (en el dev):  scripts/release.sh && scripts/deploy.sh HOST
# en el destino:
crewhall update --status                 # versión instalada / del daemon / rollback disponible
crewhall update --from <dir-release> --check
crewhall update --from <dir-release>     # instala; el daemon sigue con el código viejo
crewhall update --from <dir-release> --restart   # y lo reinicia (se niega si hay agentes corriendo)
crewhall update --rollback --restart     # volver a la versión anterior
```

Detalle de garantías (backups, firma, rollback automático) en [RELEASING.md](RELEASING.md).

Desinstalar: `./uninstall.sh` (conserva config y estado; `--purge` los borra). Detiene el daemon: los
agentes en ejecución se cierran.

## Problemas frecuentes

- **`crewhall: command not found`** → añade `~/.local/bin` al `PATH`.
- **El Web UI sale en blanco / 404** → instalación sin el `index.html` (versiones < 0.20 sin
  `package-data`); reinstala con el paquete de release.
- **Un agente `claude` se queda en el diálogo de confianza de carpeta** → ábrelo una vez con
  `claude` en esa carpeta y acepta; crewhall no lo acepta por ti a propósito.
- **Cualquier duda** → `crewhall doctor` primero.
