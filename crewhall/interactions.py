"""Interactive requests an agent's TUI raises for a human: permissions and questions.

The agent CLI asks (OpenCode: ``permission.asked`` / ``question.asked`` on its
local server), crewhall records the request here in a CLI-neutral shape,
any front-end (Web UI) lists and answers it, and the answer goes back through
the CLI's own API. Answering in the TUI itself closes the request here too.

Shapes (what front-ends see):

* permission: ``{"kind": "permission", "title", "detail", "patterns": [...],
  "choices": ["once", "always", "reject"]}``; answer ``{"reply": <choice>,
  "message"?: str}``.
* question: ``{"kind": "question", "questions": [{"question", "header",
  "options": [{"label", "description"}], "multiple", "custom"}]}``; answer
  ``{"answers": [[label, ...] per question]}`` or ``{"reject": true}``.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

KINDS = ("permission", "question")
PERMISSION_REPLIES = ("once", "always", "reject")
MAX_TEXT = 4000
# Answered/closed requests are kept briefly so a viewer sees how they ended.
KEEP_CLOSED = 30.0


class InteractionError(ValueError):
    pass


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class Interaction:
    interaction_id: str
    agent_id: str
    kind: str
    created_at: float
    data: dict[str, Any]
    # CLI-side reference used to answer (e.g. OpenCode request id).
    ref: dict[str, Any] = field(default_factory=dict)
    state: str = "pending"  # pending | answered | closed
    answer: dict[str, Any] | None = None
    answered_by: str | None = None
    closed_at: float | None = None
    # Set when the request stops being pending (a hook may be blocked on it).
    done: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.interaction_id,
            "agent_id": self.agent_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "state": self.state,
            "answer": self.answer,
            "answered_by": self.answered_by,
            **self.data,
        }


def normalize_questions(raw: Any) -> list[dict[str, Any]]:
    out = []
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        options = [
            {"label": _clip(o.get("label", ""), 200),
             "description": _clip(o.get("description", ""), 1000)}
            for o in q.get("options") or [] if isinstance(o, dict) and o.get("label")
        ]
        out.append({
            "question": _clip(q.get("question", ""), 2000),
            "header": _clip(q.get("header", ""), 60),
            "options": options,
            "multiple": bool(q.get("multiple") or q.get("multiSelect")),
            # OpenCode: custom answers are allowed unless explicitly disabled.
            "custom": q.get("custom") is not False,
        })
    return out


def validate_answer(item: Interaction, answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict):
        raise InteractionError("answer must be an object")
    if answer.get("terminal"):
        if not item.data.get("terminal"):
            raise InteractionError("this request can only be answered here")
        return {"terminal": True}
    if item.kind == "permission":
        reply = answer.get("reply")
        if reply not in item.data.get("choices", PERMISSION_REPLIES):
            raise InteractionError(f"reply must be one of {', '.join(item.data.get('choices', PERMISSION_REPLIES))}")
        out: dict[str, Any] = {"reply": reply}
        if answer.get("message"):
            out["message"] = _clip(answer["message"], 2000)
        return out
    if answer.get("reject"):
        return {"reject": True}
    questions = item.data.get("questions") or []
    answers = answer.get("answers")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise InteractionError(f"answers must be a list with one entry per question ({len(questions)})")
    clean: list[list[str]] = []
    for q, given in zip(questions, answers, strict=False):
        if not isinstance(given, list) or not all(isinstance(x, str) and x.strip() for x in given):
            raise InteractionError("each answer must be a non-empty list of labels")
        given = [_clip(x.strip(), 2000) for x in given]
        if not given:
            raise InteractionError(f"no answer for {q['header'] or q['question']!r}")
        if len(given) > 1 and not q.get("multiple"):
            raise InteractionError(f"{q['header'] or q['question']!r} accepts a single answer")
        labels = {o["label"] for o in q.get("options") or []}
        if not q.get("custom") and any(x not in labels for x in given):
            raise InteractionError(f"{q['header'] or q['question']!r} only accepts its options")
        clean.append(given)
    return {"answers": clean}


class InteractionBoard:
    """Thread-safe registry of pending interactions, keyed by id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Interaction] = {}

    def open(
        self, agent_id: str, kind: str, interaction_id: str,
        data: dict[str, Any], ref: dict[str, Any] | None = None,
    ) -> tuple[Interaction, bool]:
        """Record a request; idempotent per id. Returns ``(item, created)``."""
        if kind not in KINDS:
            raise InteractionError(f"unknown interaction kind {kind!r}")
        with self._lock:
            self._prune()
            existing = self._items.get(interaction_id)
            if existing is not None:
                return existing, False
            item = Interaction(interaction_id, agent_id, kind, time.time(), data, dict(ref or {}))
            self._items[interaction_id] = item
            return item, True

    def get(self, interaction_id: str) -> Interaction:
        with self._lock:
            item = self._items.get(interaction_id)
        if item is None:
            raise InteractionError(f"no such interaction {interaction_id!r}")
        return item

    def pending(self, agent_id: str | None = None) -> list[Interaction]:
        with self._lock:
            self._prune()
            return sorted(
                (i for i in self._items.values()
                 if i.state == "pending" and (agent_id is None or i.agent_id == agent_id)),
                key=lambda i: i.created_at,
            )

    def close(
        self, interaction_id: str, *, answer: dict[str, Any] | None = None,
        by: str | None = None,
    ) -> Interaction | None:
        """Mark a request as no longer pending (answered here or elsewhere)."""
        with self._lock:
            item = self._items.get(interaction_id)
            if item is None or item.state != "pending":
                return item
            item.state = "answered" if answer is not None else "closed"
            item.answer = answer
            item.answered_by = by
            item.closed_at = time.time()
        item.done.set()
        return item

    def forget_agent(self, agent_id: str) -> None:
        with self._lock:
            for key in [k for k, i in self._items.items() if i.agent_id == agent_id]:
                del self._items[key]

    def _prune(self) -> None:
        now = time.time()
        for key in [k for k, i in self._items.items()
                    if i.closed_at is not None and now - i.closed_at > KEEP_CLOSED]:
            del self._items[key]


def opencode_permission(props: dict[str, Any]) -> dict[str, Any]:
    """OpenCode ``PermissionRequest`` -> neutral permission data."""
    metadata = props.get("metadata") if isinstance(props.get("metadata"), dict) else {}
    patterns = [_clip(p, 500) for p in props.get("patterns") or [] if isinstance(p, str)]
    detail_bits = []
    for key in ("command", "filepath", "filePath", "path", "url", "description"):
        if metadata.get(key):
            detail_bits.append(f"{key}: {_clip(metadata[key], 1500)}")
    if not detail_bits and metadata:
        detail_bits.append(_clip(metadata, 1500))
    return {
        "title": _clip(props.get("permission") or "permission", 200),
        "detail": "\n".join(detail_bits),
        "patterns": patterns,
        "always": [_clip(p, 500) for p in props.get("always") or [] if isinstance(p, str)],
        "choices": list(PERMISSION_REPLIES),
        "session": props.get("sessionID"),
    }


# -- Claude Code (PermissionRequest hook) ---------------------------------------
_DETAIL_KEYS = ("command", "file_path", "path", "url", "pattern", "description", "prompt")


def claude_request(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Claude ``PermissionRequest`` hook payload -> ``(kind, data)``; None if unusable."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return None
    if tool == "AskUserQuestion":
        questions = normalize_questions(tool_input.get("questions"))
        if not questions:
            return None
        for q in questions:
            q["custom"] = True  # Claude always offers "Other"
        return "question", {"questions": questions, "source": "claude", "terminal": True}
    bits = [f"{k}: {_clip(tool_input[k], 1500)}" for k in _DETAIL_KEYS if tool_input.get(k)]
    if not bits:
        import json

        bits.append(_clip(json.dumps(tool_input, ensure_ascii=False, indent=1), 1500))
    suggestions = payload.get("permission_suggestions")
    choices = ["once", "always", "reject"] if suggestions else ["once", "reject"]
    return "permission", {
        "title": _clip(tool, 200), "detail": "\n".join(bits), "patterns": [],
        "always": [], "choices": choices, "source": "claude", "terminal": True,
    }


def claude_decision(
    item: Interaction, payload: dict[str, Any], answer: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The answer as Claude's PermissionRequest ``decision``; None = ask in the TUI."""
    if not answer or answer.get("terminal"):
        return None
    if item.kind == "permission":
        reply = answer.get("reply")
        if reply == "reject":
            return {"behavior": "deny",
                    "message": answer.get("message") or "The user denied this from crewhall."}
        decision: dict[str, Any] = {"behavior": "allow"}
        if reply == "always" and payload.get("permission_suggestions"):
            decision["updatedPermissions"] = payload["permission_suggestions"]
        return decision
    if answer.get("reject"):
        return {"behavior": "deny", "message": "The user dismissed the question."}
    tool_input = dict(payload.get("tool_input") or {})
    # Keyed by the question text exactly as Claude sent it (not the clipped copy).
    raw = [q for q in tool_input.get("questions") or [] if isinstance(q, dict)]
    tool_input["answers"] = {
        str(q.get("question", "")): ", ".join(given)
        for q, given in zip(raw, answer.get("answers") or [], strict=False)
    }
    return {"behavior": "allow", "updatedInput": tool_input}
