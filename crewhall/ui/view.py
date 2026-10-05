from __future__ import annotations

from .model import BACKEND_DESC, TERMINAL_STATES, AppModel, Modal

MIN_W, MIN_H = 80, 24
Segment = tuple[str, str]
Line = list[Segment]


def _pad(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text[:width].ljust(width)


def _seg(text: str) -> Segment:
    return (text, "normal")


# ------------------------------------------------------------------ public
def render_text(model: AppModel, width: int, height: int) -> list[str]:
    return ["".join(text for text, _ in line) for line in render(model, width, height)]


def render(model: AppModel, width: int, height: int) -> list[Line]:
    if width < MIN_W or height < MIN_H:
        return _too_small(width, height)

    lines: list[Line] = []
    summary = f"{len(model.agents)} agents \u00b7 {len(model.teams)} teams "
    lines.append([(" CREWHALL", "title"), (summary.rjust(max(0, width - 15)), "title")])
    lines.append([("\u2500" * width, "border")])

    body_h = max(1, height - 4)
    left_w = _sidebar_width(width)
    right_w = width - left_w - 1

    left = _navigator(model, left_w, body_h)
    right = _session(model, right_w, body_h)
    for i in range(body_h):
        line: Line = []
        line.extend(left[i] if i < len(left) else [(_pad("", left_w), "normal")])
        line.append(("\u2502", "border"))
        line.extend(right[i] if i < len(right) else [(_pad("", right_w), "normal")])
        lines.append(line)

    lines.append([(f" {model.status}", _status_style(model))])
    hints = "   ".join(model.footer_hints())
    context = _context(model)
    footer = f" {context}"
    if len(footer) + len(hints) + 2 <= width:
        footer = footer.ljust(width - len(hints)) + hints
    lines.append([(footer, "status")])

    if model.modal is not None:
        lines = _overlay(model, lines, width, height, _modal_lines(model, min(width - 6, 76)))
    if not model.connected:
        box = _box("DISCONNECTED", [
            "", "Unable to reach the crewhall daemon.", "",
            "   [r] Retry        [q] Quit",
        ], min(width - 6, 60), "error")
        lines = _overlay(model, lines, width, height, box)
    return lines[:height]


def _sidebar_width(width: int) -> int:
    left = width // 3
    left = max(22, min(left, 38))
    if width - left - 1 < 40:
        left = max(18, width - 41)
    return left


def _too_small(width: int, height: int) -> list[Line]:
    mid = height // 2
    lines: list[Line] = []
    for i in range(max(height, 0)):
        if i == mid:
            lines.append([(_pad("terminal too small", width), "error")])
        elif i == mid + 1:
            lines.append([(_pad(f"resize to at least {MIN_W}x{MIN_H}", width), "muted")])
        else:
            lines.append([(_pad("", width), "normal")])
    return lines


# -------------------------------------------------------------- navigator
def _navigator(model: AppModel, width: int, height: int) -> list[Line]:
    current = model.current_row()
    current_key = current.get("key") if current else None
    selected = model.focus == "nav"
    rows: list[Line] = []
    for row in model.rows():
        kind = row["kind"]
        if kind == "header":
            rows.append([(_pad(row["label"], width), "muted")])
        elif kind == "team":
            arrow = "\u25be" if not row["collapsed"] else "\u25b8"
            mark = "\u258c" if row["key"] == current_key and selected else " "
            text = f"{mark}{arrow} {row['label']}"
            count = f" ({row['count']})"
            segs = [(text, "selected" if mark.strip() else "primary"),
                    (count, "muted")]
            rows.append(_fit_line(segs, width))
        elif kind == "agent":
            sym = model.state_symbol(row["state"])
            mark = "\u258c" if row["key"] == current_key and selected else " "
            indent = "  " * row.get("indent", 0)
            name_style = "selected" if mark.strip() else "normal"
            segs = [
                (mark, "primary"),
                (f"{indent}{sym} ", _state_style(row["state"])),
                (row["name"], name_style),
            ]
            if width >= 30:
                used = len(mark) + len(indent) + 2 + len(row["name"])
                word = model.state_word(row["state"])
                if used + len(word) + 1 <= width:
                    segs.append((" " * (width - used - len(word)), "normal"))
                    segs.append((word, "muted"))
            rows.append(_fit_line(segs, width))
        else:  # missing
            indent = "  " * row.get("indent", 0)
            rows.append([(_pad(f"{indent}\u00d7 {row['label']} (missing)", width), "muted")])
    while len(rows) < height:
        rows.append([(_pad("", width), "normal")])
    return rows[:height]


# ---------------------------------------------------------------- session
def _session_head(model: AppModel, width: int, height: int) -> list[Line]:
    agent = model.selected_agent()
    team = model.selected_team()
    if agent is not None:
        name = agent.get("name") or agent["agent_id"]
        line = (
            f"{name}   [{agent['kind']}] \u00b7 {agent['backend']} \u00b7 "
            f"{model.state_word(agent['state'])}"
        )
        if model.focus == "interactive":
            line += "  \u00b7  INTERACTIVE"
        head: list[Line] = [[(line, _state_style(agent["state"]))]]
        if model.focus != "interactive" and width >= 60 and agent.get("cwd"):
            head.append([(agent["cwd"], "muted")])
        return head
    if team is not None:
        head = [
            [(f"TEAM {team['name']}", "title")],
            [(f"{len(team['members'])} members \u00b7 {len(team['missing'])} missing", "muted")],
        ]
        for member in team["members"][: max(0, height // 2)]:
            head.append([
                (model.state_symbol(member["state"]) + " ", _state_style(member["state"])),
                (f"{member.get('name')} \u00b7 {model.state_word(member['state'])}", "normal"),
            ])
        return head
    return [
        [("No session selected", "muted")],
        [("Select an agent in the navigator (Enter for Interactive Focus).", "muted")],
    ]


def session_viewport(model: AppModel, width: int, height: int) -> tuple[int, int]:
    """Real (cols, rows) available to the selected agent's TUI."""
    left = _sidebar_width(width)
    right = width - left - 1
    body_h = max(1, height - 4)
    head = _session_head(model, right, body_h)
    rows = max(1, body_h - len(head) - 1)
    return right, rows


def _session(model: AppModel, width: int, height: int) -> list[Line]:
    lines: list[Line] = [
        [(text, style) for text, style in line]
        for line in _session_head(model, width, height)
    ]
    lines.append([("\u2500" * width, "border")])

    agent = model.selected_agent()
    if agent is not None and agent["state"] in TERMINAL_STATES:
        lines.append([(
            f"Agent {agent['state']}. Press d to delete, or select another agent.",
            "warning",
        )])
        return _resize(lines, height)

    out_h = max(1, height - len(lines))
    transcript = model.output_slice(out_h)
    if transcript:
        for text in transcript:
            style = "normal"
            stripped = text.strip()
            if stripped.startswith(("\u276f", "\u203a", ">", "$")):
                style = "input"
            lines.append(_fit_line([(text, style)], width))
    else:
        lines.append([("", "normal")])
        if agent is not None:
            lines.append([("\u2014 no output yet \u2014", "muted")])
    return _resize(lines, height)


def _resize(lines: list[Line], height: int) -> list[Line]:
    while len(lines) < height:
        lines.append([("", "normal")])
    return lines[:height]


def _context(model: AppModel) -> str:
    agent = model.selected_agent()
    if agent is not None:
        return f"{agent.get('name')} \u00b7 {model.state_word(agent['state'])}"
    team = model.selected_team()
    if team is not None:
        return f"team {team['name']}"
    return "navigator"


def _status_style(model: AppModel) -> str:
    return {
        "success": "success",
        "error": "error",
        "working": "working",
        "warning": "warning",
    }.get(model.status_kind, "muted")


def _state_style(state: str) -> str:
    return {
        "ready": "success",
        "working": "working",
        "waiting_input": "primary",
        "starting": "muted",
        "unknown": "muted",
        "exited": "muted",
        "error": "error",
    }.get(state, "normal")


def _fit_line(segments: list[Segment], width: int) -> Line:
    out: Line = []
    used = 0
    for text, style in segments:
        if used >= width:
            break
        chunk = text[: width - used]
        out.append((chunk, style))
        used += len(chunk)
    if used < width:
        out.append((" " * (width - used), "normal"))
    return out


# ------------------------------------------------------------------ modals
def _overlay(
    model: AppModel, base: list[Line], width: int, height: int, box: list[Segment]
) -> list[Line]:
    top = max(1, (height - len(box)) // 2)
    for i, (text, style) in enumerate(box):
        row = top + i
        if 0 <= row < len(base):
            pad = max(0, (width - len(text)) // 2)
            base[row] = [(" " * pad, "normal"), (text, style)]
    return base


def _box(title: str, body: list[str], width: int, style: str = "modal") -> list[Segment]:
    inner = max(1, width - 2)
    fill = max(0, width - len(title) - 5)
    out: list[Segment] = [("\u250c\u2500 " + title + " " + "\u2500" * fill + "\u2510", "border")]
    for text in body:
        out.append(("\u2502 " + _pad(text, inner - 1) + "\u2502", style))
    out.append(("\u2514" + "\u2500" * inner + "\u2518", "border"))
    return out


def _modal_lines(model: AppModel, width: int) -> list[Segment]:
    modal = model.modal
    assert modal is not None
    body: list[str] = []

    if modal.kind in ("help", "activity", "agent_info"):
        body.extend(_window(modal.lines, modal.index, 16))
    elif modal.kind == "palette":
        body.append(f"> {modal.fields[0].value}")
        body.append("")
        commands = model.filtered_commands()
        sel = min(modal.index, len(commands) - 1) if commands else -1
        for i, (label, _action) in enumerate(commands):
            body.append((" \u25b8 " if i == sel else "   ") + label)
    elif modal.kind in ("add_members", "remove_members"):
        field = modal.fields[0]
        if not field.options:
            body.append("(none available)")
        for i, opt in enumerate(field.options):
            check = "[x]" if field.checked[i] else "[ ]"
            pointer = "\u25b8" if i == field.cursor else " "
            body.append(f"{pointer} {check} {opt}")
    elif modal.kind == "confirm":
        body.extend(modal.lines)
        body.append("")
        left = "[ Delete ]" if modal.button == 0 else "  Delete  "
        right = "[ Cancel ]" if modal.button == 1 else "  Cancel  "
        body.append(f"        {left}      {right}")
    else:
        body.extend(_form_lines(model, modal))
        body.append("")
        body.append("      [ Enter: submit ]   [ Esc: cancel ]")

    if modal.error:
        body.append("")
        body.append(f"! {modal.error}")
    style = "error" if modal.error else "modal"
    return _box(modal.title, body, width, style)


def _window(lines: list[str], index: int, height: int) -> list[str]:
    if len(lines) <= height:
        return lines
    start = max(0, min(index, len(lines) - height))
    return lines[start : start + height]


def _form_lines(model: AppModel, modal: Modal) -> list[str]:
    out: list[str] = []
    for i, f in enumerate(modal.fields):
        marker = "\u25b8" if i == modal.focus else " "
        if f.type == "text":
            value = f.value if f.editable else f"{f.value} (fixed)"
            out.append(f"{marker} {f.label}: {value}")
        elif f.type == "select":
            current = f.options[min(f.index, len(f.options) - 1)] if f.options else "(none)"
            if f.name == "backend":
                desc = f"   {BACKEND_DESC.get(current, '')}"
            elif f.name == "kind":
                desc = f"   {model.command_for_kind(current)}"
            else:
                desc = ""
            out.append(f"{marker} {f.label}:  \u2039 {current} \u203a{desc}")
        elif f.type == "checklist":
            out.append(f"{marker} {f.label}:")
            for j, opt in enumerate(f.options):
                check = "[x]" if f.checked[j] else "[ ]"
                pointer = "\u25b8" if i == modal.focus and j == f.cursor else " "
                out.append(f"    {pointer} {check} {opt}")
    return out
