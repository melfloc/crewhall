from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from crewhall import office, office_setup, settings


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-office-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.d,
            "XDG_STATE_HOME": self.d,
        })
        env.start(); self.addCleanup(env.stop)
        settings.patch({
            "office.enabled": True,
            "office.jwt_enabled": True,
            "office.jwt_secret": "s3cr3t",
            "office.public_url": "https://office.example/",
            "office.base_url": "https://crewhall.example/",
        })


class Jwt(_Base):
    def test_roundtrip_and_tamper(self):
        token = office.jwt_encode({"a": 1}, ttl=60)
        self.assertEqual(office.jwt_decode(token)["a"], 1)
        with self.assertRaises(office.OfficeError):
            office.jwt_decode(token[:-2] + "xx")

    def test_expiry(self):
        token = office.jwt_encode({"a": 1}, ttl=-1)
        with self.assertRaises(office.OfficeError):
            office.jwt_decode(token)

    def test_download_token(self):
        token = office.download_token("c-1", "out/doc.docx")
        self.assertTrue(office.verify_download_token(token, "c-1", "out/doc.docx"))
        self.assertFalse(office.verify_download_token(token, "c-2", "out/doc.docx"))
        self.assertFalse(office.verify_download_token(token, "c-1", "other.docx"))
        self.assertFalse(office.verify_download_token("nope", "c-1", "out/doc.docx"))


class Config(_Base):
    def test_origins_and_types(self):
        self.assertEqual(office.public_origin(), "https://office.example")
        self.assertEqual(office.base_url("http://ignored"), "https://crewhall.example")
        self.assertEqual(office.document_type("a.docx"), ("word", "docx", False))
        self.assertEqual(office.document_type("a.pdf"), ("pdf", "pdf", True))

    def test_build_config_has_three_urls(self):
        path = os.path.join(self.d, "doc.docx")
        with open(path, "wb") as fh:
            fh.write(b"hello")
        cfg = office.build_config(
            conversation_id="c-1", relpath="doc.docx", name="doc.docx", path=path,
            document_base="https://crewhall.example", callback_base="https://crewhall.example",
            user_id="web", user_name="you",
        )
        self.assertEqual(cfg["documentType"], "word")
        self.assertTrue(cfg["document"]["url"].startswith(
            "https://crewhall.example/api/conversation/artifact?"))
        self.assertIn("dt=", cfg["document"]["url"])
        self.assertEqual(cfg["editorConfig"]["callbackUrl"],
                         "https://crewhall.example/api/office/callback?id=c-1&path=doc.docx")
        self.assertIn("token", cfg)

    def test_file_key_changes_with_mtime(self):
        path = os.path.join(self.d, "doc.docx")
        with open(path, "wb") as fh:
            fh.write(b"one")
        key1 = office.file_key(path)
        time.sleep(0.01)
        with open(path, "wb") as fh:
            fh.write(b"two")
        self.assertNotEqual(key1, office.file_key(path))

    def test_callback_requires_token_when_enabled(self):
        self.assertFalse(office.callback_body_ok({}))
        self.assertTrue(office.callback_body_ok({"token": office.jwt_encode({"x": 1})}))


class SetupPlan(unittest.TestCase):
    def _plan(self, port=8081, bind=None, ts_ip=None, dns=None, gw="172.17.0.1"):
        return office_setup.connection_plan(port, bind, ts_ip=ts_ip, dns=dns,
                                            bridge_gw=gw, crewhall_port=8765)

    def test_default_without_tailscale(self):
        plan = self._plan()
        self.assertEqual(plan["bind"], "127.0.0.1")
        self.assertEqual(plan["public_url"], "http://127.0.0.1:8081")
        self.assertEqual(plan["base_url"], "http://172.17.0.1:8765")

    def test_tailscale_binds_the_tailnet_address(self):
        plan = self._plan(ts_ip="100.1.2.3", dns="box.ts.net")
        self.assertEqual(plan["bind"], "100.1.2.3")
        self.assertEqual(plan["public_url"], "http://box.ts.net:8081")
        self.assertEqual(plan["base_url"], "http://100.1.2.3:8765")

    def test_tailscale_without_dns_uses_the_ip(self):
        plan = self._plan(ts_ip="100.1.2.3", dns=None)
        self.assertEqual(plan["public_url"], "http://100.1.2.3:8081")

    def test_explicit_bind_still_wins_for_the_browser(self):
        plan = self._plan(9000, "0.0.0.0", ts_ip="100.1.2.3", dns="box.ts.net")
        self.assertEqual(plan["bind"], "0.0.0.0")
        self.assertEqual(plan["public_url"], "http://box.ts.net:9000")
        self.assertEqual(plan["base_url"], "http://100.1.2.3:8765")

    def test_compose_is_pinned_and_has_a_healthcheck(self):
        self.assertIn("onlyoffice/documentserver:8.1.3", office_setup.COMPOSE)
        self.assertNotIn("documentserver:8.1\n", office_setup.COMPOSE)
        self.assertIn("healthcheck:", office_setup.COMPOSE)
        self.assertIn("JWT_ENABLED=true", office_setup.COMPOSE)

    def test_probe_host_uses_the_bound_address_not_loopback(self):
        # The DS binds the Tailscale IP, so probing 127.0.0.1 would never work.
        self.assertEqual(office_setup._probe_host("100.1.2.3"), "100.1.2.3")
        self.assertEqual(office_setup._probe_host("127.0.0.1"), "127.0.0.1")
        for wildcard in (None, "", "0.0.0.0", "::", "*"):
            self.assertEqual(office_setup._probe_host(wildcard), "127.0.0.1")


class FirewallIngress(unittest.TestCase):
    def test_ingress_ifaces_tailscale_first_then_default(self):
        with mock.patch.object(office_setup.os.path, "exists", return_value=True), \
                mock.patch.object(office_setup.subprocess, "run") as run:
            run.return_value = mock.Mock(
                stdout="default via 192.168.200.1 dev enp4s0 proto dhcp metric 100\n",
                returncode=0)
            ifaces = office_setup._ingress_ifaces()
        self.assertEqual(ifaces, ["tailscale0", "enp4s0"])

    def test_ingress_ifaces_dedupes_tailscale_default(self):
        with mock.patch.object(office_setup.os.path, "exists", return_value=False), \
                mock.patch.object(office_setup.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="default via 100.0.0.1 dev tailscale0\n", returncode=0)
            self.assertEqual(office_setup._ingress_ifaces(), ["tailscale0"])


class ExternalSetup(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="at-office-ext-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.d,
            "XDG_STATE_HOME": self.d,
        })
        env.start(); self.addCleanup(env.stop)

    def test_external_points_settings_and_reports_health(self):
        with mock.patch.object(office_setup, "_healthcheck", return_value=True), \
                mock.patch.object(office_setup, "_tailscale_ip", return_value=None):
            res = office_setup._setup_external("http://ds.local:8081/", "sekret", log=lambda *a: None)
        self.assertTrue(res["healthy"])
        self.assertTrue(res["external"])
        self.assertTrue(res["secret_set"])
        self.assertEqual(settings.get("office.public_url"), "http://ds.local:8081")
        self.assertEqual(settings.get("office.jwt_secret"), "sekret")
        self.assertTrue(settings.get("office.enabled"))


if __name__ == "__main__":
    unittest.main()
