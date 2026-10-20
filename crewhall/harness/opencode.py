from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any, cast

from .base import AgentState, Harness, HarnessNotReady
from ..session import InteractiveSession

WORKING_RE = re.compile(r"esc\s+interrupt", re.IGNORECASE)
# One ``Build · <model> · 4.3s`` line is appended to the transcript per completed
# turn, so counting matches tells us how many turns have finished (robust across
# turns even when the agent is not polled while working). Validated units are
# ``ms``/``s`` (fast turns), ``m`` and ``h`` plus combinations
# (``2m 9s``, ``1h 2m``); a long turn must not stay UNKNOWN.
COMPLETION_RE = re.compile(
    r"\u00b7\s*\d+(?:\.\d+)?(?:ms|s|m|h)\b"
    r"(?:\s*\d+(?:\.\d+)?(?:ms|s|m|h)\b)*",
    re.IGNORECASE,
)
MOUNT_MARKERS = ("ctrl+p", "build ·", "build -")
# Overlays that own the keyboard. Only the navigation/picker ones may be closed
# with ESC: ESC in a permission or form rejects/cancels the request the user was
# asked to answer, so those always raise ``HarnessNotReady`` for the queue to retry.
_SAFE_OVERLAYS = {"sessions", "palette", "models", "agents", "variants", "themes"}
_TAB_BAR_RE = re.compile(r"^\s{0,6}\S.*\s{2,}\+\s*$")
# Session view footer: context tokens, e.g. ``13.1K (1%) · $0.00``. The home
# screen (no session yet, e.g. right after ``/new``) shows the version instead.
# Composer footer: ``<Agent> · <model> [provider]`` (``Build · …`` by default).
FOOTER_LINE_RE = re.compile(r"^[\w][\w .-]{0,40} [\u00b7-] \S")
TOKENS_RE = re.compile(r"\d+(?:\.\d+)?[KM]?\s+\(\d+%\)")
READY_PLACEHOLDER = "ask anything"
# The composer's closing rule (``╹▀▀▀…``). It is drawn whenever the input box is
# mounted, so it is the readiness marker for an *empty* session: v2 opened with
# ``--session`` right after crewhall created it (or after a v1->v2 fallback)
# shows this composer without the ``Ask anything`` placeholder v1 draws.
INPUT_BOX_RE = re.compile(r"\u2579\u2580{3,}")


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

    def __init__(self, session: InteractiveSession, *, name: str | None = None) -> None:
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
        server_url: str | None = None,
    ) -> list[str]:
        # v2: connect to a crewhall-managed `serve` (the TUI no longer opens its
        # own port). v1: the TUI opens its private server on `port`.
        if server_url:
            return ["--server", server_url]
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

    def history(self, limit: int = 200, before: int | None = None) -> dict[str, Any]:
        if self.link and not self.conversation_id and self.conversation_resolver:
            self.conversation_id = self.conversation_resolver()
        if not (self.link and self.conversation_id):
            return super().history(limit, before)
        data = cast(dict[str, Any], self.link.history(self.conversation_id, limit, before))
        # ``/new`` typed straight into the TUI (not through crewhall): the start
        # screen shows while the tracked conversation still has messages, so we
        # left it.  A start screen over an *empty* conversation is just the fresh
        # session crewhall's own ``/new`` created — retiring it here blacklisted
        # the new conversation and left the Conversation view empty forever, even
        # as the Live view kept showing activity (OpenCode >= 1.18.34 creates the
        # empty session before the first prompt).
        if (getattr(self.link, "api", "v1") != "v2"
                and self.session.status.alive and int(data.get("total") or 0) > 0
                and self.on_home_screen()):
            self._new_session_started()
            if self.conversation_resolver:
                self.conversation_resolver()
                if self.conversation_id:
                    return cast(dict[str, Any],
                                self.link.history(self.conversation_id, limit, before))
            return super().history(limit, before)
        return data

    def activity_snapshot(self) -> dict[str, Any]:
        from .. import activity

        return activity.opencode_snapshot(self.link, self.conversation_id)

    @classmethod
    def model_from_screen(cls, text: str | None) -> str | None:
        from .. import activity

        return activity.opencode_model(text)

    @classmethod
    def screen_input_box(cls, text: str | None) -> bool:
        blob = text or ""
        return READY_PLACEHOLDER in blob.lower() or "\u2503" in blob

    @classmethod
    def screen_footer(cls, text: str | None) -> bool:
        return "ctrl+p" in (text or "").lower()

    # OpenCode has no one-shot ``/model <name>``: ``/models`` opens its own
    # picker. We drive it (open, type to fuzzy-filter, Enter) so the Web UI can
    # still offer a real model list; the catalog comes from the local server.
    model_switch_mode = "direct"
    PICKER_TITLE = "select model"
    VARIANT_TITLE = "select variant"
    _MODELS_TTL = 60.0

    def model_switch(self, model: str | None = None) -> list[str]:
        return ["/models"]

    def available_models(self) -> list[dict[str, str]]:
        """Model catalog from the agent's own server (connected providers only)."""
        link = self.link
        if link is None:
            return []
        cache: tuple[float, list[dict[str, str]]] | None = getattr(self, "_models_cache", None)
        now = time.monotonic()
        if cache and now - cache[0] < self._MODELS_TTL:
            return cache[1]
        try:
            if getattr(link, "api", "v1") == "v2":
                data = link.get_json("/api/model", timeout=3.0)
                models = data.get("data") if isinstance(data, dict) else None
                v2_out = [
                    {"value": str(m.get("name") or m.get("modelID") or m.get("id")),
                     "label": f"{m.get('name') or m.get('modelID') or m.get('id')} — "
                              f"{m.get('providerID') or ''}".rstrip(" —")}
                    for m in (models if isinstance(models, list) else []) if isinstance(m, dict)
                ]
                v2_out.sort(key=lambda item: item["label"].lower())
                result = v2_out[:300]
                self._models_cache = (now, result)
                return result
            data = link.get_json("/provider", timeout=3.0)
        except Exception:  # noqa: BLE001 - the server may be busy or older
            return cache[1] if cache else []
        providers = data.get("all") if isinstance(data, dict) else None
        if not isinstance(providers, list):
            return cache[1] if cache else []
        connected = data.get("connected")
        allowed = {c if isinstance(c, str) else (c or {}).get("id") for c in connected} \
            if isinstance(connected, list) and connected else None
        out: list[dict[str, str]] = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            pid = provider.get("id")
            if allowed is not None and pid not in allowed:
                continue
            pname = provider.get("name") or pid or ""
            models = provider.get("models")
            for mid, model in (models.items() if isinstance(models, dict) else []):
                name = (model or {}).get("name") or mid
                out.append({"value": name, "label": f"{name} — {pname}"})
        out.sort(key=lambda item: item["label"].lower())
        result = out[:300]
        self._models_cache = (now, result)
        return result

    _DIALOG_TITLES = ("select model", "select variant", "select agent",
                      "select session", "select theme", "sessions for",
                      "commands", "search")

    @classmethod
    def overlay(cls, text: str | None) -> str | None:
        """Name of the v2 overlay/mode owning the screen, if any.

        ``sessions``/``palette``/``models``/``agents``/``variants``/``themes``
        are safe to dismiss with ESC. ``permission``/``form``/``shell`` are not:
        they need an explicit answer (or the user leaving shell mode), so the
        keyboard path refuses to type instead of answering for them.
        """
        low = (text or "").lower()
        if "permission required" in low or "\u25b3 permission" in low:
            return "permission"
        if "need input" in low:
            return "form"
        if "sessions for " in low:
            return "sessions"
        if "\u25b3" not in low and cls._has_title_line(text, "commands"):
            return "palette"
        if "select model" in low:
            return "models"
        if "select agent" in low:
            return "agents"
        if "select variant" in low:
            return "variants"
        if "select theme" in low:
            return "themes"
        if "esc exit shell mode" in low or (
                "run a command" in low and cls._has_title_line(text, "shell")):
            return "shell"
        return None

    @staticmethod
    def _has_title_line(text: str | None, title: str) -> bool:
        return any(line.strip().lower().startswith(title)
                   for line in (text or "").splitlines())

    def close_safe_overlay(self) -> None:
        """Dismiss a navigation overlay; never touch a permission/form/shell."""
        for _ in range(3):
            current = self.capture()
            kind = self.overlay(current)
            if kind in (None, "shell", "permission", "form"):
                return
            if kind not in _SAFE_OVERLAYS:
                return
            self.session.send_key("ESC")
            time.sleep(0.2)
            if self.overlay(self.capture()) != kind:
                return

    def _await_text(self, needle: str, timeout: float) -> bool:
        """Wait until ``needle`` shows up in a capture (small test/UI helper)."""
        return self._await(lambda text: needle.lower() in text.lower(), timeout)

    def _await_overlay_clear(self, kind: str, timeout: float = 2.0) -> bool:
        return self._await(lambda text: self.overlay(text) != kind, timeout)

    def set_model(self, model: str | None = None) -> bool:
        """Open ``/models`` and, when a name is given, filter and select it.

        The picker has a fuzzy search box: typing the model name narrows the
        list to the match and Enter selects the highlighted row. We first close
        any dialog left open by a previous attempt (so keystrokes do not land in
        its filter), wait for the picker to appear, type from an empty filter and
        only then press Enter.
        """
        self._close_dialogs()
        self.send_command("/models")
        name = (model or "").strip()
        if not name:
            return True
        if not self._await(lambda text: self.PICKER_TITLE in text.lower(), 4.0):
            raise HarnessNotReady(self.agent_id, "the model picker did not open")
        # Type from an empty filter so the name never appends to residue.
        self.session.send_key("CTRL_U")
        time.sleep(0.1)
        self.write_raw(name)
        first = name.split()[0].lower()
        self._await(lambda text: first in text.lower(), 2.0)
        time.sleep(0.25)
        self.session.send_enter()
        # Some models chain into a variant picker (Default/low/high/max) right
        # after the model is chosen; accept its default. Otherwise, if the list
        # is still open the Enter did not land, so try once more.
        if self._await(lambda text: self.VARIANT_TITLE in text.lower(), 1.2):
            self.session.send_enter()
            self._await(lambda text: self.VARIANT_TITLE not in text.lower(), 2.0)
        elif self._await(lambda text: self.PICKER_TITLE in text.lower(), 0.3):
            self.session.send_enter()
            self._await(lambda text: self.PICKER_TITLE not in text.lower(), 1.5)
        return True

    def _close_dialogs(self) -> None:
        """Dismiss any safe TUI dialog left open (a picker swallows keystrokes)."""
        self.close_safe_overlay()

    def _await(self, predicate: Callable[[str], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate(self.session.capture()):
                    return True
            except Exception:  # noqa: BLE001 - best effort polling
                pass  # noqa: BLE001 - justified boundary guard (B1)
            time.sleep(0.15)
        return False

    def usage_from_screen(self, text: str) -> dict[str, Any]:
        from .. import usage

        return usage.opencode_usage(text)

    def running_shells(self, root: int | None = None) -> dict[str, Any]:
        link, conv = self.link, self.conversation_id
        if link is None or not conv:
            return {}
        try:
            return cast(dict[str, Any], link.running_tools(conv))
        except Exception:  # noqa: BLE001 - the server may be busy/restarting
            return {}

    def send(self, prompt: str, timeout: float = 30.0) -> None:
        """Deliver a turn through the v2 API; keyboard only for UI commands.

        The v2 TUI's active tab is not the tracked session (and an open picker
        would swallow the text), so with an explicit v2 link a prompt is posted
        to ``/api/session/{conversation_id}/prompt``. Slash commands (``/new``,
        ``/models``…) still go to the keyboard because they drive the UI. A
        session that does not exist yet (first prompt after ``/new``) also goes
        through the keyboard; the API takes over once ``session.created`` lands.

        A human draft in the composer blocks the API path too: the message is
        queued (reason ``human draft``) and delivered once the human clears it,
        exactly as on the keyboard path.
        """
        link = self.link
        uses_api = (
            link is not None
            and getattr(link, "api", "v1") == "v2"
            and self.conversation_id
            and not prompt.lstrip().startswith("/")
        )
        if uses_api:
            # A human drafting in the composer is never clobbered and never
            # raced: if the input holds text, the message waits in the queue
            # (reason "human draft") until the human clears it. The API path
            # must apply the same guard as the keyboard path below.
            if not self.input_is_clean():
                raise HarnessNotReady(self.agent_id, "input line not clean")
            link.post_json(
                f"/api/session/{self.conversation_id}/prompt", {"text": prompt},
                timeout=max(10.0, timeout),
            )
            self._prompt_sent = True
            self._seen_working = False
            self._sent_at = time.time()
            self._completion_count_at_send = len(
                COMPLETION_RE.findall(self.session.capture())
            )
            return
        self._keyboard_guard()
        super().send(prompt, timeout=timeout)
        self._completion_count_at_send = len(
            COMPLETION_RE.findall(self.session.capture())
        )
        self._seen_working = False
        self._sent_at = time.time()

    def send_command(self, text: str, timeout: float = 30.0) -> None:
        self._keyboard_guard()
        super().send_command(text, timeout=timeout)

    def _keyboard_guard(self) -> None:
        """Close safe pickers; refuse to type into an overlay that needs an answer."""
        self.close_safe_overlay()
        kind = self.overlay(self.capture())
        if kind == "permission":
            raise HarnessNotReady(self.agent_id, "a permission request needs an answer")
        if kind == "form":
            raise HarnessNotReady(self.agent_id, "a form needs an answer")
        if kind == "shell":
            raise HarnessNotReady(self.agent_id, "the TUI is in shell mode")
        if kind is not None:
            raise HarnessNotReady(self.agent_id, f"an overlay is open ({kind})")

    def input_line(self) -> str:
        """Content of the OpenCode composer line (the ``┃`` block), if any.

        The composer is the contiguous ``┃`` block closed by the ``╹▀▀▀`` rule;
        its last line is the agent/model footer (``Build · <model>``, or the
        agent's own name with ``--agent``, e.g. ``Ejecutor · <model>``). Earlier
        ``┃`` blocks are echoes of past messages and are never input. A fresh
        OpenCode shows a rotating placeholder (``Ask anything… "..."``): empty.
        """
        lines = self.session.capture().splitlines()
        if self.overlay(self.session.capture()) is not None:
            return ""  # a picker/shell/ask owns the keyboard: not a composer draft

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
        # v2 draws a tab strip above the session; it is UI chrome, not
        # conversation, so the Web UI transcript must not repeat it.
        if lines and _TAB_BAR_RE.match(lines[0]) and not lines[0].lstrip().startswith("\u2503"):
            cut = 0
            while cut < len(lines) and (cut == 0 or not lines[cut].strip()):
                cut += 1
                if cut < len(lines) and _TAB_BAR_RE.match(lines[cut]):
                    continue
            while cut < len(lines) and not lines[cut].strip():
                cut += 1
            lines = lines[cut:]
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

    def state(self) -> AgentState:
        """Authoritative v2 state from the server, with the screen as fallback.

        A background tab can look idle (or working) while the tracked session is
        doing the opposite, and a long turn may not be captured by polling. When
        the private server is linked: an active tracked sid is WORKING; an
        ``outcome``/``time.idle`` newer than the last send is WAITING_INPUT.
        """
        link = self.link
        if (link is not None and getattr(link, "api", "v1") == "v2"
                and self.conversation_id):
            self.session.poll()
            if self.session.status.alive:
                try:
                    raw = link.get_json("/api/session/active", timeout=1.0)
                    data = raw.get("data") if isinstance(raw, dict) else raw
                    if isinstance(data, dict) and self.conversation_id in data:
                        self._seen_working = True
                        self._evidence = "the session is active on the v2 server"
                        return AgentState.WORKING
                    raw = link.get_json(
                        f"/api/session/{self.conversation_id}", timeout=1.0)
                    session = raw.get("data") if isinstance(raw, dict) else raw
                    if isinstance(session, dict):
                        times = session.get("time") or {}
                        stamp = max(int(times.get("idle") or 0),
                                    int(times.get("updated") or 0))
                        if ((self._prompt_sent or self._sent_at)
                                and (session.get("outcome") is not None
                                     or (stamp and stamp >= self._sent_at * 1000))):
                            self._seen_working = True
                            self._evidence = "the v2 server reports the turn finished"
                            return AgentState.WAITING_INPUT
                except Exception:  # noqa: BLE001 - the screen is the fallback
                    pass
        return super().state()

    def _detect(self, text: str) -> tuple[AgentState, str]:
        low = text.lower()
        overlay = self.overlay(text)
        if overlay == "permission":
            return AgentState.WAITING_INPUT, "permission required (ask)"
        if overlay == "form":
            return AgentState.WAITING_INPUT, "need input (ask)"
        if overlay == "shell":
            return AgentState.WAITING_INPUT, "shell mode is open"
        if overlay is not None:
            return AgentState.WAITING_INPUT, f"overlay open: {overlay}"
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
            if INPUT_BOX_RE.search(text):
                # An empty session (v2 with ``--session``, or ``/new``) shows the
                # composer without the placeholder: still a ready input box.
                return AgentState.READY, "input box visible (empty session)"
            return AgentState.UNKNOWN, "TUI mounted, no readiness marker"
        if (
            self._seen_working
            or (self._stop_at and self._stop_at >= self._sent_at)
            or completed_turns > self._completion_count_at_send
        ):
            return AgentState.WAITING_INPUT, "TUI idle after a completed work cycle"
        return AgentState.UNKNOWN, "prompt sent, no completion evidence yet"
