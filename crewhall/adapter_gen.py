"""Scaffold a new provider adapter (``crewhall adapter new <kind>``).

Creates ``harness/<kind>.py`` (not registered), its fixture directory and a test
that inherits the contract kit. Nothing is wired into ``HARNESSES``: the adapter
only becomes usable once its contract passes and a real run is documented
(ADAPTERS.md). Refuses to overwrite anything.
"""
from __future__ import annotations

import os
import re

KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

_HARNESS = '''from __future__ import annotations

from .base import AgentState, Harness


class {cls}Harness(Harness):
    """TODO: implement the {kind} adapter (see ADAPTERS.md).

    Fill ``_detect`` with evidence from the real TUI, add the optional
    capabilities this TUI supports, then register it in ``harness/__init__.py``
    and make the contract kit pass.
    """

    kind = "{kind}"

    @classmethod
    def command(cls) -> list[str]:
        return ["{kind}"]

    def _detect(self, text: str) -> tuple[AgentState, str]:
        # Never report READY/WORKING on a guess: use a positive on-screen marker.
        return AgentState.STARTING, "TUI not mounted yet"
'''

_TEST = '''from __future__ import annotations

import unittest

from crewhall.harness.{kind} import {cls}Harness

from . import adapter_contract


class {cls}Contract(adapter_contract.AdapterContract):
    harness_cls = {cls}Harness
    kind = "{kind}"


if __name__ == "__main__":
    unittest.main()
'''

_README = """# {kind} screen fixtures

Drop sanitized captures from the real TUI here (ADAPTERS.md §5), one file per
state, and list them in `expected.json`: `{{"<file>": {{"state": "...",
"evidence_contains": "..."}}}}`. Never commit real `$HOME` paths, emails,
tokens or account ids.
"""


def _pascal(kind: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[-_]", kind))


def create(kind: str, *, root: str | None = None) -> list[str]:
    """Create the adapter skeleton; returns the paths written (raises ValueError)."""
    if not KIND_RE.match(kind or ""):
        raise ValueError("kind must be a short lowercase identifier (a-z, 0-9, - or _)")
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    harness = os.path.join(root, "crewhall", "harness", f"{kind}.py")
    fixture = os.path.join(root, "tests", "fixtures", "screens", kind)
    test = os.path.join(root, "tests", f"test_{kind}_contract.py")
    for path in (harness, test):
        if os.path.exists(path):
            raise ValueError(f"{path} already exists; refusing to overwrite")
    if os.path.exists(fixture):
        raise ValueError(f"{fixture} already exists; refusing to overwrite")
    cls = _pascal(kind)
    os.makedirs(fixture, mode=0o755, exist_ok=True)
    with open(harness, "w", encoding="utf-8") as fh:
        fh.write(_HARNESS.format(cls=cls, kind=kind))
    with open(test, "w", encoding="utf-8") as fh:
        fh.write(_TEST.format(cls=cls, kind=kind))
    with open(os.path.join(fixture, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(_README.format(kind=kind))
    with open(os.path.join(fixture, "expected.json"), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    return [harness, test, os.path.join(fixture, "expected.json")]
