from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from crewhall import chat as conversations
from crewhall.office import guide as office_guide


def _lang(value: str):
    def _get(key, default=None):
        if key == "chat.language":
            return value
        if key == "conversations.dir":
            return tempfile.gettempdir()
        return default
    return _get


class RulesLanguage(unittest.TestCase):
    """Fase E3: the rules/guides follow chat.language."""

    def test_chat_rules_english(self) -> None:
        with mock.patch("crewhall.settings.get", side_effect=_lang("en")):
            text = conversations.rules_text()
        self.assertIn("isolated crewhall chat", text)
        self.assertNotIn("agente aislado", text)

    def test_chat_rules_spanish(self) -> None:
        with mock.patch("crewhall.settings.get", side_effect=_lang("es")):
            text = conversations.rules_text()
        self.assertIn("agente aislado", text)

    def test_office_guide_language(self) -> None:
        self.assertIn("professional guide", office_guide.guide_text("en"))
        self.assertIn("guía profesional", office_guide.guide_text("es"))
        self.assertIn("flawless documents", office_guide.rules_section("en"))
        self.assertIn("documentos impecables", office_guide.rules_section("es"))

    def test_ensure_rules_writes_the_selected_language(self) -> None:
        directory = tempfile.mkdtemp(prefix="at-lang-")
        with mock.patch("crewhall.settings.get", side_effect=_lang("en")):
            conversations.ensure_rules(directory)
        with open(os.path.join(directory, "CLAUDE.md"), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("isolated crewhall chat", body)
        with open(os.path.join(directory, "ONLYOFFICE.md"), encoding="utf-8") as fh:
            self.assertIn("professional guide", fh.read())


if __name__ == "__main__":
    unittest.main()
