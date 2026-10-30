"""Tests for i18n (English/Spanish). Run: python -m unittest -v"""

import ast
import importlib
import os
import re
import sys
import unittest

import i18n
from i18n import tr

HERE = os.path.dirname(os.path.abspath(__file__))
# Every MapleJet module in the folder, as soon as it exists; not the test-only fake or the
# key file the release build generates
HX_SOURCES = tuple(sorted(name for name in os.listdir(HERE)
                          if name.startswith("hx_") and name.endswith(".py")
                          and name not in ("hx_fake.py", "hx_key.py")))
SOURCES = ("stripes.py", "label_design.py", "zpl_view.py") + HX_SOURCES


def setUpModule():
    # Importing a MapleJet module registers its own Spanish texts (i18n.add_spanish)
    for name in HX_SOURCES:
        importlib.import_module(name[:-3])


def texts_in_code():
    """Every literal passed to tr() or N_() (or i18n.tr(), i18n.N_()) in the app, with where it is."""
    found = {}
    for name in SOURCES:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)):
                continue
            func = node.func
            if ((isinstance(func, ast.Name) and func.id in ("tr", "N_"))
                    or (isinstance(func, ast.Attribute) and func.attr in ("tr", "N_")
                        and isinstance(func.value, ast.Name) and func.value.id == "i18n")):
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
        self.assertEqual(i18n.retranslate("ZT410 via IP 192.0.2.5"), "ZT410 via IP 192.0.2.5")  # not fixed text
        i18n.set_language("en")
        self.assertEqual(i18n.retranslate("  Sencillo  "), "  Simple  ")
        self.assertEqual(i18n.retranslate("Print"), "Print")

    def test_label_messages_follow_the_language(self):
        import label_design as ld
        design = ld.Design([ld.Element("qr", "")])
        i18n.set_language("es")
        self.assertEqual(ld.render(design, ld.Label(406, 203, 8)).errors, ["Elemento 1: el código QR está vacío."])


class MapleJetTextTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_every_hx_module_is_checked(self):
        for name in ("hx_core.py", "hx_model.py", "hx_win.py"):
            self.assertIn(name, SOURCES)
        self.assertNotIn("hx_fake.py", SOURCES)
        self.assertNotIn("hx_key.py", SOURCES)
        for name in HX_SOURCES:
            self.assertIn(name[:-3], sys.modules)

    def test_every_module_catalogue_is_registered(self):
        # A module that defines ES but never calls add_spanish(ES) would show English in Spanish
        for name in HX_SOURCES:
            for english, spanish in getattr(sys.modules[name[:-3]], "ES", {}).items():
                self.assertEqual(i18n.ES.get(english), spanish, f"{name}: {english!r}")

    def test_new_stripes_texts(self):
        i18n.set_language("es")
        self.assertEqual(tr("MapleJet monitor"), "Monitor MapleJet")
        self.assertEqual(tr("Zebra labels and MapleJet bag printing"), "Etiquetas Zebra e impresión de bolsas MapleJet")
        self.assertEqual(tr("Version {version} (development key)", version="v2.0.0"),
                         "Versión v2.0.0 (clave de desarrollo)")
        self.assertEqual(tr("Zebra label printing"), "Impresión de etiquetas Zebra")   # the old key stays

    def test_add_spanish(self):
        english, spanish = "Test text for add_spanish", "Texto de prueba para add_spanish"
        try:
            i18n.add_spanish({english: spanish})
            i18n.set_language("es")
            self.assertEqual(tr(english), spanish)
            self.assertEqual(i18n.retranslate(spanish), spanish)
            i18n.set_language("en")
            self.assertEqual(i18n.retranslate(spanish), english)
            # The same pair again, and a pair the catalogue already has, are fine
            i18n.add_spanish({english: spanish, "Print": "Imprimir"})
        finally:
            i18n.ES.pop(english, None)
            i18n._ENGLISH.pop(spanish, None)

    def test_add_spanish_refuses_a_conflicting_pair(self):
        before = (dict(i18n.ES), dict(i18n._ENGLISH))
        with self.assertRaises(ValueError):
            i18n.add_spanish({"Print": "Imprima"})               # another Spanish for a known text
        with self.assertRaises(ValueError):
            i18n.add_spanish({"Print this now": "Imprimir"})     # a Spanish text another text has
        self.assertEqual((i18n.ES, i18n._ENGLISH), before)


if __name__ == "__main__":
    unittest.main()
