from __future__ import annotations

import re
import time

from .base import AgentState, Harness

WORKING_RE = re.compile(r"esc\s+to\s+interrupt", re.IGNORECASE)
COMPLETION_RE = re.compile(r"\u00b7\s*done\s+\d", re.IGNORECASE)
COMPLETION_RE_LINE = re.compile(r"^.*\u00b7\s*done\s+\d.*$", re.IGNORECASE | re.MULTILINE)
HEADER_MARKER = "claude code v"
FOOTER_MARKER = "shift+tab to cycle"
# The footer is rewritten while background shells/agents run, e.g.
# ``⏵⏵ auto mode on · 1 shell · ← for agents · ↓ to manage`` (no "shift+tab").
FOOTER_RE = re.compile(
    r"shift\+tab to cycle|for agents|to manage|for shortcuts|esc to interrupt"
    r"|auto mode on|accept edits on|plan mode on|bypass permissions on",
    re.IGNORECASE,
)


class ClaudeCodeHarness(Harness):
    """Adapter for the Claude Code interactive TUI.

    Observable signals (validated against Claude Code 2.1.284 at 120x42):

    - TUI mounted: footer ``⏵⏵ auto mode on (shift+tab to cycle) · ← for agents``
      plus the ``❯`` input box (the header scrolls away, so it is not required).
    - READY: mounted, no prompt sent yet and no completed turn on screen
      (the input box ``❯`` is available).
    - WORKING: the footer contains ``esc to interrupt``
      (``⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents``).
    - WAITING_INPUT: mounted, not working, and a work cycle is known to have
      finished. Completion is evidenced either by observing WORKING at least
      once, or by the status line ``✻ … · done 4:04 PM`` appearing when it was
      not already present when the prompt was submitted.
    - EXITED / ERROR: derived from the InteractiveSession process state.
    - UNKNOWN: prompt submitted but no completion evidence yet.

    Note: a workspace that has not been trusted shows a separate trust dialog
    instead of this TUI. That dialog is intentionally NOT auto-accepted (it
    would modify the user's Claude configuration), so the harness reports
    STARTING/UNKNOWN until the workspace is trusted by the user.
    """

    kind = "claude"
    supports_history = True
    uses_hooks = True
    supports_task_files = True
    mcp_supported = True
    dangerous_flags = ("--dangerously-skip-permissions",)
    # The new session id itself arrives with the SessionStart hook.
    new_session_commands = ("/clear", "/new", "/reset")

    # ESC is deliberately not used to clear input: on an empty Claude input
    # two ESCs open the "Rewind" selector, which swallows the next text+ENTER
    # (the message looks delivered but never becomes a turn). Ctrl+U kills the
    # line and has no such side effect.
    clear_input_keys = ("CTRL_U",)
    unclearable_input_is_ghost = True

    def __init__(self, session, *, name: str | None = None) -> None:
        super().__init__(session, name=name)
        self._seen_working = False
        self._sent_marker = ""
        self._completion_count_at_send = 0
        self._last_done_at_send = ""
        self._sent_at = 0.0
        self._stop_at = 0.0

    @classmethod
    def command(cls) -> list[str]:
        return ["claude"]

    @classmethod
    def launch_args(
        cls,
        hooks_settings: str | None,
        conversation_id: str | None = None,
        port: int | None = None,
    ) -> list[str]:
        args = ["--settings", hooks_settings] if hooks_settings else []
        if conversation_id:
            args += ["--session-id", conversation_id]
        return args

    def _host_cfg(self) -> dict | None:
        """The configured SSH host of a remote agent (None for a local one)."""
        name = getattr(self.session.spec, "host", None)
        if not name:
            return None
        from .. import settings

        try:
            return settings.host(name)
        except settings.SettingsError:
            return None

    def history(self, limit: int = 200, before: int | None = None) -> dict:
        from ..transcripts import read_history

        if not self.conversation_id:
            return super().history(limit, before)
        return read_history(self.conversation_id, limit, before, host=self._host_cfg())

    @classmethod
    def mcp_config_file(cls) -> str | None:
        from ..mcp import config

        return config.write_claude_config()

    @classmethod
    def mcp_launch_args(cls, config_path: str | None) -> list[str]:
        return ["--mcp-config", config_path] if config_path else []

    def activity_snapshot(self) -> dict:
        from .. import activity

        return activity.claude_snapshot(self.conversation_id, host=self._host_cfg())

    @classmethod
    def model_from_screen(cls, text: str | None) -> str | None:
        from .. import activity

        return activity.claude_model(text)

    # Claude accepts ``/model <alias|name>`` mid-session; the aliases below are
    # stable across versions (the versions they resolve to are not).
    model_switch_mode = "direct"

    @classmethod
    def models(cls) -> list[str]:
        return ["default", "sonnet", "opus", "haiku", "opusplan", "fable", "best"]

    def config_risks(self) -> list[str]:
        import json
        import os

        path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return []
        risky = []
        if "bypassPermissions" in text:
            risky.append("Claude settings set bypassPermissions")
        try:
            data = json.loads(text)
        except ValueError:
            return risky
        mode = (data.get("permissions") or {}).get("defaultMode")
        if mode == "bypassPermissions":
            risky.append("Claude permissions.defaultMode = bypassPermissions")
        return risky

    def on_hook(self, event: str, at: float | None = None) -> None:
        """Claude Code hook events: ``prompt_submit`` and ``stop``.

        Exact signals that complement the screen heuristics: a turn started
        (``prompt_submit``) and the agent finished responding (``stop``).
        """
        at = at if at is not None else time.time()
        if event == "prompt_submit":
            self._seen_working = True
        elif event == "stop":
            self._seen_working = True
            self._stop_at = at

    def start(self, timeout: float = 30.0) -> AgentState:
        state = super().start(timeout=timeout)
        if state.usable:
            self._settle()
            state = self.state()
        return state

    def ensure_ready(self, timeout: float = 30.0) -> AgentState:
        state = super().ensure_ready(timeout=timeout)
        if state.usable:
            self._settle()
            state = self.state()
        return state

    def accept_workspace_trust(self, timeout: float = 15.0) -> bool:
        """Accept Claude's folder-trust dialog for a crewhall-owned chat workspace.

        Only chat agents call this: their cwd is an isolated folder crewhall
        created, so trusting it is safe. The dialog highlights "No, exit"; we
        move down to "Yes, I trust this folder" and confirm.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            low = self.session.capture().lower()
            if "trust this folder" in low or "accessing workspace" in low:
                self.session.send_key("DOWN")
                time.sleep(0.2)
                self.session.send_key("ENTER")
                return True
            if "shift+tab to cycle" in low or "esc to interrupt" in low:
                return False  # already past the dialog
            time.sleep(0.3)
        return False

    def _settle(self, quiet: float = 0.8, timeout: float = 10.0) -> None:
        """Wait until the screen stops changing (startup notices settle)."""
        deadline = time.monotonic() + timeout
        last = self.session.capture()
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.15)
            current = self.session.capture()
            if current != last:
                last = current
                stable_since = time.monotonic()
                continue
            if time.monotonic() - stable_since >= quiet:
                return

    def send(self, prompt: str, timeout: float = 30.0) -> None:
        super().send(prompt, timeout=timeout)
        self._sent_marker = prompt.strip()[:12]
        self._sent_at = time.time()
        self._confirm_submit(prompt)
        done = COMPLETION_RE_LINE.findall(self.session.capture())
        self._completion_count_at_send = len(done)
        self._last_done_at_send = done[-1] if done else ""
        self._seen_working = False

    def _input_line(self) -> str:
        lines = [
            line
            for line in self.session.capture().splitlines()
            if line.lstrip().startswith("\u276f")
        ]
        return lines[-1] if lines else ""

    def input_line(self) -> str:
        """Content of the Claude input box, excluding its placeholder tip."""
        raw = self._input_line().lstrip("\u276f").strip("\xa0 ").strip()
        # A fresh Claude shows a rotating placeholder like: Try "write a test".
        if raw.startswith("Try "):
            return ""
        return raw

    def transcript(self) -> str:
        """Capture with Claude's own input box/footer stripped.

        The bottom of the screen is:
            ❯ <input…>
            ────────────────────────────
              ⏵⏵ auto mode on (shift+tab to cycle) · …
        We cut from the last horizontal rule that precedes the ``❯`` input line
        so the Web UI can draw a single input of its own.
        """
        lines = self.session.capture().splitlines()
        idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].lstrip().startswith("\u276f"):
                idx = i
                break
        if idx is None:
            return self.session.capture()
        # Walk up past a horizontal rule and any blank lines above the input.
        cut = idx
        j = idx - 1
        while j >= 0 and (not lines[j].strip() or set(lines[j].strip()) <= {"\u2500"}):
            cut = j
            j -= 1
        return "\n".join(lines[:cut]).rstrip("\n")

    def _confirm_submit(self, prompt: str, window: float = 2.5) -> None:
        marker = prompt.strip()[:12]
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            line = self._input_line()
            if not marker or marker not in line:
                return
            time.sleep(0.2)
        if marker and marker in self._input_line():
            self.session.send_enter()

    def _turn_completed(self, text: str) -> bool:
        """True when the turn we started has finished (evidence on screen).

        Preferred, position-based: a completion line *after* the echo of the
        prompt we sent (the echo is a ``❯`` line above the live input box).
        Counting ``· done`` lines alone breaks once older ones scroll off the
        visible screen, so when the echo itself is not visible we compare the
        last completion line with the one seen when the prompt was submitted
        (it changes when a new turn finishes), falling back to the count.
        """
        marker = self._sent_marker
        lines = text.splitlines()
        prompt_idx = [
            i for i, line in enumerate(lines) if line.lstrip().startswith("\u276f")
        ]
        history = prompt_idx[:-1]  # the last ❯ line is the live input box
        echo = [i for i in history if marker and marker in lines[i]]
        if echo:
            return any(COMPLETION_RE.search(line) for line in lines[echo[-1] + 1:])
        done = COMPLETION_RE_LINE.findall(text)
        if not done:
            return False
        return (
            done[-1] != self._last_done_at_send
            or len(done) > self._completion_count_at_send
        )

    @staticmethod
    def _footer_present(text: str) -> bool:
        """Claude's status footer under the input box (its text varies)."""
        tail = [line for line in text.splitlines() if line.strip()][-4:]
        return any(FOOTER_RE.search(line) for line in tail)

    def _detect(self, text: str) -> tuple[AgentState, str]:
        low = text.lower()
        if WORKING_RE.search(low):
            self._seen_working = True
            return AgentState.WORKING, "footer shows 'esc to interrupt'"
        # Footer + input box. The header is not required: it scrolls away once
        # the conversation grows and would wrongly read as "not mounted".
        mounted = "\u276f" in text and self._footer_present(text)
        if not mounted:
            return AgentState.STARTING, "Claude Code TUI not mounted yet"
        if not self._prompt_sent:
            if COMPLETION_RE.search(text):
                return AgentState.WAITING_INPUT, "TUI idle (completed turn visible)"
            return AgentState.READY, "input box available"
        if (
            self._seen_working
            or (self._stop_at and self._stop_at >= self._sent_at)
            or self._turn_completed(text)
        ):
            return AgentState.WAITING_INPUT, "TUI idle after a completed work cycle"
        return AgentState.UNKNOWN, "prompt sent, no completion evidence yet"
