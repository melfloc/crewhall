#!/usr/bin/env python3
"""Regression script: how crewhall reacts to an OpenCode v1 -> v2 upgrade.

Run it from anywhere (the repo root is added to ``sys.path``)::

    python3 scripts/opencode_v2_switch_test.py          # sandboxed, safe
    python3 scripts/opencode_v2_switch_test.py --real   # report this machine

The sandboxed run fakes an ``opencode`` binary and a version-manager install so
the real one is never touched. It exercises three phases:

  1. **before the upgrade**: ``opencode`` is v1 -> in range, no switch, no notice.
  2. **after the upgrade**: ``opencode`` is v2 but a v1 install exists on disk ->
     crewhall switches to it *automatically* and records a notice (the Web UI
     shows it in a modal). This is the path the user was promised: no manual fix.
  3. **after the upgrade, nothing compatible left**: crewhall refuses to launch
     (blocked) and records a "not supported" notice.

With ``--real`` it only prints the verdict for the CLI actually installed here, so
it can be run before and after the real ``opencode`` update.

Exit code 0 = every check passed (including the version-gate unit tests).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

V1 = "1.18.34"
V2 = "2.0.0"
COMPATIBLE = "1.18.33"


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks += 1
        mark = "PASS" if ok else "FAIL"
        suffix = f"  — {detail}" if detail else ""
        print(f"  [{mark}] {label}{suffix}")
        if not ok:
            self.failures += 1
        return ok

    def section(self, title: str) -> None:
        print(f"\n{title}")


def _fake_opencode(directory: str, version: str) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "opencode")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"#!/bin/sh\necho {version}\n")
    os.chmod(path, 0o755)
    return path


def _short(report) -> str:
    return f"{report.command[0]} ({report.text or 'unknown'})"


def run_sandbox() -> int:
    from crewhall import Controller, notices, settings
    from crewhall.harness.versions import UnsupportedVersionError

    report = Report()
    sandbox = tempfile.mkdtemp(prefix="crewhall-v2-switch-")
    base_path = os.environ.get("PATH", "")
    try:
        config = os.path.join(sandbox, "config")
        state = os.path.join(sandbox, "state")
        data = os.path.join(sandbox, "data")
        mise = os.path.join(data, "mise", "installs", "opencode")
        v1_dir = os.path.join(sandbox, "bin-v1")
        v2_dir = os.path.join(sandbox, "bin-v2")
        _fake_opencode(v1_dir, V1)
        _fake_opencode(v2_dir, V2)
        compatible = _fake_opencode(os.path.join(mise, COMPATIBLE), COMPATIBLE)

        os.environ.update({
            "XDG_CONFIG_HOME": config,
            "XDG_STATE_HOME": state,
            "XDG_DATA_HOME": data,
            # Point auto-discovery at the sandbox so the host's real installs
            # (including the real `opencode`) never leak into the test.
            "CREWHALL_DISCOVERY_PATH": mise,
        })
        for var in ("CREWHALL_HOOKS", "CREWHALL_QUIET"):
            os.environ.pop(var, None)
        controller = Controller(adopt=False)

        def set_opencode(directory: str) -> None:
            os.environ["PATH"] = os.pathsep.join([directory, base_path])
            from crewhall.harness import versions

            versions._clear_probe_cache()

        # -- phase 1: before the upgrade (opencode is v1) -------------------
        report.section("Phase 1 — before updating OpenCode (opencode v1)")
        set_opencode(v1_dir)
        notices.clear()
        argv, eff, switched = settings.provider_effective_command("opencode")
        report.check(argv == ["opencode"], "uses the configured/PATH opencode", f"argv={argv}")
        report.check(switched is False, "no automatic switch needed")
        report.check(eff.supported is True, "version is inside the validated range",
                     f"opencode {eff.text} (range ends before 2.0.0)")
        controller.check_provider_versions()
        report.check(notices.pending() == [], "no notice raised")
        controller._enforce_version("opencode", eff)
        report.check(True, "launch gate allows it (no exception)")

        # -- phase 2: after the upgrade, a compatible install exists --------
        report.section("Phase 2 — after updating OpenCode to v2 (v1 still installed)")
        set_opencode(v2_dir)
        notices.clear()
        primary = settings.provider_effective_command("opencode")
        argv, eff, switched = primary
        report.check(switched is True, "switched to a compatible install automatically")
        report.check(argv == [compatible], "picked the v1 install on disk",
                     _short(eff))
        report.check(eff.supported is True, "the selected binary is in range",
                     f"opencode {eff.text}")
        controller.check_provider_versions()
        switch_notices = [n for n in notices.pending()
                          if n.get("kind") == "opencode" and n.get("key", "").startswith("switch:")]
        report.check(bool(switch_notices), "a switch notice was recorded")
        if switch_notices:
            report.check("switched automatically" in switch_notices[0]["title"],
                         "the modal explains it was automatic",
                         switch_notices[0]["title"])
        controller._enforce_version("opencode", eff)
        report.check(True, "launch gate allows the switched binary (no exception)")

        # -- phase 3: after the upgrade, nothing compatible left ------------
        report.section("Phase 3 — after updating to v2 with no compatible v1 left")
        shutil.rmtree(mise, ignore_errors=True)
        set_opencode(v2_dir)
        notices.clear()
        argv, eff, switched = settings.provider_effective_command("opencode")
        report.check(switched is False, "no candidate to switch to")
        report.check(eff.blocked is True, "v2 is detected as out of range (blocked)")
        controller.check_provider_versions()
        blocked = [n for n in notices.pending()
                   if n.get("kind") == "opencode" and n.get("key", "").startswith("blocked:")]
        report.check(bool(blocked), "a 'not supported' notice was recorded")
        try:
            controller._enforce_version("opencode", eff)
            report.check(False, "launch gate refuses an unsupported CLI")
        except UnsupportedVersionError as exc:
            report.check(True, "launch gate refuses an unsupported CLI", str(exc)[:60] + "…")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    report.section("Unit tests — tests/test_version_gate.py")
    if _run_unit_tests() == 0:
        report.check(True, "version-gate unit tests pass")
    else:
        report.check(False, "version-gate unit tests pass")

    print(f"\n{report.checks - report.failures}/{report.checks} checks passed.")
    return 1 if report.failures else 0


def _run_unit_tests() -> int:
    import logging
    import unittest

    from tests import test_version_gate

    suite = unittest.TestLoader().loadTestsFromModule(test_version_gate)
    with open(os.devnull, "w") as sink:
        logging.disable(logging.CRITICAL)
        try:
            result = unittest.TextTestRunner(verbosity=0, stream=sink).run(suite)
        finally:
            logging.disable(logging.NOTSET)
    return 0 if result.wasSuccessful() else 1


def report_real() -> int:
    from crewhall import settings

    print("Provider CLI verdict on this machine:\n")
    for kind in ("claude", "codex", "opencode"):
        try:
            argv, eff, switched = settings.provider_effective_command(kind)
        except Exception as exc:  # noqa: BLE001
            print(f"  {kind}: could not resolve ({exc})")
            continue
        if eff.supported is True:
            status = "in range"
        elif eff.blocked:
            status = "OUT OF RANGE / blocked"
        else:
            status = "unknown"
        extra = f" -> auto-switch to {_short(eff)}" if switched else ""
        print(f"  {kind}: primary {eff.text or '?'} ({status}){extra}")
    print(
        "\nBefore updating OpenCode you should see opencode 'in range'.\n"
        "After updating it to v2, crewhall should either auto-switch to a\n"
        "compatible install (shown above) or report 'OUT OF RANGE / blocked'."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--real", action="store_true",
                        help="report the CLI actually installed here (no sandbox)")
    args = parser.parse_args()
    print("crewhall — OpenCode v1/v2 version-gate regression")
    return report_real() if args.real else run_sandbox()


if __name__ == "__main__":
    raise SystemExit(main())
