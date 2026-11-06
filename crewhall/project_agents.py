"""Read the project-owned worker catalog used by Team spawners."""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from typing import Any

MAX_AGENT_FILES = 200
MAX_DEFINITION_BYTES = 256 * 1024
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PROFILES = 100
MANIFEST_NAME = "cwh-agents.json"
GUIDE_NAME = "cwh-agent.md"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@+#-]{0,199}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
log = logging.getLogger("crewhall.project_agents")

TEAM_GUIDE = """# Crewhall: perfiles de agentes de este equipo

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
"""


def _project_roots(cwd: str) -> list[str]:
    start = os.path.realpath(os.path.abspath(os.path.expanduser(cwd)))
    if not os.path.isdir(start):
        return []
    chain = [start]
    current = start
    while True:
        marker = os.path.join(current, ".git")
        if os.path.isdir(marker) or os.path.isfile(marker):
            return chain
        parent = os.path.dirname(current)
        if parent == current:
            return [start]
        chain.append(parent)
        current = parent


def _frontmatter(path: str) -> dict[str, str]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_DEFINITION_BYTES + 1)
        if len(raw) > MAX_DEFINITION_BYTES:
            return {}
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^([A-Za-z_-]+)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.lower()] = value
    return values


def _usable_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > 160 or name.startswith("-"):
        return None
    if any(ord(char) < 32 or char.isspace() for char in name):
        return None
    return name


def _definition(kind: str, name: Any, description: Any, source: str) -> dict[str, str] | None:
    clean_name = _usable_name(name)
    if not clean_name:
        return None
    desc = description.strip() if isinstance(description, str) else ""
    return {
        "profile": f"{kind}:{clean_name}",
        "kind": kind,
        "name": clean_name,
        "description": desc[:500],
        "source": source,
    }


def _markdown_agents(root: str, kind: str, dirname: str) -> list[dict[str, str]]:
    agent_dir = os.path.join(root, dirname)
    if not os.path.isdir(agent_dir) or os.path.islink(agent_dir):
        return []
    found: list[dict[str, str]] = []
    for current, dirs, files in os.walk(agent_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(current, d)))
        for filename in sorted(files):
            if not filename.endswith(".md") or len(found) >= MAX_AGENT_FILES:
                continue
            path = os.path.join(current, filename)
            if os.path.islink(path):
                continue
            meta = _frontmatter(path)
            if kind == "claude":
                name = meta.get("name")
            else:
                relative = os.path.relpath(path, agent_dir)
                name = relative[:-3].replace(os.sep, "/")
            if meta.get("mode", "primary").lower() == "subagent":
                continue
            if meta.get("hidden", "false").lower() == "true":
                continue
            if meta.get("disabled", meta.get("disable", "false")).lower() == "true":
                continue
            item = _definition(kind, name, meta.get("description"), path)
            if item:
                found.append(item)
    return found


def _strip_json_comments(text: str) -> str:
    """Remove JSONC comments without interpreting comment markers inside strings."""
    out: list[str] = []
    i = 0
    quoted = escaped = False
    while i < len(text):
        char = text[i]
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            i += 1
            continue
        if char == '"':
            quoted = True
            out.append(char)
            i += 1
            continue
        if text.startswith("//", i):
            while i < len(text) and text[i] not in "\r\n":
                out.append(" ")
                i += 1
            continue
        if text.startswith("/*", i):
            out.extend("  ")
            i += 2
            while i < len(text) and not text.startswith("*/", i):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i >= len(text):
                raise ValueError("unterminated JSONC comment")
            out.extend("  ")
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _jsonc(text: str) -> Any:
    clean = _strip_json_comments(text)
    # Remove trailing commas outside strings, which JSONC permits.
    out: list[str] = []
    i = 0
    quoted = escaped = False
    while i < len(clean):
        char = clean[i]
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            i += 1
            continue
        if char == '"':
            quoted = True
            out.append(char)
            i += 1
            continue
        if char == ",":
            j = i + 1
            while j < len(clean) and clean[j].isspace():
                j += 1
            if j < len(clean) and clean[j] in "]}":
                i += 1
                continue
        out.append(char)
        i += 1
    return json.loads("".join(out))


def _opencode_config(root: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    candidates = (
        os.path.join(root, "opencode.jsonc"), os.path.join(root, "opencode.json"),
        os.path.join(root, ".opencode", "opencode.jsonc"),
        os.path.join(root, ".opencode", "opencode.json"),
    )
    for path in candidates:
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        try:
            if os.path.getsize(path) > MAX_CONFIG_BYTES:
                continue
            with open(path, encoding="utf-8") as fh:
                config = _jsonc(fh.read(MAX_CONFIG_BYTES + 1))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(config, dict):
            continue
        entries: dict[str, Any] = {}
        for key in ("agent", "agents"):
            value = config.get(key)
            if isinstance(value, dict):
                entries.update(value)
        for name, meta in sorted(entries.items()):
            if not isinstance(meta, dict):
                continue
            if str(meta.get("mode") or "primary").lower() == "subagent":
                continue
            if meta.get("hidden") is True or meta.get("disabled") is True or meta.get("disable") is True:
                continue
            item = _definition("opencode", name, meta.get("description"), path)
            if item:
                found.append(item)
    return found


def _native_project_agents(cwd: str | None) -> list[dict[str, str]]:
    """Return provider agents defined in the project, for validating `agent`."""
    if not cwd:
        return []
    results: dict[str, dict[str, str]] = {}
    for root in _project_roots(cwd):
        for item in _markdown_agents(root, "claude", os.path.join(".claude", "agents")):
            results.setdefault(item["profile"], item)
        for item in _markdown_agents(root, "opencode", os.path.join(".opencode", "agents")):
            results.setdefault(item["profile"], item)
        for item in _opencode_config(root):
            results.setdefault(item["profile"], item)
    return [results[key] for key in sorted(results)]


def _validate_flags(provider: str, value: Any, profile: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"profile {profile!r}: flags must be a JSON object")
    allowed = {
        "claude": {"--max-turns", "--append-system-prompt"},
        "opencode": {"--thinking"},
    }[provider]
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"profile {profile!r}: unsupported flags: {', '.join(sorted(map(str, unknown)))}"
        )
    result: dict[str, Any] = {}
    if "--max-turns" in value:
        turns = value["--max-turns"]
        if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 1000:
            raise ValueError(f"profile {profile!r}: --max-turns must be an integer from 1 to 1000")
        result["--max-turns"] = turns
    if "--append-system-prompt" in value:
        prompt = value["--append-system-prompt"]
        if (not isinstance(prompt, str) or len(prompt) > 4000
                or any(ord(char) < 32 and char not in "\t" for char in prompt)):
            raise ValueError(f"profile {profile!r}: --append-system-prompt must be short text")
        result["--append-system-prompt"] = prompt
    if "--thinking" in value:
        thinking = value["--thinking"]
        if not isinstance(thinking, bool):
            raise ValueError(f"profile {profile!r}: --thinking must be true or false")
        result["--thinking"] = thinking
    return result


def _catalog_fingerprint(
    root: str, manifest_raw: bytes, native_agents: list[dict[str, str]],
) -> str:
    """Pin the manifest together with every native definition it can select."""
    digest = hashlib.sha256(b"crewhall-project-agent-catalog-v1\0")
    digest.update(manifest_raw)
    for path in sorted({item["source"] for item in native_agents}):
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError(f"project agent definition is not a regular file: {path}")
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_CONFIG_BYTES + 1)
        except OSError as exc:
            raise ValueError(f"could not read project agent definition {path}: {exc}") from exc
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError(f"project agent definition is larger than {MAX_CONFIG_BYTES} bytes")
        try:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
        except ValueError:
            rel = os.path.realpath(path)
        encoded = rel.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def _manifest_profiles(
    root: str,
    *,
    raw: bytes | None = None,
    native_agents: list[dict[str, str]] | None = None,
    catalog_sha256: str | None = None,
) -> list[dict[str, Any]]:
    path = os.path.join(root, MANIFEST_NAME)
    if not os.path.isfile(path) or os.path.islink(path):
        return []
    try:
        if raw is None:
            if os.path.getsize(path) > MAX_CONFIG_BYTES:
                raise ValueError(f"{MANIFEST_NAME} is larger than {MAX_CONFIG_BYTES} bytes")
            with open(path, "rb") as fh:
                raw = fh.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError(f"{MANIFEST_NAME} is larger than {MAX_CONFIG_BYTES} bytes")
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"could not read {MANIFEST_NAME}: {exc}") from exc
    version = data.get("version") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or isinstance(version, bool)
            or not isinstance(version, int) or version != 1):
        raise ValueError(f"{MANIFEST_NAME}: version must be 1")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        raise ValueError(f"{MANIFEST_NAME}: profiles must be an object")
    if len(profiles) > MAX_PROFILES:
        raise ValueError(f"{MANIFEST_NAME}: at most {MAX_PROFILES} profiles are allowed")

    native_agents = native_agents if native_agents is not None else _native_project_agents(root)
    native_names = {(item["kind"], item["name"]) for item in native_agents}
    manifest_sha256 = catalog_sha256 or _catalog_fingerprint(root, raw, native_agents)
    result: list[dict[str, Any]] = []
    for profile_name, body in sorted(profiles.items()):
        if not isinstance(profile_name, str) or not _PROFILE_NAME_RE.fullmatch(profile_name):
            raise ValueError(f"{MANIFEST_NAME}: invalid profile name {profile_name!r}")
        if not isinstance(body, dict):
            raise ValueError(f"profile {profile_name!r} must be an object")
        allowed_keys = {"description", "provider", "model", "agent", "effort", "flags"}
        unknown = set(body) - allowed_keys
        if unknown:
            raise ValueError(
                f"profile {profile_name!r}: unsupported fields: "
                f"{', '.join(sorted(map(str, unknown)))}"
            )
        provider = body.get("provider")
        if provider not in ("claude", "opencode"):
            raise ValueError(f"profile {profile_name!r}: provider must be claude or opencode")
        description = body.get("description", "")
        if (not isinstance(description, str) or len(description) > 500
                or any(ord(char) < 32 and char not in "\t" for char in description)):
            raise ValueError(f"profile {profile_name!r}: description must be at most 500 characters")
        model = body.get("model")
        if model is not None and (
            not isinstance(model, str) or not _MODEL_RE.fullmatch(model)
        ):
            raise ValueError(f"profile {profile_name!r}: invalid model name")
        agent = body.get("agent")
        if agent is not None and (
            not isinstance(agent, str) or not _usable_name(agent)
            or (provider, agent) not in native_names
        ):
            raise ValueError(
                f"profile {profile_name!r}: agent must name a provider agent defined in this project"
            )
        effort = body.get("effort")
        if effort is not None and (
            not isinstance(effort, str) or not _TOKEN_RE.fullmatch(effort)
        ):
            raise ValueError(f"profile {profile_name!r}: effort must be a short provider value")
        if (provider == "opencode" and effort and model and "#" in model
                and model.rsplit("#", 1)[1] != effort):
            raise ValueError(
                f"profile {profile_name!r}: OpenCode effort conflicts with the model suffix"
            )
        flags = _validate_flags(provider, body.get("flags"), profile_name)
        result.append({
            "profile": profile_name,
            "name": profile_name,
            "kind": provider,
            "provider": provider,
            "description": description.strip(),
            "model": model,
            "agent": agent,
            "effort": effort,
            "flags": flags,
            "source": path,
            "manifest_sha256": manifest_sha256,
        })
    return result


def project_agent_catalog(cwd: str | None) -> dict[str, Any]:
    """Return manifest status and the selected project profile catalog."""
    if not cwd:
        return {"present": False, "path": None, "profiles": [], "sha256": None,
                "error": None}
    for root in _project_roots(cwd):
        path = os.path.join(root, MANIFEST_NAME)
        if os.path.lexists(path):
            if os.path.islink(path) or not os.path.isfile(path):
                return {"present": True, "path": path, "profiles": [],
                        "sha256": None,
                        "error": f"{MANIFEST_NAME} must be a regular file, not a link"}
            try:
                with open(path, "rb") as fh:
                    raw = fh.read(MAX_CONFIG_BYTES + 1)
                if len(raw) > MAX_CONFIG_BYTES:
                    raise ValueError(f"{MANIFEST_NAME} is larger than {MAX_CONFIG_BYTES} bytes")
                native_agents = _native_project_agents(root)
                catalog_sha256 = _catalog_fingerprint(root, raw, native_agents)
                profiles = _manifest_profiles(
                    root, raw=raw, native_agents=native_agents,
                    catalog_sha256=catalog_sha256,
                )
            except (OSError, ValueError) as exc:
                return {"present": True, "path": path, "profiles": [], "sha256": None,
                        "error": str(exc)}
            manifest_sha256 = catalog_sha256
            return {"present": True, "path": path, "profiles": profiles,
                    "sha256": manifest_sha256, "error": None}
    return {"present": False, "path": None, "profiles": [], "sha256": None, "error": None}


def discover_project_agents(cwd: str | None) -> list[dict[str, Any]]:
    """Return only explicitly curated profiles from the project manifest."""
    catalog = project_agent_catalog(cwd)
    if catalog["error"]:
        return []
    return catalog["profiles"]


def ensure_team_guide(workspace: str | None) -> bool:
    """Create cwh-agent.md once in a local team workspace; never overwrite it."""
    if not workspace or not os.path.isdir(workspace):
        return False
    path = os.path.join(workspace, GUIDE_NAME)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        return False
    except OSError:
        log.warning("could not create team guide %s", path, exc_info=True)
        return False
    st = os.fstat(fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(TEAM_GUIDE)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        try:
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino):
                os.unlink(path)
        except OSError:
            pass
        log.warning("could not finish creating team guide %s", path, exc_info=True)
        return False
    return True
