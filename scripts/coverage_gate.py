#!/usr/bin/env python3
"""Coverage gate (Fase C2).

Reads the combined coverage data and fails when any threshold is not met:

* 80% global;
* 90% on ``crewhall/messaging.py`` and ``crewhall/conversations.py``;
* 90% on the specific functions ``daemon._guard_origin`` and
  ``controller.publish_artifact`` (line coverage of their bodies).

Run after ``coverage run -m unittest discover`` + ``coverage combine``.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass

from coverage import CoverageData
from coverage.parser import PythonParser


@dataclass
class Target:
    label: str
    path: str
    threshold: float
    func: str | None = None

    def statements(self, resolved: str) -> set[int]:
        parser = PythonParser(filename=resolved)
        parser.parse_source()
        lines = set(parser.statements)
        if self.func is None:
            return lines
        return lines & _function_lines(resolved, self.func)


def _function_lines(path: str, name: str) -> set[int]:
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    raise SystemExit(f"function {name!r} not found in {path}")


TARGETS = [
    Target("messaging", "crewhall/messaging.py", 90.0),
    Target("conversations", "crewhall/chat/__init__.py", 90.0),
    Target("daemon._guard_origin", "crewhall/daemon.py", 90.0, func="_guard_origin"),
    Target("controller.publish_artifact", "crewhall/services/chat_bridge.py", 90.0, func="publish_artifact"),
]
GLOBAL_THRESHOLD = 80.0


def _pct(covered: int, total: int) -> float:
    return 100.0 if total == 0 else 100.0 * covered / total


def main() -> int:
    data = CoverageData()
    data.read()
    measured = list(data.measured_files())

    def resolve(rel: str) -> str:
        rel_norm = os.path.normpath(rel)
        for path in measured:
            if os.path.normpath(path).endswith(os.sep + rel_norm):
                return path
        return rel

    failed = False
    total_stmts = total_cov = 0
    print(f"{'target':<32} {'cover':>7}  {'threshold':>9}")
    for target in TARGETS:
        resolved = resolve(target.path)
        statements = target.statements(resolved)
        covered = statements & set(data.lines(resolved) or [])
        pct = _pct(len(covered), len(statements))
        ok = pct + 1e-9 >= target.threshold
        failed |= not ok
        print(f"{target.label:<32} {pct:6.1f}%  {target.threshold:8.1f}%  {'ok' if ok else 'FAIL'}")

    for path in measured:
        norm = os.path.normpath(path)
        if not norm.endswith(".py") or f"{os.sep}crewhall{os.sep}" not in norm:
            continue
        parser = PythonParser(filename=path)
        parser.parse_source()
        statements = set(parser.statements)
        total_stmts += len(statements)
        total_cov += len(statements & set(data.lines(path) or []))
    global_pct = _pct(total_cov, total_stmts)
    ok = global_pct + 1e-9 >= GLOBAL_THRESHOLD
    failed |= not ok
    print(f"{'GLOBAL':<32} {global_pct:6.1f}%  {GLOBAL_THRESHOLD:8.1f}%  {'ok' if ok else 'FAIL'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
