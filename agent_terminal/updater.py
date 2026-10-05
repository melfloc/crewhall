"""Update a *managed* installation to a verified release, safely.

A managed install is the layout ``scripts/install.sh`` creates (``<prefix>/venv`` plus an
``install.json`` marker). Development checkouts are never updated this way: the office
machine only ever runs releases built from tagged commits of the main line.

Steps (any failure leaves the running installation untouched):

 1. fetch the release directory (local dir or http(s) base) and verify sha256 + signature;
 2. refuse downgrades / same version (unless forced);
 3. build ``venv.new`` from the wheel and smoke-test it;
 4. back up config + state (a ``bundle``) to ``<state>/backups``;
 5. swap atomically: ``venv`` -> ``venv.prev``, ``venv.new`` -> ``venv``;
 6. optionally restart the daemon (never implicit: restarting closes running agents),
    health-check it and roll back automatically if it does not come up healthy.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from typing import Any
from collections.abc import Callable

from . import __version__, paths, signing
from .buildinfo import build_info

MANIFEST = "release.json"
KEEP_BACKUPS = 5


def _keep() -> int:
    try:
        from . import settings

        return max(1, int(settings.get("maintenance.keep_backups")))
    except Exception:  # noqa: BLE001
        return KEEP_BACKUPS


class UpdateError(RuntimeError):
    pass


class RestartRefused(UpdateError):
    """A restart was requested but would close running agents. A *refusal*, never a failure:
    it must not trigger a rollback or any daemon restart."""


Log = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# -- versions ----------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", text or "")
    if not m:
        raise UpdateError(f"unparseable version {text!r}")
    return tuple(int(x) for x in m.groups())


# -- layout ------------------------------------------------------------------
def install_root() -> str | None:
    """``<prefix>`` if the running interpreter belongs to a managed install."""
    venv = os.path.abspath(sys.prefix)
    if os.path.basename(venv) != "venv":
        return None
    prefix = os.path.dirname(venv)
    return prefix if os.path.isfile(os.path.join(prefix, "install.json")) else None


def _require_managed() -> str:
    prefix = install_root()
    if not prefix:
        raise UpdateError(
            "this is not a managed installation (development checkout or hand-made venv). "
            "Releases are installed with install.sh; the dev machine is updated with git, "
            "not with `update`."
        )
    return prefix


def read_install(prefix: str) -> dict[str, Any]:
    try:
        with open(os.path.join(prefix, "install.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_install(prefix: str, data: dict[str, Any]) -> None:
    tmp = os.path.join(prefix, "install.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, os.path.join(prefix, "install.json"))


class _Lock:
    def __init__(self, prefix: str) -> None:
        self.path = os.path.join(prefix, "update.lock")

    def __enter__(self) -> "_Lock":
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UpdateError("another update is in progress") from None
        return self

    def __exit__(self, *exc: object) -> None:
        self.fd.close()


# -- fetching + verification ---------------------------------------------------
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(base: str, name: str, dest: str, required: bool = True) -> bool:
    url = base.rstrip("/") + "/" + name
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(os.path.join(dest, name), "wb") as out:
            shutil.copyfileobj(resp, out)
        return True
    except OSError as exc:
        if required:
            raise UpdateError(f"cannot download {url}: {exc}") from exc
        return False


def fetch(source: str, workdir: str) -> str:
    """Return a local directory holding release.json + tarball (+ .sig)."""
    if re.match(r"^https?://", source):
        _download(source, MANIFEST, workdir)
        manifest = json.load(open(os.path.join(workdir, MANIFEST), encoding="utf-8"))
        _download(source, manifest["tarball"], workdir)
        _download(source, manifest["tarball"] + ".sig", workdir, required=False)
        return workdir
    path = os.path.abspath(os.path.expanduser(source))
    if not os.path.isdir(path) or not os.path.isfile(os.path.join(path, MANIFEST)):
        raise UpdateError(f"{source}: not a release directory (no {MANIFEST})")
    return path


def verify_release(directory: str) -> dict[str, Any]:
    try:
        manifest = json.load(open(os.path.join(directory, MANIFEST), encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"bad {MANIFEST}: {exc}") from exc
    for key in ("version", "tarball", "sha256"):
        if key not in manifest:
            raise UpdateError(f"{MANIFEST} lacks '{key}'")
    tarball = os.path.join(directory, os.path.basename(manifest["tarball"]))
    if not os.path.isfile(tarball):
        raise UpdateError(f"release tarball missing: {manifest['tarball']}")
    if _sha256(tarball) != manifest["sha256"]:
        raise UpdateError("sha256 mismatch: the release is corrupted or was tampered with")
    signers = os.path.join(_config_dir(), "allowed_signers")
    if os.path.isfile(signers):
        try:
            signing.verify(tarball, tarball + ".sig", signers)
        except signing.SignatureError as exc:
            raise UpdateError(str(exc)) from exc
        manifest["_signature"] = "verified"
    else:
        manifest["_signature"] = "unsigned (no trusted signer configured: integrity check only)"
    manifest["_tarball_path"] = tarball
    return manifest


def _config_dir() -> str:
    from .bundle import config_dir

    return config_dir()


# -- building the new environment ---------------------------------------------
def _run(cmd: list[str], what: str, timeout: int = 300) -> subprocess.CompletedProcess:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"{what}: {exc}") from exc
    if out.returncode != 0:
        raise UpdateError(f"{what} failed: {(out.stderr or out.stdout).strip()[-400:]}")
    return out


def _extract_wheel(tarball: str, dest: str) -> str:
    with tarfile.open(tarball, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile() and m.name.endswith(".whl")]
        if len(members) != 1:
            raise UpdateError("the release tarball must contain exactly one wheel")
        member = members[0]
        member.name = os.path.basename(member.name)  # no path tricks
        tar.extract(member, dest)
        return os.path.join(dest, member.name)


def _build_env(prefix: str, wheel: str, version: str) -> str:
    new = os.path.join(prefix, "venv.new")
    shutil.rmtree(new, ignore_errors=True)
    _run([sys.executable, "-m", "venv", new], "creating the new virtualenv")
    py = os.path.join(new, "bin", "python")
    _run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-deps", wheel],
         "installing the wheel")
    # By module, never through bin/crewhall: that script's shebang names this staging path.
    ver = _run([py, "-P", "-m", "agent_terminal", "--version"], "smoke test (--version)").stdout
    if version not in ver:
        raise UpdateError(f"smoke test: new build reports {ver.strip()!r}, expected {version}")
    _run([py, "-P", "-c", "import agent_terminal.daemon, agent_terminal.web.server, agent_terminal.doctor, "
                    "agent_terminal.updater"], "smoke test (imports)")
    return new


# -- state backup ---------------------------------------------------------------
def backup_before_update() -> str | None:
    from . import bundle

    d = os.path.join(paths.state_dir(), "backups")
    os.makedirs(d, mode=0o700, exist_ok=True)
    dest = os.path.join(d, f"pre-update-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    out = bundle.export_bundle(dest, with_state=True)
    for name in sorted(n for n in os.listdir(d) if n.startswith("pre-update-"))[:-_keep()]:
        try:
            os.unlink(os.path.join(d, name))
        except OSError:
            pass
    return dest if out["files"] else None


# -- daemon restart + health ---------------------------------------------------
def _running_agents() -> list[str]:
    from .client import Client, ping

    if not ping():
        return []
    try:
        agents = Client(autostart=False).call("agent_list")["agents"]
    except Exception:  # noqa: BLE001
        return []
    return [a["name"] or a["agent_id"] for a in agents if a["state"] not in ("exited", "error")]


def daemon_version() -> str | None:
    from .client import Client, ping

    try:
        if not ping():
            return None
        return Client(autostart=False).call("ping").get("version") or "unknown (older build)"
    except Exception:  # noqa: BLE001
        return None


def wait_daemon_gone(timeout: float = 45.0) -> None:
    """Wait until the old daemon has really finished (it keeps its lock while closing agents/frontends).

    The socket disappears first; starting the new daemon in that window makes it exit with
    "another daemon already holds the lock"."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(paths.lock_path(), "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise UpdateError("the old daemon is still shutting down (lock held)") from None
            time.sleep(0.2)


def check_restart_allowed(force: bool) -> None:
    running = _running_agents()
    if running and not force:
        raise RestartRefused(
            f"{len(running)} agent(s) are running ({', '.join(running[:5])}): restarting the daemon would "
            "close them. Wait until they are idle, or pass --force-restart."
        )


def restart_daemon(prefix: str, expect_version: str, *, force: bool, log: Log) -> None:
    from .client import Client, ensure_daemon, ping

    check_restart_allowed(force)
    if ping():
        log("stopping the daemon…")
        try:
            Client(autostart=False).call("shutdown")
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + 20
        while ping() and time.monotonic() < deadline:
            time.sleep(0.2)
        if ping():
            raise UpdateError("the old daemon did not stop")
        wait_daemon_gone()
    log("starting the new daemon…")
    ensure_daemon(start_timeout=30.0)
    got = daemon_version()
    if got != expect_version:
        raise UpdateError(f"daemon reports version {got!r}, expected {expect_version}")
    health = subprocess.run([os.path.join(prefix, "venv", "bin", "python"), "-P", "-m", "agent_terminal", "doctor", "--json"],
                            capture_output=True, text=True, timeout=120)
    if health.returncode != 0:
        raise UpdateError("post-restart health check (doctor) reported problems")


# -- public operations ----------------------------------------------------------
def check(source: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="at-upd-") as work:
        manifest = verify_release(fetch(source, work))
    return {"current": __version__, "available": manifest["version"],
            "newer": parse_version(manifest["version"]) > parse_version(__version__),
            "commit": manifest.get("commit"), "signature": manifest["_signature"]}


def apply(source: str, *, restart: bool = False, force: bool = False,
          allow_downgrade: bool = False, force_restart: bool = False, log: Log = _noop) -> dict[str, Any]:
    prefix = _require_managed()
    if restart:  # refuse BEFORE changing anything: a refused restart must leave the machine exactly as it was
        check_restart_allowed(force_restart)
    with _Lock(prefix), tempfile.TemporaryDirectory(prefix="at-upd-") as work:
        log(f"fetching {source}")
        manifest = verify_release(fetch(source, work))
        log(f"verified sha256; signature: {manifest['_signature']}")
        new_v, cur_v = parse_version(manifest["version"]), parse_version(__version__)
        if new_v == cur_v and not force:
            out: dict[str, Any] = {"status": "up-to-date", "version": __version__}
            if restart and daemon_version() not in (None, __version__):
                out["restart"] = restart_pending(force_restart=force_restart, log=log)
            return out
        if new_v < cur_v and not allow_downgrade:
            raise UpdateError(f"{manifest['version']} is older than the installed {__version__} "
                              "(use `update --rollback`, or --allow-downgrade)")
        py_min = manifest.get("min_python")
        if py_min and tuple(sys.version_info[:2]) < tuple(int(x) for x in py_min.split(".")):
            raise UpdateError(f"this release needs Python >= {py_min}")
        wheel = _extract_wheel(manifest["_tarball_path"], work)
        log("building and testing the new environment…")
        new_env = _build_env(prefix, wheel, manifest["version"])
        backup = backup_before_update()
        if backup:
            log(f"backed up config+state to {backup}")
        venv, prev = os.path.join(prefix, "venv"), os.path.join(prefix, "venv.prev")
        shutil.rmtree(prev, ignore_errors=True)
        os.rename(venv, prev)
        os.rename(new_env, venv)  # same filesystem: atomic enough, and reversible below
        fix_shebangs(venv, os.path.basename(new_env))
        record = read_install(prefix)
        write_install(prefix, {
            **record, "version": manifest["version"], "commit": manifest.get("commit"),
            "state_schema": manifest.get("state_schema"), "previous_version": __version__,
            "previous_state_schema": record.get("state_schema"),
            "previous_commit": build_info().get("commit"), "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        try:  # the *swapped* environment is what will actually run: prove it, or undo (record included)
            _post_swap_check(prefix, manifest["version"])
        except UpdateError as exc:
            _swap_back(prefix)
            raise UpdateError(f"the new environment failed after the swap; rolled back: {exc}") from exc
        log(f"installed {manifest['version']} (previous {__version__} kept for rollback)")
        result: dict[str, Any] = {"status": "updated", "from": __version__, "to": manifest["version"],
                                  "backup": backup, "restarted": False}
        if restart:
            try:
                restart_daemon(prefix, manifest["version"], force=force_restart, log=log)
                result["restarted"] = True
            except RestartRefused as exc:  # agents appeared meanwhile: keep the install, do not touch the daemon
                result["restart_refused"] = str(exc)
                result["restart_pending"] = True
            except UpdateError as exc:
                log(f"restart failed ({exc}); rolling back")
                _swap_back(prefix)
                try:
                    restart_daemon(prefix, __version__, force=True, log=log)
                except UpdateError:
                    pass
                raise UpdateError(f"update rolled back to {__version__}: {exc}") from exc
        else:
            result["restart_pending"] = bool(daemon_version() and daemon_version() != manifest["version"])
        return result


def fix_shebangs(venv: str, staged_name: str) -> None:
    """Console scripts embed the path of the dir the venv was *created* in; after the rename
    that is stale. Rewrite ``.../<staged_name>/...`` to ``.../venv/...`` in bin/ scripts."""
    old, new = f"/{staged_name}/", "/venv/"
    bindir = os.path.join(venv, "bin")
    if not os.path.isdir(bindir):
        return
    for name in os.listdir(bindir):
        path = os.path.join(bindir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                head = fh.read(4)
                if head != b"#!/u" and head[:2] != b"#!":
                    continue
                data = head + fh.read()
        except OSError:
            continue
        first, _, rest = data.partition(b"\n")
        if old.encode() in first:
            with open(path, "wb") as fh:
                fh.write(first.replace(old.encode(), new.encode()) + b"\n" + rest)


def _post_swap_check(prefix: str, version: str) -> None:
    py = os.path.join(prefix, "venv", "bin", "python")
    out = _run([py, "-P", "-m", "agent_terminal", "--version"], "post-swap check (--version)", timeout=60)
    if version not in out.stdout:
        raise UpdateError(f"post-swap check: reports {out.stdout.strip()!r}, expected {version}")
    script = os.path.join(prefix, "venv", "bin", "crewhall")
    if os.path.exists(script):
        _run([script, "--version"], "post-swap check (console script)", timeout=60)


def _swap_back(prefix: str) -> None:
    venv, prev, tmp = (os.path.join(prefix, n) for n in ("venv", "venv.prev", "venv.swap"))
    shutil.rmtree(tmp, ignore_errors=True)
    os.rename(venv, tmp)
    os.rename(prev, venv)
    os.rename(tmp, prev)
    record = read_install(prefix)
    write_install(prefix, {**record, "version": record.get("previous_version"),
                           "commit": record.get("previous_commit"),
                           "state_schema": record.get("previous_state_schema"),
                           "previous_version": record.get("version"),
                           "previous_commit": record.get("commit"),
                           "previous_state_schema": record.get("state_schema")})


def restart_pending(*, force_restart: bool = False, log: Log = _noop) -> dict[str, Any]:
    """Apply an already-installed update: restart the daemon on the installed code (when idle)."""
    prefix = _require_managed()
    check_restart_allowed(force_restart)
    dv = daemon_version()
    if dv == __version__:
        return {"status": "nothing-pending", "version": __version__}
    restart_daemon(prefix, __version__, force=force_restart, log=log)
    return {"status": "restarted", "version": __version__, "was": dv}


def rollback(*, restart: bool = False, force: bool = False, force_restart: bool = False,
             log: Log = _noop) -> dict[str, Any]:
    prefix = _require_managed()
    if restart:
        check_restart_allowed(force_restart)
    with _Lock(prefix):
        if not os.path.isdir(os.path.join(prefix, "venv.prev")):
            raise UpdateError("there is no previous version to roll back to")
        record = read_install(prefix)
        target = record.get("previous_version")
        # The state file may have been written by a newer schema than the old code understands.
        try:
            state_schema = json.load(open(os.path.join(paths.state_dir(), "state.json"))).get("schema", 1)
        except (OSError, ValueError):
            state_schema = 1
        prev_schema = (record.get("previous_state_schema") or record.get("state_schema") or state_schema)
        if state_schema > prev_schema and not force:
            raise UpdateError("the state file uses a newer schema than the previous version understands "
                              "(restore a pre-update backup first, or use --force)")
        _swap_back(prefix)
        log(f"rolled back to {target}")
        result: dict[str, Any] = {"status": "rolled-back", "to": target, "restarted": False}
        if restart:
            restart_daemon(prefix, target or "", force=force_restart, log=log)
            result["restarted"] = True
        return result


def status() -> dict[str, Any]:
    prefix = install_root()
    info = build_info()
    out: dict[str, Any] = {"managed": bool(prefix), "version": __version__, "channel": info["kind"],
                           "commit": info.get("commit")}
    if prefix:
        rec = read_install(prefix)
        out.update(previous=rec.get("previous_version"), installed_at=rec.get("installed_at"),
                   rollback_available=os.path.isdir(os.path.join(prefix, "venv.prev")))
    dv = daemon_version()
    out["daemon_version"] = dv
    out["restart_pending"] = bool(dv and dv != __version__)
    out["signers_configured"] = os.path.isfile(os.path.join(_config_dir(), "allowed_signers"))
    return out
