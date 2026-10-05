from __future__ import annotations

import unittest

from crewhall.harness import ClaudeCodeHarness, CodexHarness, OpenCodeHarness

from . import adapter_contract


class ClaudeContract(adapter_contract.AdapterContract):
    harness_cls = ClaudeCodeHarness
    kind = "claude"


class OpenCodeContract(adapter_contract.AdapterContract):
    harness_cls = OpenCodeHarness
    kind = "opencode"


class CodexContract(adapter_contract.AdapterContract):
    harness_cls = CodexHarness
    kind = "codex"


if __name__ == "__main__":
    unittest.main()
