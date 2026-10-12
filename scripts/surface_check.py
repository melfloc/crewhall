#!/usr/bin/env python3
"""Compare the public surface of a refactor against a git revision.

A split must not change what the code *does*, but it also must not change what
it *exposes*. This tool dumps three descriptions of the API from a checkout and
compares them between the current working tree and an arbitrary ``<ref>``:

* the method set of ``crewhall.web.server.Handler`` (with signatures), resolved
  across its MRO so mixins count as part of the handler;
* the method set of ``crewhall.ui.model.AppModel`` (with signatures), likewise;
* the HTTP routes served by ``Handler.do_GET`` / ``Handler.do_POST``;
* the argparse tree of ``crewhall.cli.build_parser``.

Usage::

    python scripts/surface_check.py <ref>

``<ref>`` is anything ``git rev-parse`` understands (a commit, ``HEAD~1``, a
tag). The revision is checked out into a throw-away ``git worktree`` and the
dump runs in a subprocess, so the two sides never share imports. Exit code is
non-zero when the surfaces differ (a diff is printed), and zero when they are
identical. ``--dump``/``--root`` are internal and used by that subprocess.
"""
from __future__ import annotations

import argparse
import ast
import inspect
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from typing import Any

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _method_surface(cls: type, prefix: str) -> dict[str, str]:
    """Methods (and properties) defined under ``prefix`` anywhere in the MRO."""
    out: dict[str, str] = {}
    for base in cls.__mro__:
        for name, obj in vars(base).items():
            module = getattr(obj, "__module__", "")
            if not (isinstance(module, str) and module.startswith(prefix)):
                continue
            if isinstance(obj, property):
                out.setdefault(name, "property")
            elif isinstance(obj, (staticmethod, classmethod)):
                out.setdefault(name, str(inspect.signature(obj.__func__)))
            elif inspect.isfunction(obj):
                out.setdefault(name, str(inspect.signature(obj)))
    return out


def _is_path_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "path" or node.id.endswith("_path")
    if isinstance(node, ast.Attribute):
        return node.attr == "path" or node.attr.endswith("_path")
    return False


def _string_constants(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out: list[str] = []
        for element in node.elts:
            out.extend(_string_constants(element))
        return out
    return []


def _routes_of(fn: Any) -> dict[str, list[str]]:
    """String-literal routes compared against ``path`` in a handler method."""
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return {"exact": [], "prefix": []}
    tree = ast.parse(source)
    exact: set[str] = set()
    prefixes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and _is_path_expr(node.left):
            for operator, comparator in zip(node.ops, node.comparators, strict=False):
                if isinstance(operator, (ast.Eq, ast.In)):
                    exact.update(_string_constants(comparator))
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "startswith"
                    and _is_path_expr(func.value)):
                prefixes.update(_string_constants(node.args[0]))
    return {"exact": sorted(exact), "prefix": sorted(prefixes)}


def _arg_choices(action: argparse.Action) -> list[str] | None:
    choices = getattr(action, "choices", None)
    if isinstance(choices, (list, tuple, set, frozenset)):
        return sorted(str(c) for c in choices)
    return None


def _argparse_tree(parser: argparse.ArgumentParser) -> dict[str, Any]:
    options: list[dict[str, Any]] = []
    subcommands: dict[str, Any] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                subcommands[name] = _argparse_tree(sub)
        else:
            entry: dict[str, Any] = {
                "options": list(action.option_strings),
                "dest": action.dest,
                "nargs": action.nargs if isinstance(action.nargs, (int, str)) else None,
                "required": bool(getattr(action, "required", False)),
            }
            choices = _arg_choices(action)
            if choices is not None:
                entry["choices"] = choices
            options.append(entry)
    options.sort(key=lambda e: (e["options"], e["dest"]))
    return {"options": options, "subcommands": subcommands}


def collect() -> dict[str, Any]:
    from crewhall.ui import model as ui_model
    from crewhall.web import server as web_server
    from crewhall import cli

    handler = web_server.Handler
    return {
        "handler_methods": _method_surface(handler, "crewhall.web"),
        "appmodel_methods": _method_surface(ui_model.AppModel, "crewhall.ui"),
        "do_GET": _routes_of(handler.do_GET),
        "do_POST": _routes_of(handler.do_POST),
        "argparse": _argparse_tree(cli.build_parser()),
    }


def _emit(root: str | None) -> None:
    if root:
        sys.path.insert(0, root)
    json.dump(collect(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _diff(a: Any, b: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            child = f"{path}.{key}" if path else key
            if key not in a:
                out.append(f"{child}: only in ref")
            elif key not in b:
                out.append(f"{child}: only in current")
            else:
                out.extend(_diff(a[key], b[key], child))
    elif a != b:
        out.append(f"{path}: current={a!r} ref={b!r}")
    return out


def _dump_in(root: str) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--dump", "--root", root],
        cwd=root, env=env, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"surface dump failed for {root} (rc={proc.returncode})")
    return json.loads(proc.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ref", nargs="?", help="git revision to compare against")
    parser.add_argument("--dump", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.dump:
        _emit(args.root)
        return 0
    if not args.ref:
        parser.error("a git ref is required (or use --dump)")

    current = _dump_in(REPO)
    worktree = tempfile.mkdtemp(prefix="at-surface-")
    os.rmdir(worktree)
    added = subprocess.run(
        ["git", "-C", REPO, "worktree", "add", "--detach", worktree, args.ref],
        capture_output=True, text=True,
    )
    if added.returncode != 0:
        sys.stderr.write(added.stderr)
        return 2
    try:
        ref = _dump_in(worktree)
    finally:
        subprocess.run(["git", "-C", REPO, "worktree", "remove", "--force", worktree],
                       capture_output=True, text=True)

    differences = _diff(current, ref)
    if differences:
        print(f"surface differs from {args.ref}:")
        for line in differences:
            print("  " + line)
        return 1
    print(f"surface identical to {args.ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
