"""Browser regression tests for the Web UI (skipped without Playwright)."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import unittest
from urllib.parse import parse_qs, urlparse

try:  # pragma: no cover - optional dependency
    from playwright.sync_api import sync_playwright
except Exception:  # noqa: BLE001
    sync_playwright = None

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
INDEX = os.path.join(os.path.dirname(__file__), "..", "agent_terminal", "web", "static", "index.html")
AGENT = {"agent_id": "sess_a", "name": "a", "kind": "claude", "state": "waiting_input",
         "backend": "tmux", "cwd": "/x", "pid": 1, "evidence": "", "history": True}

PERMISSION = {
    "id": "sess_attn:per_1", "kind": "permission", "title": "Bash: rm -rf build",
    "detail": "Allow the agent to run this command?", "patterns": ["rm *"],
    "always": [{"tool": "Bash", "pattern": "rm *"}], "choices": ["once", "always", "reject"],
}
QUESTION = {
    "id": "sess_attn:q_1", "kind": "question",
    "questions": [{
        "header": "Database", "question": "Which database should I use?",
        "options": [{"label": "postgres", "description": "production"},
                    {"label": "sqlite", "description": "local"}],
        "multiple": False, "custom": True,
    }],
}
WORKING = {"agent_id": "sess_w", "name": "alpha", "kind": "opencode", "state": "working",
           "backend": "tmux", "cwd": "/w", "pid": 2, "evidence": "", "history": True}
READY = {"agent_id": "sess_r", "name": "beta", "kind": "claude", "state": "ready",
         "backend": "tmux", "cwd": "/r", "pid": 3, "evidence": "", "history": True}
ATTN = {"agent_id": "sess_attn", "name": "gamma", "kind": "claude", "state": "waiting_input",
        "backend": "tmux", "cwd": "/g", "pid": 4, "evidence": "", "history": True,
        "interactions": [PERMISSION, QUESTION]}
TEAM = {"team_id": "t1", "name": "Team One", "workspace": "/home/me/proj",
        "members": [{"agent_id": "sess_w", "name": "alpha", "kind": "opencode"}], "missing": []}


def msg(i: int) -> dict:
    text = f"mensaje {i}\n" + "relleno\n" * 4
    return {"role": "assistant" if i % 2 else "user", "text": text,
            "blocks": [{"type": "text", "text": text}], "at": "2026-10-03T08:00:00Z"}


SETTINGS_FIXTURE = {
    "path": "/home/me/.config/crewhall/settings.json", "exists": False,
    "providers": [
        {"kind": "claude", "enabled": True, "command": "", "default_args": "", "default_model": "",
         "env": {"ANTHROPIC_API_KEY": "••••••••", "REGION": "eu"}, "stored": True},
        {"kind": "opencode", "enabled": True, "command": "", "default_args": "", "default_model": "", "env": {}, "stored": False}],
    "items": [
        {"key": "agents.default_kind", "group": "agents", "type": "choice", "choices": ["claude", "opencode"], "value": "opencode",
         "label": "Default provider", "help": "Preselected.", "env": None, "stored": False},
        {"key": "agents.hooks", "group": "agents", "type": "bool", "value": True, "label": "Lifecycle hooks", "help": "", "env": "CREWHALL_HOOKS", "restart": True, "stored": False},
        {"key": "agents.permission_wait", "group": "agents", "type": "int", "min": 0, "max": 3600, "value": 90, "label": "Permission wait (s)", "help": "", "env": None, "stored": False},
        {"key": "security.session_ttl_hours", "group": "security", "type": "int", "min": 1, "max": 720, "value": 12, "label": "Web session lifetime (hours)", "help": "", "env": None, "stored": False},
        {"key": "security.allow_hosts", "group": "security", "type": "list", "value": ["box.local"], "label": "Extra allowed hostnames", "help": "", "env": None, "stored": False},
        {"key": "maintenance.tmp_max_mb", "group": "maintenance", "type": "int", "min": 64, "max": 65536, "value": 512, "label": "Temp dir: size cap (MB)", "help": "", "env": None, "stored": False}],
}


def rich_msg() -> dict:
    return {"role": "assistant", "text": "here is the change",
            "blocks": [
                {"type": "text", "text": "Here is a snippet:\n```python\ndef hello(name):\n    return f\"hi {name}\"  # greet\n```"},
                {"type": "tool_use", "name": "Edit",
                 "text": '{"file_path": "/x/a.py", "new_string": "-old\\n+new\\n context"}'},
                {"type": "tool_result", "text": "-removed line\n+added line\n unchanged"},
            ],
            "at": "2026-10-03T08:00:00Z"}


@unittest.skipUnless(sync_playwright and CHROMIUM, "needs playwright + chromium")
class _Browser(unittest.TestCase):
    initial_total = 450
    available = True
    wait_for_messages = True
    auto_select = True
    onboarded = True
    agents = [AGENT]
    teams: list = []
    messages: list = []
    viewport = {"width": 1300, "height": 800}

    def setUp(self):
        self.total = self.initial_total
        self.calls: list[str] = []
        self.payloads: list[dict] = []
        self.ws = None
        self.shells: list = []
        self.update: dict = {}
        self.web_sessions: list = []
        self.bundles: list = []
        self.archived: list = []
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        browser = self.pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        self.addCleanup(browser.close)
        self.page = browser.new_page(viewport=self.viewport)
        # Hermetic Service Worker: record the register() call instead of hitting
        # the network (a real /sw.js fetch would hang on the mocked origin).
        if self.onboarded:
            self.page.add_init_script("try { localStorage.setItem('at.onboarded','1'); } catch(e){}")
        self.page.add_init_script(
            "window.__swRegistered = null;\n"
            "try { Object.defineProperty(navigator, 'serviceWorker', {configurable: true, value: {\n"
            "  register: (u) => { window.__swRegistered = u; return Promise.resolve({scope: '/'}); },\n"
            "  ready: Promise.resolve({scope: '/', showNotification() {}, addEventListener() {}}),\n"
            "  controller: null, addEventListener() {}, removeEventListener() {}}}); } catch (e) {}\n")
        self.errors: list[str] = []
        self.console_errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.console_errors.append(m.text) if m.type == "error" else None)
        self.page.route(re.compile(r".*/api/op.*"), self._api)
        self.page.route("http://localhost/static/**", self._static)
        self.page.route("http://localhost/sw.js", self._swjs)
        self.page.route("http://localhost/", lambda r: r.fulfill(
            status=200, content_type="text/html", body=open(INDEX, encoding="utf-8").read()))
        self.page.route_web_socket("**/ws", self._ws)
        self.page.goto("http://localhost/")
        if self.auto_select:
            self.page.locator("#nav").get_by_text(self.agents[0]["name"], exact=True).first.wait_for()
            self.page.locator("#nav").get_by_text(self.agents[0]["name"], exact=True).first.click()
        if self.wait_for_messages and self.auto_select:
            self.page.wait_for_selector("#hist-list .msg")
        elif self.auto_select:
            self.page.wait_for_selector("#hist-status")
        else:
            self.page.locator("#nav").get_by_text(self.agents[0]["name"], exact=True).first.wait_for()
        # Tests of the send path want the composer already outside a history nav.
        self.page.evaluate("if (typeof historyList === 'function') S.histIdx = null")

    def _static(self, route):
        rel = route.request.url.split("/static/", 1)[1]
        ctype = "text/css" if rel.endswith(".css") else "text/javascript"
        with open(os.path.join(os.path.dirname(INDEX), rel), encoding="utf-8") as fh:
            route.fulfill(status=200, content_type=ctype, body=fh.read())

    def _swjs(self, route):
        with open(os.path.join(os.path.dirname(INDEX), "sw.js"), encoding="utf-8") as fh:
            route.fulfill(status=200, content_type="text/javascript", body=fh.read())

    def _ws(self, ws):
        self.ws = ws
        self._push()

    def _push(self):
        self.ws.send(json.dumps({"type": "state", "agents": self.agents, "teams": self.teams,
                                 "messages": self.messages}))

    def _api(self, route):
        if route.request.method == "GET":
            query = parse_qs(urlparse(route.request.url).query)
            body = {"op": (query.get("op") or [""])[0]}
        else:
            body = json.loads(route.request.post_data or "{}")
        self.calls.append(body.get("op"))
        self.payloads.append(body)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(self._reply(body)))

    def _reply(self, body) -> dict:
        op = body.get("op")
        if op == "agent_history":
            end = self.total if body.get("before") is None else body["before"]
            start = max(0, end - body.get("limit", 200))
            return {"ok": True, "available": self.available, "total": self.total, "start": start,
                    "conversation_id": "c1",
                    "messages": [msg(i) for i in range(start, end)] if self.available else []}
        if op == "meta_info":
            return {"ok": True, "harnesses": [{"kind": "claude"}, {"kind": "opencode"}],
                    "backends": ["tmux", "pty"]}
        if op in ("agent_interrupt", "interaction_respond", "agent_key", "agent_write",
                  "agent_stop", "team_create", "team_set_workspace", "team_remove",
                  "team_add_member", "team_remove_member", "agent_new_session"):
            return {"ok": True, "interrupted": True, "confirmed": True}
        if op == "team_up":
            return {"ok": True, "team": {"team_id": "t9", "name": (body.get("spec") or {}).get("name", "")},
                    "team_created": True, "created": ["revisor"], "existing": []}
        if op == "agent_create":
            return {"ok": True, "agent": {**AGENT, "agent_id": "sess_new", "name": body.get("name", "new")}}
        if op == "agent_processes":
            return {"ok": True, "shells": self.shells, "finished": [], "others": []}
        if op == "agent_process_output":
            return {"ok": True, "output": "", "available": False, "truncated": False}
        if op == "agent_process_signal":
            return {"ok": True, "signalled": [42], "abort_if_stuck": False}
        if op == "update_status":
            return {"ok": True, "managed": self.update.get("managed", True),
                    "version": self.update.get("version", "1.0.0"),
                    "daemon_version": self.update.get("daemon_version", "1.0.0"),
                    "restart_pending": self.update.get("restart_pending", False),
                    "rollback_available": self.update.get("rollback_available", False),
                    "channel": "release"}
        if op == "web_sessions":
            return {"ok": True, "sessions": self.web_sessions}
        if op == "agent_archive_list":
            return {"ok": True, "archived": self.archived}
        if op == "agent_archive_get":
            return {"ok": True, "found": True, "archive": {
                "agent_id": "sess_gone", "name": "old", "kind": "claude", "cwd": "/old",
                "teams": ["Team One"], "archived_at": 0,
                "summary": {"messages": 1},
                "messages": [{"sender": "sess_gone", "sender_name": "old",
                              "recipient": "sess_w", "recipient_name": "alpha", "body": "bye"}]}}
        if op == "settings_get":
            return {"ok": True, **SETTINGS_FIXTURE}
        if op in ("settings_set", "settings_reset", "provider_check", "frontend_set", "web_token_rotate",
                  "reset_apply", "reset_plan", "fs_complete", "frontend_status", "web_session_revoke"):
            if op == "settings_set":
                return {"ok": False, "error": "security.session_ttl_hours: must be between 1 and 720"} if body["changes"].get("security.session_ttl_hours") == 0 \
                    else {"ok": True, **SETTINGS_FIXTURE}
            if op == "settings_reset":
                return {"ok": True, **SETTINGS_FIXTURE}
            if op == "provider_check":
                return {"ok": True, "providers": [
                    {"kind": k, "ok": k != "opencode", "path": f"/usr/bin/{k}", "version": "9.9.9", "error": "'opencode' not found in PATH"}
                    for k in ([body["kind"]] if body.get("kind") else ["claude", "opencode"])]}
            if op == "frontend_status":
                return {"ok": True, "mode": "local", "port": 8765, "token_configured": True, "frontends": {
                    "local": {"running": True, "url": "http://127.0.0.1:8765/", "auth": "off", "error": None},
                    "tailscale": {"running": False, "url": None, "auth": "required", "error": None}}}
            if op == "frontend_set":
                return {"ok": True}
            if op == "web_token_rotate":
                return {"ok": True, "token": "NEW-TOKEN-123", "fingerprint": "sha256:new", "signed_out_devices": 2}
            if op == "web_session_revoke":
                return {"ok": True, "revoked": 3}
            if op == "reset_plan":
                full = body.get("level") == "full"
                return {"ok": True, "level": body["level"], "needs_confirm": full, "confirm_word": "RESET",
                        "live_agents": [{"id": "sess_w", "name": "alpha", "kind": "opencode"}] if full else [],
                        "steps": [{"id": "x", "text": "Back up configuration and state first", "count": 1}],
                        "options": {"forget_state": "Also forget saved teams and agent definitions"} if full else {}}
            if op == "reset_apply":
                return {"ok": True, "level": body["level"], "done": ["temp files: 3 removed"], "errors": [], "backup": None, "restart": False}
            if op == "fs_complete":
                tree = {"/srv/": ["alpha", "alps", "beta"], "/srv/alpha/": ["deep", "inner"], "/srv/alpha/deep/": ["x"]}
                p = body.get("prefix", "")
                if "/" not in p:
                    p = "/srv/" + p                       # empty / relative input: the daemon's cwd, here /srv/
                base, stem = (p, "") if p.endswith("/") else (p.rsplit("/", 1)[0] + "/", p.rsplit("/", 1)[1])
                names = [n for n in tree.get(base, []) if n.startswith(stem)]
                return {"ok": True, "base": base, "error": None, "truncated": False,
                        "entries": [{"name": n, "path": base + n + "/"} for n in names]}
        if op == "bundle_list":
            return {"ok": True, "directory": "/state/bundles", "bundles": self.bundles}
        if op == "bundle_export":
            return {"ok": True, "name": "bundle-1.tar.gz", "path": "/state/bundles/bundle-1.tar.gz", "files": ["config/profiles.toml"]}
        if op == "bundle_delete":
            names = body.get("names") or [body.get("name")]
            self.bundles = [b for b in self.bundles if b["name"] not in names]
            return {"ok": True, "deleted": names, "failed": []}
        if op == "bundle_import":
            return {"ok": True, "written": ["config/teams/t.json"], "skipped": [], "backed_up": [],
                    "dry_run": body.get("dry_run", True), "source_version": "1.0.0",
                    "teams_plan": [{"team": "T", "workspace": None, "agents": [
                        {"name": "orq", "kind": "claude", "cwd": "/a", "cwd_ok": True},
                        {"name": "ex", "kind": "opencode", "cwd": "/b", "cwd_ok": False}]}],
                    "teams_applied": {"created": ["T/orq"], "existing": [], "skipped": [{"agent": "ex"}], "failed": []}}
        if op == "web_token_status":
            return {"ok": True, "token_exists": True, "fingerprint": "sha256:abc", "require_auth": True}
        if op == "clean_plan":
            return {"ok": True, "total_bytes": 4096,
                    "items": [{"path": "/tmp/at-tests-x", "kind": "tmp", "bytes": 10},
                              {"path": "/state/backups/pre-update-1.tar.gz", "kind": "update-backup", "bytes": 4086}]}
        if op == "clean_apply":
            return {"ok": True, "removed": ["/tmp/at-tests-x"], "failed": [], "bytes": 4096, "total_bytes": 4096, "items": []}
        return {"ok": True, "output": "", "agent": AGENT, "agents": self.agents, "teams": self.teams}

    def _count(self):
        return self.page.locator("#hist-list .msg").count()

    def _at_bottom(self):
        return self.page.evaluate(
            "(()=>{const b=document.getElementById('hist');return b.scrollHeight-b.scrollTop-b.clientHeight<60})()")

    def _agent_names(self):
        return self.page.eval_on_selector_all("#nav .agent-row .nm", "els => els.map(e => e.textContent)")

    def _menu_item(self, label):
        return self.page.locator(".menu button", has_text=label).first


class ConversationView(_Browser):
    def test_page_loads_every_module_from_static_without_errors(self):
        srcs = self.page.eval_on_selector_all("script[src]", "els => els.map(e => e.getAttribute('src'))")
        self.assertEqual(srcs, [
            "/static/js/core.js", "/static/js/transport.js", "/static/js/sidebar.js",
            "/static/js/agent.js", "/static/js/timeline.js", "/static/js/usage.js", "/static/js/cleaning.js",
            "/static/js/version.js", "/static/js/inbox.js", "/static/js/access.js", "/static/js/bundle.js", "/static/js/settings.js",
            "/static/js/archived.js", "/static/js/onboarding.js", "/static/js/mission.js", "/static/js/notify.js",
            "/static/js/palette.js", "/static/js/composer.js", "/static/js/conversation.js",
            "/static/js/actions.js", "/static/js/main.js"])
        self.assertTrue(self.page.evaluate("!!document.querySelector('link[href=\"/static/app.css\"]')"))
        self.assertEqual(self.console_errors, [])
        self.assertEqual(self.errors, [])

    def test_container_scrolls_not_the_page_and_has_no_buttons(self):
        self.assertTrue(self.page.evaluate(
            "(()=>{const b=document.getElementById('hist');return b.scrollHeight>b.clientHeight+500})()"))
        self.assertTrue(self.page.evaluate("document.documentElement.scrollHeight<=innerHeight+2"))
        self.assertEqual(self.page.locator("#hist button:not(.msg-link)").count(), 0)
        self.assertEqual(self.page.evaluate("getComputedStyle(document.getElementById('keys')).display"), "none")
        self.assertTrue(self._at_bottom())

    def test_scrolling_to_the_top_loads_earlier_messages_automatically(self):
        self.assertEqual(self._count(), 200)
        self.page.evaluate("document.getElementById('hist').scrollTop=0")
        self.page.wait_for_function("document.querySelectorAll('#hist-list .msg').length>=400")
        self.page.evaluate("document.getElementById('hist').scrollTop=0")
        self.page.wait_for_function("document.querySelectorAll('#hist-list .msg').length==450")
        self.assertIn("start of conversation", self.page.inner_text("#hist-top"))

    def test_follows_latest_only_when_user_is_at_the_bottom(self):
        self.page.evaluate("document.getElementById('hist').scrollTop=400")
        self.total = 451
        self.page.wait_for_function("document.getElementById('hist-note').textContent.startsWith('451')", timeout=6000)
        self.assertFalse(self._at_bottom())  # reading up: not dragged down
        self.page.evaluate("(()=>{const b=document.getElementById('hist');b.scrollTop=b.scrollHeight})()")
        self.total = 452
        self.page.wait_for_function("document.getElementById('hist-note').textContent.startsWith('452')", timeout=6000)
        self.assertTrue(self._at_bottom())
        self.assertEqual(self.errors, [])

    def test_ctrl_c_in_the_input_interrupts_but_keeps_copy_when_text_is_selected(self):
        # Works even when the input is disabled (agent working) and nothing is focused.
        self.page.evaluate("document.getElementById('input').disabled=true")
        self.page.keyboard.press("Control+c")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.calls.count("agent_interrupt"), 1)
        self.page.evaluate("document.getElementById('input').disabled=false")
        self.page.fill("#input", "texto")
        self.page.evaluate("document.getElementById('input').select()")
        self.page.keyboard.press("Control+c")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.calls.count("agent_interrupt"), 1)  # selection: normal copy


class NewEmptyAgent(_Browser):
    """A just-created agent: straight into Conversation, first message shows at once."""

    initial_total = 0
    available = False  # no conversation file / session yet
    wait_for_messages = False

    def test_opens_in_conversation_view_even_when_empty(self):
        self.assertEqual(self.page.evaluate("getComputedStyle(document.getElementById('hist')).display"), "block")
        self.assertEqual(self.page.evaluate("getComputedStyle(document.getElementById('term')).display"), "none")
        self.assertIn("No messages yet", self.page.inner_text("#hist-status"))
        self.assertEqual(self.page.locator("#tab-hist").is_visible(), True)

    def test_first_message_is_shown_immediately_then_replaced_by_real_history(self):
        self.page.fill("#input", "hola agente")
        self.page.press("#input", "Enter")
        self.page.wait_for_selector("#hist-list .msg.pending")          # echo, before any history exists
        self.assertIn("hola agente", self.page.inner_text("#hist-list .msg.pending"))
        self.assertEqual(self.page.inner_text("#hist-status").strip(), "")
        self.total, self.available = 1, True                            # the agent's history appears
        self.page.wait_for_function("document.querySelectorAll('#hist-list .msg.pending').length===0", timeout=8000)
        self.page.wait_for_function("document.querySelectorAll('#hist-list .msg').length===1")


class Sidebar(_Browser):
    agents = [WORKING, READY, ATTN]
    teams = [TEAM]

    def test_filters_count_and_narrow_the_list(self):
        filters = self.page.locator("#filters button")
        self.assertEqual(self.page.locator("#filters button").count(), 3)
        self.assertIn("3", filters.nth(0).inner_text())                 # All: 3
        filters.nth(1).click()                                          # Working
        self.assertEqual(self._agent_names(), ["alpha"])
        self.assertTrue(filters.nth(1).evaluate("b => b.classList.contains('on')"))
        filters.nth(2).click()                                          # Needs you
        self.assertEqual(self._agent_names(), ["gamma"])
        filters.nth(0).click()                                          # All again
        self.assertEqual(sorted(self._agent_names()), ["alpha", "beta", "gamma"])

    def test_search_filters_and_escape_clears(self):
        self.page.fill("#q", "beta")
        self.assertEqual(self._agent_names(), ["beta"])
        self.page.press("#q", "Escape")
        self.assertEqual(self.page.input_value("#q"), "")
        self.assertEqual(len(self._agent_names()), 3)

    def test_team_actions_menu_and_set_workspace_dialog(self):
        self.page.click('button[aria-label="Actions for team Team One"]')
        self.assertTrue(self.page.locator(".menu").is_visible())
        self._menu_item("Set workspace…").click()
        self.page.wait_for_selector("#dlg[open] #pf-ws")
        self.page.fill("#pf-ws", "/tmp/ws")
        self.page.locator("#dlg .btn.primary").click()
        self.assertIn("team_set_workspace", self.calls)
        payload = next(p for p in self.payloads if p.get("op") == "team_set_workspace")
        self.assertEqual(payload.get("workspace"), "/tmp/ws")

    def test_agent_actions_menu_lists_and_deletes(self):
        row = self.page.locator('.agent-row:has-text("alpha")').first
        row.hover()
        row.locator("button.more").click()
        self.assertTrue(self.page.locator(".menu").is_visible())
        self._menu_item("Copy name").click()
        self.assertNotIn("agent_stop", self.calls)
        row = self.page.locator('.agent-row:has-text("alpha")').first
        row.hover()
        row.locator("button.more").click()
        self._menu_item("Delete agent").click()
        self.page.wait_for_selector("#dlg[open]")
        self.page.locator("#dlg .btn.danger").click()
        self.assertIn("agent_stop", self.calls)


class Dialogs(_Browser):
    agents = [WORKING, READY, ATTN]
    teams = [TEAM]

    def test_new_agent_dialog_populates_meta_and_creates(self):
        self.page.click("#newAgentLink")
        self.page.wait_for_selector("#newAgent[open]")
        self.assertEqual(self.page.locator("#na-kind option").count(), 2)
        self.page.fill("#na-name", "reviewer")
        self.page.locator("#na-ok").click()
        self.assertIn("agent_create", self.calls)
        payload = next(p for p in self.payloads if p.get("op") == "agent_create")
        self.assertEqual(payload.get("name"), "reviewer")
        self.assertEqual(payload.get("kind"), "claude")

    def test_new_agent_requires_a_name(self):
        self.page.click("#newAgentLink")
        self.page.wait_for_selector("#newAgent[open]")
        self.page.locator("#na-ok").click()
        self.assertNotIn("agent_create", self.calls)
        self.assertTrue(self.page.locator("#newAgent").evaluate("d => d.open"))
        self.assertTrue(self.page.locator("#na-name").evaluate("i => !i.checkValidity()"))

    def test_new_team_dialog_creates_a_team(self):
        self.page.click("#newTeamLink")
        self.page.wait_for_selector("#dlg[open] #pf-name")
        self.page.fill("#pf-name", "backend")
        self.page.locator("#dlg .btn.primary").click()
        self.assertIn("team_create", self.calls)
        payload = next(p for p in self.payloads if p.get("op") == "team_create")
        self.assertEqual(payload.get("name"), "backend")

    def test_add_members_picker_calls_add_member(self):
        self.page.click('button[aria-label="Actions for team Team One"]')
        self._menu_item("Add members…").click()
        self.page.wait_for_selector("#dlg[open] .pick")
        boxes = self.page.locator("#dlg .pick input[type=checkbox]")
        self.assertTrue(boxes.count() >= 1)
        boxes.first.check()
        self.page.locator("#dlg .btn.primary").click()
        self.assertIn("team_add_member", self.calls)


class InteractionCards(_Browser):
    agents = [ATTN]

    def test_permission_card_allow_once(self):
        card = self.page.locator(".ask").first
        self.assertIn("PERMISSION REQUESTED", card.inner_text())
        card.get_by_role("button", name="Allow once").click()
        self.assertIn("interaction_respond", self.calls)
        payload = next(p for p in self.payloads if p.get("op") == "interaction_respond")
        self.assertEqual(payload.get("id"), PERMISSION["id"])
        self.assertEqual(payload.get("answer"), {"reply": "once"})

    def test_permission_card_deny_sends_the_note(self):
        card = self.page.locator(".ask").first
        card.locator('input[type="text"]').fill("too risky")
        card.get_by_role("button", name="Deny").click()
        payload = next(p for p in self.payloads if p.get("op") == "interaction_respond")
        self.assertEqual(payload.get("answer"), {"reply": "reject", "message": "too risky"})

    def test_question_card_requires_an_answer_then_sends_it(self):
        card = self.page.locator(".ask").nth(1)
        self.assertIn("THE AGENT ASKS", card.inner_text())
        card.get_by_role("button", name="Answer").click()
        self.assertIn("Answer every question first", card.inner_text())
        self.assertNotIn("interaction_respond", self.calls)
        card.locator('input[type="radio"]').first.check()
        card.get_by_role("button", name="Answer").click()
        payload = next(p for p in self.payloads if p.get("op") == "interaction_respond")
        self.assertEqual(payload.get("answer"), {"answers": [["postgres"]]})

    def test_question_card_free_text(self):
        card = self.page.locator(".ask").nth(1)
        card.locator('input[type="text"]').fill("duckdb")
        card.get_by_role("button", name="Answer").click()
        payload = next(p for p in self.payloads if p.get("op") == "interaction_respond")
        self.assertEqual(payload.get("answer"), {"answers": [["duckdb"]]})


class Inbox(_Browser):
    agents = [WORKING, ATTN]

    def test_title_and_counts_cover_every_pending_interaction(self):
        self.assertEqual(self.page.title().split(")")[0], "(2")
        self.assertEqual(self.page.inner_text("#inboxN"), "2")
        self.assertEqual(self.page.inner_text(".inbox-row .badge"), "2")
        self.assertEqual(self.page.locator(".inbox-row").count(), 1)

    def test_topbar_button_opens_the_global_inbox_with_a_card_per_item(self):
        self.page.click("#inboxBtn")
        self.page.wait_for_selector("#inboxDlg[open]")
        self.assertEqual(self.page.locator("#inbox-list .inbox-item").count(), 2)
        self.assertIn("gamma", self.page.inner_text("#inbox-list"))
        self.assertEqual(self.page.locator("#inbox-list .ask").count(), 2)

    def test_answering_a_permission_from_the_inbox_does_not_open_the_agent(self):
        self.page.click("#inboxBtn")
        card = self.page.locator("#inbox-list .ask").first
        card.get_by_role("button", name="Allow once").click()
        payload = next(p for p in self.payloads if p.get("op") == "interaction_respond")
        self.assertEqual(payload.get("id"), PERMISSION["id"])
        self.assertEqual(payload.get("answer"), {"reply": "once"})
        self.assertIn("alpha", self.page.inner_text("#head .nm"))  # still on the working agent

    def test_open_agent_from_the_inbox_selects_it_and_closes(self):
        self.page.click("#inboxBtn")
        self.page.locator("#inbox-list .inbox-item").first.get_by_role("button", name="Open agent").click()
        self.assertFalse(self.page.locator("#inboxDlg").evaluate("d => d.open"))
        self.assertIn("gamma", self.page.inner_text("#head .nm"))


class MissionControl(_Browser):
    agents = [WORKING, ATTN]
    teams = [TEAM]
    messages = [
        {"message_id": "m1", "sender": "sess_w", "sender_name": "alpha",
         "recipient": "sess_attn", "recipient_name": "gamma", "body": "please review",
         "timestamp": time.time() - 30, "delivered": True, "status": "injected"},
        {"message_id": "m2", "sender": "sess_attn", "sender_name": "gamma",
         "recipient": "sess_w", "recipient_name": "alpha", "body": "done",
         "timestamp": time.time() - 5, "delivered": True, "status": "acknowledged"},
    ]

    def test_board_has_a_card_per_agent_and_the_message_timeline(self):
        self.page.click("#missionBtn")
        self.page.wait_for_selector("#missionDlg[open]")
        self.assertEqual(self.page.locator("#mission-body .mc-card").count(), 2)
        board = self.page.inner_text("#mission-body")
        self.assertIn("alpha", board)
        self.assertIn("Working now", board)
        self.assertIn("Waiting for your answer", board)  # gamma has pending interactions
        self.assertIn("1 sent", board)                   # alpha sent m1
        self.assertEqual(self.page.locator("#mission-body .mc-msg").count(), 2)
        flow = self.page.inner_text("#mission-body .mc-timeline")
        self.assertIn("alpha → gamma", flow)
        self.assertIn("gamma → alpha", flow)

    def test_open_requests_are_shown_and_cancellable(self):
        self.page.evaluate("""() => {
            S.state = {...(S.state || {}), requests: [{request_id:"req_1", sender:"sess_w",
                recipient:"sess_attn", task:"review", state:"pending",
                created_at: Date.now()/1000 - 10, deadline: Date.now()/1000 + 100}]};
            if(typeof renderMission === "function") renderMission();
        }""")
        self.page.click("#missionBtn")
        self.page.wait_for_selector("#missionDlg[open] .mc-req")
        self.assertIn("req_1", self.page.inner_text("#mission-body"))
        self.page.locator(".mc-req button").click()
        self.page.wait_for_timeout(200)
        self.assertIn("request_cancel", self.calls)


class MissionControlByTeam(MissionControl):
    def test_team_scope_shows_only_its_members_and_messages(self):
        self.page.click("#missionBtn")
        self.page.wait_for_selector("#missionDlg[open]")
        self.page.select_option("#mission-scope", "t1")  # TEAM has only alpha
        self.assertEqual(self.page.locator("#mission-body .mc-card").count(), 1)
        body = self.page.inner_text("#mission-body")
        self.assertIn("alpha", body)
        self.assertNotIn("gamma\nclaude", body)
        self.assertIn("1 working", body)
        # alpha takes part in both messages
        self.assertEqual(self.page.locator("#mission-body .mc-msg").count(), 2)
        self.page.select_option("#mission-scope", "")
        self.assertEqual(self.page.locator("#mission-body .mc-card").count(), 2)

    def test_team_menu_opens_mission_control_for_that_team(self):
        self.page.locator('[aria-label="Actions for team Team One"]').click()
        self.page.get_by_role("menuitem", name="Mission control").click()
        self.page.wait_for_selector("#missionDlg[open]")
        self.assertEqual(self.page.input_value("#mission-scope"), "t1")
        self.assertEqual(self.page.locator("#mission-body .mc-card").count(), 1)

    def test_palette_has_a_command_per_team(self):
        self.page.keyboard.press("Control+k")
        self.page.keyboard.type("Mission control: Team")
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector("#missionDlg[open]")
        self.assertEqual(self.page.input_value("#mission-scope"), "t1")


class UsageView(_Browser):
    agents = [
        {**WORKING, "usage": {"available": True, "tokens": 13100, "tokens_estimated": True,
                              "cost_usd": 0.42, "source": "opencode footer"}},
        {**READY, "usage": {"available": False}},
    ]
    teams = [{"team_id": "t1", "name": "Team One",
              "members": [{"agent_id": "sess_w"}, {"agent_id": "sess_r"}], "missing": []}]

    def test_usage_dialog_shows_real_values_and_n_d_for_the_missing(self):
        self.page.click("#usageBtn")
        self.page.wait_for_selector("#usageDlg[open]")
        body = self.page.inner_text("#usage-body")
        self.assertIn("~13K", body)            # estimated tokens
        self.assertIn("$0.42", body)
        self.assertIn("n/d", body)             # beta reports nothing
        self.assertIn("does not report", body)
        self.assertIn("Team One", body)


class AccessibilityAndPerf(_Browser):
    agents = [WORKING, READY, ATTN]
    teams = [TEAM]

    def test_menu_is_keyboard_navigable_and_escape_restores_focus(self):
        row = self.page.locator('.agent-row:has-text("alpha")').first
        row.hover()
        more = row.locator("button.more")
        more.click()
        self.page.wait_for_selector(".menu")
        self.page.keyboard.press("ArrowDown")
        self.page.keyboard.press("ArrowUp")
        focused_is_button = self.page.evaluate("document.activeElement.tagName === 'BUTTON' && !!document.activeElement.closest('.menu')")
        self.assertTrue(focused_is_button)
        self.page.keyboard.press("Escape")
        self.assertEqual(self.page.locator(".menu").count(), 0)

    def test_dynamic_regions_are_announced(self):
        self.page.wait_for_selector('[aria-live="polite"]')
        for sel in ("#summary", "#nav", "#hist-status", "#toasts"):
            self.assertEqual(self.page.get_attribute(sel, "aria-live"), "polite")

    def test_unchanged_state_reuses_the_existing_rows(self):
        # Tag a row, push a state update with the same structure, and check the
        # node was not recreated (incremental update).
        self.page.evaluate("document.querySelector('#nav .agent-row').__mark = 'kept'")
        self.page.wait_for_timeout(1400)   # at least one WS snapshot tick
        self.assertEqual(self.page.evaluate("document.querySelector('#nav .agent-row').__mark"), "kept")


class Onboarding(_Browser):
    onboarded = False
    auto_select = False
    wait_for_messages = False
    agents = [READY]

    def test_wizard_opens_on_first_use_and_creates_a_team(self):
        self.page.wait_for_selector("#onb[open]")
        self.assertIn("Welcome to crewhall", self.page.inner_text("#onb"))
        self.page.locator("#onb .btn.primary").click()
        self.page.wait_for_timeout(200)
        payload = next(p for p in self.payloads if p.get("op") == "team_up")
        self.assertEqual(payload["spec"]["name"], "trabajo")
        self.assertEqual(len(payload["spec"]["agents"]), 2)
        self.assertEqual(self.page.evaluate("localStorage.getItem('at.onboarded')"), "1")

    def test_skip_marks_it_done(self):
        self.page.wait_for_selector("#onb[open]")
        self.page.locator("#onb .btn", has_text="Skip").click()
        self.assertFalse(self.page.locator("#onb").evaluate("d => d.open"))
        self.assertEqual(self.page.evaluate("localStorage.getItem('at.onboarded')"), "1")


class Archived(_Browser):
    agents = [READY]

    def test_palette_lists_archived_and_shows_the_summary(self):
        self.archived = [{"agent_id": "sess_gone", "name": "old", "kind": "claude",
                          "teams": ["Team One"], "archived_at": 0, "summary": {"messages": 1}}]
        self.page.keyboard.press("Control+k")
        self.page.fill("#palette-q", "Archived agents")
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector("#dlg[open] .arch-row")
        self.assertIn("old", self.page.inner_text("#dlg"))
        self.page.locator(".arch-row").first.click()
        self.page.wait_for_selector("#dlg[open] .arch-msgs")
        self.assertIn("old → alpha", self.page.inner_text("#dlg"))


class Bundle(_Browser):
    agents = [READY]

    def test_export_and_import_flow(self):
        self.bundles = [{"name": "bundle-1.tar.gz", "bytes": 100, "mtime": 0, "valid": True, "files": 1, "teams": 1}]
        self.page.click("#bundleLink")
        self.page.wait_for_selector("#dlg[open] .bundle-row")
        self.assertIn("bundle-1.tar.gz", self.page.inner_text("#dlg"))
        self.page.click("#bundleExport")
        self.page.wait_for_timeout(200)
        self.assertIn("bundle_export", self.calls)
        self.page.locator(".bundle-row").get_by_role("button", name="Import…").click()
        self.page.wait_for_selector("#dlg[open] form")   # confirm dialog
        text = self.page.inner_text("#dlg")
        self.assertIn("Team T: orq, ex ⚠", text)
        self.assertIn("will be skipped", text)
        self.page.locator("#dlg .btn.danger.solid").click()
        self.page.wait_for_timeout(200)
        self.assertIn("bundle_import", self.calls)
        self.assertTrue(any(p.get("apply_teams") is True and p.get("dry_run") is False for p in self.payloads))


class BundleManage(_Browser):
    agents = [READY]

    def _bundles(self):
        return [
            {"name": "bundle-good.tar.gz", "bytes": 900, "mtime": 10, "valid": True, "files": 2, "teams": 1},
            {"name": "bundle-empty.tar.gz", "bytes": 120, "mtime": 5, "valid": True, "empty": True, "files": 0, "teams": 0},
            {"name": "bundle-bad.tar.gz", "bytes": 30, "mtime": 1, "valid": False, "error": "cannot read", "files": 0, "teams": 0},
        ]

    def _open(self):
        self.bundles = self._bundles()
        self.page.click("#bundleLink")
        self.page.wait_for_selector("#dlg[open] .bundle-row")

    def test_junk_is_flagged_and_downloadable_with_a_link(self):
        self._open()
        self.assertEqual(self.page.locator(".bundle-row.junk").count(), 2)
        text = self.page.inner_text("#dlg")
        self.assertIn("empty — nothing to restore", text)
        self.assertIn("invalid: cannot read", text)
        self.assertIn("2 file(s), 1 team(s)", text)
        href = self.page.locator('.bundle-row[data-bundle="bundle-good.tar.gz"] a').get_attribute("href")
        self.assertEqual(href, "/api/bundle/bundle-good.tar.gz")
        # junk cannot be imported, only downloaded or deleted
        self.assertEqual(self.page.locator('.bundle-row[data-bundle="bundle-empty.tar.gz"]').get_by_role("button", name="Import…").count(), 0)

    def test_delete_one_bundle(self):
        self._open()
        self.page.get_by_role("button", name="Delete bundle-empty.tar.gz").click()
        self.page.wait_for_selector("#dlg[open] form")
        self.page.locator("#dlg .btn.danger.solid").click()
        self.page.wait_for_selector('.bundle-row[data-bundle="bundle-good.tar.gz"]')
        self.assertEqual(self.page.locator(".bundle-row").count(), 2)
        self.assertEqual(self.page.locator(".bundle-row[data-bundle='bundle-empty.tar.gz']").count(), 0)

    def test_delete_all_junk_in_one_go_keeps_good_bundles(self):
        self._open()
        self.page.click("#bundleClean")
        self.page.wait_for_selector("#dlg[open] form")
        self.assertIn("bundle-bad.tar.gz", self.page.inner_text("#dlg"))
        self.page.locator("#dlg .btn.danger.solid").click()
        self.page.wait_for_selector('.bundle-row[data-bundle="bundle-good.tar.gz"]')
        self.assertEqual(self.page.locator(".bundle-row").count(), 1)
        self.assertFalse(self.page.locator("#bundleClean").is_visible())

    def test_cancelling_a_delete_returns_to_the_list(self):
        self._open()
        self.page.get_by_role("button", name="Delete bundle-bad.tar.gz").click()
        self.page.wait_for_selector("#dlg[open] form")
        self.page.get_by_role("button", name="Cancel").click()
        self.page.wait_for_selector("#dlg[open] .bundle-row")
        self.assertEqual(self.page.locator(".bundle-row").count(), 3)

    def test_upload_posts_the_file_and_refreshes_the_list(self):
        uploads = []
        def upload(route):
            uploads.append((route.request.method, route.request.url, route.request.post_data_buffer))
            self.bundles.append({"name": "uploaded-1.tar.gz", "bytes": 5, "mtime": 20, "valid": True, "files": 1, "teams": 0})
            route.fulfill(status=200, content_type="application/json", body='{"ok": true, "name": "uploaded-1.tar.gz"}')
        self.page.route("http://localhost/api/bundle?**", upload)
        self._open()
        self.page.set_input_files("#bundleFile", files=[{"name": "setup.tar.gz", "mimeType": "application/gzip", "buffer": b"\x1f\x8bdata"}])
        self.page.wait_for_selector('.bundle-row[data-bundle="uploaded-1.tar.gz"]')
        self.assertEqual(uploads[0][0], "POST")
        self.assertIn("name=setup.tar.gz", uploads[0][1])
        self.assertEqual(uploads[0][2], b"\x1f\x8bdata")

    def test_a_rejected_upload_shows_the_server_error(self):
        self.page.route("http://localhost/api/bundle?**", lambda r: r.fulfill(
            status=400, content_type="application/json", body='{"ok": false, "error": "not a valid bundle: bad manifest"}'))
        self._open()
        self.page.set_input_files("#bundleFile", files=[{"name": "x.tar.gz", "mimeType": "application/gzip", "buffer": b"junk"}])
        self.page.wait_for_selector(".toast:has-text('not a valid bundle')")


class SettingsPanel(_Browser):
    agents = [READY]

    def _open(self, tab=None):
        self.page.click("#settingsBtn")
        self.page.wait_for_selector("#settingsDlg[open] .set-view")
        if tab:
            self.page.click(f'#settings-nav button[data-tab="{tab}"]')
            self.page.wait_for_selector(f'#settings-nav button[data-tab="{tab}"].on')

    def _ops(self, name):
        return [p for p in self.payloads if p.get("op") == name]

    def test_providers_show_status_and_masked_secrets(self):
        self._open()
        self.page.wait_for_selector('.prov-card[data-provider="claude"] .prov-status.ok')
        self.assertIn("9.9.9", self.page.inner_text('.prov-card[data-provider="claude"]'))
        self.assertIn("not found", self.page.inner_text('.prov-card[data-provider="opencode"] .prov-status'))
        env = self.page.input_value("#pv-claude-env")
        self.assertIn("ANTHROPIC_API_KEY=••••••••", env)
        self.assertNotIn("sk-", env)

    def test_saving_a_provider_sends_only_its_own_keys_and_can_disable_it(self):
        self._open()
        self.page.wait_for_selector('.prov-card[data-provider="opencode"]')
        card = self.page.locator('.prov-card[data-provider="opencode"]')
        card.locator("#pv-opencode-enabled").uncheck()
        card.locator("#pv-opencode-model").fill("provider/model-x")
        card.get_by_role("button", name="Save", exact=True).click()
        self.page.wait_for_function("window.__ok = true")
        self.page.wait_for_timeout(200)
        changes = self._ops("settings_set")[-1]["changes"]
        self.assertEqual(changes["providers.opencode.enabled"], False)
        self.assertEqual(changes["providers.opencode.default_model"], "provider/model-x")
        self.assertTrue(all(k.startswith("providers.opencode.") for k in changes))

    def test_testing_a_command_asks_the_server_with_the_typed_command(self):
        self._open()
        self.page.wait_for_selector('.prov-card[data-provider="claude"]')
        card = self.page.locator('.prov-card[data-provider="claude"]')
        card.locator("#pv-claude-command").fill("/opt/claude")
        card.get_by_role("button", name="Test").click()
        self.page.wait_for_timeout(300)
        last = self._ops("provider_check")[-1]
        self.assertEqual((last["kind"], last["command"]), ("claude", "/opt/claude"))

    def test_agent_settings_save_only_what_changed_and_env_locked_fields_are_disabled(self):
        self._open("agents")
        self.assertTrue(self.page.locator("#sf-agents-hooks").is_disabled())
        self.assertIn("CREWHALL_HOOKS", self.page.inner_text(".set-view"))
        save = self.page.get_by_role("button", name="Save", exact=True)
        self.assertTrue(save.is_disabled())
        self.page.fill("#sf-agents-permission_wait", "30")
        save.click()
        self.page.wait_for_timeout(300)
        self.assertEqual(self._ops("settings_set")[-1]["changes"], {"agents.permission_wait": 30})

    def test_a_server_validation_error_is_shown(self):
        self._open("access")
        self.page.wait_for_selector("#sf-security-session_ttl_hours")
        self.page.fill("#sf-security-session_ttl_hours", "0")
        self.page.locator(".set-group").get_by_role("button", name="Save", exact=True).click()
        self.page.wait_for_selector(".toast:has-text('must be between 1 and 720')")

    def test_rotating_the_token_shows_it_once_after_confirmation(self):
        self._open("access")
        self.page.click("#rotateToken")
        self.page.wait_for_selector("#dlg[open] form")
        self.assertEqual(self._ops("web_token_rotate"), [])  # not before confirming
        self.page.locator("#dlg .btn.danger.solid").click()
        self.page.wait_for_selector("#newToken")
        self.assertEqual(self.page.input_value("#newToken"), "NEW-TOKEN-123")
        self.assertIn("2 other device(s)", self.page.inner_text("#dlg"))
        self.assertEqual(len(self._ops("web_token_rotate")), 1)

    def test_web_ui_mode_can_be_changed_but_not_turned_off(self):
        self._open("access")
        self.page.wait_for_selector('input[name="fe-mode"]')
        self.assertEqual(self.page.locator('input[name="fe-mode"]').count(), 3)  # local / tailscale / both
        self.assertEqual(self.page.locator('input[name="fe-mode"][value="off"]').count(), 0)
        self.page.check('input[name="fe-mode"][value="both"]')
        self.page.get_by_role("button", name="Apply").click()
        self.page.wait_for_selector("#dlg[open] form")
        self.page.locator("#dlg .btn.danger.solid").click()
        self.page.wait_for_timeout(300)
        self.assertEqual(self._ops("frontend_set")[-1]["mode"], "both")

    def test_clean_level_previews_then_runs_without_a_typed_confirmation(self):
        self._open("emergency")
        self.page.locator('.reset-card[data-level="clean"]').get_by_role("button", name="Preview…").click()
        self.page.wait_for_selector("#dlg[open] .reset-steps")
        self.assertIn("Back up configuration", self.page.inner_text("#dlg"))
        self.assertFalse(self.page.locator("#reset-go").is_disabled())
        self.page.click("#reset-go")
        self.page.wait_for_selector(".toast:has-text('Done')")
        self.assertEqual(self._ops("reset_apply")[-1]["level"], "clean")

    def test_full_reset_needs_the_typed_word_and_sends_the_options(self):
        self._open("emergency")
        self.page.locator('.reset-card[data-level="full"]').get_by_role("button", name="Preview…").click()
        self.page.wait_for_selector("#reset-word")
        self.assertIn("alpha", self.page.inner_text("#dlg"))        # running agents are named
        self.assertTrue(self.page.locator("#reset-go").is_disabled())
        self.page.fill("#reset-word", "reset")                       # case matters
        self.assertTrue(self.page.locator("#reset-go").is_disabled())
        self.page.fill("#reset-word", "RESET")
        self.page.check("#ro-forget_state")
        self.assertFalse(self.page.locator("#reset-go").is_disabled())
        self.page.click("#reset-go")
        self.page.wait_for_timeout(400)
        call = self._ops("reset_apply")[-1]
        self.assertEqual((call["level"], call["confirm"], call["forget_state"]), ("full", "RESET", True))

    def test_interface_tab_changes_the_theme_locally(self):
        self._open("interface")
        self.page.select_option("#if-theme", "light")
        self.assertEqual(self.page.evaluate("document.documentElement.getAttribute('data-theme')"), "light")


class PathAutocomplete(_Browser):
    agents = [READY]

    def _open_cwd(self):
        self.page.click("#newAgentLink")
        self.page.wait_for_selector("#newAgent[open]")
        self.page.evaluate("document.querySelector('#newAgent details.adv').open = true")  # cwd lives under Advanced
        self.page.click("#na-cwd")

    def _options(self):
        return self.page.eval_on_selector_all(".path-opt", "els => els.map(e => e.textContent)")

    def _wait_options(self, expected):
        self.page.wait_for_function("exp => JSON.stringify([...document.querySelectorAll('.path-opt')].map(e => e.textContent)) === JSON.stringify(exp)", arg=expected)

    def test_typing_character_by_character_lists_matching_directories(self):
        self._open_cwd()
        self.page.keyboard.type("/srv/al")
        self._wait_options(["/srv/alpha/", "/srv/alps/"])
        self.assertEqual(self.page.get_attribute("#na-cwd", "aria-expanded"), "true")

    def test_picking_a_suggestion_and_typing_on_keeps_rendering_the_next_levels(self):
        # The regression: the first level showed, later ones were fetched but never drawn.
        self._open_cwd()
        self.page.keyboard.type("/srv/al")
        self._wait_options(["/srv/alpha/", "/srv/alps/"])
        self.page.click('.path-opt:has-text("/srv/alpha/")')
        self.assertEqual(self.page.input_value("#na-cwd"), "/srv/alpha/")
        self._wait_options(["/srv/alpha/deep/", "/srv/alpha/inner/"])          # second level
        self.page.keyboard.type("d")
        self._wait_options(["/srv/alpha/deep/"])                                # narrowed while open
        self.page.click(".path-opt")
        self._wait_options(["/srv/alpha/deep/x/"])                              # third level

    def test_keyboard_navigation_tab_and_escape(self):
        self._open_cwd()
        self.page.keyboard.type("/srv/")
        self._wait_options(["/srv/alpha/", "/srv/alps/", "/srv/beta/"])
        self.page.keyboard.press("ArrowDown"); self.page.keyboard.press("ArrowDown")
        self.page.keyboard.press("Enter")
        self.assertEqual(self.page.input_value("#na-cwd"), "/srv/alps/")       # Enter accepts, does not submit the form
        self.assertTrue(self.page.locator("#newAgent").evaluate("d => d.open"))
        self.page.fill("#na-cwd", "")
        self.page.keyboard.type("/srv/b")
        self._wait_options(["/srv/beta/"])
        self.page.keyboard.press("Tab")
        self.assertEqual(self.page.input_value("#na-cwd"), "/srv/beta/")
        self.page.keyboard.type("zz")
        self.page.wait_for_function("document.querySelectorAll('.path-opt').length === 0")  # nothing matches: menu gone
        self.page.fill("#na-cwd", "/srv/")
        self._wait_options(["/srv/alpha/", "/srv/alps/", "/srv/beta/"])
        self.page.keyboard.press("Escape")
        self.assertTrue(self.page.locator(".path-pop").evaluate("p => p.hidden"))
        self.assertTrue(self.page.locator("#newAgent").evaluate("d => d.open"))  # Esc closed the menu only

    def test_a_slow_old_answer_never_overwrites_a_newer_one(self):
        self._open_cwd()
        self.page.evaluate("""() => { const real = window.op; let n = 0;
          window.op = async (o, p) => { if(o === 'fs_complete' && p.prefix === '/srv/a'){ await new Promise(r => setTimeout(r, 700)); }
                                       return real(o, p); }; }""")
        self.page.keyboard.type("/srv/a")
        self.page.wait_for_timeout(250)                # the slow query for "/srv/a" is now in flight
        self.page.keyboard.type("lps")                 # ...and the user already moved on to "/srv/alps"
        self._wait_options(["/srv/alps/"])
        self.page.wait_for_timeout(1000)
        self.assertEqual(self._options(), ["/srv/alps/"])

    def test_team_workspace_prompt_has_autocomplete_too(self):
        self.page.evaluate("() => { promptDlg({title:'W', fields:[{key:'ws', label:'p', path:true}]}); }")  # do not await the dialog
        self.page.wait_for_selector("#pf-ws[role=combobox]")
        self.page.click("#pf-ws")
        self.page.keyboard.type("/srv/")
        self._wait_options(["/srv/alpha/", "/srv/alps/", "/srv/beta/"])


class Access(_Browser):
    agents = [READY]

    def test_dialog_lists_connected_devices_and_token_status(self):
        self.web_sessions = [{"ip": "100.64.0.2", "user_agent": "Mozilla/5.0", "issued_at": 0,
                              "expires_at": 9e9, "last_seen": 0}]
        self.page.click("#logoutBtn")
        self.page.wait_for_selector("#dlg[open] .access-list")
        text = self.page.inner_text("#dlg")
        self.assertIn("100.64.0.2", text)
        self.assertIn("sha256:abc", text)
        self.assertIn("Sign out", text)


class UpdateStatus(_Browser):
    agents = [READY]

    def test_no_badge_when_versions_match(self):
        self.page.wait_for_timeout(300)
        self.assertEqual(self.page.evaluate("getComputedStyle(document.getElementById('updateBadge')).display"), "none")

    def test_restart_pending_shows_a_badge_and_the_warning(self):
        self.update = {"version": "2.0.0", "daemon_version": "1.0.0",
                       "restart_pending": True, "rollback_available": True}
        # The badge only appears after the status refresh; refresh it explicitly.
        self.page.evaluate("refreshUpdateStatus()")
        self.page.wait_for_selector("#updateBadge", state="visible")
        self.page.evaluate("document.getElementById('updateBadge').click()")
        self.page.wait_for_selector("#dlg[open]")
        text = self.page.inner_text("#dlg")
        self.assertIn("2.0.0", text)
        self.assertIn("CLOSES every running agent", text)
        self.assertIn("crewhall update --restart", text)


class Cleanup(_Browser):
    agents = [READY]

    def test_dialog_shows_the_plan_and_removes_after_confirmation(self):
        self.page.click("#cleanBtn")
        self.page.wait_for_selector("#dlg[open] .clean-list")
        self.assertIn("TEST LEFTOVERS", self.page.inner_text("#dlg"))
        self.assertIn("at-tests-x", self.page.inner_text("#dlg"))
        self.assertIn("Remove 2 item(s)", self.page.inner_text("#dlg .btn.danger"))
        self.page.click("#dlg .btn.danger")
        self.page.wait_for_selector("#dlg[open] form")   # confirm dialog
        self.page.locator("#dlg .btn.danger.solid").click()
        self.page.wait_for_timeout(200)
        self.assertIn("clean_apply", self.calls)


class Timeline(_Browser):
    agents = [WORKING, READY]

    def test_records_periods_and_shows_them(self):
        self.page.evaluate("localStorage.removeItem('x')")
        # Working for a moment, then it finishes: two observed states.
        self.page.wait_for_timeout(300)
        self.agents = [{**WORKING, "state": "waiting_input"}, READY]
        self._push()
        self.page.wait_for_timeout(300)
        self.page.click("#timelineBtn")
        self.page.wait_for_selector("#timelineDlg[open]")
        body = self.page.inner_text("#timeline-body")
        self.assertIn("alpha", body)
        self.assertIn("work", body)
        self.assertIn("turn", body)
        # The bar has at least one working segment for alpha.
        self.assertTrue(self.page.locator("#timeline-body .tl-seg").count() >= 1)

    def test_empty_state_before_any_activity(self):
        self.page.evaluate("S.segments = {}; S.turns = {};")
        self.page.click("#timelineBtn")
        self.page.wait_for_selector("#timelineDlg[open]")
        self.assertIn("No activity observed", self.page.inner_text("#timeline-body"))


def _act(agent, kind, label, detail=None, model="claude-opus-5-5"):
    return {**agent, "activity": {"kind": kind, "label": label, "detail": detail, "model": model}}


class ActivityAndModel(_Browser):
    auto_select = False
    wait_for_messages = False
    agents = [_act(WORKING, "shell", "Running command", "npm test", "gpt-x"),
              _act({**READY, "state": "waiting_input"}, "waiting", "Waiting for input")]
    viewport = {"width": 1300, "height": 800}

    def test_no_stray_null_text_in_header_or_summary(self):
        self.page.wait_for_selector("#nav .agent-row")
        self.page.locator("#nav").get_by_text("beta", exact=True).first.click()
        self.page.wait_for_selector("#agent-model")
        for sel in ("#summary", "#head"):
            text = self.page.inner_text(sel)
            self.assertNotIn("null", text.lower(), (sel, text))
            self.assertNotIn("false", text.lower(), (sel, text))

    def test_sidebar_shows_what_each_agent_is_doing(self):
        self.page.wait_for_selector("#nav .agent-row")
        rows = self.page.locator("#nav .agent-row")
        self.assertIn("Running command — npm test", rows.nth(0).inner_text())
        self.assertIn("Waiting for input", rows.nth(1).inner_text())

    def test_waiting_state_has_no_animation(self):
        self.page.wait_for_selector("#nav .agent-row")
        names = self.page.evaluate("""() => {
          const el = document.querySelector('#nav .agent-row.waiting_input');
          const dot = el.querySelector('.dot'), av = el.querySelector('.avatar');
          return [getComputedStyle(dot,'::after').animationName, getComputedStyle(av,'::before').animationName,
                  getComputedStyle(dot).animationName];
        }""")
        self.assertEqual(names, ["none", "none", "none"])

    def test_header_shows_model_and_activity(self):
        self.page.wait_for_selector("#nav .agent-row")
        self.page.locator("#nav").get_by_text("alpha", exact=True).first.click()
        self.page.wait_for_selector("#agent-model")
        self.assertEqual(self.page.inner_text("#agent-model").strip(), "gpt-x")
        self.assertIn("npm test", self.page.inner_text("#agent-activity"))

    def test_sidebar_footer_buttons_stay_visible_when_narrow(self):
        self.page.wait_for_selector("#nav .agent-row")
        self.page.set_viewport_size({"width": 1000, "height": 700})
        self.page.add_style_tag(content=".shell{grid-template-columns:230px minmax(0,1fr)!important}")
        box = self.page.evaluate("""() => { const f = document.querySelector('.side-foot').getBoundingClientRect();
          return [...document.querySelectorAll('.side-foot .btn')].filter(b => b.offsetParent !== null).map(b => {
            const r = b.getBoundingClientRect(); return r.left >= f.left - 1 && r.right <= f.right + 1 && r.width > 0; }); }""")
        self.assertTrue(box and all(box), box)


class LatestActivityOpen(_Browser):
    auto_select = False
    wait_for_messages = False
    agents = [READY]

    def _tool(self, name, i):
        return {"role": "assistant", "text": "x", "at": "2026-10-03T08:00:00Z", "blocks": [
            {"type": "tool_use", "name": name, "text": json.dumps({"command": f"cmd{i}"})}]}

    def _show(self, *ms):
        self.page.locator("#nav").get_by_text("beta", exact=True).first.click()
        self.page.wait_for_selector("#hist-list .msg")
        self.page.evaluate("ms => { S.hist = {}; const h = ensureHist('sess_r'); ms.forEach((m,i)=>upsert(h, i, m)); openHistory(); }", list(ms))

    def _open(self):
        return self.page.evaluate("[...document.querySelectorAll('#hist-list details')].map(d => d.open)")

    def test_only_the_latest_activity_is_expanded_and_it_moves(self):
        self._show(self._tool("Bash", 1), self._tool("Read", 2))
        self.page.wait_for_function("document.querySelectorAll('#hist-list details[open]').length === 1")
        self.assertEqual(self._open(), [False, True])
        self.page.evaluate(f"upsert(ensureHist('sess_r'), 2, {json.dumps(self._tool('Edit', 3))})")
        self.page.wait_for_function("document.querySelectorAll('#hist-list details')[2]?.open")
        self.assertEqual(self._open(), [False, False, True])

    def test_manual_choice_is_respected(self):
        self._show(self._tool("Bash", 1), self._tool("Read", 2))
        self.page.wait_for_function("document.querySelectorAll('#hist-list details[open]').length === 1")
        self.page.evaluate("document.querySelector('#hist-list details summary').click()")
        self.page.evaluate(f"upsert(ensureHist('sess_r'), 2, {json.dumps(self._tool('Edit', 3))})")
        self.page.wait_for_function("document.querySelectorAll('#hist-list details')[2]?.open")
        self.assertEqual(self._open(), [True, False, True])


class RichConversation(_Browser):
    auto_select = False
    wait_for_messages = False
    agents = [READY]

    def _show(self, m):
        self.page.locator("#nav").get_by_text("beta", exact=True).first.click()
        self.page.wait_for_selector("#hist-list .msg")
        self.page.evaluate("m => { S.hist = {}; const h = ensureHist('sess_r'); upsert(h, 0, m); openHistory(); }", m)

    def test_code_blocks_are_highlighted_and_html_stays_text(self):
        self._show({"role": "assistant", "text": "x",
             "blocks": [{"type": "text", "text": "```python\ndef f(x):\n  return '<b>' + x  # hi\n```"}],
             "at": "2026-10-03T08:00:00Z"})
        self.page.wait_for_selector("#hist-list code .hl-k")
        self.assertEqual(self.page.locator("#hist-list code .hl-k").first.inner_text(), "def")
        self.assertEqual(self.page.locator("#hist-list code b").count(), 0)
        self.assertIn("<b>", self.page.inner_text("#hist-list"))

    def test_tool_use_shows_a_summary_and_a_colored_diff(self):
        self._show(rich_msg())
        self.page.wait_for_selector("#hist-list .diff", state="attached")
        self.assertIn("/x/a.py", self.page.locator("#hist-list .tool-summary").first.text_content())
        self.assertGreaterEqual(self.page.locator("#hist-list .diff-line.add").count(), 1)
        self.assertGreaterEqual(self.page.locator("#hist-list .diff-line.del").count(), 1)

    def test_search_marks_matches_and_reports_the_count(self):
        self.page.locator("#nav").get_by_text("beta", exact=True).first.click()
        self.page.wait_for_selector("#hist-list .msg")
        q = self.page.locator("#hist-q")
        q.fill("relleno")
        self.page.wait_for_function("document.querySelectorAll('#hist-list mark').length > 0")
        self.assertTrue(self.page.inner_text("#hist-find").endswith("matches"))
        q.fill("")
        self.assertEqual(self.page.locator("#hist-list mark").count(), 0)

    def test_message_link_is_copied(self):
        self.page.locator("#nav").get_by_text("beta", exact=True).first.click()
        self.page.wait_for_selector("#hist-list .msg")
        self.page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="http://localhost")
        self.page.evaluate("(()=>{const b=document.querySelector('#hist-list .msg');b.querySelector('.msg-link').click();})()")
        self.page.wait_for_timeout(200)
        info = self.page.evaluate("navigator.clipboard.readText()")
        self.assertIn("#msg-", info)


class LiveAnsi(_Browser):
    agents = [WORKING]

    def _live(self, output):
        self.page.evaluate("id => { S.output[id] = ''; }", "sess_w")
        self.page.evaluate("o => { S.output['sess_w'] = o; renderTerm(); }", output)
        self.page.wait_for_timeout(50)

    def test_sgr_is_rendered_as_colored_spans(self):
        self.page.click("#tab-live")
        self._live("\x1b[31mRED\x1b[0m plain \x1b[32mGREEN\x1b[0m \x1b[1mBOLD\x1b[0m")
        term = self.page.locator("#term")
        self.assertIn("RED plain GREEN BOLD", term.inner_text())
        self.assertNotIn("\x1b", term.inner_text())
        for text, cls in (("RED", "ansi-c1"), ("plain", "ansi-d"), ("GREEN", "ansi-c2"), ("BOLD", "b")):
            node = self.page.locator(f'#term span:text-is("{text}")')
            self.assertIn(cls, node.get_attribute("class"), text)
        self.assertNotIn("\x1b", term.inner_text())

    def test_html_in_the_stream_is_escaped_not_injected(self):
        self.page.click("#tab-live")
        payload = "<img src=x onerror=window.__xss=1>\x1b[32m<script>window.__y=1</script>\x1b[0m"
        self._live(payload)
        term = self.page.locator("#term")
        self.assertIn("<img", term.inner_text())                 # shown literally
        self.assertEqual(self.page.locator("#term img").count(), 0)
        self.assertEqual(self.page.locator("#term script").count(), 0)
        self.assertIsNone(self.page.evaluate("window.__xss"))
        self.assertIsNone(self.page.evaluate("window.__y"))


class Composer(_Browser):
    agents = [READY]

    def _send(self, text="hello"):
        self.page.fill("#input", text)
        self.page.press("#input", "Enter")
        self.page.wait_for_timeout(150)

    def test_shift_enter_adds_a_newline_and_does_not_send(self):
        self.page.fill("#input", "line1")
        self.page.press("#input", "Shift+Enter")
        self.page.type("#input", "line2")
        self.assertIn("\n", self.page.input_value("#input"))
        self.assertNotIn("agent_write", self.calls)

    def test_enter_sends_the_text_and_clears_the_composer(self):
        self._send("hola")
        payload = next(p for p in self.payloads if p.get("op") == "agent_write")
        self.assertEqual(payload.get("text"), "hola")
        self.assertIn("agent_key", self.calls)
        self.assertEqual(self.page.input_value("#input"), "")

    def test_up_recalls_the_previous_prompt(self):
        self._send("prompt one")
        self._send("prompt two")
        self.page.press("#input", "ArrowUp")
        self.assertEqual(self.page.input_value("#input"), "prompt two")
        self.page.press("#input", "ArrowUp")
        self.assertEqual(self.page.input_value("#input"), "prompt one")
        self.page.press("#input", "ArrowDown")
        self.assertEqual(self.page.input_value("#input"), "prompt two")

    def test_saving_a_prompt_template_then_inserting_it(self):
        self.page.fill("#input", "Please review this diff")
        self.page.click("#tplBtn")
        self._menu_item("Save current prompt as template…").click()
        self.page.wait_for_selector("#dlg[open] #pf-name")
        self.page.fill("#pf-name", "review")
        self.page.locator("#dlg .btn.primary").click()
        self.page.wait_for_selector("#dlg", state="hidden")
        self.page.fill("#input", "")
        self.page.click("#tplBtn")
        self._menu_item("review").click()
        self.assertEqual(self.page.input_value("#input"), "Please review this diff")

    def test_deleting_a_template(self):
        self.page.evaluate("localStorage.setItem('at.tpl', JSON.stringify([{name:'a', text:'A'}]))")
        self.page.click("#tplBtn")
        self._menu_item("Delete a template…").click()
        self.page.wait_for_selector("#dlg[open] .pick")
        self.page.locator("#dlg .pick input[type=checkbox]").first.check()
        self.page.locator("#dlg .btn.primary").click()
        self.page.wait_for_selector("#dlg", state="hidden")
        self.assertEqual(self.page.evaluate("JSON.parse(localStorage.getItem('at.tpl')).length"), 0)


class CommandPalette(_Browser):
    agents = [WORKING, READY, ATTN]

    def _open(self):
        self.page.keyboard.press("Control+k")
        self.page.wait_for_selector("#palette", state="visible")
        self.assertEqual(self.page.evaluate("document.activeElement.id"), "palette-q")

    def test_opens_with_ctrl_k_and_escape_closes(self):
        self._open()
        self.page.keyboard.press("Escape")
        self.page.wait_for_selector("#palette", state="hidden")

    def test_filter_then_enter_switches_agent(self):
        self._open()
        self.page.fill("#palette-q", "Switch to beta")
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector("#palette", state="hidden")
        self.assertIn("beta", self.page.inner_text("#head .nm"))

    def test_new_agent_command_opens_the_dialog(self):
        self._open()
        self.page.fill("#palette-q", "New agent")
        self.page.keyboard.press("Enter")
        self.page.wait_for_selector("#newAgent[open]")

    def test_send_to_command_selects_the_agent_and_focuses_the_composer(self):
        self._open()
        self.page.fill("#palette-q", "Send to gamma")
        self.page.keyboard.press("Enter")
        self.assertIn("gamma", self.page.inner_text("#head .nm"))
        self.assertEqual(self.page.evaluate("document.activeElement.id"), "input")

    def test_stop_command_interrupts_the_working_agent(self):
        self._open()
        self.page.fill("#palette-q", "Stop alpha")
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(300)
        payload = next(p for p in self.payloads if p.get("op") == "agent_interrupt")
        self.assertEqual(payload.get("target"), "sess_w")

    def test_go_to_tab_command_switches_the_view(self):
        self._open()
        self.page.fill("#palette-q", "Go to Live")
        self.page.keyboard.press("Enter")
        self.assertTrue(self.page.evaluate("getComputedStyle(document.getElementById('term')).display !== 'none'"))


class Notifications(_Browser):
    agents = [WORKING, READY]

    def _grant(self):
        self.page.context.grant_permissions(["notifications"], origin="http://localhost")

    def test_bell_is_visible_and_toggles_the_saved_preference(self):
        self.assertTrue(self.page.evaluate("typeof Notification !== 'undefined'"))
        self.assertNotEqual(self.page.evaluate("getComputedStyle(document.getElementById('notifyBtn')).display"), "none")
        self._grant()
        self.page.click("#notifyBtn")
        self.assertEqual(self.page.evaluate("localStorage.getItem('at.notify')"), "1")
        self.assertTrue(self.page.locator("#notifyBtn").evaluate("b => b.classList.contains('on')"))
        self.page.click("#notifyBtn")
        self.assertEqual(self.page.evaluate("localStorage.getItem('at.notify')"), "0")

    def test_finished_turn_and_new_permission_fire_notifications(self):
        self._grant()
        self.page.evaluate("localStorage.setItem('at.notify','1')")
        self.page.evaluate("window.notify = (t, b) => { (window.__n = window.__n || []).push([t, b]); }")
        # A non-selected agent (beta) starts working, then finishes its turn.
        self.agents = [WORKING, {**READY, "state": "working"}]
        self._push()
        self.agents = [WORKING, {**READY, "state": "waiting_input"}]
        self._push()
        self.page.wait_for_function("(window.__n || []).some(n => /finished/.test(n[0]))")
        # ... and then raises a permission request.
        self.agents = [WORKING, {**READY, "state": "waiting_input", "interactions": [PERMISSION]}]
        self._push()
        self.page.wait_for_function("(window.__n || []).some(n => /asks/.test(n[0]))")

    def test_service_worker_is_registered(self):
        self.page.wait_for_function("window.__swRegistered !== null")
        self.assertEqual(self.page.evaluate("window.__swRegistered"), "/sw.js")


class Processes(_Browser):
    def test_processes_tab_lists_shells_and_signals(self):
        self.shells = [{"pid": 42, "command": "npm test", "state": "R", "elapsed": 12.0,
                        "cpu": 1.2, "children": [], "last_output_ago": 5.0,
                        "output": {"kind": "file", "task": "t42"}}]
        self.page.click("#tab-proc")
        self.page.wait_for_selector("#procs-in .sh")
        self.assertIn("npm test", self.page.inner_text("#procs-in"))
        self.page.locator("#procs-in .sh").first.get_by_role("button", name="Ctrl-C").click()
        self.page.wait_for_selector("#dlg[open]")
        self.page.locator("#dlg .btn.primary").click()
        self.assertIn("agent_process_signal", self.calls)
        payload = next(p for p in self.payloads if p.get("op") == "agent_process_signal")
        self.assertEqual(payload.get("pid"), 42)
        self.assertEqual(payload.get("signal"), "INT")


class Mobile(_Browser):
    viewport = {"width": 390, "height": 800}
    auto_select = False
    wait_for_messages = False

    def test_drawer_opens_with_the_menu_button_and_closes_on_the_scrim(self):
        self.assertNotEqual(self.page.evaluate("getComputedStyle(document.getElementById('menuBtn')).display"), "none")
        self.assertFalse(self.page.locator("#shell").evaluate("s => s.classList.contains('drawer')"))
        self.page.click("#menuBtn")
        self.assertTrue(self.page.locator("#shell").evaluate("s => s.classList.contains('drawer')"))
        self.assertTrue(self.page.locator("#scrim").is_visible())
        self.page.locator("#scrim").click(position={"x": 380, "y": 20})  # right of the drawer
        self.assertFalse(self.page.locator("#shell").evaluate("s => s.classList.contains('drawer')"))

    def test_search_shortcut_opens_the_drawer(self):
        self.page.keyboard.press("/")
        self.assertTrue(self.page.locator("#shell").evaluate("s => s.classList.contains('drawer')"))
        self.assertEqual(self.page.evaluate("document.activeElement.id"), "q")


if __name__ == "__main__":
    unittest.main()
