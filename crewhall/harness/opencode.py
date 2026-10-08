from __future__ import annotations

import re
import time

from .base import AgentState, Harness

WORKING_RE = re.compile(r"esc\s+interrupt", re.IGNORECASE)
# One ``Build · <model> · 4.3s`` line is appended to the transcript per completed
# turn, so counting matches tells us how many turns have finished (robust across
# turns even when the agent is not polled while working).
COMPLETION_RE = re.compile(r"\u00b7\s*\d+(?:\.\d+)?s\b")
MOUNT_MARKERS = ("ctrl+p", "build ·", "build -")
# Session view footer: context tokens, e.g. ``13.1K (1%) · $0.00``. The home
# screen (no session yet, e.g. right after ``/new``) shows the version instead.
# Composer footer: ``<Agent> · <model> [provider]`` (``Build · …`` by default).
FOOTER_LINE_RE = re.compile(r"^[\w][\w .-]{0,40} [\u00b7-] \S")
TOKENS_RE = re.compile(r"\d+(?:\.\d+)?[KM]?\s+\(\d+%\)")
READY_PLACEHOLDER = "ask anything"


class OpenCodeHarness(Harness):
    """Adapter for the OpenCode interactive TUI.

    Observable signals (validated against opencode 1.18.31 at 120x40):

    - TUI mounted: footer contains "ctrl+p commands" (also "Build · <model>").
    - READY: mounted and the input placeholder "Ask anything…" is visible
      (only before the first prompt is sent).
    - WORKING: the status line matches ``esc\\s+interrupt``
      (e.g. ``■■■■⬝⬝⬝⬝  esc interrupt   tab agents  ctrl+p commands``).
    - WAITING_INPUT: mounted, not working, and a work cycle is known to have
      finished. Completion is evidenced either by observing WORKING at least
      once, or by the token/cost footer appearing (``12.6K (1%) · $0.00``)
      when it was not already present when the prompt was submitted.
    - EXITED / ERROR: derived from the InteractiveSession process state.
    - UNKNOWN: prompt submitted but no completion evidence yet (we prefer
      UNKNOWN to guessing that work finished).
    """

    kind = "opencode"
    supports_history = True
    uses_local_server = True
    interrupt_keys = ("ESC", "ESC")  # "esc again to interrupt"

    def __init__(self, session, *, name: str | None = None) -> None:
        super().__init__(session, name=name)
        self._seen_working = False
        self._completion_count_at_send = 0
        self._sent_at = 0.0
        self._stop_at = 0.0

    @classmethod
    def command(cls) -> list[str]:
        return ["opencode"]

    @classmethod
    def launch_args(
        cls,
        hooks_settings: str | None,
        conversation_id: str | None = None,
        port: int | None = None,
    ) -> list[str]:
        # Local server (127.0.0.1, password-protected): exact events + history.
        return ["--port", str(port)] if port else []

    def on_hook(self, event: str, at: float | None = None) -> None:
        """Events from OpenCode's server: ``prompt_submit`` (busy) / ``stop`` (idle)."""
        at = at if at is not None else time.time()
        if event == "prompt_submit":
            self._seen_working = True
        elif event == "stop":
            self._seen_working = True
            self._stop_at = at

    # Set by the controller: finds the session when the creation event was missed.
    conversation_resolver = None

    # ``/clear`` is an alias of ``/new`` in OpenCode.
    new_session_commands = ("/new", "/clear")

    def on_home_screen(self, text: str | None = None) -> bool:
        """True when the TUI shows its start screen (no session open)."""
        text = self.session.capture() if text is None else text
        low = text.lower()
        return (READY_PLACEHOLDER in low and not TOKENS_RE.search(text)
                and not WORKING_RE.search(low) and not COMPLETION_RE.search(text))

    def history(self, limit: int = 200, before: int | None = None) -> dict:
        if self.link and not self.conversation_id and self.conversation_resolver:
            self.conversation_id = self.conversation_resolver()
        if not (self.link and self.conversation_id):
            return super().history(limit, before)
        data = self.link.history(self.conversation_id, limit, before)
        # ``/new`` typed straight into the TUI (not through crewhall): the start
        # screen shows while the tracked conversation still has messages, so we
        # left it.  A start screen over an *empty* conversation is just the fresh
        # session crewhall's own ``/new`` created — retiring it here blacklisted
        # the new conversation and left the Conversation view empty forever, even
        # as the Live view kept showing activity (OpenCode >= 1.18.34 creates the
        # empty session before the first prompt).
        if (self.session.status.alive and int(data.get("total") or 0) > 0
                and self.on_home_screen()):
            self._new_session_started()
            if self.conversation_resolver:
                self.conversation_resolver()
                if self.conversation_id:
                    return self.link.history(self.conversation_id, limit, before)
            return super().history(limit, before)
        return data

    def activity_snapshot(self) -> dict:
        from .. import activity

        return activity.opencode_snapshot(self.link, self.conversation_id)

    @classmethod
    def model_from_screen(cls, text: str | None) -> str | None:
        from .. import activity

        return activity.opencode_model(text)

    def usage_from_screen(self, text: str) -> dict:
        from .. import usage

        return usage.opencode_usage(text)

    def running_shells(self, root: int | None = None) -> dict:
        link, conv = self.link, self.conversation_id
        if link is None or not conv:
            return {}
        try:
            return link.running_tools(conv)
        except Exception:  # noqa: BLE001 - the server may be busy/restarting
            return {}

    def send(self, prompt: str, timeout: float = 30.0) -> None:
        super().send(prompt, timeout=timeout)
        self._completion_count_at_send = len(
            COMPLETION_RE.findall(self.session.capture())
        )
        self._seen_working = False
        self._sent_at = time.time()

    def input_line(self) -> str:
        """Content of the OpenCode composer line (the ``┃`` block), if any.

        The composer is the contiguous ``┃`` block closed by the ``╹▀▀▀`` rule;
        its last line is the agent/model footer (``Build · <model>``, or the
        agent's own name with ``--agent``, e.g. ``Ejecutor · <model>``). Earlier
        ``┃`` blocks are echoes of past messages and are never input. A fresh
        OpenCode shows a rotating placeholder (``Ask anything… "..."``): empty.
        """
        lines = self.session.capture().splitlines()

        def rail(line: str) -> bool:
            return line.strip().startswith("\u2503")

        def text(line: str) -> str:
            return line.split("\u2503", 1)[1].strip()

        close = next((i for i in range(len(lines) - 1, -1, -1)
                      if lines[i].lstrip().startswith("\u2579")), None)
        if close is not None:
            start = close
            while start > 0 and rail(lines[start - 1]):
                start -= 1
            block = [text(line) for line in lines[start:close]]
            while block and not block[-1]:
                block.pop()
            block = block[:-1]  # the agent/model footer
        else:  # no closing rule visible: the last rail block, footer by its shape
            rails = [i for i, line in enumerate(lines) if rail(line)]
            if not rails:
                return ""
            end = rails[-1]
            start = end
            while start > 0 and rail(lines[start - 1]):
                start -= 1
            block = [text(line) for line in lines[start:end + 1]]
            block = [t for t in block if not FOOTER_LINE_RE.match(t)]
        for content in block:
            if not content:
                continue
            if content.lower().startswith("ask anything"):
                return ""
            return content
        return ""

    def transcript(self) -> str:
        """Capture with OpenCode's own composer box/footer stripped.

        The bottom of the screen is a ``┃`` rail box (input + model line) closed
        by a ``╹▀▀▀`` rule, followed by hints and the status line. We cut from
        the first ``┃`` rail line that begins the composer (the last contiguous
        rail block) so the Web UI can draw a single input of its own.
        """
        lines = self.session.capture().splitlines()
        rail = "\u2503"  # ┃
        close = "\u2579"  # ╹ — the rule that closes the composer box
        # The composer is only present when its closing rule (╹▀▀▀) is on screen.
        # If it is not (e.g. the agent is WORKING), return the full capture
        # rather than guessing and cutting the transcript away.
        close_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].lstrip().startswith(close):
                close_idx = i
                break
        if close_idx is None:
            return self.session.capture()
        # Cut from the first rail line of the composer block that ends at close.
        first = close_idx
        j = close_idx - 1
        while j >= 0 and (lines[j].lstrip().startswith(rail) or not lines[j].strip()):
            first = j
            j -= 1
        if first <= 0:
            return self.session.capture()
        return "\n".join(lines[:first]).rstrip("\n")

    def _detect(self, text: str) -> tuple[AgentState, str]:
        low = text.lower()
        if WORKING_RE.search(low):
            self._seen_working = True
            return AgentState.WORKING, "status line shows 'esc interrupt'"
        mounted = any(marker in low for marker in MOUNT_MARKERS)
        if not mounted:
            return AgentState.STARTING, "opencode TUI not mounted yet"
        completed_turns = len(COMPLETION_RE.findall(text))
        if not self._prompt_sent:
            # A mounted session with context (the token/cost footer) has already
            # run at least one turn: idle, not unknown. This survives a daemon
            # restart, where the in-memory completion signals are gone and the
            # per-turn ``· Ns`` line may have scrolled out of the captured text.
            if completed_turns > 0 or TOKENS_RE.search(text):
                return AgentState.WAITING_INPUT, "TUI idle (completed turn visible)"
            if READY_PLACEHOLDER in low:
                return AgentState.READY, "input placeholder 'Ask anything' visible"
            return AgentState.UNKNOWN, "TUI mounted, no readiness marker"
        if (
            self._seen_working
            or (self._stop_at and self._stop_at >= self._sent_at)
            or completed_turns > self._completion_count_at_send
        ):
            return AgentState.WAITING_INPUT, "TUI idle after a completed work cycle"
        return AgentState.UNKNOWN, "prompt sent, no completion evidence yet"
