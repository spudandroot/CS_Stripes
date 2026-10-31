"""Tests for hx_win: the non-Windows fallbacks, the identity overrides and open_for_read.
The Windows calls themselves run in test_hx_share_windows.py and the exe's self-check.
Run: python -m unittest -v"""

import os
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

import hx_win
from hx_model import Identity

OVERRIDES = ("STRIPES_HX_USER", "STRIPES_HX_COMPUTER", "STRIPES_HX_SIDS")
NOT_WINDOWS = "the fallbacks are what runs off Windows"


class Env:
    """Sets (or with None, removes) environment variables, and puts them back afterwards."""

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.values}
        for k, v in self.values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def no_overrides():
    return Env(**{k: None for k in OVERRIDES})


class PlatformTests(unittest.TestCase):
    def test_is_windows(self):
        self.assertEqual(hx_win.is_windows(), sys.platform == "win32")


class IdentityTests(unittest.TestCase):
    def test_overrides_in_an_unfrozen_run(self):
        with Env(STRIPES_HX_USER="EXAMPLE\\jsmith", STRIPES_HX_COMPUTER="FLOOR01",
                 STRIPES_HX_SIDS="S-1-5-21-1-2-3-2101, S-1-5-21-1-2-3-2102,,"):
            ident = hx_win.current_identity()
        self.assertIsInstance(ident, Identity)
        self.assertEqual(ident.user, "EXAMPLE\\jsmith")
        self.assertEqual(ident.computer, "FLOOR01")
        self.assertEqual(ident.sids, frozenset({"S-1-5-21-1-2-3-2101", "S-1-5-21-1-2-3-2102"}))
        self.assertEqual(ident.pid, os.getpid())
        self.assertEqual(ident.key, f"FLOOR01_EXAMPLE_jsmith_{os.getpid()}")

    def test_each_override_alone(self):
        with no_overrides(), Env(STRIPES_HX_USER="EXAMPLE\\akim"):
            ident = hx_win.current_identity()
        self.assertEqual(ident.user, "EXAMPLE\\akim")
        self.assertTrue(ident.computer)
        with no_overrides(), Env(STRIPES_HX_SIDS=""):
            self.assertEqual(hx_win.current_identity().sids, frozenset())   # set but empty: no groups

    @unittest.skipIf(hx_win.is_windows(), NOT_WINDOWS)
    def test_fallback_off_windows(self):
        with no_overrides():
            ident = hx_win.current_identity()
        self.assertTrue(ident.user.startswith("LOCAL\\"))
        self.assertEqual(ident.user, "LOCAL\\" + hx_win._login_name())
        self.assertEqual(ident.computer, socket.gethostname())
        self.assertEqual(ident.sids, frozenset())
        self.assertEqual(ident.pid, os.getpid())

    @unittest.skipIf(hx_win.is_windows(), NOT_WINDOWS)
    def test_login_name_never_fails(self):
        with no_overrides(), mock.patch("getpass.getuser", side_effect=OSError("no user")):
            self.assertEqual(hx_win.current_identity().user, "LOCAL\\unknown")

    def test_frozen_exe_ignores_the_overrides(self):
        with Env(STRIPES_HX_USER="EXAMPLE\\override", STRIPES_HX_COMPUTER="OVERRIDE",
                 STRIPES_HX_SIDS="S-1-5-21-1-2-3-2101"), mock.patch.object(sys, "frozen", True, create=True):
            ident = hx_win.current_identity()
        self.assertNotEqual(ident.user, "EXAMPLE\\override")
        self.assertNotEqual(ident.computer, "OVERRIDE")
        self.assertNotIn("S-1-5-21-1-2-3-2101", ident.sids)

    @unittest.skipIf(hx_win.is_windows(), NOT_WINDOWS)
    def test_lookup_group_sid_off_windows(self):
        self.assertIsNone(hx_win.lookup_group_sid("Administrators"))
        self.assertIsNone(hx_win.lookup_group_sid("Stripes MapleJet Senders"))

    def test_lookup_group_sid_of_nothing(self):
        self.assertIsNone(hx_win.lookup_group_sid(""))


class OpenForReadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)

    def path(self, name):
        return os.path.join(self.folder, name)

    def write(self, name, data):
        with open(self.path(name), "wb") as f:
            f.write(data)
        return self.path(name)

    def test_reads_bytes(self):
        path = self.write("a.json", b'{"x": 1}\r\n\xff')
        with hx_win.open_for_read(path) as f:
            self.assertEqual(f.read(), b'{"x": 1}\r\n\xff')
        self.assertTrue(f.closed)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            hx_win.open_for_read(self.path("missing.json"))
        with self.assertRaises(FileNotFoundError):
            hx_win.open_for_read(self.path(os.path.join("no-folder", "missing.json")))

    def test_a_folder_isnt_a_file(self):
        with self.assertRaises(OSError):
            hx_win.open_for_read(self.folder)

    def test_swap_while_open(self):
        # A reader keeps the old file to the end of its read. Elsewhere os.replace goes through while it
        # holds the file; Windows' os.replace (MoveFileExW) can refuse a target that is still open, even
        # one that shares delete (storage.md §8.2 item 3), and must work once the reader lets go
        a = self.write("a", b"old")
        f = hx_win.open_for_read(a)
        try:
            b = self.write("b", b"new")
            try:
                os.replace(b, a)
                swapped = True
            except PermissionError:
                self.assertTrue(hx_win.is_windows())
                swapped = False
            self.assertEqual(f.read(), b"old")
        finally:
            f.close()
        if not swapped:
            os.replace(b, a)
        with hx_win.open_for_read(a) as g:
            self.assertEqual(g.read(), b"new")

    def test_path_like(self):
        import pathlib
        path = pathlib.Path(self.write("p", b"x"))
        with hx_win.open_for_read(path) as f:
            self.assertEqual(f.read(), b"x")


class ScreenAndSoundTests(unittest.TestCase):
    def test_monitor(self):
        mon = hx_win.Monitor(0, 0, 0, 1920, 1080, True)
        self.assertEqual((mon.index, mon.width, mon.height, mon.primary), (0, 1920, 1080, True))
        with self.assertRaises(AttributeError):
            mon.width = 5

    def test_work_area(self):
        # The screen less the taskbar (Windows' rcWork); without one, the whole screen (ui.md §10.2)
        self.assertEqual(hx_win.Monitor(1, 1920, 0, 1280, 1024, False).work_area(), (1920, 0, 1280, 1024))
        mon = hx_win.Monitor(0, 0, 0, 1920, 1080, True, (0, 0, 1920, 1040))
        self.assertEqual((mon.work, mon.work_area()), ((0, 0, 1920, 1040), (0, 0, 1920, 1040)))
        for m in hx_win.monitors():            # Windows: each work area lies inside its screen
            x, y, w, h = m.work_area()
            self.assertTrue(m.x <= x and m.y <= y and x + w <= m.x + m.width and y + h <= m.y + m.height, m)
            self.assertTrue(0 < w <= m.width and 0 < h <= m.height, m)

    @unittest.skipIf(hx_win.is_windows(), NOT_WINDOWS)
    def test_monitors_off_windows(self):
        self.assertEqual(hx_win.monitors(), [])

    def test_monitors_returns_a_list(self):
        mons = hx_win.monitors()
        self.assertIsInstance(mons, list)
        if mons:
            self.assertTrue(mons[0].primary)
            self.assertEqual([m.index for m in mons], list(range(len(mons))))

    @unittest.skipIf(hx_win.is_windows(), NOT_WINDOWS)
    def test_sound_is_a_no_op_off_windows(self):
        self.assertIsNone(hx_win.play_alert())
        self.assertIsNone(hx_win.play_alert())       # again while "playing"
        self.assertIsNone(hx_win.play_alert(repeat=False))
        self.assertIsNone(hx_win.stop_alert())
        self.assertIsNone(hx_win.stop_alert())

    def test_alert_path(self):
        here = os.path.dirname(os.path.abspath(hx_win.__file__))
        self.assertEqual(hx_win._alert_path(), os.path.join(here, "assets", "alert.wav"))
        bundle = os.path.join(tempfile.gettempdir(), "_MEI12345")     # where a frozen exe unpacks
        with mock.patch.object(sys, "_MEIPASS", bundle, create=True):
            self.assertEqual(hx_win._alert_path(), os.path.join(bundle, "assets", "alert.wav"))


class SelfCheckTests(unittest.TestCase):
    def test_self_check_passes(self):
        hx_win.self_check()

    def test_self_check_raises_on_failure(self):
        with mock.patch.object(hx_win, "open_for_read", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                hx_win.self_check()
        with mock.patch.object(hx_win, "monitors", return_value=None):
            with self.assertRaises(RuntimeError):
                hx_win.self_check()

    def test_self_check_leaves_no_files(self):
        before = set(os.listdir(tempfile.gettempdir()))
        hx_win.self_check()
        after = set(os.listdir(tempfile.gettempdir()))
        self.assertEqual({n for n in after - before if n.startswith("stripes-hx-")}, set())


if __name__ == "__main__":
    unittest.main()
