"""Per-conversation storage: inputs, outputs and the artifacts a chat produces.

A conversation lives in ``<conversations.dir>/<id>/`` with two owned sections:
``inputs/`` (files the user uploaded) and ``outputs/`` (artifacts the agent
produced). Metadata lives in a small JSON store under the state directory, kept
separate from ``persistence.StateStore`` (which is deliberately limited to
teams/agents/requests/terminals).

The module is used both by the daemon (ops) and by the web layer (upload and
artifact serving): both run on the same host and share ``state_dir()``. It never
executes a file and refuses any path that escapes the section it owns.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from typing import Any

from . import paths, settings

# An artifact/input name is reduced to a safe basename; an oversized extension
# is not an extension.
MAX_NAME = 120
MAX_EXT = 16
_BAD = re.compile(r"[\x00-\x1f\x7f/\\]")
_VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SECTIONS = ("inputs", "outputs")

# Extension -> coarse kind the Web UI uses to pick a previewer.
_KINDS: dict[str, str] = {
    # OnlyOffice (editable) — word
    ".docx": "office", ".doc": "office", ".odt": "office", ".rtf": "office",
    ".txt": "office", ".md": "office",
    # OnlyOffice — cell
    ".xlsx": "office", ".xls": "office", ".ods": "office", ".csv": "office",
    # OnlyOffice — slide
    ".pptx": "office", ".ppt": "office", ".odp": "office",
    # Read-only document
    ".pdf": "pdf",
    # Images
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".svg": "image", ".bmp": "image", ".avif": "image",
    # Text / code
    ".json": "text", ".yaml": "text", ".yml": "text", ".toml": "text",
    ".ini": "text", ".cfg": "text", ".log": "text", ".py": "text",
    ".js": "text", ".ts": "text", ".tsx": "text", ".jsx": "text",
    ".sh": "text", ".css": "text", ".html": "text", ".xml": "text",
    # Archives
    ".zip": "archive", ".tar": "archive", ".gz": "archive", ".tgz": "archive",
    ".bz2": "archive", ".xz": "archive", ".7z": "archive",
}

# Extensions the OnlyOffice viewer can open.
OFFICE_EXTENSIONS = {
    ".docx", ".doc", ".odt", ".rtf", ".txt", ".md",
    ".xlsx", ".xls", ".ods", ".csv",
    ".pptx", ".ppt", ".odp",
}


class ConversationError(RuntimeError):
    pass


_LOCK = threading.RLock()


def _meta_path() -> str:
    return os.path.join(paths.state_dir(), "conversations.json")


def _load() -> dict[str, Any]:
    try:
        with open(_meta_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"conversations": {}}
    if not isinstance(data, dict) or not isinstance(data.get("conversations"), dict):
        return {"conversations": {}}
    return data


def _save(data: dict[str, Any]) -> None:
    path = _meta_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ConversationError(str(exc)) from exc


def enabled() -> bool:
    try:
        return bool(settings.get("conversations.enabled"))
    except KeyError:
        return True


def _root(create: bool = True) -> str:
    raw = str(settings.get("conversations.dir") or "").strip()
    directory = os.path.expanduser(raw) if raw else os.path.join(paths.state_dir(), "conversations")
    if not os.path.isabs(directory):
        raise ConversationError("the conversations directory must be absolute")
    if create:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    return directory


def root() -> str:
    return _root(create=True)


def max_bytes() -> int:
    try:
        return max(1, int(settings.get("conversations.max_mb"))) * 1024 * 1024
    except (TypeError, ValueError):
        return 200 * 1024 * 1024


def _valid_id(conversation_id: str) -> str:
    cid = str(conversation_id or "")
    if not _VALID_ID.match(cid):
        raise ConversationError("invalid conversation id")
    return cid


def conversation_dir(conversation_id: str, create: bool = False) -> str:
    cid = _valid_id(conversation_id)
    directory = os.path.join(_root(create=create), cid)
    if create:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        for section in (*SECTIONS, "workspace"):
            os.makedirs(os.path.join(directory, section), mode=0o700, exist_ok=True)
    return directory


def _section_dir(conversation_id: str, section: str, create: bool = True) -> str:
    if section not in SECTIONS:
        raise ConversationError("unknown section")
    return os.path.join(conversation_dir(conversation_id, create=create), section)


def _clean_name(name: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _BAD.sub("", base).strip(". ") or "file"
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem or len(ext) > MAX_EXT or not ext.isalnum():
        stem, ext = base, ""
    stem = stem[:MAX_NAME] or "file"
    return f"{stem}.{ext}" if ext else stem


def kind_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return _KINDS.get(ext, "other")


def is_office(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in OFFICE_EXTENSIONS


def _unique(directory: str, name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for _ in range(5):
        suffix = secrets.token_hex(4)
        candidate = os.path.join(directory, f"{stem}-{suffix}{'.' + ext if ext else ''}")
        if not os.path.exists(candidate):
            return candidate
    raise ConversationError("could not find a free name")


def _write_new(directory: str, name: str, data: bytes) -> dict[str, Any]:
    clean = _clean_name(name)
    path = _unique(directory, clean)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except OSError as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise ConversationError(str(exc)) from exc
    return {"name": os.path.basename(path), "size": len(data),
            "kind": kind_of(path), "path": path}


def _atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ConversationError(str(exc)) from exc


# -- metadata -----------------------------------------------------------------

# Rules the chat agent reads: how to work with the isolated folders and how to
# interact with the OnlyOffice editor so its changes appear live.
RULES = """# crewhall · chat (agente aislado)

Estás en un **chat aislado** de crewhall. Tu directorio de trabajo es esta
carpeta; no leas ni escribas fuera de la carpeta de la conversación.

## Carpetas (variables de entorno)
- `$CREWHALL_INPUTS`  — lo que sube el usuario.
- `$CREWHALL_OUTPUTS` — tus **artefactos** (lo que el usuario ve y descarga).
- `$CREWHALL_WORKSPACE` — scratch (aquí).

Cuando el usuario pida crear/editar un documento, escríbelo en
`$CREWHALL_OUTPUTS` (o edítalo por OnlyOffice, ver abajo).

## Editar en vivo con OnlyOffice
El usuario puede tener un artefacto **abierto en el editor**. Para que tus
cambios se vean **en vivo** (sin recargar), NO edites el archivo directamente:
usa el co-editor. Abre primero (idempotente) y luego edita:

    crewhall office open    --conversation "$CREWHALL_CONVERSATION" --path informe.docx
    crewhall office insert  --conversation "$CREWHALL_CONVERSATION" --path informe.docx --text "…"
    crewhall office read    --conversation "$CREWHALL_CONVERSATION" --path informe.docx
    crewhall office comment --conversation "$CREWHALL_CONVERSATION" --path informe.docx --text "…"
    crewhall office comments--conversation "$CREWHALL_CONVERSATION" --path informe.docx
    crewhall office save    --conversation "$CREWHALL_CONVERSATION" --path informe.docx

`insert` guarda solo (el usuario lo ve al instante). Si `office open` falla
porque OnlyOffice no está disponible, edita el archivo en `$CREWHALL_OUTPUTS`
con tus herramientas normales.

## Responder en el chat del documento
Si recibes un mensaje como `[OnlyOffice chat · usuario · informe.docx] …`, el
usuario te escribió **dentro del editor**. Responde con:

    crewhall office say --conversation "$CREWHALL_CONVERSATION" --path informe.docx --text "…"

Los **comentarios** anclados llegan como `[OnlyOffice comentario · …] sobre
"texto": …`; haz el cambio pedido sobre ese texto y responde por el chat o con
un comentario.
"""


def ensure_rules(directory: str) -> None:
    """Write the chat rules (CLAUDE.md / AGENTS.md) into a directory, idempotent."""
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    if fh.read() == RULES:
                        continue
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(RULES)
            os.chmod(path, 0o600)
        except OSError:
            pass


def ensure_global_rules() -> None:
    """The global chat directory carries the interaction rules; each chat's
    workspace gets its own copy so the agent reads them as CLAUDE.md/AGENTS.md."""
    try:
        ensure_rules(_root(create=True))
    except (OSError, ConversationError):
        return
    for meta in list_all():
        try:
            ensure_rules(workspace(meta["id"]))
        except (OSError, ConversationError):
            continue


def create(title: str = "", agent_id: str | None = None, host: str | None = None) -> dict[str, Any]:
    cid = "c-" + secrets.token_hex(8)
    now = time.time()
    meta = {
        "id": cid,
        "title": (str(title).strip()[:200] or "New chat"),
        "agent_id": agent_id or None,
        "host": host or os.uname().nodename,
        "created_at": now,
        "updated_at": now,
    }
    conversation_dir(cid, create=True)
    ensure_rules(workspace(cid))
    ensure_global_rules()
    with _LOCK:
        data = _load()
        data["conversations"][cid] = meta
        _save(data)
    return meta


def get(conversation_id: str) -> dict[str, Any]:
    cid = _valid_id(conversation_id)
    data = _load()
    meta = data["conversations"].get(cid)
    if not meta:
        raise ConversationError("conversation not found")
    return dict(meta)


def set_agent(conversation_id: str, agent_id: str | None) -> dict[str, Any]:
    cid = _valid_id(conversation_id)
    with _LOCK:
        data = _load()
        meta = data["conversations"].get(cid)
        if not meta:
            raise ConversationError("conversation not found")
        meta["agent_id"] = agent_id or None
        meta["updated_at"] = time.time()
        _save(data)
        return dict(meta)


def rename(conversation_id: str, title: str) -> dict[str, Any]:
    cid = _valid_id(conversation_id)
    new_title = str(title).strip()[:200]
    if not new_title:
        raise ConversationError("a title is required")
    with _LOCK:
        data = _load()
        meta = data["conversations"].get(cid)
        if not meta:
            raise ConversationError("conversation not found")
        meta["title"] = new_title
        meta["updated_at"] = time.time()
        _save(data)
        return dict(meta)


def touch(conversation_id: str) -> None:
    try:
        cid = _valid_id(conversation_id)
    except ConversationError:
        return
    with _LOCK:
        data = _load()
        meta = data["conversations"].get(cid)
        if not meta:
            return
        meta["updated_at"] = time.time()
        _save(data)


def delete(conversation_id: str, delete_files: bool = False) -> dict[str, Any]:
    import shutil

    cid = _valid_id(conversation_id)
    with _LOCK:
        data = _load()
        meta = data["conversations"].pop(cid, None)
        if not meta:
            raise ConversationError("conversation not found")
        _save(data)
    removed_files = False
    if delete_files:
        directory = os.path.join(_root(create=False), cid)
        real_root = os.path.realpath(_root(create=False))
        if os.path.realpath(directory).startswith(real_root + os.sep) and os.path.isdir(directory):
            shutil.rmtree(directory, ignore_errors=True)
            removed_files = True
    return {"deleted": True, "files_removed": removed_files}


def list_all() -> list[dict[str, Any]]:
    data = _load()
    items = [dict(m) for m in data["conversations"].values()]
    items.sort(key=lambda m: m.get("updated_at") or 0, reverse=True)
    return items


def agent_ids() -> set[str]:
    """The agents that belong to a chat conversation (isolated from Cowork)."""
    return {str(m.get("agent_id")) for m in _load()["conversations"].values()
            if m.get("agent_id")}


# -- files --------------------------------------------------------------------

def store_input(conversation_id: str, name: str, data: bytes) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise ConversationError("no data")
    if len(data) > max_bytes():
        raise ConversationError("file too large")
    conversation_dir(conversation_id, create=True)
    info = _write_new(_section_dir(conversation_id, "inputs"), name, bytes(data))
    touch(conversation_id)
    return info


def store_artifact(conversation_id: str, name: str, data: bytes) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise ConversationError("no data")
    if len(data) > max_bytes():
        raise ConversationError("file too large")
    conversation_dir(conversation_id, create=True)
    info = _write_new(_section_dir(conversation_id, "outputs"), name, bytes(data))
    touch(conversation_id)
    return info


def resolve(conversation_id: str, section: str, relpath: str) -> str:
    """Absolute path for a file inside a section, or raise. No escape allowed."""
    if section not in SECTIONS:
        raise ConversationError("unknown section")
    raw = str(relpath or "")
    if raw.startswith(("/", "\\")):
        raise ConversationError("invalid path")
    rel = raw.replace("\\", "/")
    if not rel or rel.endswith("/") or "\0" in rel:
        raise ConversationError("invalid path")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ConversationError("invalid path")
    base = os.path.realpath(_section_dir(conversation_id, section, create=True))
    candidate = os.path.realpath(os.path.join(base, *parts))
    if candidate != base and not candidate.startswith(base + os.sep):
        raise ConversationError("path escapes the conversation")
    return candidate


def write_artifact(conversation_id: str, relpath: str, data: bytes) -> dict[str, Any]:
    """Overwrite an artifact in place (used by the OnlyOffice callback)."""
    if len(data) > max_bytes():
        raise ConversationError("file too large")
    path = resolve(conversation_id, "outputs", relpath)
    real_out = os.path.realpath(_section_dir(conversation_id, "outputs", create=True))
    if os.path.realpath(os.path.dirname(path)) == real_out or path.startswith(real_out + os.sep):
        _atomic_write(path, data)
        touch(conversation_id)
        return {"name": os.path.basename(path), "size": len(data),
                "relpath": os.path.relpath(path, real_out)}
    raise ConversationError("path escapes the conversation")


def list_files(conversation_id: str, section: str) -> list[dict[str, Any]]:
    directory = _section_dir(conversation_id, section, create=False)
    if not os.path.isdir(directory):
        return []
    base = os.path.realpath(directory)
    out: list[dict[str, Any]] = []
    for root, _dirs, files in os.walk(base):
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, base)
            out.append({
                "name": name, "relpath": rel, "size": st.st_size,
                "mtime": st.st_mtime, "kind": kind_of(name),
                "office": is_office(name),
            })
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


def info(conversation_id: str) -> dict[str, Any]:
    meta = get(conversation_id)
    meta = dict(meta)
    meta["inputs"] = list_files(conversation_id, "inputs")
    meta["outputs"] = list_files(conversation_id, "outputs")
    return meta


def delete_artifact(conversation_id: str, relpath: str) -> dict[str, Any]:
    path = resolve(conversation_id, "outputs", relpath)
    if not os.path.isfile(path) and not os.path.islink(path):
        raise ConversationError("artifact not found")
    os.unlink(path)
    touch(conversation_id)
    return {"deleted": True, "name": os.path.basename(path)}


def workspace(conversation_id: str, create: bool = True) -> str:
    """The isolated working directory of a conversation's agent.

    A chat agent always runs here: it never touches the user's projects nor the
    daemon's cwd, so a conversation leaves nothing outside its own folder.
    """
    path = os.path.join(conversation_dir(conversation_id, create=create), "workspace")
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def env(conversation_id: str) -> dict[str, str]:
    directory = conversation_dir(conversation_id, create=True)
    ensure_rules(workspace(conversation_id))
    return {
        "CREWHALL_CONVERSATION": conversation_id,
        "CREWHALL_CONVERSATION_DIR": directory,
        "CREWHALL_INPUTS": os.path.join(directory, "inputs"),
        "CREWHALL_OUTPUTS": os.path.join(directory, "outputs"),
        "CREWHALL_WORKSPACE": workspace(conversation_id),
    }
