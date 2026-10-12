"""Health journal and long-duration state (``state/health/``).

Long-running installations are observed over days: every ten minutes the daemon
appends one sample (memory, threads, queue depth, failures of the last hour,
restarted workers and versions) to ``journal.jsonl``. ``crewhall doctor --trend``
summarises the last 30 days. The same directory holds the cached CLI versions
(layer 1: ``crewhall watch versions``) and the nightly smoke results.

Everything here is best effort: a health file that cannot be written must never
break the daemon or a command.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

from . import __version__, paths
import contextlib

JOURNAL = "journal.jsonl"
VERSIONS = "versions.json"


def health_dir() -> str:
    """``state/health`` (created 0700 on demand)."""
    path = os.path.join(paths.state_dir(), "health")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def journal_path() -> str:
    return os.path.join(health_dir(), JOURNAL)


def versions_path() -> str:
    return os.path.join(health_dir(), VERSIONS)


def smoke_path(date: str | None = None) -> str:
    date = date or time.strftime("%Y-%m-%d")
    return os.path.join(health_dir(), f"smoke-{date}.json")


def _atomic_write_json(path: str, data: Any) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".health-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


# -- cached CLI versions (layer 1) --------------------------------------------

def read_versions(path: str | None = None) -> dict[str, Any]:
    try:
        with open(path or versions_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_versions(versions: dict[str, Any], path: str | None = None) -> None:
    _atomic_write_json(path or versions_path(),
                       {"versions": versions, "checked_at": time.time()})


def write_json(path: str, data: Any) -> None:
    """Atomically write a 0600 JSON file (used for smoke/soak results)."""
    _atomic_write_json(path, data)


def smoke_result(total: int, failed: list[str]) -> dict[str, Any]:
    """Build the nightly smoke outcome (layer 3)."""
    return {
        "date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "passed": total - len(failed),
        "failed": list(failed),
        "ok": not failed,
    }


# -- journal (layer 5) --------------------------------------------------------

def _memory_kb() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _failures_last_hour(now: float, path: str | None = None) -> int:
    """Error results recorded by the audit log in the last hour (best effort)."""
    from . import audit

    target = path or audit.audit_path()
    count = 0
    try:
        with open(target, encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                try:
                    ts = float(record.get("ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                if now - ts <= 3600 and record.get("result") == "error":
                    count += 1
    except OSError:
        return 0
    return count


def sample(controller: Any = None, *, now: float | None = None,
           audit_path: str | None = None) -> dict[str, Any]:
    """Build one journal sample from the live controller (all fields optional)."""
    now = time.time() if now is None else now
    data: dict[str, Any] = {
        "ts": now,
        "rst": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "version": __version__,
        "memory_kb": _memory_kb(),
        "threads": _threads(),
        "queue": None,
        "failures_last_hour": _failures_last_hour(now, audit_path),
        "workers_restarted": None,
        "versions": read_versions().get("versions", {}),
    }
    if controller is not None:
        with contextlib.suppress(Exception):  # health must never break the daemon
            data["queue"] = controller.messaging.queue_depth()
        with contextlib.suppress(Exception):  # noqa: BLE001
            snapshot = controller.workers.snapshot()
            data["workers_restarted"] = sum(int(w.get("restarts", 0)) for w in snapshot)
    return data


def _threads() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def append_sample(data: dict[str, Any] | None = None, *, controller: Any = None,
                  path: str | None = None) -> dict[str, Any]:
    """Append one sample to the journal; never raises."""
    record = data if data is not None else sample(controller)
    target = path or journal_path()
    try:
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass
    return record


def read_journal(days: float = 30, *, path: str | None = None,
                 now: float | None = None) -> list[dict[str, Any]]:
    """Journal samples newer than ``days`` (all of them when ``days <= 0``)."""
    now = time.time() if now is None else now
    cutoff = 0.0 if days <= 0 else now - days * 86400.0
    records: list[dict[str, Any]] = []
    try:
        with open(path or journal_path(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                try:
                    ts = float(record.get("ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                if ts >= cutoff:
                    records.append(record)
    except OSError:
        return []
    records.sort(key=lambda r: float(r.get("ts") or 0.0))
    return records


def _stat(records: list[dict[str, Any]], key: str) -> dict[str, float] | None:
    values = [r[key] for r in records if isinstance(r.get(key), (int, float))]
    if not values:
        return None
    return {"min": min(values), "max": max(values), "last": values[-1], "n": len(values)}


def summarize_trend(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact summary of journal samples (for ``doctor --trend``)."""
    if not records:
        return {"samples": 0}
    versions = sorted({str(r.get("version")) for r in records if r.get("version")})
    failures = [int(r.get("failures_last_hour") or 0) for r in records]
    return {
        "samples": len(records),
        "since": records[0].get("rst") or records[0].get("ts"),
        "until": records[-1].get("rst") or records[-1].get("ts"),
        "memory_kb": _stat(records, "memory_kb"),
        "threads": _stat(records, "threads"),
        "queue": _stat(records, "queue"),
        "workers_restarted": _stat(records, "workers_restarted"),
        "failures_last_hour": {
            "max": max(failures) if failures else 0,
            "sum": sum(failures),
        },
        "versions": versions,
    }


def trend_report(days: float = 30, *, path: str | None = None) -> dict[str, Any]:
    return summarize_trend(read_journal(days, path=path))
