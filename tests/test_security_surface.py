from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from crewhall import chat as conversations
from crewhall import hooks, office, paths
from crewhall.controller import Controller
from crewhall.messaging import MessagingError
from tests.support import FakeHarness


class ClaudeSandbox(unittest.TestCase):
    """D1: the generated Claude settings deny the control-plane state."""

    def test_settings_deny_runtime_and_state(self) -> None:
        settings = hooks.claude_settings()
        deny = settings["permissions"]["deny"]
        self.assertTrue(any(paths.runtime_dir() in rule for rule in deny))
        self.assertTrue(any(paths.state_dir() in rule for rule in deny))
        self.assertTrue(any("Read(" in rule for rule in deny))

    def test_deny_rules_cover_the_token_with_absolute_paths(self) -> None:
        # Regression: rules written as ``Read(/abs/path)`` are anchored at the
        # settings file, not at ``/``, and the token (no extension, in the state
        # dir) was not covered at all.
        deny = hooks.claude_settings()["permissions"]["deny"]
        token = paths.control_token_path()
        self.assertIn(f"Read(/{token})", deny)
        self.assertIn(f"Edit(/{token})", deny)
        for rule in deny:
            tool, _, rest = rule.partition("(")
            self.assertIn(tool, ("Read", "Edit"), rule)  # Write(path) is ignored by Claude
            self.assertTrue(rest.startswith("//"), rule)

    def test_generated_file_carries_the_deny(self) -> None:
        path = hooks.ensure_claude_settings()
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("permissions", data)
        self.assertTrue(data["permissions"]["deny"])


class OfficeBaseUrl(unittest.TestCase):
    """D2: office_collab_open must not accept an arbitrary agent base."""

    def test_agent_base_unconfigured_rejects_arbitrary(self) -> None:
        with mock.patch("crewhall.settings.get", return_value=""):
            self.assertEqual(office.agent_base("http://evil.example"), "")

    def test_agent_base_same_origin_is_accepted(self) -> None:
        values = {"office.base_url": "", "web.public_url": "",
                  "office.public_url": "http://127.0.0.1:8081"}
        with mock.patch("crewhall.settings.get",
                        side_effect=lambda k, d=None: values.get(k, d)):
            self.assertEqual(office.agent_base("http://127.0.0.1:8081"),
                             "http://127.0.0.1:8081")
            self.assertEqual(office.agent_base("http://evil.example"), "")

    def test_agent_base_configured_wins(self) -> None:
        with mock.patch("crewhall.settings.get",
                        side_effect=lambda k, d=None: "http://cfg" if k == "office.base_url" else d):
            self.assertEqual(office.agent_base("http://evil.example"), "http://cfg")

    def test_web_base_url_still_trusts_the_request(self) -> None:
        # The Web UI passes its own request base; that is legitimate.
        with mock.patch("crewhall.settings.get", return_value=""):
            self.assertEqual(office.base_url("http://127.0.0.1:9999"),
                             "http://127.0.0.1:9999")


class _ControllerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-sec-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.controller = Controller(adopt=False, persist=False)
        self.workspace = os.path.join(self.root, "work")
        os.makedirs(self.workspace)
        self.lead = FakeHarness("lead")
        self.worker = FakeHarness("worker")
        self.lead.session.spec.cwd = self.workspace
        self.worker.session.spec.cwd = self.workspace
        self.controller.register_agent(self.lead)
        self.controller.register_agent(self.worker)
        self.controller.teams.create(
            "crew1", [self.lead.agent_id, self.worker.agent_id], workspace=self.workspace)
        self.worker_token = self.controller._agent_token(self.worker.agent_id)
        self.lead_token = self.controller._agent_token(self.lead.agent_id)
        self.conv = conversations.create("chat", team="crew1")


class PublishArtifactSafety(_ControllerCase):
    def test_symlink_is_rejected(self) -> None:
        outside = os.path.join(self.root, "outside.txt")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("secret")
        link = os.path.join(self.workspace, "link.txt")
        os.symlink(outside, link)
        with self.assertRaises(conversations.ConversationError):
            self.controller.publish_artifact(
                self.worker.agent_id, self.worker_token, self.conv["id"], link)

    def test_directory_is_rejected(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            self.controller.publish_artifact(
                self.worker.agent_id, self.worker_token, self.conv["id"], self.workspace)

    def test_too_large_is_rejected(self) -> None:
        src = os.path.join(self.workspace, "big.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("x" * 4096)
        with mock.patch.object(conversations, "max_bytes", return_value=10):
            with self.assertRaises(conversations.ConversationError):
                self.controller.publish_artifact(
                    self.worker.agent_id, self.worker_token, self.conv["id"], src)

    def test_regular_file_is_accepted(self) -> None:
        src = os.path.join(self.workspace, "ok.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("fine")
        out = self.controller.publish_artifact(
            self.worker.agent_id, self.worker_token, self.conv["id"], src)
        self.assertTrue(out["artifact"])


class SendLimits(_ControllerCase):
    def test_body_too_large(self) -> None:
        with mock.patch("crewhall.settings.get",
                        side_effect=lambda k, d=None: 10 if k == "messaging.max_body_chars" else d):
            with self.assertRaises(MessagingError):
                self.controller.send_message_as(
                    self.lead.agent_id, self.lead_token, self.worker.agent_id, "x" * 50)

    def test_rate_limited(self) -> None:
        def _get(key, default=None):
            if key == "messaging.rate_per_minute":
                return 2
            if key == "messaging.max_body_chars":
                return 16000
            return default

        with mock.patch("crewhall.settings.get", side_effect=_get):
            self.controller.send_message_as(
                self.lead.agent_id, self.lead_token, self.worker.agent_id, "one")
            self.controller.send_message_as(
                self.lead.agent_id, self.lead_token, self.worker.agent_id, "two")
            with self.assertRaises(MessagingError):
                self.controller.send_message_as(
                    self.lead.agent_id, self.lead_token, self.worker.agent_id, "three")


if __name__ == "__main__":
    unittest.main()
