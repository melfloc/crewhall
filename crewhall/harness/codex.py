from __future__ import annotations

import re

from .base import AgentState, Harness

# Verified against Codex CLI 0.158.0 (see ADAPTERS.md §8).
WORKING_RE = re.compile(r"Working\s*\([^)]*esc to interrupt", re.IGNORECASE)
PLACEHOLDER = "ask codex to do anything"
TRUST_MARKERS = ("trust this folder", "is this a project you created", "do you trust")
MODEL_FOOTER_RE = re.compile(r"^\s*([A-Za-z0-9][\w.+-]*)\s+(?:[a-z]+\s+)?[\u00b7-]\s+\S", re.MULTILINE)


class CodexHarness(Harness):
    """Adapter for the Codex CLI interactive TUI.

    Observable signals (ADAPTERS.md §8, Codex 0.158.0):

    - TUI mounted: input box ``› …`` plus the footer ``… for shortcuts`` /
      ``← for agents``.
    - READY: the placeholder ``› Ask Codex to do anything`` is visible and no
      turn has completed yet.
    - WORKING: ``• Working (Ns • esc to interrupt)`` with the braille spinner.
    - WAITING_INPUT: a work cycle is known to have finished and the placeholder
      is back.
    - EXITED / ERROR: derived from the InteractiveSession process state.
    - A folder-trust dialog is intentionally NOT auto-accepted: the TUI stays
      STARTING until a person resolves it.

    The exact per-turn signal comes from the rollout JSONL (``task_started`` /
    ``task_complete``); the screen is the fallback.
    """

    kind = "codex"
    supports_history = True
    mcp_supported = True
    interrupt_keys = ("ESC",)
    new_session_commands = ("/new",)
    dangerous_flags = (
        "--dangerously-bypass-approvals-and-sandbox", "--full-auto", "--yolo", "-a never",
    )

    def __init__(self, session, *, name: str | None = None) -> None:
        super().__init__(session, name=name)
        self._seen_working = False

    @classmethod
    def command(cls) -> list[str]:
        return ["codex"]

    @classmethod
    def mcp_launch_args(cls, config_path: str | None) -> list[str]:
        from ..mcp import config

        return config.codex_args()

    def _cwd(self) -> str | None:
        try:
            return self.session.info().cwd
        except Exception:  # noqa: BLE001
            return None

    def history(self, limit: int = 200, before: int | None = None) -> dict:
        from .. import codex_rollout

        return codex_rollout.read(self.conversation_id, cwd=self._cwd(), limit=limit)

    def activity_snapshot(self) -> dict:
        from .. import codex_rollout

        snap = codex_rollout.latest_signal(self.conversation_id, cwd=self._cwd())
        if not snap:
            return {}
        return {"model": snap.get("model"),
                "tool": None,
                "last_event": snap.get("last_event")}

    # Codex opens its own picker with ``/model`` (model + reasoning effort).
    model_switch_mode = "picker"

    def model_switch(self, model: str | None = None) -> list[str]:
        return ["/model"]

    @classmethod
    def model_from_screen(cls, text: str | None) -> str | None:
        for line in reversed((text or "").splitlines()):
            if "·" in line and ("for shortcuts" in line or "for agents" in line or "/" in line):
                m = MODEL_FOOTER_RE.match(line)
                if m:
                    return m.group(1).strip()
        m = MODEL_FOOTER_RE.search(text or "")
        return m.group(1).strip() if m else None

    def input_line(self) -> str:
        for line in reversed(self.session.capture().splitlines()):
            m = re.match(r"^\s*›\s?(.*)$", line)
            if m:
                text = m.group(1).strip()
                return "" if text.lower().startswith("ask codex") else text
        return ""

    def transcript(self) -> str:
        """Capture with the input box (from the last ``›`` line) removed."""
        lines = self.session.capture().splitlines()
        for i in range(len(lines) - 1, -1, -1):
            if re.match(r"^\s*›", lines[i]):
                return "\n".join(lines[:i]).rstrip("\n")
        return self.session.capture()

    def config_risks(self) -> list[str]:
        import os

        path = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return []
        risks: list[str] = []
        low = text.lower().replace(" ", "")
        if 'approval_policy="never"' in low or "approval_policy='never'" in low:
            risks.append("Codex approval_policy = \"never\" (commands run without asking)")
        if 'sandbox_mode="danger-full-access"' in low or "danger-full-access" in low:
            risks.append("Codex sandbox is fully open (danger-full-access)")
        return risks

    def _detect(self, text: str) -> tuple[AgentState, str]:
        low = text.lower()
        if any(marker in low for marker in TRUST_MARKERS):
            return AgentState.STARTING, "folder trust dialog (waiting for a person)"
        if WORKING_RE.search(text):
            self._seen_working = True
            return AgentState.WORKING, "'Working … esc to interrupt' on screen"
        placeholder = PLACEHOLDER in low
        mounted = placeholder or "for shortcuts" in low or "for agents" in low
        if not mounted:
            return AgentState.STARTING, "Codex TUI not mounted yet"
        if self._prompt_sent:
            if self._seen_working and placeholder:
                return AgentState.WAITING_INPUT, "TUI idle after a completed work cycle"
            return AgentState.UNKNOWN, "prompt sent, no completion evidence yet"
        if placeholder:
            return AgentState.READY, "input box shows the placeholder"
        return AgentState.UNKNOWN, "TUI mounted, no readiness marker"
