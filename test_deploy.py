"""Tests for how Stripes is built and released (deploy.md §6.2): the printer-password key stays out
of the repository, the fake printer never ships, the app runs without what the exe leaves out,
and the self-check works from source. Run: python -m unittest -v

The self-check tests need a display (on Linux: xvfb-run -a python -m unittest -v); they skip
themselves when Tk can't start.
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from unittest import mock

import hx_time
import hx_ui
import i18n
import stripes
from hx_model import TimeServerError

HERE = os.path.dirname(os.path.abspath(__file__))

# What the exe leaves out (pyinstaller.spec EXCLUDES) that app code could reach for
BLOCKED = ("_hashlib", "ssl", "_ssl", "multiprocessing", "hx_fake")


def app_sources():
    """Every file of app code: everything except the tests, the fake printer and the generated key."""
    hx = sorted(name for name in os.listdir(HERE)
                if name.startswith("hx_") and name.endswith(".py") and name not in ("hx_fake.py", "hx_key.py"))
    return ["stripes.py", "label_design.py", "zpl_view.py", "i18n.py"] + hx


def fake_imports(source, name):
    """Where the code imports hx_fake, statically or by a constant name."""
    found = []
    for node in ast.walk(ast.parse(source, name)):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "hx_fake" for alias in node.names):
                found.append(f"{name}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "hx_fake" or any(a.name == "hx_fake" for a in node.names):
                found.append(f"{name}:{node.lineno}")
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            first = node.args[0]
            if (called in ("import_module", "__import__") and isinstance(first, ast.Constant)
                    and isinstance(first.value, str) and first.value.split(".")[0] == "hx_fake"):
                found.append(f"{name}:{node.lineno}")
    return found


class RepoTests(unittest.TestCase):
    def test_key_file_ignored(self):
        with open(os.path.join(HERE, ".gitignore"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertIn("hx_key.py", lines)

    def test_no_key_in_checkout(self):
        # CI runs the tests before "Stamp version", and make_version.py without the secret removes it
        self.assertFalse(os.path.exists(os.path.join(HERE, "hx_key.py")),
                         "hx_key.py is in the checkout: delete it (python make_version.py dev)")

    def test_app_never_imports_fake(self):
        found = []
        for name in app_sources():
            with open(os.path.join(HERE, name), encoding="utf-8") as f:
                found += fake_imports(f.read(), name)
        self.assertEqual(found, [])

    def test_fake_import_scan_finds_each_form(self):
        for code in ("import hx_fake", "import hx_fake as f", "from hx_fake import FakeWorld",
                     "importlib.import_module('hx_fake')", "__import__('hx_fake')", "import_module('hx_fake')"):
            self.assertEqual(len(fake_imports(code, "x.py")), 1, code)
        # The frozen self-check's own question is allowed
        self.assertEqual(fake_imports("importlib.util.find_spec('hx_fake')", "x.py"), [])

    def test_spec_excludes(self):
        with open(os.path.join(HERE, "pyinstaller.spec"), encoding="utf-8") as f:
            tree = ast.parse(f.read(), "pyinstaller.spec")
        assigned = [node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "EXCLUDES" for t in node.targets)]
        self.assertEqual(len(assigned), 1)
        excludes = ast.literal_eval(assigned[0])
        for name in ("ssl", "_ssl", "_hashlib", "multiprocessing", "hx_fake"):
            self.assertIn(name, excludes)
        self.assertEqual([n for n in excludes if n.startswith("hx_")], ["hx_fake"])


class BundleTests(unittest.TestCase):
    def test_app_runs_without_excluded_modules(self):
        # The exe's exclusions, simulated: a dependency on any of them fails here, on every platform,
        # before PyInstaller runs (deploy.md §2.2)
        code = "\n".join((
            "import sys",
            f"sys.path.insert(0, {HERE!r})",
            f"for name in {BLOCKED!r}:",
            "    sys.modules[name] = None",
            "import hashlib",
            "import stripes, hx_ui, hx_protocol",
            "assert hashlib.md5(b'123321').hexdigest() == 'c8837b23ff8aaa8a2dde915473ce0991'",
            "key = hx_protocol.release_key()",
            "token = hx_protocol.make_token('123321')",
            "assert hx_protocol.unscramble(hx_protocol.scramble(token, key), key) == token",
            "print('ok')",
        ))
        done = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "ok", done.stderr)


class SelfCheckTests(unittest.TestCase):
    """The exe's --self-check, run from source (non-frozen; its Windows rows run on Windows CI)."""
    root = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest(f"Tk can't start: {e}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stripes_deploy_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = os.path.join(self.tmp, "self-check.txt")

    def tearDown(self):
        i18n.set_language("en")

    def written(self):
        with open(self.out, encoding="utf-8") as f:
            return f.read()

    def test_hx_self_check_from_source(self):
        hx_ui.self_check(self.root)

    def test_self_check_writes_ok(self):
        with mock.patch.object(stripes, "VERSION", "dev"):      # a local stripes_version.py doesn't matter
            stripes.self_check(self.out)
        self.assertEqual(self.written(), "ok dev\n")

    def test_self_check_reports_failure(self):
        with mock.patch.object(stripes, "VERSION", "dev"), \
                mock.patch.object(hx_time, "sntp_query", side_effect=TimeServerError("no answer")):
            stripes.self_check(self.out)
        text = self.written()
        self.assertTrue(text.startswith("failed: "), text)
        self.assertIn("hx sntp", text)

    def test_release_needs_key(self):
        with self.assertRaisesRegex(RuntimeError, "STRIPES_HX_KEY"):
            stripes.check_release_key("v2.0.0", False)
        for version, keyed in (("v2.0.0", True), ("dev", False), ("dev-abc1234", False)):
            stripes.check_release_key(version, keyed)
        # And the self-check of a release-shaped build without the key fails
        with mock.patch.object(stripes, "VERSION", "v2.0.0"), mock.patch.object(stripes, "HX_KEYED", False):
            stripes.self_check(self.out)
        self.assertTrue(self.written().startswith("failed: RuntimeError("), self.written())
        self.assertIn("STRIPES_HX_KEY", self.written())

    def test_version_text(self):
        with mock.patch.object(stripes, "VERSION", "dev"), mock.patch.object(stripes, "HX_KEYED", False):
            self.assertEqual(stripes.version_text(), "Version dev (development key)")
            i18n.set_language("es")
            self.assertEqual(stripes.version_text(), "Versión dev (clave de desarrollo)")
            i18n.set_language("en")
        with mock.patch.object(stripes, "VERSION", "dev"), mock.patch.object(stripes, "HX_KEYED", True):
            self.assertEqual(stripes.version_text(), "Version dev")


if __name__ == "__main__":
    unittest.main()
