from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from crewhall.controller import Controller
from crewhall.daemon import Server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _modules_after(import_line: str) -> set[str]:
    code = (
        "import sys; "
        f"{import_line}; "
        "print('\\n'.join(m for m in sys.modules if m.startswith('crewhall')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=REPO, timeout=30)
    return set(out.stdout.split())


class ImportIsolation(unittest.TestCase):
    def test_messaging_does_not_load_chat_or_office(self):
        mods = _modules_after("import crewhall.messaging")
        self.assertNotIn("crewhall.office", mods)
        self.assertNotIn("crewhall.office_guide", mods)
        self.assertNotIn("crewhall.office_collab", mods)
        self.assertNotIn("crewhall.conversations", mods)

    def test_core_does_not_load_office(self):
        mods = _modules_after("import crewhall")
        self.assertNotIn("crewhall.office", mods)
        self.assertNotIn("crewhall.conversations", mods)


class OfficeDisabled(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ati-layer-", dir="/tmp")
        env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.root, "c"),
            "XDG_STATE_HOME": os.path.join(self.root, "s"),
            "XDG_RUNTIME_DIR": os.path.join(self.root, "r"),
        })
        env.start()
        self.addCleanup(env.stop)
        os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700)
        self.server = Server(Controller(adopt=False, persist=False),
                             os.path.join(self.root, "r", "d.sock"))

    def test_collab_ops_answer_disabled(self):
        result = self.server.dispatch({"op": "office_collab_list"})
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("disabled"))


if __name__ == "__main__":
    unittest.main()
