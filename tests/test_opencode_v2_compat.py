"""OpenCode v2 compatibility: sessions, delivery, interactions, state, persistence.

These tests exercise the behaviours from the handoff that used to lose messages
or sessions: background-tab focus, ``--session``/``--continue``, API delivery,
flag translation, serve reuse, long prompts, permissions/forms and the v2
activity/state contract. They run against a local fake ``serve`` and never touch
the user's OpenCode config, state or live agents.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import Controller, OpenCodeHarness, notices
from crewhall import activity
from crewhall.archive import read_archived
from crewhall.backends.tmux import TmuxBackend
from crewhall.harness import versions
from crewhall.opencode_link import OpenCodeLink
from crewhall.persistence import StateStore

from .fake_opencode_v2 import PASSWORD, FakeOpenCodeV2, start_server
from .support import FakeHarness, FakeSession

SID = "ses_mine"
OTHER = "ses_other"


def screen(name: str = "tabs-off-idle", size: str = "120x40") -> str:
    path = os.path.join(os.path.dirname(__file__), "fixtures", "opencode_v2", size,
                        name + ".txt")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.server, self.port = start_server()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        FakeOpenCodeV2.reset()
        FakeOpenCodeV2.sessions[SID] = {
            "id": SID, "agent": "build",
            "model": {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"},
            "time": {"created": 1000, "updated": 0},
            "location": {"directory": "/tmp/work"},
        }
        self.controller = Controller(adopt=False)
        self.controller._V2_AGENT_WAIT = 0.0  # a missing agent fails fast in tests
        self.link = OpenCodeLink(self.port, PASSWORD, api="v2")
        notices.clear()
        self.addCleanup(notices.clear)
        patcher = mock.patch("crewhall.watch.notify")
        patcher.start()
        self.addCleanup(patcher.stop)

    def harness(self, name: str = "h", *, link=True) -> OpenCodeHarness:
        h = OpenCodeHarness(FakeSession(screen=screen(), name=name), name=name)
        h.link = self.link if link else None
        h.conversation_id = SID
        self.controller.register_agent(h)
        return h


class FocusRule(ServerCase):
    def test_a_background_tab_never_hijacks_the_conversation(self):
        h = self.harness()
        self.controller._on_opencode_event_v2(
            h, {"type": "session.execution.started", "data": {"sessionID": OTHER}})
        self.assertEqual(h.conversation_id, SID)
        self.assertNotIn("prompt_submit", json.dumps(self.controller._hook_events))

    def test_viewed_adopts_the_session_the_user_opened(self):
        FakeOpenCodeV2.sessions[OTHER] = {"id": OTHER, "time": {"created": 2000}}
        h = self.harness()
        self.controller._on_opencode_event_v2(
            h, {"type": "session.viewed", "data": {"sessionID": OTHER, "idle": False}})
        self.assertEqual(h.conversation_id, OTHER)

    def test_fork_is_adopted_and_leaves_the_children_set(self):
        h = self.harness()
        self.controller._opencode_children[h.agent_id] = {"ses_fork"}
        self.controller._on_opencode_event_v2(
            h, {"type": "session.forked",
                "data": {"sessionID": "ses_fork", "parentID": SID}})
        self.assertEqual(h.conversation_id, "ses_fork")
        self.assertNotIn("ses_fork", self.controller._opencode_children[h.agent_id])

    def test_deleting_the_tracked_session_clears_it_with_a_notice(self):
        h = self.harness()
        self.controller._on_opencode_event_v2(
            h, {"type": "session.deleted", "data": {"sessionID": SID}})
        self.assertIsNone(h.conversation_id)
        self.assertTrue(any("deleted" in n["title"].lower() for n in notices.pending()))

    def test_moved_renamed_and_selected_update_metadata_and_config(self):
        h = self.harness()
        self.controller._on_opencode_event_v2(
            h, {"type": "session.moved",
                "data": {"sessionID": SID, "location": {"directory": "/new/dir"}}})
        self.assertEqual(h.session.spec.cwd, "/new/dir")
        self.controller._on_opencode_event_v2(
            h, {"type": "session.renamed", "data": {"sessionID": SID, "title": "Tarea"}})
        self.assertEqual(h.session_title, "Tarea")
        self.controller._on_opencode_event_v2(
            h, {"type": "session.agent.selected",
                "data": {"sessionID": SID, "agent": "plan"}})
        self.assertEqual(self.controller._v2_config[h.agent_id]["agent"], "plan")
        self.controller._on_opencode_event_v2(
            h, {"type": "session.model.selected",
                "data": {"sessionID": SID,
                         "model": {"providerID": "p", "id": "m", "variant": "high"}}})
        cfg = self.controller._v2_config[h.agent_id]
        self.assertEqual(cfg["model"], "p/m")
        self.assertEqual(cfg["model_variant"], "high")

    def test_history_does_not_retire_a_v2_conversation_on_the_home_screen(self):
        h = self.harness()
        h.session.screen = screen("home-no-tabs")
        h.link.history = lambda *a, **k: {"available": True, "messages": [], "total": 3}
        h.conversation_resolver = lambda: None
        h.history()
        self.assertEqual(h.conversation_id, SID)


class SessionFlags(ServerCase):
    def test_session_flag_is_validated_and_adopted(self):
        FakeOpenCodeV2.sessions["ses_old"] = {
            "id": "ses_old", "time": {"created": 500},
            "location": {"directory": "/tmp/work"},
        }
        sid, cleaned, missing = self.controller._resolve_v2_session(
            self.link, ["--session", "ses_old", "--model", "p/m"], "/tmp/work", "a1")
        self.assertEqual(sid, "ses_old")
        self.assertIsNone(missing)
        self.assertNotIn("--session", cleaned)

    def test_missing_session_is_not_opened_empty(self):
        sid, _cleaned, missing = self.controller._resolve_v2_session(
            self.link, ["--session", "ses_gone"], "/tmp/work", "a1")
        self.assertIsNone(sid)
        self.assertEqual(missing, "ses_gone")
        self.assertTrue(any("not found" in n["title"].lower() for n in notices.pending()))

    def test_continue_resolves_the_newest_session_of_the_cwd(self):
        FakeOpenCodeV2.sessions["ses_new"] = {
            "id": "ses_new", "time": {"created": 4000},
            "location": {"directory": "/tmp/work"},
        }
        sid, cleaned, _missing = self.controller._resolve_v2_session(
            self.link, ["--continue"], "/tmp/work", "a1")
        self.assertEqual(sid, "ses_new")
        self.assertNotIn("--continue", cleaned)
        self.assertIn("/api/session", FakeOpenCodeV2.paths())

    def test_the_gate_is_the_api_not_the_presence_of_a_link(self):
        with mock.patch.object(self.controller, "_opencode_api", return_value="v2"), \
                mock.patch.object(self.controller, "_resolve_provider_command",
                                  return_value=(["opencode"], None, False)):
            command = self.controller._launch_command(
                "opencode",
                ["--model", "p/m", "--agent", "x", "--session-id", "s", "--resume", "r",
                 "--port", "1", "--hostname", "h", "--variant", "v", "--bogus", "z"],
                None, None, server_url=None,
            )
        joined = " ".join(command)
        for flag in ("--model", "--agent", "--session-id", "--resume", "--port",
                     "--hostname", "--variant", "--bogus"):
            self.assertNotIn(flag, joined)

    def test_invalid_session_flags_are_not_in_the_v2_set(self):
        for flag in ("--session-id", "--resume", "-r"):
            self.assertNotIn(flag, self.controller._V2_SESSION_FLAGS)
        self.assertIn("--session", self.controller._V2_SESSION_FLAGS)


class ServeHandling(ServerCase):
    def test_an_orphan_serve_is_adopted_instead_of_rebinding(self):
        h = self.harness(link=False)
        h.conversation_id = None
        self.controller.track_conversations = True
        self.controller._agent_args[h.agent_id] = []
        with mock.patch.object(self.controller, "_start_opencode_server") as start, \
                mock.patch.object(self.controller, "_attach_link"):
            self.controller._reattach_opencode_link(
                h, {"port": self.port, "password": PASSWORD, "api": "v2"}, "/tmp/work")
        start.assert_not_called()
        adopted = self.controller._opencode_servers[h.agent_id]
        self.assertEqual(adopted.pid, 4242)
        self.assertEqual(self.controller.agent_root_pids(), {4242} | {h.session.pid})

    def test_failed_serve_never_falls_back_to_the_managed_service(self):
        with mock.patch.object(self.controller, "_opencode_api", return_value="v2"):
            args = self.controller._v2_serve_fallback(
                "opencode", ["--agent", "x"], None, "a1")
        self.assertIn("--standalone", args)
        self.assertTrue(any("private server" in n["title"] for n in notices.pending()))

    def test_detect_api_does_not_downgrade_a_transient_401(self):
        import urllib.error

        FakeOpenCodeV2.info_status = 401
        with self.assertRaises(urllib.error.HTTPError):
            OpenCodeLink.detect_api(self.port, PASSWORD)


class LongPrompt(unittest.TestCase):
    def test_tmux_pastes_40kb_multiline_through_a_buffer(self):
        backend = TmuxBackend(socket="crewhall-test")
        backend.pane_id = "%1"
        calls: list[tuple[tuple, dict]] = []
        backend._check = lambda *a, **k: calls.append((a, k)) or mock.Mock(returncode=0)
        text = ("línea de prueba con acentos ✓\n" * 1800)[:40 * 1024]
        backend.write(text)
        loaded = "".join(k.get("input_text", "") for a, k in calls if "load-buffer" in a)
        self.assertEqual(loaded, text)
        self.assertTrue(any("paste-buffer" in a and "-p" in a for a, _k in calls))
        self.assertGreater(len([a for a, _k in calls if "load-buffer" in a]), 1)
        # The prompt never appears in an argv (it would leak into daemon.log).
        self.assertFalse(any(text[:40] in " ".join(a) for a, _k in calls))


class Interactions(ServerCase):
    def test_permission_v2_shape_is_rendered(self):
        h = self.harness()
        props = {"id": "per_1", "sessionID": "ses_sub", "action": "shell",
                 "resources": ["echo permission-test"], "save": False, "source": "user"}
        self.controller._on_opencode_event_v2(h, {"type": "permission.asked", "data": props})
        item = self.controller.interactions.pending(h.agent_id)[0]
        self.assertEqual(item.data["title"], "shell")
        self.assertIn("echo permission-test", item.data["detail"])
        self.assertEqual(item.ref["sessionID"], "ses_sub")

    def test_permission_reply_uses_decision_and_the_request_session(self):
        FakeOpenCodeV2.sessions["ses_sub"] = {"id": "ses_sub", "time": {"created": 1}}
        FakeOpenCodeV2.permissions = [{
            "id": "per_1", "sessionID": "ses_sub", "action": "shell",
            "resources": ["echo permission-test"],
        }]
        h = self.harness()
        self.controller._on_opencode_event_v2(h, {
            "type": "permission.asked", "data": FakeOpenCodeV2.permissions[0]})
        item = self.controller.interactions.pending(h.agent_id)[0]
        self.controller.respond_interaction(item.interaction_id, {"reply": "once"})
        self.assertIn("/api/session/ses_sub/permission/per_1/reply",
                      FakeOpenCodeV2.paths())
        body = FakeOpenCodeV2.bodies("/reply")[-1]
        self.assertEqual(body, {"decision": "once"})

    def test_form_created_is_visible_and_answered_by_field_key(self):
        FakeOpenCodeV2.sessions["ses_sub"] = {"id": "ses_sub", "time": {"created": 1}}
        form = {"id": "frm_1", "sessionID": "ses_sub", "title": "Need input",
                "fields": [{"key": "name", "title": "What is your name?",
                            "type": "string", "required": True}]}
        h = self.harness()
        FakeOpenCodeV2.forms = [dict(form)]
        self.controller._on_opencode_event_v2(h, {"type": "form.created", "data": {"form": form}})
        item = self.controller.interactions.pending(h.agent_id)[0]
        self.assertEqual(item.data["questions"][0]["key"], "name")
        self.controller.respond_interaction(item.interaction_id, {"answers": [["Alice"]]})
        self.assertIn("/api/session/ses_sub/form/frm_1/reply", FakeOpenCodeV2.paths())
        self.assertEqual(FakeOpenCodeV2.bodies("/reply")[-1], {"answer": {"name": "Alice"}})

    def test_unknown_agent_is_not_reported_as_applied(self):
        report = self.controller._configure_v2_session(
            "a1", "opencode", self.link, ["--agent", "nope"], session_id=SID)
        self.assertFalse(report["agent_applied"])
        self.assertIn("--agent", report["ignored_flags"])
        self.assertTrue(any("could not apply" in n["title"] for n in notices.pending()))

    def test_agent_that_the_server_loads_late_is_still_applied(self):
        # A freshly started `serve` answers /api/info before it has loaded the
        # user's agents: the first /api/agent lists only the built-ins.
        late = {"id": "ejecutor", "name": "ejecutor", "mode": "all"}
        calls = []
        original = type(self.link).get_json

        def get_json(link, path, *a, **kw):
            if path == "/api/agent":
                calls.append(path)
                if len(calls) < 3:
                    return {"data": [x for x in FakeOpenCodeV2.agents if x["id"] != "ejecutor"]}
            return original(link, path, *a, **kw)

        FakeOpenCodeV2.agents = [*FakeOpenCodeV2.agents, late]
        self.controller._V2_AGENT_WAIT = 5.0
        with mock.patch.object(type(self.link), "get_json", get_json), \
                mock.patch("crewhall.services.agents.time.sleep"):
            report = self.controller._configure_v2_session(
                "a1", "opencode", self.link, ["--agent", "ejecutor"], session_id=SID)
        self.assertTrue(report["agent_applied"], report)
        self.assertGreaterEqual(len(calls), 3)

    def test_unknown_model_is_not_reported_as_applied(self):
        FakeOpenCodeV2.models = [{"id": "real", "modelID": "real", "providerID": "p"}]
        report = self.controller._configure_v2_session(
            "a1", "opencode", self.link, ["--model", "nope"], session_id=SID)
        self.assertFalse(report["model_applied"])
        self.assertTrue(report["error"])

    def test_model_ref_accepts_variant_display_name_and_alias(self):
        FakeOpenCodeV2.models = [{
            "id": "fast", "modelID": "fast", "providerID": "acme-go",
            "canonical": "acme-go", "name": "Fast Model", "variants": ["low", "high"],
        }]
        self.assertEqual(
            self.controller._v2_model_ref(self.link, "Fast Model#high"),
            {"providerID": "acme-go", "id": "fast", "variant": "high"})
        self.assertEqual(
            self.controller._v2_model_ref(self.link, "acme/fast"),
            {"providerID": "acme-go", "id": "fast"})
        self.assertEqual(self.controller._v2_model_ref(self.link, "nope"), {})

    def test_model_ref_prefers_the_exact_provider_over_a_prefix(self):
        # The same model under "opencode" and "opencode-go": "opencode" is a
        # substring of "opencode-go", so a fuzzy pass must not pick it first.
        FakeOpenCodeV2.models = [
            {"id": "m", "modelID": "m", "providerID": "opencode"},
            {"id": "m", "modelID": "m", "providerID": "opencode-go"},
        ]
        self.assertEqual(self.controller._v2_model_ref(self.link, "opencode-go/m"),
                         {"providerID": "opencode-go", "id": "m"})
        self.assertEqual(self.controller._v2_model_ref(self.link, "opencode/m"),
                         {"providerID": "opencode", "id": "m"})


class ActivityV2(ServerCase):
    def setUp(self):
        super().setUp()
        FakeOpenCodeV2.messages[SID] = [
            {"id": "m1", "type": "user", "text": "run it", "time": {"created": 1000}},
            {"id": "m2", "type": "assistant", "time": {"created": 2000},
             "model": {"providerID": "p", "id": "m"},
             "content": [
                 {"type": "text", "text": "working"},
                 {"type": "tool", "name": "bash", "id": "call_1",
                  "state": {"status": "running", "input": {"command": "sleep 30"},
                            "content": [{"type": "text", "text": "partial output"}]}},
             ]},
        ]

    def test_snapshot_reads_v2_messages(self):
        snap = activity.opencode_snapshot(self.link, SID)
        self.assertEqual(snap["model"], "m")
        self.assertEqual(snap["tool"]["name"], "bash")
        self.assertIn("sleep", snap["tool"]["brief"])

    def test_running_tools_and_process_output_read_state_content(self):
        running = self.link.running_tools(SID)
        self.assertEqual(running["sleep 30"]["output"], "partial output")
        h = self.harness()
        out = self.controller.process_output(h.agent_id, call="call_1")
        self.assertEqual(out["output"], "partial output")

    def test_agent_status_label_uses_the_v2_tool(self):
        h = self.harness()
        h.session.screen = screen("working-seconds")
        with mock.patch.object(self.controller, "_agent_root", return_value=os.getpid()):
            summary = self.controller.agent_summary(h)
        self.assertEqual(summary["activity"]["kind"], "shell")
        self.assertEqual(summary["activity"]["label"], "Running command")


class CreateAgentSession(ServerCase):
    """``opencode --session``/``--continue`` reach conversation_id before launch."""

    def _create(self, args):
        FakeOpenCodeV2.sessions["ses_old"] = {
            "id": "ses_old", "time": {"created": 500},
            "location": {"directory": "/tmp/work"},
        }
        FakeOpenCodeV2.sessions["ses_new"] = {
            "id": "ses_new", "time": {"created": 4000},
            "location": {"directory": "/tmp/work"},
        }
        captured: dict[str, object] = {}

        class _Session(FakeSession):
            def __init__(self, backend, spec, session_id=None):
                super().__init__(screen=screen(), name=spec.name)
                self.spec = spec
                if session_id:
                    self.session_id = session_id

            def start(self):
                captured["command"] = self.spec.command

        def launch(kind, extra, conversation=None, port=None, mcp_args=None,
                   host=None, resume=None, enforce=True, server_url=None):
            captured["launch"] = {
                "args": list(extra), "conversation": conversation,
                "resume": resume, "server_url": server_url,
            }
            return ["opencode", "--server", server_url or "", "--session", resume or ""]

        with mock.patch.object(self.controller, "_open_opencode_link",
                               return_value=(self.link, None)), \
                mock.patch("crewhall.services.agents.InteractiveSession", _Session), \
                mock.patch.object(self.controller, "_attach_version"), \
                mock.patch.object(self.controller, "_attach_link"), \
                mock.patch.object(self.controller, "_persist"), \
                mock.patch.object(self.controller, "_ensure_control_file"), \
                mock.patch.object(self.controller, "_isolate_opencode_tabs"), \
                mock.patch.object(self.controller, "_launch_command", side_effect=launch), \
                mock.patch("crewhall.services.agents.settings.provider_enabled",
                           return_value=True), \
                mock.patch("crewhall.services.agents.settings.provider_args",
                           return_value=[]), \
                mock.patch("crewhall.services.agents.settings.provider_env",
                           return_value={}):
            harness = self.controller.create_agent(
                "opencode", name="ses", cwd="/tmp/work", args=args)
        return harness, captured

    def test_session_sets_conversation_id_and_is_opened(self):
        harness, captured = self._create(["--session", "ses_old"])
        self.assertEqual(harness.conversation_id, "ses_old")
        self.assertEqual(captured["launch"]["resume"], "ses_old")
        self.assertNotIn("--session", captured["launch"]["args"])

    def test_continue_resolves_to_the_newest_and_rewrites_the_flag(self):
        harness, captured = self._create(["--continue"])
        self.assertEqual(harness.conversation_id, "ses_new")
        self.assertEqual(captured["launch"]["resume"], "ses_new")
        self.assertNotIn("--continue", captured["launch"]["args"])

    def test_missing_session_starts_fresh_with_a_notice(self):
        harness, captured = self._create(["--session", "ses_gone"])
        self.assertNotEqual(harness.conversation_id, "ses_gone")
        self.assertTrue(any("not found" in n["title"].lower() for n in notices.pending()))
        self.assertNotIn("ses_gone", str(captured["launch"]))

    def test_tabs_helper_merges_instead_of_replacing_the_user_config(self):
        merged = self.controller._v2_tabs_off_content('{"tabs":{"scope":"cwd"}}')
        self.assertEqual(json.loads(merged)["tabs"], {"mode": "off", "scope": "cwd"})


class Persistence(ServerCase):
    def test_state_snapshot_stores_launch_session_config_and_env_keys_only(self):
        h = self.harness()
        self.controller._agent_args[h.agent_id] = ["--agent", "build"]
        self.controller._base_env[h.agent_id] = {"SECRET_TOKEN": "s3cret", "TMPDIR": "/tmp"}
        self.controller._agent_launch[h.agent_id] = {
            "resolved_command": "/opt/opencode", "version": "opencode v2.0.29",
            "args": ["--agent", "build"], "session": SID,
            "server": {"port": self.port, "api": "v2"},
        }
        self.controller._v2_config[h.agent_id] = {
            "model": "p/m", "agent": "build", "session_id": SID,
        }
        store = StateStore(os.path.join(tempfile.mkdtemp(), "state.json"))
        self.addCleanup(shutil.rmtree, os.path.dirname(store.path), True)
        agent = store.snapshot(self.controller)["agents"][0]
        self.assertEqual(agent["env_keys"], ["SECRET_TOKEN", "TMPDIR"])
        self.assertNotIn("env", agent)
        self.assertNotIn("s3cret", json.dumps(agent))
        self.assertEqual(agent["launch"]["version"], "opencode v2.0.29")
        self.assertEqual(agent["session_config"]["model"], "p/m")

    def test_archive_includes_args_and_session_config(self):
        h = self.harness()
        self.controller._agent_args[h.agent_id] = ["--agent", "build"]
        self.controller._v2_config[h.agent_id] = {"model": "p/m", "agent": "build"}
        self.controller.archive_agent(h, ["t"])
        record = read_archived(h.agent_id)
        self.assertEqual(record["args"], ["--agent", "build"])
        self.assertEqual(record["session_config"]["model"], "p/m")

    def test_env_scrubs_inherited_opencode_and_agent_markers(self):
        with mock.patch.dict(os.environ, {
                "OPENCODE_SESSION_ID": "parent", "OPENCODE_PASSWORD": "pw",
                "AI_AGENT": "1", "AGENT": "x"}, clear=False):
            env = self.controller._build_agent_env("a1", "n", [], None)
        self.assertEqual(env["OPENCODE_SESSION_ID"], "")
        self.assertEqual(env["OPENCODE_PASSWORD"], "")
        self.assertEqual(env["AI_AGENT"], "")
        self.assertNotIn("OPENCODE_SERVER_PASSWORD", env)

    def test_tabs_off_merges_and_prompt_history_is_not_copied(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        real = os.path.join(tmp, "real", "opencode", "latest", "tui")
        os.makedirs(real)
        open(os.path.join(real, "prompt-history.jsonl"), "w").write('{"prompt":"secret"}\n')
        open(os.path.join(real, "frecency.jsonl"), "w").write("secret")
        open(os.path.join(tmp, "real", "opencode", "kv.json"), "w").write('{"a":1}')
        env: dict[str, str] = {"OPENCODE_CLI_CONFIG_CONTENT": '{"theme":"x","tabs":{"scope":"cwd"}}'}
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(tmp, "real")}), \
                mock.patch("crewhall.services.agents.paths.state_dir",
                           return_value=os.path.join(tmp, "crewhall")):
            self.controller._isolate_opencode_tabs(env, "a1", self.link, api="v2")
        mine = os.path.join(env["XDG_STATE_HOME"], "opencode")
        self.assertFalse(os.path.exists(os.path.join(mine, "latest", "tui",
                                                     "prompt-history.jsonl")))
        self.assertEqual(env["OPENCODE_CLI_CONFIG_CONTENT"],
                         '{"tabs":{"mode":"off","scope":"cwd"},"theme":"x"}')
        self.assertEqual(open(os.path.join(mine, "kv.json")).read(), '{"a":1}')

    def test_remove_agent_cleans_its_opencode_state(self):
        h = self.harness()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        state_home = os.path.join(tmp, "opencode-state", h.agent_id)
        os.makedirs(state_home)
        with mock.patch("crewhall.services.agents.paths.state_dir", return_value=tmp), \
                mock.patch.object(h, "stop"), \
                mock.patch.object(self.controller, "archive_agent",
                                  return_value={}), \
                mock.patch.object(self.controller, "_persist"):
            self.controller.remove_agent(h.agent_id)
        self.assertFalse(os.path.exists(state_home))


class ReviveResume(unittest.TestCase):
    def test_claude_revive_resumes_its_conversation(self):
        controller = Controller(adopt=False)
        h = FakeHarness("claude-old")
        h.kind = "claude"
        h.conversation_id = "11111111-1111-4111-8111-111111111111"
        controller.register_agent(h)
        h.session.status = mock.Mock(alive=False)
        with mock.patch.object(controller, "_reopen_opencode_link", return_value=(None, None)), \
                mock.patch("crewhall.services.persistence.InteractiveSession"), \
                mock.patch.object(controller, "_launch_command",
                                  return_value=["claude"]) as launch, \
                mock.patch.object(controller, "_build_agent_env", return_value={}), \
                mock.patch.object(controller, "_attach_link"), \
                mock.patch.object(controller, "_attach_version"), \
                mock.patch.object(controller, "_persist"), \
                mock.patch.object(controller, "_ensure_control_file"), \
                mock.patch("crewhall.services.persistence.get_harness",
                           return_value=FakeHarness):
            try:
                controller.restart_agent(h.agent_id)
            except Exception:  # noqa: BLE001 - only the launch call matters
                pass
        self.assertEqual(launch.call_args.kwargs.get("resume"), h.conversation_id)


class MixedVersions(unittest.TestCase):
    def test_v2_refuses_to_share_the_db_with_a_live_v1(self):
        controller = Controller(adopt=False)
        legacy = FakeHarness("viejo")
        legacy.kind = "opencode"
        legacy.session.status = mock.Mock(alive=True)
        controller.register_agent(legacy)
        controller._opencode_links[legacy.agent_id] = {"api": "v1"}
        with mock.patch.object(controller, "_opencode_api", return_value="v2"):
            with self.assertRaises(ValueError) as caught:
                controller._assert_no_mixed_opencode_db("opencode")
        self.assertIn("must not run concurrently", str(caught.exception))


class DoctorAndVersions(unittest.TestCase):
    def test_opencode_is_captured_at_2_0_29(self):
        self.assertEqual(versions.VALIDATED["opencode"]["captured"], "2.0.29")

    def test_doctor_warns_when_the_live_runtime_differs(self):
        from crewhall import doctor

        agents = [{"agent_id": "a1", "name": "viejo", "kind": "opencode",
                   "launch": {"version": "opencode v1.18.31"},
                   "cli": {"version": "opencode v2.0.29"}}]
        with mock.patch("crewhall.client.ping", return_value=True), \
                mock.patch("crewhall.client.Client") as client:
            client.return_value.call.return_value = {"agents": agents}
            checks = doctor.check_agent_runtime_versions()
        self.assertEqual(checks[0]["status"], "warn")
        self.assertIn("1.18.31", checks[0]["detail"])
        self.assertIn("2.0.29", checks[0]["detail"])


if __name__ == "__main__":
    unittest.main()
