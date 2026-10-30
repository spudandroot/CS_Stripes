"""Tests for make_version. Run: python -m unittest -v"""

import ast
import contextlib
import io
import os
import re
import secrets
import tempfile
import unittest

import hx_protocol
import make_version

HERE = os.path.dirname(os.path.abspath(__file__))


def read_key_file(folder):
    """The KEY of a written hx_key.py, run the way Python would import it."""
    scope = {}
    with open(os.path.join(folder, "hx_key.py"), encoding="utf-8") as f:
        exec(f.read(), scope)
    return scope["KEY"]


def run_main(argv, environ, folder):
    """main() with stdout and stderr captured: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = make_version.main(argv, environ, folder)
    return code, out.getvalue(), err.getvalue()


def run_build_bat(folder, environ, version="", fail=()):
    """Runs build.bat line by line, the way cmd.exe would, for the few commands it uses.
    make_version.main is the real one; the tests and PyInstaller only record whether hx_key.py
    was there when they ran ("unittest" or "pyinstaller" in fail makes them fail).
    Returns (exit code, echoed lines, {"unittest": key there?, "pyinstaller": key there?})."""
    with open(os.path.join(HERE, "build.bat"), newline="") as f:
        lines = f.read().split("\r\n")
    labels = {line.strip()[1:].lower(): i for i, line in enumerate(lines) if line.startswith(":")}
    env = dict(environ)
    echoed, saw_key = [], {}
    errorlevel = 0

    def expand(text):
        text = text.replace("%~1", version).replace("%~dp0", folder + os.sep).replace("%cd%", folder)
        return re.sub(r"%(\w+)%", lambda m: env.get(m.group(1), ""), text)

    def path(name):
        return os.path.join(folder, name)

    def run(cmd):
        """Runs one command: True when it worked, ("goto", label) or ("exit", code)."""
        nonlocal errorlevel
        cmd = re.sub(r" *2?>nul", "", cmd).strip()
        if " || " in cmd and not cmd.startswith("if "):
            first, rest = cmd.split(" || ", 1)
            ok = run(first)
            return ok if ok is True else run(rest)
        m = re.fullmatch(r'if "(.*)"=="(.*)" (.+)', cmd)
        if m:
            return run(m.group(3)) if m.group(1) == m.group(2) else True
        m = re.fullmatch(r"if (not )?(exist|defined) (\S+) (.+)", cmd)
        if m:
            there = os.path.exists(path(m.group(3))) if m.group(2) == "exist" else m.group(3) in env
            return run(m.group(4)) if there != bool(m.group(1)) else True
        m = re.fullmatch(r'set "(\w+)=(.*)"', cmd)
        if m:
            if m.group(2):
                env[m.group(1)] = m.group(2)
            else:
                env.pop(m.group(1), None)
            return True
        m = re.fullmatch(r"del /f (\S+)", cmd)
        if m:
            os.remove(path(m.group(1)))
            return True
        if cmd == "echo.":
            echoed.append("")
            return True
        if cmd.startswith("echo "):
            echoed.append(cmd[5:])
            return True
        if cmd.startswith("goto :"):
            return ("goto", cmd[6:].lower())
        m = re.fullmatch(r"exit /b (\d+)", cmd)
        if m:
            return ("exit", int(m.group(1)))
        m = re.fullmatch(r"python make_version\.py (\S+)", cmd)
        if m:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return make_version.main([m.group(1)], env, folder) == 0
        step = ("unittest" if cmd == "python -m unittest"
                else "pyinstaller" if cmd.startswith("pyinstaller ") else None)
        if step:
            saw_key[step] = os.path.exists(path("hx_key.py"))
            return step not in fail
        if cmd.startswith("where "):
            errorlevel = 0
            return True
        if cmd in ("setlocal", "pause") or cmd.startswith(
                ("cd /d ", "py -m venv ", "call .venv\\", "python -m pip install ", "copy /y ")):
            return True
        raise AssertionError(f"run_build_bat doesn't know {cmd!r}: teach it the new command")

    i = 0
    while i < len(lines):
        line = expand(lines[i].strip())
        i += 1
        if not line or line.startswith((":", "rem ", "@echo ")):
            continue
        if line == "if errorlevel 1 (":
            if errorlevel < 1:
                while lines[i].strip() != ")":
                    i += 1
                i += 1
            continue
        if line == ")":
            continue
        result = run(line)
        if result is False:
            continue  # a failed command without || goto: cmd carries on
        if isinstance(result, tuple) and result[0] == "goto":
            i = labels[result[1]] + 1
        elif isinstance(result, tuple):
            return result[1], echoed, saw_key
    raise AssertionError("build.bat ran off its end without exit /b")


class MakeVersionTests(unittest.TestCase):
    def test_version_numbers(self):
        self.assertEqual(make_version.version_numbers("v1.10.2"), (1, 10, 2, 0))
        self.assertEqual(make_version.version_numbers("dev-abc1234"), (0, 0, 0, 0))

    def test_stamp(self):
        with tempfile.TemporaryDirectory() as folder:
            make_version.stamp("v1.2.0", folder)
            scope = {}
            with open(os.path.join(folder, "stripes_version.py"), encoding="utf-8") as f:
                exec(f.read(), scope)
            self.assertEqual(scope["VERSION"], "v1.2.0")
            with open(os.path.join(folder, "version_info.txt"), encoding="utf-8") as f:
                info = f.read()
            self.assertIn("filevers=(1, 2, 0, 0)", info)
            self.assertIn("StringStruct('ProductVersion', 'v1.2.0')", info)


class KeyTests(unittest.TestCase):
    def test_parse_key(self):
        self.assertEqual(make_version.parse_key("00" * 32), bytes(32))
        self.assertEqual(make_version.parse_key("AB" * 32), b"\xab" * 32)
        self.assertEqual(make_version.parse_key("aB" * 32), b"\xab" * 32)
        key = secrets.token_bytes(32)
        self.assertEqual(make_version.parse_key(f"  {key.hex()}\r\n"), key)
        self.assertEqual(make_version.parse_key(f"\t{key.hex().upper()} "), key)

        good = "0123456789abcdef" * 4
        bad = [
            good[:-1],                    # 63 characters
            good + "0",                   # 65
            "zz" * 32,                    # not hex
            "",
            "   ",
            good[:30] + " " + good[31:],  # 64 characters with a space inside (bytes.fromhex takes it)
            good[:-1] + "g",
            "٠" * 64,                # Arabic-Indic zeros: digits, but not hex
            good[:32],                    # 16 bytes
            good * 2,                     # 64 bytes
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as cm:
                    make_version.parse_key(text)
                message = str(cm.exception)
                self.assertTrue(message.startswith("STRIPES_HX_KEY must be"), message)
                if text.strip():
                    self.assertNotIn(text.strip(), message)
                    self.assertNotIn(text.strip()[:8], message)

        with self.assertRaises(ValueError) as cm:
            make_version.parse_key(good[:-1])
        self.assertEqual(str(cm.exception),
                         "STRIPES_HX_KEY must be 64 hex characters (32 bytes), not 63 characters")
        with self.assertRaises(ValueError) as cm:
            make_version.parse_key("zz" * 32)
        self.assertEqual(str(cm.exception), "STRIPES_HX_KEY must be hex")

    def test_dev_key_is_never_accepted(self):
        # The public development key is 29 bytes, so it can't be a release key by accident
        self.assertNotEqual(len(hx_protocol.DEV_KEY), make_version.KEY_BYTES)
        with self.assertRaises(ValueError):
            make_version.parse_key(hx_protocol.DEV_KEY.hex())
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                make_version.stamp_key(hx_protocol.DEV_KEY, folder)
            with self.assertRaises(ValueError):
                make_version.stamp_key(bytes(31), folder)
            with self.assertRaises(ValueError):
                make_version.stamp_key("00" * 32, folder)
            self.assertFalse(os.path.exists(os.path.join(folder, "hx_key.py")))

    def test_stamp_key(self):
        key = secrets.token_bytes(32)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "hx_key.py")
            self.assertEqual(make_version.stamp_key(key, folder), make_version.key_id(key))
            self.assertEqual(read_key_file(folder), key)
            with open(path, "rb") as f:
                raw = f.read()
            # UTF-8 with \n line ends (also on Windows), the docstring, KEY and a final newline
            self.assertNotIn(b"\r", raw)
            self.assertTrue(raw.startswith(b'"""The printer-password key of this build.'), raw[:60])
            self.assertTrue(raw.endswith(b"\n"))
            self.assertIn(f"\nKEY = {key!r}\n".encode("ascii"), raw)
            tree = ast.parse(raw.decode("utf-8"))
            self.assertIn("never commit it", ast.get_docstring(tree))
            self.assertFalse([n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))])

            # A key whose bytes include quotes and backslashes survives repr()
            awkward = b"'\"\\\n\x00" + bytes(range(250, 256)) + bytes(21)
            make_version.stamp_key(awkward, folder)
            self.assertEqual(read_key_file(folder), awkward)

            self.assertEqual(make_version.stamp_key(None, folder), "development")
            self.assertFalse(os.path.exists(path))
            self.assertEqual(make_version.stamp_key(None, folder), "development")  # no file: fine
            self.assertEqual(os.listdir(folder), [])

    def test_key_id_matches_protocol(self):
        for key in (hx_protocol.DEV_KEY, secrets.token_bytes(32), bytes(32)):
            with self.subTest(key=key):
                self.assertEqual(make_version.key_id(key), hx_protocol.key_id(key))
                self.assertRegex(make_version.key_id(key), r"^[0-9a-f]{8}$")

    def test_key_id_names_scrambled_tokens(self):
        key = secrets.token_bytes(32)
        stored = hx_protocol.scramble(hx_protocol.make_token("123321"), key)
        self.assertTrue(stored.startswith(f"hx1:{make_version.key_id(key)}:"))


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name
        self.key_path = os.path.join(self.folder, "hx_key.py")
        self.addCleanup(self._tmp.cleanup)

    def stale_key(self):
        make_version.stamp_key(secrets.token_bytes(32), self.folder)
        self.assertTrue(os.path.exists(self.key_path))

    def test_main(self):
        code, out, err = run_main(["v1.2.0"], {}, self.folder)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.key_path))
        self.assertIn("development key", out)
        self.assertEqual(out, "Stamped v1.2.0 with the development key\n")
        self.assertEqual(err, "")

        key = secrets.token_bytes(32)
        h = key.hex()
        code, out, err = run_main(["v1.2.0"], {"STRIPES_HX_KEY": h}, self.folder)
        self.assertEqual(code, 0)
        self.assertEqual(read_key_file(self.folder), key)
        self.assertEqual(out, f"Stamped v1.2.0 with printer-password key {make_version.key_id(key)}\n")
        for text in (out, err):
            self.assertNotIn(h, text)
            self.assertNotIn(h.upper(), text)
            self.assertNotIn(h[:16], text)
            self.assertNotIn(repr(key), text)

        for bad in ("zz" * 32, h[:-1], h + "0", h[:-1] + "x"):
            with self.subTest(bad=bad):
                self.stale_key()
                code, out, err = run_main(["v1.2.0"], {"STRIPES_HX_KEY": bad}, self.folder)
                self.assertEqual(code, 1)
                self.assertFalse(os.path.exists(self.key_path))  # the one there before is gone too
                self.assertIn("STRIPES_HX_KEY must be", err)
                self.assertNotIn(bad, err)
                self.assertNotIn(bad, out)
                self.assertNotIn(bad[:16], err + out)

        code, out, err = run_main([], {}, self.folder)
        self.assertEqual(code, 1)
        self.assertIn("usage: python make_version.py v1.2.0", err)
        code, out, err = run_main(["v1.2.0", "v1.3.0"], {"STRIPES_HX_KEY": h}, self.folder)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.key_path))

    def test_main_stamps_the_version(self):
        h = secrets.token_hex(32)
        code, _out, _err = run_main(["v2.0.1"], {"STRIPES_HX_KEY": f"  {h.upper()}\n"}, self.folder)
        self.assertEqual(code, 0)
        self.assertEqual(read_key_file(self.folder), bytes.fromhex(h))
        scope = {}
        with open(os.path.join(self.folder, "stripes_version.py"), encoding="utf-8") as f:
            exec(f.read(), scope)
        self.assertEqual(scope["VERSION"], "v2.0.1")
        with open(os.path.join(self.folder, "version_info.txt"), encoding="utf-8") as f:
            self.assertIn("filevers=(2, 0, 1, 0)", f.read())

    def test_main_without_key_removes_a_stale_one(self):
        for environ in ({}, {"STRIPES_HX_KEY": ""}, {"STRIPES_HX_KEY": "  \n"}, {"OTHER": "00" * 32}):
            with self.subTest(environ=environ):
                self.stale_key()
                code, out, _err = run_main(["dev-abc1234"], environ, self.folder)
                self.assertEqual(code, 0)
                self.assertFalse(os.path.exists(self.key_path))
                self.assertEqual(out, "Stamped dev-abc1234 with the development key\n")


class BuildBatTests(unittest.TestCase):
    """build.bat with and without STRIPES_HX_KEY: the key file exists only while PyInstaller runs
    (deploy.md §1.2), so the tests of this build and the next never see it (§3.5 item 3)."""

    WARNING = [
        "Local builds have the development key: printer passwords saved with them don't work in a",
        "release. Only put releases from GitHub on the share.",
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name
        self.key_path = os.path.join(self.folder, "hx_key.py")
        self.addCleanup(self._tmp.cleanup)

    def stale_key(self):
        """An hx_key.py left by an interrupted keyed build."""
        make_version.stamp_key(secrets.token_bytes(32), self.folder)

    def built_version(self):
        scope = {}
        with open(os.path.join(self.folder, "stripes_version.py"), encoding="utf-8") as f:
            exec(f.read(), scope)
        return scope["VERSION"]

    def test_crlf_line_ends(self):
        with open(os.path.join(HERE, "build.bat"), "rb") as f:
            raw = f.read()
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))  # cmd.exe mis-reads labels after bare LFs

    def test_development_build(self):
        self.stale_key()
        code, echoed, saw_key = run_build_bat(self.folder, {})
        self.assertEqual(code, 0)
        self.assertEqual(saw_key, {"unittest": False, "pyinstaller": False})
        self.assertFalse(os.path.exists(self.key_path))
        self.assertEqual(self.built_version(), "dev")
        done = next(i for i, line in enumerate(echoed) if line.startswith("Done: "))
        self.assertEqual(echoed[done + 1:done + 3], self.WARNING)

    def test_keyed_build_leaves_no_key(self):
        environ = {"STRIPES_HX_KEY": secrets.token_hex(32)}
        for stale in (False, True):
            with self.subTest(stale=stale):
                if stale:
                    self.stale_key()
                code, echoed, saw_key = run_build_bat(self.folder, environ, "v1.2.0")
                self.assertEqual(code, 0)
                # The exe gets the key, the tests never see it, and it is gone afterwards
                self.assertEqual(saw_key, {"unittest": False, "pyinstaller": True})
                self.assertFalse(os.path.exists(self.key_path))
                self.assertEqual(self.built_version(), "v1.2.0")
                self.assertNotIn(self.WARNING[0], echoed)  # this build has the release key

        # The next run, from a shell without the key, tests a clean checkout
        code, echoed, saw_key = run_build_bat(self.folder, {})
        self.assertEqual(code, 0)
        self.assertEqual(saw_key, {"unittest": False, "pyinstaller": False})
        self.assertIn(self.WARNING[0], echoed)

    def test_failed_build_leaves_no_key(self):
        environ = {"STRIPES_HX_KEY": secrets.token_hex(32)}
        for fail, ran in (("pyinstaller", {"unittest": False, "pyinstaller": True}),
                          ("unittest", {"unittest": False})):
            with self.subTest(fail=fail):
                self.stale_key()
                code, echoed, saw_key = run_build_bat(self.folder, environ, fail=(fail,))
                self.assertEqual(code, 1)
                self.assertEqual(saw_key, ran)
                self.assertFalse(os.path.exists(self.key_path))
                self.assertIn("Build failed - see the messages above.", echoed)

    def test_bad_key_stops_before_pyinstaller(self):
        self.stale_key()
        code, echoed, saw_key = run_build_bat(self.folder, {"STRIPES_HX_KEY": "zz" * 32})
        self.assertEqual(code, 1)
        self.assertEqual(saw_key, {"unittest": False})
        self.assertFalse(os.path.exists(self.key_path))
        self.assertIn("Build failed - see the messages above.", echoed)


if __name__ == "__main__":
    unittest.main()
