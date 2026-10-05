from __future__ import annotations

import shutil
import unittest

from agent_terminal import InteractiveSession, SessionSpec

from .support import BrokenBackend, ContractMixin


class PtyContract(ContractMixin, unittest.TestCase):
    backend_name = "pty"

    def test_backend_contract_pty(self) -> None:
        self.contract_flow()


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxContract(ContractMixin, unittest.TestCase):
    backend_name = "tmux"

    def test_backend_contract_tmux(self) -> None:
        self.contract_flow()


class ContractGuard(unittest.TestCase):
    def test_contract_detects_broken_backend(self) -> None:
        from agent_terminal import SessionTimeout

        spec = SessionSpec(command=["/bin/bash"])
        session = InteractiveSession(BrokenBackend(), spec)
        session.start()
        try:
            with self.assertRaises(SessionTimeout):
                session.read_until("CONTRACT_42", timeout=0.5)
        finally:
            session.close()

    def test_stub_backend_reports_running_but_no_output(self) -> None:
        spec = SessionSpec(command=["/bin/bash"])
        session = InteractiveSession(BrokenBackend(), spec)
        session.start()
        try:
            self.assertTrue(session.status.alive)
            self.assertEqual(session.capture(), "")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
