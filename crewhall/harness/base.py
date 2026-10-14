from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from collections.abc import Callable, Iterable

from ..session import InteractiveSession

log = logging.getLogger("crewhall.harness")


def sgr_faint(params: str, faint: bool) -> bool:
    """Apply one SGR parameter list to the "faint" flag (2 on; 0/22 off).

    Extended colours (``38;5;n``, ``38;2;r;g;b`` and the 48/58 forms) carry
    numbers that are arguments, not attributes, so they are skipped.
    """
    codes = params.split(";") if params else ["0"]
    i = 0
    while i < len(codes):
        code = codes[i] or "0"
        if code in ("38", "48", "58"):
            mode = codes[i + 1] if i + 1 < len(codes) else ""
            i += 3 if mode == "5" else 5 if mode == "2" else 2
            continue
        if code in ("0", "22"):
            faint = False
        elif code == "2":
            faint = True
        i += 1
    return faint


def faint_first_visible(text: str) -> tuple[bool, str]:
    """``(is_faint, visible)`` for the first visible character of ``text``.

    Walks leading SGR escape sequences (and the spaces among them) and reports
    whether the first real character is drawn faint. Used by the adapters to tell
    a TUI's dim placeholder/suggestion apart from a real typed draft.
    """
    faint = False
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\x1b" and i + 1 < len(text) and text[i + 1] == "[":
            end = text.find("m", i)
            if end < 0:
                break
            faint = sgr_faint(text[i + 2:end], faint)
            i = end + 1
            continue
        if char in " \t\xa0\r\n":
            i += 1
            continue
        return faint, text[i:]
    return faint, ""


class AgentState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    WORKING = "working"
    WAITING_INPUT = "waiting_input"
    EXITED = "exited"
    ERROR = "error"
    UNKNOWN = "unknown"

    @property
    def usable(self) -> bool:
        return self in (AgentState.READY, AgentState.WAITING_INPUT)

    @property
    def terminal(self) -> bool:
        return self in (AgentState.EXITED, AgentState.ERROR)


class HarnessError(RuntimeError):
    def __init__(self, agent: str, message: str) -> None:
        super().__init__(f"agent {agent}: {message}")
        self.agent = agent


class HarnessNotReady(HarnessError):
    """The agent could not safely accept input *right now* (retryable).

    Raised instead of typing into a TUI that is not showing its normal input
    box (an overlay/dialog) or whose input still holds text we could not clear.
    """


@dataclass
class AgentInfo:
    agent_id: str
    name: str | None
    kind: str
    state: AgentState
    session_id: str
    backend: str
    pid: int | None
    created_at: float
    last_input_at: float | None
    last_output_at: float | None
    exited_at: float | None
    exit_code: int | None
    evidence: str
    cwd: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "kind": self.kind,
            "state": self.state.value,
            "session_id": self.session_id,
            "backend": self.backend,
            "pid": self.pid,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "last_input_at": self.last_input_at,
            "last_output_at": self.last_output_at,
            "exited_at": self.exited_at,
            "exit_code": self.exit_code,
            "evidence": self.evidence,
            "meta": self.meta,
        }


class Harness(ABC):
    """Semantic control over a CLI agent running inside an InteractiveSession.

    The harness knows what the agent *is* (command, ready/working markers,
    prompt semantics). It never talks to tmux or a PTY directly; it only uses
    the public InteractiveSession API (write/send_enter/capture/poll/...).
    """

    kind = "harness"

    # Keys used to clear residual input. Adapters override when a key has a
    # side effect on their TUI (see ClaudeCodeHarness).
    # ESC is never a clearing key: in Claude, OpenCode and Codex it is the
    # *interrupt* key. Sent to an agent that is (or is about to be) working it
    # arms "esc again to interrupt" and swallows the next keystrokes, so a
    # message typed right after lost its first characters ("[from: …" became
    # "rom: …"). Ctrl+U clears the input line in all three.
    clear_input_keys: tuple[str, ...] = ("CTRL_U",)

    @classmethod
    def command(cls) -> list[str]:
        raise NotImplementedError

    @classmethod
    def launch_args(
        cls,
        hooks_settings: str | None,
        conversation_id: str | None = None,
        port: int | None = None,
        server_url: str | None = None,
    ) -> list[str]:
        """Extra argv wiring hooks / a known conversation id into the agent."""
        return []

    # Whether this kind of agent has a readable conversation history (the
    # Conversation view is then always offered, even before the first message).
    supports_history = False

    # -- capabilities (the controller branches on these, never on ``kind``) --
    # The agent pushes exact lifecycle events through hooks (Claude).
    uses_hooks = False
    # The agent exposes its own local HTTP server for history/running tools.
    uses_local_server = False
    # The agent leaves per-task output files on disk (Claude task files).
    supports_task_files = False
    # Seconds to wait between typing text and ENTER (TUIs that detect *paste*).
    submit_delay = 0.0
    # Whether this CLI can be pointed at an MCP server config (Fase 4).
    mcp_supported = False

    @classmethod
    def mcp_config_file(cls) -> str | None:
        """Write (0600) a per-agent MCP config file, or None when the CLI takes
        the wiring through flags/env instead."""
        return None
    # argv flags that must never be injected automatically by this adapter.
    dangerous_flags: tuple[str, ...] = (
        "--dangerously-skip-permissions", "--yolo", "--dangerously-bypass-approvals-and-sandbox",
        "-a never",
    )

    # Set by the controller when the agent was launched with a known
    # conversation id (used to read its full history back from disk).
    conversation_id: str | None = None
    # When the current conversation started being looked for (after /new).
    conversation_since: float = 0.0
    # Set by the controller for agents that expose a local server (OpenCode).
    link: Any = None
    # Version gate, filled by the controller at launch (see harness.versions):
    # ``version_report`` is a VersionReport and ``version_blocked`` is True when
    # the CLI is outside the range the adapter was verified against and the
    # operator has not allowed unvalidated versions.
    version_report: Any = None
    version_blocked: bool = False

    def history(self, limit: int = 200, before: int | None = None) -> dict[str, Any]:
        """Full conversation history, or ``available: False`` if not supported."""
        return {"available": False, "messages": [], "total": 0, "start": 0}

    @classmethod
    def mcp_launch_args(cls, config_path: str | None) -> list[str]:
        """argv wiring an MCP server config into the agent (Fase 4). Empty when
        the CLI does not support it; adapters override."""
        return []

    def activity_snapshot(self) -> dict[str, Any]:
        """Model and still-running tool from the adapter's own signals."""
        return {}

    @classmethod
    def model_from_screen(cls, text: str | None) -> str | None:
        """Model display name shown on the agent's own screen."""
        return None

    @classmethod
    def screen_input_box(cls, text: str | None) -> bool:
        """Whether the adapter's input box is visible on a captured screen.

        Used by ``crewhall watch screens`` as a long-duration detector; an
        adapter that has no reliable marker leaves it false.
        """
        return False

    @classmethod
    def screen_footer(cls, text: str | None) -> bool:
        """Whether the adapter's status footer is visible on a captured screen."""
        return False

    # How the Web UI may change the model:
    #  - "direct": the composer offers a model list and ``set_model`` applies the
    #    chosen name (a slash command, or driving the TUI's own picker);
    #  - "picker": there is no list to offer; the UI only opens the TUI selector.
    model_switch_mode = "direct"

    @classmethod
    def models(cls) -> list[str]:
        """Static model names this adapter can switch to (may be empty)."""
        return []

    def available_models(self) -> list[Any]:
        """Model names/labels discoverable at runtime (e.g. OpenCode's catalog).

        Returns a list of strings or ``{"value", "label"}`` dicts. The default
        is the static ``models()``; adapters with a local server override it.
        """
        return list(self.models())

    def model_switch(self, model: str | None = None) -> list[str]:
        """Inputs to type (each submitted with Enter) to change the model.

        ``direct`` adapters return a single ``/model <name>``; ``picker``
        adapters return the command that opens their selector. ``set_model``
        sends each step through ``Harness.send_command`` (a non-turn input), so
        input cleanliness and readiness are enforced without faking a turn.
        """
        return [f"/model {model}".strip() if model else "/model"]

    def set_model(self, model: str | None = None) -> bool:
        """Apply a model change; True when it was driven successfully.

        The default sends ``model_switch`` steps as non-turn commands. Adapters
        whose CLI only has an interactive picker override this to open it and
        drive the selection.
        """
        for step in self.model_switch(model):
            self.send_command(step)
        return True

    def usage_from_screen(self, text: str) -> dict[str, Any]:
        """Usage (tokens/cost) the TUI shows; ``{"available": False}`` if none."""
        return {"available": False}

    def running_shells(self, root: int | None = None) -> dict[str, Any]:
        """Still-running tool commands known through the agent's own signals."""
        return {}

    @classmethod
    def config_risks(cls) -> list[str]:
        """Read-only warnings about the agent's own user config (permissive
        approval/sandbox settings). Adapters override; never modifies anything."""
        return []

    def on_hook(self, event: str, at: float | None = None) -> None:
        """Structured lifecycle signal pushed by the agent (optional)."""
        return None

    def __init__(
        self,
        session: InteractiveSession,
        *,
        name: str | None = None,
    ) -> None:
        self.session = session
        self.name = name or getattr(session, "name", None)
        self._prompt_sent = False
        self._evidence = "not observed yet"

    @property
    def agent_id(self) -> str:
        return self.session.session_id

    def start(self, timeout: float = 30.0) -> AgentState:
        return self.wait_for_state(
            (AgentState.READY, AgentState.WAITING_INPUT), timeout=timeout
        )

    def ensure_ready(self, timeout: float = 30.0) -> AgentState:
        return self.wait_for_state(
            (AgentState.READY, AgentState.WAITING_INPUT), timeout=timeout
        )

    def accept_workspace_trust(self, timeout: float = 0.0) -> bool:
        """Accept a one-time workspace trust dialog, if the agent shows one.

        Only used for chat agents: their working directory is an isolated folder
        crewhall created, so accepting it is safe and lets the chat start. The
        base harness has no such dialog.
        """
        return False

    def send(self, prompt: str, timeout: float = 30.0) -> None:
        state = self.ensure_ready(timeout)
        if not state.usable:
            raise HarnessError(
                self.agent_id, f"cannot send while state={state.value}"
            )
        if not self.ensure_input_clean(timeout=timeout):
            raise HarnessNotReady(self.agent_id, "input line not clean")
        # Clearing input must not have changed what the TUI is showing (e.g. a
        # stray key opening an overlay). Typing + ENTER into an overlay is
        # swallowed while still looking like a successful injection.
        state = self.state()
        if not state.usable:
            raise HarnessNotReady(
                self.agent_id, f"not ready to receive input (state={state.value})"
            )
        previous = self.conversation_id
        self._injected = prompt
        self.session.write(prompt)
        if self.submit_delay > 0:
            # Some TUIs read a fast text+ENTER as a paste and do not submit it.
            time.sleep(self.submit_delay)
        self.session.send_enter()
        self._prompt_sent = True
        self._typed = ""
        if self._is_new_session_command(prompt):
            # Not a turn: the TUI is idle on a fresh conversation, no reply comes.
            self._prompt_sent = False
            self._new_session_started(previous)

    def send_command(self, text: str, timeout: float = 30.0) -> None:
        """Send a slash command / non-turn input to the agent's own TUI.

        Same readiness and clean-input guarantees as ``send`` but it does *not*
        mark a work cycle as started: commands like ``/model``, ``/compact`` or
        a picker opener produce no completion line, so treating them as a turn
        would leave the agent reading as ``unknown`` afterwards.
        """
        state = self.ensure_ready(timeout)
        if not state.usable:
            raise HarnessError(self.agent_id, f"cannot send while state={state.value}")
        if not self.ensure_input_clean(timeout=timeout):
            raise HarnessNotReady(self.agent_id, "input line not clean")
        state = self.state()
        if not state.usable:
            raise HarnessNotReady(
                self.agent_id, f"not ready to receive input (state={state.value})"
            )
        self._injected = text
        self.session.write(text)
        if self.submit_delay > 0:
            time.sleep(self.submit_delay)
        self.session.send_enter()
        self._typed = ""

    # -- input readiness ---------------------------------------------------
    def input_line(self) -> str:
        """Return the current content of the agent's input line, or ''.

        Overridden per adapter; the base implementation has no reliable marker
        and therefore reports an empty (i.e. clean) line.
        """
        return ""

    def _input_is_clean(self) -> bool:
        """True when the agent's input has no residual text/completion."""
        return not self.input_line().strip()

    def _residue_is_ours(self, line: str) -> bool:
        """True when the dirty input line is text crewhall itself injected.

        Only ``_injected`` (what ``send``/``send_command`` typed) counts. Keys a
        human types through the Web UI (``write_raw`` -> ``_typed``) are a draft,
        never residue, so they are never cleared.
        """
        injected = (self._injected or "").strip()
        current = line.strip()
        if not injected or not current:
            return False
        if current == injected or current.endswith(injected):
            return True
        # A partially written injection (prefix) is ours too, but never a short
        # line: one or two human keystrokes would match the prefix of almost any
        # injected text and must not be wiped.
        return len(current) >= min(8, len(injected)) and injected.startswith(current)

    def ensure_input_clean(
        self, timeout: float = 30.0, poll: float = 0.1
    ) -> bool:
        """Wait until the agent's input line is free of residual content.

        A freshly (re)started TUI can still hold previous text or have a
        completion popup pending; writing then would concatenate our message
        with that residue. We wait for an empty input line before injecting.

        Crucially, only residue crewhall itself injected (``_injected``) is cleared
        with a non-destructive ``Ctrl+U``. A non-empty line the human is
        drafting in the agent's own terminal (tmux attach or the Web UI) is
        **left untouched**: we return ``False`` so the caller queues and retries
        instead of destroying the user's work. The TUI's own placeholder /
        suggestion is not residue: each adapter's ``input_line`` filters it, so
        it reads as clean here.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.session.status.alive:
                return False
            if self._input_is_clean():
                return True
            # Clear residual input only when the agent is not mid-turn or
            # starting (keys would interfere), and only if the residue is ours.
            state = self.state()
            if state in (AgentState.WORKING, AgentState.STARTING):
                time.sleep(poll)
                continue
            line = self.input_line()
            if not self._residue_is_ours(line):
                # Not ours: a human draft (never destroy it) or the TUI's own
                # ghost text that the adapter could not filter. Refuse; the
                # higher layer queues the message and retries.
                return False
            for key in self.clear_input_keys:
                try:
                    self.session.send_key(key)
                except Exception:  # noqa: BLE001
                    log.debug("ignored exception in a boundary guard", exc_info=True)
            time.sleep(poll)
        return self._input_is_clean()

    def input_is_clean(self) -> bool:
        """Public check: is the agent's input line free of residual text?"""
        try:
            return self._input_is_clean()
        except Exception:  # noqa: BLE001
            return False

    # Keys that stop a running turn. Per adapter: Claude needs exactly one ESC
    # (two would open its Rewind selector); OpenCode needs ESC twice.
    interrupt_keys: tuple[str, ...] = ("ESC",)

    def interrupt(self) -> bool:
        """Stop the current turn. Only acts while WORKING; True if keys were sent."""
        if self.state() != AgentState.WORKING:
            return False
        for i, key in enumerate(self.interrupt_keys):
            if i:
                time.sleep(0.15)
            self.session.send_key(key)
        return True

    def write_raw(self, text: str) -> None:
        """Forward raw text to the agent's own TUI (no ENTER appended)."""
        self.session.write(text)
        self._typed = (self._typed or "") + text

    def send_key(self, key: str) -> None:
        """Forward a named key to the agent's own TUI (arrows, ESC, C-c, …)."""
        submitted = (self._typed or "").strip() if key.upper() == "ENTER" else None
        if key.upper() != "ENTER" or submitted is not None:
            self._typed = ""
        previous = self.conversation_id
        self.session.send_key(key)
        if submitted is not None and self._is_new_session_command(submitted):
            self._new_session_started(previous)

    # -- new conversation in the same process (/clear, /new) ---------------
    # Commands that make the TUI drop its conversation and start another one.
    new_session_commands: tuple[str, ...] = ()
    # Set by the controller: called when one of those commands is submitted.
    on_new_session: Callable[[str | None], None] | None = None
    _typed = ""
    # Last text crewhall itself injected (send/send_command): the only residue
    # ``ensure_input_clean`` may clear.
    _injected = ""

    def _is_new_session_command(self, text: str) -> bool:
        return text.strip().lower() in self.new_session_commands

    def _new_session_started(self, previous: str | None = None) -> None:
        """``previous``: the conversation before the command (None = current)."""
        if callable(self.on_new_session):
            self.on_new_session(self.conversation_id if previous is None else previous)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the agent's real terminal (PTY/tmux) to the given viewport."""
        self.session.resize(cols, rows)

    def capture(self) -> str:
        return self.session.capture()

    def capture_recent(self, max_lines: int = 40) -> str:
        lines = self.session.capture().splitlines()
        return "\n".join(lines[-max_lines:])

    def transcript(self) -> str:
        """Captured output with the agent's *own* input area removed.

        Consumers that draw their own input (the Web UI) must not show the
        agent's prompt box a second time. Adapters override this to strip the
        region they know is the agent's input/composer. The base implementation
        returns the raw capture (no reliable markers known).
        """
        return self.session.capture()

    def state(self) -> AgentState:
        try:
            self.session.poll()
        except Exception:  # noqa: BLE001 - a broken poll falls back to the last known status
            log.debug("session poll failed in state()", exc_info=True)
        status = self.session.status
        if not status.alive:
            code = self.session.exit_code
            if code not in (None, 0):
                self._evidence = f"process exited with code {code}"
                return AgentState.ERROR
            self._evidence = f"process exited with code {code}"
            return AgentState.EXITED
        state, evidence = self._detect(self.session.capture())
        self._evidence = evidence
        return state

    def evidence(self) -> str:
        return self._evidence

    def is_waiting(self) -> bool:
        return self.state().usable

    def wait_for_state(
        self,
        targets: AgentState | Iterable[AgentState],
        timeout: float = 30.0,
        interval: float = 0.1,
    ) -> AgentState:
        wanted = (targets,) if isinstance(targets, AgentState) else tuple(targets)
        deadline = time.monotonic() + timeout
        state = self.state()
        while state not in wanted and not state.terminal:
            if time.monotonic() >= deadline:
                return state
            time.sleep(interval)
            state = self.state()
        return state

    def stop(self, force: bool = False) -> None:
        if force:
            self.session.kill()
            self.session.backend.close()
        else:
            self.session.close()

    def info(self) -> AgentInfo:
        session_info = self.session.info()
        return AgentInfo(
            agent_id=self.agent_id,
            name=self.name,
            kind=self.kind,
            state=self.state(),
            session_id=session_info.session_id,
            backend=session_info.backend,
            pid=session_info.pid,
            created_at=session_info.created_at,
            last_input_at=session_info.last_input_at,
            last_output_at=session_info.last_output_at,
            exited_at=session_info.exited_at,
            exit_code=session_info.exit_code,
            evidence=self._evidence,
            cwd=session_info.cwd,
            meta=dict(session_info.meta),
        )

    @abstractmethod
    def _detect(self, text: str) -> tuple[AgentState, str]:
        """Return (state, evidence) for the currently captured text."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} agent={self.agent_id} name={self.name}>"
