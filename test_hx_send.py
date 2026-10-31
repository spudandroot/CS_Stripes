"""Tests for hx_send: the Send pipeline, the sender's answer, Stop, Resume, Set clocks and the test print.

Every test drives FakeWorld printers (protocol.md §16.2) through the real share code on a temp folder,
with scaled timings and a fresh GateRegistry per simulated PC. Each test that talks to a printer ends
by checking the fake saw no violation and never more than one request at a time.
Run: python -m unittest test_hx_send -v
"""

import contextlib
import dataclasses
import io
import itertools
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

import hx_fake
import hx_send
import hx_share
import i18n
from hx_core import SYSTEM_CLOCK, CancelToken, Clock, EventBus, Msg
from hx_fake import FakePrinter
from hx_message import (NOTE_TEXTS, STEP_TEXTS, fingerprint, merge_plan, origin_from_design, plan_test,
                        resume_refusal)
from hx_model import (ALLOWED_WRITE_TAGS, AMBER, GREEN, RED, STANDARD_FONTS, STEP_BUILD, STEP_CHECK_CHOICE,
                      STEP_CHECK_MACHINE, STEP_CONFIRM_1, STEP_CONFIRM_2, STEP_LOCK, STEP_SET_CLOCK, STEP_SWITCH,
                      STEP_VERIFY_IDLE, STEP_WAIT_POLLER, STEPS, TIMINGS, ActionEvent, ActionFinished, Calibration,
                      ExpectedState, Line, LineChoice, LineStatus, LockHeld, Machine, NotAllowed, PlanError, PollStatus,
                      RunningMessage, SendEvent, SendFinished, SendRequest, ShareError, ShareUnavailable, TestEvent,
                      TestFinished, TestRanges, TestResult, from_json, new_id, to_json)
from hx_protocol import ClientPool, GateRegistry, release_key

try:        # hx_state is built by another package; its checks run once it is there
    import hx_state
    HX_STATE = hasattr(hx_state, "classify") and hasattr(hx_state, "Facts")
except Exception:
    hx_state, HX_STATE = None, False

# The real waits scaled down, with printer timeouts that leave room for a busy test machine. The lock's
# stale time stays above twice the longest call, as the lock budget needs (storage.md §11.3)
FAST = dataclasses.replace(TIMINGS.scaled(0.02), http_timeout_s=0.25, web_repeat_window_s=0.25,
                           lock_stale_s=4.0, share_timeout_s=1.0, test_no_print_s=0.3)

WRITES = ["TD", "CRTMSG", "CTDAT", "CTEXT", "CTEXT", "CPPARM", "SAVEMSG", "PRINTMSG", "PRINT"]
GOLDEN_DELAY = [       # send.md §6.4
    "<CRTMSG>EXCO1-PKO01-FDNP-H9</CRTMSG>",
    "<CTDAT><OBJID>10001</OBJID><POS>0,0</POS><MARG>0,0</MARG><FONTIND>2</FONTIND><SIZE>4</SIZE>"
    "<PREF>Packed on </PREF><FRMT>M/D/y</FRMT><DAY>0</DAY><MON>0</MON><YEAR>0</YEAR></CTDAT>",
    "<CTEXT><OBJID>10002</OBJID><XRID>10001</XRID><YRID>10001</YRID><SXAS>1</SXAS><SYAS>0</SYAS><MARG>0,0</MARG>"
    "<FONTIND>2</FONTIND><SIZE>4</SIZE><TXT>Product of USA and Canada</TXT></CTEXT>",
    # the lot is right of the origin with gap 0: it is sent SIDE_GAP_MIN (8) away, never touching it
    "<CTEXT><OBJID>10003</OBJID><XRID>10002</XRID><YRID>10002</YRID><SXAS>0</SXAS><SYAS>1</SYAS><MARG>8,0</MARG>"
    "<FONTIND>2</FONTIND><SIZE>4</SIZE><TXT>Lot 4417</TXT></CTEXT>",
    "<CPPARM><ENCST>0</ENCST><DELAY>480000</DELAY><PRNSPD>2400</PRNSPD><ROTAT>0</ROTAT><MIRR>0</MIRR>"
    "<RESMOD>0</RESMOD><BOLD>1</BOLD><WDTH>1</WDTH><QUANT>1</QUANT><REVRS>0</REVRS><AUTREP>0</AUTREP></CPPARM>",
    "<SAVEMSG>9</SAVEMSG>",
]
LOCK_LOST = "Another PC took over this printer's lock."


# --------------------------------------------------------------------------
# What the fakes saw
# --------------------------------------------------------------------------

def apcmds(fake):
    """The APCMD of every write the fake received, in order."""
    out = []
    for req in list(fake.requests):
        if req["kind"] != "autopilot":
            continue
        m = re.search(r"<APCMD>(.*)</APCMD>", req["body"].decode("utf-8", "replace"), re.S)
        if m and not m.group(1).strip().endswith("/>"):
            out.append(m.group(1))
    return out


def queries(fake, tag):
    """How many times the fake was asked the query tag."""
    return sum(1 for req in list(fake.requests)
               if req["kind"] == "autopilot" and req["tag"] == tag and f"<{tag}/>".encode() in req["body"])


def loads(fake):
    """The slot of every web Load, in order (0 = the message printing now)."""
    out = []
    for req in list(fake.requests):
        m = re.search(rb"<MSNO>(\d+)</MSNO>", req["body"]) if req["kind"] == "sme" else None
        if m:
            out.append(int(m.group(1)))
    return out


def dlymm(fake):
    return [int(m.group(1)) for x in apcmds(fake) for m in [re.search(r"<DLYMM>(\d+)</DLYMM>", x)] if m]


def change_lot(body):
    return body.replace(b"<TXT>Lot 4417</TXT>", b"<TXT>Lot 4418</TXT>")


def change_lot_and_origin(body):
    return change_lot(body).replace(b"<TXT>Product of USA and Canada</TXT>", b"<TXT>Product of USA and Mexico</TXT>")


# A record id in a text people read: "mc_5d1e2f3a4b", "ln_missing"
ID_TEXT = re.compile(r"\b(?:mc|ln|pr|fm|pt|pd|sd|tp|ac)_\w+")


def spanish(msg):
    """The Msg as a Spanish screen shows it, nested Msgs included."""
    values = {k: spanish(v) if isinstance(v, Msg) else v for k, v in msg.values.items()}
    i18n.set_language("es")
    try:
        return i18n.tr(msg.text, **values)
    finally:
        i18n.set_language("en")


def change_date(body):
    return re.sub(rb"<TXT>\d+/\d+/\d+</TXT>", b"<TXT>01/01/20</TXT>", body, count=1)


def hook_load(fake, slot, nth, change):
    """Changes the nth Load result of slot (as the printer numbers it) with change(body)."""
    seen = itertools.count(1)
    mark = b"<MSNO>%d</MSNO>" % slot

    def hook(body):
        if b"<TYPE>Load</TYPE>" in body and mark in body and next(seen) == nth:
            return change(body)
        return body
    fake.web_reply_hook = hook


def rising(start=100_000):
    """A PMCNT override that rises by one at every read."""
    numbers = itertools.count(start)
    return lambda: str(next(numbers))


def running_of(design):
    return RunningMessage(slot=design.slot, name=design.name, fingerprint=fingerprint(design),
                          origin_line=origin_from_design(design), read_at=design.read_at)


def wait_until(test, timeout_s=2.0):
    end = time.monotonic() + timeout_s
    while not test():
        if time.monotonic() > end:
            return False
        time.sleep(0.01)
    return True


# --------------------------------------------------------------------------
# Simulated PCs
# --------------------------------------------------------------------------

class PC:
    """One simulated PC: its identity, store, data, printer clients (own gates), time source and the
    three classes of hx_send."""

    def __init__(self, world, identity, timings):
        self.world = world
        self.identity = identity
        self.store = world.store(identity)
        self.local = hx_share.LocalStore(world.paths(identity))
        self.data = hx_share.DataService(store=self.store, local=self.local, bus=EventBus(), identity=identity,
                                         timings=timings)
        self.data.run_once()
        # The world's clock throughout (the real one, except in LeaseTests)
        self.clients = ClientPool(key=release_key(), clock=world.clock, gates=GateRegistry(), timings=timings)
        self.time = world.time_source()
        self.time.timeout_s = 0.2
        self.kw = dict(store=self.store, data=self.data, clients=self.clients, time=self.time, identity=identity,
                       clock=world.clock, timings=timings)
        self.pipeline = hx_send.SendPipeline(**self.kw)
        self.actions = hx_send.PrinterActions(**self.kw)

    def print_run(self, plan, **extra):
        return hx_send.TestPrintRun(plan=plan, **self.kw, **extra)

    def client(self, n=1):
        return self.clients.client(self.data.current.printers[self.world.ids[f"printer{n}"]])


class Base(unittest.TestCase):
    timings = FAST

    def world(self, printers=None, *, timings=None, clock=SYSTEM_CLOCK):
        tmp = tempfile.mkdtemp(prefix="hx_send_")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.w = hx_fake.FakeWorld(tmp, printers=printers or [{}], timings=timings or self.timings, clock=clock)
        self.addCleanup(self.w.close)
        self.addCleanup(self.check_whitelist, self.w)
        self._polls = {}
        return self.w

    def check_whitelist(self, world):
        # Across the whole file: only whitelisted writes, never SETPPARAM, never the printing slot
        for fake in world.printers:
            self.assertLessEqual(set(fake.writes), set(ALLOWED_WRITE_TAGS))
            self.assertNotIn("SETPPARAM", fake.writes)
            self.assertNotIn("SAVEMSG to the printing slot", fake.violations)
            self.assertLessEqual(fake.max_in_flight, 1)

    def pc(self, name="jsmith", **kw):
        return PC(self.w, self.w.identity(name, **kw), self.timings)

    def mid(self, n=1):
        return self.w.ids[f"machine{n}"]

    def own_poll(self, pc, *ns):
        """poll.json written by this PC itself, so its handshake answers "paused" at once."""
        for n in ns or range(1, len(self.w.printers) + 1):
            counter = self._polls.get(n, 0) + 1
            self._polls[n] = counter
            pc.store.write_poll(PollStatus(machine_id=self.mid(n), printer_id=self.w.ids[f"printer{n}"], poller="",
                                           counter=counter))

    def choice(self, machines=(1,), *, line="line", countries=("USA", "Canada"), lot="4417"):
        w = self.w
        return LineChoice(line_id=w.ids[line], machine_ids=[self.mid(n) for n in machines],
                          product_id=w.ids["product"], film_id=w.ids["film"], print_text_id=w.ids["print_text"],
                          lot=lot, countries=list(countries))

    def request(self, pc, *choices):
        return SendRequest(send_id=new_id("sd"), sender=pc.identity.user, computer=pc.identity.computer,
                           created_at=time.time(), lines=list(choices))

    def send(self, pc, machines=(1,), *, cancel=None, on_event=None, choices=None, **kw):
        """(result, plan, events) of one Send."""
        request = self.request(pc, *(choices or [self.choice(machines, **kw)]))
        plan = merge_plan(request, pc.data.current, pc.identity)
        events = []

        def post(event):
            events.append(event)
            if on_event is not None:
                on_event(event)
        return pc.pipeline.run(plan, post, cancel=cancel), plan, events

    def quiet(self):
        """Captures stderr, where hx_send reports what it couldn't write (and internal errors)."""
        out = io.StringIO()
        redirect = contextlib.redirect_stderr(out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        return out

    def assertClean(self, *fakes):
        for fake in fakes or self.w.printers:
            self.assertEqual(fake.violations, [])
            self.assertEqual(fake.max_in_flight, 1)

    def assertUntouched(self, *fakes):
        for fake in fakes or self.w.printers:
            self.assertEqual(fake.requests, [])
            self.assertEqual(fake.violations, [])

    def logs(self, pc, kind="hx-sends", entry_type="", ref=""):
        return [pc.store.read_log(kind, name) for name in pc.store.list_logs(kind, entry_type=entry_type, ref=ref)]

    def lock_files(self):
        return os.listdir(os.path.join(self.w.share_root, "status", "locks"))

    def add_line2(self):
        """Line 2 with machine 2 on it (before the PCs are made)."""
        w = self.w
        admin = w.store(w.identity("admin", admin=True, sender=False))
        line = Line(id=new_id("ln"), revision=1, name="Line 2", code="L2", cartridge="plastic",
                    notify_user="EXAMPLE\\akim")
        hx_share.write_record(admin, line)
        machine = from_json(Machine, admin.read_json(hx_share.record_rel_for(Machine, self.mid(2))))
        hx_share.write_record(admin, dataclasses.replace(machine, line_id=line.id, revision=2,
                                                         name="Line 2 Bagger 1"))
        admin.write_json("config/hx/changes.json", {"file_type": "changes", "counter": 2, "written_at": time.time(),
                                                    "written_by": "EXAMPLE\\admin", "last": None})
        w.ids["line2"] = line.id

    def hold_lock(self, pc, n=1, purpose="send"):
        lock = pc.store.lock(self.w.ids[f"printer{n}"], purpose=purpose, run_id=new_id("sd"),
                             line_id=self.w.ids["line"], machine_id=self.mid(n))
        self.assertEqual(lock.acquire(), "held")
        lock.start_keeping()
        self.addCleanup(lock.release)
        return lock


# --------------------------------------------------------------------------
# Sends
# --------------------------------------------------------------------------

class SendTests(Base):
    def test_delay(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake, mid = w.printers[0], self.mid()
        result, plan, events = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.colour, r.ok, r.step, r.msg, r.after_switch, r.notes), (AMBER, True, STEP_CONFIRM_2,
                                                                                    None, True, []))
        self.assertEqual((r.slot, r.message_name, r.content_code), (9, "EXCO1-PKO01-FDNP-H9", "FDNP"))
        self.assertEqual(r.origin_line, "Product of USA and Canada")
        self.assertEqual(fake.writes, WRITES)
        self.assertEqual(apcmds(fake)[1:7], GOLDEN_DELAY)
        self.assertTrue(apcmds(fake)[0].startswith("<TD><DAY>"))
        self.assertEqual(apcmds(fake)[7:], ["<PRINTMSG>9</PRINTMSG>", "<PRINT>1</PRINT>"])
        # The first slot 0 Load, then three read-backs: the idle slot, then the running message twice
        self.assertEqual(loads(fake), [0, 9, 0, 0])
        self.assertEqual([d.slot for d in r.readbacks], [9, 9, 9])
        self.assertEqual((fake.current_slot, fake.printing), (9, True))
        fake.print_bags(1)
        self.assertEqual(fake.printed[-1]["delay_us"], 480000)
        self.assertEqual(r.previous.slot, 1)
        self.assertEqual(r.counts_before, 100000)
        # The expected state: live, amber, waiting for the sender, with the fingerprint of slot 9
        slot9 = pc.client().load_slot(9)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.mode, e.result, e.confirmed, e.printing_expected, e.send_id, e.sender),
                         ("running", AMBER, False, True, result.send_id, "EXAMPLE\\jsmith"))
        self.assertEqual((e.fingerprint, e.message_name, e.slot, e.origin_line, e.failure),
                         (fingerprint(slot9), r.message_name, 9, "Product of USA and Canada", None))
        self.assertEqual(e.fingerprint, r.fingerprint)
        status = pc.store.read_line_status(w.ids["line"])
        self.assertEqual((status.send_id, status.machines, status.sender, status.lot, status.countries),
                         (result.send_id, {mid: AMBER}, "EXAMPLE\\jsmith", "4417", ["USA", "Canada"]))
        self.assertEqual((status.film_id, status.print_text_revision, status.confirmed), (w.ids["film"], 1, False))
        # The send log, with four pictures, and the switch entry
        self.assertFalse(result.log_queued)
        entry = pc.data.find_send(result.send_id)
        self.assertEqual((entry["type"], entry["sender"], entry["time_source"]), ("send", "EXAMPLE\\jsmith", "server"))
        row = entry["machines"][0]
        pictures = [row["result"]["previous_preview_b64"]] + [x["preview_b64"] for x in row["result"]["readbacks"]]
        self.assertEqual(len(pictures), 4)
        self.assertTrue(all(pictures))
        self.assertEqual(row["commands"], GOLDEN_DELAY)
        self.assertEqual((row["slot"], row["message_name"], row["expected_before"]),
                         (9, r.message_name, {"send_id": "", "counter": 0}))
        self.assertEqual(row["clock"]["source"], "server")
        self.assertIsNone(row["clock"]["before"])
        self.assertEqual(entry["lines"][0]["unticked"], [])
        back = hx_send.result_from_log(entry)
        self.assertEqual(back.machines[0].readbacks[2].preview_bmp, r.readbacks[2].preview_bmp)
        self.assertEqual(back.machines[0].previous, r.previous)
        self.assertEqual(hx_send.request_from_log(entry), plan.request)
        self.assertEqual(len(self.logs(pc, entry_type="send_switch", ref=result.send_id)), 1)
        # Events: the whole-Send ones, then each machine's in the step order, SendFinished last
        mine = [e for e in events if isinstance(e, SendEvent) and e.machine_id == mid]
        order = [STEPS.index(e.step) for e in mine]
        self.assertEqual(order, sorted(order))
        self.assertEqual([e.step for e in mine if e.status == "running"], list(STEPS[1:]))
        self.assertTrue(all(e.status in ("running", "ok") for e in mine))
        whole = [(e.step, e.status) for e in events if isinstance(e, SendEvent) and e.machine_id == ""]
        self.assertEqual(whole, [(STEP_CHECK_CHOICE, "running"), (STEP_CHECK_CHOICE, "ok"), (STEP_LOCK, "running"),
                                 (STEP_LOCK, "ok")])
        self.assertIsInstance(events[-1], SendFinished)
        self.assertIs(events[-1].result, result)
        self.assertEqual(self.lock_files(), [])
        self.assertClean(fake)

    def test_encoder(self):
        w = self.world([{"placement": "encoder"}])
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertTrue(r.ok)
        self.assertEqual(r.message_name, "EXCO1-PKO01-G79S-N9")
        self.assertTrue(apcmds(fake)[5].startswith("<CPPARM><ENCST>1</ENCST><DLYMM>348</DLYMM><PRNSPD>2400</PRNSPD>"))
        fake.print_bags(1)
        self.assertEqual(fake.printed[-1]["position_mm"], 348)
        self.assertEqual((queries(fake, "ENWHD"), queries(fake, "ENPPR")), (1, 1))
        self.assertClean(fake)

    def test_alternation(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        slots = []
        for _ in range(3):
            result, _, _ = self.send(pc)
            self.assertTrue(result.machines[0].ok)
            slots.append((result.machines[0].slot, fake.current_slot))
        self.assertEqual(slots, [(9, 9), (10, 10), (9, 9)])
        self.assertEqual([x for x in apcmds(fake) if x.startswith("<SAVEMSG>")],
                         ["<SAVEMSG>9</SAVEMSG>", "<SAVEMSG>10</SAVEMSG>", "<SAVEMSG>9</SAVEMSG>"])
        self.assertClean(fake)

    def test_partial(self):
        w = self.world([{}, {}])
        pc = self.pc()
        self.own_poll(pc)
        good, bad = w.printers
        bad.override("CTEXT", "3")
        result, plan, _ = self.send(pc, (1, 2))
        r1, r2 = result.machines
        self.assertEqual((r1.ok, r1.colour), (True, AMBER))
        self.assertEqual((r2.ok, r2.colour, r2.step, r2.after_switch), (False, RED, STEP_BUILD, False))
        self.assertEqual((bad.current_slot, bad.printing), (1, True))       # still printing its previous message
        self.assertEqual((good.current_slot, good.printing), (9, True))
        status = pc.store.read_line_status(w.ids["line"])
        self.assertEqual(status.machines, {self.mid(1): AMBER, self.mid(2): RED})
        rows = hx_send.failure_rows(result, plan)
        self.assertEqual([row.machine_id for row in rows], [self.mid(2)])
        self.assertEqual((rows[0].step, rows[0].step_text, rows[0].machine_name, rows[0].line_name),
                         (STEP_BUILD, STEP_TEXTS[STEP_BUILD], "Line 1 Bagger 2", "Line 1"))
        self.assertFalse(rows[0].after_switch)
        self.assertIsNone(rows[0].after_switch_text)
        self.assertEqual(pc.store.read_expected(self.mid(2)).failure.step, STEP_BUILD)
        self.assertClean()

    def test_each_failure(self):
        self.quiet()
        def switch_record(w, pc, fake):
            real = pc.store.write_expected
            failing = []

            def write(state):
                if failing or state.mode == "switch_pending":
                    failing.append(state)
                    raise ShareUnavailable(Msg("The share went away (test)."))
                return real(state)
            patcher = mock.patch.object(pc.store, "write_expected", write)
            patcher.start()
            self.addCleanup(patcher.stop)

        cases = [
            ("HXUI 1110", lambda w, pc, f: f.override("HXUI", "1110"), STEP_CHECK_MACHINE, False, [], "before"),
            ("no font file", lambda w, pc, f: f.files.__setitem__("/sfl.xml", None), STEP_CHECK_MACHINE, False, [],
             "before"),
            ("TD refused", lambda w, pc, f: f.override("TD", "3"), STEP_SET_CLOCK, False, WRITES[:1], "before"),
            ("CPPARM refused", lambda w, pc, f: f.override("CPPARM", "3"), STEP_BUILD, False, WRITES[:6], "before"),
            ("slot 9 TXT", lambda w, pc, f: hook_load(f, 9, 1, change_lot), STEP_VERIFY_IDLE, False, WRITES[:7],
             "before"),
            ("switch record", switch_record, STEP_SWITCH, False, WRITES[:7], "none"),
            ("PRINTMSG dropped", lambda w, pc, f: f.override("PRINTMSG", FakePrinter.DROP), STEP_SWITCH, True,
             WRITES[:8], "after"),
            ("PRIN 0", lambda w, pc, f: f.override("PRIN", lambda: "0" if "PRINT" in f.writes else None),
             STEP_CONFIRM_1, True, WRITES, "after"),
            ("second slot 0 TXT", lambda w, pc, f: hook_load(f, 9, 3, change_lot), STEP_CONFIRM_2, True, WRITES,
             "after"),
        ]
        for name, inject, step, after, writes, row in cases:
            with self.subTest(name):
                w = self.world()
                pc = self.pc()
                self.own_poll(pc)
                fake, mid = w.printers[0], self.mid()
                inject(w, pc, fake)
                result, plan, events = self.send(pc)
                r = result.machines[0]
                self.assertEqual((r.ok, r.colour, r.step, r.after_switch), (False, RED, step, after))
                self.assertEqual(fake.writes, writes)       # nothing written after the failure
                e = w.store(w.identity("observer")).read_expected(mid)
                if row == "none":
                    self.assertIsNone(e)
                elif row == "before":
                    self.assertEqual((e.mode, e.send_id, e.sender, e.result), ("none", result.send_id,
                                                                               "EXAMPLE\\jsmith", RED))
                    self.assertEqual((e.failure.step, e.failure.after_switch, e.rejected), (step, False, False))
                    self.assertEqual(e.failure.msg, r.msg)
                else:
                    self.assertEqual((e.mode, e.send_id, e.result, e.message_name), ("switch_pending", result.send_id,
                                                                                    RED, r.message_name))
                    self.assertEqual((e.failure.step, e.failure.after_switch), (step, True))
                if not after:
                    self.assertEqual(fake.current_slot, 1)
                failed = [ev for ev in events if isinstance(ev, SendEvent) and ev.status == "failed"]
                self.assertEqual([(ev.machine_id, ev.step) for ev in failed], [(mid, step)])
                rows = hx_send.failure_rows(result, plan)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].after_switch_text is not None, after)
                # The red window names the machine and line, never their ids
                shown = [r.msg.english(), spanish(r.msg), rows[0].machine_name, rows[0].line_name]
                self.assertEqual((rows[0].machine_name, rows[0].line_name), ("Line 1 Bagger 1", "Line 1"))
                self.assertEqual([t for t in shown if ID_TEXT.search(t)], [])
                self.assertEqual(self.lock_files(), [])
                self.assertClean(fake)

    def test_lock_held(self):
        w = self.world()
        akim, pc = self.pc("akim"), self.pc()
        self.hold_lock(akim)
        result, plan, events = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.step), (False, RED, STEP_LOCK))
        self.assertEqual(r.msg.english(), "Line 1 is being sent by EXAMPLE\\akim.")
        self.assertIsNone(pc.store.read_expected(self.mid()))
        self.assertIsNone(pc.store.read_line_status(w.ids["line"]))      # nothing happened at its printers
        self.assertEqual(len(hx_send.failure_rows(result, plan)), 1)
        self.assertUntouched()

    def test_lock_held_messages(self):
        self.world()
        akim, pc = self.pc("akim"), self.pc()
        lock = self.hold_lock(akim, purpose="test")
        result, _, _ = self.send(pc)
        self.assertEqual(result.machines[0].msg.english(), "Line 1 Bagger 1 is running a test print by EXAMPLE\\akim.")
        lock.release()
        self.hold_lock(akim, purpose="clock")
        result, _, _ = self.send(pc)
        self.assertEqual(result.machines[0].msg.english(), "Line 1 Bagger 1 is being changed by EXAMPLE\\akim.")
        self.assertUntouched()

    def _polling(self, poller, *, give_after_s):
        """A poller on another PC: poll.json with a rising counter; it writes the lock's token after
        give_after_s (never when None). Returns (stop, when it wrote the token)."""
        mid, pid = self.mid(), self.w.ids["printer1"]
        stop, paused = threading.Event(), {}

        def run():
            started, n = time.monotonic(), 0
            while not stop.is_set():
                n += 1
                token = ""
                if give_after_s is not None and time.monotonic() - started >= give_after_s:
                    info = poller.store.read_lock(pid)
                    token = info.token if info is not None else ""
                poller.store.write_poll(PollStatus(machine_id=mid, printer_id=pid, poller="", counter=n,
                                                   paused_for_lock=token))
                if token and "at" not in paused:
                    paused["at"] = time.monotonic()
                stop.wait(0.03)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def stopping():
            stop.set()
            thread.join()
        self.addCleanup(stopping)
        return stopping, paused

    def test_poller_steps_aside(self):
        w = self.world()
        pc, poller = self.pc(), self.pc("akim")
        fake = w.printers[0]
        stopping, paused = self._polling(poller, give_after_s=0.25)
        time.sleep(0.05)
        result, _, _ = self.send(pc)
        stopping()
        self.assertTrue(result.machines[0].ok)
        self.assertIn("at", paused)
        self.assertGreaterEqual(fake.requests[0]["t"], paused["at"])    # the first request came after
        self.assertClean(fake)

    def test_poller_never_steps_aside(self):
        self.world()
        pc, poller = self.pc(), self.pc("akim")
        stopping, _ = self._polling(poller, give_after_s=None)
        time.sleep(0.05)
        started = time.monotonic()
        result, _, _ = self.send(pc)
        stopping()
        r = result.machines[0]
        self.assertEqual((r.ok, r.step), (False, STEP_WAIT_POLLER))
        self.assertEqual(r.msg.english(), "Waited 1 s for the PC polling Line 1 Bagger 1 to step aside. Try again.")
        self.assertGreaterEqual(time.monotonic() - started, 2 * FAST.stale_s)
        self.assertEqual(pc.store.read_expected(self.mid()).failure.step, STEP_WAIT_POLLER)
        self.assertUntouched()

    def test_share_down(self):
        self.quiet()
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        pc.store.fs.down = True
        pc.data.current = pc.local.read_local_copy()      # the local copy, as when the share went away
        self.assertEqual(pc.data.current.source, "local")
        result, _, events = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.notes), (True, AMBER, ["no_lock", "local_copy"]))
        self.assertTrue(result.log_queued)
        self.assertTrue(result.from_local_copy)
        self.assertIn(SendEvent(send_id=result.send_id, machine_id=self.mid(), step=STEP_LOCK, status="warning",
                                msg=NOTE_TEXTS["no_lock"], at=0.0),
                      [dataclasses.replace(e, at=0.0) for e in events if isinstance(e, SendEvent)])
        self.assertEqual(fake.writes, WRITES)
        other = w.store(w.identity("observer"))
        self.assertIsNone(other.read_expected(self.mid()))      # nothing written on the share
        self.assertEqual(pc.local.queued_logs()[-1][2]["type"], "send")
        self.assertClean(fake)

    def test_pc_time(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        w.sntp.silent = True
        fake = w.printers[0]
        result, _, events = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.notes, result.time_source), (True, AMBER, ["pc_time"], "pc"))
        warnings = [(e.machine_id, e.step, e.msg) for e in events if isinstance(e, SendEvent) and e.status == "warning"]
        self.assertIn(("", STEP_SET_CLOCK, NOTE_TEXTS["pc_time"]), warnings)
        self.assertIn((self.mid(), STEP_SET_CLOCK, NOTE_TEXTS["pc_time"]), warnings)
        self.assertEqual(fake.writes, WRITES)           # the TD is still sent, from the PC's time
        self.assertEqual(pc.store.read_expected(self.mid()).notes, ["pc_time"])
        self.assertClean(fake)

    def test_cancel(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        cancel = CancelToken()

        def on(event):
            if isinstance(event, SendEvent) and event.machine_id == "" and event.step == STEP_LOCK:
                cancel.cancel()
        result, plan, events = self.send(pc, cancel=cancel, on_event=on)
        r = result.machines[0]
        self.assertTrue(hx_send.is_cancelled(result))
        self.assertEqual((r.ok, r.step, r.msg, r.notes), (False, STEP_LOCK, NOTE_TEXTS["cancelled"], ["cancelled"]))
        self.assertEqual(hx_send.failure_rows(result, plan), [])
        self.assertIn(("skipped", STEP_LOCK), [(e.status, e.step) for e in events
                                               if isinstance(e, SendEvent) and e.machine_id == self.mid()])
        self.assertIsInstance(events[-1], SendFinished)
        self.assertEqual(self.lock_files(), [])
        self.assertEqual(pc.store.list_logs("hx-sends"), [])
        self.assertEqual(pc.local.queued_logs(), [])
        self.assertIsNone(pc.store.read_expected(self.mid()))
        self.assertIsNone(pc.store.read_line_status(w.ids["line"]))
        self.assertUntouched()

    def test_templates_changed(self):
        w = self.world([{"placement": "encoder"}])
        pc = self.pc()
        self.own_poll(pc)
        plan = merge_plan(self.request(pc, self.choice()), pc.data.current, pc.identity)
        current = pc.data.current
        film = current.films[w.ids["film"]]
        pc.data.current = dataclasses.replace(current, films={film.id: dataclasses.replace(film, position_mm=160.0)})
        events = []
        with self.assertRaises(PlanError) as cm:
            pc.pipeline.run(plan, events.append)
        self.assertEqual(cm.exception.msg.english(),
                         "The templates changed since you checked them. Look at the Send window again.")
        self.assertEqual([(e.step, e.status) for e in events], [(STEP_CHECK_CHOICE, "running")])
        self.assertEqual(self.lock_files(), [])
        self.assertEqual(pc.store.list_logs("hx-sends"), [])
        self.assertIsNone(pc.store.read_expected(self.mid()))
        self.assertUntouched()

    def test_not_a_sender(self):
        self.world()
        pc, nobody = self.pc(), self.pc("nobody", sender=False)
        plan = merge_plan(self.request(pc, self.choice()), pc.data.current, pc.identity)
        events = []
        with self.assertRaises(NotAllowed):
            nobody.pipeline.run(plan, events.append)
        self.assertEqual(events, [])
        self.assertEqual(self.lock_files(), [])
        self.assertEqual(pc.store.list_logs("hx-sends"), [])
        self.assertUntouched()

    def test_format_too_new(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        newer = os.path.join(w.share_root, "status", "machines", f"{self.mid()}.expected.json")
        with open(newer, "w", encoding="utf-8") as f:
            f.write('{"format_version": 99, "file_type": "expected", "machine_id": "%s", "counter": 3}' % self.mid())
        result, plan, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step), (False, STEP_LOCK))
        self.assertTrue(r.msg.english().startswith("Update Stripes: "), r.msg.english())
        self.assertEqual(self.lock_files(), [])
        self.assertUntouched()

    def test_internal_error(self):
        err = self.quiet()
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        with mock.patch.object(hx_send, "_readback_problem", side_effect=ZeroDivisionError("Product of Nowhere")):
            result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step), (False, STEP_VERIFY_IDLE))
        self.assertEqual(r.msg.english(), "Stripes had an internal error at this step: ZeroDivisionError")
        self.assertEqual(fake.writes, WRITES[:7])
        errors = self.logs(pc, "hx-events", entry_type="internal_error", ref=result.send_id)
        self.assertEqual([(x["where"], x["machine_id"], x["step"], x["error"]) for x in errors],
                         [("send", self.mid(), STEP_VERIFY_IDLE, "ZeroDivisionError")])
        self.assertIn("ZeroDivisionError", errors[0]["traceback"])
        self.assertIn("Traceback", err.getvalue())      # printed with its traceback
        self.assertEqual(pc.store.read_expected(self.mid()).failure.step, STEP_VERIFY_IDLE)
        self.assertEqual(self.lock_files(), [])
        self.assertClean(fake)

    def test_ink(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        fake.ink_name = "Example White"
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step, r.notes), (False, STEP_CHECK_MACHINE, ["ink_mismatch"]))
        self.assertEqual(r.msg.english(), "Line 1 Bagger 1 is set for white ink; this film needs black.")
        self.assertEqual(fake.writes, [])
        self.assertClean(fake)

    def test_ink_not_listed(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        w.printers[0].ink_name = "Mystery Ink"
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step, r.notes), (False, STEP_CHECK_MACHINE, []))
        self.assertEqual(r.msg.english(), "Line 1 Bagger 1 reports ink \"Mystery Ink\", which isn't on Stripes' ink list.")
        self.assertClean()

    def test_fonts(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        with fake.lock:
            fake.fonts = tuple("Arial2" if f == "Arial" else f for f in STANDARD_FONTS)
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step), (False, STEP_CHECK_MACHINE))
        self.assertEqual(r.msg.english(), "Line 1 Bagger 1's printer has fonts that differ from Stripes' standard list.")
        self.assertEqual(fake.writes, [])
        self.assertClean(fake)

    def test_encoder_settings_changed(self):
        w = self.world([{"placement": "encoder"}])
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        fake.encoder["ENWHD"] = 57
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step), (False, STEP_CHECK_MACHINE))
        self.assertTrue(r.msg.english().startswith("Line 1 Bagger 1's encoder settings changed: wheel 57 mm, 5000 "))
        self.assertEqual(fake.writes, [])
        self.assertClean(fake)

    def test_clock(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        fake.override("TIME", lambda: (fake.printer_clock() + timedelta(minutes=5)).strftime("%H:%M:%S"))
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step, r.after_switch), (False, STEP_SET_CLOCK, False))
        self.assertTrue(r.msg.english().startswith("Line 1 Bagger 1's clock reads "), r.msg.english())
        self.assertEqual(queries(fake, "TIME"), 2)      # one re-read
        self.assertEqual(fake.writes, ["TD"])
        entry = pc.data.find_send(result.send_id)
        self.assertEqual(entry["machines"][0]["clock"]["source"], "server")
        self.assertClean(fake)

    def test_dates(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        hook_load(fake, 9, 1, change_date)
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step, r.after_switch), (False, STEP_VERIFY_IDLE, False))
        self.assertEqual(r.msg.english(), "The date Line 1 Bagger 1 would print differs from the date Stripes expects.")
        differences = pc.data.find_send(result.send_id)["machines"][0]["differences"]
        self.assertEqual([(d[0], d[1], d[3]) for d in differences], [("item 1 (CTDAT)", "TXT", "01/01/20")])
        self.assertEqual(fake.current_slot, 1)
        self.assertClean(fake)

    def test_switch_pending(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake, mid = w.printers[0], self.mid()
        observer = w.store(w.identity("observer"))
        seen = []
        fake.override("PRINTMSG", lambda: seen.append(observer.read_expected(mid)))   # then behaves normally
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertTrue(r.ok)
        pending = seen[0]
        self.assertEqual((pending.mode, pending.send_id, pending.sender, pending.slot, pending.message_name),
                         ("switch_pending", result.send_id, "EXAMPLE\\jsmith", 9, r.message_name))
        self.assertEqual(pending.fingerprint, fingerprint(r.readbacks[0]))
        self.assertEqual((pending.previous_message_name, pending.previous_fingerprint), ("", r.previous.fingerprint))
        self.assertEqual((pending.origin_line, pending.content_code, pending.failure), ("Product of USA and Canada",
                                                                                      "FDNP", None))
        e = pc.store.read_expected(mid)
        self.assertEqual((e.mode, e.fingerprint), ("running", pending.fingerprint))
        self.assertClean(fake)

    def test_switch_record_fails(self):
        self.quiet()
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        real = pc.store.write_expected
        failing = []

        def write(state):
            if failing or state.mode == "switch_pending":
                failing.append(state.mode)
                raise ShareUnavailable(Msg("The share went away (test)."))
            return real(state)
        with mock.patch.object(pc.store, "write_expected", write):
            result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.step, r.after_switch), (False, STEP_SWITCH, False))
        self.assertEqual(r.msg.english(), "Stripes couldn't record the switch on the server, so it didn't switch.")
        self.assertNotIn("PRINTMSG", fake.writes)
        self.assertEqual(failing[0], "switch_pending")
        self.assertIsNone(pc.store.read_expected(self.mid()))
        self.assertEqual(self.logs(pc, entry_type="send_switch", ref=result.send_id), [])
        self.assertEqual(fake.current_slot, 1)
        self.assertClean(fake)

    def test_cancel_with_a_held_line(self):
        w = self.world([{}, {}])
        self.add_line2()
        akim, pc = self.pc("akim"), self.pc()
        self.own_poll(pc)
        self.hold_lock(akim, 1)
        cancel = CancelToken()

        def on(event):
            if isinstance(event, SendEvent) and event.machine_id == "" and event.step == STEP_LOCK:
                cancel.cancel()
        result, plan, _ = self.send(pc, cancel=cancel, on_event=on,
                                    choices=[self.choice((1,)), self.choice((2,), line="line2")])
        r1, r2 = result.machines
        self.assertTrue(hx_send.is_cancelled(result))
        self.assertEqual((r1.step, r1.msg.english(), r1.notes), (STEP_LOCK, "Line 1 is being sent by EXAMPLE\\akim.",
                                                                 ["cancelled"]))
        self.assertEqual((r2.step, r2.msg, r2.notes), (STEP_LOCK, NOTE_TEXTS["cancelled"], ["cancelled"]))
        self.assertEqual(hx_send.failure_rows(result, plan), [])
        self.assertEqual(self.lock_files(), [f"{w.ids['printer1']}.lock"])       # only akim's
        self.assertUntouched()

    def test_unticked(self):
        w = self.world([{}, {}])
        pc, poller = self.pc(), self.pc("akim")
        self.own_poll(pc, 1)
        running = RunningMessage(slot=1, name="", fingerprint="f" * 64, origin_line="Product of Mexico",
                                 read_at=time.time())
        poller.store.write_poll(PollStatus(machine_id=self.mid(2), printer_id=w.ids["printer2"], poller="", counter=1,
                                           printing=True, running=running))
        result, _, _ = self.send(pc, (1,))
        self.assertTrue(result.machines[0].ok)
        entry = pc.data.find_send(result.send_id)
        self.assertEqual(entry["lines"][0]["unticked"],
                         [{"machine_id": self.mid(2), "origin_line": "Product of Mexico", "printing": True}])
        self.assertClean(w.printers[0])
        self.assertEqual(w.printers[1].requests, [])

    def test_previous_confirmed(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed):
                w = self.world()
                pc = self.pc()
                self.own_poll(pc)
                fake = w.printers[0]
                first, _, _ = self.send(pc)
                if confirmed:
                    pc.pipeline.confirm(first.send_id, [self.mid()], True)
                hook_load(fake, 10, 1, change_lot)
                result, plan, _ = self.send(pc)
                r = result.machines[0]
                self.assertEqual((r.ok, r.step), (False, STEP_VERIFY_IDLE))
                self.assertEqual(r.previous_confirmed, confirmed)
                self.assertEqual((r.previous.name, r.previous.origin_line),
                                 (first.machines[0].message_name, "Product of USA and Canada"))
                row = hx_send.failure_rows(result, plan)[0]
                self.assertEqual((row.previous_confirmed, row.previous_name, row.previous_origin),
                                 (confirmed, first.machines[0].message_name, "Product of USA and Canada"))
                self.assertTrue(row.previous_preview)
                # The old expectation stays, with this Send's failure added
                e = pc.store.read_expected(self.mid())
                self.assertEqual((e.mode, e.message_name, e.confirmed, e.send_id, e.failure.step),
                                 ("running", first.machines[0].message_name, confirmed, result.send_id,
                                  STEP_VERIFY_IDLE))
                self.assertClean(fake)

    def test_lock_budget(self):
        self.quiet()
        # Scaled as a whole (the keeper's refresh and the share timeout within lock_stale_s / 2), and twice as
        # long on a Windows runner, whose clock ticks every 15.6 ms and whose file writes are slower
        windows = 2.0 if sys.platform == "win32" else 1.0
        budget = dataclasses.replace(FAST, http_timeout_s=0.1, web_repeat_window_s=0.1, lock_stale_s=1.6,
                                     share_timeout_s=0.25).scaled(windows)
        self.timings = budget
        w = self.world([{"latency_s": 0.03 * windows}], timings=budget)
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        marks = {}
        refreshed = []
        real_write = pc.store.write_bytes

        def write_bytes(rel, data):
            real_write(rel, data)
            if rel.startswith("status/locks/"):
                refreshed.append(time.monotonic())      # a good refresh by the lock's keeper
        pc.store.write_bytes = write_bytes

        def on(event):
            if isinstance(event, SendEvent) and event.step == STEP_CHECK_MACHINE and event.status == "running":
                # Right after a good refresh, so the lease has its whole length left: at any other moment in the
                # keeper's cycle a slow share (a Windows runner) can leave too little of it for even the first
                # request, and the Send rightly asks nothing more
                n = len(refreshed)
                marks["refreshed"] = wait_until(lambda: len(refreshed) > n,
                                                2 * (budget.lock_refresh_s + budget.share_timeout_s))
                pc.store.fs.fail_replace = 10 ** 6      # the keeper's refreshes fail from here
                marks["fail"] = time.monotonic()
            if isinstance(event, SendEvent) and event.status == "failed":
                marks["lost"] = time.monotonic()
        result, _, _ = self.send(pc, on_event=on)
        r = result.machines[0]
        self.assertTrue(marks["refreshed"])
        self.assertFalse(r.ok)
        self.assertEqual(r.msg.english(), LOCK_LOST)
        self.assertLess(marks["lost"] - marks["fail"], budget.lock_stale_s / 2)
        times = [req["t"] for req in fake.requests]
        self.assertTrue(any(t > marks["fail"] for t in times))      # it carried on while it could
        self.assertTrue(all(t < marks["lost"] for t in times))      # and asked nothing after
        self.assertClean(fake)

    def test_offline_then_back(self):
        self.quiet()
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake, mid = w.printers[0], self.mid()
        first, _, _ = self.send(pc)                     # so this PC knows the machine's expected state
        before = pc.store.read_expected(mid)
        pc.store.fs.down = True
        result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.notes), (True, AMBER, ["no_lock"]))
        self.assertTrue(result.log_queued)
        pc.store.fs.down = False
        pc.store.probe_now()
        self.assertTrue(wait_until(lambda: pc.store.available))
        self.assertEqual(pc.store.read_expected(mid).counter, before.counter)     # untouched meanwhile
        entry = pc.data.find_send(result.send_id)
        self.assertEqual(entry["machines"][0]["expected_before"], {"send_id": first.send_id, "counter": before.counter})
        self.assertEqual(pc.pipeline.record_offline(entry), [mid])
        e = pc.store.read_expected(mid)
        self.assertEqual((e.mode, e.result, e.notes, e.send_id, e.sender, e.confirmed),
                         ("running", AMBER, ["no_lock"], result.send_id, "EXAMPLE\\jsmith", False))
        self.assertEqual((e.fingerprint, e.message_name, e.slot), (r.fingerprint, r.message_name, r.slot))
        # What the poller would read matches it: unconfirmed, not drift
        running = running_of(pc.client().load_slot(0))
        self.assertEqual((running.name, running.slot, running.fingerprint), (e.message_name, e.slot, e.fingerprint))
        self.assertEqual(pc.pipeline.pending_confirmation(result.send_id), [mid])
        # Another Send's expected state written meanwhile: nothing is written
        pc.store.fs.down = True
        third, _, _ = self.send(pc)
        pc.store.fs.down = False
        pc.store.probe_now()
        self.assertTrue(wait_until(lambda: pc.store.available))
        akim = self.pc("akim")
        lock = self.hold_lock(akim)
        akim.store.write_expected(dataclasses.replace(akim.store.read_expected(mid), send_id=new_id("sd")))
        lock.release()
        later = akim.store.read_expected(mid)
        self.assertEqual(pc.pipeline.record_offline(pc.data.find_send(third.send_id)), [])
        self.assertEqual(pc.store.read_expected(mid).counter, later.counter)
        self.assertClean(fake)

    def test_a_store_marked_down_by_one_slow_op_still_takes_its_locks(self):
        # The flake fix plan's product gap: one share operation slower than share_timeout_s (say the last
        # Send's log entry) marks the store down for share_retry_s. The next Send's lock step then took a
        # local_only lock (amber no_lock) without trying the share, although it answered, and the other
        # PCs' pollers, seeing no lock, kept polling during the Send. The lock step now tries once
        self.quiet()
        w = self.world()
        pc, akim = self.pc(), self.pc("akim")
        self.own_poll(pc)
        pid, mid = w.ids["printer1"], self.mid()
        pc.store._mark_down(Msg("The server didn't answer within {seconds} s.", {"seconds": 1}))
        self.assertFalse(pc.store.available)
        seen = []

        def on_event(e):
            if isinstance(e, SendEvent) and e.step == STEP_LOCK and e.machine_id == mid and e.status != "running":
                seen.append((e.status, akim.store.read_lock(pid)))
        result, _, _ = self.send(pc, on_event=on_event)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.notes), (True, AMBER, []))      # as test_delay: no no_lock
        self.assertEqual([(status, lock.user) for status, lock in seen], [("ok", pc.identity.user)])
        self.assertTrue(pc.store.available)
        self.assertEqual(akim.store.read_expected(mid).send_id, result.send_id)
        self.assertEqual(self.lock_files(), [])
        # A real outage: the one try fails within share_timeout_s, and the Send carries on with a
        # local_only lock, amber no_lock, as before
        pc.store.fs.down = True
        pc.store._mark_down(Msg("Stripes can't reach the server."))
        seen.clear()
        first = result.send_id
        result, _, _ = self.send(pc, on_event=on_event)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.notes), (True, AMBER, ["no_lock"]))
        self.assertEqual([status for status, _ in seen], ["warning"])
        self.assertIsNone(seen[0][1])                   # no lock on the share
        self.assertTrue(result.log_queued)
        self.assertEqual(akim.store.read_expected(mid).send_id, first)     # nothing written meanwhile
        pc.store.fs.down = False
        self.assertClean()

    def test_record_offline_skipped_machine_raises(self):
        # Review finding: a machine skipped for now (LockHeld, or a local_only lock) returned quietly,
        # so the Navigator dropped the Send and never recorded it (ui.md §15.3 keeps a send_id only when
        # record_offline raises). It raises after the other machines are done, and a later try records it
        self.quiet()
        w = self.world([{}, {}])
        pc = self.pc()
        self.own_poll(pc)
        m1, m2 = self.mid(1), self.mid(2)
        self.send(pc, (1, 2))                           # so this PC knows both expected states
        before = {mid: pc.store.read_expected(mid).counter for mid in (m1, m2)}

        def share(up):
            pc.store.fs.down = not up
            if up:
                pc.store.probe_now()
                self.assertTrue(wait_until(lambda: pc.store.available))
        share(False)
        result, _, _ = self.send(pc, (1, 2))
        self.assertEqual([r.notes for r in result.machines], [["no_lock"], ["no_lock"]])
        share(True)
        entry = pc.data.find_send(result.send_id)
        # akim is working on Bagger 1's printer right now: Bagger 2 is recorded, then LockHeld is raised
        lock = self.hold_lock(self.pc("akim"), 1)
        with self.assertRaises(LockHeld):
            pc.pipeline.record_offline(entry)
        lock.release()
        self.assertEqual(pc.store.read_expected(m1).counter, before[m1])
        e2 = pc.store.read_expected(m2)
        self.assertEqual((e2.mode, e2.result, e2.send_id, e2.notes), ("running", AMBER, result.send_id, ["no_lock"]))
        # The share gone again as it is tried: every lock is local_only, nothing is written, and it raises
        share(False)
        with self.assertRaises(ShareUnavailable):
            pc.pipeline.record_offline(entry)
        share(True)
        self.assertEqual(pc.store.read_expected(m1).counter, before[m1])
        # Next time: Bagger 1 is recorded; Bagger 2 is already this Send's and stays as it is
        self.assertEqual(pc.pipeline.record_offline(entry), [m1])
        e1 = pc.store.read_expected(m1)
        self.assertEqual((e1.mode, e1.result, e1.send_id, e1.notes), ("running", AMBER, result.send_id, ["no_lock"]))
        self.assertEqual(pc.store.read_expected(m2).counter, e2.counter)
        self.assertEqual(pc.pipeline.pending_confirmation(result.send_id), [m1, m2])
        self.assertEqual(self.lock_files(), [])
        self.assertClean()

    def test_record_offline_own_switch_pending(self):
        self.quiet()
        # The share went away after "switch pending" was written: that record is this Send's own
        self.world()
        pc = self.pc()
        self.own_poll(pc)
        mid = self.mid()
        real = pc.store.write_expected

        def write(state):
            if state.mode == "running":
                raise ShareUnavailable(Msg("The share went away (test)."))
            return real(state)
        with mock.patch.object(pc.store, "write_expected", write):
            result, _, _ = self.send(pc)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.notes), (True, AMBER, ["no_lock"]))
        self.assertEqual(pc.store.read_expected(mid).mode, "switch_pending")
        self.assertEqual(pc.pipeline.record_offline(pc.data.find_send(result.send_id)), [mid])
        e = pc.store.read_expected(mid)
        self.assertEqual((e.mode, e.fingerprint, e.result), ("running", r.fingerprint, AMBER))
        self.assertClean()


# --------------------------------------------------------------------------
# The sender's answer
# --------------------------------------------------------------------------

class ConfirmTests(Base):
    def live(self, printers=None, machines=(1,)):
        w = self.world(printers)
        pc = self.pc()
        self.own_poll(pc)
        result, plan, _ = self.send(pc, machines)
        self.assertTrue(all(r.ok for r in result.machines))
        return w, pc, result

    def test_right(self):
        w, pc, result = self.live()
        mid = self.mid()
        pc.pipeline.confirm(result.send_id, [mid], True)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.confirmed, e.confirmed_by, e.result, e.rejected), (True, "EXAMPLE\\jsmith", GREEN, False))
        self.assertIsNotNone(e.confirmed_at)
        entries = self.logs(pc, entry_type="send_confirm", ref=result.send_id)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["everything_right"], entries[0]["machine_ids"], entries[0]["sender"],
                          entries[0]["logged_by"]), (True, [mid], "EXAMPLE\\jsmith", "EXAMPLE\\jsmith"))
        self.assertEqual(entries[0]["machines"], [{"machine_id": mid, "message_name": result.machines[0].message_name,
                                                   "fingerprint": result.machines[0].fingerprint}])
        status = pc.store.read_line_status(w.ids["line"])
        self.assertEqual((status.confirmed, status.confirmed_by, status.machines), (True, "EXAMPLE\\jsmith",
                                                                                   {mid: GREEN}))
        self.assertEqual(self.lock_files(), [])
        self.assertClean()

    def test_pending(self):
        w, pc, result = self.live([{}, {}], (1, 2))
        self.assertEqual(pc.pipeline.pending_confirmation(result.send_id), [self.mid(1), self.mid(2)])
        pc.pipeline.confirm(result.send_id, [self.mid(1)], True)
        self.assertEqual(pc.pipeline.pending_confirmation(result.send_id), [self.mid(2)])
        self.assertFalse(pc.store.read_line_status(w.ids["line"]).confirmed)    # not every machine yet
        pc.pipeline.confirm(result.send_id, [self.mid(2)], True)
        self.assertEqual(pc.pipeline.pending_confirmation(result.send_id), [])
        self.assertTrue(pc.store.read_line_status(w.ids["line"]).confirmed)
        self.assertClean()

    def remove_logs(self, entry_type, send_id):
        for name in self.logs_names(entry_type, send_id):
            os.remove(os.path.join(self.w.share_root, "logs", "hx-sends", hx_share.log_month(name), name))

    def logs_names(self, entry_type, send_id):
        store = self.w.store(self.w.identity("observer"))
        return store.list_logs("hx-sends", entry_type=entry_type, ref=send_id)

    def test_no_send_entry(self):
        w, pc, result = self.live([{}, {}])
        mid, sid = self.mid(1), result.send_id
        self.remove_logs("send", sid)               # as after a crash: no main entry anywhere
        self.assertIsNone(pc.data.find_send(sid))
        # Bagger 2 wasn't in the Send, but its expected state is made to look pending for it. With the
        # send_switch entry there, only that entry's machine is a candidate, so Bagger 2 isn't listed
        # (review finding: with two machines and only Bagger 1 pending, the every-active-machine
        # fallback gave the same answer, so the send_switch lookup went untested)
        mid2 = self.mid(2)
        lock = self.hold_lock(pc, 2)
        pc.store.write_expected(ExpectedState(machine_id=mid2, mode="running", printing_expected=True, send_id=sid,
                                              sender="EXAMPLE\\jsmith", result=AMBER))
        lock.release()
        self.assertEqual([self.logs(pc, entry_type="send_switch", ref=sid)[0]["machine_id"]], [mid])
        self.assertEqual(pc.pipeline.pending_confirmation(sid), [mid])
        akim = self.pc("akim")
        with self.assertRaises(NotAllowed) as cm:
            akim.pipeline.confirm(sid, [mid], True)
        self.assertEqual(cm.exception.msg.english(), "Only EXAMPLE\\jsmith can confirm this Send.")
        # Without the switch entry either: every active machine is a candidate (Bagger 2 too), and a
        # second machine whose expected state a newer Stripes wrote is skipped, not raised
        self.remove_logs("send_switch", sid)
        self.assertEqual(pc.pipeline.pending_confirmation(sid), [mid, mid2])
        newer = os.path.join(w.share_root, "status", "machines", f"{mid2}.expected.json")
        with open(newer, "w", encoding="utf-8") as f:
            f.write('{"format_version": 99, "file_type": "expected", "machine_id": "%s", "counter": 3}' % self.mid(2))
        self.assertEqual(pc.pipeline.pending_confirmation(sid), [mid])
        pc.pipeline.confirm(sid, [mid, self.mid(2)], True)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.confirmed, e.result), (True, GREEN))
        entry = self.logs(pc, entry_type="send_confirm", ref=sid)[0]
        self.assertEqual((entry["sender"], entry["machine_ids"]), ("EXAMPLE\\jsmith", [mid]))
        self.assertEqual(entry["machines"], [{"machine_id": mid, "message_name": e.message_name,
                                              "fingerprint": e.fingerprint}])
        self.assertTrue(pc.store.read_line_status(w.ids["line"]).confirmed)
        self.assertClean(w.printers[0])

    def test_nothing_to_find(self):
        w, pc, result = self.live()
        with self.assertRaises(ShareError) as cm:
            pc.pipeline.confirm(new_id("sd"), [self.mid()], True)
        self.assertEqual(cm.exception.msg.english(), "Stripes can't find this Send yet. Try again in a minute.")
        self.assertClean()

    def test_sender_kept(self):
        w, pc, result = self.live()
        mid = self.mid()
        akim = self.pc("akim")
        actions = akim.actions.set_clocks([mid], lambda e: None)
        self.assertTrue(actions.machines[0].ok)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.sender, e.written_by, e.confirmed), ("EXAMPLE\\jsmith", "EXAMPLE\\akim", False))
        pc.pipeline.confirm(result.send_id, [mid], True)
        self.assertTrue(pc.store.read_expected(mid).confirmed)
        self.assertClean()

    def test_someone_else(self):
        w, pc, result = self.live()
        mid = self.mid()
        before = pc.store.read_expected(mid)
        with self.assertRaises(NotAllowed):
            self.pc("akim").pipeline.confirm(result.send_id, [mid], True)
        self.assertEqual(pc.store.read_expected(mid).counter, before.counter)
        self.assertEqual(self.logs(pc, entry_type="send_confirm"), [])
        self.assertClean()

    def test_wrong_then_stop(self):
        w, pc, result = self.live()
        mid, sid = self.mid(), result.send_id
        fake = w.printers[0]
        pc.pipeline.confirm(sid, [mid], False)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.rejected, e.resume_blocked, e.result, e.failure, e.mode), (True, True, RED, None,
                                                                                      "running"))
        self.assertFalse(pc.store.read_line_status(w.ids["line"]).confirmed)
        prin_before = queries(fake, "PRIN")
        events = []
        stopped = pc.actions.stop([mid], events.append, reason="rejected", send_id=sid)
        r = stopped.machines[0]
        self.assertEqual((r.ok, r.colour, r.step), (True, GREEN, hx_send.ACTION_STOP))
        self.assertEqual(apcmds(fake)[-1], "<PRINT>0</PRINT>")
        self.assertEqual(queries(fake, "PRIN") - prin_before, 2)        # read twice to confirm it's off
        self.assertFalse(fake.printing)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.mode, e.stop_reason, e.stopped_by, e.rejected, e.resume_blocked, e.printing_expected),
                         ("stopped_by_user", "rejected", "EXAMPLE\\jsmith", True, True, False))
        self.assertTrue(pc.store.read_line_status(w.ids["line"]).stopped_needs_send)
        self.assertEqual([(ev.status, ev.machine_id) for ev in events if isinstance(ev, ActionEvent)],
                         [("running", mid), ("ok", mid)])
        self.assertIsInstance(events[-1], ActionFinished)
        entry = self.logs(pc, entry_type="stop")[0]
        self.assertEqual((entry["reason"], entry["send_id"], entry["machines"][0]["printing_after"]),
                         ("rejected", sid, 0))
        check = pc.actions.check_resume(mid)
        self.assertFalse(check.allowed)
        self.assertIsNone(check.design)
        self.assertEqual(check.msg.english(), "The last Send to Line 1 Bagger 1 was rejected. Send again instead.")
        writes = list(fake.writes)
        resumed = pc.actions.resume([mid], lambda ev: None)
        self.assertEqual((resumed.machines[0].ok, resumed.machines[0].msg.english()),
                         (False, "The last Send to Line 1 Bagger 1 was rejected. Send again instead."))
        self.assertEqual(fake.writes, writes)           # no write to the printer
        self.assertClean(fake)

    def test_wrong_then_a_failed_send(self):
        w, pc, result = self.live()
        mid = self.mid()
        pc.pipeline.confirm(result.send_id, [mid], False)
        w.printers[0].override("HXUI", "1110")
        failed, _, _ = self.send(pc)
        self.assertEqual(failed.machines[0].step, STEP_CHECK_MACHINE)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.rejected, e.resume_blocked, e.send_id, e.failure.step, e.failure.after_switch),
                         (False, True, failed.send_id, STEP_CHECK_MACHINE, False))
        self.assertEqual((e.mode, e.message_name), ("running", result.machines[0].message_name))
        self.assertIsNotNone(resume_refusal(e, pc.data.current.machines[mid]))
        self.assertClean()

    def test_superseded(self):
        w, pc, older = self.live()
        mid = self.mid()
        newer, _, _ = self.send(pc)
        before = pc.store.read_expected(mid)
        pc.pipeline.confirm(older.send_id, [mid], True)
        after = pc.store.read_expected(mid)
        self.assertEqual((after.counter, after.send_id, after.confirmed), (before.counter, newer.send_id, False))
        self.assertEqual(len(self.logs(pc, entry_type="send_confirm", ref=older.send_id)), 1)
        self.assertEqual(pc.pipeline.pending_confirmation(older.send_id), [])
        self.assertEqual(pc.pipeline.pending_confirmation(newer.send_id), [mid])
        self.assertClean()

    def test_lock_held_while_confirming(self):
        w, pc, result = self.live()
        mid = self.mid()
        self.hold_lock(self.pc("akim"))
        with self.assertRaises(ShareError) as cm:
            pc.pipeline.confirm(result.send_id, [mid], True)
        self.assertEqual(cm.exception.msg.english(),
                         "Your answer was logged, but Stripes couldn't update the monitor for Line 1 Bagger 1.")
        self.assertFalse(pc.store.read_expected(mid).confirmed)
        self.assertEqual(len(self.logs(pc, entry_type="send_confirm", ref=result.send_id)), 1)
        self.assertClean()


# --------------------------------------------------------------------------
# Failures: the red window, Try again, Leave it
# --------------------------------------------------------------------------

class FailureTests(Base):
    def test_rows(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake = w.printers[0]
        hook_load(fake, 9, 1, change_lot)
        result, plan, _ = self.send(pc)
        row = hx_send.failure_rows(result, plan)[0]
        self.assertEqual((row.step, row.after_switch, row.previous_confirmed), (STEP_VERIFY_IDLE, False, False))
        self.assertIn(("item 3 (CTEXT)", "TXT", "Lot 4417", "Lot 4418"),
                      [(d.where, d.field, d.sent, d.read) for d in row.differences])
        self.assertEqual(row.msg, result.machines[0].msg)
        self.assertEqual(row.msg.english(),
                         "The message read back from Line 1 Bagger 1 differs from what was sent (1 difference).")
        self.assertEqual(spanish(row.msg), "El mensaje leído de Line 1 Bagger 1 difiere de lo enviado (1 diferencia).")
        # A machine missing from the plan still shows, by a name and never by its id
        unknown = hx_send.failure_rows(result, plan.restricted([]))[0]
        self.assertEqual((unknown.machine_id, unknown.machine_name, unknown.line_name),
                         (self.mid(), "Unknown machine", ""))
        i18n.set_language("es")
        try:
            self.assertEqual(hx_send.failure_rows(result, plan.restricted([]))[0].machine_name, "Máquina desconocida")
        finally:
            i18n.set_language("en")
        # Two differences: the plural
        hook_load(fake, 9, 1, change_lot_and_origin)
        result, plan, _ = self.send(pc)
        row = hx_send.failure_rows(result, plan)[0]
        self.assertEqual((row.step, len(row.differences)), (STEP_VERIFY_IDLE, 2))
        self.assertEqual(row.msg.english(),
                         "The message read back from Line 1 Bagger 1 differs from what was sent (2 differences).")
        self.assertEqual(spanish(row.msg),
                         "El mensaje leído de Line 1 Bagger 1 difiere de lo enviado (2 diferencias).")
        # After the switch: the warning text and the previous picture
        fake.web_reply_hook = None
        fake.override("PRINTMSG", FakePrinter.DROP)
        result, plan, _ = self.send(pc)
        row = hx_send.failure_rows(result, plan)[0]
        self.assertEqual((row.step, row.after_switch), (STEP_SWITCH, True))
        self.assertEqual(row.after_switch_text.english(),
                         "This machine may be printing the new message, the old one, or nothing. Check the bags now.")
        self.assertTrue(row.previous_preview.startswith(b"BM"))
        self.assertEqual(row.differences, [])
        self.assertClean(fake)

    def test_try_again(self):
        w = self.world([{}, {}])
        pc = self.pc()
        self.own_poll(pc)
        w.printers[1].override("CPPARM", "3")
        result, plan, _ = self.send(pc, (1, 2))
        rows = hx_send.failure_rows(result, plan)
        self.assertEqual([row.machine_id for row in rows], [self.mid(2)])
        w.printers[1].override("CPPARM", None)
        again = plan.restricted([row.machine_id for row in rows])
        events = []
        retried = pc.pipeline.run(again, events.append)
        self.assertNotEqual(retried.send_id, result.send_id)
        self.assertEqual([r.machine_id for r in retried.machines], [self.mid(2)])
        self.assertTrue(retried.machines[0].ok)
        mine = [e for e in events if isinstance(e, SendEvent) and e.machine_id == self.mid(2)]
        self.assertEqual((mine[0].step, mine[0].status), (STEP_LOCK, "running"))
        self.assertFalse(any(isinstance(e, SendEvent) and e.machine_id == self.mid(1) for e in events))
        self.assertEqual(pc.store.read_expected(self.mid(2)).send_id, retried.send_id)
        self.assertEqual(pc.store.read_expected(self.mid(1)).send_id, result.send_id)
        self.assertClean()

    def test_leave_it(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        w.printers[0].override("CPPARM", "3")
        result, _, _ = self.send(pc)
        mid = self.mid()
        pc.pipeline.leave_it(result.send_id, [mid])
        e = pc.store.read_expected(mid)
        self.assertEqual((e.left_by, e.result, e.failure.step), ("EXAMPLE\\jsmith", RED, STEP_BUILD))
        self.assertIsNotNone(e.left_at)
        self.assertRegex(e.reread_token, r"^[0-9a-f]{16}$")
        entry = self.logs(pc, entry_type="send_leave", ref=result.send_id)[0]
        self.assertEqual((entry["machine_ids"], entry["reread_token"]), ([mid], e.reread_token))
        with self.assertRaises(NotAllowed):
            self.pc("nobody", sender=False).pipeline.leave_it(result.send_id, [mid])
        # A machine the records no longer have can't be updated, and the text doesn't show its id
        with self.assertRaises(ShareError) as cm:
            pc.pipeline.leave_it(result.send_id, [mid, "mc_0123456789"])
        self.assertEqual(cm.exception.msg.english(), "Your answer was logged, but Stripes couldn't update the monitor.")
        self.assertEqual(spanish(cm.exception.msg),
                         "Su respuesta quedó registrada, pero Stripes no pudo actualizar el monitor.")
        self.assertEqual(pc.store.read_expected(mid).left_by, "EXAMPLE\\jsmith")
        self.assertClean()


# --------------------------------------------------------------------------
# Stop, Resume, Set clocks
# --------------------------------------------------------------------------

class ActionTests(Base):
    def confirmed(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        result, _, _ = self.send(pc)
        pc.pipeline.confirm(result.send_id, [self.mid()], True)
        return w, pc, result

    def test_stop_and_resume(self):
        w, pc, result = self.confirmed()
        mid, fake = self.mid(), w.printers[0]
        events = []
        stopped = pc.actions.stop([mid], events.append, reason="user", action_id="ac_test")
        self.assertEqual((stopped.action_id, stopped.kind, stopped.machines[0].colour), ("ac_test", "stop", GREEN))
        self.assertFalse(fake.printing)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.mode, e.stop_reason, e.resume_blocked), ("stopped_by_user", "user", False))
        check = pc.actions.check_resume(mid)
        self.assertTrue(check.allowed)
        self.assertEqual((check.design.slot, check.origin_line), (9, "Product of USA and Canada"))
        prin_before = queries(fake, "PRIN")
        events = []
        resumed = pc.actions.resume([mid], events.append)
        r = resumed.machines[0]
        self.assertEqual((r.ok, r.colour, r.step, r.message_name, r.slot), (True, GREEN, hx_send.ACTION_RESUME,
                                                                           result.machines[0].message_name, 9))
        self.assertEqual(apcmds(fake)[-1], "<PRINT>1</PRINT>")
        self.assertEqual(queries(fake, "PRIN") - prin_before, 2)
        self.assertTrue(fake.printing)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.mode, e.printing_expected, e.stop_reason, e.stopped_by), ("running", True, "", ""))
        self.assertEqual([ev.status for ev in events if isinstance(ev, ActionEvent)], ["running", "ok"])
        entry = self.logs(pc, entry_type="resume")[0]
        self.assertEqual(entry["machines"][0]["origin_line"], "Product of USA and Canada")
        self.assertTrue(entry["machines"][0]["design"]["preview_b64"])
        self.assertClean(fake)

    def test_resume_refused(self):
        w, pc, result = self.confirmed()
        mid, fake = self.mid(), w.printers[0]
        pc.actions.stop([mid], lambda e: None)
        with fake.lock:         # someone saves another message into the selected slot at the printer
            design = fake.slots[fake.current_slot]
            for item in design.items:
                item.pairs = [(k, "Lot 9999" if (k, v) == ("TXT", "Lot 4417") else v) for k, v in item.pairs]
        mismatch = "The message on Line 1 Bagger 1 isn't the last confirmed Send. Send again instead."
        check = pc.actions.check_resume(mid)
        self.assertFalse(check.allowed)
        self.assertEqual(check.msg.english(), mismatch)
        self.assertIsNotNone(check.design)
        writes = list(fake.writes)
        resumed = pc.actions.resume([mid], lambda e: None)
        self.assertEqual((resumed.machines[0].ok, resumed.machines[0].msg.english()), (False, mismatch))
        self.assertEqual(fake.writes, writes)
        self.assertFalse(fake.printing)
        self.assertClean(fake)

    def test_stop_fails(self):
        w, pc, result = self.confirmed()
        mid, fake = self.mid(), w.printers[0]
        fake.override("PRINT", FakePrinter.DROP)
        stopped = pc.actions.stop([mid], lambda e: None)
        r = stopped.machines[0]
        self.assertEqual((r.ok, r.colour), (False, RED))
        self.assertEqual(r.msg.english(), "Stripes couldn't stop printing on Line 1 Bagger 1. Stop it at the printer "
                                          "with its Print On/Off button now.")
        e = pc.store.read_expected(mid)
        self.assertEqual((e.mode, e.printing_expected, e.stop_reason), ("stop_requested_at_printer", False, "user"))
        self.assertClean(fake)

    def test_check_resume_needs_the_server(self):
        w, pc, result = self.confirmed()
        pc.actions.stop([self.mid()], lambda e: None)
        pc.store.fs.down = True
        check = pc.actions.check_resume(self.mid())
        self.assertFalse(check.allowed)
        self.assertEqual(check.msg.english(), "Stripes can't reach the server, so it can't check the last confirmed "
                                              "Send.")
        resumed = pc.actions.resume([self.mid()], lambda e: None)
        self.assertEqual((resumed.machines[0].ok, resumed.machines[0].msg), (False, check.msg))
        self.assertFalse(w.printers[0].printing)
        self.assertClean()

    def test_lock_held_names_the_machine_without_its_line(self):
        # A sender's lock names the line; a line the records no longer have falls back to the machine's
        # name, never to an empty name or an id
        self.world()
        pc = self.pc()
        self.hold_lock(self.pc("akim"))
        result = pc.actions.stop([self.mid()], lambda e: None)
        self.assertEqual(result.machines[0].msg.english(), "Line 1 is being sent by EXAMPLE\\akim.")
        del pc.data.current.lines[self.w.ids["line"]]
        result = pc.actions.stop([self.mid()], lambda e: None)
        self.assertEqual(result.machines[0].msg.english(), "Line 1 Bagger 1 is being sent by EXAMPLE\\akim.")
        self.assertEqual(spanish(result.machines[0].msg), "EXAMPLE\\akim está enviando a Line 1 Bagger 1.")
        self.assertUntouched()

    def test_not_a_sender(self):
        self.world()
        with self.assertRaises(NotAllowed) as cm:
            self.pc("admin", admin=True, sender=False).actions.stop([self.mid()], lambda e: None)
        self.assertEqual(cm.exception.msg.english(), "Only Stripes MapleJet Senders can stop, resume or set clocks.")
        self.assertUntouched()

    def test_set_clocks(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake, mid = w.printers[0], self.mid()
        w.sntp.silent = True
        self.send(pc)                                   # made with the PC's time
        self.assertEqual(pc.store.read_expected(mid).notes, ["pc_time"])
        w.sntp.silent = False
        with fake.lock:
            fake.clock_offset_s = 7860.0                # 2 h 11 min fast
        result = pc.actions.set_clocks([mid], lambda e: None)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.step, r.notes), (True, GREEN, STEP_SET_CLOCK, []))
        local = pc.time.now().local
        self.assertLessEqual(abs((fake.printer_clock() - local).total_seconds()), 90)
        entry = self.logs(pc, entry_type="clock_set")[0]
        clock = entry["machines"][0]["clock"]
        self.assertEqual((entry["time_source"], clock["source"]), ("server", "server"))
        self.assertIsNotNone(clock["before"])
        self.assertIsNotNone(clock["after"])
        self.assertGreater(abs((fake.printer_clock() - local).total_seconds() - 7860), 3000)
        e = pc.store.read_expected(mid)
        self.assertEqual((e.notes, e.result), ([], AMBER))         # unconfirmed: still amber
        self.assertClean(fake)

    def test_set_clocks_with_pc_time(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        w.sntp.silent = True
        result = pc.actions.set_clocks([self.mid()], lambda e: None)
        r = result.machines[0]
        self.assertEqual((r.ok, r.colour, r.notes), (True, AMBER, ["pc_time"]))
        self.assertIsNone(pc.store.read_expected(self.mid()))       # nothing recorded for it before
        self.assertClean()


# --------------------------------------------------------------------------
# Test print and calibration
# --------------------------------------------------------------------------

class TestPrintTests(Base):
    def admin(self, *, data=None):
        admin = self.pc("admin", admin=True, sender=False)
        self.own_poll(admin)
        return admin

    def plan(self, admin, ranges=None, kind="test", data=None):
        ranges = ranges or TestRanges(vary="delay", start=470, stop=490, step=10)
        return plan_test(self.mid(), self.w.ids["film"], ranges, data or admin.data.current, admin.identity, kind=kind)

    def test_counted(self):
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        fake.override("PMCNT", rising())
        plan = self.plan(admin)
        self.assertEqual([s.label for s in plan.steps], ["D470", "D480", "D490"])
        events = []
        result = admin.print_run(plan).run(events.append)
        self.assertEqual((result.ok, result.steps_done, result.of, result.cleared, result.printing_off, result.msg),
                         (True, 3, 3, True, True, None))
        # The steps alternate between the slots; each is checked in the idle slot, then twice running
        saves = [x for x in apcmds(fake) if x.startswith("<SAVEMSG>")]
        self.assertEqual(saves, ["<SAVEMSG>9</SAVEMSG>", "<SAVEMSG>10</SAVEMSG>", "<SAVEMSG>9</SAVEMSG>",
                                 "<SAVEMSG>9</SAVEMSG>", "<SAVEMSG>10</SAVEMSG>"])
        self.assertEqual(loads(fake), [0, 9, 0, 0, 10, 0, 0, 9, 0, 0, 9, 10])
        test_events = [e for e in events if isinstance(e, TestEvent)]
        for k, step in enumerate(plan.steps, start=1):
            printing = [e for e in test_events if e.status == "printing" and e.step_index == k]
            self.assertEqual([e.bags for e in printing], [0, 1, 2, 3, 4, 5])
            labels = [v for it in printing[0].design.items if it.tag == "CTEXT" for k2, v in it.pairs if k2 == "TXT"]
            self.assertEqual(labels, [step.label])
        self.assertEqual([e.status for e in test_events][-2:], ["stopping", "cleared"])
        self.assertIsInstance(events[-1], TestFinished)
        tail = apcmds(fake)[-5:]
        self.assertEqual(tail, ["<PRINT>0</PRINT>", "<CRTMSG></CRTMSG>", "<SAVEMSG>9</SAVEMSG>", "<CRTMSG></CRTMSG>",
                                "<SAVEMSG>10</SAVEMSG>"])
        self.assertFalse(fake.printing)
        self.assertEqual((fake.slots[9].items, fake.slots[10].items), ([], []))
        e = admin.store.read_expected(self.mid())
        self.assertEqual((e.mode, e.resume_blocked, e.stop_reason, e.stopped_by, e.message_name, e.slot),
                         ("test_finished", True, "test", "EXAMPLE\\admin", "", None))
        self.assertEqual((e.test["run_id"], e.test["step"], e.test["of"], e.sender), (plan.run_id, 3, 3,
                                                                                     "EXAMPLE\\admin"))
        self.assertIsNotNone(resume_refusal(e, admin.data.current.machines[self.mid()]))
        self.assertTrue(admin.store.read_line_status(w.ids["line"]).stopped_needs_send)
        steps = self.logs(admin, "hx-admin", entry_type="test_step", ref=plan.run_id)
        self.assertEqual([(s["index"], s["label"], s["bags"]) for s in steps], [(1, "D470", 5), (2, "D480", 5),
                                                                                (3, "D490", 5)])
        self.assertEqual([s["settings"]["delay_us"] for s in steps], [470000, 480000, 490000])
        start = self.logs(admin, "hx-admin", entry_type="test_start", ref=plan.run_id)[0]
        self.assertEqual((start["counter_tag"], len(start["steps"]), start["previous"]["slot"]), ("PMCNT", 3, 1))
        end = self.logs(admin, "hx-admin", entry_type="test_end", ref=plan.run_id)[0]
        self.assertEqual((end["reason"], end["ok"], end["slots"]), ("done", True, {"9": True, "10": True}))
        self.assertEqual(self.lock_files(), [])
        self.assertClean(fake)

    def test_end_again(self):
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        fake.override("PMCNT", rising())
        plan = self.plan(admin, TestRanges(vary="delay", start=480, stop=480, step=10))

        def on(event):
            if isinstance(event, TestEvent) and event.status == "ok" and event.step_index == event.of:
                fake.override("PRINT", FakePrinter.DROP)    # the end's PRINT 0 gets no reply
        run = admin.print_run(plan)
        result = run.run(on)
        self.assertEqual((result.printing_off, result.cleared, result.ok, result.steps_done), (False, False, False, 1))
        self.assertEqual(result.msg.english(), "Stripes couldn't stop printing on Line 1 Bagger 1. Stop it at the "
                                               "printer with its Print On/Off button now.")
        e = admin.store.read_expected(self.mid())
        self.assertEqual((e.mode, e.stop_reason, e.resume_blocked), ("stop_requested_at_printer", "test", True))
        fake.override("PRINT", None)
        sender = self.pc()                          # a sender who isn't an administrator can't
        with self.assertRaises(NotAllowed):
            hx_send.TestPrintRun(plan=plan, **sender.kw).end_again(lambda e: None)
        events = []
        again = run.end_again(events.append)        # the administrator, who isn't a sender
        self.assertEqual((again.printing_off, again.cleared, again.ok, again.steps_done, again.of),
                         (True, True, False, 1, 1))
        e = admin.store.read_expected(self.mid())
        self.assertEqual((e.mode, e.stop_reason), ("test_finished", "test"))
        ends = self.logs(admin, "hx-admin", entry_type="test_end", ref=plan.run_id)
        self.assertEqual([x["reason"] for x in ends], ["done", "retried"])
        self.assertIsInstance(events[-1], TestFinished)
        self.assertEqual(self.lock_files(), [])
        # After a Send, Try again touches nothing
        sender = self.pc()
        self.own_poll(sender)
        self.assertTrue(self.send(sender)[0].machines[0].ok)
        requests = len(fake.requests)
        refused = run.end_again(lambda e: None)
        # True, not False: the machine prints the Send now, so the red page must not stay up. A bool,
        # as architecture §5.9 types it
        self.assertEqual((refused.ok, refused.printing_off, refused.cleared), (False, True, False))
        self.assertIs(type(refused.printing_off), bool)
        self.assertEqual(refused.msg.english(), "Line 1 Bagger 1 has had a Send or another test since this test, "
                                                "so Stripes didn't change it.")
        self.assertEqual(len(fake.requests), requests)
        self.assertEqual(admin.store.read_expected(self.mid()).mode, "running")
        self.assertClean(fake)

    def test_no_prints(self):
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        plan = self.plan(admin)
        started = time.monotonic()
        result = admin.print_run(plan).run(lambda e: None)
        self.assertGreaterEqual(time.monotonic() - started, FAST.test_no_print_s)
        self.assertEqual((result.ok, result.steps_done, result.printing_off, result.cleared), (False, 0, True, True))
        self.assertEqual(result.msg.english(), f"No prints for {round(FAST.test_no_print_s)} s, so the test was stopped.")
        end = self.logs(admin, "hx-admin", entry_type="test_end", ref=plan.run_id)[0]
        self.assertEqual(end["reason"], "no_prints")
        self.assertClean(fake)

    def test_timed(self):
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        current = admin.data.current
        admin.data.current = dataclasses.replace(current, company=dataclasses.replace(current.company,
                                                                                      bag_counter="none"))
        plan = self.plan(admin, TestRanges(vary="delay", start=470, stop=480, step=10))
        self.assertTrue(plan.timed)
        plan.base.machine.seconds_per_bag = 0.04            # 0.2 s a step
        events = []
        result = admin.print_run(plan).run(events.append)
        self.assertEqual((result.ok, result.steps_done), (True, 2))
        self.assertEqual((queries(fake, "PMCNT"), queries(fake, "MSCNT")), (0, 0))
        test_events = [e for e in events if isinstance(e, TestEvent)]
        for k in (1, 2):
            printing = [e for e in test_events if e.status == "printing" and e.step_index == k]
            done = [e for e in test_events if e.status == "ok" and e.step_index == k]
            self.assertEqual([e.bags for e in printing], [None])
            # The step's time runs from before its expected-state and lock writes, which the "printing"
            # event follows: a loaded machine can spend tens of ms on them, so allow 50 ms
            self.assertGreaterEqual(done[0].at - printing[0].at, 0.15)
        self.assertClean(fake)

    def test_stop_test(self):
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        fake.override("PMCNT", rising())
        plan = self.plan(admin)
        cancel = CancelToken()

        def on(event):
            if isinstance(event, TestEvent) and event.status == "ok" and event.step_index == 1:
                cancel.cancel()
        result = admin.print_run(plan).run(on, cancel=cancel)
        self.assertEqual((result.ok, result.steps_done, result.printing_off, result.cleared), (False, 1, True, True))
        self.assertEqual(result.msg.english(), "Stopped by EXAMPLE\\admin.")
        end = self.logs(admin, "hx-admin", entry_type="test_end", ref=plan.run_id)[0]
        self.assertEqual(end["reason"], "cancelled")
        self.assertEqual(admin.store.read_expected(self.mid()).mode, "test_finished")
        self.assertClean(fake)

    def test_failed_step_runs_the_end(self):
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        fake.override("PMCNT", rising())

        def on(event):
            if isinstance(event, TestEvent) and event.status == "printing" and event.step_index == 1:
                fake.override("CPPARM", "3")        # step 2's build is refused
        events = []
        result = admin.print_run(self.plan(admin)).run(lambda e: (events.append(e), on(e)))
        self.assertEqual((result.ok, result.steps_done, result.printing_off, result.cleared), (False, 0, True, True))
        self.assertTrue(result.msg.english().endswith("refused CPPARM (code 3)."), result.msg.english())
        self.assertIn("failed", [e.status for e in events if isinstance(e, TestEvent)])
        self.assertEqual(apcmds(fake)[-5:], ["<PRINT>0</PRINT>", "<CRTMSG></CRTMSG>", "<SAVEMSG>9</SAVEMSG>",
                                             "<CRTMSG></CRTMSG>", "<SAVEMSG>10</SAVEMSG>"])
        self.assertEqual(self.logs(admin, "hx-admin", entry_type="test_end")[0]["reason"], "failed")
        self.assertEqual(admin.store.read_expected(self.mid()).mode, "test_finished")
        self.assertClean(fake)

    def test_password_refused_mid_test(self):
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        fake.override("PMCNT", rising())

        def on(event):
            if isinstance(event, TestEvent) and event.status == "ok" and event.step_index == 1:
                fake.set_password("654321")
        result = admin.print_run(self.plan(admin)).run(on)
        self.assertEqual((result.ok, result.steps_done, result.printing_off, result.cleared), (False, 1, False, False))
        self.assertEqual(result.msg.english(), "Stripes couldn't stop printing on Line 1 Bagger 1. Stop it at the "
                                               "printer with its Print On/Off button now.")
        self.assertEqual(fake.requests[-1]["status"], 401)      # nothing more after the refusal
        e = admin.store.read_expected(self.mid())
        self.assertEqual((e.mode, e.stop_reason, e.resume_blocked), ("stop_requested_at_printer", "test", True))
        self.assertEqual(self.logs(admin, "hx-admin", entry_type="test_end")[0]["printing_off"], False)
        self.assertClean(fake)

    def test_lock_held(self):
        self.world()
        admin = self.admin()
        self.hold_lock(self.pc("akim"))
        result = admin.print_run(self.plan(admin)).run(lambda e: None)
        self.assertEqual(result.msg.english(), "Line 1 is being sent by EXAMPLE\\akim.")
        self.assertUntouched()

    def test_needs_the_server(self):
        self.world()
        admin = self.admin()
        plan = self.plan(admin)
        admin.store.fs.down = True
        events = []
        result = admin.print_run(plan).run(events.append)
        self.assertEqual((result.ok, result.steps_done), (False, 0))
        self.assertEqual(result.msg.english(), "A test print needs the server, so that nobody else uses the printer "
                                               "meanwhile.")
        self.assertIsInstance(events[-1], TestFinished)
        self.assertUntouched()

    def test_not_an_administrator(self):
        self.world()
        admin = self.admin()
        plan = self.plan(admin)
        with self.assertRaises(NotAllowed):
            hx_send.TestPrintRun(plan=plan, **self.pc().kw).run(lambda e: None)
        self.assertUntouched()

    def test_ink_is_a_warning(self):
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        fake.ink_name = "Example White"
        fake.override("PMCNT", rising())
        events = []
        result = admin.print_run(self.plan(admin, TestRanges(vary="delay", start=480, stop=480, step=1))).run(
            events.append)
        self.assertTrue(result.ok)
        warnings = [e.msg.english() for e in events if isinstance(e, TestEvent) and e.status == "warning"]
        self.assertEqual(warnings, ["Line 1 Bagger 1's ink doesn't match the film: the marks may not show."])
        self.assertClean(fake)

    def test_calibration(self):
        for calibrated in (True, False):
            with self.subTest(calibrated=calibrated):
                w = self.world([{"placement": "encoder"}])
                admin = self.admin()
                fake = w.printers[0]
                fake.override("PMCNT", rising())
                mid = self.mid()
                current = admin.data.current
                machine = current.machines[mid]
                if not calibrated:
                    machine = dataclasses.replace(machine, head_distance_mm=None)
                data = dataclasses.replace(
                    current, machines={mid: machine},
                    calibrations={mid: Calibration(id=mid, machine_id=mid, rough_head_distance_mm=400.0)})
                admin.data.current = data
                plan = self.plan(admin, TestRanges(vary="position", start=0, stop=0, step=1), kind="spacing",
                                 data=data)
                self.assertEqual([(s.label, s.position_mm) for s in plan.steps], [("SPACE A", 60.0),
                                                                                  ("SPACE B", 360.0)])
                result = admin.print_run(plan).run(lambda e: None)
                self.assertTrue(result.ok, result.msg)
                self.assertEqual(dlymm(fake), [60, 360])
                self.assertClean(fake)

    # ---- runs that end before they send anything (review findings: printing_off True, not False,
    # and a bool, not None)

    def test_refused_runs_change_nothing(self):
        # Nothing is sent, no row is written and printing_off is True (nothing of this test is
        # printing), so the window never tells the administrator to press Print On/Off on a printer
        # this test didn't touch (ui.md §12.5 item 5). It is a bool, as architecture §5.9 types it, so
        # a UI that tests "not printing_off" is right and the result round-trips through from_json
        def lock_held(admin, fake):
            return self.hold_lock(self.pc("akim"))      # akim is sending to this machine right now

        def share_down(admin, fake):
            admin.store.fs.down = True

        cases = [
            ("lock held", lock_held, "Line 1 is being sent by EXAMPLE\\akim."),
            ("share down", share_down, "A test print needs the server, so that nobody else uses the printer "
                                       "meanwhile."),
            ("wrong printer", lambda admin, fake: fake.override("HXUI", "1110"), None),
            ("UID unreadable", lambda admin, fake: fake.override("HXUI", "abc"), None),
        ]
        for name, inject, text in cases:
            with self.subTest(name):
                w = self.world()
                admin = self.admin()
                fake = w.printers[0]
                plan = self.plan(admin)
                other_lock = inject(admin, fake)
                events = []
                result = admin.print_run(plan).run(events.append)
                self.assertEqual((result.ok, result.steps_done, result.printing_off, result.cleared),
                                 (False, 0, True, False))
                self.assertIs(type(result.printing_off), bool)
                self.assertEqual(from_json(TestResult, to_json(result)), result)
                if text is not None:
                    self.assertEqual(result.msg.english(), text)
                self.assertEqual(fake.writes, [])
                self.assertNotIn("stopping", [e.status for e in events if isinstance(e, TestEvent)])
                self.assertIsInstance(events[-1], TestFinished)
                self.assertIs(events[-1].result.printing_off, True)
                other = w.store(w.identity("observer"))
                self.assertIsNone(other.read_expected(self.mid()))      # no row written
                status = other.read_line_status(w.ids["line"])
                self.assertFalse(status is not None and status.stopped_needs_send)
                # Still in the admin-change log (queued on this PC while the share was down)
                ends = [other.read_log("hx-admin", n) for n in other.list_logs("hx-admin", entry_type="test_end",
                                                                               ref=plan.run_id)]
                ends += [stored for kind, _name, stored in admin.local.queued_logs()
                         if kind == "hx-admin" and stored.get("type") == "test_end"]
                self.assertEqual([(e["reason"], e["ok"], e["printing_off"], e["cleared"]) for e in ends],
                                 [("failed", False, True, False)])
                if other_lock is not None:
                    other_lock.release()
                self.assertEqual(self.lock_files(), [])
                if text is not None:
                    self.assertUntouched(fake)              # not even a read
                else:
                    self.assertClean(fake)

    def test_end_again_refusals(self):
        # Try again after a real "Stop it at the printer now": while someone else has the printer
        # nothing is sent and printing_off stays False (the test may still be printing)
        w = self.world()
        admin = self.admin()
        fake = w.printers[0]
        fake.override("PMCNT", rising())
        plan = self.plan(admin, TestRanges(vary="delay", start=480, stop=480, step=10))

        def on(event):
            if isinstance(event, TestEvent) and event.status == "ok":
                fake.override("PRINT", FakePrinter.DROP)
        run = admin.print_run(plan)
        self.assertIs(run.run(on).printing_off, False)
        fake.override("PRINT", None)
        akim_lock = self.hold_lock(self.pc("akim"))
        requests = len(fake.requests)
        refused = run.end_again(lambda e: None)
        self.assertEqual((refused.ok, refused.printing_off, refused.msg.english()),
                         (False, False, "Line 1 is being sent by EXAMPLE\\akim."))
        self.assertEqual(len(fake.requests), requests)
        akim_lock.release()
        self.assertEqual(admin.store.read_expected(self.mid()).mode, "stop_requested_at_printer")
        again = run.end_again(lambda e: None)
        self.assertEqual((again.printing_off, again.cleared), (True, True))
        ends = self.logs(admin, "hx-admin", entry_type="test_end", ref=plan.run_id)
        self.assertEqual(sorted((e["reason"], e["printing_off"]) for e in ends),
                         [("done", False), ("retried", False), ("retried", True)])
        self.assertClean(fake)

    def test_bag_counter_changed_since_the_plan(self):
        # Review finding: a counted plan run after the company switched to "none" read the counter
        # "none" mid-test (ForbiddenCommand), so run() raised with step 1 still printing. TestPlan.timed
        # and Company.bag_counter must agree; when they don't, the run is refused before the lock
        text = "The bag counter setting changed since this test was set up. Set it up again."

        def company(admin, counter):
            current = admin.data.current
            admin.data.current = dataclasses.replace(current, company=dataclasses.replace(current.company,
                                                                                          bag_counter=counter))
        for planned, now in (("PMCNT", "none"), ("none", "PMCNT"), ("none", "MSCNT")):
            with self.subTest(planned=planned, now=now):
                w = self.world()
                admin = self.admin()
                fake = w.printers[0]
                fake.override("PMCNT", rising())
                company(admin, planned)
                plan = self.plan(admin, TestRanges(vary="delay", start=470, stop=480, step=10))
                self.assertEqual(plan.timed, planned == "none")
                company(admin, now)     # an administrator changes it before Start is pressed
                events = []
                result = admin.print_run(plan).run(events.append)        # raises nothing
                self.assertEqual((result.ok, result.steps_done, result.printing_off, result.cleared,
                                  result.msg.english()), (False, 0, True, False, text))
                self.assertIsInstance(events[-1], TestFinished)
                self.assertUntouched(fake)                  # not even a read: refused before the lock
                self.assertIsNone(admin.store.read_expected(self.mid()))
                self.assertEqual(self.lock_files(), [])
                ends = self.logs(admin, "hx-admin", entry_type="test_end", ref=plan.run_id)
                self.assertEqual([(e["reason"], e["printing_off"]) for e in ends], [("failed", True)])
        # The same company as the plan's runs as usual, here counting with MSCNT
        company(admin, "MSCNT")
        plan = self.plan(admin, TestRanges(vary="delay", start=480, stop=480, step=10))
        fake.override("MSCNT", rising(2_000))
        result = admin.print_run(plan).run(lambda e: None)
        self.assertEqual((result.ok, result.printing_off, result.cleared), (True, True, True))
        self.assertGreater(queries(fake, "MSCNT"), 0)
        self.assertFalse(fake.printing)
        self.assertClean(fake)

    # ---- the machine check and an early Stop test run the end procedure (send.md §15.4)

    def test_machine_check_failure_runs_the_end(self):
        def fonts(fake, cancel):
            with fake.lock:
                fake.fonts = tuple("Arial2" if f == "Arial" else f for f in STANDARD_FONTS)

        def stopped_first(fake, cancel):
            cancel.cancel()                         # Stop test before the first request

        def stopped_at_ink(fake, cancel):
            fake.override("INKT", lambda: cancel.cancel())      # answers normally; stops after it

        fonts_text = "Line 1 Bagger 1's printer has fonts that differ from Stripes' standard list."
        cases = [("fonts", fonts, "failed", fonts_text), ("stopped first", stopped_first, "cancelled", None),
                 ("stopped at INKT", stopped_at_ink, "cancelled", None)]
        for name, inject, reason, text in cases:
            with self.subTest(name):
                w = self.world()
                admin = self.admin()
                fake = w.printers[0]
                plan = self.plan(admin)
                cancel = CancelToken()
                inject(fake, cancel)
                events = []
                result = admin.print_run(plan).run(events.append, cancel=cancel)
                text = text or "Stopped by EXAMPLE\\admin."
                self.assertEqual((result.ok, result.steps_done, result.printing_off, result.cleared,
                                  result.msg.english()), (False, 0, True, True, text))
                # Only the end procedure's writes, after a UID check
                self.assertEqual(fake.writes, ["PRINT", "CRTMSG", "SAVEMSG", "CRTMSG", "SAVEMSG"])
                self.assertEqual(apcmds(fake)[0], "<PRINT>0</PRINT>")
                self.assertGreaterEqual(queries(fake, "HXUI"), 1)
                self.assertFalse(fake.printing)
                statuses = [e.status for e in events if isinstance(e, TestEvent)]
                self.assertEqual(statuses[-2:], ["stopping", "cleared"])
                e = admin.store.read_expected(self.mid())
                self.assertEqual((e.mode, e.stop_reason, e.resume_blocked, e.printing_expected, e.sender),
                                 ("test_finished", "test", True, False, "EXAMPLE\\admin"))
                self.assertEqual((e.test["run_id"], e.test["step"], e.test["of"]), (plan.run_id, 0, 3))
                self.assertTrue(admin.store.read_line_status(w.ids["line"]).stopped_needs_send)
                ends = self.logs(admin, "hx-admin", entry_type="test_end", ref=plan.run_id)
                self.assertEqual([(x["reason"], x["printing_off"], x["cleared"]) for x in ends],
                                 [(reason, True, True)])
                self.assertEqual(self.logs(admin, "hx-admin", entry_type="test_start"), [])
                self.assertEqual(self.lock_files(), [])
                self.assertClean(fake)

    # ---- timed steps: the limit runs from each step's own switch (send.md §15.6)

    def timed_world(self, steps):
        self.timings = dataclasses.replace(FAST, test_no_print_s=0.6)
        w = self.world()
        admin = self.admin()
        current = admin.data.current
        admin.data.current = dataclasses.replace(current, company=dataclasses.replace(current.company,
                                                                                      bag_counter="none"))
        plan = self.plan(admin, TestRanges(vary="delay", start=400, stop=400 + 10 * (steps - 1), step=10))
        self.assertEqual((len(plan.steps), plan.timed), (steps, True))
        plan.base.machine.seconds_per_bag = 0.01            # 0.05 s a step
        return w, admin, plan

    def test_long_timed_test_runs_to_the_end(self):
        # Each step's switch and running checks take far longer than its 0.05 s, so a limit of N x 0.05 s
        # + test_no_print_s from step 1's switch would stop this test halfway
        w, admin, plan = self.timed_world(6)
        fake = w.printers[0]
        fake.latency_s = 0.02
        started = time.monotonic()
        result = admin.print_run(plan).run(lambda e: None)
        self.assertGreater(time.monotonic() - started, 6 * 0.05 + self.timings.test_no_print_s)
        self.assertEqual((result.ok, result.steps_done, result.msg), (True, 6, None))
        self.assertClean(fake)

    def test_timed_step_past_its_limit(self):
        # Step 2's build, while step 1 prints, takes longer than step 1's time plus test_no_print_s
        w, admin, plan = self.timed_world(3)
        fake = w.printers[0]

        def on(event):
            if isinstance(event, TestEvent) and event.status == "building" and event.step_index == 2:
                fake.latency_s = 0.15
            elif isinstance(event, TestEvent) and event.status == "stopping":
                fake.latency_s = 0.0
        result = admin.print_run(plan).run(on)
        self.assertEqual((result.ok, result.steps_done, result.printing_off, result.cleared), (False, 0, True, True))
        self.assertEqual(result.msg.english(), "The test ran past its time limit, so it was stopped.")
        end = self.logs(admin, "hx-admin", entry_type="test_end", ref=plan.run_id)[0]
        self.assertEqual(end["reason"], "time_limit")
        self.assertClean(fake)

    # ---- the test_start entry's ranges (send.md §17.7)

    def test_start_entry_ranges(self):
        ranges = TestRanges(vary="delay", start=480, stop=480, step=10, bold=[2])
        for source in ("keyword", "plan"):
            with self.subTest(source):
                w = self.world()
                admin = self.admin()
                w.printers[0].override("PMCNT", rising())
                plan = self.plan(admin, ranges)
                if source == "plan":
                    plan.ranges = ranges            # once TestPlan carries them, the §6.7 constructor logs them
                    run = hx_send.TestPrintRun(plan=plan, **admin.kw)
                else:
                    run = admin.print_run(plan, ranges=ranges)
                self.assertTrue(run.run(lambda e: None).ok)
                start = self.logs(admin, "hx-admin", entry_type="test_start", ref=plan.run_id)[0]
                self.assertEqual(start["ranges"], to_json(ranges))
                self.assertEqual(from_json(TestRanges, start["ranges"]), ranges)


# --------------------------------------------------------------------------
# The log helpers, and safety across a whole session
# --------------------------------------------------------------------------

class LogTests(unittest.TestCase):
    def test_design_round_trip(self):
        from hx_model import DesignItem, MessageDesign
        design = MessageDesign(slot=9, name="EXCO1-PKO01-FDNP-H9", empty=False,
                               items=[DesignItem(tag="CTEXT", pairs=[("TXT", "Lot 4417"), ("LANG", "4")])],
                               settings={"PRNSPD": "2400"}, width=256, height=160, black_pixels=12,
                               preview_bmp=b"BM\x00\x01", read_at=1.5, tn="42", xid=3)
        d = hx_send.design_to_log(design)
        self.assertNotIn("preview_bmp", d)
        self.assertEqual(d["preview_b64"], "Qk0AAQ==")
        self.assertEqual(hx_send.design_from_log(d), design)

    def test_a_damaged_send_entry_is_a_value_error(self):
        # §17.2: result_from_log's times are checked as hx_model checks a file. A hand-edited 400-digit
        # int raised OverflowError, which the result window's (KeyError, ValueError) didn't catch
        entry = {"send_id": "sd_8a7b6c5d4e", "started_at": 1793275200.5, "finished_at": 1793275230, "machines": []}
        r = hx_send.result_from_log(entry)
        self.assertEqual((r.started_at, r.finished_at, r.data_as_of), (1793275200.5, 1793275230.0, None))
        self.assertEqual(hx_send.result_from_log(dict(entry, started_at=None, data_as_of=5)).started_at, 0.0)
        for key in ("started_at", "finished_at", "data_as_of"):
            for value in (10 ** 400, float("nan"), float("inf"), "soon", True, {"t": 1}):
                with self.subTest(key=key, value=str(value)[:12]):
                    with self.assertRaises(ValueError):
                        hx_send.result_from_log(dict(entry, **{key: value}))
        # Its lists, objects and pictures too: a TypeError from iterating or indexing one escaped the window
        result = {"machine_id": "mc_0123456789", "colour": RED, "ok": False, "step": STEP_LOCK,
                  "previous_preview_b64": "",
                  "readbacks": [{"slot": 9, "name": "A", "empty": False, "preview_b64": ""}]}
        self.assertEqual(hx_send.result_from_log(dict(entry, machines=[{"result": result}])).machines[0].step,
                         STEP_LOCK)
        for name, machines in (("machines", 5), ("machines", {"result": result}), ("machine", ["x"]),
                               ("machine", [None]), ("result", [{"result": 5}]), ("result", [{"result": None}]),
                               ("readbacks", [{"result": dict(result, readbacks=5)}]),
                               ("readback", [{"result": dict(result, readbacks=[5])}]),
                               ("preview_b64", [{"result": dict(result, readbacks=[dict(result["readbacks"][0],
                                                                                        preview_b64=5)])}]),
                               ("previous_preview_b64", [{"result": dict(result, previous_preview_b64=[1])}])):
            with self.subTest(damaged=name, machines=str(machines)[:30]):
                with self.assertRaises(ValueError):
                    hx_send.result_from_log(dict(entry, machines=machines))

    def test_notes_order(self):
        self.assertEqual(hx_send._ordered(["cancelled", "no_lock", "pc_time", "no_lock"]),
                         ["pc_time", "no_lock", "cancelled"])
        self.assertEqual(hx_send.LOG_TYPES["test_step"], "hx-admin")


_CHECKED = threading.local()       # .lock: the PrinterLock last checked on this thread (LeaseTests)


class JumpClock(Clock):
    """The real clock, plus jumps: a PC that froze (asleep, a paused VM) finds time moved on at once."""

    def __init__(self):
        self.offset = 0.0

    def time(self):
        return time.time() + self.offset

    def monotonic(self):
        return time.monotonic() + self.offset

    def sleep(self, seconds, cancel=None):
        return SYSTEM_CLOCK.sleep(seconds, cancel)

    def jump(self, seconds):
        self.offset += seconds


class Seen:
    """A PrinterClient seen through: each printer call with its deadline, and the lease end of the lock
    last checked on that thread when the call was made. before(name, cmd) runs first, if given."""
    CALLS = ("read", "query", "command", "check_uid", "read_twice", "load_slot", "fonts", "firmware")

    def __init__(self, client, calls, timings, before=None):
        self._client, self._calls, self._timings, self._before = client, calls, timings, before

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if name not in self.CALLS:
            return attr

        def call(*args, **kw):
            if self._before is not None:
                self._before(name, args)
            lock = getattr(_CHECKED, "lock", None)
            lease = lock._last_ok + self._timings.lock_stale_s / 2 if lock is not None and lock.state == "held" \
                else None
            self._calls.append((name, args[0] if args else None, kw.get("deadline"), lease))
            return attr(*args, **kw)
        return call


class LeaseTests(Base):
    """send.md §10.5: every printer call made under a lock carries the lock's lease end as its deadline,
    so a call that passed its guard and then waited (the gate, a frozen PC) is refused, not sent late."""

    def watch(self, pc, before=None):
        calls = []
        real = pc.clients.client
        pc.clients.client = lambda printer: Seen(real(printer), calls, self.timings, before)
        real_check = hx_share.PrinterLock.check

        def check(lock, budget_s=0.0):
            _CHECKED.lock = lock
            return real_check(lock, budget_s)
        patcher = mock.patch.object(hx_share.PrinterLock, "check", check)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_every_printer_call_under_a_lock_has_the_lease_deadline(self):
        self.quiet()
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        fake, mid = w.printers[0], self.mid()
        fake.override("PMCNT", rising())
        calls = self.watch(pc)
        result, _, _ = self.send(pc)
        self.assertTrue(result.machines[0].ok)
        pc.pipeline.confirm(result.send_id, [mid], True)
        self.assertTrue(pc.actions.stop([mid], lambda e: None).machines[0].ok)
        self.assertTrue(pc.actions.check_resume(mid).allowed)
        self.assertTrue(pc.actions.resume([mid], lambda e: None).machines[0].ok)
        self.assertTrue(pc.actions.set_clocks([mid], lambda e: None).machines[0].ok)
        admin = self.pc("admin", admin=True, sender=False)
        self.own_poll(admin)
        test_calls = self.watch(admin)
        plan = plan_test(mid, w.ids["film"], TestRanges(vary="delay", start=480, stop=490, step=10),
                         admin.data.current, admin.identity)
        self.assertTrue(admin.print_run(plan).run(lambda e: None).ok)
        calls += test_calls
        self.assertGreater(len(calls), 60)
        self.assertEqual({name for name, *_ in calls}, {"check_uid", "read", "command", "fonts", "load_slot",
                                                         "read_twice"})
        for name, arg, deadline, lease in calls:
            with self.subTest(call=name, arg=arg):
                self.assertIsNotNone(lease)                     # made under a held lock
                self.assertIsNotNone(deadline)
                self.assertLessEqual(deadline, lease + 1e-6)
        self.assertClean(fake)

    def test_a_frozen_pc_sends_nothing_after_a_takeover(self):
        # The guard before PRINTMSG passed; then the PC froze longer than the lease (asleep, a paused VM)
        # and another PC took the stale lock over. On waking the command is refused: nothing reaches the
        # printer, and the Send ends red with the lock's text. Before, PRINTMSG went out after the takeover
        self.quiet()
        clock = JumpClock()
        w = self.world(clock=clock)
        a, b = self.pc(), self.pc("akim")
        self.own_poll(a)
        fake, pid = w.printers[0], w.ids["printer1"]
        taker = b.store.lock(pid, purpose="send", run_id=new_id("sd"), line_id=w.ids["line"], machine_id=self.mid())
        self.addCleanup(taker.release)
        marks = {}

        def freeze(name, args):
            if name != "command" or args[0].tag != "PRINTMSG" or marks:
                return
            for _ in range(4):                                  # A's keeper stops by itself once frozen
                b.store.read_lock(pid)
                if (b.store.lock_age(pid) or 0.0) >= self.timings.lock_stale_s:
                    break
                clock.jump(self.timings.lock_stale_s + 1)
            self.assertEqual(taker.acquire(), "held")
            marks["requests"] = len(fake.requests)
        self.watch(a, before=freeze)
        result, _, _ = self.send(a)
        r = result.machines[0]
        self.assertIn("requests", marks)
        self.assertEqual(len(fake.requests), marks["requests"])  # nothing after the takeover
        self.assertNotIn("PRINTMSG", fake.writes)
        self.assertEqual((r.ok, r.step, r.msg.english()), (False, STEP_SWITCH, LOCK_LOST))
        self.assertEqual(a.store.read_lock(pid).token, taker.token)
        self.assertClean(fake)

    def test_a_frozen_test_end_writes_nothing_after_a_takeover(self):
        # The test print's end procedure: the guard before PRINT 0 passed, then the admin PC froze past the
        # lease and B took the printer over and wrote the line's status. PRINT 0 is refused at its deadline
        # (nothing reaches the printer), and nothing is written. Before, the refusal was an ordinary
        # failure there, so the rows step ran and the admin PC wrote "Stopped, needs a Send" over B's Send
        self.quiet()
        clock = JumpClock()
        w = self.world(clock=clock)
        admin, b = self.pc("admin", admin=True, sender=False), self.pc("akim")
        self.own_poll(admin)
        fake, pid, mid, line_id = w.printers[0], w.ids["printer1"], self.mid(), w.ids["line"]
        fake.override("PMCNT", rising())
        taker = b.store.lock(pid, purpose="send", run_id=new_id("sd"), line_id=line_id, machine_id=mid)
        self.addCleanup(taker.release)
        marks = {}

        def stale_for_b():
            for _ in range(4):
                b.store.read_lock(pid)
                if (b.store.lock_age(pid) or 0.0) >= self.timings.lock_stale_s:
                    return
                clock.jump(self.timings.lock_stale_s + 1)

        def freeze(name, args):
            if name != "command" or args[0].tag != "PRINT" or ">0<" not in args[0].xml or marks:
                return
            stale_for_b()
            # The admin PC's keeper stops by itself at its next refresh, past the lease. But its thread runs on
            # while this PC "freezes", and a refresh already writing may still land over B's new file (the
            # residual README sign-off 5 accepts; not what this test is about), so B takes over only once the
            # keeper has ended, and the lock has stood still for lock_stale_s on B's clock
            keeper = _CHECKED.lock._keeper
            if keeper is not None:
                keeper.join(10 * self.timings.lock_refresh_s + 2 * self.timings.share_timeout_s)
            marks["keeper_ended"] = keeper is None or not keeper.is_alive()
            stale_for_b()
            self.assertEqual(taker.acquire(), "held")
            b.store.write_line_status(LineStatus(line_id=line_id, send_id="sd_bbbbbbbbbb"))
            marks.update(requests=len(fake.requests), line=b.store.read_line_status(line_id),
                         expected=b.store.read_expected(mid))
        self.watch(admin, before=freeze)
        plan = plan_test(mid, w.ids["film"], TestRanges(vary="delay", start=480, stop=490, step=10),
                         admin.data.current, admin.identity)
        result = admin.print_run(plan).run(lambda e: None)
        self.assertIn("requests", marks)
        self.assertTrue(marks["keeper_ended"])
        self.assertEqual(len(fake.requests), marks["requests"])  # nothing after the takeover
        self.assertFalse(result.ok)
        self.assertEqual(to_json(b.store.read_line_status(line_id)), to_json(marks["line"]))
        self.assertFalse(marks["line"].stopped_needs_send)
        self.assertEqual(to_json(b.store.read_expected(mid)), to_json(marks["expected"]))
        self.assertEqual(admin.store.read_lock(pid).token, taker.token)


class SafetyTests(Base):
    def test_whitelist(self):
        w = self.world([{}, {"placement": "encoder"}])
        pc = self.pc()
        self.own_poll(pc)
        result, _, _ = self.send(pc, (1, 2))
        self.assertTrue(all(r.ok for r in result.machines))
        ids = [self.mid(1), self.mid(2)]
        pc.pipeline.confirm(result.send_id, ids, True)
        pc.actions.set_clocks(ids, lambda e: None)
        pc.actions.stop(ids, lambda e: None)
        resumed = pc.actions.resume(ids, lambda e: None)
        self.assertTrue(all(r.ok for r in resumed.machines))
        result, _, _ = self.send(pc, (1, 2))
        self.assertTrue(all(r.ok for r in result.machines))
        admin = self.pc("admin", admin=True, sender=False)
        w.printers[0].override("PMCNT", rising())
        plan = plan_test(self.mid(1), w.ids["film"], TestRanges(vary="delay", start=480, stop=490, step=10),
                         admin.data.current, admin.identity)
        self.assertTrue(admin.print_run(plan).run(lambda e: None).ok)
        for fake in w.printers:
            self.assertLessEqual(set(fake.writes), set(ALLOWED_WRITE_TAGS))
            self.assertNotIn("SETPPARAM", fake.writes)
            self.assertNotIn("SAVEMSG to the printing slot", fake.violations)
        self.assertClean()


@unittest.skipUnless(HX_STATE, "hx_state isn't built yet")
class PollerJudgementTests(Base):
    """What the poller makes of the expected states this module writes (states.md §3.4)."""

    def classify(self, pc, mid):
        current = pc.data.current
        machine = current.machines[mid]
        running = running_of(pc.client().load_slot(0))
        facts = hx_state.Facts(reachable=True, printing=True, running=running)
        return hx_state.classify(facts, None, pc.store.read_expected(mid), None, machine,
                                 current.lines[machine.line_id], current)

    def test_recorded_after_an_outage_is_unconfirmed(self):
        self.quiet()
        self.world()
        pc = self.pc()
        self.own_poll(pc)
        self.send(pc)
        pc.store.fs.down = True
        result, _, _ = self.send(pc)
        pc.store.fs.down = False
        pc.store.probe_now()
        self.assertTrue(wait_until(lambda: pc.store.available))
        self.assertEqual(pc.pipeline.record_offline(pc.data.find_send(result.send_id)), [self.mid()])
        self.assertEqual(self.classify(pc, self.mid()).code, "unconfirmed")
        self.assertClean()

    def test_failed_after_a_rejection(self):
        w = self.world()
        pc = self.pc()
        self.own_poll(pc)
        result, _, _ = self.send(pc)
        pc.pipeline.confirm(result.send_id, [self.mid()], False)
        w.printers[0].override("HXUI", "1110")
        self.send(pc)
        w.printers[0].override("HXUI", None)
        outcome = self.classify(pc, self.mid())
        self.assertEqual(outcome.code, "failed_before_switch")
        self.assertEqual({a.kind for a in outcome.alerts}, {"send_failed"})
        self.assertClean()


if __name__ == "__main__":
    unittest.main()
