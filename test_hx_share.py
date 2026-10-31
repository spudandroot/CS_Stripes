"""Tests for hx_share: paths, the file envelope, whole-file writes, records and loading, the local
copy and the 24-hour rule, share timeouts, logs and the upload queue, freshness, printer locks,
heartbeats, the poller assignment, reach memory, status files and the fake share's faults.
Logic tests use FakeClock; threaded tests use scaled timings. Run: python -m unittest -v"""

import dataclasses
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import hx_share
from hx_core import EventBus, FakeClock, Msg, SYSTEM_CLOCK
from hx_model import (
    FORMAT, TIMINGS, Ack, Calibration, Company, Country, DataUpdate, ExpectedState, FileRef, Film,
    FormatTooNew, HeartbeatInfo, Identity, Ink, Line, LineStatus, LockHeld, LockLost, Machine,
    MachineFilmAdjustment, NotAllowed, PollStatus, PrintText, PrintTextItem, Printer, Product,
    RevisionConflict, ShareError, ShareHealth, ShareUnavailable, adjustment_id, from_json, new_id, to_json,
)
from hx_share import (
    POOL_MAX, DataService, FileOps, FlakyFileOps, HxPaths, LocalStore, ReachMemory, ShareDenied,
    ShareStore, CounterWatch, assign, decode_json, decode_log, encode_json, encode_log, log_month, log_name,
    make_log_record, record_rel, safe_file_name, score, settle_s, takeover_after_s, write_record,
)

ADMIN_SID = "S-1-5-21-1000000001-1000000002-1000000003-2101"
SENDER_SID = "S-1-5-21-1000000001-1000000002-1000000003-2102"
LINE_ID, MACHINE_ID, PRINTER_ID = "ln_1a2b3c4d5e", "mc_5d1e2f3a4b", "pr_9c8b7a6d5e"
PRODUCT_ID, FILM_ID, PT_ID = "pd_7e6f5a4b3c", "fm_3f9a1c22b0", "pt_0b1c2d3e4f"
OLD_FILM_ID, DEV_PT_ID = "fm_00000000a1", "pt_00000000b2"
MACHINE2_ID, PRINTER2_ID = "mc_00000000c3", "pr_00000000d4"
SHA_FRONT = "0e475da77f3e10d2f36222f0395039ac6ed5abe8b52a0eddbf05bf75e0c42bd8"
SHA_OLD = "a" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"a small fake preview"
T0 = 1_793_275_200.0                 # the FakeClock start: 29 Oct 2026 12:00 UTC
FAST = TIMINGS.scaled(0.01)

# The example log file of storage.md §6.4
EXAMPLE_LOG = (
    b'{"format_version":1,"file_type":"log","log_kind":"hx-events","logged_at":1793275205.123,'
    b'"logged_by":"EXAMPLE\\\\jsmith","logged_on":"FLOOR01","logged_pid":4242,"type":"lock_taken_over",'
    b'"printer_id":"pr_9c8b7a6d5e","previous":{"token":"5be0c41d93aa7f20","user":"EXAMPLE\\\\akim",'
    b'"computer":"FLOOR02","purpose":"send","counter":7},"unchanged_s":60.4}\n'
    b'{"end":"9df18f692105394cab4ba02654291492519cdcf0f314fabb26f0ed26b1169ba7"}\n')


def identity(user="jsmith", computer="FLOOR01", *, admin=False, sender=False, pid=4242):
    sids = set()
    if admin:
        sids.add(ADMIN_SID)
    if sender:
        sids.add(SENDER_SID)
    return Identity(user=f"EXAMPLE\\{user}", computer=computer, sids=frozenset(sids), pid=pid)


ADMIN = identity("admin", "OFFICE01", admin=True)


def records():
    """The example records (storage.md §4), plus an inactive film and a print text in development."""
    return [
        Company(id="company", revision=1, updated_by="EXAMPLE\\admin", time_server="time.example.local",
                time_server_fallback="192.0.2.123",
                countries=[Country(name="USA", aliases=["US", "U.S.A."]), Country(name="Canada", aliases=["CAN"])],
                inks=[Ink(name="Example Black Plastic", cartridge="plastic", colour="black")],
                admin_group_sid=ADMIN_SID, sender_group_sid=SENDER_SID),
        Line(id=LINE_ID, revision=2, name="Line 1", code="L1", cartridge="plastic", notify_user="EXAMPLE\\jsmith"),
        Printer(id=PRINTER_ID, revision=1, name="Printer 1", host="192.0.2.10", uid=1109,
                token="SCRAMBLED-TOKEN-EXAMPLE"),
        Machine(id=MACHINE_ID, revision=5, name="Line 1 Bagger 1", code="B1", line_id=LINE_ID,
                printer_id=PRINTER_ID, placement="delay", print_speed=2400),
        Product(id=PRODUCT_ID, revision=1, name="Example Co 2lb Onion", customer="Example Co"),
        Film(id=FILM_ID, revision=4, product_id=PRODUCT_ID, code="EXON2", artwork_revision="Rev C", status="active",
             files=[FileRef(kind="front_photo", name="front.jpg", path=f"films/{FILM_ID}/files/0e475da7_front.jpg",
                            sha256=SHA_FRONT, size=2481337, added_by="EXAMPLE\\admin")],
             length_mm=610.0, position_mm=150.0, delay_ms=480.0, print_text_ids=[PT_ID],
             default_print_text_id=PT_ID),
        Film(id=OLD_FILM_ID, revision=1, product_id=PRODUCT_ID, code="EXON1", status="inactive",
             files=[FileRef(kind="back_photo", name="back.jpg", path=f"films/{OLD_FILM_ID}/files/aaaaaaaa_back.jpg",
                            sha256=SHA_OLD, size=10)]),
        PrintText(id=PT_ID, revision=7, name="Packed on + origin", code="PKO", status="active",
                  items=[PrintTextItem(id="i1", kind="packed_date", before="Packed on ", date_format="MM/DD/YY"),
                         PrintTextItem(id="i2", kind="origin", placement="below")]),
        PrintText(id=DEV_PT_ID, revision=1, name="Draft", code="DRF", status="development"),
        MachineFilmAdjustment(id=adjustment_id(MACHINE_ID, FILM_ID), revision=2, machine_id=MACHINE_ID,
                              film_id=FILM_ID, delay_ms=20.0),
        Calibration(id=MACHINE_ID, revision=1, machine_id=MACHINE_ID),
    ]


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


def wait_until(condition, timeout_s=0.5):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


QUICK = dataclasses.replace(TIMINGS, share_timeout_s=0.1)    # only the real-time share wait is short


def stuck_write(test, view, suffix, *, after_temp):
    """view's next whole-file write to a path ending in suffix hangs like an SMB call on a lost connection
    (before anything reaches the share, or once its temp file is written: after_temp), until the returned
    event go is set; then the OS call carries on. done is set once it has returned or raised."""
    go, done = threading.Event(), threading.Event()
    test.addCleanup(go.set)
    real_write, real_open = view.write_whole, view._do_open_temp
    first, stalled = [], []

    class Stalling:
        """The temp file, written: it hangs at the flush that starts the commit."""
        def __init__(self, f):
            self.f = f

        def flush(self):
            go.wait(5.0)
            self.f.flush()

        def __getattr__(self, name):
            return getattr(self.f, name)

    def open_temp(path):
        handle = real_open(path)
        if first == [path] and not stalled:
            stalled.append(path)
            handle.f = Stalling(handle.f)
        return handle

    def write_whole(path, data):
        if not path.endswith(suffix) or first:
            return real_write(path, data)
        first.append(path)
        try:
            if not after_temp:
                go.wait(5.0)
            return real_write(path, data)
        finally:
            done.set()
    view.write_whole = write_whole
    if after_temp:
        view._do_open_temp = open_temp
    return go, done


class ShareCase(unittest.TestCase):
    """A temp share (with the folder script's folders), a FakeClock and a FlakyFileOps."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hx-share-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "share", "v2")
        for rel in ("",) + hx_share._SHARE_FOLDERS:
            os.makedirs(os.path.join(self.root, *rel.split("/")) if rel else self.root, exist_ok=True)
        self.clock = FakeClock()
        self.fs = FlakyFileOps(clock=self.clock)
        self.addCleanup(setattr, self.fs, "hang", False)

    def store(self, ident=None, *, view=None, timings=TIMINGS, clock=None):
        return ShareStore(self.root, identity=ident or identity(), clock=clock or self.clock,
                          fs=view if view is not None else self.fs.view(), timings=timings, version="test")

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/"))

    def read(self, rel):
        # Like every reader of the share: a handle that never blocks a writer's swap
        with hx_share.hx_win.open_for_read(self.path(rel)) as f:
            return f.read()

    def write(self, rel, data):
        os.makedirs(os.path.dirname(self.path(rel)), exist_ok=True)
        with open(self.path(rel), "wb") as f:
            f.write(data if isinstance(data, bytes) else json.dumps(data).encode("utf-8"))

    def listing(self, rel):
        return sorted(os.listdir(self.path(rel)))

    def seed(self, store=None, recs=None, counter=1):
        store = store or self.store(ADMIN)
        for rec in recs if recs is not None else records():
            write_record(store, rec)
        self.set_counter(store, counter)
        return store

    def set_counter(self, store, counter):
        store.write_json("config/hx/changes.json", {"file_type": "changes", "counter": counter, "written_at": T0,
                                                    "written_by": "EXAMPLE\\admin", "last": None})

    def pc(self, name="pc1", *, share_root="default"):
        base = os.path.join(self.tmp, "pcs", name)
        return HxPaths(share_root=self.root if share_root == "default" else share_root,
                       roaming=os.path.join(base, "roaming"), local=os.path.join(base, "local"),
                       machine=os.path.join(base, "machine"))

    def service(self, ident=ADMIN, *, store=None, pc=None, view=None):
        store = store or self.store(ident, view=view)
        local = LocalStore(pc or self.pc(), clock=self.clock)
        bus = EventBus()
        return DataService(store=store, local=local, bus=bus, identity=ident, clock=self.clock), store, bus

    def poll(self, counter, *, machine_id=MACHINE_ID, **fields):
        return PollStatus(machine_id=machine_id, printer_id=PRINTER_ID, poller="", counter=counter, **fields)


# --------------------------------------------------------------------------
# 1. Paths
# --------------------------------------------------------------------------

class PathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hx-paths-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_v2_share_root(self):
        expected = os.path.normpath("/srv/share/Stripes/v2")
        with Env(STRIPES_V2=None):
            self.assertEqual(os.path.normpath(hx_share.v2_share_root("/srv/share/Stripes/config")), expected)
            self.assertEqual(os.path.normpath(hx_share.v2_share_root("/srv/share/Stripes/config/")), expected)
            if sys.platform != "win32":
                self.assertEqual(hx_share.v2_share_root("/srv/share/Stripes/config"), "/srv/share/Stripes/v2")
            self.assertIsNone(hx_share.v2_share_root(None))
            self.assertIsNone(hx_share.v2_share_root(""))
        with Env(STRIPES_V2="/elsewhere/v2"):
            self.assertEqual(hx_share.v2_share_root("/srv/share/Stripes/config"), "/elsewhere/v2")
            self.assertEqual(hx_share.v2_share_root(None), "/elsewhere/v2")

    def test_pc_folders_follow_the_environment(self):
        with Env(STRIPES_V2_ROAMING="/r", STRIPES_V2_LOCAL="/l", STRIPES_V2_MACHINE="/m"):
            self.assertEqual((hx_share.roaming_dir(), hx_share.local_dir(), hx_share.machine_dir()), ("/r", "/l", "/m"))
        with Env(STRIPES_V2_ROAMING=None, STRIPES_V2_LOCAL=None, STRIPES_V2_MACHINE=None,
                 APPDATA="/a", LOCALAPPDATA="/la", PROGRAMDATA="/pd"):
            self.assertEqual(hx_share.roaming_dir(), os.path.join("/a", "Stripes", "v2"))
            self.assertEqual(hx_share.local_dir(), os.path.join("/la", "Stripes", "v2"))
            self.assertEqual(hx_share.machine_dir(), os.path.join("/pd", "Stripes", "v2"))
        with Env(STRIPES_V2_ROAMING=None, STRIPES_V2_LOCAL=None, STRIPES_V2_MACHINE=None,
                 APPDATA=None, LOCALAPPDATA=None, PROGRAMDATA=None):
            self.assertEqual(hx_share.roaming_dir(), os.path.join(os.path.expanduser("~/.config"), "Stripes", "v2"))
            self.assertEqual(hx_share.local_dir(), os.path.join(os.path.expanduser("~/.cache"), "Stripes", "v2"))
            self.assertEqual(hx_share.machine_dir(), os.path.join(hx_share.local_dir(), "machine"))

    def test_paths(self):
        with Env(STRIPES_V2=None, STRIPES_V2_ROAMING="/r", STRIPES_V2_LOCAL="/l", STRIPES_V2_MACHINE="/m"):
            p = hx_share.paths("/srv/share/Stripes/config")
        self.assertEqual(p, HxPaths(share_root=os.path.join(os.path.dirname(os.path.normpath(
            "/srv/share/Stripes/config")), "v2"), roaming="/r", local="/l", machine="/m"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            p.roaming = "/x"

    def test_copy_once(self):
        old = os.path.join(self.tmp, "old", "settings.json")
        new = os.path.join(self.tmp, "new", "v2", "settings.json")
        self.assertFalse(hx_share.copy_once(old, new))          # nothing to copy
        os.makedirs(os.path.dirname(old))
        with open(old, "wb") as f:
            f.write(b'{"language": "es"}')
        self.assertTrue(hx_share.copy_once(old, new))           # makes the folder too
        with open(new, "rb") as f:
            self.assertEqual(f.read(), b'{"language": "es"}')
        with open(new, "wb") as f:
            f.write(b"changed")
        self.assertFalse(hx_share.copy_once(old, new))          # never overwrites
        with open(new, "rb") as f:
            self.assertEqual(f.read(), b"changed")
        self.assertEqual([n for n in os.listdir(os.path.dirname(new)) if n.endswith(".tmp")], [])


# --------------------------------------------------------------------------
# 2. The envelope
# --------------------------------------------------------------------------

class EnvelopeTests(unittest.TestCase):
    def line(self):
        return Line(id=LINE_ID, revision=2, updated_by="EXAMPLE\\jsmith", updated_at=T0, name="Línea 1",
                    code="L1", cartridge="plastic")

    def test_round_trip(self):
        raw = encode_json("line", to_json(self.line()))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Línea".encode("utf-8"), raw)         # UTF-8, not \u escapes
        d = decode_json(raw, file_type="line", rel="config/hx/lines/x.json")
        self.assertEqual(list(d)[:3], ["format_version", "file_type", "id"])
        self.assertEqual((d["format_version"], d["file_type"]), (FORMAT, "line"))
        self.assertEqual(from_json(Line, d), self.line())
        self.assertEqual(decode_json(raw, file_type=None, rel="x"), d)

    def test_bom_is_accepted(self):
        raw = b"\xef\xbb\xbf" + encode_json("line", to_json(self.line()))
        self.assertEqual(from_json(Line, decode_json(raw, file_type="line", rel="x")), self.line())

    def test_damaged(self):
        for raw in (b'{"file_type": "line"}', b'{"format_version": "1", "file_type": "line"}',
                    b'{"format_version": true, "file_type": "line"}', b'{"format_version": 0, "file_type": "line"}',
                    b'{"format_version": 1.0, "file_type": "line"}', b'[1, 2]', b'{"format_version": 1', b"\xff\xfe",
                    b""):
            with self.assertRaises(ValueError, msg=raw):
                decode_json(raw, file_type="line", rel="x")

    def test_nesting_too_deep_for_json_is_damage(self):
        # json.loads raises RecursionError, not ValueError, past a few thousand levels
        raw = b'{"format_version": 1, "file_type": "line", "a": ' + b"[" * 10_000 + b"]" * 10_000 + b"}"
        with self.assertRaises(ValueError):
            decode_json(raw, file_type="line", rel="x")
        self.assertIsNone(hx_share._lenient_json(raw))

    def test_newer_format(self):
        raw = json.dumps({"format_version": 2, "file_type": "line", "id": LINE_ID}).encode()
        with self.assertRaises(FormatTooNew) as cm:
            decode_json(raw, file_type="line", rel="config/hx/lines/ln_1a2b3c4d5e.json")
        self.assertEqual(cm.exception.found, 2)
        self.assertEqual(cm.exception.path, "config/hx/lines/ln_1a2b3c4d5e.json")
        self.assertEqual(cm.exception.msg.text, "Update Stripes: {file} was saved by a newer version.")
        with self.assertRaises(FormatTooNew):           # newer is checked before the file type
            decode_json(raw, file_type="machine", rel="x")

    def test_wrong_file_type(self):
        with self.assertRaises(ValueError):
            decode_json(encode_json("line", to_json(self.line())), file_type="machine", rel="x")

    def test_unknown_keys_are_ignored(self):
        d = decode_json(encode_json("line", dict(to_json(self.line()), colour="blue")), file_type="line", rel="x")
        self.assertEqual(d["colour"], "blue")
        self.assertEqual(from_json(Line, d), self.line())

    def test_values_json_cant_hold(self):
        with self.assertRaises(TypeError):
            encode_json("local_state", {"picture": b"\x00\x01"})
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                encode_json("local_state", {"x": bad})
        with self.assertRaises(ValueError):
            encode_json("nonsense", {})

    def test_writers_always_write_this_format(self):
        d = json.loads(encode_json("local_state", {"format_version": 9, "file_type": "x", "a": 1}))
        self.assertEqual(d, {"format_version": FORMAT, "file_type": "local_state", "a": 1})

    def test_older_formats_are_upgraded_in_memory(self):
        raw = json.dumps({"format_version": 1, "file_type": "line", "a": 1}).encode()
        with mock.patch.object(hx_share, "FORMAT", 2), \
                mock.patch.dict(hx_share.UPGRADES, {1: lambda ft, d: dict(d, upgraded=ft)}):
            d = decode_json(raw, file_type="line", rel="x")
        self.assertEqual(d["upgraded"], "line")


# --------------------------------------------------------------------------
# 3. write_whole and the other FileOps
# --------------------------------------------------------------------------

class FileOpsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="hx-fileops-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.fs = FlakyFileOps(clock=FakeClock())
        self.fs.tmp_tag = "c6a3264501b9-4242"
        self.fs.replace_retry_s = tuple(d * 0.01 for d in hx_share.REPLACE_RETRY_S)
        self.target = os.path.join(self.folder, "x.json")

    def content(self):
        with open(self.target, "rb") as f:
            return f.read()

    def tmp_files(self):
        return [n for n in os.listdir(self.folder) if n.endswith(".tmp")]

    def test_write_whole_replaces_and_leaves_no_tmp(self):
        self.fs.write_whole(self.target, b"old")
        self.fs.write_whole(self.target, b"new")
        self.assertEqual(self.content(), b"new")
        self.assertEqual(self.tmp_files(), [])

    def test_replace_retries(self):
        self.fs.write_whole(self.target, b"old")
        self.fs.fail_replace = 3
        self.fs.write_whole(self.target, b"new")
        self.assertEqual(self.content(), b"new")
        self.assertEqual(self.fs.fail_replace, 0)

    def test_replace_gives_up(self):
        self.fs.write_whole(self.target, b"old")
        self.fs.fail_replace = 99
        with self.assertRaises(PermissionError):
            self.fs.write_whole(self.target, b"new")
        self.assertEqual(self.content(), b"old")
        self.assertEqual(self.tmp_files(), [])
        self.assertEqual(self.fs.fail_replace, 99 - 1 - len(hx_share.REPLACE_RETRY_S))

    def test_store_scales_the_retries_with_its_timings(self):
        st = ShareStore(self.folder, identity=identity(), fs=FileOps(), timings=TIMINGS.scaled(0.1))
        self.assertEqual(st.fs.replace_retry_s, tuple(d * 0.1 for d in hx_share.REPLACE_RETRY_S))
        self.assertEqual(st.fs.read_retry_s, tuple(d * 0.1 for d in hx_share.READ_RETRY_S))
        self.assertTrue(st.fs.tmp_tag.endswith("-4242"))
        self.assertEqual(st.fs.tmp_tag[:12], hashlib.sha256(b"FLOOR01|EXAMPLE\\jsmith").hexdigest()[:12])

    def test_write_whole_never_creates_folders(self):
        with self.assertRaises(FileNotFoundError):
            self.fs.write_whole(os.path.join(self.folder, "missing", "x.json"), b"x")
        self.assertFalse(os.path.exists(os.path.join(self.folder, "missing")))

    def test_create_exclusive(self):
        self.assertTrue(self.fs.create_exclusive(self.target, b"first"))
        self.assertFalse(self.fs.create_exclusive(self.target, b"second"))
        self.assertEqual(self.content(), b"first")

    def test_break_after_create(self):
        self.fs.break_after_create = 1
        with self.assertRaises(OSError) as cm:
            self.fs.create_exclusive(self.target, b"0123456789")
        self.assertEqual(cm.exception.errno, errno.EIO)
        self.assertEqual(self.content(), b"01234")                 # half the data, left behind
        self.assertFalse(self.fs.create_exclusive(self.target, b"x"))
        self.assertEqual(self.fs.break_after_create, 0)

    def test_rename_never_replaces(self):
        other = os.path.join(self.folder, "y.json")
        self.fs.write_whole(self.target, b"a")
        self.assertTrue(self.fs.rename(self.target, other))
        self.assertFalse(self.fs.rename(self.target, other))       # src gone
        self.fs.write_whole(self.target, b"b")
        self.assertFalse(self.fs.rename(self.target, other))       # dst exists
        self.assertEqual(self.content(), b"b")
        with open(other, "rb") as f:
            self.assertEqual(f.read(), b"a")

    def test_exists_opens_and_never_stats(self):
        self.fs.write_whole(self.target, b"a")
        with mock.patch("os.stat", side_effect=AssertionError("stat")), \
                mock.patch("os.path.exists", side_effect=AssertionError("exists")):
            self.assertTrue(FileOps().exists(self.target))
            self.assertFalse(FileOps().exists(self.target + ".missing"))

    def test_read_bytes_retries_a_sharing_violation(self):
        self.fs.write_whole(self.target, b"data")
        real = hx_share.hx_win.open_for_read
        attempts = []

        def flaky(path):
            attempts.append(path)
            if len(attempts) < 3:
                raise PermissionError(errno.EACCES, "sharing violation")
            return real(path)
        ops = FileOps()
        ops.read_retry_s = (0.0, 0.0, 0.0)
        with mock.patch.object(hx_share.hx_win, "open_for_read", side_effect=flaky):
            self.assertEqual(ops.read_bytes(self.target), b"data")
        self.assertEqual(len(attempts), 3)
        with mock.patch.object(hx_share.hx_win, "open_for_read", side_effect=PermissionError(errno.EACCES, "x")):
            with self.assertRaises(PermissionError):
                ops.read_bytes(self.target)
        with self.assertRaises(FileNotFoundError):
            ops.read_bytes(self.target + ".missing")

    def test_remove_of_a_missing_file(self):
        self.fs.remove(self.target)

    def test_calls_are_recorded(self):
        self.fs.write_whole(self.target, b"a")
        self.fs.read_bytes(self.target)
        self.assertEqual(self.fs.calls, [("write_whole", self.target), ("read_bytes", self.target)])

    def test_hang_blocks_until_cleared(self):
        self.fs.hang = True
        self.addCleanup(setattr, self.fs, "hang", False)
        errors = []

        def run():
            try:
                self.fs.listdir(self.folder)
            except OSError as e:
                errors.append(e)
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.05)
        self.assertTrue(t.is_alive())
        self.fs.hang = False
        t.join(1.0)
        self.assertEqual(errors[0].errno, errno.EHOSTDOWN)

    def test_down(self):
        self.fs.down = True
        with self.assertRaises(OSError) as cm:
            self.fs.read_bytes(self.target)
        self.assertEqual(cm.exception.errno, errno.EHOSTDOWN)


# --------------------------------------------------------------------------
# 4. Records (DataService.save)
# --------------------------------------------------------------------------

class RecordSaveTests(ShareCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.data, self.st, self.bus = self.service(ADMIN)
        self.data.run_once()
        self.assertEqual(self.data.problems, [])

    def admin_log(self, record_id):
        return [e for e in self.st.read_logs("hx-admin") if e.get("record_id") == record_id]

    def counter(self):
        return json.loads(self.read("config/hx/changes.json"))["counter"]

    def test_each_record_type_writes_its_path(self):
        machine = new_id("mc")
        film = new_id("fm")
        fresh = [
            Line(id=new_id("ln"), name="Line 2", code="L2", cartridge="paper"),
            Machine(id=machine, name="Line 2 Bagger 1", code="B1", line_id=LINE_ID, printer_id=PRINTER_ID,
                    placement="encoder"),
            Printer(id=new_id("pr"), name="Printer 2", host="192.0.2.11", uid=1109),
            MachineFilmAdjustment(id=adjustment_id(machine, FILM_ID), machine_id=machine, film_id=FILM_ID),
            Calibration(id=machine, machine_id=machine),
            Product(id=new_id("pd"), name="Example Co 5lb Onion"),
            Film(id=film, product_id=PRODUCT_ID, code="EXON5"),
            PrintText(id=new_id("pt"), name="Lot only", code="LOT"),
        ]
        expected = {
            "line": "config/hx/lines/{}.json", "machine": "config/hx/machines/{}.json",
            "printer": "config/hx/printers/{}.json", "adjustment": "config/hx/adjustments/{}.json",
            "calibration": "config/hx/calibrations/{}.json", "product": "templates/products/{}.json",
            "film": "templates/films/{}/film.json", "print_text": "templates/print-texts/{}.json",
        }
        for rec in fresh:
            saved = self.data.save(rec, expected_revision=None)
            file_type = hx_share.FILE_TYPES[type(rec)]
            rel = expected[file_type].format(rec.id)
            self.assertEqual(record_rel(rec), rel)
            d = decode_json(self.read(rel), file_type=file_type, rel=rel)
            self.assertEqual((d["format_version"], d["file_type"], d["revision"]), (FORMAT, file_type, 1))
            self.assertEqual((d["updated_by"], d["updated_at"]), ("EXAMPLE\\admin", self.clock.time()))
            self.assertEqual(saved, from_json(type(rec), d))
        company = self.data.save(records()[0], expected_revision=1)
        self.assertEqual(company.revision, 2)
        self.assertEqual(decode_json(self.read("config/hx/company.json"), file_type="company", rel="x")["revision"], 2)

    def test_revisions(self):
        product = Product(id=new_id("pd"), name="Example Co 2lb Onion")
        self.assertEqual(self.data.save(product, expected_revision=None).revision, 1)
        self.assertEqual(self.data.save(product, expected_revision=1).revision, 2)
        with self.assertRaises(RevisionConflict) as cm:
            self.data.save(product, expected_revision=1)
        self.assertEqual(cm.exception.current, 2)
        self.assertEqual(cm.exception.msg.values, {"user": "EXAMPLE\\admin"})
        with self.assertRaises(RevisionConflict):
            self.data.save(product, expected_revision=None)        # a new record whose file exists
        with self.assertRaises(RevisionConflict) as cm:
            self.data.save(Product(id=new_id("pd"), name="x"), expected_revision=3)   # missing file
        self.assertEqual((cm.exception.current, cm.exception.msg.values), (0, {"user": "?"}))

    def test_only_administrators(self):
        data, _st, _bus = self.service(identity(sender=True))
        data.run_once()
        with self.assertRaises(NotAllowed) as cm:
            data.save(Product(id=new_id("pd"), name="x"), expected_revision=None)
        self.assertEqual(cm.exception.msg.text, "Only Stripes Administrators can change this.")

    def test_unknown_keys_survive(self):
        rel = record_rel_for_line()
        d = json.loads(self.read(rel))
        d["future_field"] = {"kept": True}
        self.write(rel, d)
        line = records()[1]
        self.data.save(dataclasses.replace(line, name="Line One"), expected_revision=2)
        d = json.loads(self.read(rel))
        self.assertEqual((d["name"], d["future_field"], d["revision"]), ("Line One", {"kept": True}, 3))

    def test_admin_log_and_changes_counter(self):
        before = self.counter()
        printer = records()[2]
        self.data.save(dataclasses.replace(printer, name="Printer One"), expected_revision=1)
        self.assertEqual(self.counter(), before + 1)
        last = json.loads(self.read("config/hx/changes.json"))["last"]
        self.assertEqual(last, {"file_type": "printer", "id": PRINTER_ID, "revision": 2})
        (entry,) = self.admin_log(PRINTER_ID)
        self.assertEqual(entry["type"], "record_saved")
        self.assertEqual((entry["record_type"], entry["revision"], entry["previous_revision"]), ("printer", 2, 1))
        self.assertEqual((entry["record"]["token"], entry["previous"]["token"]), ("(set)", "(set)"))
        self.assertEqual(entry["record"]["name"], "Printer One")
        for name in os.listdir(self.path(f"logs/hx-admin/{log_month(entry['log_name'])}")):
            self.assertNotIn(b"SCRAMBLED-TOKEN-EXAMPLE", self.read(f"logs/hx-admin/2026-10/{name}"))
        self.data.save(Product(id=new_id("pd"), name="x"), expected_revision=None)
        self.assertEqual(self.counter(), before + 2)

    def test_a_failed_changes_bump_is_raised_after_the_save(self):
        other, _st, _bus = self.service(identity("akim", "FLOOR02", sender=True), pc=self.pc("pc2"))
        other.run_once()
        before = self.counter()
        real = self.st.write_json

        def write_json(rel, data):
            if rel == "config/hx/changes.json":
                raise ShareDenied(Msg("Stripes isn't allowed to change {folder} on the server.",
                                      {"folder": "config/hx"}))
            return real(rel, data)

        line = records()[1]
        with mock.patch.object(self.st, "write_json", side_effect=write_json):
            with self.assertRaises(ShareDenied) as cm:
                self.data.save(dataclasses.replace(line, name="Line One"), expected_revision=2)
        self.assertEqual(cm.exception.msg.values, {"folder": "config/hx"})
        # The record and its log entry are written; only the announcement is missing
        self.assertEqual(json.loads(self.read(record_rel_for_line()))["revision"], 3)
        (entry,) = self.admin_log(LINE_ID)
        self.assertEqual(entry["revision"], 3)
        self.assertEqual(self.counter(), before)
        # This PC's own reload loads the saved record although the counter didn't move
        self.data.run_once()
        self.assertEqual(self.data.current.lines[LINE_ID].name, "Line One")
        # Once a later save bumps the counter, the other PCs pick up both
        self.data.save(Product(id=new_id("pd"), name="x"), expected_revision=None)
        self.assertEqual(self.counter(), before + 1)
        self.clock.advance(TIMINGS.data_refresh_s)
        other.run_once()
        self.assertEqual(other.current.lines[LINE_ID].name, "Line One")

    def test_a_failed_changes_bump_still_copies_the_head_distance(self):
        real = self.st.write_json

        def write_json(rel, data):
            if rel == "config/hx/changes.json":
                raise ShareError(Msg("Stripes couldn't use {folder} on the server: {error}",
                                     {"folder": "config/hx", "error": "OSError: disk full"}))
            return real(rel, data)

        cal = dataclasses.replace(records()[-1], head_distance_mm=412.0, head_by="EXAMPLE\\admin")
        with mock.patch.object(self.st, "write_json", side_effect=write_json):
            with self.assertRaises(ShareError):
                self.data.save(cal, expected_revision=1)
        machine = json.loads(self.read(f"config/hx/machines/{MACHINE_ID}.json"))
        self.assertEqual((machine["head_distance_mm"], machine["revision"]), (412.0, 6))

    def test_printer_without_a_token(self):
        printer = Printer(id=new_id("pr"), name="Printer 3", host="192.0.2.12", uid=1109)
        self.data.save(printer, expected_revision=None)
        (entry,) = self.admin_log(printer.id)
        self.assertEqual((entry["record"]["token"], entry["previous"]), ("", None))

    def test_calibration_copies_the_head_distance(self):
        cal = dataclasses.replace(records()[-1], head_distance_mm=412.0, head_by="EXAMPLE\\admin")
        self.data.save(cal, expected_revision=1)
        machine = json.loads(self.read(f"config/hx/machines/{MACHINE_ID}.json"))
        self.assertEqual((machine["head_distance_mm"], machine["revision"]), (412.0, 6))
        self.assertEqual(len(self.admin_log(MACHINE_ID)), 2)        # the calibration and the machine
        self.data.save(dataclasses.replace(cal, spacing_measured_mm=300.5), expected_revision=2)
        self.assertEqual(json.loads(self.read(f"config/hx/machines/{MACHINE_ID}.json"))["revision"], 6)

    def test_ids_must_agree(self):
        film = dataclasses.replace(records()[5], files=[FileRef(kind="artwork", name="a.pdf", path="films/x/a.pdf",
                                                                sha256="b" * 64, size=1)])
        bad = [film,
               dataclasses.replace(film, files=[FileRef(kind="other", name="a", path=f"films/{FILM_ID}/files/",
                                                        sha256="b" * 64, size=1)]),
               MachineFilmAdjustment(id=adjustment_id(MACHINE_ID, OLD_FILM_ID), machine_id=MACHINE_ID, film_id=FILM_ID),
               Calibration(id=MACHINE2_ID, machine_id=MACHINE_ID),
               Company(id="other"),
               Line(id="ln_xyz", name="x", code="x", cartridge="paper")]
        for rec in bad:
            with self.assertRaises(ValueError, msg=rec):
                self.data.save(rec, expected_revision=None)

    def test_too_big(self):
        with self.assertRaises(ValueError):
            self.data.save(Product(id=new_id("pd"), name="x" * 1_000_001), expected_revision=None)

    def test_another_admin_saving_at_the_same_moment(self):
        real = self.st.write_json

        def both(rel, data):
            real(rel, data)
            if rel.startswith("templates/products/"):
                real(rel, dict(data, updated_by="EXAMPLE\\akim"))       # the other save lands last
        product = Product(id=new_id("pd"), name="x")
        with mock.patch.object(self.st, "write_json", side_effect=both):
            with self.assertRaises(RevisionConflict) as cm:
                self.data.save(product, expected_revision=None)
        self.assertEqual((cm.exception.current, cm.exception.msg.values), (1, {"user": "EXAMPLE\\akim"}))
        self.assertEqual(len(self.admin_log(product.id)), 1)          # ours is logged too

    def test_an_edit_repairs_a_damaged_file(self):
        rel = f"templates/products/{PRODUCT_ID}.json"
        self.write(rel, b"{not json")
        saved = self.data.save(records()[4], expected_revision=1)
        self.assertEqual(saved.revision, 2)
        with self.assertRaises(RevisionConflict):
            self.write(rel, b"{not json")
            self.data.save(records()[4], expected_revision=None)

    def test_newer_record_refuses_the_save(self):
        rel = f"templates/products/{PRODUCT_ID}.json"
        self.write(rel, {"format_version": 2, "file_type": "product", "id": PRODUCT_ID})
        with self.assertRaises(FormatTooNew):
            self.data.save(records()[4], expected_revision=1)


def record_rel_for_line():
    return f"config/hx/lines/{LINE_ID}.json"


# --------------------------------------------------------------------------
# 5. Loading
# --------------------------------------------------------------------------

class LoadingTests(ShareCase):
    def setUp(self):
        super().setUp()
        self.admin_store = self.seed()
        self.view = self.fs.view()
        self.data, self.st, self.bus = self.service(identity(sender=True), view=self.view)
        self.updates = []
        self.bus.subscribe(DataUpdate, self.updates.append)

    def reads(self):
        return [(op, os.path.relpath(path, self.root).replace(os.sep, "/")) for op, path in self.view.calls]

    def test_a_seeded_share_loads_completely(self):
        self.data.run_once()
        current = self.data.current
        self.assertEqual((current.source, self.data.problems, self.data.too_new), ("share", [], []))
        self.assertEqual(current.company, records()[0])
        self.assertEqual(set(current.films), {FILM_ID, OLD_FILM_ID})
        self.assertEqual(set(current.print_texts), {PT_ID, DEV_PT_ID})
        self.assertEqual(current.machines[MACHINE_ID], records()[3])
        self.assertEqual(set(current.adjustments), {adjustment_id(MACHINE_ID, FILM_ID)})
        self.assertEqual(set(current.calibrations), {MACHINE_ID})
        self.assertEqual((current.loaded_at, current.reached_at), (T0, T0))
        self.bus.pump()
        self.assertEqual([u.data for u in self.updates], [current])
        self.assertEqual(self.data.can_send(), (True, None))

    def test_a_damaged_film_keeps_the_previous_set(self):
        self.data.run_once()
        before = self.data.current
        local = self.data.local
        copy_before = self.read_local(local)
        rel = f"templates/films/{FILM_ID}/film.json"
        self.write(rel, b'{"format_version": 1, "file_type": "film", "id": "')
        self.set_counter(self.admin_store, 2)
        self.clock.advance(60)
        self.data.run_once()
        self.assertIs(self.data.current, before)
        self.assertEqual(self.data.problems, [Msg("{file} on the server is damaged.", {"file": rel})])
        self.assertEqual(self.read_local(local), copy_before)
        self.bus.pump()
        self.assertEqual(len(self.updates), 2)                      # the load, then the new problem

    def read_local(self, local):
        with open(os.path.join(local.hx_dir, "local-copy.json"), "rb") as f:
            return f.read()

    def test_a_newer_film_blocks_sending(self):
        rel = f"templates/films/{FILM_ID}/film.json"
        self.write(rel, {"format_version": 2, "file_type": "film", "id": FILM_ID})
        self.data.run_once()
        self.assertIsNone(self.data.current)
        self.assertEqual(self.data.too_new, [rel])
        ok, msg = self.data.can_send()
        self.assertFalse(ok)
        self.assertEqual(msg.text, "Update Stripes: {file} was saved by a newer version.")
        self.assertTrue(msg.english().startswith("Update Stripes"))

    def test_odd_names_are_ignored_and_a_wrong_id_reported(self):
        for name in ("desktop.ini", "Thumbs.db", f"{LINE_ID}.json.c6a3264501b9-4242-4be1a9.tmp", "~$x.json",
                     "x.json.tmp"):
            self.write(f"config/hx/lines/{name}", b"junk")
        self.data.run_once()
        self.assertEqual(self.data.problems, [])
        wrong = "ln_00000000ee"
        self.write(f"config/hx/lines/{wrong}.json", encode_json("line", to_json(records()[1])))
        self.set_counter(self.admin_store, 2)
        self.data.run_once()
        self.assertEqual(self.data.problems, [Msg("{file} on the server is damaged.",
                                                  {"file": f"config/hx/lines/{wrong}.json"})])

    def test_an_unchanged_counter_reads_only_changes_json(self):
        self.data.run_once()
        self.view.calls.clear()
        self.clock.advance(60)
        self.data.run_once()
        self.assertEqual(self.reads(), [("read_bytes", "config/hx/changes.json")])
        self.assertEqual(self.data.current.reached_at, T0 + 60)

    def test_a_changed_counter_reloads_twice(self):
        self.data.run_once()
        self.set_counter(self.admin_store, 2)
        for full in (True, True, False):
            self.view.calls.clear()
            self.data.run_once()
            listed = any(op == "listdir" for op, _ in self.reads())
            self.assertEqual(listed, full)

    def test_a_forced_full_reload_every_ten_refreshes(self):
        self.data.run_once()
        self.clock.advance(10 * TIMINGS.data_refresh_s)
        self.view.calls.clear()
        self.data.run_once()
        self.assertTrue(any(op == "listdir" for op, _ in self.reads()))

    def test_missing_company(self):
        os.remove(self.path("config/hx/company.json"))
        self.data.run_once()
        self.assertIsNone(self.data.current)
        self.assertEqual(self.data.problems, [Msg("MapleJet isn't set up on the server yet.")])
        self.assertEqual(self.data.can_send(), (False, Msg("MapleJet isn't set up on the server yet.")))

    def test_no_share_configured(self):
        st = ShareStore(None, identity=ADMIN, clock=self.clock, fs=self.fs.view())
        self.assertFalse(st.available)
        self.assertEqual(st.down_reason, Msg("No server folder is set up for Stripes on this PC."))
        with self.assertRaises(ShareUnavailable):
            st.read_bytes("config/hx/changes.json")
        st.probe_now()
        self.assertIsNone(st.full_path("templates/x"))
        data, _st, _bus = self.service(ADMIN, store=st)
        data.run_once()
        self.assertEqual(data.problems, [Msg("No server folder is set up for Stripes on this PC.")])
        ok, msg = data.can_send()
        self.assertFalse(ok)


# --------------------------------------------------------------------------
# 6. The local copy and the 24-hour rule
# --------------------------------------------------------------------------

class LocalCopyTests(ShareCase):
    def setUp(self):
        super().setUp()
        self.admin_store = self.seed()
        self.write(f"templates/films/{FILM_ID}/previews/{SHA_FRONT}.png", PNG)
        self.write(f"templates/films/{OLD_FILM_ID}/previews/{SHA_OLD}.png", PNG)
        self.view = self.fs.view()
        self.data, self.st, self.bus = self.service(identity(sender=True), view=self.view)
        self.local = self.data.local

    def stored_copy(self):
        with open(os.path.join(self.local.hx_dir, "local-copy.json"), "rb") as f:
            return json.loads(f.read())

    def test_the_local_copy_holds_the_active_templates(self):
        self.data.run_once()
        d = self.stored_copy()
        self.assertEqual((d["format_version"], d["file_type"]), (FORMAT, "local_copy"))
        self.assertEqual(set(d["films"]), {FILM_ID})
        self.assertEqual(set(d["print_texts"]), {PT_ID})
        self.assertEqual(set(d["machines"]), {MACHINE_ID})
        self.assertEqual((d["share_root"], d["reached_at"], d["changes_counter"]), (self.root, T0, 1))
        self.assertEqual(d["printers"][PRINTER_ID]["token"], "SCRAMBLED-TOKEN-EXAMPLE")   # standalone polling
        self.assertTrue(self.local.has_preview(SHA_FRONT))
        self.assertFalse(self.local.has_preview(SHA_OLD))
        with open(self.local.preview_path(SHA_FRONT), "rb") as f:
            self.assertEqual(f.read(), PNG)
        self.assertEqual(self.data.preview_bytes(FILM_ID, SHA_FRONT), PNG)

        self.clock.advance(5)
        copy_ = self.local.read_local_copy()
        self.assertEqual((copy_.source, copy_.loaded_at, copy_.reached_at), ("local", T0 + 5, T0))
        self.assertEqual(set(copy_.films), {FILM_ID})
        self.assertEqual(copy_.films[FILM_ID], self.data.current.films[FILM_ID])
        self.assertEqual(copy_.company, self.data.current.company)
        other = LocalStore(dataclasses.replace(self.local.paths, share_root=os.path.join(self.tmp, "other", "v2")))
        self.assertIsNone(other.read_local_copy())

    def test_previews_of_films_no_longer_active_are_pruned(self):
        self.data.run_once()
        self.local.write_preview("c" * 64, PNG)
        self.set_counter(self.admin_store, 2)
        self.data.run_once()
        self.assertFalse(self.local.has_preview("c" * 64))
        self.assertTrue(self.local.has_preview(SHA_FRONT))

    def test_damaged_or_newer_local_copy(self):
        self.data.run_once()
        path = os.path.join(self.local.hx_dir, "local-copy.json")
        d = self.stored_copy()
        good = json.loads(json.dumps(d))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(d, format_version=2), f)
        self.assertIsNone(self.local.read_local_copy())
        d["films"][FILM_ID]["length_mm"] = "long"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.assertIsNone(self.local.read_local_copy())
        for bad in (int("9" * 400), float("nan")):              # float() of the int raised OverflowError
            with open(path, "w", encoding="utf-8") as f:
                json.dump(dict(good, reached_at=bad), f)
            self.assertIsNone(self.local.read_local_copy())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(good, reached_at=10 ** 9), f)
        self.assertEqual(self.local.read_local_copy().reached_at, 1e9)     # the good copy still reads

    def test_the_24_hour_rule(self):
        self.data.run_once()
        self.assertEqual(self.data.can_send(), (True, None))
        self.view.down = True
        self.clock.advance(86_399)
        self.data.run_once()
        self.assertFalse(self.st.available)
        self.assertEqual(self.data.can_send(), (True, None))
        self.clock.advance(2)
        ok, msg = self.data.can_send()
        self.assertFalse(ok)
        self.assertEqual(msg.text, "Can't reach the server since {time}.")
        self.assertEqual(msg.values["time"], time.strftime("%Y-%m-%d %H:%M", time.localtime(T0)))
        self.view.down = False
        self.clock.advance(TIMINGS.share_retry_s)
        with self.assertRaises(ShareUnavailable):
            self.st.read_bytes("config/hx/changes.json")            # starts the probe
        self.assertTrue(wait_until(lambda: self.st.available))
        self.data.run_once()
        self.assertEqual(self.data.can_send(), (True, None))
        self.assertEqual(self.data.current.reached_at, T0 + 86_401 + TIMINGS.share_retry_s)

    def test_a_clock_set_back(self):
        self.data.run_once()
        self.clock.set_wall(T0 - 299)
        self.assertEqual(self.data.can_send(), (True, None))
        self.clock.set_wall(T0 - 301)
        ok, msg = self.data.can_send()
        self.assertFalse(ok)
        self.assertEqual(msg.text, "Stripes hasn't been able to read the templates from the server since {time}.")

    def test_the_local_copy_is_rewritten_after_600_s(self):
        self.data.run_once()
        first = self.stored_copy()
        self.clock.advance(599)
        self.data.run_once()
        self.assertEqual(self.stored_copy(), first)
        self.clock.advance(1)
        self.data.run_once()
        self.assertEqual(self.stored_copy()["reached_at"], T0 + 600)

    def test_share_down_moves_current_to_local(self):
        self.data.run_once()
        before = self.data.current
        self.assertEqual(before.source, "share")
        seen = []
        self.bus.subscribe(ShareHealth, seen.append)
        self.data._pass()
        self.view.down = True
        with self.assertRaises(ShareUnavailable):
            self.st.read_bytes("config/hx/changes.json")
        self.data._pass()
        self.bus.pump()
        self.assertEqual([h.ok for h in seen], [True, False])
        # Only the source changes; the content and its times stay
        self.assertEqual(self.data.current, dataclasses.replace(before, source="local"))


class DataThreadTests(ShareCase):
    def test_start_uses_the_local_copy_first_then_the_share(self):
        self.seed()
        pc = self.pc()
        first, _st, _bus = self.service(ADMIN, pc=pc)
        first.run_once()                                            # leaves a local copy

        view = self.fs.view(down=True)
        st = self.store(ADMIN, view=view, timings=FAST, clock=SYSTEM_CLOCK)
        bus = EventBus()
        data = DataService(store=st, local=LocalStore(pc), bus=bus, identity=ADMIN, clock=SYSTEM_CLOCK, timings=FAST)
        updates, health = [], []
        bus.subscribe(DataUpdate, updates.append)
        bus.subscribe(ShareHealth, health.append)
        data.start()
        self.addCleanup(data.stop)
        self.assertTrue(wait_until(lambda: (bus.pump(), updates)[1] and any(not h.ok for h in health)))
        self.assertEqual(updates[0].data.source, "local")
        view.down = False
        st.probe_now()
        self.assertTrue(wait_until(lambda: (bus.pump(), any(u.data.source == "share" for u in updates))[1], 2.0))
        started = time.perf_counter()
        data.stop(timeout_s=1.0)
        self.assertLess(time.perf_counter() - started, 0.5)

    def test_reload_wakes_the_thread(self):
        self.seed()
        st = self.store(ADMIN, timings=TIMINGS, clock=SYSTEM_CLOCK)
        bus = EventBus()
        data = DataService(store=st, local=LocalStore(self.pc()), bus=bus, identity=ADMIN, clock=SYSTEM_CLOCK)
        data.start()
        self.addCleanup(data.stop)
        self.assertTrue(wait_until(lambda: data.current is not None, 2.0))
        loaded = data.current.loaded_at
        time.sleep(0.01)
        data.reload()
        self.assertTrue(wait_until(lambda: data.current.loaded_at != loaded, 2.0))


# --------------------------------------------------------------------------
# 7. Timeouts and share health
# --------------------------------------------------------------------------

class TimeoutTests(ShareCase):
    def test_a_hung_share_times_out_and_is_probed(self):
        self.fs.hang = True
        st = self.store(view=self.fs, timings=FAST)
        started = time.perf_counter()
        with self.assertRaises(ShareUnavailable) as cm:
            st.read_json("config/hx/changes.json")
        self.assertLess(time.perf_counter() - started, FAST.share_timeout_s + 0.1)
        self.assertEqual(cm.exception.msg.text, "The server didn't answer within {seconds} s.")
        self.assertFalse(st.available)
        calls = len(self.fs.calls)
        started = time.perf_counter()
        with self.assertRaises(ShareUnavailable):
            st.read_json("config/hx/changes.json")
        self.assertLess(time.perf_counter() - started, 0.01)
        self.assertEqual(len(self.fs.calls), calls)

        probes = lambda: [c for c in self.fs.calls if c == ("listdir", self.root)]
        self.clock.advance(FAST.share_retry_s)
        with self.assertRaises(ShareUnavailable):
            st.read_json("config/hx/changes.json")
        self.assertTrue(wait_until(lambda: len(probes()) == 1))
        with self.assertRaises(ShareUnavailable):
            st.read_json("config/hx/changes.json")                  # one probe at a time
        time.sleep(0.02)
        self.assertEqual(len(probes()), 1)
        self.fs.hang = False
        self.assertTrue(wait_until(lambda: not st._probing))
        self.assertFalse(st.available)                              # that probe failed
        self.clock.advance(FAST.share_retry_s)
        with self.assertRaises(ShareUnavailable):
            st.read_json("config/hx/changes.json")
        self.assertTrue(wait_until(lambda: st.available))
        self.assertIsNone(st.down_reason)
        self.assertEqual(len(probes()), 2)
        self.assertIsNone(st.read_json("config/hx/changes.json"))
        self.assertEqual(st.last_reached, self.clock.time())

    def test_a_probe_whose_listing_hangs_is_abandoned(self):
        view = self.fs.view(down=True)
        st = self.store(view=view, timings=FAST)
        with self.assertRaises(ShareUnavailable):
            st.read_bytes("config/hx/changes.json")               # marks the share down
        view.down = False
        release = threading.Event()
        self.addCleanup(release.set)
        real_listdir = view.listdir
        hung = []

        def listdir(path):
            if path == self.root and len(hung) < 2:
                hung.append(path)
                release.wait(2.0)                                 # an SMB call that doesn't return
                raise OSError(errno.EHOSTDOWN, "late failure")
            return real_listdir(path)

        with mock.patch.object(view, "listdir", side_effect=listdir):
            self.clock.advance(FAST.share_retry_s)
            with self.assertRaises(ShareUnavailable):
                st.read_bytes("config/hx/changes.json")           # the due probe, which hangs
            self.assertTrue(wait_until(lambda: len(hung) == 1))
            self.assertTrue(wait_until(lambda: not st._probing))  # given up after the share timeout
            self.assertEqual(st.down_reason.text, "The server didn't answer within {seconds} s.")
            st.probe_now()                                        # Retry starts another at once
            self.assertTrue(wait_until(lambda: len(hung) == 2))
            self.assertTrue(wait_until(lambda: not st._probing))
            self.assertFalse(st.available)
            self.clock.advance(FAST.share_retry_s)
            with self.assertRaises(ShareUnavailable):
                st.read_bytes("config/hx/changes.json")           # the next due probe works
            self.assertTrue(wait_until(lambda: st.available))
        self.assertIsNone(st.down_reason)
        # The abandoned listings fail late; that changes nothing
        alive = st._pool.alive
        release.set()
        self.assertTrue(wait_until(lambda: st._pool.alive <= alive - 2))   # both abandoned threads exit
        time.sleep(0.02)
        self.assertTrue(st.available)
        self.assertIsNone(st.read_bytes("config/hx/changes.json"))

    def test_probe_now(self):
        view = self.fs.view(down=True)
        st = self.store(view=view, timings=FAST)
        with self.assertRaises(ShareUnavailable) as cm:
            st.read_bytes("x.json")
        self.assertEqual(cm.exception.msg, Msg("Stripes can't reach the server."))
        view.down = False
        st.probe_now()
        self.assertTrue(wait_until(lambda: st.available))

    def test_a_missing_root_counts_as_down_for_the_probe(self):
        st = ShareStore(os.path.join(self.tmp, "nowhere", "v2"), identity=identity(), clock=self.clock,
                        fs=self.fs.view(), timings=FAST)
        self.assertIsNone(st.read_bytes("config/hx/changes.json"))
        st.probe_now()
        self.assertTrue(wait_until(lambda: not st.available))
        self.assertEqual(st.down_reason.text, "Stripes can't find {folder} on the server.")

    def test_down_fails_at_once(self):
        st = self.store(view=self.fs.view(down=True))
        started = time.perf_counter()
        with self.assertRaises(ShareUnavailable):
            st.read_json("config/hx/changes.json")
        self.assertLess(time.perf_counter() - started, 0.1)
        self.assertFalse(st.available)

    def test_permission_errors_leave_the_share_up(self):
        view = self.fs.view()
        st = self.store(view=view)
        with mock.patch.object(view, "read_bytes", side_effect=PermissionError(errno.EACCES, "denied")):
            with self.assertRaises(ShareDenied) as cm:
                st.read_bytes("status/locks/x.lock")
        self.assertEqual(cm.exception.msg, Msg("Stripes isn't allowed to change {folder} on the server.",
                                               {"folder": "status/locks"}))
        self.assertTrue(st.available)
        with mock.patch.object(view, "write_whole", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(ShareError) as cm:
                st.write_bytes("status/lines/x.json", b"x")
        self.assertEqual(cm.exception.msg.text, "Stripes couldn't use {folder} on the server: {error}")
        self.assertTrue(st.available)
        with self.assertRaises(ShareError) as cm:
            st.write_bytes("status/nowhere/x.json", b"x")
        self.assertEqual(cm.exception.msg, Msg("Stripes can't find {folder} on the server.",
                                               {"folder": "status/nowhere"}))
        self.assertTrue(st.available)

    def test_windows_network_errors_mark_the_share_down(self):
        view = self.fs.view()
        st = self.store(view=view, timings=FAST)
        err = FileNotFoundError(errno.ENOENT, "The network path was not found")
        err.winerror = 53
        with mock.patch.object(view, "read_bytes", side_effect=err):
            with self.assertRaises(ShareUnavailable):
                st.read_bytes("config/hx/changes.json")
        self.assertFalse(st.available)
        with mock.patch.object(view, "listdir", side_effect=err):
            st.probe_now()
            self.assertTrue(wait_until(lambda: not st._probing))
        self.assertEqual(st.down_reason, Msg("Stripes can't reach the server."))

    def test_windows_network_errors_that_python_maps_to_einval(self):
        # 55 ERROR_DEV_NOT_EXIST, 1203 ERROR_NO_NET_OR_BAD_PATH and 1225 ERROR_CONNECTION_REFUSED: CPython
        # gives them errno EINVAL, so they read as a folder error (ShareError) and the share was never
        # marked down: no probe, no standalone checks, no banner, and a Send went red at the lock step
        class DroppedShare(FileOps):
            def __init__(self, code):
                self.code, self.calls = code, 0

            def _fail(self, path, *args):
                self.calls += 1
                e = OSError(errno.EINVAL, "The specified network resource or device is no longer available", path)
                e.winerror = self.code          # as CPython builds it on Windows
                raise e
            read_bytes = create_exclusive = write_whole = listdir = replace = remove = _fail

        for code in (55, 1203, 1225):
            with self.subTest(winerror=code):
                fs = DroppedShare(code)
                st = self.store(view=fs, timings=FAST)
                with self.assertRaises(ShareUnavailable):
                    st.read_lock(PRINTER_ID)
                self.assertFalse(st.available)
                self.assertEqual(st.down_reason, Msg("Stripes can't reach the server."))
                calls = fs.calls
                with self.assertRaises(ShareUnavailable):
                    st.read_lock(PRINTER_ID)                    # at once, without touching the server
                self.assertEqual(fs.calls, calls)
                lk = st.lock(PRINTER_ID, purpose="send", run_id="sd_8a7b6c5d4e", line_id=LINE_ID,
                             machine_id=MACHINE_ID)
                self.assertEqual(lk.acquire(), "local_only")    # the Send carries on (amber no_lock)
                lk.release()
                st.probe_now()
                self.assertTrue(wait_until(lambda: not st._probing))
                self.assertGreater(fs.calls, calls)             # the probe tried, got the same error ...
                self.assertFalse(st.available)                  # ... and the share stays down
        e = OSError(errno.EINVAL, "The parameter is incorrect", "x")
        e.winerror = 87                                         # ERROR_INVALID_PARAMETER: still a folder error
        self.assertFalse(hx_share._network_error(e))

    def test_an_abandoned_write_never_lands(self):
        # §9.1 item 3: a write the caller gave up on (ShareUnavailable) never commits, wherever it hung
        # before the replace. Before, the hx-io thread finished it later, over a newer writer's file
        for after_temp in (False, True):
            with self.subTest(after_temp=after_temp):
                view = self.fs.view()
                a = self.store(view=view, timings=QUICK)
                b = self.store(identity("akim", "FLOOR02"))
                rel = f"status/lines/{LINE_ID}.json"
                a.write_bytes(rel, b"first")
                go, done = stuck_write(self, view, f"{LINE_ID}.json", after_temp=after_temp)
                with self.assertRaises(ShareUnavailable):
                    a.write_bytes(rel, b"from A, given up")
                b.write_bytes(rel, b"from B")
                go.set()
                self.assertTrue(done.wait(5.0))
                self.assertEqual(self.read(rel), b"from B")
                self.assertEqual([n for n in self.listing("status/lines") if n.endswith(".tmp")], [])
        # Called directly, not through the pool, FileOps writes as before
        ops = FileOps()
        ops.write_whole(self.path(rel), b"direct")
        self.assertEqual(self.read(rel), b"direct")

    def test_abandoned_status_writes_never_replace_a_newer_owners(self):
        # poll.json and expected.json: a stuck write of the superseded owner A must not land over B's
        # newer file (a red drift or a failure record shown as A's old green)
        view = self.fs.view()
        a = self.store(identity("jsmith", "FLOOR01"), view=view, timings=QUICK)
        b = self.store(identity("akim", "FLOOR02"))
        a.write_poll(self.poll(100))
        go, done = stuck_write(self, view, f"{MACHINE_ID}.poll.json", after_temp=False)
        with self.assertRaises(ShareUnavailable):
            a.write_poll(self.poll(101))
        for counter in (101, 102):
            b.write_poll(self.poll(counter, problem="drift"))
        go.set()
        self.assertTrue(done.wait(5.0))
        p = self.store(identity("observer", "OFFICE01")).read_poll(MACHINE_ID)
        self.assertEqual((p.counter, p.poller, p.problem), (102, b.identity.key, "drift"))

        view2 = self.fs.view()
        a2 = self.store(identity("jsmith", "FLOOR01"), view=view2, timings=QUICK)
        a2.write_expected(ExpectedState(machine_id=MACHINE_ID, mode="switch_pending", send_id="sd_aaaaaaaaaa"))
        go, done = stuck_write(self, view2, f"{MACHINE_ID}.expected.json", after_temp=True)
        with self.assertRaises(ShareUnavailable):
            a2.write_expected(ExpectedState(machine_id=MACHINE_ID, mode="running", send_id="sd_aaaaaaaaaa"))
        b.write_expected(ExpectedState(machine_id=MACHINE_ID, mode="switch_pending", send_id="sd_bbbbbbbbbb",
                                       result="red"))
        go.set()
        self.assertTrue(done.wait(5.0))
        e = self.store(identity("observer", "OFFICE01")).read_expected(MACHINE_ID)
        self.assertEqual((e.send_id, e.result), ("sd_bbbbbbbbbb", "red"))

    def test_an_abandoned_lock_create_takes_its_file_back(self):
        # §8.1: a create the caller gave up on leaves no file. acquire() said "local_only" (no lock on the
        # share), but before, the hx-io thread finished the create later: a lock no one knew it held, which
        # kept every other PC's Send out and their pollers aside until it went stale
        lock_path = self.path(f"status/locks/{PRINTER_ID}.lock")
        for late in (False, True):
            with self.subTest(late=late):
                view = self.fs.view()
                a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view, timings=QUICK)
                b = self.store(identity("akim", "FLOOR02", sender=True))
                if late:
                    view.undo_create_s = 0.0        # it hung past lock_stale_s / 2: another PC may have it now
                go, stuck = threading.Event(), []
                self.addCleanup(go.set)
                real_fsync = os.fsync

                def fsync(fd):
                    if threading.current_thread().name.startswith("hx-io") and not stuck:
                        stuck.append(threading.current_thread())   # made and written; the flush hangs
                        go.wait(5.0)
                    return real_fsync(fd)
                held = a.lock(PRINTER_ID, purpose="send", run_id="sd_8a7b6c5d4e", line_id=LINE_ID,
                              machine_id=MACHINE_ID)
                with mock.patch.object(os, "fsync", fsync):
                    self.assertEqual(held.acquire(), "local_only")
                    go.set()
                    stuck[0].join(5.0)
                self.assertFalse(stuck[0].is_alive())
                held.release()                      # local_only: the file isn't touched
                self.assertEqual(os.path.exists(lock_path), late)
                if late:
                    # Left to go stale (liveness only), never removed once a takeover may have happened
                    with self.assertRaises(LockHeld):
                        b.lock(PRINTER_ID, purpose="send", run_id="sd_bbbbbbbbbb").acquire()
                    os.remove(lock_path)
                    continue
                taker = b.lock(PRINTER_ID, purpose="send", run_id="sd_bbbbbbbbbb")
                self.assertEqual(taker.acquire(), "held")
                taker.release()
        # Called directly, not through the pool, FileOps creates as before
        self.assertTrue(FileOps().create_exclusive(lock_path, b"direct"))
        self.assertEqual(self.read(f"status/locks/{PRINTER_ID}.lock"), b"direct")

    def test_never_more_than_pool_max_threads(self):
        self.fs.hang = True
        timings = TIMINGS.scaled(0.06)
        st = self.store(view=self.fs, timings=timings)
        results = []

        def call():
            try:
                st.read_bytes("config/hx/changes.json")
                results.append("ok")
            except ShareUnavailable:
                results.append("unavailable")
        threads = [threading.Thread(target=call) for _ in range(POOL_MAX + 8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(2.0)
        self.assertEqual(results, ["unavailable"] * (POOL_MAX + 8))
        self.assertLessEqual(st._pool.peak, POOL_MAX)
        self.fs.hang = False
        self.assertTrue(wait_until(lambda: st._pool.alive == 0))    # abandoned threads exit


# --------------------------------------------------------------------------
# 8. Logs
# --------------------------------------------------------------------------

class LogTests(ShareCase):
    def test_append_log_writes_the_month_folder(self):
        st = self.store()
        name = st.append_log("hx-events", {"type": "lock_taken_over", "printer_id": PRINTER_ID, "unchanged_s": 60.4})
        m = hx_share.LOG_NAME_RE.match(name)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "20261029T120000000Z")
        self.assertEqual((m.group(2), m.group(3)), ("lock_taken_over", PRINTER_ID))
        raw = self.read(f"logs/hx-events/2026-10/{name}")
        stored = decode_log(raw)
        self.assertEqual(stored["log_kind"], "hx-events")
        self.assertEqual((stored["logged_at"], stored["logged_by"], stored["logged_on"], stored["logged_pid"]),
                         (T0, "EXAMPLE\\jsmith", "FLOOR01", 4242))
        self.assertEqual(st.read_log("hx-events", name), dict(stored, log_name=name))

    def test_month_boundaries(self):
        st = self.store()
        names = []
        for at in (1793491199.999, 1793491200.0):
            self.clock.set_wall(at)
            names.append(st.append_log("hx-events", {"type": "x"}))
        self.assertEqual([log_month(n) for n in names], ["2026-10", "2026-11"])
        self.assertTrue(names[0].startswith("20261031T235959999Z.x.x."))
        self.assertTrue(os.path.exists(self.path(f"logs/hx-events/2026-11/{names[1]}")))

    def test_log_names(self):
        self.assertRegex(log_name({"type": "send", "send_id": "sd_8a7b6c5d4e", "film_id": FILM_ID}, T0 + 5.123),
                         r"^20261029T120005123Z\.send\.sd_8a7b6c5d4e\.[0-9a-f]{6}\.json$")
        self.assertIn(".x.", log_name({"type": "x", "send_id": ""}, T0))
        self.assertIn(f".{FILM_ID}.", log_name({"type": "x", "send_id": "", "film_id": FILM_ID}, T0))
        self.assertIn(".a_b_c_d_aaaaaaaaaaaaaaaa.", log_name({"type": "x", "run_id": "a/b c.d " + "a" * 30}, T0))
        for bad in ({"type": "Bad"}, {}, {"type": "x" * 25}, {"type": 3}):
            with self.assertRaises(ValueError):
                log_name(bad, T0)

    def test_completion_marker(self):
        stored = make_log_record("hx-sends", {"type": "send", "send_id": "sd_8a7b6c5d4e", "text": "a\nb ñ"},
                                 identity(), T0)
        raw = encode_log(stored)
        self.assertEqual(raw.count(b"\n"), 2)
        self.assertEqual(decode_log(raw), stored)
        for i in range(len(raw)):
            self.assertIsNone(decode_log(raw[:i]), i)
        for i in range(0, len(raw) - 1, 7):
            changed = bytearray(raw)
            changed[i] = (changed[i] + 1) % 256
            self.assertIsNone(decode_log(bytes(changed)), i)
        newer = dict(stored, format_version=2)
        with self.assertRaises(FormatTooNew):
            decode_log(encode_log(newer))
        self.assertIsNone(decode_log(encode_log(dict(stored, file_type="other"))))
        line1 = b"[" * 10_000 + b"]" * 10_000            # json.loads raises RecursionError on it
        self.assertIsNone(decode_log(line1 + b'\n{"end":"' + hashlib.sha256(line1).hexdigest().encode() + b'"}\n'))

    def test_the_example_file(self):
        stored = decode_log(EXAMPLE_LOG)
        self.assertEqual(stored["previous"]["user"], "EXAMPLE\\akim")
        line1 = EXAMPLE_LOG.split(b"\n")[0]
        self.assertEqual(hashlib.sha256(line1).hexdigest(),
                         "9df18f692105394cab4ba02654291492519cdcf0f314fabb26f0ed26b1169ba7")
        self.assertEqual(encode_log(stored), EXAMPLE_LOG)

    def test_unfinished_entries_are_skipped(self):
        view = self.fs.view()
        st = self.store(view=view)
        view.break_after_create = 1
        with self.assertRaises(ShareError):
            st.append_log("hx-events", {"type": "broken"})
        good = st.append_log("hx-events", {"type": "good"})
        self.assertEqual(len(st.list_logs("hx-events")), 2)
        self.assertEqual([e["log_name"] for e in st.read_logs("hx-events")], [good])

    def test_list_logs_filters_by_name_only(self):
        st = self.store()
        a = st.append_log("hx-sends", {"type": "send", "send_id": "sd_000000000a"})
        self.clock.advance(1)
        b = st.append_log("hx-sends", {"type": "send_switch", "send_id": "sd_000000000a"})
        self.clock.advance(1)
        c = st.append_log("hx-sends", {"type": "send", "send_id": "sd_000000000b"})
        self.write("logs/hx-sends/2026-10/desktop.ini", b"x")
        self.write("logs/hx-sends/2026-10/20261101T000000000Z.send.x.abcdef.json", b"wrong month")
        view = st.fs
        view.calls.clear()
        self.assertEqual(st.list_logs("hx-sends"), [a, b, c])
        self.assertEqual(st.list_logs("hx-sends", entry_type="send"), [a, c])
        self.assertEqual(st.list_logs("hx-sends", ref="sd_000000000a"), [a, b])
        self.assertEqual(st.list_logs("hx-sends", since=T0 + 1), [b, c])
        self.assertEqual(st.list_logs("hx-sends", since=T0 + 1, entry_type="send"), [c])
        self.assertEqual(st.list_logs("hx-sends", since=T0 - 400 * 86_400, entry_type="send", ref="sd_000000000b"), [c])
        self.assertEqual([op for op, _ in view.calls if op != "listdir"], [])

    def test_bad_entries(self):
        st = self.store()
        bad = [{"type": "x", "logged_at": 1}, {"type": "x", "log_name": "y"}, {"type": "x", "format_version": 1},
               {"no": "type"}, {"type": "Has Caps"}, {"type": "x", "picture": b"\x00"},
               {"type": "x", "v": float("nan")},
               {"type": "x", "m": Msg("text")}, {"type": "x", "d": {1: "int key"}},
               {"type": "x", "data_b64": "A" * 8_000_000}]
        for entry in bad:
            with self.assertRaises(ValueError, msg=str(entry)[:60]):
                st.append_log("hx-events", entry)
        with self.assertRaises(ValueError):
            st.append_log("hx-other", {"type": "x"})
        st.append_log("hx-events", {"type": "x", "pairs": (("a", 1),), "m": Msg("text").to_json()})

    def test_the_same_bytes_land_once_and_others_go_to_dash_2(self):
        st = self.store()
        stored = make_log_record("hx-events", {"type": "x"}, identity(), T0)
        name = log_name({"type": "x"}, T0)
        raw = encode_log(stored)
        self.assertEqual(st.put_log_bytes("hx-events", name, raw), name)
        self.assertEqual(st.put_log_bytes("hx-events", name, raw), name)        # uploaded before
        other = encode_log(dict(stored, extra=1))
        self.assertEqual(st.put_log_bytes("hx-events", name, other), name[:-5] + "-2.json")
        self.assertEqual(st.read_log("hx-events", name[:-5] + "-2.json")["extra"], 1)
        self.assertEqual(st.list_logs("hx-events"), sorted([name, name[:-5] + "-2.json"]))

    def test_a_missing_log_folder(self):
        shutil.rmtree(self.path("logs/hx-admin"))
        st = self.store()
        with self.assertRaises(ShareError) as cm:
            st.append_log("hx-admin", {"type": "x"})
        self.assertEqual(cm.exception.msg, Msg("Stripes can't find {folder} on the server.",
                                               {"folder": "logs/hx-admin"}))
        self.assertFalse(os.path.exists(self.path("logs/hx-admin")))    # never made by Stripes


# --------------------------------------------------------------------------
# 9. The upload queue
# --------------------------------------------------------------------------

class QueueTests(ShareCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.sender = identity(sender=True)
        self.view = self.fs.view()
        self.data, self.st, self.bus = self.service(self.sender, view=self.view)
        self.data.run_once()
        self.local = self.data.local

    def go_down(self):
        self.view.down = True
        with self.assertRaises(ShareUnavailable):
            self.st.read_bytes("config/hx/changes.json")

    def come_back(self):
        self.view.down = False
        self.st.probe_now()
        self.assertTrue(wait_until(lambda: self.st.available))

    def entry(self, send_id="sd_8a7b6c5d4e"):
        return {"type": "send", "send_id": send_id, "result": "amber"}

    def test_queued_while_down_then_uploaded(self):
        self.go_down()
        name, queued = self.data.append_send_log(self.entry())
        self.assertTrue(queued)
        self.assertEqual(self.data.pending_logs(), 1)
        self.assertTrue(os.path.exists(os.path.join(self.local.queue_dir, f"hx-sends~{name}")))
        self.come_back()
        self.data.run_once()
        on_share = self.path(f"logs/hx-sends/{log_month(name)}/{name}")
        self.assertEqual(decode_log(self.read(f"logs/hx-sends/{log_month(name)}/{name}"))["send_id"], "sd_8a7b6c5d4e")
        self.assertTrue(os.path.exists(on_share))
        self.assertEqual(self.data.pending_logs(), 0)
        self.assertEqual(os.listdir(self.local.queue_dir), [])          # our own entry and marker are gone

    def test_another_users_entry_keeps_its_marker(self):
        self.go_down()
        name, _ = self.data.append_send_log(self.entry())
        self.come_back()
        entry = os.path.join(self.local.queue_dir, f"hx-sends~{name}")
        real = self.local.fs.remove

        def refuse(path):
            if path == entry:
                raise PermissionError(errno.EACCES, "another user's file")
            real(path)
        with mock.patch.object(self.local.fs, "remove", side_effect=refuse):
            self.data.run_once()
        self.assertTrue(os.path.exists(entry))
        with open(entry + ".uploaded", "rb") as f:
            marker = decode_json(f.read(), file_type="upload_marker", rel="marker")
        self.assertEqual((marker["by"], marker["computer"], marker["at"]), ("EXAMPLE\\jsmith", "FLOOR01", T0))
        self.assertEqual(self.data.pending_logs(), 0)

    def test_a_non_sender_leaves_it_queued(self):
        self.go_down()
        name, _ = self.data.append_send_log(self.entry())
        self.come_back()
        watcher, _st, _bus = self.service(identity("akim", "FLOOR01"), pc=self.local.paths)
        watcher.run_once()
        self.assertEqual(watcher.pending_logs(), 1)
        self.assertFalse(os.path.exists(self.path(f"logs/hx-sends/{log_month(name)}/{name}")))

    def test_events_upload_for_anyone(self):
        self.go_down()
        name, queued = self.data.append_log("hx-events", {"type": "state_changed", "machine_id": MACHINE_ID})
        self.assertTrue(queued)
        self.come_back()
        watcher, _st, _bus = self.service(identity("akim", "FLOOR01"), pc=self.local.paths)
        watcher.run_once()
        self.assertTrue(os.path.exists(self.path(f"logs/hx-events/{log_month(name)}/{name}")))
        self.assertEqual(self.local.queued_logs(), [])

    def test_an_unfinished_file_on_the_share_means_dash_2(self):
        self.go_down()
        name, _ = self.data.append_send_log(self.entry())
        raw = encode_log(self.local.queued_logs()[0][2])
        self.write(f"logs/hx-sends/{log_month(name)}/{name}", raw[:len(raw) // 2])
        self.come_back()
        self.data.run_once()
        dash2 = name[:-5] + "-2.json"
        self.assertEqual(self.read(f"logs/hx-sends/{log_month(name)}/{dash2}"), raw)
        self.assertEqual(self.data.pending_logs(), 0)
        self.assertEqual([e["log_name"] for e in self.st.read_logs("hx-sends")], [dash2])

    def test_a_failed_upload_is_queued(self):
        self.view.break_after_create = 1
        name, queued = self.data.append_send_log(self.entry())
        self.assertTrue(queued)
        self.assertTrue(self.st.available)
        self.data.run_once()
        dash2 = name[:-5] + "-2.json"
        self.assertTrue(os.path.exists(self.path(f"logs/hx-sends/{log_month(name)}/{dash2}")))
        self.assertEqual(self.data.pending_logs(), 0)

    def test_falls_back_to_the_users_own_queue(self):
        self.go_down()
        real = self.local.fs.makedirs

        def refuse(path):
            if path.startswith(self.local.paths.machine):
                raise PermissionError(errno.EACCES, "not this user's")
            real(path)
        with mock.patch.object(self.local.fs, "makedirs", side_effect=refuse):
            name, queued = self.data.append_send_log(self.entry())
        self.assertTrue(queued)
        self.assertTrue(os.path.exists(os.path.join(self.local.fallback_queue_dir, f"hx-sends~{name}")))
        self.assertFalse(os.path.exists(self.local.queue_dir))
        self.assertEqual(self.data.pending_logs(), 1)
        self.come_back()
        self.data.run_once()
        self.assertEqual(self.data.pending_logs(), 0)

    def test_queue_failure(self):
        self.go_down()
        with mock.patch.object(self.local.fs, "create_exclusive", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(ShareError) as cm:
                self.data.append_send_log(self.entry())
        self.assertEqual(cm.exception.msg.text, "Stripes couldn't save the log entry on this PC: {error}")
        self.assertEqual(cm.exception.msg.values["error"], "OSError: disk full")

    def test_queued_logs_oldest_first_and_unfinished_skipped(self):
        self.go_down()
        names = []
        for i in range(3):
            names.append(self.data.append_log("hx-events", {"type": "x", "n": i})[0])
            self.clock.advance(1)
        with open(os.path.join(self.local.queue_dir, "hx-events~20261029T110000000Z.x.x.abcdef.json"), "wb") as f:
            f.write(b'{"half')
        with open(os.path.join(self.local.queue_dir, "notes.txt"), "wb") as f:
            f.write(b"x")
        self.assertEqual([n for _k, n, _s in self.local.queued_logs()], names)
        self.assertEqual([s["n"] for _k, _n, s in self.local.queued_logs()], [0, 1, 2])
        self.assertEqual(self.local.pending_send_logs(), [])
        self.local.queue_send_log(names[0], self.local.queued_logs()[0][2])       # same name, other kind
        self.assertEqual(len(self.local.pending_send_logs()), 0)                  # one entry per log name

    def test_find_send(self):
        on_share, queued = self.data.append_send_log(self.entry("sd_00000000aa"))
        self.assertFalse(queued)
        self.data.append_send_log({"type": "send_switch", "send_id": "sd_00000000bb"})
        self.go_down()
        in_queue, queued = self.data.append_send_log(self.entry("sd_00000000bb"))
        self.assertTrue(queued)
        self.assertEqual(self.data.find_send("sd_00000000bb")["log_name"], in_queue)
        self.come_back()
        self.assertEqual(self.data.find_send("sd_00000000aa")["log_name"], on_share)
        self.assertEqual(self.data.find_send("sd_00000000bb")["type"], "send")
        self.assertIsNone(self.data.find_send("sd_00000000cc"))


# --------------------------------------------------------------------------
# 10. CounterWatch
# --------------------------------------------------------------------------

class CounterWatchTests(unittest.TestCase):
    def test_freshness_by_change(self):
        clock = FakeClock()
        w = CounterWatch(clock)
        self.assertIsNone(w.age("poll:a"))
        self.assertEqual(w.see("poll:a", 5), 0.0)
        clock.advance(4)
        self.assertEqual(w.see("poll:a", 5), 4.0)
        self.assertEqual(w.age("poll:a"), 4.0)
        self.assertEqual(w.see("poll:a", 6), 0.0)
        clock.advance(2)
        self.assertEqual(w.missing("poll:a"), 0.0)
        clock.advance(3)
        self.assertEqual(w.missing("poll:a"), 3.0)
        self.assertEqual(w.age("poll:a"), 3.0)
        clock.advance(3)
        self.assertEqual(w.missing("poll:a"), 6.0)
        self.assertEqual(w.see("poll:a", 6), 0.0)              # seen again: fresh
        self.assertEqual(w.missing("poll:a"), 0.0)
        self.assertEqual(w.missing("poll:never"), 0.0)
        w.forget("poll:a")
        self.assertIsNone(w.age("poll:a"))

    def test_untouched_keys_are_pruned(self):
        clock = FakeClock()
        w = CounterWatch(clock)
        w.see("hb:old", 1)
        clock.advance(3599)
        w.see("hb:new", 1)
        self.assertIsNotNone(w.age("hb:old"))
        clock.advance(1)
        w.see("hb:new", 1)
        self.assertIsNone(w.age("hb:old"))
        self.assertIsNotNone(w.age("hb:new"))


# --------------------------------------------------------------------------
# 11. Printer locks
# --------------------------------------------------------------------------

class LockTests(ShareCase):
    def setUp(self):
        super().setUp()
        self.a = self.store(identity("jsmith", "FLOOR01", sender=True))
        self.b = self.store(identity("akim", "FLOOR02", sender=True))

    def lock_file(self, printer_id=PRINTER_ID):
        return decode_json(self.read(f"status/locks/{printer_id}.lock"), file_type="lock", rel="x")

    def lock(self, store, printer_id=PRINTER_ID, purpose="send"):
        return store.lock(printer_id, purpose=purpose, run_id="sd_8a7b6c5d4e", line_id=LINE_ID, machine_id=MACHINE_ID)

    def watch_until_stale(self, store, seconds, printer_id=PRINTER_ID):
        """The other store reads the lock every 5 s, as the status watcher does."""
        store.read_lock(printer_id)
        for _ in range(int(seconds // 5)):
            self.clock.advance(5)
            store.read_lock(printer_id)
        self.clock.advance(seconds % 5)

    def test_acquire_creates_the_file(self):
        lk = self.lock(self.a)
        self.assertEqual(lk.acquire(), "held")
        d = self.lock_file()
        self.assertEqual((d["format_version"], d["file_type"]), (FORMAT, "lock"))
        self.assertRegex(d["token"], r"^[0-9a-f]{16}$")
        self.assertEqual(lk.token, d["token"])
        self.assertEqual((d["user"], d["computer"], d["purpose"], d["counter"], d["run_id"]),
                         ("EXAMPLE\\jsmith", "FLOOR01", "send", 1, "sd_8a7b6c5d4e"))
        self.assertEqual(self.a.local_locks, {PRINTER_ID: "sd_8a7b6c5d4e"})

    def test_a_live_holder(self):
        self.lock(self.a).acquire()
        with self.assertRaises(LockHeld) as cm:
            self.lock(self.b).acquire()
        self.assertEqual((cm.exception.info.user, cm.exception.info.computer), ("EXAMPLE\\jsmith", "FLOOR01"))
        self.assertEqual(cm.exception.msg, Msg("{user} on {computer} is using this printer.",
                                               {"user": "EXAMPLE\\jsmith", "computer": "FLOOR01"}))
        self.assertEqual(self.b.local_locks, {})

    def test_a_second_acquire_in_one_process(self):
        self.lock(self.a).acquire()
        with self.assertRaises(LockHeld) as cm:
            self.lock(self.a).acquire()
        self.assertEqual(cm.exception.info.user, "EXAMPLE\\jsmith")
        self.assertEqual(len(self.a.local_locks), 1)

    def test_takeover_after_60_s(self):
        held = self.lock(self.a)
        held.acquire()
        old_token = held.token
        self.watch_until_stale(self.b, 59)
        with self.assertRaises(LockHeld):
            self.lock(self.b).acquire()
        self.clock.advance(1)
        taker = self.lock(self.b)
        self.assertEqual(taker.acquire(), "held")
        d = self.lock_file()
        self.assertEqual((d["token"], d["user"]), (taker.token, "EXAMPLE\\akim"))
        self.assertNotEqual(d["token"], old_token)
        self.assertEqual([n for n in self.listing("status/locks") if ".stale-" in n], [])
        (entry,) = [e for e in self.b.read_logs("hx-events") if e["type"] == "lock_taken_over"]
        self.assertEqual(entry["printer_id"], PRINTER_ID)
        self.assertEqual(entry["previous"], {"token": old_token, "user": "EXAMPLE\\jsmith", "computer": "FLOOR01",
                                             "purpose": "send", "run_id": "sd_8a7b6c5d4e", "counter": 1})
        self.assertEqual(entry["unchanged_s"], 60.0)

        # The old holder: lost at its next refresh, and its release leaves the new file alone
        self.assertFalse(held._keep_once())
        self.assertEqual(held.state, "lost")
        with self.assertRaises(LockLost) as cm:
            held.check()
        self.assertEqual(cm.exception.msg.text, "Another PC took over this printer's lock.")
        held.release()
        self.assertEqual(self.lock_file()["token"], taker.token)
        self.assertEqual(self.a.local_locks, {})

    def test_two_takeovers_at_once(self):
        for i in range(5):
            printer = f"pr_00000001{i:02x}"
            self.lock(self.a, printer).acquire()
            c = self.store(identity("bwong", "FLOOR03", sender=True))
            self.b.read_lock(printer)
            c.read_lock(printer)
            self.clock.advance(60)
            results = []
            barrier = threading.Barrier(2)

            def take(store):
                lk = self.lock(store, printer)
                barrier.wait()
                try:
                    results.append(lk.acquire())
                except LockHeld:
                    results.append("refused")
            threads = [threading.Thread(target=take, args=(s,)) for s in (self.b, c)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5.0)
            self.assertEqual(sorted(results), ["held", "refused"], i)
            self.assertEqual([n for n in self.listing("status/locks") if ".stale-" in n], [])

    def test_a_store_that_first_sees_a_stale_lock(self):
        self.lock(self.a).acquire()
        self.clock.advance(600)
        fresh = self.store(identity("bwong", "FLOOR03", sender=True))
        with self.assertRaises(LockHeld):
            self.lock(fresh).acquire()

    def test_release(self):
        lk = self.lock(self.a)
        lk.acquire()
        lk.release()
        self.assertFalse(os.path.exists(self.path(f"status/locks/{PRINTER_ID}.lock")))
        self.assertEqual((lk.state, lk.token, self.a.local_locks), ("unheld", "", {}))
        lk.release()
        self.assertEqual(self.lock(self.b).acquire(), "held")

    def test_share_down_gives_a_local_only_lock(self):
        view = self.fs.view(down=True)
        st = self.store(identity(sender=True), view=view)
        self.assertIsNone(st.local_lock_info(PRINTER_ID))
        lk = self.lock(st)
        self.assertEqual(lk.acquire(), "local_only")
        self.assertEqual((lk.state, lk.token), ("local_only", ""))
        self.assertEqual(st.local_locks, {PRINTER_ID: "sd_8a7b6c5d4e"})
        lk.update(step=3, of=40)
        info = st.local_lock_info(PRINTER_ID)
        self.assertEqual((info.user, info.computer, info.purpose, info.note), ("EXAMPLE\\jsmith", "FLOOR01", "send",
                                                                             {"step": 3, "of": 40}))
        info.note["step"] = 99
        self.assertEqual(st.local_lock_info(PRINTER_ID).note["step"], 3)
        lk.check(budget_s=1000)                                     # nothing to lose
        lk.start_keeping()
        self.assertIsNone(lk._keeper)
        lk.release()
        self.assertIsNone(st.local_lock_info(PRINTER_ID))
        self.assertEqual(st.local_locks, {})
        self.assertIsNone(st.local_lock_info(PRINTER2_ID))

    def test_damaged_and_newer_locks(self):
        self.write(f"status/locks/{PRINTER_ID}.lock", b"\x00garbage")
        info = self.b.read_lock(PRINTER_ID)
        self.assertEqual((info.token[:8], info.purpose, info.user), ("damaged-", "unknown", ""))
        self.assertEqual(self.b.lock_age(PRINTER_ID), 0.0)
        with self.assertRaises(LockHeld):
            self.lock(self.b).acquire()
        self.watch_until_stale(self.b, 60)
        self.assertEqual(self.lock(self.b).acquire(), "held")

        newer = {"format_version": 2, "file_type": "lock", "printer_id": PRINTER2_ID, "token": "f" * 16,
                 "user": "EXAMPLE\\new", "computer": "FLOOR09", "purpose": "send", "counter": 3, "new_key": 1}
        self.write(f"status/locks/{PRINTER2_ID}.lock", newer)
        info = self.a.read_lock(PRINTER2_ID)
        self.assertEqual((info.token, info.user, info.counter), ("f" * 16, "EXAMPLE\\new", 3))
        with self.assertRaises(LockHeld):
            self.lock(self.a, PRINTER2_ID).acquire()

    def test_failing_refreshes(self):
        view = self.fs.view()
        st = self.store(identity(sender=True), view=view)
        lk = self.lock(st)
        lk.acquire()
        view.down = True
        for _ in range(5):
            self.clock.advance(5)
            self.assertTrue(lk._keep_once())
        self.clock.advance(5)
        self.assertFalse(lk._keep_once())                       # 30 s without a good refresh
        self.assertEqual(lk.state, "lost")
        view.down = False
        st.probe_now()
        self.assertTrue(wait_until(lambda: st.available))
        writes = len(view.calls)
        self.assertFalse(lk._keep_once())
        self.assertEqual(view.calls[writes:], [])

    def test_a_stalled_holder_never_revives_its_lock(self):
        # The PC slept with the lock held, and its keeper runs before its work thread on waking
        view = self.fs.view()
        st = self.store(identity(sender=True), view=view)
        lk = self.lock(st)
        lk.acquire()
        self.clock.advance(29)
        self.assertTrue(lk._keep_once())                        # late, but nobody may judge it stale yet
        lk.check()
        self.watch_until_stale(self.b, 65)
        self.assertGreaterEqual(self.b.lock_age(PRINTER_ID), TIMINGS.lock_stale_s)
        writes = len(view.calls)
        self.assertFalse(lk._keep_once())
        self.assertEqual(lk.state, "lost")
        self.assertNotIn("write_whole", [op for op, _ in view.calls[writes:]])
        self.assertEqual(self.lock_file()["counter"], 2)
        with self.assertRaises(LockLost):
            lk.check(budget_s=7.5)
        self.assertFalse(lk._keep_once())                       # and it never writes again
        self.assertNotIn("write_whole", [op for op, _ in view.calls[writes:]])

    def test_a_stalled_keeper_never_overwrites_a_takeover(self):
        held = self.lock(self.a)
        held.acquire()
        self.watch_until_stale(self.b, 65)
        taker = self.lock(self.b)
        rel = self.a._lock_rel(PRINTER_ID)
        read_bytes = self.a.read_bytes

        def read_then_takeover(path):
            raw = read_bytes(path)
            if path == rel and taker.state == "unheld":
                self.assertEqual(taker.acquire(), "held")       # between the keeper's read and its write
            return raw
        with mock.patch.object(self.a, "read_bytes", read_then_takeover):
            self.assertFalse(held._keep_once())
        self.assertEqual(held.state, "lost")
        d = self.lock_file()
        self.assertEqual((d["token"], d["user"], d["counter"]), (taker.token, "EXAMPLE\\akim", 1))
        with self.assertRaises(LockLost):
            held.check()
        taker.check(budget_s=7.5)

    # A PC that freezes (sleep, hibernate, a paused VM) inside a share call. The freeze is the FakeClock
    # moving on while other PCs work, from inside the frozen PC's own FileOps call (storage.md §11.3)

    def frozen_inside(self, view, op, during, *, landed, path=None):
        """The next `op` of view on the lock file (or path) freezes its PC: during() runs before the call
        reaches the share, or after it landed (landed=True). Returns a list that holds True once it ran."""
        path = os.path.normpath(path or self.path(f"status/locks/{PRINTER_ID}.lock"))
        real, fired = getattr(view, op), []

        def frozen(p, *args):
            if os.path.normpath(p) != path or fired:
                return real(p, *args)
            fired.append(True)
            if landed:
                result = real(p, *args)
                during()
                return result
            during()
            return real(p, *args)
        setattr(view, op, frozen)
        return fired

    def passes(self, lk, budget_s=7.5):
        """Would this holder send a command now (send.md's guard: check with the call's worst case)?"""
        try:
            lk.check(budget_s=budget_s)
            return True
        except LockLost:
            return False

    def test_a_refresh_that_froze_proves_nothing(self):
        # last_ok is the time before the write, and a write that came back lock_stale_s / 2 or more after
        # it started makes the lock lost. Before, last_ok was taken after the write: a PC frozen 65 s in
        # it woke with a fresh lease while B, which had seen no change for 60 s, held the printer too
        for landed in (False, True):
            with self.subTest(landed=landed):
                for name in os.listdir(self.path("status/locks")):
                    os.remove(self.path(f"status/locks/{name}"))
                view = self.fs.view()
                a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view)
                b = self.store(identity("akim", "FLOOR02", sender=True))
                held, taker = self.lock(a), self.lock(b)
                held.acquire()
                self.clock.advance(5)
                self.assertTrue(held._keep_once())
                b.read_lock(PRINTER_ID)
                self.clock.advance(5)

                def b_takes_over():
                    self.watch_until_stale(b, 65)
                    self.assertEqual(taker.acquire(), "held")
                fired = self.frozen_inside(view, "write_whole", b_takes_over, landed=landed)
                self.assertFalse(held._keep_once())
                self.assertEqual((fired, held.state), ([True], "lost"))
                self.assertFalse(self.passes(held))
                writes = len(view.calls)
                self.clock.advance(5)
                self.assertFalse(held._keep_once())                 # and it never writes again
                self.assertEqual(view.calls[writes:], [])
                # A write that landed after the takeover put A's dead token over B's file: B stops at its
                # next refresh too (both stop, the named residual of §11.3). One that landed first: B keeps
                self.assertEqual(taker._keep_once(), landed)
                self.assertFalse(self.passes(held) and self.passes(taker))
                held.release()
                taker.release()

    def test_a_refresh_that_froze_with_a_poller_polling_past_it(self):
        # No taker: a poller PC saw the counter unchanged for 60 s and polls past the lock (§11.6 rule 3).
        # The woken holder must not carry on sending with no new handshake
        view = self.fs.view()
        a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view)
        held = self.lock(a)
        held.acquire()
        self.clock.advance(5)
        self.frozen_inside(view, "write_whole", lambda: self.watch_until_stale(self.b, 65), landed=False)
        self.assertFalse(held._keep_once())
        self.assertGreaterEqual(self.b.lock_age(PRINTER_ID), TIMINGS.lock_stale_s)
        self.assertFalse(self.passes(held))

    def test_a_slow_refresh_keeps_its_start_as_last_ok(self):
        view = self.fs.view()
        a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view)
        held = self.lock(a)
        held.acquire()
        self.clock.advance(5)
        started = self.clock.monotonic()
        self.frozen_inside(view, "write_whole", lambda: self.clock.advance(4.9), landed=True)
        self.assertTrue(held._keep_once())
        self.assertEqual(held._last_ok, started)            # not 4.9 s later

    def test_a_pc_frozen_right_after_its_create_landed(self):
        # §11.2 step 4 and §11.4 step 5: last_ok is the time just before the create that made the file,
        # not after it returned, nor after the takeover's remove and log
        view = self.fs.view()
        a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view)
        held, taker = self.lock(a), self.lock(self.b)

        def b_takes_over():
            self.watch_until_stale(self.b, 65)
            self.assertEqual(taker.acquire(), "held")
        self.frozen_inside(view, "create_exclusive", b_takes_over, landed=True)
        self.assertEqual(held.acquire(), "held")
        self.assertEqual(self.lock_file()["token"], taker.token)
        self.assertFalse(self.passes(held))
        self.assertTrue(self.passes(taker))
        held.release()                                          # lost: B's file is left alone
        self.assertEqual(self.lock_file()["token"], taker.token)

    def test_a_pc_frozen_in_its_takeover_log(self):
        c = self.store(identity("bwong", "FLOOR03", sender=True))
        self.lock(self.a).acquire()
        self.watch_until_stale(self.b, 65)
        b_lock, c_lock = self.lock(self.b), self.lock(c)

        def c_takes_over():
            self.watch_until_stale(c, 65)
            self.assertEqual(c_lock.acquire(), "held")
        real_put = self.b.put_log_bytes

        def put_log_bytes(kind, name, raw):
            if c_lock.state == "unheld":
                c_takes_over()                                  # B froze 65 s in its lock_taken_over entry
            return real_put(kind, name, raw)
        with mock.patch.object(self.b, "put_log_bytes", put_log_bytes):
            self.assertEqual(b_lock.acquire(), "held")
        self.assertEqual(self.lock_file()["token"], c_lock.token)
        self.assertFalse(self.passes(b_lock) and self.passes(c_lock))
        self.assertFalse(self.passes(b_lock))

    def test_a_slow_takeover_log_keeps_the_create_time(self):
        self.lock(self.a).acquire()
        self.watch_until_stale(self.b, 65)
        taker = self.lock(self.b)
        created = []
        real_create, real_put = self.b.create_once, self.b.put_log_bytes

        def create_once(rel, data):
            if rel == f"status/locks/{PRINTER_ID}.lock":
                created.append(self.clock.monotonic())
            return real_create(rel, data)

        def put_log_bytes(kind, name, raw):
            self.clock.advance(4.9)                             # a slow share, an awake PC
            return real_put(kind, name, raw)
        with mock.patch.object(self.b, "create_once", create_once), \
                mock.patch.object(self.b, "put_log_bytes", put_log_bytes):
            self.assertEqual(taker.acquire(), "held")
        self.assertEqual(taker._last_ok, created[-1])       # not after the 4.9 s log append

    def test_an_abandoned_refresh_never_replaces_a_takeover(self):
        # A's refresh hangs on the share and is given up (ShareUnavailable); A goes lost by its own lease,
        # B takes over 60 s later, then A's stuck OS call finishes. It must not put A's dead token over
        # B's file: before, B was evicted and the printer looked locked, unpolled, for 60 s more
        for after_temp in (False, True):
            with self.subTest(after_temp=after_temp):
                for name in os.listdir(self.path("status/locks")):
                    os.remove(self.path(f"status/locks/{name}"))
                view = self.fs.view()
                a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view, timings=QUICK)
                b = self.store(identity("akim", "FLOOR02", sender=True))
                held = self.lock(a)
                held.acquire()
                b.read_lock(PRINTER_ID)
                go, done = stuck_write(self, view, f"{PRINTER_ID}.lock", after_temp=after_temp)
                self.clock.advance(5)
                self.assertTrue(held._keep_once())              # given up: under 30 s, still held
                self.watch_until_stale(b, 60)
                self.assertFalse(self.passes(held, 0))
                taker = self.lock(b)
                self.assertEqual(taker.acquire(), "held")
                go.set()
                self.assertTrue(done.wait(5.0))
                self.assertEqual(self.lock_file()["token"], taker.token)
                self.clock.advance(5)
                self.assertTrue(taker._keep_once())
                taker.release()
                held.release()

    def test_lease_end(self):
        view = self.fs.view()
        st = self.store(identity(sender=True), view=view)
        lk = self.lock(st)
        self.assertIsNone(lk.lease_end())                        # unheld
        lk.acquire()
        self.assertEqual(lk.lease_end(), self.clock.monotonic() + TIMINGS.lock_stale_s / 2)
        self.clock.advance(5)
        self.assertTrue(lk._keep_once())
        self.assertEqual(lk.lease_end(), self.clock.monotonic() + TIMINGS.lock_stale_s / 2)
        self.clock.advance(31)
        with self.assertRaises(LockLost):
            lk.check()
        with self.assertRaises(LockLost):
            lk.lease_end()
        local = self.lock(self.store(identity("akim", "FLOOR02", sender=True), view=self.fs.view(down=True)),
                          PRINTER2_ID)
        self.assertEqual(local.acquire(), "local_only")
        self.assertIsNone(local.lease_end())

    def test_check_with_a_budget(self):
        view = self.fs.view()
        st = self.store(identity(sender=True), view=view)
        first, second = self.lock(st), self.lock(st, PRINTER2_ID)
        first.acquire()
        second.acquire()
        view.down = True
        self.clock.advance(21)
        with self.assertRaises(LockLost):
            first.check(budget_s=10)
        self.assertEqual(first.state, "lost")
        second.check()
        self.assertEqual(second.state, "held")

        healthy = self.lock(self.a, "pr_00000000e5")
        healthy.acquire()
        for _ in range(12):
            self.clock.advance(5)
            self.assertTrue(healthy._keep_once())
            healthy.check(budget_s=10)
        self.assertEqual(self.lock_file("pr_00000000e5")["counter"], 13)

    def test_use_from_other_threads(self):
        lk = self.lock(self.a)
        lk.acquire()
        errors = []

        def other():
            try:
                lk.check()
                lk.update(step=1)
                lk.release()
            except Exception as e:
                errors.append(e)
        t = threading.Thread(target=other)
        t.start()
        t.join(2.0)
        third = threading.Thread(target=lk.release)
        third.start()
        third.join(2.0)
        self.assertEqual(errors, [])
        self.assertEqual((lk.state, self.a.local_locks), ("unheld", {}))
        self.assertFalse(os.path.exists(self.path(f"status/locks/{PRINTER_ID}.lock")))

    def test_update_is_written_at_the_next_refresh(self):
        lk = self.lock(self.a, purpose="test")
        lk.acquire()
        lk.update(step=12, of=40)
        self.assertEqual(self.lock_file()["note"], {})
        self.assertEqual(self.a.local_lock_info(PRINTER_ID).note, {"step": 12, "of": 40})
        lk._keep_once()
        d = self.lock_file()
        self.assertEqual((d["note"], d["counter"]), ({"step": 12, "of": 40}, 2))
        with self.assertRaises((TypeError, ValueError)):
            lk.update(picture=b"x")

    def test_a_failed_release_is_retried(self):
        view = self.fs.view()
        st = self.store(identity(sender=True), view=view)
        lk = self.lock(st)
        lk.acquire()
        view.down = True
        lk.release()
        self.assertTrue(os.path.exists(self.path(f"status/locks/{PRINTER_ID}.lock")))
        self.assertEqual(st.local_locks, {})
        view.down = False
        st.probe_now()                                  # the next good operation removes it
        self.assertTrue(wait_until(lambda: not os.path.exists(self.path(f"status/locks/{PRINTER_ID}.lock"))))

    def test_a_pending_release_never_removes_a_new_holders_lock(self):
        # §11.5 step 2: a removal that failed is retried only before last_ok + lock_stale_s / 2. Before, it
        # was retried for 10 minutes by read-then-remove, so a takeover between the read and the remove
        # (the share back just as B took the stale file over) lost B's new lock, and pollers polled with B
        view = self.fs.view()
        a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view)
        held = self.lock(a)
        held.acquire()
        view.down = True
        held.release()                                          # the removal is left pending
        view.down = False
        self.watch_until_stale(self.b, 65)
        taker = self.lock(self.b)
        real_read = a.read_bytes

        def read_then_takeover(rel):
            raw = real_read(rel)
            if rel == a._lock_rel(PRINTER_ID) and taker.state == "unheld":
                self.assertEqual(taker.acquire(), "held")       # between A's read and A's remove
            return raw
        with a._lock:
            a.available = True                                  # A's probe found the share back
        with mock.patch.object(a, "read_bytes", read_then_takeover):
            a._retry_releases()                                 # what _reached() runs then
        if taker.state == "unheld":
            self.assertEqual(taker.acquire(), "held")
        self.assertEqual(self.lock_file()["token"], taker.token)
        self.assertEqual(self.store(identity("bwong", "FLOOR03")).read_lock(PRINTER_ID).token, taker.token)
        self.assertEqual(a._pending_releases, {})
        self.assertTrue(taker._keep_once())

    def test_a_pending_release_ends_with_its_lease(self):
        view = self.fs.view()
        a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view)
        held = self.lock(a)
        held.acquire()
        token = held.token
        self.clock.advance(20)
        view.down = True
        held.release()                                          # pending until last_ok + 30 s
        view.down = False
        self.clock.advance(11)
        with a._lock:
            a.available = True
        calls = len(view.calls)
        a._retry_releases()
        self.assertEqual([op for op, _ in view.calls[calls:]], [])      # dropped, not retried
        self.assertEqual(a._pending_releases, {})
        # Forgotten: the file is an ordinary lock now, here too, taken over once stale (§11.4)
        self.assertEqual(a.read_lock(PRINTER_ID).token, token)
        self.watch_until_stale(a, 60)
        self.assertEqual(self.lock(a).acquire(), "held")

    def test_a_pending_release_never_removes_this_pcs_new_lock(self):
        # §11.5: the retry (run by _reached() on whatever thread just had a good share call) read the
        # released file and was about to remove it, while a new Send on another thread removed that file
        # itself and made its own lock. The retry's remove then deleted the new lock: other PCs saw no lock
        # and polled while the Send sent. Now the retry and acquire() take turns on the printer
        view = self.fs.view()
        a = self.store(identity("jsmith", "FLOOR01", sender=True), view=view)
        first = self.lock(a)
        first.acquire()
        view.down = True
        first.release()                                         # the removal is left pending
        view.down = False
        with a._lock:
            a.available = True                                  # A's probe found the share back
        self.assertTrue(a._pending_releases)
        h_read, s_waits = threading.Event(), threading.Event()
        real_remove, real_mutex = a.remove, getattr(a, "_printer_mutex", None)

        def remove(rel):
            if threading.current_thread().name == "H":
                h_read.set()                                    # H read the old token and removes it now
                s_waits.wait(5.0)
            return real_remove(rel)

        def printer_mutex(printer_id):
            if threading.current_thread().name == "S":
                s_waits.set()                                   # S is about to wait for H to finish
            return real_mutex(printer_id)
        a.remove = remove
        if real_mutex is not None:
            a._printer_mutex = printer_mutex
        second, result = self.lock(a), []

        def send():
            result.append(second.acquire())
            s_waits.set()
        h = threading.Thread(target=a._retry_releases, name="H")
        h.start()
        self.assertTrue(h_read.wait(5.0))
        s = threading.Thread(target=send, name="S")
        s.start()
        h.join(5.0)
        s.join(5.0)
        self.assertEqual(result, ["held"])
        self.assertTrue(os.path.exists(self.path(f"status/locks/{PRINTER_ID}.lock")), "the retry removed the new lock")
        self.assertEqual(self.lock_file()["token"], second.token)
        self.assertEqual(self.b.read_lock(PRINTER_ID).token, second.token)
        self.assertTrue(second._keep_once())
        second.release()
        self.assertFalse(os.path.exists(self.path(f"status/locks/{PRINTER_ID}.lock")))

    def test_a_stalled_release_leaves_a_takers_file_alone(self):
        # The holder's PC froze 65 s: its release must not read-then-remove a file another PC may take
        # over between the two calls. It is lost instead, and leaves the file to go stale
        held = self.lock(self.a)
        held.acquire()
        self.watch_until_stale(self.b, 65)
        taker = self.lock(self.b)
        real_read = self.a.read_bytes

        def read_then_takeover(rel):
            raw = real_read(rel)
            if rel == self.a._lock_rel(PRINTER_ID) and taker.state == "unheld":
                self.assertEqual(taker.acquire(), "held")
            return raw
        with mock.patch.object(self.a, "read_bytes", read_then_takeover):
            held.release()
        if taker.state == "unheld":
            self.assertEqual(taker.acquire(), "held")
        self.assertEqual((held.state, self.a.local_locks), ("unheld", {}))
        self.assertEqual(self.lock_file()["token"], taker.token)
        self.assertTrue(taker._keep_once())

    def test_a_released_token_is_no_lock_here(self):
        lk = self.lock(self.a)
        lk.acquire()
        raw = self.read(f"status/locks/{PRINTER_ID}.lock")
        lk.release()
        self.write(f"status/locks/{PRINTER_ID}.lock", raw)          # as if the removal never happened
        self.assertIsNone(self.a.read_lock(PRINTER_ID))
        self.assertIsNone(self.a.lock_age(PRINTER_ID))
        self.assertIsNotNone(self.b.read_lock(PRINTER_ID))
        self.assertEqual(self.lock(self.a).acquire(), "held")       # it clears its own leftover

    def test_the_keeper_bumps_the_counter(self):
        st = self.store(identity(sender=True), timings=FAST, clock=SYSTEM_CLOCK)
        with self.lock(st) as lk:
            self.assertEqual(lk.state, "held")
            self.assertTrue(wait_until(lambda: self.lock_file()["counter"] >= 3))
        self.assertFalse(os.path.exists(self.path(f"status/locks/{PRINTER_ID}.lock")))
        self.assertIsNone(lk._keeper)

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            self.a.lock(PRINTER_ID, purpose="party", run_id="x")
        with self.assertRaises(ValueError):
            self.a.lock("printer 1", purpose="send", run_id="x")

    def test_check_poller_paused(self):
        poller = self.store(identity("floor", "FLOOR02"))
        poller.write_poll(self.poll(1))
        lk = self.lock(self.a)
        lk.acquire()
        self.assertEqual(self.a.check_poller_paused(MACHINE_ID, lk.token), "polling")
        poller.write_poll(self.poll(2, paused_for_lock=lk.token))
        self.assertEqual(self.a.check_poller_paused(MACHINE_ID, lk.token), "paused")
        self.assertEqual(poller.check_poller_paused(MACHINE_ID, "someone-else"), "paused")    # its own poller
        poller.write_poll(self.poll(3, paused_for_lock="other"))
        self.assertEqual(self.a.check_poller_paused(MACHINE_ID, lk.token), "polling")
        self.clock.advance(14)
        self.assertEqual(self.a.check_poller_paused(MACHINE_ID, lk.token), "polling")
        self.clock.advance(1)
        self.assertEqual(self.a.check_poller_paused(MACHINE_ID, lk.token), "stale")

        self.assertEqual(self.a.check_poller_paused(MACHINE2_ID, lk.token), "polling")
        self.clock.advance(14)
        self.assertEqual(self.a.check_poller_paused(MACHINE2_ID, lk.token), "polling")
        self.clock.advance(1)
        self.assertEqual(self.a.check_poller_paused(MACHINE2_ID, lk.token), "missing")

        newer = dict(to_json(self.poll(9, machine_id=MACHINE2_ID, paused_for_lock=lk.token)), format_version=2,
                     file_type="poll")
        self.write(f"status/machines/{MACHINE2_ID}.poll.json", newer)
        self.assertEqual(self.a.check_poller_paused(MACHINE2_ID, lk.token), "paused")


# --------------------------------------------------------------------------
# 12. Heartbeats
# --------------------------------------------------------------------------

class HeartbeatTests(ShareCase):
    def beat(self, store, **fields):
        args = dict(can_poll=True, last_printer_reach=T0, polling=[PRINTER_ID], monitor_open=False, floor_board=False)
        args.update(fields)
        store.heartbeat().beat(**args)

    def hb_file(self, store):
        return decode_json(self.read(f"status/pollers/{store.identity.key}.json"), file_type="heartbeat", rel="x")

    def test_beat(self):
        st = self.store(identity(pid=4242))
        self.assertEqual(st.heartbeat().key, "FLOOR01_EXAMPLE_jsmith_4242")
        self.beat(st)
        d = self.hb_file(st)
        self.assertEqual((d["counter"], d["version"], d["polling"], d["can_poll"]), (1, "test", [PRINTER_ID], True))
        self.clock.advance(5)
        self.beat(st, polling=[])
        d = self.hb_file(st)
        self.assertEqual((d["counter"], d["written_at"], d["polling"]), (2, T0 + 5, []))
        self.assertEqual(d["declined"], [])
        self.beat(st, declined=[PRINTER_ID])
        self.assertEqual(self.hb_file(st)["declined"], [PRINTER_ID])
        self.assertEqual(st.live_heartbeats()[0].declined, [PRINTER_ID])

    def test_the_live_set(self):
        a, b = self.store(identity("jsmith", "FLOOR01")), self.store(identity("akim", "FLOOR02"))
        self.assertEqual(b.live_heartbeats(), [])
        self.beat(a)
        self.beat(b)
        self.assertEqual([hb.key for hb in b.live_heartbeats()], [a.identity.key, b.identity.key])
        for _ in range(2):
            self.clock.advance(5)
            self.beat(b)
            self.assertEqual(len(b.live_heartbeats()), 2)
        self.clock.advance(5)                       # a stopped 15 s ago
        self.beat(b)
        self.assertEqual([hb.key for hb in b.live_heartbeats()], [b.identity.key])
        self.beat(a)
        self.assertEqual(len(b.live_heartbeats()), 2)               # back again

    def test_other_formats_and_damaged_files_are_left_out(self):
        st = self.store()
        self.write("status/pollers/FLOOR09_EXAMPLE_x_1.json",
                   dict(to_json(HeartbeatInfo(key="FLOOR09_EXAMPLE_x_1", computer="FLOOR09", user="EXAMPLE\\x",
                                              pid=1, version="v3")), format_version=2, file_type="heartbeat"))
        self.write("status/pollers/FLOOR08_EXAMPLE_y_1.json", b"{damaged")
        self.write("status/pollers/FLOOR07_EXAMPLE_z_1.json",
                   dict(to_json(HeartbeatInfo(key="someone_else", computer="FLOOR07", user="EXAMPLE\\z", pid=1,
                                              version="v2")), format_version=1, file_type="heartbeat"))
        self.write("status/pollers/FLOOR06_EXAMPLE_w_1.json",
                   dict(to_json(HeartbeatInfo(key="FLOOR06_EXAMPLE_w_1", computer="FLOOR06", user="EXAMPLE\\w", pid=1,
                                              version="v2")), format_version=True, file_type="heartbeat"))
        self.write("status/pollers/desktop.ini", b"x")
        self.assertEqual(st.live_heartbeats(), [])

    def test_the_own_heartbeat_is_included_while_hidden(self):
        view = self.fs.view()
        view.hide_new_s = 10
        st, other = self.store(view=view), self.store(identity("akim", "FLOOR02"))
        self.beat(other)
        self.beat(st)
        self.assertEqual([hb.key for hb in st.live_heartbeats()], [st.identity.key])     # the other's is hidden
        self.clock.advance(10)
        self.beat(other)
        self.assertEqual(len(st.live_heartbeats()), 2)

    def test_old_files_are_removed(self):
        a, b = self.store(identity("jsmith", "FLOOR01")), self.store(identity("akim", "FLOOR02"))
        self.beat(a)
        b.live_heartbeats()
        self.clock.advance(599)
        b.live_heartbeats()
        self.assertIn(f"{a.identity.key}.json", self.listing("status/pollers"))
        self.clock.advance(1)
        b.live_heartbeats()
        self.assertNotIn(f"{a.identity.key}.json", self.listing("status/pollers"))

    def test_remove(self):
        st = self.store()
        self.beat(st)
        st.heartbeat().remove()
        self.assertEqual(self.listing("status/pollers"), [])
        self.assertEqual(st.live_heartbeats(), [])
        st.heartbeat().remove()                                    # best effort, again


# --------------------------------------------------------------------------
# 13. Assignment
# --------------------------------------------------------------------------

def hb(computer, user="jsmith", pid=1, can_poll=True):
    key = f"{computer}_EXAMPLE_{user}_{pid}"
    return HeartbeatInfo(key=key, computer=computer, user=f"EXAMPLE\\{user}", pid=pid, version="v2", can_poll=can_poll)


GOLDEN = {
    "pr_0000000001": ("FLOOR03, FLOOR04, FLOOR01, FLOOR02", "FLOOR03, FLOOR04, FLOOR01"),
    "pr_0000000002": ("FLOOR03, FLOOR01, FLOOR02, FLOOR04", "FLOOR03, FLOOR01, FLOOR04"),
    "pr_0000000003": ("FLOOR03, FLOOR01, FLOOR02, FLOOR04", "FLOOR03, FLOOR01, FLOOR04"),
    "pr_0000000004": ("FLOOR02, FLOOR04, FLOOR03, FLOOR01", "FLOOR04, FLOOR03, FLOOR01"),
    "pr_0000000005": ("FLOOR03, FLOOR02, FLOOR04, FLOOR01", "FLOOR03, FLOOR04, FLOOR01"),
    "pr_0000000006": ("FLOOR03, FLOOR04, FLOOR02, FLOOR01", "FLOOR03, FLOOR04, FLOOR01"),
    "pr_0000000007": ("FLOOR01, FLOOR03, FLOOR04, FLOOR02", "FLOOR01, FLOOR03, FLOOR04"),
    "pr_0000000008": ("FLOOR03, FLOOR04, FLOOR02, FLOOR01", "FLOOR03, FLOOR04, FLOOR01"),
    "pr_0000000009": ("FLOOR03, FLOOR01, FLOOR04, FLOOR02", "FLOOR03, FLOOR01, FLOOR04"),
    "pr_000000000a": ("FLOOR04, FLOOR03, FLOOR02, FLOOR01", "FLOOR04, FLOOR03, FLOOR01"),
    "pr_000000000b": ("FLOOR02, FLOOR04, FLOOR01, FLOOR03", "FLOOR04, FLOOR01, FLOOR03"),
    "pr_000000000c": ("FLOOR02, FLOOR01, FLOOR03, FLOOR04", "FLOOR01, FLOOR03, FLOOR04"),
}


class AssignmentTests(unittest.TestCase):
    def computers(self, ranks):
        return ", ".join(key.split("_")[0] for key in ranks)

    def test_score_vector(self):
        self.assertEqual(score("pr_0000000001", "FLOOR01"), 5924401758749967213)
        self.assertEqual(score("pr_0000000001", "floor01"), score("pr_0000000001", "FLOOR01"))

    def test_golden_table(self):
        pcs = [hb(f"FLOOR0{n}", pid=100 + n) for n in range(1, 5)]
        full = assign(list(GOLDEN), pcs)
        without = assign(list(GOLDEN), [h for h in pcs if h.computer != "FLOOR02"])
        for printer, (ranks, ranks_without) in GOLDEN.items():
            self.assertEqual(self.computers(full[printer]), ranks, printer)
            self.assertEqual(self.computers(without[printer]), ranks_without, printer)
        moved = sorted(p for p in GOLDEN if full[p][0] != without[p][0])
        self.assertEqual(moved, ["pr_0000000004", "pr_000000000b", "pr_000000000c"])

    def test_one_computer_two_users(self):
        a, b = hb("FLOOR01", "jsmith", 2), hb("FLOOR01", "akim", 9)
        ranks = assign(["pr_0000000001"], [a, b, hb("FLOOR03")])["pr_0000000001"]
        self.assertEqual(ranks, ["FLOOR03_EXAMPLE_jsmith_1", b.key, a.key])    # side by side, by key

    def test_a_declined_printer_skips_that_pc(self):
        # states.md §6.2 item 3.0: a PC whose worker for a printer is stuck declines it in its heartbeat, so
        # every PC ranks the next one first for that printer only
        a, b = hb("FLOOR01"), hb("FLOOR03")
        both = assign(["pr_0000000001", "pr_0000000002"], [a, b])
        first = both["pr_0000000001"][0]
        declining = dataclasses.replace(a if first == a.key else b, declined=["pr_0000000001"])
        other = b if first == a.key else a
        ranks = assign(["pr_0000000001", "pr_0000000002"], [declining, other])
        self.assertEqual(ranks["pr_0000000001"], [other.key])
        self.assertEqual(ranks["pr_0000000002"], both["pr_0000000002"])

    def test_who_is_left_out(self):
        self.assertEqual(assign(["pr_0000000001"], [hb("FLOOR01", can_poll=False)]), {"pr_0000000001": []})
        self.assertEqual(assign(["pr_0000000001"], []), {"pr_0000000001": []})
        self.assertEqual(assign(["pr_0000000001"], [hb("FLOOR01"), hb("FLOOR01")]),
                         {"pr_0000000001": ["FLOOR01_EXAMPLE_jsmith_1"]})

    def test_balance(self):
        pcs = [hb(f"FLOOR0{n}") for n in range(1, 6)]
        counts = {}
        for ranks in assign([f"pr_{i:010x}" for i in range(1000)], pcs).values():
            counts[ranks[0]] = counts.get(ranks[0], 0) + 1
        self.assertEqual(sorted(counts.values(), reverse=True), [213, 206, 199, 197, 185])
        for n in counts.values():
            self.assertTrue(150 <= n <= 250)

    def test_takeover_order(self):
        self.assertEqual([takeover_after_s(r) for r in range(5)], [0.0, 15.0, 20.0, 25.0, 30.0])
        fast = TIMINGS.scaled(0.1)
        for r, expected in enumerate((0.0, 1.5, 2.0, 2.5, 3.0)):
            self.assertAlmostEqual(takeover_after_s(r, fast), expected)
        self.assertEqual(settle_s(), 6.0)
        self.assertAlmostEqual(settle_s(fast), 0.6)


# --------------------------------------------------------------------------
# 14. Reach memory
# --------------------------------------------------------------------------

class ReachMemoryTests(ShareCase):
    def test_reach_survives_a_restart(self):
        local = LocalStore(self.pc(), clock=self.clock)
        reach = ReachMemory(local, clock=self.clock)
        self.assertIsNone(reach.last_printer_reach)
        self.assertFalse(reach.can_poll())
        reach.reached()
        self.assertEqual(local.read_local_json("reach.json"), {"last_printer_reach": T0})
        with open(os.path.join(local.hx_dir, "reach.json"), "rb") as f:
            self.assertEqual(json.loads(f.read())["file_type"], "local_state")
        again = ReachMemory(LocalStore(self.pc(), clock=self.clock), clock=self.clock)
        self.assertEqual(again.last_printer_reach, T0)
        self.clock.advance(86_400)
        self.assertTrue(again.can_poll())
        self.clock.advance(1)
        self.assertFalse(again.can_poll())

    def test_the_file_is_written_every_300_s(self):
        local = LocalStore(self.pc(), clock=self.clock)
        reach = ReachMemory(local, clock=self.clock)
        reach.reached()
        self.clock.advance(299)
        reach.reached()
        self.assertEqual(reach.last_printer_reach, T0 + 299)
        self.assertEqual(local.read_local_json("reach.json"), {"last_printer_reach": T0})
        self.clock.advance(1)
        reach.reached()
        self.assertEqual(local.read_local_json("reach.json"), {"last_printer_reach": T0 + 300})

    def test_clock_tolerance(self):
        reach = ReachMemory(None, clock=self.clock)
        reach.reached()
        self.clock.set_wall(T0 - 300)
        self.assertTrue(reach.can_poll())
        self.clock.set_wall(T0 - 301)
        self.assertFalse(reach.can_poll())

    def test_probe_due(self):
        reach = ReachMemory(None, clock=self.clock)
        self.assertFalse(reach.probe_due(unpolled=False))
        self.assertTrue(reach.probe_due(unpolled=True))
        reach.probed(False)
        self.assertFalse(reach.probe_due(unpolled=True))
        self.clock.advance(59)
        self.assertFalse(reach.probe_due(unpolled=True))
        self.clock.advance(1)
        self.assertTrue(reach.probe_due(unpolled=True))
        reach.reached()
        reach.probed(True)
        self.assertFalse(reach.probe_due(unpolled=False))         # never with a live poller, reach or not
        self.clock.advance(60)
        self.assertTrue(reach.probe_due(unpolled=True))
        self.assertFalse(reach.probe_due(unpolled=False))

    def test_a_damaged_reach_file(self):
        local = LocalStore(self.pc(), clock=self.clock)
        os.makedirs(local.hx_dir)
        with open(os.path.join(local.hx_dir, "reach.json"), "wb") as f:
            f.write(b'{"format_version": 1, "file_type": "local_state", "last_printer_reach": true}')
        self.assertIsNone(ReachMemory(local, clock=self.clock).last_printer_reach)
        for bad in (b"9" * 400, b"NaN", b"1e400"):              # 9...9 made math.isfinite raise OverflowError
            with open(os.path.join(local.hx_dir, "reach.json"), "wb") as f:
                f.write(b'{"format_version": 1, "file_type": "local_state", "last_printer_reach": ' + bad + b"}")
            self.assertIsNone(ReachMemory(local, clock=self.clock).last_printer_reach, bad[:5])


# --------------------------------------------------------------------------
# 15. Status files
# --------------------------------------------------------------------------

class StatusFileTests(ShareCase):
    def setUp(self):
        super().setUp()
        self.poller = self.store(identity("floor", "FLOOR02"))
        self.sender = self.store(identity(sender=True))

    def test_poll_round_trip(self):
        status = self.poll(18734, reachable=True, printing=True, bag_count=436512, alerts=[],
                           running=None, daily={"fonts": ["Arial"]})
        self.poller.write_poll(status)
        d = decode_json(self.read(f"status/machines/{MACHINE_ID}.poll.json"), file_type="poll", rel="x")
        self.assertEqual((d["poller"], d["written_at"], d["counter"]), ("FLOOR02_EXAMPLE_floor_4242", T0, 18734))
        back = self.sender.read_poll(MACHINE_ID)
        self.assertEqual(back, dataclasses.replace(status, poller="FLOOR02_EXAMPLE_floor_4242", written_at=T0))
        self.assertEqual(self.sender.poll_age(MACHINE_ID), 0.0)
        self.assertEqual(status.poller, "")                          # the caller's copy is untouched

    def test_poll_counter_must_go_up(self):
        self.poller.write_poll(self.poll(5))
        for counter in (5, 4):
            with self.assertRaises(ValueError):
                self.poller.write_poll(self.poll(counter))
        taker = self.store(identity("akim", "FLOOR03"))
        taker.read_poll(MACHINE_ID)
        with self.assertRaises(ValueError):
            taker.write_poll(self.poll(5))
        taker.write_poll(self.poll(6))                              # a takeover continues from file + 1

    def test_expected_state(self):
        state = ExpectedState(machine_id=MACHINE_ID, mode="running", send_id="sd_8a7b6c5d4e", counter=99,
                              origin_line="Product of USA and Canada")
        self.sender.write_expected(state)
        d = decode_json(self.read(f"status/machines/{MACHINE_ID}.expected.json"), file_type="expected", rel="x")
        self.assertEqual((d["counter"], d["written_by"], d["computer"], d["written_at"]),
                         (1, "EXAMPLE\\jsmith", "FLOOR01", T0))
        self.clock.advance(1)
        self.store(identity("akim", "FLOOR02")).write_expected(dataclasses.replace(state, confirmed=True))
        back = self.sender.read_expected(MACHINE_ID)
        self.assertEqual((back.counter, back.written_by, back.computer, back.confirmed),
                         (2, "EXAMPLE\\akim", "FLOOR02", True))
        self.write(f"status/machines/{MACHINE_ID}.expected.json", b"{damaged")
        self.assertIsNone(self.sender.read_expected(MACHINE_ID))
        self.sender.write_expected(state)
        self.assertEqual(self.sender.read_expected(MACHINE_ID).counter, 1)

    def test_newer_status_files(self):
        newer = dict(to_json(self.poll(7)), format_version=2, file_type="poll", poller="FLOOR09_x_1")
        self.write(f"status/machines/{MACHINE_ID}.poll.json", newer)
        with self.assertRaises(FormatTooNew):
            self.sender.read_poll(MACHINE_ID)
        self.assertEqual(self.sender.poll_age(MACHINE_ID), 0.0)
        self.clock.advance(10)
        with self.assertRaises(FormatTooNew):
            self.sender.read_poll(MACHINE_ID)
        self.assertEqual(self.sender.poll_age(MACHINE_ID), 10.0)
        with self.assertRaises(ValueError):
            self.sender.write_poll(self.poll(7))                    # its counter counts too

        rel = f"status/machines/{MACHINE_ID}.expected.json"
        self.write(rel, {"format_version": 2, "file_type": "expected", "machine_id": MACHINE_ID, "counter": 3})
        with self.assertRaises(FormatTooNew):
            self.sender.read_expected(MACHINE_ID)
        with self.assertRaises(FormatTooNew):
            self.sender.write_expected(ExpectedState(machine_id=MACHINE_ID))
        self.assertEqual(json.loads(self.read(rel))["format_version"], 2)   # not written

    def test_a_damaged_poll_file_counts_as_missing(self):
        self.write(f"status/machines/{MACHINE_ID}.poll.json", b"{damaged")
        self.assertIsNone(self.sender.read_poll(MACHINE_ID))
        self.clock.advance(3)
        self.assertIsNone(self.sender.read_poll(MACHINE_ID))
        self.assertEqual(self.sender.poll_age(MACHINE_ID), 3.0)
        other = dict(to_json(self.poll(2, machine_id=MACHINE2_ID)), format_version=1, file_type="poll")
        self.write(f"status/machines/{MACHINE_ID}.poll.json", other)        # another machine's content
        self.assertIsNone(self.sender.read_poll(MACHINE_ID))

    def test_hand_edited_numbers_and_nesting_are_damage_too(self):
        # Domain Users can edit status\machines. json.loads takes a 400-digit int, NaN, 1e400 and a
        # 10 000-deep nest, which raised OverflowError or RecursionError past every reader that catches
        # ValueError: the watcher's pass then stopped for the whole floor, keeping the last board
        poll_rel, expected_rel = (f"status/machines/{MACHINE_ID}.poll.json",
                                  f"status/machines/{MACHINE_ID}.expected.json")
        deep = "[" * 10_000 + "]" * 10_000
        for i, (name, value) in enumerate((("huge int", "9" * 400), ("NaN", "NaN"), ("1e400", "1e400"),
                                           ("nesting", deep))):
            with self.subTest(name=name):
                field = "daily" if name == "nesting" else "written_at"
                value = '{"a": ' + value + "}" if name == "nesting" else value
                for rel, file_type in ((poll_rel, "poll"), (expected_rel, "expected")):
                    self.write(rel, ('{"format_version": 1, "file_type": "%s", "machine_id": "%s", "printer_id": '
                                     '"%s", "poller": "FLOOR09_x_1", "counter": 7, "%s": %s}'
                                     % (file_type, MACHINE_ID, PRINTER_ID, field, value)).encode())
                self.assertIsNone(self.sender.read_poll(MACHINE_ID))
                self.assertEqual(self.sender.check_poller_paused(MACHINE_ID, "t"), "polling")
                self.assertIsNone(self.sender.read_expected(MACHINE_ID))
                self.assertIsNone(self.sender.cached_expected(MACHINE_ID))
                ack = '{"format_version": 1, "file_type": "ack", "alert_id": "drift-x-%d", "by": "b", ' \
                      '"computer": "c", "at": %s}' % (i, value)
                self.write(f"status/alerts/drift-x-{i}.json", ack.encode())
                self.assertIsNone(self.sender.read_ack(f"drift-x-{i}"))
        self.clock.advance(TIMINGS.stale_s)
        self.assertEqual(self.sender.check_poller_paused(MACHINE_ID, "t"), "missing")
        self.poller.write_poll(self.poll(1))                     # a taker overwrites it from counter 1
        self.assertEqual(self.sender.read_poll(MACHINE_ID).counter, 1)

    def test_line_status(self):
        status = LineStatus(line_id=LINE_ID, send_id="sd_8a7b6c5d4e", countries=["USA", "Canada"],
                            machines={MACHINE_ID: "amber"})
        self.sender.write_line_status(status)
        self.sender.write_line_status(status)
        back = self.poller.read_line_status(LINE_ID)
        self.assertEqual((back.counter, back.written_by, back.machines), (2, "EXAMPLE\\jsmith", {MACHINE_ID: "amber"}))
        self.assertIsNone(self.poller.read_line_status("ln_00000000ff"))

    def test_acks(self):
        view = self.fs.view()
        reader = self.store(identity("floor", "FLOOR03"), view=view)
        self.assertIsNone(reader.read_ack("drift-mc_5d1e2f3a4b-1"))
        self.sender.write_ack(Ack(alert_id="drift-mc_5d1e2f3a4b-1", by="EXAMPLE\\akim", computer="FLOOR02", at=T0))
        self.sender.write_ack(Ack(alert_id="drift-mc_5d1e2f3a4b-1", by="EXAMPLE\\jsmith", computer="FLOOR01",
                                  at=T0 + 1))
        view.calls.clear()
        for _ in range(2):
            ack = reader.read_ack("drift-mc_5d1e2f3a4b-1")
            self.assertEqual((ack.by, ack.computer), ("EXAMPLE\\akim", "FLOOR02"))
        self.assertEqual([op for op, _ in view.calls], ["read_bytes"])
        with self.assertRaises(ValueError):
            reader.read_ack("../x")
        self.write("status/alerts/newer-1.json", {"format_version": 2, "file_type": "ack", "alert_id": "newer-1"})
        self.assertEqual(reader.read_ack("newer-1").alert_id, "newer-1")
        for i, bad in enumerate((b"9" * 400, b"NaN", b"1e400")):  # 9...9 made float() raise OverflowError
            self.write(f"status/alerts/newer-{i + 2}.json",
                       b'{"format_version": 2, "file_type": "ack", "alert_id": "newer-%d", "at": %s}' % (i + 2, bad))
            self.assertEqual(reader.read_ack(f"newer-{i + 2}").at, 0.0)     # still acknowledged

    def test_preview(self):
        bmp = b"BM" + bytes(20540)
        self.poller.write_preview(MACHINE_ID, bmp)
        self.assertEqual(self.sender.read_preview(MACHINE_ID), bmp)
        self.assertIsNone(self.sender.read_preview(MACHINE2_ID))
        with self.assertRaises(ValueError):
            self.poller.write_preview(MACHINE_ID, bytes(1_000_001))

    def test_cached_expected(self):
        view = self.fs.view()
        st = self.store(identity(sender=True), view=view)
        self.assertIsNone(st.cached_expected(MACHINE_ID))
        st.write_expected(ExpectedState(machine_id=MACHINE_ID, send_id="sd_1"))
        self.assertEqual(st.cached_expected(MACHINE_ID).counter, 1)
        self.store(identity("akim", "FLOOR02")).write_expected(ExpectedState(machine_id=MACHINE_ID, send_id="sd_2"))
        st.read_expected(MACHINE_ID)
        view.down = True
        with self.assertRaises(ShareUnavailable):
            st.read_expected(MACHINE_ID)
        cached = st.cached_expected(MACHINE_ID)
        self.assertEqual((cached.send_id, cached.counter), ("sd_2", 2))
        cached.send_id = "changed"
        self.assertEqual(st.cached_expected(MACHINE_ID).send_id, "sd_2")

    def test_cached_poll(self):
        # The newest P any reader on this PC read, kept for the standalone seed (states.md §6.12 item 3)
        view = self.fs.view()
        st = self.store(identity(sender=True), view=view)
        self.assertIsNone(st.cached_poll(MACHINE_ID))
        self.poller.write_poll(self.poll(1))
        self.poller.write_poll(self.poll(2))
        self.assertEqual(st.read_poll(MACHINE_ID).counter, 2)
        view.down = True
        with self.assertRaises(ShareUnavailable):
            st.read_poll(MACHINE_ID)
        cached = st.cached_poll(MACHINE_ID)
        self.assertEqual(cached.counter, 2)
        cached.counter = 99
        self.assertEqual(st.cached_poll(MACHINE_ID).counter, 2)
        self.assertIsNone(st.cached_poll(MACHINE2_ID))

    def test_ids_are_checked(self):
        for call in (lambda: self.sender.read_poll("x"), lambda: self.sender.read_lock("../pr"),
                     lambda: self.sender.read_expected("mc_ZZZZZZZZZZ"), lambda: self.sender.read_line_status(""),
                     lambda: self.sender.write_ack(Ack(alert_id="a/b", by="x", computer="y", at=0.0)),
                     lambda: self.sender.write_poll(self.poll(1, machine_id="machine")),
                     lambda: self.sender.read_bytes("../outside"), lambda: self.sender.read_bytes("a//b"),
                     lambda: self.sender.read_bytes("C:x"), lambda: self.sender.read_bytes("a\\b"),
                     lambda: self.sender.write_json("status/x.json", {"no": "type"})):
            with self.assertRaises(ValueError):
                call()


# --------------------------------------------------------------------------
# 16. The not-found cache; 17. per-view faults
# --------------------------------------------------------------------------

class FakeShareTests(ShareCase):
    def test_not_found_cache(self):
        b_view = self.fs.view()
        b_view.not_found_cache_s = 5
        a = self.store(identity(sender=True))
        b = self.store(identity("akim", "FLOOR02"), view=b_view)
        self.assertIsNone(b.read_lock(PRINTER_ID))
        a.lock(PRINTER_ID, purpose="send", run_id="sd_1").acquire()
        self.assertIsNotNone(a.read_lock(PRINTER_ID))               # A isn't fooled by its own file
        self.assertIsNone(b.read_lock(PRINTER_ID))
        self.clock.advance(4.9)
        self.assertIsNone(b.read_lock(PRINTER_ID))
        self.clock.advance(0.1)
        self.assertEqual(b.read_lock(PRINTER_ID).user, "EXAMPLE\\jsmith")
        self.assertGreater(settle_s(), 5)
        self.assertEqual(settle_s(), 6.0)

    def test_per_view_faults(self):
        a_view, b_view = self.fs.view(), self.fs.view(down=True)
        a, b = self.store(view=a_view), self.store(identity("akim", "FLOOR02"), view=b_view)
        with self.assertRaises(ShareUnavailable):
            b.read_bytes("status/lines/x.json")
        a.write_bytes("status/lines/x.json", b"x")
        self.assertEqual(a.read_bytes("status/lines/x.json"), b"x")
        b_view.down = False
        self.assertEqual(b_view.read_bytes(self.path("status/lines/x.json")), b"x")
        b.probe_now()
        self.assertTrue(wait_until(lambda: b.available))
        self.assertEqual(b.read_bytes("status/lines/x.json"), b"x")
        self.assertFalse(self.fs.down)

        self.fs.slow_s = 0.05
        try:
            for view in (a_view, b_view):
                started = time.perf_counter()
                view.listdir(self.root)
                self.assertGreaterEqual(time.perf_counter() - started, 0.045)
        finally:
            self.fs.slow_s = 0.0
        self.assertEqual(b_view.slow_s, 0.0)

    def test_a_view_that_changes_a_shared_switch_gets_its_own(self):
        a_view, c_view = self.fs.view(), self.fs.view()
        a_view.fail_replace = 10
        self.assertEqual((self.fs.fail_replace, c_view.fail_replace), (0, 0))
        target = self.path("status/lines/y.json")
        c_view.write_whole(target, b"c")
        a_view.replace_retry_s = ()
        with self.assertRaises(PermissionError):
            a_view.write_whole(target, b"a")
        self.assertEqual(a_view.fail_replace, 9)
        self.fs.break_after_create = 1                             # the root's switch: every view sharing it
        with self.assertRaises(OSError):
            c_view.create_exclusive(self.path("status/lines/z.json"), b"zz")
        self.assertEqual(self.fs.break_after_create, 0)

    def test_hide_new_s(self):
        self.fs.hide_new_s = 10
        a_view, c_view = self.fs.view(), self.fs.view()
        a_view.create_exclusive(self.path("status/pollers/a.json"), b"x")
        self.assertEqual(a_view.listdir(self.path("status/pollers")), ["a.json"])
        self.assertEqual(c_view.listdir(self.path("status/pollers")), [])
        self.clock.advance(10)
        self.assertEqual(c_view.listdir(self.path("status/pollers")), ["a.json"])
        a_view.makedirs(self.path("logs/hx-events/2026-10"))
        self.assertNotIn("2026-10", c_view.listdir(self.path("logs/hx-events")))


# --------------------------------------------------------------------------
# Film files, previews, folders and the rest
# --------------------------------------------------------------------------

class FilmFileTests(ShareCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.view = self.fs.view()
        self.data, self.st, _bus = self.service(ADMIN, view=self.view)
        self.data.run_once()
        self.source = os.path.join(self.tmp, "Front photo (final).JPG")
        with open(self.source, "wb") as f:
            f.write(b"\xff\xd8" + bytes(range(256)) * 40)
        with open(self.source, "rb") as f:
            self.sha = hashlib.sha256(f.read()).hexdigest()

    def test_save_file(self):
        with mock.patch.object(hx_share, "CHUNK_BYTES", 4096):
            ref = self.data.save_file(FILM_ID, "front_photo", self.source)
        self.assertEqual(ref.path, f"films/{FILM_ID}/files/{self.sha[:8]}_Front_photo_final_.JPG")
        self.assertEqual((ref.kind, ref.name, ref.sha256, ref.size), ("front_photo", "Front photo (final).JPG",
                                                                      self.sha, 2 + 256 * 40))
        self.assertEqual((ref.added_by, ref.added_at), ("EXAMPLE\\admin", T0))
        with open(self.source, "rb") as f:
            self.assertEqual(self.read("templates/" + ref.path), f.read())
        self.assertEqual(sum(1 for op, _ in self.view.calls if op == "copy_chunk"), 3)
        self.assertEqual([n for n in self.listing(f"templates/films/{FILM_ID}/files") if n.endswith(".tmp")], [])
        (entry,) = [e for e in self.st.read_logs("hx-admin") if e["type"] == "file_added"]
        self.assertEqual((entry["film_id"], entry["path"], entry["sha256"], entry["size"]),
                         (FILM_ID, ref.path, self.sha, ref.size))
        self.view.calls.clear()
        self.assertEqual(self.data.save_file(FILM_ID, "front_photo", self.source).path, ref.path)
        self.assertFalse(any(op.startswith("copy_") for op, _ in self.view.calls))   # already there

    def test_save_file_for_a_new_film(self):
        film = new_id("fm")
        ref = self.data.save_file(film, "spec", self.source)
        self.assertTrue(os.path.exists(self.path("templates/" + ref.path)))

    def test_save_file_refusals(self):
        with self.assertRaises(ValueError):
            self.data.save_file(FILM_ID, "selfie", self.source)
        with mock.patch("os.path.getsize", return_value=50_000_001):
            with self.assertRaises(ShareError) as cm:
                self.data.save_file(FILM_ID, "artwork", self.source)
        self.assertEqual(cm.exception.msg, Msg("The file is too big (at most {mb} MB).", {"mb": 50}))
        sender, _st, _bus = self.service(identity(sender=True))
        sender.run_once()
        with self.assertRaises(NotAllowed):
            sender.save_file(FILM_ID, "artwork", self.source)
        with self.assertRaises(NotAllowed):
            sender.save_preview(FILM_ID, SHA_FRONT, PNG)

    def test_a_failed_copy_leaves_no_temp_file(self):
        self.view.fail_replace = 100
        self.view.replace_retry_s = ()
        with self.assertRaises(ShareDenied):
            self.data.save_file(FILM_ID, "artwork", self.source)
        self.assertEqual(self.listing(f"templates/films/{FILM_ID}/files"), [])

    def test_previews(self):
        self.data.save_preview(FILM_ID, SHA_FRONT, PNG)
        self.assertEqual(self.read(f"templates/films/{FILM_ID}/previews/{SHA_FRONT}.png"), PNG)
        for bad in (b"GIF89a", PNG + bytes(512_000)):
            with self.assertRaises(ValueError):
                self.data.save_preview(FILM_ID, SHA_FRONT, bad)
        local = self.data.local
        self.assertFalse(local.has_preview(SHA_FRONT))
        self.assertEqual(self.data.preview_bytes(FILM_ID, SHA_FRONT), PNG)       # from the share, then cached
        self.assertTrue(local.has_preview(SHA_FRONT))
        os.remove(self.path(f"templates/films/{FILM_ID}/previews/{SHA_FRONT}.png"))
        self.assertEqual(self.data.preview_bytes(FILM_ID, SHA_FRONT), PNG)
        self.assertIsNone(self.data.preview_bytes(FILM_ID, "d" * 64))

    def test_share_path(self):
        self.assertEqual(self.data.share_path(f"films/{FILM_ID}/files/a.jpg"),
                         os.path.join(self.root, "templates", "films", FILM_ID, "files", "a.jpg"))
        self.assertEqual(self.data.share_path("status/machines/x.bmp"),
                         os.path.join(self.root, "status", "machines", "x.bmp"))
        no_share, _st, _bus = self.service(ADMIN, store=ShareStore(None, identity=ADMIN))
        self.assertIsNone(no_share.share_path("films/x"))


class FolderTests(ShareCase):
    def test_stripes_makes_only_its_own_folders(self):
        st = self.store(ADMIN)
        st.makedirs(f"templates/films/{FILM_ID}/files")
        self.assertTrue(os.path.isdir(self.path(f"templates/films/{FILM_ID}/files")))
        shutil.rmtree(self.path("templates/films"))
        with self.assertRaises(ShareError) as cm:
            st.makedirs(f"templates/films/{FILM_ID}/previews")
        self.assertEqual(cm.exception.msg.values["folder"], "templates/films")
        self.assertFalse(os.path.exists(self.path("templates/films")))

    def test_write_record_makes_a_test_world(self):
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        st = self.store(ADMIN)
        for rec in records():
            write_record(st, rec)
        for rel in hx_share._SHARE_FOLDERS:
            self.assertTrue(os.path.isdir(self.path(rel)), rel)
        self.assertEqual(json.loads(self.read(f"templates/films/{FILM_ID}/film.json"))["revision"], 4)
        self.assertEqual(self.listing("logs/hx-admin"), [])         # no log, no changes.json
        self.assertFalse(os.path.exists(self.path("config/hx/changes.json")))

    def test_crash_leftovers_are_cleaned_once(self):
        mine = identity(pid=4242)
        tag = hashlib.sha256(b"FLOOR01|EXAMPLE\\jsmith").hexdigest()[:12]
        old = f"x.json.{tag}-1111-abcdef.tmp"
        live = f"x.json.{tag}-4242-abcdef.tmp"
        theirs = "x.json.0123456789ab-1111-abcdef.tmp"
        for name in (old, live, theirs):
            self.write(f"status/machines/{name}", b"x")
        st = self.store(mine)
        st.read_bytes("config/hx/changes.json")
        self.assertEqual(self.listing("status/machines"), sorted([live, theirs]))


class MiscTests(unittest.TestCase):
    def test_safe_file_name(self):
        self.assertEqual(safe_file_name("front.jpg"), "front.jpg")
        self.assertEqual(safe_file_name("C:\\Users\\x\\Spec sheet (v2).pdf"), "Spec_sheet_v2_.pdf")
        self.assertEqual(safe_file_name("ñandú.jpeg"), "_and_.jpeg")
        self.assertEqual(safe_file_name("a" * 60 + ".tar.gz"), "a" * 48 + ".gz")
        self.assertEqual(safe_file_name("photo.j p*g"), "photo.jpg")
        self.assertEqual(safe_file_name(""), "file")
        self.assertEqual(safe_file_name("..."), "file")
        self.assertEqual(safe_file_name("x."), "x")

    def test_record_rel(self):
        self.assertEqual(record_rel(Company(id="company")), "config/hx/company.json")
        self.assertEqual(hx_share.record_rel_for(Film, FILM_ID), f"templates/films/{FILM_ID}/film.json")
        for cls, bad in ((Company, "x"), (Line, "ln_123"), (MachineFilmAdjustment, MACHINE_ID),
                         (ExpectedState, MACHINE_ID)):
            with self.assertRaises(ValueError):
                hx_share.record_rel_for(cls, bad)

    def test_self_check(self):
        before = set(os.listdir(tempfile.gettempdir()))
        hx_share.self_check()
        after = set(os.listdir(tempfile.gettempdir()))
        self.assertEqual({n for n in after - before if n.startswith("stripes-hx-share-")}, set())

    def test_self_check_raises_on_failure(self):
        with mock.patch.object(FileOps, "create_exclusive", return_value=False):
            with self.assertRaises(RuntimeError):
                hx_share.self_check()

    def test_user_settings(self):
        tmp = tempfile.mkdtemp(prefix="hx-local-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        local = LocalStore(HxPaths(share_root=None, roaming=os.path.join(tmp, "r"), local=os.path.join(tmp, "l"),
                                   machine=os.path.join(tmp, "m")))
        self.assertEqual(local.read_user_json("maplejet.json"), {})
        local.write_user_json("maplejet.json", {"floor_board_at_startup": True, "floor_board_size": "full"})
        self.assertEqual(local.read_user_json("maplejet.json"),
                         {"floor_board_at_startup": True, "floor_board_size": "full"})
        with open(os.path.join(tmp, "r", "maplejet.json"), "rb") as f:
            d = json.loads(f.read())
        self.assertEqual((d["format_version"], d["file_type"]), (1, "user_settings"))
        with open(os.path.join(tmp, "r", "maplejet.json"), "wb") as f:
            f.write(b'\xef\xbb\xbf{"format_version": 2, "file_type": "user_settings", "x": 1}')
        self.assertEqual(local.read_user_json("maplejet.json"), {})
        with self.assertRaises(ValueError):
            local.read_user_json("../settings.json")

    def test_spanish(self):
        import i18n
        for english, spanish in hx_share.ES.items():
            self.assertEqual(i18n.ES[english], spanish)


if __name__ == "__main__":
    unittest.main()
