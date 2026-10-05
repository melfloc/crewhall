from __future__ import annotations

import unittest

from crewhall import Controller, Status
from crewhall.ui import (
    AppModel,
    LocalControl,
    render,
    render_text,
    session_viewport,
    translate_sequence,
)

from .support import FakeHarness


class FakeControl(LocalControl):
    def __init__(self, controller: Controller) -> None:
        super().__init__(controller)
        self.created: list[tuple] = []

    def create_agent(self, kind, name, backend=None, cwd=None, team=None, args=None):
        self.created.append((kind, name, backend, cwd))
        self.last_args = args
        harness = FakeHarness(name)
        self.controller.register_agent(harness)
        if team:
            try:
                self.controller.add_team_member(team, name)
            except Exception:
                pass
        return self.controller.agent_summary(harness)


class CreateAgentArgs(unittest.TestCase):
    def _model(self):
        control = FakeControl(Controller(adopt=False))
        return control, AppModel(control, cwd="/tmp", async_ops=False)

    def _submit(self, model, args_value):
        model.open_create_agent()
        fields = {f.name: f for f in model.modal.fields}
        self.assertIn("args", fields)
        fields["name"].value = "worker"
        fields["args"].value = args_value
        model._submit_modal()

    def test_args_field_is_forwarded_to_control(self):
        control, model = self._model()
        self._submit(model, "--agent reviewer")
        self.assertEqual(control.last_args, "--agent reviewer")

    def test_empty_args_are_none(self):
        control, model = self._model()
        self._submit(model, "")
        self.assertIsNone(control.last_args)

    def test_invalid_args_keep_modal_open_with_error(self):
        control, model = self._model()
        self._submit(model, '--agent "oops')
        self.assertIsNotNone(model.modal)
        self.assertIn("invalid arguments", model.modal.error)
        self.assertEqual(control.created, [])


class DownControl:
    def meta(self):
        raise RuntimeError("daemon down")

    def list_agents(self):
        raise RuntimeError("daemon down")

    def list_teams(self):
        raise RuntimeError("daemon down")

    def message_history(self, agent=None, limit=None):
        raise RuntimeError("daemon down")


def field(modal, name):
    return next(f for f in modal.fields if f.name == name)


class UiBase(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = Controller(adopt=False)
        self.control = FakeControl(self.controller)
        self.model = AppModel(self.control, cwd="/tmp", async_ops=False)
        self.model.refresh()

    def _agent(self, name) -> FakeHarness:
        for harness in self.controller.agents.all():
            if harness.name == name:
                return harness
        raise AssertionError(f"agent {name} not found")

    def _add_agent(self, name, kind="opencode"):
        self.model.create_agent(name, kind)
        self.model.refresh()
        self.model.focus = "nav"

    def _select(self, name, kind="agent"):
        self.model.focus = "nav"
        for index, row in enumerate(self.model.rows()):
            if row["kind"] == kind and row["label"] == name:
                self.model.cursor = index
                return
        raise AssertionError(f"row {kind} {name} not found")

    def _enter_interactive(self, name):
        self._select(name, kind="agent")
        self.model.handle_key("enter")
        self.assertEqual(self.model.focus, "interactive")


class InteractiveFocus(UiBase):
    def setUp(self):
        super().setUp()
        self._add_agent("a")

    def test_enter_enters_interactive_and_exit_combo_leaves(self):
        self._select("a", kind="agent")
        self.model.handle_key("enter")
        self.assertEqual(self.model.focus, "interactive")
        self.assertEqual(self.model.selected_agent()["name"], "a")
        self.model.handle_key("exit_interactive")
        self.assertEqual(self.model.focus, "nav")
        self.assertEqual(self.model.selected_agent()["name"], "a")

    def test_ctrl_b_is_forwarded_to_agent(self):
        self._enter_interactive("a")
        self.model.handle_key("ctrl_b")
        self.assertEqual(self.model.focus, "interactive")
        self.assertIn("CTRL_B", self._agent("a").session.keys)

    def test_printable_characters_go_to_agent(self):
        self._enter_interactive("a")
        self.model.handle_key("h")
        self.model.handle_key("i")
        self.assertEqual(self._agent("a").session.writes, ["h", "i"])

    def test_special_keys_go_to_agent(self):
        self._enter_interactive("a")
        for ui_key, backend in (
            ("enter", "ENTER"), ("backspace", "BACKSPACE"), ("delete", "DELETE"),
            ("up", "UP"), ("down", "DOWN"), ("left", "LEFT"), ("right", "RIGHT"),
            ("home", "HOME"), ("end", "END"), ("tab", "TAB"), ("esc", "ESC"),
            ("ctrl_c", "CTRL_C"), ("ctrl_d", "CTRL_D"), ("pgup", "PAGE_UP"),
            ("pgdn", "PAGE_DOWN"),
        ):
            self.model.handle_key(ui_key)
            self.assertIn(backend, self._agent("a").session.keys)

    def test_slash_command_is_forwarded_verbatim(self):
        self._enter_interactive("a")
        for char in "/clear":
            self.model.handle_key(char)
        self.model.handle_key("enter")
        writes = "".join(self._agent("a").session.writes)
        self.assertEqual(writes, "/clear")
        self.assertIn("ENTER", self._agent("a").session.keys)

    def test_no_external_composer_in_view(self):
        self._enter_interactive("a")
        screen = "\n".join(render_text(self.model, 100, 30))
        self.assertIn("INTERACTIVE", screen)
        self.assertNotIn("Type a message", screen)


class EscapeSequences(unittest.TestCase):
    def test_exit_sequence_maps_to_exit_interactive(self):
        self.assertEqual(translate_sequence(b"\x1b\x02"), ["exit_interactive"])

    def test_navigation_sequences(self):
        self.assertEqual(translate_sequence(b"\x1bJ"), ["interactive_prev"])
        self.assertEqual(translate_sequence(b"\x1bK"), ["interactive_next"])

    def test_alt_key_is_forwarded(self):
        self.assertEqual(translate_sequence(b"\x1b\x62"), ["esc", "b"])

    def test_plain_and_control_bytes(self):
        self.assertEqual(translate_sequence(b"a"), ["a"])
        self.assertEqual(translate_sequence(b"\x03"), ["ctrl_c"])
        self.assertEqual(translate_sequence(b"\x02"), ["ctrl_b"])
        self.assertEqual(translate_sequence(b"\r"), ["enter"])


class InteractiveNavigation(UiBase):
    def setUp(self):
        super().setUp()
        for n in ("a", "b", "c"):
            self._add_agent(n)
        self.model.create_team("fiscal", ["a", "b", "c"])
        self.model.refresh()
        self.model.focus = "nav"

    def test_next_and_prev_wrap_around(self):
        self._select("a", kind="agent")
        self.model.handle_key("enter")  # interactive, viewing a
        self.assertEqual(self.model.focus, "interactive")
        self.assertEqual(self.model.selected_agent()["name"], "a")
        self.model.handle_key("interactive_next")
        self.assertEqual(self.model.selected_agent()["name"], "b")
        self.assertEqual(self.model.focus, "interactive")
        self.model.handle_key("interactive_next")
        self.assertEqual(self.model.selected_agent()["name"], "c")
        self.model.handle_key("interactive_next")
        self.assertEqual(self.model.selected_agent()["name"], "a")  # wrap
        self.model.handle_key("interactive_prev")
        self.assertEqual(self.model.selected_agent()["name"], "c")  # wrap back

    def test_navigation_does_not_send_input(self):
        self._select("a", kind="agent")
        self.model.handle_key("enter")
        before = list(self._agent("a").session.writes)
        self.model.handle_key("interactive_next")
        self.model.handle_key("interactive_prev")
        self.assertEqual(self._agent("a").session.writes, before)
        self.assertEqual(self._agent("b").session.writes, [])

    def test_single_agent_navigation_is_noop(self):
        model = AppModel(FakeControl(Controller(adopt=False)), cwd="/tmp", async_ops=False)
        model.create_agent("only", "opencode")
        model.refresh()
        model.focus = "nav"
        for i, r in enumerate(model.rows()):
            if r["kind"] == "agent":
                model.cursor = i
        model.handle_key("enter")
        model.handle_key("interactive_next")
        model.handle_key("interactive_prev")
        self.assertEqual(model.selected_agent()["name"], "only")
        self.assertEqual(model.focus, "interactive")

    def test_exited_agent_can_be_viewed(self):
        from crewhall import Status

        self._agent("b").session.status = Status.EXITED
        self._agent("b").session.exit_code = 0
        self.model.refresh()
        self._select("a", kind="agent")
        self.model.handle_key("enter")
        self.model.handle_key("interactive_next")
        # Viewing an exited agent must not crash and must not stay interactive
        # on a dead process.
        self.model.refresh()
        row = self.model.selected_agent()
        self.assertIsNotNone(row)

    def test_deleted_agent_navigation(self):
        self._select("a", kind="agent")
        self.model.handle_key("enter")
        self.model.remove_agent("b")
        self.model.refresh()
        self.model.handle_key("interactive_next")
        self.assertIn(self.model.selected_agent()["name"], ("a", "c"))


class InteractiveSafety(UiBase):
    def setUp(self):
        super().setUp()
        self._add_agent("a")
        self._add_agent("b")
        self.model.create_team("fiscal", ["a"])
        self.model.refresh()
        self.model.focus = "nav"

    def test_admin_keys_do_nothing_in_interactive(self):
        before_agents = {a["agent_id"] for a in self.model.agents}
        self._enter_interactive("a")
        for key in ("q", "d", "n", "m", "t"):
            self.model.handle_key(key)
        self.assertEqual(self.model.focus, "interactive")
        self.assertFalse(self.model.should_quit)
        self.assertIsNone(self.model.modal)
        self.assertEqual({a["agent_id"] for a in self.model.agents}, before_agents)
        self.assertEqual(len(self.model.teams), 1)

    def test_ctrl_p_does_not_open_palette_in_interactive(self):
        self._enter_interactive("a")
        self.model.handle_key("ctrl_p")
        self.assertIsNone(self.model.modal)
        self.assertIn("CTRL_P", self._agent("a").session.keys)

    def test_ctrl_c_does_not_quit_in_interactive(self):
        self._enter_interactive("a")
        self.model.handle_key("ctrl_c")
        self.assertFalse(self.model.should_quit)
        self.assertIn("CTRL_C", self._agent("a").session.keys)

    def test_ctrl_c_quits_in_navigator(self):
        self.model.focus = "nav"
        self.model.handle_key("ctrl_c")
        self.assertTrue(self.model.should_quit)


class Navigation(UiBase):
    def setUp(self):
        super().setUp()
        self._add_agent("a")
        self._add_agent("b")
        self.model.create_team("fiscal", ["a"])
        self.model.refresh()

    def test_select_team_and_agent(self):
        self._select("fiscal", kind="team")
        self.assertEqual(self.model.selected_team()["name"], "fiscal")
        self._select("a", kind="agent")
        self.assertEqual(self.model.selected_agent()["name"], "a")

    def test_enter_on_team_toggles_collapse(self):
        self._select("fiscal", kind="team")
        self.model.handle_key("enter")
        self.assertIn(self.model.selected_team()["team_id"], self.model.collapsed)
        self.model.handle_key("enter")
        self.assertNotIn(self.model.selected_team()["team_id"], self.model.collapsed)

    def test_enter_on_agent_enters_interactive(self):
        self._select("a", kind="agent")
        self.model.handle_key("enter")
        self.assertEqual(self.model.focus, "interactive")

    def test_no_teams_section_without_teams(self):
        model = AppModel(FakeControl(Controller(adopt=False)), cwd="/tmp", async_ops=False)
        model.refresh()
        labels = [r["label"] for r in model.rows()]
        self.assertIn("AGENTS", labels)
        self.assertNotIn("TEAMS", labels)

    def test_ungrouped_section(self):
        ungrouped = [a["name"] for a in self.model.ungrouped_agents()]
        self.assertEqual(ungrouped, ["b"])


class Creation(UiBase):
    def test_create_agent_selects_and_enters_interactive(self):
        self.model.focus = "nav"
        self.model.handle_key("n")
        for char in "worker":
            self.model.handle_key(char)
        self.model.handle_key("enter")
        self.model.refresh()
        self.assertEqual(self.model.selected_agent()["name"], "worker")
        self.assertEqual(self.model.focus, "interactive")

    def test_validation_empty_and_duplicate(self):
        self.model.focus = "nav"
        self.model.handle_key("n")
        self.model.handle_key("enter")
        self.assertEqual(self.model.modal.error, "name is required")
        self.model.handle_key("esc")
        self._add_agent("dup")
        self.model.handle_key("n")
        for char in "dup":
            self.model.handle_key(char)
        self.model.handle_key("enter")
        self.assertIn("already exists", self.model.modal.error)

    def test_create_team_empty_and_selected(self):
        self.model.focus = "nav"
        self.model.handle_key("t")
        for char in "fiscal":
            self.model.handle_key(char)
        self.model.handle_key("enter")
        self.model.refresh()
        self.assertEqual(self.model.selected_team()["name"], "fiscal")
        self.assertEqual(self.model.selected_team()["members"], [])


class TeamWorkflow(UiBase):
    def setUp(self):
        super().setUp()
        self.model.create_team("fiscal", [])
        self.model.refresh()
        self.model.focus = "nav"

    def test_empty_team_add_members_offers_create_agent(self):
        self._select("fiscal", kind="team")
        self.model.handle_key("a")
        self.assertEqual(self.model.modal.kind, "add_members")
        self.assertEqual(self.model.modal.fields[0].options, [])
        self.model.handle_key("enter")
        self.assertEqual(self.model.modal.kind, "create_agent")
        self.assertEqual(self.model.modal.target, self.model.selected_team()["team_id"])

    def test_new_agent_from_team_is_added_and_selected(self):
        self._select("fiscal", kind="team")
        team_id = self.model.selected_team()["team_id"]
        self.model.handle_key("n")
        self.assertEqual(self.model.modal.kind, "create_agent")
        self.assertEqual(self.model.modal.target, team_id)
        for char in "auditor":
            self.model.handle_key(char)
        self.model.handle_key("enter")
        self.model.refresh()
        self.assertIn("auditor", self.model.agent_names())
        team = next(t for t in self.model.teams if t["team_id"] == team_id)
        self.assertEqual({m["name"] for m in team["members"]}, {"auditor"})
        self.assertEqual(self.model.selected_agent()["name"], "auditor")
        self.assertEqual(self.model.focus, "interactive")

    def test_add_multiple_members_existing(self):
        self._add_agent("a")
        self._add_agent("b")
        self._select("fiscal", kind="team")
        self.model.handle_key("a")
        self.assertEqual(self.model.modal.fields[0].options, ["a", "b"])
        self.model.handle_key(" ")
        self.model.handle_key("down")
        self.model.handle_key(" ")
        self.model.handle_key("enter")
        self.model.refresh()
        team = next(t for t in self.model.teams if t["name"] == "fiscal")
        self.assertEqual({m["name"] for m in team["members"]}, {"a", "b"})

    def test_remove_members_keeps_agents(self):
        self._add_agent("a")
        self._add_agent("b")
        self.model.add_members("fiscal", ["a", "b"])
        self.model.refresh()
        self._select("fiscal", kind="team")
        self.model.handle_key("x")
        self.model.handle_key(" ")
        self.model.handle_key("down")
        self.model.handle_key(" ")
        self.model.handle_key("enter")
        self.model.refresh()
        team = next(t for t in self.model.teams if t["name"] == "fiscal")
        self.assertEqual(team["members"], [])
        self.assertEqual({a["name"] for a in self.model.agents}, {"a", "b"})
        self.assertEqual({a["name"] for a in self.model.ungrouped_agents()}, {"a", "b"})

    def test_footer_team_context_has_new_agent(self):
        self._select("fiscal", kind="team")
        hints = " ".join(self.model.footer_hints())
        self.assertIn("New Agent", hints)


class ContextActions(UiBase):
    def setUp(self):
        super().setUp()
        self._add_agent("a")
        self._add_agent("b")
        self.model.create_team("fiscal", ["a"])
        self.model.refresh()

    def test_footer_agent_context(self):
        self._select("a", kind="agent")
        hints = " ".join(self.model.footer_hints())
        self.assertIn("Interactive", hints)
        self.assertIn("Message", hints)

    def test_footer_interactive_context(self):
        self._enter_interactive("a")
        self.assertIn("Navigator", " ".join(self.model.footer_hints()))
        self.assertIn("Agent input active", self.model.footer_hints())

    def test_palette_context_aware(self):
        self._select("a", kind="agent")
        labels = [c[0] for c in self.model.available_commands()]
        self.assertIn("Delete Agent", labels)
        self.assertIn("Send Message", labels)
        model = AppModel(FakeControl(Controller(adopt=False)), cwd="/tmp", async_ops=False)
        model.refresh()
        labels2 = [c[0] for c in model.available_commands()]
        self.assertNotIn("Delete Agent", labels2)


class MessagingUi(UiBase):
    def setUp(self):
        super().setUp()
        self._add_agent("a")
        self._add_agent("b")

    def test_message_from_context(self):
        self._select("a", kind="agent")
        self.model.handle_key("m")
        self.assertEqual(field(self.model.modal, "from").value, "a")
        field(self.model.modal, "body").value = "hola"
        self.model.handle_key("enter")
        self.assertEqual(self._agent("b").session.writes, ["[from: a] hola"])


class ViewportResize(UiBase):
    def setUp(self):
        super().setUp()
        self._add_agent("a")

    def test_session_viewport_dimensions(self):
        self._select("a", kind="agent")
        self.model.handle_key("enter")  # interactive
        cols, rows = session_viewport(self.model, 100, 30)
        self.assertEqual(cols, 100 - 33 - 1)  # sidebar clamped to 33 at width 100
        self.assertEqual(rows, (30 - 4) - 1 - 1)  # body - header(1) - separator
        self.assertGreater(cols, 10)
        self.assertGreater(rows, 2)

    def test_resize_propagates_to_agent(self):
        self._enter_interactive("a")
        self.model.sync_viewport(66, 20)
        self.assertEqual(self._agent("a").session.resizes, [(66, 20)])

    def test_resize_not_sent_when_not_interactive(self):
        self.model.focus = "nav"
        self.model.sync_viewport(66, 20)
        self.assertEqual(self._agent("a").session.resizes, [])

    def test_resize_deduplicates(self):
        self._enter_interactive("a")
        self.model.sync_viewport(66, 20)
        self.model.sync_viewport(66, 20)
        self.assertEqual(self._agent("a").session.resizes, [(66, 20)])

    def test_resize_on_dimension_change(self):
        self._enter_interactive("a")
        self.model.sync_viewport(80, 24)
        self.model.sync_viewport(120, 40)
        self.model.sync_viewport(80, 24)
        self.assertEqual(
            self._agent("a").session.resizes, [(80, 24), (120, 40), (80, 24)]
        )


class AgentExitRecovery(UiBase):
    def setUp(self):
        super().setUp()
        self._add_agent("a")

    def _kill(self, code=0):
        self._agent("a").session.status = Status.EXITED
        self._agent("a").session.exit_code = code

    def test_exited_leaves_interactive_keeps_selection(self):
        self._enter_interactive("a")
        self._kill(0)
        self.model.refresh()
        self.assertEqual(self.model.focus, "nav")
        self.assertEqual(self.model.selected_agent()["name"], "a")
        self.assertIn("a", {x["name"] for x in self.model.agents})

    def test_error_leaves_interactive(self):
        self._enter_interactive("a")
        self._kill(7)
        self.model.refresh()
        self.assertEqual(self.model.focus, "nav")
        self.assertEqual(self.model.selected_agent()["state"], "error")

    def test_exited_agent_not_deleted_automatically(self):
        self._enter_interactive("a")
        self._kill(0)
        self.model.refresh()
        self.assertIn("a", self.model.agent_names())

    def test_exited_footer_shows_delete(self):
        self._enter_interactive("a")
        self._kill(0)
        self.model.refresh()
        hints = " ".join(self.model.footer_hints())
        self.assertIn("Delete", hints)
        self.assertNotIn("Interactive", hints)

    def test_enter_on_exited_opens_info_not_interactive(self):
        self._kill(0)
        self.model.refresh()
        self._select("a", kind="agent")
        self.model.handle_key("enter")
        self.assertEqual(self.model.focus, "nav")
        self.assertIsNotNone(self.model.modal)
        self.assertEqual(self.model.modal.kind, "agent_info")

    def test_no_input_after_exit(self):
        self._enter_interactive("a")
        self._kill(0)
        self.model.refresh()
        writes_before = list(self._agent("a").session.writes)
        for char in "hello":
            self.model.handle_key(char)
        self.assertEqual(self._agent("a").session.writes, writes_before)

    def test_delete_exited_agent(self):
        self._enter_interactive("a")
        self._kill(0)
        self.model.refresh()
        self._select("a", kind="agent")
        self.model.handle_key("d")
        self.assertEqual(self.model.modal.kind, "confirm")
        self.model.handle_key("y")
        self.model.refresh()
        self.assertNotIn("a", self.model.agent_names())


class CtrlCHandling(UiBase):
    def setUp(self):
        super().setUp()
        self._add_agent("a")

    def test_ctrl_c_navigator_quits(self):
        self.model.focus = "nav"
        self.model.handle_key("ctrl_c")
        self.assertTrue(self.model.should_quit)

    def test_ctrl_c_interactive_forwarded(self):
        self._enter_interactive("a")
        self.model.handle_key("ctrl_c")
        self.assertFalse(self.model.should_quit)
        self.assertIn("CTRL_C", self._agent("a").session.keys)


class OutputUi(UiBase):
    def test_follow_and_scroll(self):
        self.model.capture_cache = "\n".join(f"line-{i}" for i in range(50))
        self.assertEqual(self.model.output_slice(3), ["line-47", "line-48", "line-49"])
        self.model.scroll_output(10)
        self.assertEqual(self.model.output_slice(3), ["line-37", "line-38", "line-39"])
        self.model.output_end()
        self.assertTrue(self.model.follow)

    def test_home(self):
        self.model.capture_cache = "\n".join(f"line-{i}" for i in range(50))
        self.model.output_home()
        self.assertEqual(self.model.output_slice(2), ["line-0", "line-1"])


class Responsive(UiBase):
    def test_80x24(self):
        self._add_agent("a")
        lines = render_text(self.model, 80, 24)
        self.assertEqual(len(lines), 24)
        self.assertIn("CREWHALL", "\n".join(lines))
        self.assertNotIn("terminal too small", "\n".join(lines))

    def test_sizes(self):
        self._add_agent("a")
        for width, height in ((100, 30), (120, 40)):
            self.assertIn("CREWHALL", "\n".join(render_text(self.model, width, height)))

    def test_too_small(self):
        self.assertIn("terminal too small", "\n".join(render_text(self.model, 60, 16)))

    def test_render_segments(self):
        lines = render(self.model, 100, 30)
        self.assertTrue(all(isinstance(line, list) for line in lines))
        self.assertTrue(all(
            isinstance(seg, tuple) and len(seg) == 2 for line in lines for seg in line
        ))


class ErrorHandling(UiBase):
    def test_disconnected(self):
        model = AppModel(DownControl(), cwd="/tmp", async_ops=False)
        model.refresh()
        self.assertFalse(model.connected)
        self.assertIn("DISCONNECTED", "\n".join(render_text(model, 100, 30)))

    def test_retry_recovers(self):
        model = AppModel(DownControl(), cwd="/tmp", async_ops=False)
        model.refresh()
        model.control = self.control
        model.retry()
        self.assertTrue(model.connected)


if __name__ == "__main__":
    unittest.main()
