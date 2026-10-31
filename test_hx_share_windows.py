"""Windows-only tests of the file behaviour hx_share relies on (storage.md §17.2): a reader holding
a file open during a swap, readers never breaking a writer, open_for_read sharing delete, the
create_exclusive race and add-only folders. They skip elsewhere. CRuntimeErrorTests fakes Windows' errors,
so it runs everywhere. Run on Windows: python -m unittest -v"""

import contextlib
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import hx_share
import hx_win
from hx_core import Msg
from hx_model import TIMINGS, Identity, ShareError, ShareUnavailable
from hx_share import FileOps

PRINTER_ID = "pr_9c8b7a6d5e"

WINDOWS_ONLY = "Windows share behaviour"


def file_ops():
    fs = FileOps()
    fs.tmp_tag = f"wintest-{os.getpid()}"
    return fs


@unittest.skipUnless(sys.platform == "win32", WINDOWS_ONLY)
class SwapTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="hx-share-win-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.target = os.path.join(self.folder, "status.json")
        self.fs = file_ops()

    def read(self):
        with hx_win.open_for_read(self.target) as f:
            return f.read()

    def hold(self, release, longest_s):
        """Holds the target open with open_for_read until release is set or longest_s passes."""
        opened = threading.Event()

        def run():
            with hx_win.open_for_read(self.target) as f:
                opened.set()
                release.wait(longest_s)
                f.read()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.assertTrue(opened.wait(2.0))
        return t

    def test_swap_while_held_briefly(self):
        self.fs.write_whole(self.target, b"old")
        release = threading.Event()
        holder = self.hold(release, 0.3)
        self.fs.write_whole(self.target, b"new")       # with retries if the reader is still there
        release.set()
        holder.join(2.0)
        self.assertEqual(self.read(), b"new")
        self.assertEqual([n for n in os.listdir(self.folder) if n.endswith(".tmp")], [])

    def test_swap_while_held_long(self):
        self.fs.write_whole(self.target, b"old")
        release = threading.Event()
        holder = self.hold(release, 5.0)
        try:
            self.fs.write_whole(self.target, b"new")
            outcome = "replaced while held"
        except PermissionError:
            outcome = "PermissionError after the retries"
        finally:
            release.set()           # the replace has had its chance; no need to wait out the 5 s
            holder.join(6.0)
        print(f"\nwrite_whole onto a file held with FILE_SHARE_DELETE: {outcome}", file=sys.stderr)
        self.assertIn(self.read(), (b"old", b"new"))
        self.assertEqual([n for n in os.listdir(self.folder) if n.endswith(".tmp")], [])

    def test_readers_never_break_a_writer(self):
        self.fs.write_whole(self.target, json.dumps({"n": 0}).encode())
        errors, seen, done = [], set(), threading.Event()

        def write():
            try:
                for n in range(1, 201):
                    self.fs.write_whole(self.target, json.dumps({"n": n, "pad": "x" * 2000}).encode())
            except Exception as e:
                errors.append(e)
            finally:
                done.set()
        writer = threading.Thread(target=write)
        writer.start()
        reads = 0
        while not done.is_set() or reads == 0:
            seen.add(json.loads(self.fs.read_bytes(self.target))["n"])     # always one whole version
            reads += 1
            time.sleep(0.001)       # each read holds its handle for well under a millisecond
        writer.join(30.0)
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(self.read())["n"], 200)
        self.assertTrue(seen <= set(range(201)))

    def test_open_for_read_shares_delete(self):
        self.fs.write_whole(self.target, b"data")
        moved = os.path.join(self.folder, "moved.json")
        with hx_win.open_for_read(self.target) as f:
            os.rename(self.target, moved)
            os.remove(moved)
            self.assertEqual(f.read(), b"data")
        self.assertFalse(os.path.exists(moved))

    def test_exists_opens_by_name(self):
        self.assertFalse(self.fs.exists(self.target))
        self.fs.write_whole(self.target, b"x")
        self.assertTrue(self.fs.exists(self.target))
        self.assertTrue(self.fs.exists(self.folder))           # a folder can't be opened, but it's there


@unittest.skipUnless(sys.platform == "win32", WINDOWS_ONLY)
class CreateExclusiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="hx-share-win-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.fs = file_ops()

    def test_exactly_one_wins(self):
        for round_ in range(100):
            path = os.path.join(self.folder, f"{round_}.lock")
            barrier = threading.Barrier(2)
            results = []

            def create(data):
                barrier.wait()
                results.append(self.fs.create_exclusive(path, data))
            threads = [threading.Thread(target=create, args=(d,)) for d in (b"first", b"second")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5.0)
            self.assertEqual(sorted(results), [False, True], round_)
            with hx_win.open_for_read(path) as f:
                self.assertIn(f.read(), (b"first", b"second"))

    def test_rename_never_replaces(self):
        a, b = os.path.join(self.folder, "a"), os.path.join(self.folder, "b")
        self.fs.write_whole(a, b"a")
        self.fs.write_whole(b, b"b")
        self.assertFalse(self.fs.rename(a, b))
        self.assertTrue(self.fs.rename(a, os.path.join(self.folder, "c")))
        self.assertFalse(self.fs.rename(a, os.path.join(self.folder, "d")))


@unittest.skipUnless(sys.platform == "win32", WINDOWS_ONLY)
class AddOnlyFolderTests(unittest.TestCase):
    """A folder with the log folders' rights: list, create files, read; nothing else (storage.md §14.2)."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="hx-share-win-")
        domain, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
        if not user:
            self.skipTest("no USERNAME")
        self.who = f"{domain}\\{user}" if domain else user
        self.addCleanup(self.clean_up)
        try:
            done = subprocess.run(["icacls", self.folder, "/inheritance:r", "/grant:r", f"{self.who}:(RX,WD,AD)",
                                   f"{self.who}:(OI)(IO)(R)", "*S-1-3-4:(OI)(IO)(R)"],
                                  capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            self.skipTest(f"icacls failed: {e}")
        if done.returncode != 0:
            self.skipTest(f"icacls failed: {done.stdout!r} {done.stderr!r}")
        self.fs = file_ops()

    def clean_up(self):
        # Full control first, or the folder can't be removed
        subprocess.run(["icacls", self.folder, "/grant", f"{self.who}:(OI)(CI)(F)", "/T", "/C"],
                       capture_output=True, timeout=30)
        shutil.rmtree(self.folder, True)

    def test_a_log_file_is_written_once(self):
        stored = hx_share.make_log_record("hx-events", {"type": "x"}, hx_win.current_identity(), 1_793_275_200.0)
        raw = hx_share.encode_log(stored)
        path = os.path.join(self.folder, "20261029T120000000Z.x.x.abcdef.json")
        self.assertTrue(self.fs.create_exclusive(path, raw))
        self.assertEqual(self.fs.read_bytes(path), raw)
        self.assertEqual(hx_share.decode_log(self.fs.read_bytes(path)), stored)

        with self.assertRaises(PermissionError):
            open(path, "r+b").close()
        with self.assertRaises(PermissionError):
            open(path, "ab").close()
        other = os.path.join(self.folder, "other.json")
        self.assertTrue(self.fs.create_exclusive(other, b"other"))
        with self.assertRaises(PermissionError):
            os.replace(other, path)
        with self.assertRaises(PermissionError):
            os.remove(path)
        with self.assertRaises(PermissionError):
            self.fs.rename(path, os.path.join(self.folder, "renamed.json"))
        self.assertEqual(self.fs.read_bytes(path), raw)


class CRuntimeErrorTests(unittest.TestCase):
    """storage.md §9.2. On Windows, create_exclusive's os.open and the temp file's open(..., "xb") go through
    the C runtime, whose OSError has an errno and no winerror: a lost server reads as EINVAL (64, 1231 ...)
    or ENOENT (53, 67), a folder error, so the share wasn't marked down and a Send's lock step went red
    instead of amber "no lock". Windows' errors are faked here, so this runs on every platform."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hx-share-crt-")
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel in ("",) + hx_share._SHARE_FOLDERS:
            os.makedirs(os.path.join(self.root, *rel.split("/")) if rel else self.root, exist_ok=True)

    def store(self):
        identity = Identity(user="EXAMPLE\\jsmith", computer="FLOOR01", sids=frozenset(), pid=4242)
        return hx_share.ShareStore(self.root, identity=identity, fs=file_ops(), timings=TIMINGS.scaled(0.1),
                                   version="test")

    def windows(self, winerror):
        """Patches in Windows' behaviour: the C runtime's opens fail with only an errno (as _dosmaperr maps
        winerror), CreateFileW (open_for_read) with the winerror itself."""
        crt_errno = errno.ENOENT if winerror in (2, 3, 53, 67) else errno.EINVAL

        def crt(*args, **kw):
            raise OSError(crt_errno, os.strerror(crt_errno))

        def create_file(path):
            e = OSError(crt_errno, "Windows error", path)
            e.winerror = winerror           # as CPython builds it on Windows
            raise e
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(hx_share.hx_win, "is_windows", return_value=True))
        stack.enter_context(mock.patch.object(hx_share.hx_win, "open_for_read", side_effect=create_file))
        stack.enter_context(mock.patch.object(hx_share.os, "open", side_effect=crt))
        stack.enter_context(mock.patch.object(hx_share, "open", side_effect=crt, create=True))
        return stack

    def test_a_lost_server_on_the_c_runtime_paths_is_share_unavailable(self):
        writes = [("create_once", lambda st: st.create_once(f"status/locks/{PRINTER_ID}.lock", b"x")),
                  ("write_bytes", lambda st: st.write_bytes("status/lines/ln_1a2b3c4d5e.json", b"x")),
                  ("lock", lambda st: st.lock(PRINTER_ID, purpose="send", run_id="sd_8a7b6c5d4e").acquire())]
        for winerror in (53, 64, 1231):
            for name, write in writes:
                with self.subTest(winerror=winerror, call=name):
                    st = self.store()
                    with self.windows(winerror):
                        if name == "lock":
                            self.assertEqual(write(st), "local_only")       # amber "no lock", not red
                        else:
                            with self.assertRaises(ShareUnavailable):
                                write(st)
                    self.assertFalse(st.available)
                    self.assertEqual(st.down_reason, Msg("Stripes can't reach the server."))

    def test_a_lost_server_during_a_read_is_share_unavailable(self):
        # CreateFileW opened the file, then the server went away: the C runtime's read (or seek) says only
        # EINVAL. Before, that was a folder error (red), and the share wasn't marked down
        rel = "status/lines/ln_1a2b3c4d5e.json"

        class Handle:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *args):
                raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
            seek = tell = read

        calls = [("read_bytes", lambda st: st.read_bytes(rel)), ("size", lambda st: st._size(rel))]
        for winerror, down in ((64, True), (1231, True), (87, False)):
            for name, call in calls:
                with self.subTest(winerror=winerror, call=name):
                    st = self.store()
                    st.write_bytes(rel, b"{}")
                    opens = []

                    def open_for_read(path):
                        opens.append(path)
                        if len(opens) == 1:
                            return Handle()
                        e = OSError(errno.EINVAL, "Windows error", path)
                        e.winerror = winerror       # CreateFileW, asked again, has the winerror
                        raise e
                    with mock.patch.object(hx_share.hx_win, "is_windows", return_value=True), \
                            mock.patch.object(hx_share.hx_win, "open_for_read", side_effect=open_for_read):
                        with self.assertRaises(ShareError) as cm:
                            call(st)
                    self.assertEqual(isinstance(cm.exception, ShareUnavailable), down)
                    self.assertEqual(st.available, not down)

    def test_a_folder_error_on_the_c_runtime_paths_stays_one(self):
        # A missing folder (winerror 3) or a refused parameter (87) is no reason to call the share down
        for winerror in (3, 87):
            with self.subTest(winerror=winerror):
                st = self.store()
                with self.windows(winerror):
                    with self.assertRaises(ShareError) as cm:
                        st.create_once(f"status/locks/{PRINTER_ID}.lock", b"x")
                self.assertNotIsInstance(cm.exception, ShareUnavailable)
                self.assertTrue(st.available)


if __name__ == "__main__":
    unittest.main()
