from __future__ import annotations

import logging
import os
import tempfile
import unittest

from crewhall import paths
from crewhall.daemon import LOG_FORMAT, build_log_handler
from crewhall.doctor import check_daemon_log


class LoggingRotation(unittest.TestCase):
    """Fase B2: daemon.log rotates and carries structured fields."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="at-log-")
        self.path = os.path.join(self.dir, "daemon.log")

    def _logger(self, handler: logging.Handler) -> logging.Logger:
        logger = logging.getLogger(f"crewhall.test.rot.{id(handler)}")
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        self.addCleanup(logger.handlers.clear)
        return logger

    def test_rotates_when_full(self) -> None:
        handler = build_log_handler(self.path, max_bytes=256, backup_count=2)
        logger = self._logger(handler)
        for i in range(80):
            logger.info("line %d %s", i, "x" * 40)
        handler.close()
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(os.path.exists(self.path + ".1"))
        # No rotation beyond the configured backup count.
        self.assertFalse(os.path.exists(self.path + ".3"))

    def test_format_includes_module_and_agent_id(self) -> None:
        self.assertIn("%(agent_id)s", LOG_FORMAT)
        handler = build_log_handler(self.path, max_bytes=10 ** 6, backup_count=0)
        logger = self._logger(handler)
        logger.info("hello", extra={"agent_id": "agent-123"})
        logger.info("no agent here")
        handler.close()
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("agent-123", text)
        self.assertIn("crewhall.test.rot", text)
        self.assertIn("no agent here", text)

    def test_doctor_reports_the_log_size(self) -> None:
        log = paths.log_path()
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("x" * 4096)
        check = check_daemon_log()
        self.assertEqual(check["name"], "log")
        self.assertEqual(check["status"], "ok")
        self.assertIn("MiB", check["detail"])


if __name__ == "__main__":
    unittest.main()
