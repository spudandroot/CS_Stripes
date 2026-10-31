"""Windows-only tests of the file behaviour hx_share relies on (storage.md §17.2): a reader holding
a file open during a swap, readers never breaking a writer, open_for_read sharing delete, the
create_exclusive race and add-only folders. They skip elsewhere. CRuntimeErrorTests fakes Windows' errors,
so it runs everywhere. Run on Windows: python -m unittest -v"""

import contextlib
import errno
import json
import os
import secrets
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


def _printed(*command):
    """What a command printed, with the command, for a failure message; never raises."""
    shown = " ".join(command)
    try:
        done = subprocess.run(command, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return f"> {shown}\n{type(e).__name__}: {e}"
    raw = done.stdout + done.stderr
    try:
        printed = raw.decode("oem")         # the console's code page (a Windows-only codec)
    except (LookupError, UnicodeDecodeError):
        printed = raw.decode("utf-8", "replace")
    return f"> {shown}  (exit code {done.returncode})\n{printed.strip()}"


def inheriting_temp_folder(prefix):
    """A new folder in the temp folder whose rights are inherited the usual way, marked (I); the caller
    removes it. tempfile.mkdtemp's folder won't do on Windows: Python 3.12.4 and later make it (mode 0o700)
    with a protected list of its own, SYSTEM, Administrators and OWNER RIGHTS full control, not marked as
    inheriting automatically, and Windows gives every folder made inside it copies of those entries as
    entries of its own (no (I) in icacls, as the CI runner showed for os.mkdir and os.makedirs).
    icacls /inheritance:r, which removes inherited entries only, leaves them."""
    base = tempfile.gettempdir()
    while True:
        path = os.path.join(base, f"{prefix}{secrets.token_hex(6)}")
        try:
            os.mkdir(path)      # the default mode: no list of its own, only what the temp folder passes down
        except FileExistsError:
            continue
        return path


def icacls_entries(path):
    """The entries icacls lists for path, as it prints them ("NT AUTHORITY\\SYSTEM:(I)(F)")."""
    done = subprocess.run(["icacls", path], capture_output=True, timeout=30)
    if done.returncode != 0:
        raise OSError(f"icacls {path} failed: {done.stdout!r} {done.stderr!r}")
    try:
        text = done.stdout.decode("oem")        # the console's code page (a Windows-only codec)
    except (LookupError, UnicodeDecodeError):
        text = done.stdout.decode("utf-8", "replace")
    entries = []
    for i, line in enumerate(text.splitlines()):
        if i == 0:
            # The first line starts with the path as given, then the first entry
            if not line.startswith(path):
                raise OSError(f"icacls {path} printed something else: {text!r}")
            line = line[len(path):]
        line = line.strip()
        if not line:
            break               # a blank line, then "Successfully processed 1 files; ..."
        entries.append(line)
    return entries


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
    """A folder with the log folders' rights for the account running the test: list, create files,
    read; nothing else (storage.md §14.2). The server's own SYSTEM and Administrators entries are left
    out: they may do anything on the server, and an elevated administrator (a CI runner) would get
    everything through them."""

    def setUp(self):
        domain, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
        if not user:
            self.skipTest("no USERNAME")
        self.who = f"{domain}\\{user}" if domain else user
        # The folder must end with exactly the three entries below. Not inside mkdtemp's folder: every folder
        # made in it gets SYSTEM, Administrators and OWNER RIGHTS full control as entries of its own
        # (inheriting_temp_folder), which icacls keeps, so every new file inherited Administrators' full
        # control and on the CI runner (an elevated administrator) the log file could be opened to change
        # it. Inside a folder that inherits the usual way, every entry the new folder gets is marked
        # inherited, and /inheritance:r takes them all away
        self.parent = inheriting_temp_folder("hx-share-win-")
        self.folder = os.path.join(self.parent, "hx-events")
        os.mkdir(self.folder)
        self.addCleanup(self.clean_up)
        # Exactly these entries, so an elevated administrator gets nothing through its groups. Anything
        # else, and the test couldn't tell an add-only folder from a writable one
        wanted = sorted(e.lower() for e in (f"{self.who}:(RX,WD,AD)", f"{self.who}:(OI)(IO)(R)",
                                            "OWNER RIGHTS:(OI)(IO)(R)"))
        self.set_rights()
        entries = self.entries()
        if sorted(e.lower() for e in entries) != wanted:
            # Should Windows still have copied entries in as the folder's own: every account off, then the
            # three entries again
            for account in sorted({e.partition(":(")[0] for e in entries}):
                self.icacls("/remove:g", account)
            self.set_rights()
            entries = self.entries()
        if sorted(e.lower() for e in entries) != wanted:
            self.fail(f"the test folder's entries aren't exactly {wanted}\n\n"
                      f"{_printed('icacls', self.folder)}\n\n{_printed('whoami', '/user', '/groups')}")
        self.fs = file_ops()

    def icacls(self, *args):
        """icacls on the test folder; the set-up fails if it does."""
        try:
            done = subprocess.run(["icacls", self.folder, *args], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            self.fail(f"the set-up's icacls {' '.join(args)} failed: {e}")
        if done.returncode != 0:
            self.fail(f"the set-up's icacls {' '.join(args)} failed: {done.stdout!r} {done.stderr!r}")

    def set_rights(self):
        # The account's two entries differ only in where they apply: the second is added (/grant), so
        # it can't replace the first (/grant:r)
        self.icacls("/inheritance:r", "/grant:r", f"{self.who}:(RX,WD,AD)", "*S-1-3-4:(OI)(IO)(R)")
        self.icacls("/grant", f"{self.who}:(OI)(IO)(R)")

    def entries(self):
        try:
            return icacls_entries(self.folder)
        except (OSError, subprocess.SubprocessError) as e:
            self.fail(f"the set-up's icacls failed: {e}")

    def clean_up(self):
        # Full control first, or the files can't be removed
        try:
            subprocess.run(["icacls", self.folder, "/grant", f"{self.who}:(OI)(CI)(F)", "/T", "/C"],
                           capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(self.parent, True)

    def evidence(self, path):
        """The folder's and the file's rights and the account's token, for a failure message: why
        Windows allowed it, in the CI log."""
        return "\n\n".join(_printed(*command) for command in (
            ("icacls", self.folder), ("icacls", path), ("whoami", "/user", "/groups", "/priv")))

    @contextlib.contextmanager
    def refused(self, what, path):
        """The block must raise PermissionError."""
        try:
            yield
        except PermissionError:
            return
        self.fail(f"PermissionError not raised: {what} was allowed\n\n{self.evidence(path)}")

    def test_a_log_file_is_written_once(self):
        stored = hx_share.make_log_record("hx-events", {"type": "x"}, hx_win.current_identity(), 1_793_275_200.0)
        raw = hx_share.encode_log(stored)
        path = os.path.join(self.folder, "20261029T120000000Z.x.x.abcdef.json")
        try:
            self.assertTrue(self.fs.create_exclusive(path, raw))
            self.assertEqual(self.fs.read_bytes(path), raw)
        except PermissionError as e:
            self.fail(f"writing the log file once and reading it was refused: {e}\n\n{self.evidence(path)}")
        self.assertEqual(hx_share.decode_log(self.fs.read_bytes(path)), stored)

        with self.refused("opening the log file to change it", path):
            open(path, "r+b").close()
        with self.refused("opening the log file to add to it", path):
            open(path, "ab").close()
        other = os.path.join(self.folder, "other.json")
        self.assertTrue(self.fs.create_exclusive(other, b"other"))
        with self.refused("replacing the log file", path):
            os.replace(other, path)
        with self.refused("deleting the log file", path):
            os.remove(path)
        with self.refused("renaming the log file", path):
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
