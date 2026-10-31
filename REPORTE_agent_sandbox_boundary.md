# REPORTE — Stage 1: frontera OS para agentes

Rama: `feat/agent-sandbox-boundary` (creada desde `main` @ `f262455`).
No se ha tocado `feat/agent-spawn-policy`, ni `agent_spawners`, ni las ops
`agent_create`/`agent_stop` del plano de control. Todo lo ejecutado usó XDG
desechables, un socket tmux privado y stubs; no se arrancó el daemon real ni se
leyó/escribió su estado.

---

## 1. Reproducer baseline (main, antes del cambio)

Fichero: `tests/security/test_os_boundary.py` (adaptación de los reproducers
`sec2` de `REVISION_SEGURIDAD_agent_spawn_r2.md`; el repo de `main` no tiene
`spawn_policy`, así que R2b/N1 se reproducen sobre el mismo launch path que
comparten ambas ramas).

Comando literal:

```bash
python3 -m unittest tests.security.test_os_boundary -v
```

Salida literal (recortada a las líneas de evidencia):

```text
Ran 3 tests in 0.298s
FAILED (failures=3)

[N1] attack={'rc': 0, 'rc2': 0, 'err': '', 'tmux': '/tmp/tmux-1000/sec33742549eb9cb,3374259,1'} victim_has_HACKED=True
[R2b] launch after settings rewrite: ['/tmp/at-boundary-dk5idjms/workspace/claude-b']
[BOUNDARY] {"agent_token_present": true, "agent_tokens": "{\"tokens\":{\"victim\":\"AGENT-SECRET\"}}", "audit": "ERR:2", "control_token": "CONTROL-SECRET", "daemon_socket": "ERR:[Errno 2] No such file or directory", "docker_sock": "socket", "groups": [952, 967, 983, 989, 998, 1000], "id": "uid=1000(abraham) gid=1000(abraham) groups=1000(abraham),952(adbusers),967(docker),983(video),989(lp),998(wheel)", "proc_entries": ["1", "10", "100", "1006184", "101", "102", "103", "1031", "1032973", "1035", "104", "1042", "1043", "1045", "105", "1055", "1058", "106", "107", "109"], "proc_other": "SHELL=/usr/bin/bash\u0000COLORTERM=truecolor\u0000__MISE_EXE=/usr/bin/mise", "state_json": "{\"secret\":\"STATE-SECRET\"}", "state_listing": ["agent-tokens.json", "control-token", "crewhall", "state.json"], "tmp_entries": [".ICE-unix", ".X0-lock", ".X11-unix", ".XIM-unix", ".bun-0-c2264302b7dfb1b7.so", ".bun-1000-71b650bef1b3e52c.node", ".bun-1000-89812e6e6a68aaeb.so", ".bun-1000-c2264302b7dfb1b7.so", ".font-unix", "at-audit-453ddc73", "at-audit-aph_37zr", "at-audit-fwvb3lje", "at-audit-ik5flbpy", "at-boundary-fzjba95v", "at-cf-9yucxczp", "at-conv-2iibc4l7", "at-conv-399tce4u", "at-conv-3llxat0f", "at-conv-6je2x6bh", "at-conv-6pv11vqs"], "tmux_dir": true, "workspace_write": "ok"}
```

Los tres fallos confirman:

- **R2b**: escribir `providers.claude.command` después de arrancar cambia el
  binario real del siguiente launch (`claude-b`).
- **N1**: el agente hereda `$TMUX` y escribe en el pane `victim`
  (`HACKED=True`), usando el socket compartido.
- **Frontera OS**: el agente lee `control-token`, `agent-tokens.json`,
  `state.json`, `/proc/<otro>/environ`, ve `/tmp/tmux-1000`, alcanza
  `docker.sock` y conserva los grupos `docker` (967) y `wheel` (998).

N2/N3 de la revisión original son exclusivos del spawn path y no se reproducen
en `main` (no existe `spawn_policy`); este Stage no añade `agent_spawners` ni
toca esas ops. N3 (pane muerto revivible) sí se mitigó aquí como parte de N1.

---

## 2. Cambios y líneas

### Sandbox OS

- `crewhall/sandbox.py:190` `command_for_spec()`: envuelve *cada* launch local
  de agente; `:57` exige un `bwrap` absoluto, root-owned y no escribible por
  otros; `:90` copia el launcher al state dir; `:130` verifica su SHA-256 en
  cada launch.
- `crewhall/sandbox_launcher.py:97` `build_argv()`: namespace de mount/pid/ipc/
  uts/cgroup (`:112`), `--die-with-parent`, `--new-session`, `--cap-drop ALL`
  (`:113`), root ro, `--tmpfs /tmp`, `/proc` privado, `/dev` privado.
  `:125` enmascara el state dir y reexpone solo `hooks/` y `mcp/` ro, `tmp/` rw,
  `opencode-state` rw y el workspace concreto; `:159` enmascara docker/podman
  (incluido `/run/user/<uid>/bus` y `systemd/private`); `:171` expone únicamente
  `daemon.sock`; el launcher verificado se bindea ro en
  `/tmp/crewhall-sandbox-launcher.py` (`:145`).
- `crewhall/sandbox.py:168` `_validate_workspace()`: rechaza `/`, `$HOME`,
  `/home` y cualquier workspace que contenga (o esté dentro de) state/config/
  runtime. Sin bypass por variable de entorno.
- `crewhall/types.py:45-49`: `SessionSpec.sandboxed` +
  `sandbox_agent_id`.
- `crewhall/services/agents.py:1805`, `services/control.py:161`,
  `services/persistence.py:142,357`: marcan como sandboxed **todos** los
  agentes locales (create, restart y revive), no solo Heraldo. `host` remoto
  queda fuera: no comparte el uid/plano de control local.
- `crewhall/backends/tmux.py:107` y `backends/pty.py:40`: aplican el wrapper.
- `crewhall/backends/tmux.py:127`: `unset TMUX TMUX_PANE DOCKER_HOST
  SSH_AUTH_SOCK DBUS_SESSION_BUS_ADDRESS` antes del exec; el launcher lo repite
  (`sandbox_launcher.py` `SENSITIVE_ENV`).
- `crewhall/backends/tmux.py:270`: al registrar el exit, mata el pane muerto;
  `:356` conserva el exit code observado antes del kill.

### R2b — providers congelados

- `crewhall/settings.py:401` `freeze_providers()`, `:430` `provider()` lee el
  snapshot, `:419` reset explícito solo para tests/teardown.
- `crewhall/settings.py:693,710`: `settings.patch()`/`reset()` refrescan el
  snapshot; son ops del plano de control.
- `crewhall/daemon.py:641` y `crewhall/commands/system.py:303` congelan al
  arrancar el daemon. `_run_foreground` lo libera en `finally` para no contaminar
  procesos in-process (tests/UI embebida).
- `providers.<kind>.{command,fallback_command,default_args,default_model,
  default_variant,env,mcp,models}` quedan cubiertos porque todas las funciones
  (`provider_command`, `provider_args`, `provider_env`, `provider_effective_command`,
  `enabled_kinds`, …) pasan por `provider()`.

### N1 — hooks y OpenCode

- `crewhall/hooks.py:47` `BASH_DENY = Bash(tmux:*), Bash(screen:*),
  Bash(gdb:*), Bash(strace:*)`; `:109` se añaden al `permissions.deny` de Claude.
- `crewhall/hooks.py:103` añade `$XDG_CONFIG_HOME/crewhall/**` a los denies de
  path (sec_1b).
- `crewhall/services/agents.py:203` mergea en `OPENCODE_CLI_CONFIG_CONTENT`
  `permission.bash.{tmux,screen,gdb,strace} = deny`; `:232` se aplica en create,
  restart y revive.

---

## 3. Tests invertidos (rama)

Comando literal:

```bash
python3 -m unittest tests.security.test_os_boundary -v
```

Salida literal:

```text
test_claude_settings_deny_the_shell_escapes ... ok
test_opencode_config_carries_the_same_denies ... ok
test_root_workspace_is_refused ... ok
test_agent_cannot_type_into_another_agents_pane ... ok
test_an_exited_agent_leaves_no_dead_pane ... ok
test_provider_settings_are_frozen_after_start ... ok
test_sandbox_hides_the_control_plane ... ok
Ran 7 tests in 1.775s
OK

[N1] attack={'rc': 1, 'rc2': 1, 'err': 'error connecting to /tmp/tmux-1000/default (No such file or directory)\nerror connecting to /tmp/tmux-1000/default (No such file or directory)\n', 'tmux': None} victim_has_HACKED=False
[N3-pane] status=Status.EXITED has-session rc=1
[R2b] launch after settings rewrite: ['/tmp/at-boundary-oxsa3zft/workspace/claude-a']
[BOUNDARY] {"agent_token_present": true, "agent_tokens": "ERR:2", "audit": "ERR:2", "control_token": "ERR:2", "daemon_socket": "connected", "dbus_env": null, "docker_host_env": null, "docker_sock": "not-socket", "groups": [65534, 65534, 65534, 65534, 65534, 1000], "id": "uid=1000(abraham) gid=1000(abraham) groups=1000(abraham),65534(nobody)", "proc_entries": ["1", "2", "acpi", "asound", ...], "proc_other": "ERR:2", "ssh_auth_env": null, "state_json": "ERR:2", "state_listing": ["tmp"], "tmp_entries": ["at-boundary-804851pe", "crewhall-sandbox-launcher.py"], "tmux_dir": false, "tmux_env": null, "tmux_pane_env": null, "workspace_write": "ok"}
```

Lectura de la evidencia:

| Comprobación DoD | Resultado |
|---|---|
| control-token ilegible | `ERR:2` |
| agent-tokens.json ilegible | `ERR:2` |
| state.json/audit ilegibles | `ERR:2` |
| `/proc/<otro>/environ` invisible | `ERR:2` |
| tmux socket invisible | `tmux_dir=false`; `TMUX=null` |
| docker.sock inalcanzable | `not-socket` (char device `/dev/null`) |
| `id` sin docker/wheel | `groups=1000,65534(nobody)` |
| daemon socket alcanzable | `connected` |
| workspace escribible | `ok` |
| token del agente presente | `agent_token_present=true` |
| pane muerto eliminado | `has-session rc=1` |

---

## 4. Verificación con daemon de usar y tirar

Script `/tmp/stage1-live.sh`: XDG propios, `CREWHALL_TMUX_SOCKET=stage1$$`,
`python3 -m crewhall.daemon`, stubs `claude-a`/`claude-b` que escriben un marker
y duermen. Salida literal:

```text
--- create A1 (human)
○ sess_3aacfccfb386  name=A1  kind=claude  state=starting  backend=tmux  pid=3416382
markers after A1: A=1 B=0
--- rewrite settings to claude-b (no restart)
--- create A2 (must use frozen claude-a)
○ sess_55fd759c0aab  name=A2  kind=claude  state=starting  backend=tmux  pid=3416518
markers after A2: A=2 B=0
--- restart A1 (must use frozen claude-a)
restarted A1: state=starting (resumed) in 32.3s; terminated=3 signalled, 0 remaining
markers after restart: A=3 B=0
--- agent list
AGENT_ID             NAME         KIND       STATE          BACKEND
sess_3aacfccfb386    A1           claude     starting       tmux
sess_55fd759c0aab    A2           claude     starting       tmux
--- stop A2 (human)
○ sess_55fd759c0aab  name=A2  kind=claude  state=starting  backend=tmux  pid=3416518
--- daemon restart with A1 alive
AGENT_ID             NAME         KIND       STATE          BACKEND
sess_3aacfccfb386    A1           claude     starting       tmux
--- stop A1, daemon stop
○ sess_3aacfccfb386  name=A1  kind=claude  state=starting  backend=tmux  pid=3416696
```

- R2b: A2 y el restart de A1 ejecutan `claude-a`; `B=0` pese a reescribir
  `settings.json` con `claude-b`.
- Create/stop humanos funcionan.
- El agente sobrevive al reinicio del daemon (`A1` reaparece).
- El mismo flujo se repitió con el entrypoint `crewhall daemon run`: también
  congeló el snapshot (`A=1`, `A=2`, `A=3`; `B=0` en los tres pasos).

También se arrancó el Claude real (`2.1.314`) dentro del sandbox en un tmux
privado: el binario responde `--version`, `--help` y el TUI llega al diálogo de
confianza del workspace. No se envió ningún prompt ni se usó la sesión real.

---

## 5. Suite completa y lint

```text
uv run --with pytest --with ruff python -m pytest -q
1585 passed, 182 skipped, 7 warnings, 4 subtests passed in 261.98s (0:04:21)
```

```text
uv run --with ruff python -m ruff check crewhall tests/security tests/test_security_surface.py
All checks passed!
```

`ruff check .` (repo completo) sigue mostrando 6 hallazgos **preexistentes** en
`experiments/demo_bash.py` y `experiments/demo_ui.py`, ficheros no tocados por
este Stage.

---

## 6. Tabla de mutaciones

| Mutación | Test que debe fallar | Evidencia |
|---|---|---|
| Quitar el snapshot de `providers.*` (`settings.freeze_providers`) | `test_provider_settings_are_frozen_after_start` | Baseline main: `argv[0]` = `claude-b`; rama: `claude-a`, `B=0` |
| Quitar `unset TMUX/TMUX_PANE` **y** el mask de `/tmp` | `test_agent_cannot_type_into_another_agents_pane` | Baseline main: `victim_has_HACKED=True`, `TMUX` heredado; rama: `rc=1`, `HACKED=False` |
| Quitar el sandbox (`sandboxed=False`) | `test_sandbox_hides_the_control_plane` | Baseline main: token/tokens/environ legibles, docker socket, grupos docker/wheel; rama: todos `ERR:2`/`not-socket`/sin grupos |
| Quitar el `kill-session` al registrar el exit | `test_an_exited_agent_leaves_no_dead_pane` | Baseline: el pane queda (`remain-on-exit`); rama: `has-session rc=1` |
| Quitar el deny de `Bash(tmux:*)` | `test_claude_settings_deny_the_shell_escapes` | El test exige las cuatro reglas en `permissions.deny` |

---

## 7. DoD

- [x] Reproducers R2b/N1 fallan en `main` (salida en §1).
- [x] Suite invertida verde en la rama (§3).
- [x] Frontera verificada dentro de un agente real de tmux (§3).
- [x] R2b: editar `settings.json` tras arrancar no cambia create/restart (§4).
- [x] N1: `tmux send-keys` a otro pane falla (§3).
- [x] Identidad/list/message/new-session: suite existente verde; create/stop
      humanos verificados en vivo (§4).
- [x] Agentes sobreviven al reinicio del daemon (§4).
- [x] `pytest` completo y `ruff` (§5).
- [x] Sin `agent_spawners`, sin AGENT_OPS nuevas, sin rama archivada.
- [x] Sin push/tag/release/daemon real.

---

## 8. Riesgos residuales (documentados, no bloquean Stage 1)

1. **Tokens de agente en claro en reposo** (paso 6 opcional, diferido):
   `agent-tokens.json` sigue guardando los tokens en claro. El sandbox ya impide
   que un agente lo lea, pero un proceso mismo-uid fuera del sandbox (humano,
   root, otro servicio) puede leerlo. Se deja para Stage 2 o un follow-up.
2. **Agentes remotos**: `sandboxed=not host`. Un agente SSH corre en el host
   remoto y no comparte el uid ni el plano de control local; su aislamiento es
   responsabilidad de ese host. No se debilita la frontera local.
3. **CLI config ro**: `~/.claude`, `~/.config/opencode`, etc. quedan read-only
   (root ro) y `XDG_CACHE_HOME` apunta a un cache por agente. El TUI real de
   Claude arranca, pero historiales que el CLI escriba bajo `$HOME` pueden
   degradarse; es intencional para que el agente no reescriba su propia config.
4. **`--new-session`**: bash interactivo avisa “no job control” dentro del
   sandbox; los TUIs probados (Claude real) arrancan. El `/dev/pts` es privado,
   así que no se pueden abrir los ptys de otros agentes.
5. **Dependencia de bwrap**: si falta `bwrap`, o el kernel no soporta
   `--unshare-cgroup`, el launch falla cerrado (`SandboxError`), nunca cae a un
   launch sin sandbox. No hay variable de bypass.
6. **TMPDIR compartido**: `state/tmp` se bindea rw y es el `TMPDIR` de todos los
   agentes. No contiene estado de control (el state dir está enmascarado), pero
   dos agentes mismo-uid pueden leer temporales ajenos. Aislarlo por agente
   queda como endurecimiento posterior, fuera de los DoD de este Stage.
