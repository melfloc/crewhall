# OnlyOffice para crewhall (un contenedor por host)

Integra el editor/visor de OnlyOffice en el **modo chat** de crewhall: los
artefactos de una conversación (`outputs/`) se pueden previsualizar, editar por
el usuario y co-editar en vivo por el agente (Nivel 3, ver más abajo).

## Arquitectura de URLs (lo que más se falla)

OnlyOffice distingue **tres** direcciones; confundirlas rompe la integración:

| URL | Quién la usa | Valor en crewhall |
|---|---|---|
| `DocumentServerUrl` | el **navegador** carga el SDK (`.../web-apps/apps/api/documents/api.js`) | `office.public_url` |
| `document.url` | el **servidor OnlyOffice** descarga el archivo | `office.base_url` + `/api/conversation/artifact?...&dt=<token>` |
| `callbackUrl` | el **servidor OnlyOffice** guarda los cambios | `office.base_url` + `/api/office/callback?id=&path=` |

- El **token de descarga** (`dt`) es un JWT temporal que crewhall firma; el
  Document Server no tiene la cookie de sesión, así que se autentica con él.
- El **callback** lo firma OnlyOffice con el JWT y crewhall lo verifica
  (`office.jwt_secret` debe ser idéntico en ambos lados).
- `office.base_url` es la base de crewhall **vista desde el contenedor**. No es
  lo mismo que `office.public_url` (la que ve el navegador).

## Despliegue

```bash
cd scripts/onlyoffice
cp .env.example .env
printf 'ONLYOFFICE_JWT_SECRET=%s\n' "$(openssl rand -hex 32)" > .env
docker compose up -d
# comprobar: curl http://127.0.0.1:8081/healthcheck
```

## Configurar crewhall

```bash
crewhall settings set office.enabled true
crewhall settings set office.public_url "http://127.0.0.1:8081"   # el navegador
crewhall settings set office.jwt_enabled true
crewhall settings set office.jwt_secret "<el mismo secret del .env>"
crewhall settings set office.base_url "<base de crewhall vista por el DS>"
```

### Elección de `office.base_url`

**A) DS en Docker y navegador en la misma máquina.**
El contenedor no puede resolver `127.0.0.1` como el host; usa el gateway del
bridge Docker. Además crewhall debe escuchar en una dirección que el contenedor
alcance (el gateway o `0.0.0.0`, con token):

```bash
crewhall web --host 0.0.0.0 --port 8765        # expone también en el bridge
crewhall settings set office.base_url "http://172.17.0.1:8765"
```

**B) Acceso remoto por Tailscale.**
`office.base_url` y `office.public_url` apuntan al nombre del tailnet, que debe
resolver tanto el navegador como el contenedor (Tailscale en el host):

```bash
crewhall web --tailscale
crewhall settings set web.public_url "http://<maquina>.ts.net:8765"
crewhall settings set office.base_url  "http://<maquina>.ts.net:8765"
crewhall settings set office.public_url "http://<maquina>.ts.net:8081"
```

En el `docker-compose.yml`, expón el DS en la IP del tailnet
(`<tailscale-ip>:8081:80`).

### CSP

Al activar OnlyOffice, crewhall añade automáticamente el origen de
`office.public_url` a `script-src`, `frame-src`, `connect-src`, etc. No hay que
tocar nada más.

## Colaboración en vivo del agente (Nivel 3)

Con `office.collab_enabled = true` y **Chromium** instalado, crewhall levanta un
editor OnlyOffice *headless* como usuario "AI Agent" unido a la **misma sesión
de co-edición** (mismo `document.key`). El agente aplica cambios por la
Automation API vía CDP; aparecen en vivo (con su propio cursor) en el editor del
usuario.

```bash
crewhall settings set office.collab_enabled true
crewhall settings set office.chromium /usr/bin/chromium   # vacío = autodetecta
```

En el panel de artefactos del modo chat, el botón **Live** abre el co-editor.
Los comandos disponibles (ops del daemon):

- `office_collab_open {id, path}` — abre el editor headless.
- `office_collab_read {id, path}` — lee el texto del documento.
- `office_collab_insert {id, path, text}` — inserta contenido.
- `office_collab_command {id, path, method, args}` — cualquier método del
  connector de OnlyOffice (`Api.GetDocument()...`).
- `office_collab_close {id, path}`.

Sin Chromium, la op falla con un mensaje claro y sigue disponible el **Nivel 1**
(el agente edita el archivo por FS y el callback de OnlyOffice lo guarda).

## Firewall (UFW)

Hay **dos** direcciones y UFW rompe cada una por un motivo distinto:

1. **Contenedor → crewhall** (descargar el documento y guardar cambios por
   `callbackUrl`). La política INPUT por defecto descarta el tráfico que llega por
   `docker0`; lo verás como `ETIMEDOUT` en los logs del DS.
2. **Navegador → contenedor** (el editor). El puerto publicado (`8081`) es tráfico
   *forwarded* y la política FORWARD por defecto lo descarta: crewhall (8765)
   entra por la tailnet pero el editor no. Hay que permitir el forwarding.

`crewhall office setup` añade ambas reglas automáticamente (idempotente). A mano:

```bash
sudo ufw allow in on docker0
sudo ufw route allow in on tailscale0 out on docker0    # navegador por Tailscale
sudo ufw route allow in on <iface-lan> out on docker0   # navegador por LAN (opcional)
```

## Problemas conocidos

- **El editor no carga en el navegador / 8081 no responde desde la tailnet**: falta
  la regla de *forwarding* de UFW (punto 2 arriba); `crewhall office setup`
  interactivo la añade (necesita `sudo`).
- **El editor muestra la versión anterior**: el `document.key` no cambió.
  crewhall lo deriva de `path:mtime:size`, así que cambia al guardar.
- **El callback da 403**: el `office.jwt_secret` no coincide con el del DS.
- **OnlyOffice no puede descargar el archivo / callback ETIMEDOUT**: el
  contenedor no alcanza `office.base_url`. Con UFW activo, permite `docker0`
  (`sudo ufw allow in on docker0`); si no, usa una base alcanzable desde el
  contenedor (la IP de Tailscale o el gateway del bridge).
- **El editor no carga en el navegador**: `office.public_url` no es alcanzable
  desde el navegador, o el origen no está permitido (revisa la CSP generada).
- **`docker pull` se queda colgado sin descargar** (algunos hosts con el
  *containerd image store*): usa el driver overlay2 clásico añadiendo
  `"features": { "containerd-snapshotter": false }` a `/etc/docker/daemon.json`
  y `sudo systemctl restart docker`.
- **El contenedor no ve `office.base_url`**: el Document Server debe estar en el
  bridge por defecto (`network_mode: bridge`, gateway `172.17.0.1`); `crewhall
  office setup` ya lo configura así.
