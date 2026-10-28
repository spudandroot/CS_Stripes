"""Tests for i18n (English/Spanish). Run: python -m unittest -v"""

import ast
import os
import re
import unittest

import i18n
from i18n import tr

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = ("stripes.py", "label_design.py", "zpl_view.py")


def texts_in_code():
    """Every literal passed to tr() or N_() in the app, with where it is."""
    found = {}
    for name in SOURCES:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("tr", "N_")
                    and node.args and isinstance(node.args[0], ast.Constant)):
                found.setdefault(node.args[0].value, f"{name}:{node.lineno}")
    return found


def blanks(text):
    return sorted(re.findall(r"\{(\w+)\}", text))


class I18nTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_every_text_has_spanish(self):
        missing = [f"{where}: {text!r}" for text, where in texts_in_code().items() if text not in i18n.ES]
        self.assertEqual(missing, [])

    def test_spanish_fills_the_same_blanks(self):
        for english, spanish in i18n.ES.items():
            self.assertEqual(blanks(english), blanks(spanish), english)

    def test_switching_back_finds_the_english(self):
        # A live switch maps text on screen back to English, so no two texts may share a translation
        spanish = list(i18n.ES.values())
        self.assertEqual(sorted(s for s in set(spanish) if spanish.count(s) > 1), [])
        for english, translated in i18n.ES.items():
            if translated in i18n.ES and translated != english:
                self.fail(f"{translated!r} is both the Spanish for {english!r} and an English text")

    def test_tr(self):
        self.assertEqual(tr("Sent {n} labels to {name}.", n=3, name="Line 1"), "Sent 3 labels to Line 1.")
        i18n.set_language("es")
        self.assertEqual(tr("Sent {n} labels to {name}.", n=3, name="Line 1"), "Se enviaron 3 etiquetas a Line 1.")
        self.assertEqual(tr("Not in the table"), "Not in the table")
        i18n.set_language("xx")
        self.assertEqual(i18n.language(), "en")

    def test_retranslate(self):
        i18n.set_language("es")
        self.assertEqual(i18n.retranslate("  Simple  "), "  Sencillo  ")
        self.assertEqual(i18n.retranslate("ZT410 via IP 10.0.0.5"), "ZT410 via IP 10.0.0.5")  # not fixed text
        i18n.set_language("en")
        self.assertEqual(i18n.retranslate("  Sencillo  "), "  Simple  ")
        self.assertEqual(i18n.retranslate("Print"), "Print")

    def test_label_messages_follow_the_language(self):
        import label_design as ld
        design = ld.Design([ld.Element("qr", "")])
        i18n.set_language("es")
        self.assertEqual(ld.render(design, ld.Label(406, 203, 8)).errors, ["Elemento 1: el código QR está vacío."])


if __name__ == "__main__":
    unittest.main()
