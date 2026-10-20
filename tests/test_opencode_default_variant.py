"""OpenCode v2 sessions default to the ``max`` reasoning effort (variant)."""
from __future__ import annotations

import unittest
from unittest import mock

from crewhall import Controller, OpenCodeHarness, notices, settings
from crewhall.opencode_link import OpenCodeLink

from .fake_opencode_v2 import PASSWORD, FakeOpenCodeV2, start_server
from .support import FakeSession

SID = "ses_mine"
MODEL = {"providerID": "opencode-go", "id": "deepseek-v4.1-flash"}
CATALOG = [{
    "id": "deepseek-v4.1-flash", "modelID": "deepseek-v4.1-flash",
    "providerID": "opencode-go",
    "variants": [{"id": "low"}, {"id": "high"}, {"id": "max"}],
}]


class DefaultVariant(unittest.TestCase):
    def setUp(self):
        notices.clear()
        self.addCleanup(notices.clear)
        FakeOpenCodeV2.reset()
        FakeOpenCodeV2.models = list(CATALOG)
        self.server, self.port = start_server()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.link = OpenCodeLink(self.port, PASSWORD, api="v2")
        self.controller = Controller(adopt=False)

    def _session(self, variant):
        model = dict(MODEL)
        if variant is not None:
            model["variant"] = variant
        FakeOpenCodeV2.sessions[SID] = {
            "id": SID, "agent": "build", "model": model,
            "time": {"created": 1, "updated": 0}, "location": {"directory": "/tmp"},
        }

    def _posted(self):
        return [body for method, path, body in FakeOpenCodeV2.requests
                if method == "POST" and path.endswith(f"/{SID}/model")]

    def test_opencode_defaults_to_max_and_it_can_be_turned_off(self):
        self.assertEqual(settings.provider("opencode")["default_variant"], "max")
        self.assertEqual(settings.provider("claude")["default_variant"], "")

    def test_a_session_on_the_default_variant_is_raised_to_max(self):
        for current in (None, "default"):
            FakeOpenCodeV2.reset()
            FakeOpenCodeV2.models = list(CATALOG)
            self._session(current)
            self.controller._ensure_default_variant(self.link, SID)
            self.assertEqual(self._posted()[-1]["model"]["variant"], "max", current)

    def test_an_explicit_variant_chosen_by_the_user_is_respected(self):
        self._session("low")
        self.controller._ensure_default_variant(self.link, SID)
        self.assertEqual(self._posted(), [])

    def test_already_max_is_not_posted_again(self):
        self._session("max")
        self.controller._ensure_default_variant(self.link, SID)
        self.assertEqual(self._posted(), [])

    def test_a_model_without_that_variant_is_left_alone(self):
        FakeOpenCodeV2.models = [{**CATALOG[0], "variants": [{"id": "low"}]}]
        self._session(None)
        self.controller._ensure_default_variant(self.link, SID)
        self.assertEqual(self._posted(), [])

    def test_an_empty_setting_disables_it(self):
        self._session(None)
        with mock.patch.object(settings, "provider",
                               return_value={"default_variant": ""}):
            self.controller._ensure_default_variant(self.link, SID)
        self.assertEqual(self._posted(), [])

    def test_a_requested_model_without_a_hash_gets_the_default_variant(self):
        ref = self.controller._v2_model_ref(self.link, "opencode-go/deepseek-v4.1-flash")
        self.assertNotIn("variant", ref)  # the ref itself stays literal
        self._session(None)
        report = self.controller._configure_v2_session(
            "a1", "opencode", self.link,
            ["--model", "opencode-go/deepseek-v4.1-flash"], session_id=SID)
        self.assertEqual(report["model_variant"], "max")
        self.assertEqual(self._posted()[-1]["model"]["variant"], "max")

    def test_resync_applies_it_to_a_session_that_is_already_running(self):
        self.controller.track_conversations = True
        harness = OpenCodeHarness(FakeSession(screen="", name="x"), name="x")
        harness.conversation_id = SID
        harness.link = self.link
        self.controller.register_agent(harness)
        self._session(None)
        self.controller._resync_opencode_v2(harness, self.link)
        self.assertEqual(self._posted()[-1]["model"]["variant"], "max")


if __name__ == "__main__":
    unittest.main()
