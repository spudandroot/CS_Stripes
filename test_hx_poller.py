"""Tests for hx_poller: the request budget, P written every tick, pausing for a lock, the two-reading rule
against a fake printer, unreachable printers and backoff, crash recovery, ink and clock warnings,
something else talking to the printer, daily checks, the daylight-saving clock set, standalone mode,
waiting recipients, handover, internal errors, the status watcher and acknowledgements, the message
check, ink names, keeping P fresh, the reachability probe and the start race.

Logic tests drive the poller without threads (Poller.run_once) on a FakeClock: every share
operation and printer request is real, only the waiting is simulated. Threaded tests use scaled
timings and sleep at most a fraction of a second at a time. Run: python -m unittest -v"""

import calendar
import dataclasses
import random
import tempfile
import threading
import time
import unittest
from unittest import mock

import hx_fake
import hx_message
import hx_poller
import hx_protocol
import hx_share
import hx_state
import hx_time
from hx_core import SYSTEM_CLOCK, EventBus, FakeClock, Msg
from hx_model import (ALLOWED_WRITE_TAGS, AMBER, RED, TIMINGS, Ack, AuthFailed, BoardUpdate, DesignItem,
                      ExpectedState, FailureInfo, HeartbeatInfo, MessageDesign, PollerUpdate, PollStatus)

EDT = lambda utc: -14400.0               # a fixed offset: the fake printer's clock doesn't follow the host
# Logic tests: default timings on a FakeClock, with short real network and share limits
LOGIC = dataclasses.replace(TIMINGS, http_timeout_s=0.1, read_retry_s=0.01, share_timeout_s=2.0)
NAME = "EXCO1-PKO07-BBBB-M9"
OLD_NAME = "EXCO1-PKO07-AAAA-K0"
ORIGIN = "Product of USA and Canada"
SID, SID2 = "sd_8a7b6c5d4e", "sd_9b8c7d6e5f"
RUN_ID = "tp_1c2d3e4f5a"
JSMITH, AKIM = "EXAMPLE\\jsmith", "EXAMPLE\\akim"
CHANGE = float(calendar.timegm((2026, 11, 1, 6, 0, 0)))      # EDT to EST
DST = lambda utc: -14400.0 if utc < CHANGE else -18000.0


def design(slot, name, origin=ORIGIN, extra=""):
    items = [DesignItem(tag="CTEXT", pairs=[("OBJID", "10001"), ("POS", "0,0"), ("TXT", origin), ("FONTIND", "2"),
                                            ("SIZE", "3"), ("MARG", "0,0")])]
    if extra:
        items.append(DesignItem(tag="CTEXT", pairs=[("OBJID", "10002"), ("POS", "0,40"), ("TXT", extra),
                                                    ("FONTIND", "2"), ("SIZE", "3"), ("MARG", "0,0")]))
    settings = {"ROTAT": "0", "MIRR": "0", "REVRS": "0", "AUTREP": "0", "DELAY": "480000", "DLYMM": "0",
                "PRNSPD": "2400", "BOLD": "1", "WDTH": "1", "QUANT": "1", "QDELAY": "0", "QDLYMM": "0",
                "RESMOD": "0", "RESNAM": "UltraFine", "ENCST": "0", "ENCSCL": "100"}
    return MessageDesign(slot=slot, name=name, empty=False, height=160, items=items, settings=settings)


def install(fake, slot=9, name=NAME, origin=ORIGIN, *, printing=True, current=True):
    with fake.lock:
        fake.slots[slot] = design(slot, name, origin)
        fake.slot_style[slot] = "verbose"
        if current:
            fake.current_slot = slot
            fake.printing = printing


def replace_text(old, new):
    """A web_reply_hook changing a CTEXT TXT in every Load result."""
    def hook(body):
        return body.replace(f"<TXT>{old}</TXT>".encode(), f"<TXT>{new}</TXT>".encode())
    return hook


class PC:
    """One simulated PC: its identity, store, data, client pool with its own gates, time source and poller."""

    def __init__(self, sim, name, *, pid=1, fs=None, local=False, time_source=None, watcher=False):
        world, clock, timings = sim.world, sim.clock, sim.timings
        self.sim = sim
        self.identity = world.identity(name, pid=pid)
        self.store = hx_share.ShareStore(world.share_root, identity=self.identity, clock=clock,
                                         fs=fs if fs is not None else world.fs.view(), timings=timings, version="test")
        self.local = hx_share.LocalStore(world.paths(self.identity), clock=clock)
        self.bus = EventBus()
        self.data = hx_share.DataService(store=self.store, local=self.local, bus=self.bus, identity=self.identity,
                                         clock=clock, timings=timings)
        self.data.run_once()
        self.logs = []
        original = self.data.append_log

        def append_log(kind, entry):
            self.logs.append(dict(entry))
            return original(kind, entry)
        self.data.append_log = append_log
        self.gates = hx_protocol.GateRegistry()
        self.clients = hx_protocol.ClientPool(key=hx_protocol.release_key(), clock=clock, gates=self.gates,
                                              timings=timings)
        self.time = time_source or sim.time_source()
        self.poller = hx_poller.Poller(store=self.store, data=self.data, clients=self.clients, time=self.time,
                                       bus=self.bus, identity=self.identity, version="test", clock=clock,
                                       timings=timings, local=self.local if local else None)
        self.watcher = hx_poller.StatusWatcher(store=self.store, data=self.data, poller=self.poller, bus=self.bus,
                                               identity=self.identity, clock=clock, timings=timings)
        self.key = self.identity.key

    def entries(self, kind):
        return [e for e in self.logs if e["type"] == kind]

    def worker(self, printer_id=None):
        printer_id = printer_id or self.sim.pid
        return self.poller._workers.get(printer_id)


class Sim:
    """A FakeWorld on a FakeClock, simulated PCs and a step-by-step run."""

    def __init__(self, test, *, printers=None, clock=None, timings=LOGIC, install_message=True):
        self.test = test
        self.clock = clock or FakeClock()
        self.timings = timings
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.world = hx_fake.FakeWorld(tmp.name, printers=printers or [{"offset_fn": EDT}], clock=self.clock,
                                       timings=timings)
        test.addCleanup(self.world.close)
        self.fake = self.world.printers[0]
        self.mid, self.pid = self.world.ids["machine1"], self.world.ids["printer1"]
        self.observer = self.world.store(self.world.identity("observer", sender=False))
        self.sender = self.world.store(self.world.identity("jsmith"))
        self.pcs = []
        self.offset_fn = (printers or [{}])[0].get("offset_fn", EDT)
        if install_message:
            install(self.fake)

    def time_source(self, offset_fn=None):
        clock = self.clock
        ts = hx_time.TimeSource(["time.example.local"], clock=clock, offset_fn=offset_fn or self.offset_fn,
                                query=lambda host, port=123, timeout_s=2.0: clock.time())
        ts.now(fresh=True)
        return ts

    def pc(self, name, **kw):
        pc = PC(self, name, **kw)
        self.pcs.append(pc)
        self.test.addCleanup(pc.store.heartbeat().remove)
        return pc

    def run(self, seconds, *, pcs=None, until=None):
        """Advance the FakeClock event by event; True as soon as until() holds."""
        pcs = self.pcs if pcs is None else pcs
        end = self.clock.monotonic() + seconds
        while True:
            due = min(pc.poller.run_once() for pc in pcs) if pcs else end
            if until is not None and until():
                return True
            now = self.clock.monotonic()
            if now >= end:
                return until is None
            self.clock.advance(max(0.001, min(due, end) - now))

    def p(self, mid=None):
        return self.observer.read_poll(mid or self.mid)

    def code(self):
        p = self.p()
        return p.state.code if p is not None and p.state is not None else ""

    def read_back(self, slot):
        """The fake's message as a Load reads it; the fake's observations are cleared afterwards."""
        client = hx_protocol.PrinterClient("127.0.0.1", self.fake.uid, hx_protocol.make_token("123321"),
                                           port=self.fake.port, clock=SYSTEM_CLOCK, gates=hx_protocol.GateRegistry(),
                                           timings=LOGIC)
        rb = client.load_slot(slot)
        self.fake.reset_observations()
        return rb

    def expect(self, mode="running", *, slot=9, **kw):
        """E as a Send would have written it, for the message in slot (its read-back fingerprint)."""
        rb = self.read_back(slot)
        e = ExpectedState(machine_id=self.mid, mode=mode, printing_expected=mode == "running", send_id=SID,
                          message_name=rb.name, slot=slot, fingerprint=hx_message.fingerprint(rb),
                          origin_line=hx_message.origin_from_design(rb), confirmed=mode == "running",
                          confirmed_by=JSMITH if mode == "running" else "", sender=JSMITH)
        e = dataclasses.replace(e, **kw)
        self.sender.write_expected(e)
        return e

    def requests(self, since=None, kind=None, tag=None):
        with self.fake.lock:
            reqs = list(self.fake.requests)
        return [r for r in reqs if (since is None or r["t"] >= since) and (kind is None or r["kind"] == kind)
                and (tag is None or r["tag"] == tag)]


class Base(unittest.TestCase):
    def sim(self, **kw):
        sim = Sim(self, **kw)
        self.addCleanup(self.check_fakes, sim)
        return sim

    def check_fakes(self, sim):
        # protocol.md §15.8: every test that drives the poller against a fake ends with no violations,
        # one request at a time and only whitelisted writes, on every fake of its world
        for fake in sim.world.printers:
            with fake.lock:
                violations, in_flight, writes = list(fake.violations), fake.max_in_flight, set(fake.writes)
            self.assertEqual(violations, [])
            self.assertLessEqual(in_flight, 1)
            self.assertLessEqual(writes, set(ALLOWED_WRITE_TAGS))

    def steady(self, sim, seconds=40):
        self.assertTrue(sim.run(seconds), "the run ended early")
        self.assertEqual(sim.code(), "running_confirmed")

    def no_violations(self, sim):
        self.assertEqual(sim.fake.violations, [])
        self.assertEqual(sim.fake.max_in_flight, 1)


# --------------------------------------------------------------------------
# 1 to 3: the budget, P every tick, pausing for a lock
# --------------------------------------------------------------------------

class BudgetTests(Base):
    def test_budget(self):
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim, 120)               # started, settled, daily check done
        t0 = sim.clock.monotonic()
        sim.run(600)
        times = [r["t"] for r in sim.requests(since=t0)]
        self.assertLess(len(times) / 600.0, 1.0)
        self.assertGreater(len(times) / 600.0, 0.5)
        worst = max(sum(1 for u in times if t <= u < t + 5.0) for t in times)
        self.assertLessEqual(worst, 15)
        self.no_violations(sim)
        self.assertEqual(sim.fake.writes, [])
        self.assertEqual(sim.code(), "running_confirmed")
        # The slow set once a minute, with one Load
        loads = [r for r in sim.requests(since=t0, kind="sme")]
        self.assertTrue(9 <= len(loads) <= 11, len(loads))
        self.assertEqual(len(sim.requests(since=t0, tag="HXUI")), len(loads))

    def test_p_every_tick(self):
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim)

        def counters(n):
            out = []
            for _ in range(n):
                sim.run(LOGIC.poll_fast_s)
                out.append(sim.p().counter)
            return out

        polling = counters(4)
        self.assertEqual(polling, sorted(set(polling)))
        lock = sim.observer.lock(sim.pid, purpose="send", run_id=SID)
        self.assertEqual(lock.acquire(), "held")
        paused = counters(3)
        self.assertEqual(paused, sorted(set(paused)))
        self.assertGreater(paused[0], polling[-1])
        self.assertEqual(sim.p().paused_for_lock, lock.token)
        lock.release()
        sim.fake.go_silent()
        self.assertTrue(sim.run(60, until=lambda: sim.code() == "off"))
        backing_off = counters(4)
        self.assertEqual(backing_off, sorted(set(backing_off)))
        sim.fake.go_silent(False)

    def test_pause_for_a_lock(self):
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim)
        lock = sim.observer.lock(sim.pid, purpose="send", run_id=SID)
        lock.acquire()
        n = len(sim.requests())
        sim.run(LOGIC.poll_fast_s)
        self.assertEqual(sim.p().paused_for_lock, lock.token)
        sim.run(20)
        self.assertEqual(len(sim.requests()), n)
        self.assertEqual(sim.observer.check_poller_paused(sim.mid, lock.token), "paused")
        lock.release()
        t0 = sim.clock.monotonic()
        sim.run(LOGIC.poll_fast_s)
        after = sim.requests(since=t0)
        self.assertEqual(after[0]["tag"], "HXUI")
        self.assertTrue(any(r["kind"] == "sme" for r in after))
        self.assertEqual(sim.p().paused_for_lock, "")
        self.no_violations(sim)


# --------------------------------------------------------------------------
# 4 to 9: the two-reading rule, unreachable printers, crash recovery, ink and clock
# --------------------------------------------------------------------------

class ReadingTests(Base):
    def test_drift_needs_two_loads(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        # One corrupted Load commits nothing
        used = []

        def once(body):
            if not used and b"<FCDOC>" in body:
                used.append(1)
                return replace_text(ORIGIN, "Product of Mexico")(body)
            return body
        sim.fake.web_reply_hook = once
        sim.run(70)
        self.assertEqual(used, [1])
        self.assertEqual(sim.code(), "running_confirmed")
        self.assertFalse([e for e in a.entries("alert_started")])
        # The content changed behind Stripes' back, same name: drift after two Loads agree
        sim.fake.web_reply_hook = None
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        t0 = sim.clock.monotonic()
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
        self.assertGreaterEqual(len([r for r in sim.requests(since=t0, kind="sme")]), 2)
        p = sim.p()
        self.assertEqual([x.kind for x in p.alerts], ["drift"])
        self.assertTrue(p.alerts[0].sound)
        self.assertEqual(p.running.origin_line, "Product of Mexico")
        self.no_violations(sim)

    def test_a_stray_prin(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        answers = ["0"]
        sim.fake.override("PRIN", lambda: answers.pop() if answers else None)
        sim.run(30)
        self.assertEqual(sim.code(), "running_confirmed")
        self.assertEqual(a.entries("alert_started"), [])
        answers.extend(["0", "0"])
        self.assertTrue(sim.run(10, until=lambda: sim.code() == "printing_off"))
        self.assertEqual([x.kind for x in sim.p().alerts], ["printing_off"])
        self.assertEqual(answers, [])

    def test_unreachable_while_printing(self):
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim)
        sim.fake.print_bags(10)
        sim.run(LOGIC.poll_fast_s * 2)
        self.assertEqual(sim.p().bags_recent, 10)
        sim.fake.go_silent()
        t0 = sim.clock.monotonic()
        self.assertTrue(sim.run(40, until=lambda: sim.code() == "unreachable_printing"))
        took = sim.clock.monotonic() - t0
        self.assertTrue(10 <= took <= 22, took)
        p = sim.p()
        self.assertEqual([x.kind for x in p.alerts], ["went_silent"])
        self.assertIsNotNone(p.unreachable_since)
        # Backoff: one PRIN 5, 10, 30, 60, 60 s after the end of the previous attempt
        mark = sim.clock.monotonic()
        sim.run(5 + 10 + 30 + 60 + 60 + 2)
        attempts = []
        for r in sim.requests(since=mark - 0.5, tag="PRIN"):     # from the confirming second reading on
            if not attempts or r["t"] - attempts[-1] > 1.0:
                attempts.append(r["t"])
        gaps = [round(b - a, 1) for a, b in zip(attempts, attempts[1:])]
        self.assertEqual(gaps[:5], [5.0, 10.0, 30.0, 60.0, 60.0])
        self.assertEqual(sim.requests(since=mark, tag="MSGNAM"), [])
        # Answering again: back after the second reading, starting with HXUI and a Load
        sim.fake.go_silent(False)
        self.assertTrue(sim.run(120, until=lambda: sim.code() == "running_confirmed"))
        self.assertEqual(sim.p().alerts, [])
        self.assertIsNone(sim.p().unreachable_since)
        self.no_violations(sim)

    def test_switched_off_without_bags_is_off(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        sim.fake.go_silent()
        self.assertTrue(sim.run(40, until=lambda: sim.code() == "off"))
        self.assertEqual(sim.p().alerts, [])
        self.assertEqual([e["to_code"] for e in a.entries("state_changed")][-1], "off")
        sim.fake.go_silent(False)

    def test_acknowledged_went_silent(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        sim.fake.print_bags(10)
        sim.run(10)
        sim.fake.go_silent()
        self.assertTrue(sim.run(40, until=lambda: sim.code() == "unreachable_printing"))
        alert = sim.p().alerts[0]
        sim.observer.write_ack(Ack(alert_id=alert.id, by=JSMITH, computer="FLOOR02", at=sim.clock.time()))
        self.assertTrue(sim.run(10, until=lambda: sim.code() == "off"))
        self.assertEqual(sim.p().alerts, [])
        ended = a.entries("alert_ended")
        self.assertEqual([(e["alert_id"], e["acked_by"]) for e in ended], [(alert.id, JSMITH)])
        self.assertEqual([e["alert_id"] for e in a.entries("alert_acknowledged")], [alert.id])
        sim.fake.go_silent(False)

    def crash(self, *, e_mode="switch_pending", lock_purpose="send", setup=None, run_id=SID, **e_kw):
        sim = self.sim()
        install(sim.fake, 1, OLD_NAME, "Product of USA", current=True)
        old = sim.read_back(1)
        install(sim.fake, 9, NAME, ORIGIN, current=False)
        e = sim.expect(e_mode, slot=9, previous_message_name=OLD_NAME, previous_fingerprint=hx_message.fingerprint(old),
                       confirmed=False, **e_kw)
        if setup:
            setup(sim)
        holder = sim.world.store(sim.world.identity("akim"))
        lock = holder.lock(sim.pid, purpose=lock_purpose, run_id=run_id)
        self.assertEqual(lock.acquire(), "held")       # never refreshed: its PC has stopped
        a = sim.pc("bob")
        sim.run(30)
        self.assertEqual(sim.p().paused_for_lock, lock.info.token)
        self.assertTrue(sim.run(60, until=lambda: sim.p().paused_for_lock == "" and sim.code() not in ("", "checking")))
        sim.run(5)
        return sim, a, e

    def test_crash_recovery(self):
        def running_new(sim):
            with sim.fake.lock:
                sim.fake.current_slot = 9
        sim, a, _ = self.crash(setup=running_new)
        p = sim.p()
        self.assertEqual((p.state.code, p.state.colour), ("interrupted_new", AMBER))
        (alert,) = p.alerts
        self.assertEqual((alert.kind, alert.colour, alert.sound), ("send_interrupted", AMBER, True))
        self.assertEqual(alert.recipients, ["user:" + AKIM, "user:" + JSMITH])
        self.assertEqual(alert.id, f"{sim.mid}.send_interrupted.{SID}.{int(alert.started_at * 1000)}")

        sim, a, _ = self.crash()                        # still running the old message (slot 1)
        self.assertEqual(sim.code(), "interrupted_old")

        def printing_off(sim):
            with sim.fake.lock:
                sim.fake.current_slot = 9
                sim.fake.printing = False
        sim, a, _ = self.crash(setup=printing_off)
        self.assertEqual(sim.code(), "interrupted_unknown")

        def other_content(sim):
            with sim.fake.lock:
                sim.fake.current_slot = 9
            sim.fake.web_reply_hook = replace_text(ORIGIN, "Product of Mexico")
        sim, a, _ = self.crash(setup=other_content)
        p = sim.p()
        self.assertEqual((p.state.code, p.state.colour), ("interrupted_unknown", RED))

        sim, a, _ = self.crash(e_mode="test_running", lock_purpose="test", run_id=RUN_ID,
                               test={"run_id": RUN_ID, "by": AKIM, "step": 3, "of": 10})
        p = sim.p()
        self.assertEqual(p.state.code, "test_orphaned")
        self.assertEqual([(x.kind, x.ref) for x in p.alerts], [("test_still_printing", RUN_ID)])
        self.no_violations(sim)

    def test_test_still_printing_again_rings_again(self):
        # A stale test lock with printing on: test_still_printing rings and is acknowledged; printing is
        # switched off (it ends), then on again at the printer, so the test message prints again. The new
        # occurrence has its own id, so the old ack (ack files are never removed) doesn't silence it.
        sim, a, _ = self.crash(e_mode="test_running", lock_purpose="test", run_id=RUN_ID,
                               test={"run_id": RUN_ID, "by": AKIM, "step": 3, "of": 10})
        (first,) = sim.p().alerts
        sim.observer.write_ack(Ack(alert_id=first.id, by=JSMITH, computer="FLOOR02", at=sim.clock.time()))
        self.assertTrue(sim.run(15, until=lambda: sim.p().alerts[0].acked_by == JSMITH))
        a.watcher.run_once()
        self.assertEqual(hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True), [])
        with sim.fake.lock:
            sim.fake.printing = False
        self.assertTrue(sim.run(20, until=lambda: sim.code() == "test_interrupted"))
        self.assertEqual(sim.p().alerts, [])
        with sim.fake.lock:
            sim.fake.printing = True
        self.assertTrue(sim.run(20, until=lambda: sim.code() == "test_orphaned"))
        sim.run(10)
        (second,) = sim.p().alerts
        self.assertEqual((second.kind, second.ref), ("test_still_printing", RUN_ID))
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.acked_by, "")
        a.watcher.run_once()
        ringing = hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)
        self.assertEqual([x.id for x in ringing], [second.id])
        started = [e["alert"]["id"] for e in a.entries("alert_started")]
        self.assertEqual(started.count(second.id), 1)

    def test_ink_and_clock(self):
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim)
        sim.fake.ink_level = 2
        self.assertTrue(sim.run(70, until=lambda: [x.kind for x in sim.p().alerts] == ["low_ink"]))
        alert = sim.p().alerts[0]
        self.assertEqual((alert.sound, alert.colour, sim.code()), (False, AMBER, "running_confirmed"))
        sim.fake.ink_level = 4
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "ink_out"))
        self.assertTrue(sim.p().alerts[0].sound)
        sim.fake.ink_level = 1
        sim.fake.clock_offset_s = 200.0
        self.assertTrue(sim.run(70, until=lambda: [x.kind for x in sim.p().alerts] == ["clock_off"]))
        self.assertEqual(sim.p().alerts[0].msg.values["minutes"], 3)
        self.assertEqual(sim.code(), "running_confirmed")


# --------------------------------------------------------------------------
# 10, 11, 19: something else talking, daily checks, ink names
# --------------------------------------------------------------------------

FOREIGN = b"<SM><TYPE>Load</TYPE><TN>834854</TN><XID>2</XID><MSNO>1</MSNO></SM>"


class ChecksTests(Base):
    @mock.patch.object(hx_poller, "FOREIGN_CLEAR_S", 700.0)     # 1800 s (ConstantsTests); shorter to run
    def test_foreign_client(self):
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim)
        for _ in range(3):
            loads = len(sim.requests(kind="sme"))
            sim.fake.leave_in_buffer(FOREIGN)
            self.assertTrue(sim.run(70, until=lambda: len(sim.requests(kind="sme")) > loads))
        self.assertTrue(sim.run(10, until=lambda: sim.p().foreign_client))
        alert = next(x for x in sim.p().alerts if x.kind == "foreign_client")
        self.assertEqual((alert.sound, alert.recipients), (False, ["admins", "monitor"]))
        self.assertEqual(alert.msg.values, {"printer": "Printer 1"})
        sim.run(hx_poller.FOREIGN_CLEAR_S - 120)
        self.assertTrue(sim.p().foreign_client)
        self.assertTrue(sim.run(200, until=lambda: not sim.p().foreign_client))
        self.assertEqual([x.kind for x in sim.p().alerts], [])

    def test_daily_check(self):
        sim = self.sim(printers=[{"placement": "encoder", "offset_fn": EDT}])
        sim.expect()
        a = sim.pc("akim")
        self.assertTrue(sim.run(150, until=lambda: a.entries("daily_check")))
        (entry,) = a.entries("daily_check")
        daily = sim.p().daily
        self.assertEqual(entry["daily"], daily)
        self.assertEqual(daily["values"]["ENWHD"], 56)
        self.assertEqual(daily["encoder_ref"], {"ENWHD": 56, "ENPPR": 5000})
        self.assertEqual(daily["fonts"], list(hx_fake.STANDARD_FONTS))
        self.assertEqual((daily["firmware"], daily["errors"], daily["machine_revision"]), ("69.75.10", [], 1))
        self.assertEqual(entry["changed"]["ENWHD"], [None, 56])
        # One item per cycle
        per_cycle = [r["tag"] for r in sim.requests() if r["tag"] in hx_poller.DAILY_ITEMS + hx_poller.ENCODER_DAILY_ITEMS]
        self.assertEqual(per_cycle, list(hx_poller.DAILY_ITEMS + hx_poller.ENCODER_DAILY_ITEMS))
        # The encoder changed at the printer: the next day's check raises encoder_changed
        with sim.fake.lock:
            sim.fake.encoder["ENWHD"] = 60
        sim.clock.set_wall(sim.clock.time() + hx_poller.DAILY_S)
        self.assertTrue(sim.run(150, until=lambda: "encoder_changed" in [x.kind for x in sim.p().alerts]))
        self.assertEqual(sim.p().daily["values"]["ENWHD"], 60)
        self.assertEqual(a.entries("daily_check")[-1]["changed"], {"ENWHD": [56, 60]})
        alert = next(x for x in sim.p().alerts if x.kind == "encoder_changed")
        self.assertTrue(alert.sound)
        # An administrator records the new wheel: the check runs again and the alert ends
        admin = sim.world.store(sim.world.identity("admin", admin=True, sender=False))
        machine = a.data.current.machines[sim.mid]
        hx_share.write_record(admin, dataclasses.replace(machine, encoder_wheel_mm=60.0, revision=2))
        admin.write_json("config/hx/changes.json", {"file_type": "changes", "counter": 2, "written_at": 0.0,
                                                    "written_by": "EXAMPLE\\admin", "last": None})
        a.data.run_once()
        self.assertTrue(sim.run(30, until=lambda: "encoder_changed" not in [x.kind for x in sim.p().alerts]))
        self.assertTrue(sim.run(150, until=lambda: sim.p().daily.get("machine_revision") == 2))
        self.assertEqual(sim.p().daily["encoder_ref"]["ENWHD"], 60)
        self.no_violations(sim)

    def test_ink_names(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim, 70)
        self.assertEqual(sim.p().ink_name, "Example Black")
        worker = a.worker()
        answers = ["Example White", "Unknown Ink"]
        sim.fake.override("INKT", lambda: answers.pop() if answers else None)
        before = len(worker.events)
        self.assertTrue(sim.run(70, until=lambda: not answers))
        sim.run(5)
        self.assertEqual(sim.p().ink_name, "Example White")
        self.assertEqual(len(worker.events), before + 1)
        sim.fake.override("INKT", "Unknown Ink")
        before = len(worker.events)
        t0 = sim.clock.monotonic()
        sim.run(65)
        self.assertEqual(len(sim.requests(since=t0, tag="INKT")), 2)
        self.assertEqual(sim.p().ink_name, "Example White")
        self.assertEqual(len(worker.events), before)


# --------------------------------------------------------------------------
# 12: the daylight-saving clock set
# --------------------------------------------------------------------------

class DstTests(Base):
    def dst_sim(self, start=CHANGE - 120):
        sim = self.sim(clock=FakeClock(start), printers=[{"offset_fn": DST}])
        sim.expect()
        return sim

    def test_one_td_after_the_change(self):
        sim = self.dst_sim()
        a = sim.pc("akim")
        self.steady(sim, 60)
        self.assertEqual(sim.fake.writes, [])
        order = []
        real_read_lock, real_command = a.store.read_lock, hx_protocol.PrinterClient.command

        def read_lock(printer_id):
            order.append("lock")
            return real_read_lock(printer_id)

        def command(client, cmd, **kw):
            order.append(cmd.tag)
            return real_command(client, cmd, **kw)
        with mock.patch.object(a.store, "read_lock", read_lock), \
                mock.patch.object(hx_protocol.PrinterClient, "command", command), \
                mock.patch.object(hx_time.TimeSource, "now", side_effect=AssertionError("a worker asked the time server")):
            t0 = sim.clock.monotonic()
            self.assertTrue(sim.run(120, until=lambda: a.entries("dst_clock_set")))
            sim.run(30)
        self.assertEqual(sim.fake.writes, ["TD"])
        self.assertEqual(order[order.index("TD") - 1], "lock")
        tags = [r["tag"] for r in sim.requests(since=t0) if r["kind"] == "autopilot"]
        i = tags.index("TD")
        self.assertEqual(tags[i - 2:i], ["DATE", "TIME"])
        self.assertEqual(tags[i + 1:i + 3], ["DATE", "TIME"])
        (entry,) = a.entries("dst_clock_set")
        self.assertEqual(set(entry) - {"type", "machine_id", "printer_id", "line_id", "poller"},
                         {"change", "change_at", "previous_offset_s", "offset_s", "ok", "skipped", "msg", "clock"})
        self.assertEqual((entry["change"], entry["change_at"], entry["previous_offset_s"], entry["offset_s"]),
                         ("20261101-18000", CHANGE, -14400.0, -18000.0))
        self.assertEqual((entry["ok"], entry["skipped"], entry["msg"]), (True, False, None))
        clock = entry["clock"]
        self.assertEqual(set(clock), {"sent", "before", "after", "difference_s", "source", "server", "offset_s"})
        self.assertLessEqual(abs(clock["difference_s"]), hx_message.CLOCK_SET_TOLERANCE_S)
        self.assertEqual(clock["source"], "server")
        dst = sim.p().dst
        self.assertEqual((dst["change"], dst["ok"], dst["skipped"], dst["sent"], dst["by"]),
                         ("20261101-18000", True, False, True, a.key))
        self.assertLess(abs(sim.p().clock_offset_s), 90)
        # A second poller taking over doesn't write again
        b = sim.pc("bob")
        a.poller.stop()
        sim.pcs.remove(a)
        self.assertTrue(sim.run(60, until=lambda: sim.p().poller == b.key))
        sim.run(60)
        self.assertEqual(sim.fake.writes, ["TD"])
        self.assertEqual(b.entries("dst_clock_set"), [])
        self.no_violations(sim)

    def test_a_lock_delays_it(self):
        sim = self.dst_sim(CHANGE - 60)
        a = sim.pc("akim")
        self.steady(sim, 40)
        lock = sim.observer.lock(sim.pid, purpose="send", run_id=SID)
        lock.acquire()
        sim.run(50)                                     # past the change, still locked
        self.assertEqual(sim.fake.writes, [])
        lock.release()
        self.assertTrue(sim.run(30, until=lambda: sim.fake.writes == ["TD"]))
        sim.run(30)
        self.assertEqual(sim.fake.writes, ["TD"])
        self.assertTrue(a.entries("dst_clock_set")[0]["ok"])

    def test_refused_td_isnt_repeated(self):
        sim = self.dst_sim()
        a = sim.pc("akim")
        sim.fake.override("TD", "3")
        self.assertTrue(sim.run(300, until=lambda: a.entries("dst_clock_set")))
        sim.run(120)
        self.assertEqual(sim.fake.writes, ["TD"])
        (entry,) = a.entries("dst_clock_set")
        self.assertEqual((entry["ok"], entry["skipped"]), (False, False))
        self.assertEqual(entry["msg"]["text"], "{printer} refused {command} (code {code}).")
        self.assertEqual((sim.p().dst["sent"], sim.p().dst["ok"]), (True, False))

    def test_catch_up(self):
        sim = self.dst_sim(CHANGE - 60)
        sim.clock.advance(60 + 7200)                    # every PC was off at the change
        a = sim.pc("akim")
        self.assertTrue(sim.run(120, until=lambda: sim.fake.writes == ["TD"]))
        self.assertTrue(a.entries("dst_clock_set")[0]["ok"])

        sim = self.dst_sim(CHANGE - 60)
        sim.clock.advance(60 + 25 * 3600)               # more than a day later: none
        a = sim.pc("akim")
        sim.run(120)
        self.assertEqual(sim.fake.writes, [])
        self.assertEqual(a.entries("dst_clock_set"), [])
        self.assertIn("clock_off", [x.kind for x in sim.p().alerts])

    def test_skipped_when_already_right(self):
        sim = self.dst_sim(CHANGE - 60)
        sim.clock.advance(65)
        sim.fake.clock_offset_s = -3600.0               # someone already set it
        a = sim.pc("akim")
        self.assertTrue(sim.run(120, until=lambda: a.entries("dst_clock_set")))
        sim.run(30)
        self.assertEqual(sim.fake.writes, [])
        (entry,) = a.entries("dst_clock_set")
        self.assertEqual((entry["ok"], entry["skipped"], entry["clock"]["sent"], entry["clock"]["after"]),
                         (True, True, None, None))
        self.assertEqual((sim.p().dst["skipped"], sim.p().dst["sent"]), (True, False))

    def test_recorded_before_it_is_sent(self):
        sim = self.dst_sim(CHANGE - 60)
        sim.clock.advance(65)
        views = {}
        a = sim.pc("akim", fs=views.setdefault("akim", sim.world.fs.view(down=False)))
        b = sim.pc("bob", fs=views.setdefault("bob", sim.world.fs.view(down=False)))
        seen = []

        def td():
            p = sim.p()
            seen.append(dict(p.dst))
            # The poller that sends it loses the share from now on
            views["akim" if p.poller == a.key else "bob"].down = True
            return "0"
        sim.fake.override("TD", td)
        self.assertTrue(sim.run(200, until=lambda: seen))
        self.assertEqual((seen[0]["change"], seen[0]["sent"], seen[0]["ok"]), ("20261101-18000", True, False))
        sender_key = seen and sim.p().dst.get("by")
        self.assertTrue(sim.run(120, until=lambda: sim.p().poller != sender_key))
        sim.run(120)
        self.assertEqual(sim.fake.writes, ["TD"])
        self.assertEqual(sim.p().dst["change"], "20261101-18000")

    def test_only_the_time_thread_asks_the_server(self):
        threads = []
        real_now = hx_time.TimeSource.now

        def now(self_, **kw):
            threads.append(threading.current_thread().name)
            return real_now(self_, **kw)
        timings = TIMINGS.scaled(0.02)
        sim = self.sim(clock=SYSTEM_CLOCK, timings=timings)
        sim.expect()
        a = sim.pc("akim", time_source=hx_time.TimeSource(["time.example.local"], clock=SYSTEM_CLOCK,
                                                           query=lambda host, port=123, timeout_s=2.0: time.time(),
                                                           cache_s=timings.poll_slow_s))
        with mock.patch.object(hx_time.TimeSource, "now", now):
            a.poller.start()
            self.addCleanup(a.poller.stop)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (threads and a.store.read_poll(sim.mid)
                                                       and a.store.read_poll(sim.mid).clock_offset_s is not None):
                time.sleep(0.05)
            a.poller.stop()
        self.assertTrue(threads)
        self.assertEqual(set(threads), {"hx-time"})


# --------------------------------------------------------------------------
# 13 and 21: standalone mode
# --------------------------------------------------------------------------

class StandaloneTests(Base):
    def test_standalone(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        install(sim.fake, 10, "SOMETHING-ELSE", "Product of Mexico")    # what a Send would call drift
        updates = []
        a.bus.subscribe(PollerUpdate, updates.append)
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        t_down = sim.clock.monotonic()
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        self.assertGreaterEqual(sim.clock.monotonic() - t_down, LOGIC.lock_stale_s)
        t0 = sim.clock.monotonic()
        sim.run(95)
        reqs = sim.requests(since=t0)
        self.assertEqual([r for r in reqs if r["kind"] != "autopilot"], [])      # no Load
        self.assertEqual({r["tag"] for r in reqs}, {"HXUI", "PRIN", "MSGNAM", "PMCNT"})
        checks = [r["t"] for r in reqs if r["tag"] == "HXUI"]
        gaps = [round(b - c, 1) for c, b in zip(checks, checks[1:])]
        self.assertTrue(all(g in (1.0, 29.0, 30.0, 31.0) or g < 2 for g in gaps), gaps)
        statuses = a.poller.local_statuses()
        st = statuses[sim.mid]
        self.assertTrue(st.standalone)
        self.assertEqual(st.poller, a.key)
        self.assertEqual(st.state.code, "unknown_while_down")
        self.assertNotIn("drift", [x.kind for x in st.alerts])
        a.bus.pump()
        self.assertTrue(any(u.mode == "standalone" and u.next_check_s is not None for u in updates))
        # Refresh now: once, then not within refresh_now_min_s
        self.assertTrue(a.poller.refresh_now())
        self.assertFalse(a.poller.refresh_now())
        n = len(sim.requests())
        sim.run(0.5)
        self.assertGreater(len(sim.requests()), n)
        # Logged on this PC, marked standalone
        self.assertTrue(any(e.get("standalone") for e in a.entries("state_changed")))
        sim.world.fs.down = False

    def test_idle_without_a_monitor(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        sim.world.fs.down = True
        sim.run(10)
        n = len(sim.requests())
        sim.run(120)
        self.assertEqual(a.poller.mode, "idle")
        self.assertEqual(len(sim.requests()), n)
        self.assertFalse(a.poller.refresh_now())
        sim.world.fs.down = False

    def standalone_after_a_confirm(self, sim):
        """E confirmed shortly before the outage (after the coordinator's last E pass), then standalone."""
        e1 = sim.expect(confirmed=False, confirmed_by="")
        a = sim.pc("akim")
        self.assertTrue(sim.run(40))
        self.assertEqual(sim.code(), "unconfirmed")
        a.watcher.run_once()
        sim.sender.write_expected(dataclasses.replace(e1, confirmed=True, confirmed_by=JSMITH))
        sim.run(5)
        a.watcher.run_once()
        self.assertEqual(machine_on_board(a, sim.mid).code, "running_confirmed")
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        return a

    def test_board_shows_the_standalone_check(self):
        # The check and the board judge against the same cached E, so "E not judged yet" never hides
        # what the check found behind a green "Printing" (§6.12 item 3)
        sim = self.sim()
        a = self.standalone_after_a_confirm(sim)
        with sim.fake.lock:
            sim.fake.printing = False
        sim.run(65)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual(st.state.code, "not_printing")
        self.assertEqual(st.expected_counter, a.store.cached_expected(sim.mid).counter)
        a.watcher.run_once()
        mv = machine_on_board(a, sim.mid)
        self.assertEqual((mv.code, mv.colour), ("not_printing", AMBER))

    def test_board_shows_a_message_changed_while_down(self):
        sim = self.sim()
        a = self.standalone_after_a_confirm(sim)
        install(sim.fake, 10, "SOMETHING-ELSE", "Product of Mexico")    # changed at the printer's panel
        sim.run(65)
        self.assertEqual(a.poller.local_statuses()[sim.mid].state.code, "unknown_while_down")
        a.watcher.run_once()
        mv = machine_on_board(a, sim.mid)
        self.assertEqual((mv.code, mv.colour), ("unknown_while_down", AMBER))
        row = next(r for r in hx_state.floor_board_rows(a.watcher.latest) if r.machine_id == sim.mid)
        self.assertEqual(row.colour, AMBER)
        self.assertEqual(row.status.text, hx_state.STATE_TEXTS["unknown_while_down"])

    def test_on_time_checks_never_show_not_monitored(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        a.poller._rng = random.Random(6)
        self.steady(sim)
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        st = a.poller._standalone
        self.assertTrue(sim.run(LOGIC.standalone_poll_s + 1,
                                until=lambda: a.poller.local_statuses().get(sim.mid) is not None))
        seen = []
        for _ in range(4):
            a.watcher.run_once()                    # the watcher sees the check's counter
            sim.clock.advance(st.next_check - sim.clock.monotonic())
            a.watcher.run_once()                    # a pass just before the next check stores its result
            mv = machine_on_board(a, sim.mid)
            seen.append((mv.code, mv.colour))
            a.poller.run_once()
        self.assertEqual(set(seen), {("running_confirmed", "green")})
        # Checks that stop are still caught: NOT MONITORED once the next check is not_monitored_s late
        with mock.patch.object(hx_poller._StandaloneCheck, "_check", lambda self: None):
            a.watcher.run_once()
            sim.run(LOGIC.standalone_poll_s + LOGIC.not_monitored_s - 1)
            a.watcher.run_once()
            self.assertEqual(machine_on_board(a, sim.mid).code, "running_confirmed")
            sim.run(2)
            a.watcher.run_once()
            self.assertEqual(machine_on_board(a, sim.mid).code, "not_monitored")

    def test_schedule_moves_on_at_each_check(self):
        # A wake-up a hair before the due time (FakeClock rounding) still moves the schedule on, so no
        # second check follows at once
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        a.poller._rng = mock.Mock(uniform=lambda lo, hi: 0.2707176553276)
        self.steady(sim)
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        t0 = sim.clock.monotonic()
        self.assertTrue(sim.run(5, until=lambda: a.poller.local_statuses().get(sim.mid) is not None))
        self.assertGreater(a.poller._standalone.next_check - sim.clock.monotonic(), LOGIC.standalone_poll_s - 2)
        sim.run(LOGIC.standalone_poll_s - 2)
        self.assertEqual(len(sim.requests(since=t0, tag="HXUI")), 1)

    def board_samples(self, sim, a, n, step):
        out = []
        for _ in range(n):
            sim.run(step)
            a.watcher.run_once()
            mv = machine_on_board(a, sim.mid)
            out.append((round(sim.clock.monotonic() - self.t0), mv.code, mv.colour))
        return out

    def test_printer_and_share_down_together_is_never_green(self):
        # A floor-network outage takes the share and the printer down at once: the first standalone
        # check has no prev and gets no reply, so it must not judge the machine green "Printing"
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        sim.fake.go_silent()
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        self.t0 = sim.clock.monotonic()
        seen = self.board_samples(sim, a, 8, 10)
        self.assertEqual([s for s in seen if s[2] == "green"], [], seen)
        st = a.poller.local_statuses()[sim.mid]
        self.assertFalse(st.reachable)
        self.assertEqual(st.state.code, "off")
        self.assertEqual(seen[-1][1:], ("off", "grey"))

    def test_printer_that_stops_answering_in_standalone_leaves_green_at_the_next_check(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        self.assertTrue(sim.run(LOGIC.standalone_poll_s + 1,
                                until=lambda: a.poller.local_statuses().get(sim.mid) is not None))
        sim.run(LOGIC.standalone_poll_s + 1)
        self.assertEqual(a.poller.local_statuses()[sim.mid].state.code, "running_confirmed")
        sim.fake.go_silent()
        self.t0 = sim.clock.monotonic()
        seen = self.board_samples(sim, a, 16, 5)
        greens = [s[0] for s in seen if s[2] == "green"]
        # At most one check interval, plus the reads and the second reading of that check
        self.assertLessEqual(max(greens, default=0), LOGIC.standalone_poll_s + 10, seen)
        self.assertFalse(a.poller.local_statuses()[sim.mid].reachable)
        self.assertEqual(seen[-1][1:], ("off", "grey"))
        # One dropped check that its second reading contradicts changes nothing
        sim.fake.go_silent(False)
        self.assertTrue(sim.run(LOGIC.standalone_poll_s + 10,
                                until=lambda: a.poller.local_statuses()[sim.mid].state.code == "running_confirmed"))
        self.assertTrue(a.poller.local_statuses()[sim.mid].reachable)

    def test_printer_printing_bags_goes_silent_in_standalone(self):
        # It was printing bags at the last checks: the first check without a reply (and its second
        # reading) raises went_silent, red with sound, as share mode does after its three failed cycles
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.poller.set_monitor_open(True, True)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        for _ in range(3):
            sim.fake.print_bags(20)
            sim.run(LOGIC.standalone_poll_s)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual(st.state.code, "running_confirmed")
        self.assertGreater(st.bags_recent or 0, 0)
        sim.fake.go_silent()
        self.assertTrue(sim.run(LOGIC.standalone_poll_s + 10,
                                until=lambda: a.poller.local_statuses()[sim.mid].state.code == "unreachable_printing"))
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual([(x.kind, x.sound, x.colour) for x in st.alerts], [("went_silent", True, RED)])
        a.watcher.run_once()
        self.assertEqual([x.kind for x in hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)],
                         ["went_silent"])

    def test_one_dropped_standalone_read_is_checked_again(self):
        # The first read of a check gets no reply but the second reading a second later does: the
        # machine stays as it was (the two-reading rule), and nothing is raised
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        self.assertTrue(sim.run(LOGIC.standalone_poll_s + 1,
                                until=lambda: a.poller.local_statuses().get(sim.mid) is not None))
        st = a.poller._standalone
        sim.clock.advance(st.next_check - sim.clock.monotonic() - 0.5)
        drops = []

        def twice():                # the read and the client's own retry
            if len(drops) < 2:
                drops.append(1)
                return sim.fake.SILENT
            return None
        sim.fake.override("HXUI", twice)
        n = len(a.entries("state_changed"))
        sim.run(10)
        self.assertEqual(drops, [1, 1])
        self.assertTrue(sim.requests(tag="PRIN", since=sim.clock.monotonic() - 10))
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual(st.state.code, "running_confirmed")
        self.assertTrue(st.reachable)
        self.assertEqual(len(a.entries("state_changed")), n)
        self.assertEqual(st.alerts, [])

    def green_standalone(self, sim):
        """A confirmed machine, then the share down with the monitor open: standalone with a green check."""
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        self.assertTrue(sim.run(LOGIC.standalone_poll_s + 1,
                                until=lambda: a.poller.local_statuses().get(sim.mid) is not None))
        sim.run(LOGIC.standalone_poll_s + 1)
        self.assertEqual(a.poller.local_statuses()[sim.mid].state.code, "running_confirmed")
        return a

    def test_off_then_no_reply_is_checked_again_at_once(self):
        # A check reads printing off and its second reading gets no reply: the two disagree, so nothing
        # commits (§3.8 step 6). The check then checks again at once instead of leaving the machine
        # green "Printing" for another standalone_poll_s
        sim = self.sim()
        a = self.green_standalone(sim)
        st = a.poller._standalone
        sim.clock.advance(st.next_check - sim.clock.monotonic() - 0.2)
        replies = []

        def prin():
            replies.append(1)
            return "0" if len(replies) == 1 else sim.fake.SILENT
        sim.fake.override("PRIN", prin)
        t0 = sim.clock.monotonic()
        sim.run(8)
        a.watcher.run_once()
        mv = machine_on_board(a, sim.mid)
        self.assertNotEqual(mv.colour, "green")
        self.assertEqual((mv.code, a.poller.local_statuses()[sim.mid].reachable), ("off", False))
        self.assertEqual(len(sim.requests(since=t0, tag="HXUI")), 2)        # the check, and once more
        self.assertLess(sim.clock.monotonic() - t0, LOGIC.standalone_poll_s)

    def test_printing_never_read_in_standalone_is_not_green(self):
        # PRIN answers busy at every check from the first one (no prev): standalone checks never load
        # the message, so without a PRIN reading the machine is amber, not green "Printing"
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        sim.fake.override("PRIN", "6")
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        self.t0 = sim.clock.monotonic()
        seen = self.board_samples(sim, a, 8, 10)
        self.assertEqual([s for s in seen if s[2] == "green"], [], seen)
        self.assertEqual(seen[-1][1:], ("unknown_while_down", "amber"))
        # PRIN answers again: green after the check and its second reading
        sim.fake.override("PRIN", None)
        self.assertTrue(sim.run(LOGIC.standalone_poll_s + 5,
                                until=lambda: a.poller.local_statuses()[sim.mid].state.code == "running_confirmed"))

    def floor_row(self, a, sim):
        return next(r for r in hx_state.floor_board_rows(a.watcher.latest) if r.machine_id == sim.mid)

    def test_message_name_never_read_in_standalone_is_not_green(self):
        # MSGNAM answers busy at every check from the first one: standalone checks never load the
        # message, so without a name the machine is amber with its origin unknown, also once the
        # printer has been switched at its panel to another message with another origin
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        sim.fake.override("MSGNAM", "7")
        a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        install(sim.fake, 10, "SOMETHING-ELSE", "Product of Mexico")
        self.t0 = sim.clock.monotonic()
        seen = self.board_samples(sim, a, 8, 10)
        self.assertEqual([s for s in seen if s[2] == "green"], [], seen)
        self.assertEqual(seen[-1][1:], ("unknown_while_down", "amber"))
        self.assertEqual((self.floor_row(a, sim).origin_line, self.floor_row(a, sim).colour), ("", AMBER))
        # MSGNAM answers again, with E's message back: green after the check and its second reading
        install(sim.fake)
        sim.fake.override("MSGNAM", None)
        self.assertTrue(sim.run(LOGIC.standalone_poll_s + 5,
                                until=lambda: a.poller.local_statuses()[sim.mid].state.code == "running_confirmed"))
        a.watcher.run_once()
        self.assertEqual(self.floor_row(a, sim).origin_line, ORIGIN)

    def test_message_name_unreadable_after_a_good_check_is_not_green(self):
        # After green checks the printer is switched at its panel to a message whose name fails MSGNAM's
        # reply check (a name "3" reads as a reply code): the name an earlier check read doesn't keep
        # the machine green with E's origin
        sim = self.sim()
        a = self.green_standalone(sim)
        install(sim.fake, 10, "3", "Product of Mexico")
        self.t0 = sim.clock.monotonic()
        seen = self.board_samples(sim, a, 4, 10)
        self.assertEqual(seen[-1][1:], ("unknown_while_down", "amber"), seen)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual((st.state.code, st.message_name), ("unknown_while_down", None))
        self.assertEqual((self.floor_row(a, sim).origin_line, self.floor_row(a, sim).colour), ("", AMBER))
        self.assertEqual(st.alerts, [])

    def test_a_client_that_cant_be_made_commits_at_once(self):
        # No client for the printer (a password that can't be unscrambled): nothing can be read again,
        # so login_failed commits at the check, as in share mode, instead of the old green never moving
        sim = self.sim()
        a = self.green_standalone(sim)

        def no_client(printer):
            raise AuthFailed(Msg("Stripes can't log in to this printer."))
        a.poller.clients.client = no_client
        sim.run(LOGIC.standalone_poll_s + 1)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual((st.state.code, st.problem), ("login_failed", "login_failed"))
        a.watcher.run_once()
        self.assertEqual(machine_on_board(a, sim.mid).colour, RED)

    def test_standalone_waits_for_lock_stale(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        a.poller.set_monitor_open(True, True)
        sim.world.fs.down = True
        modes = []
        t0 = sim.clock.monotonic()
        sim.run(LOGIC.lock_stale_s - 1, until=lambda: modes.append(a.poller.mode) or False)
        self.assertEqual(set(modes), {"idle"})
        self.assertTrue(sim.run(10, until=lambda: a.poller.mode == "standalone"))
        self.assertGreaterEqual(sim.clock.monotonic() - t0, LOGIC.lock_stale_s)
        self.assertTrue(sim.run(40, until=lambda: sim.requests(tag="PMCNT")))
        sim.run(1)
        self.assertEqual({r["tag"] for r in sim.requests()}, {"HXUI", "PRIN", "MSGNAM", "PMCNT"})
        sim.world.fs.down = False


# --------------------------------------------------------------------------
# 14 to 18: waiting recipients, handover, internal errors, the watcher, the message check
# --------------------------------------------------------------------------

class CoordinationTests(Base):
    def test_waiting_recipients(self):
        sim = self.sim()
        sim.expect(sender="EXAMPLE\\bob")
        a = sim.pc("akim")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(80, until=lambda: sim.code() == "drift"))
        sim.run(10)
        waiting = a.entries("alert_waiting")
        self.assertEqual(sorted(e["user"] for e in waiting), ["EXAMPLE\\bob", JSMITH])
        sim.run(10)
        self.assertEqual(len(a.entries("alert_waiting")), 2)
        self.assertEqual(sorted(sim.p().alerts[0].waiting), ["EXAMPLE\\bob", JSMITH])
        # jsmith signs in on another PC
        other = sim.world.store(sim.world.identity("jsmith"))
        other.heartbeat().beat(can_poll=False, last_printer_reach=None, polling=[], monitor_open=False,
                               floor_board=False)
        self.addCleanup(other.heartbeat().remove)
        sim.run(10)
        (reached,) = a.entries("alert_reached")
        self.assertEqual((reached["user"], reached["computer"]), (JSMITH, "PC-JSMITH"))
        self.assertEqual(sim.p().alerts[0].waiting, ["EXAMPLE\\bob"])

    def ranked(self, sim):
        a, b = sim.pc("akim"), sim.pc("bob")
        return (a, b) if assign_order(sim, a, b)[0] == a.key else (b, a)

    def test_a_worker_frozen_mid_cycle_never_polls_over_a_takeover(self):
        # Rank 0's PC freezes (sleep, a VM pause) in the middle of a cycle, for longer than stale_s, and
        # rank 1 takes the printer over meanwhile. When rank 0 wakes, its last P write is stale_s / 2
        # old: it reads P first, finds rank 1's, and neither sends nor writes P until rank 1 has handed
        # the printer back. Frozen right after a reply; after the last request, before the cycle's own
        # write at the end; and in a daylight-saving clock set, before it records the TD in P
        real_judge = hx_state.judge
        for where in ("PRIN", "judge", "clock set"):
            with self.subTest(freeze=where):
                if where == "clock set":
                    sim = self.sim(clock=FakeClock(CHANGE - 120), printers=[{"offset_fn": DST}])
                else:
                    sim = self.sim()
                sim.expect()
                r0, r1 = self.ranked(sim)
                sent = {r0.key: [], r1.key: []}
                freeze = {}

                def frozen():
                    freeze.clear()
                    sim.run(40.0, pcs=[r1])                 # only rank 1 runs meanwhile
                    freeze["woke"] = sim.clock.monotonic()

                def in_clock_set():
                    w = r0.worker()
                    return w is not None and w._dst_due(r0.poller._dst)
                for pc in (r0, r1):
                    real_client = pc.clients.client

                    def client(printer, real_client=real_client, pc=pc):
                        c = real_client(printer)
                        if not getattr(c, "_recorded", False):
                            for name in ("read", "command", "load_slot", "fonts", "firmware"):
                                def call(*args, _fn=getattr(c, name), **kw):
                                    sent[pc.key].append(sim.clock.monotonic())
                                    out = _fn(*args, **kw)
                                    if pc is r0 and ((freeze.get("at") == "PRIN" and args[:1] == ("PRIN",))
                                                     or (freeze.get("at") == "clock set" and args[:1] == ("TIME",)
                                                         and in_clock_set())):
                                        frozen()
                                    return out
                                setattr(c, name, call)
                            c._recorded = True
                        return c
                    pc.clients.client = client

                def judge(prev, *args, **kw):
                    out = real_judge(prev, *args, **kw)
                    if freeze.get("at") == "judge" and prev is not None and prev.poller == r0.key:
                        frozen()
                    return out
                self.steady(sim, 60)
                self.assertEqual((sim.p().poller, r1.worker()), (r0.key, None))
                if where == "clock set":
                    self.assertTrue(sim.run(120, until=lambda: sim.clock.time() >= CHANGE - 3))
                freeze["at"] = where
                with mock.patch.object(hx_state, "judge", judge):
                    self.assertTrue(sim.run(30, pcs=[r0], until=lambda: "woke" in freeze))
                    woke = freeze["woke"]
                    sim.run(LOGIC.poll_fast_s + 1, pcs=[r0])    # the rest of that cycle, and the next tick
                took = r1.worker()
                self.assertTrue(took is not None and took.rank_checked and not took.done)
                self.assertEqual(r1.entries("polling_started")[-1]["reason"], "takeover")
                self.assertEqual([t for t in sent[r0.key] if t >= woke], [])
                self.assertEqual(sim.p().poller, r1.key)
                # Both PCs run again: rank 1 sees rank 0's heartbeat and hands back; rank 0 polls on only then
                self.assertTrue(sim.run(30, until=lambda: r1.worker() is None and sim.p().poller == r0.key))
                last = r1.entries("polling_stopped")[-1]
                self.assertEqual((last["reason"], last["yielded_to"]), ("handover", r0.key))
                self.assertTrue(sim.run(10, until=lambda: any(t >= woke for t in sent[r0.key])))
                self.assertLess(max(sent[r1.key]), min(t for t in sent[r0.key] if t >= woke))
                self.assertTrue(sim.run(30, until=lambda: sim.code() == "running_confirmed"))
                if where == "clock set":
                    # One TD, by rank 1, which made the clock set while it polled; rank 0 continues from its P
                    self.assertEqual(sim.fake.writes, ["TD"])
                    self.assertEqual((len(r1.entries("dst_clock_set")), r0.entries("dst_clock_set")), (1, []))
                    self.assertEqual(sim.p().dst["by"], r1.key)
                self.no_violations(sim)

    def test_handover_and_stop(self):
        sim = self.sim()
        sim.expect()
        r0, r1 = self.ranked(sim)
        self.steady(sim)
        self.assertEqual(sim.p().poller, r0.key)
        r0.poller.stop()
        sim.pcs.remove(r0)
        p = sim.p()
        self.assertEqual(p.yielded_to, r1.key)
        self.assertNotIn(r0.key, [h.key for h in sim.observer.live_heartbeats()])
        self.assertEqual(r0.entries("polling_stopped")[-1]["reason"], "stop")
        self.assertTrue(sim.run(10, until=lambda: sim.p().poller == r1.key))
        self.assertEqual(r1.entries("polling_started")[-1]["reason"], "handover")
        # The rank-0 PC returns (a new process): it starts only once P names it in yielded_to
        back = sim.pc(r0.identity.user.split("\\")[1], pid=2)
        starts = []
        real_start = back.poller._start_worker

        def start_worker(printer_id, machine_id, reason):
            starts.append((reason, sim.p().yielded_to))
            return real_start(printer_id, machine_id, reason)
        back.poller._start_worker = start_worker
        self.assertTrue(sim.run(30, until=lambda: sim.p().poller == back.key))
        self.assertEqual(starts, [("handover", back.key)])
        last = r1.entries("polling_stopped")[-1]
        self.assertEqual((last["reason"], last["yielded_to"]), ("handover", back.key))
        self.no_violations(sim)

    def test_internal_error(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        real = hx_state.judge
        calls = []

        def judge(*args, **kw):
            if not calls:
                calls.append(1)
                raise RuntimeError("a bug")
            return real(*args, **kw)
        with mock.patch.object(hx_state, "judge", judge), mock.patch("traceback.print_exc"):
            sim.run(LOGIC.poll_fast_s)
            p = sim.p()
            self.assertEqual((p.state.code, p.state.colour), ("internal_error", RED))
            (entry,) = a.entries("internal_error")
            self.assertEqual(entry["error"], "RuntimeError: a bug")
            self.assertIn("Traceback", entry["traceback"])
            self.assertTrue(sim.run(10, until=lambda: sim.code() == "running_confirmed"))

    def broken_writes(self, pc):
        """write_poll raises a bug (not a ShareError) for the next n calls: pc.broken[0] = n."""
        real = pc.store.write_poll
        pc.broken = [0]

        def write_poll(status):
            if pc.broken[0]:
                pc.broken[0] -= 1
                raise RuntimeError("a bug while writing poll.json")
            return real(status)
        pc.store.write_poll = write_poll

    def test_internal_error_when_p_cant_be_written(self):
        # The tick's write and internal_error's own write both fail: the worker carries on (§6.4)
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.broken_writes(a)
        self.steady(sim)
        w = a.worker()
        with mock.patch("traceback.print_exc"):
            a.broken[0] = 2
            sim.run(LOGIC.poll_fast_s)
            self.assertEqual(a.broken[0], 0)
            self.assertIs(a.worker(), w)
            self.assertFalse(w.done)
            self.assertEqual(len(a.entries("internal_error")), 1)
            before = sim.p().counter
            sim.run(3 * LOGIC.poll_fast_s)
            self.assertGreater(sim.p().counter, before)
            self.assertEqual(sim.code(), "running_confirmed")
        self.no_violations(sim)

    def test_worker_thread_survives_failing_writes(self):
        timings = dataclasses.replace(TIMINGS.scaled(0.02), http_timeout_s=0.09)
        sim = self.sim(clock=SYSTEM_CLOCK, timings=timings)
        sim.expect()
        a = sim.pc("akim")
        self.broken_writes(a)
        a.poller.start()
        self.addCleanup(a.poller.stop)

        def wait_for(cond, seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not cond():
                time.sleep(0.02)
            return cond()
        self.assertTrue(wait_for(lambda: sim.code() == "running_confirmed", 3.0))
        w = a.worker()
        with mock.patch("traceback.print_exc"):
            a.broken[0] = 2
            self.assertTrue(wait_for(lambda: a.broken[0] == 0, 1.0))
            before = sim.p().counter
            self.assertTrue(wait_for(lambda: sim.p().counter > before + 2, 1.0))
        self.assertTrue(w.thread.is_alive())
        self.assertFalse(w.done)
        self.assertIs(a.worker(), w)
        self.assertTrue(wait_for(lambda: sim.code() == "running_confirmed", 1.0))

    def test_a_dead_worker_thread_is_replaced(self):
        # Belt and braces: a worker thread that ended without finishing is reaped, and a new worker
        # starts, instead of the printer staying listed as polled with nobody writing P
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        old = a.worker()
        ended = threading.Thread(target=lambda: None)
        ended.start()
        ended.join()
        old.thread = ended
        self.assertTrue(sim.run(2 * LOGIC.heartbeat_s, until=lambda: a.worker() not in (None, old)))
        self.assertEqual(a.entries("polling_started")[-1]["reason"], "restart")
        self.assertTrue(sim.run(20, until=lambda: sim.code() == "running_confirmed" and sim.p().poller == a.key))
        self.no_violations(sim)

    def test_watcher_and_acks(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        b = sim.pc("bob")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(80, until=lambda: sim.code() == "drift"))
        sim.run(LOGIC.heartbeat_s)                       # the coordinators have read the new P
        poller_pc = a if sim.p().poller == a.key else b
        other = b if poller_pc is a else a
        for pc in (a, b):
            pc.watcher.run_once()
        ringing = lambda pc: hx_state.sounding_alerts(pc.watcher.latest, pc.identity, floor_board=True)
        self.assertTrue(ringing(a) and ringing(b))
        boards = []
        a.bus.subscribe(BoardUpdate, boards.append)
        a.watcher.acknowledge([alert.id for alert in ringing(a)])
        self.assertEqual(ringing(a), [])                 # at once, before any share access
        a.bus.pump()
        self.assertTrue(boards)
        a.watcher.run_once()                             # writes the ack file
        self.assertIsNotNone(sim.observer.read_ack(sim.p().alerts[0].id))
        other.watcher.run_once()                         # within one watch_s
        self.assertEqual(ringing(other), [])
        self.assertEqual(ringing(a), [])
        sim.run(10)
        self.assertEqual(sim.p().alerts[0].acked_by, a.identity.user)
        self.assertEqual(len(poller_pc.entries("alert_acknowledged")), 1)
        self.assertEqual(sim.code(), "drift")             # the problem is still there

    def test_message_check(self):
        sim = self.sim()
        sim.expect()
        r0, r1 = self.ranked(sim)
        self.steady(sim)
        sim.fake.web_reply_hook = lambda body: b"<SM>broken</SM>" if b"<FCDOC>" in body else body
        self.assertTrue(sim.run(180, until=lambda: sim.code() == "message_unchecked"))
        p = sim.p()
        self.assertEqual(p.state.colour, AMBER)
        alert = next(x for x in p.alerts if x.kind == "message_unreadable")
        self.assertFalse(alert.sound)
        self.assertEqual(hx_state.machine_view(r0.data.current.machines[sim.mid], p, 1.0,
                                               sim.sender.read_expected(sim.mid), None, None, {}).origin_line, "")
        sim.fake.web_reply_hook = None
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "running_confirmed"))
        self.assertNotIn("message_unreadable", [x.kind for x in sim.p().alerts])
        # Broken again, then a handover and a restart after a share suspension keep it amber, same alert
        sim.fake.web_reply_hook = lambda body: b"<SM>broken</SM>" if b"<FCDOC>" in body else body
        self.assertTrue(sim.run(180, until=lambda: "message_unreadable" in [x.kind for x in sim.p().alerts]))
        alert_id = next(x.id for x in sim.p().alerts if x.kind == "message_unreadable")
        codes = []
        watch = lambda: codes.append(sim.code()) or False
        r0.poller.stop()
        sim.pcs.remove(r0)
        self.assertTrue(sim.run(20, until=lambda: watch() or sim.p().poller == r1.key))
        sim.run(60, until=watch)
        # The worker's P writes become slow: §6.13 stops it, and it restarts once the share is fine
        real_write = r1.store.write_poll
        slow = [6]

        def write_poll(status):
            if slow[0]:
                slow[0] -= 1
                sim.clock.advance(LOGIC.stale_s / 4 + 0.5)
            return real_write(status)
        r1.store.write_poll = write_poll
        self.assertTrue(sim.run(60, until=lambda: watch() or r1.entries("polling_stopped")
                                and r1.entries("polling_stopped")[-1]["reason"] == "share"))
        self.assertTrue(sim.run(60, until=lambda: watch() or r1.entries("polling_started")[-1]["reason"] == "restart"))
        sim.run(90, until=watch)
        self.assertNotIn("running_confirmed", codes)
        self.assertEqual(set(codes) - {"message_unchecked"}, set())
        self.assertEqual(next(x.id for x in sim.p().alerts if x.kind == "message_unreadable"), alert_id)
        for pc in (r0, r1):
            self.assertNotIn(alert_id, [e["alert_id"] for e in pc.entries("alert_ended")])
        started = [e for pc in (r0, r1) for e in pc.entries("alert_started") if e["alert"]["id"] == alert_id]
        self.assertEqual(len(started), 1)
        self.no_violations(sim)

    def test_origin_after_a_failed_switch(self):
        """The poller's side of a Send that failed after the switch: E says switch pending with a failure,
        the printer runs the new name with another origin, and the Loads are dropped at first."""
        sim = self.sim()
        old_e = sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        sim.fake.web_reply_hook = lambda body: b"<SM>broken</SM>" if b"<FCDOC>" in body else body
        e = dataclasses.replace(old_e, mode="switch_pending", send_id=SID2, confirmed=False, confirmed_by="",
                                failure=FailureInfo(step="confirm_1", msg=Msg("The read-back differs."),
                                                    after_switch=True, at=sim.clock.time()))
        sim.sender.write_expected(e)
        view = lambda: hx_state.machine_view(a.data.current.machines[sim.mid], sim.p(), 1.0,
                                             sim.sender.read_expected(sim.mid), None, None, {})
        self.assertTrue(sim.run(20, until=lambda: sim.code() == "failed_after_switch"))
        self.assertIsNone(sim.p().running)
        self.assertEqual(view().origin_line, "")
        sim.fake.web_reply_hook = replace_text(ORIGIN, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.p().running is not None))
        self.assertEqual(view().origin_line, "Product of Mexico")
        self.assertEqual(sim.code(), "failed_after_switch")


# --------------------------------------------------------------------------
# 20, 22, 24: keeping P fresh, the probe, the start race
# --------------------------------------------------------------------------

class FreshnessTests(Base):
    def threaded(self, *, slow_lock_s=0.0, gate_grab=False, run_s=1.3):
        timings = dataclasses.replace(TIMINGS.scaled(0.02), http_timeout_s=0.09, web_repeat_window_s=0.2)
        half = timings.stale_s / 2
        sim = self.sim(clock=SYSTEM_CLOCK, timings=timings,
                       printers=[{"offset_fn": EDT, "latency_s": 0.4 * half}])
        sim.expect()
        a = sim.pc("akim")
        writes = []
        real_write, real_lock = a.store.write_poll, a.store.read_lock

        def write_poll(status):
            t = time.monotonic()
            real_write(status)
            writes.append(t)
        grabbed = []

        def read_lock(printer_id):
            if slow_lock_s:
                time.sleep(slow_lock_s)
            if gate_grab and len(writes) > 8 and not grabbed:
                gate = a.gates.gate(f"127.0.0.1:{sim.fake.port}")
                got = threading.Event()

                def hold():
                    with gate:
                        got.set()
                        time.sleep(half + timings.http_timeout_s)
                grabbed.append(threading.Thread(target=hold, daemon=True))
                grabbed[0].start()
                got.wait(1.0)
            return real_lock(printer_id)
        a.store.write_poll, a.store.read_lock = write_poll, read_lock
        a.poller.start()
        self.addCleanup(a.poller.stop)
        deadline = time.monotonic() + run_s
        while time.monotonic() < deadline:
            time.sleep(0.2)
        a.poller.stop()
        return sim, timings, writes, grabbed

    def check(self, sim, timings, writes, *, lock_s=0.0, strict=True):
        half = timings.stale_s / 2
        latency = 0.4 * half
        bound = half + timings.http_timeout_s
        slack = 0.03                                    # thread scheduling on a busy test machine
        reqs = sim.requests()
        self.assertGreaterEqual(len(reqs), 6)
        # No request may still run stale_s / 2 + http_timeout_s after the start of the last good P write
        for r in reqs:
            p_ok = max(w for w in writes if w <= r["t"])
            self.assertLessEqual(r["t"] + latency, p_ok + bound + slack, r)
        polling = [w for w in writes if w >= reqs[0]["t"] - half]
        for c, b in zip(polling, polling[1:]):
            # Between Autopilot reads P never stands still for stale_s / 2. A web call may run until its
            # deadline (a login, the Load and a repeat take 1.2 x stale_s / 2 here), and the next write
            # follows the next lock read
            web = any(c <= r["t"] < b and r["kind"] != "autopilot" for r in reqs)
            limit = half + slack if strict and not web else bound + lock_s + slack
            self.assertLess(b - c, limit, (c, b, web))
        self.no_violations(sim)

    def test_keeping_p_fresh(self):
        sim, timings, writes, _ = self.threaded()
        self.check(sim, timings, writes)

    def test_slow_lock_reads(self):
        timings = dataclasses.replace(TIMINGS.scaled(0.02), http_timeout_s=0.09)
        slow = timings.stale_s / 4 - 0.02
        sim, timings, writes, _ = self.threaded(slow_lock_s=slow, run_s=2.2)
        self.check(sim, timings, writes, lock_s=slow, strict=False)

    def test_gate_taken_during_the_lock_read(self):
        sim, timings, writes, grabbed = self.threaded(gate_grab=True, run_s=1.8)
        self.assertTrue(grabbed)
        # A call waiting for the gate sends nothing, and waits no longer than its deadline allows
        self.check(sim, timings, writes, strict=False)

    def test_probe(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim", local=True)                  # no reach yet: can't poll
        self.assertFalse(a.poller.reach.can_poll())
        held = a.gates.gate(f"127.0.0.1:{sim.fake.port}")
        held.acquire()
        probed = []
        real_probed = a.poller.reach.probed
        a.poller.reach.probed = lambda ok: (probed.append(ok), real_probed(ok))
        sim.run(30)
        self.assertEqual(probed, [])                     # the gate was busy: None, nothing counted
        self.assertIsNone(a.poller.reach.last_printer_reach)
        held.release()
        self.assertTrue(sim.run(30, until=lambda: probed))
        self.assertEqual(probed, [True])
        self.assertEqual(sim.requests(), [])             # a bare connect, no request
        self.assertTrue(a.poller.reach.can_poll())
        self.assertTrue(sim.run(60, until=lambda: sim.code() == "running_confirmed"))

    def test_polled_printer_is_never_probed(self):
        sim = self.sim()
        sim.expect()
        b = sim.pc("bob")
        self.steady(sim)
        a = sim.pc("akim", local=True)
        probes = []
        with mock.patch.object(hx_protocol.PrinterClient, "probe", lambda self_: probes.append(1)):
            sim.run(120)
        self.assertEqual(probes, [])
        self.assertEqual(sim.p().poller, b.key)

    def test_printer_whose_poller_starts_during_the_pass_is_not_probed(self):
        # On a slow share another PC's poller can start between this pass's read of P and the probe:
        # P is read again right before probing, so a printer someone polls is never probed
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim", local=True)                  # no reach yet: it probes
        bob = sim.world.store(sim.world.identity("bob"))
        counters = []
        real_read_lock = a.store.read_lock

        def read_lock(printer_id):
            counters.append(len(counters) + 1)
            bob.write_poll(PollStatus(machine_id=sim.mid, printer_id=sim.pid, poller="", counter=counters[-1]))
            return real_read_lock(printer_id)
        a.store.read_lock = read_lock
        probes = []
        with mock.patch.object(hx_protocol.PrinterClient, "probe", lambda self_: probes.append(1)):
            sim.run(40)
        self.assertTrue(counters)                       # the probe was due and had picked its target
        self.assertEqual(probes, [])
        # Without the new poller the same PC probes
        a.store.read_lock = real_read_lock
        with mock.patch.object(hx_protocol.PrinterClient, "probe", lambda self_: probes.append(1)):
            sim.run(80)
        self.assertTrue(probes)

    def test_start_race(self):
        sim = self.sim()
        sim.expect()
        a, b = sim.pc("akim"), sim.pc("bob")
        order = assign_order(sim, a, b)
        r0, r1 = (a, b) if order[0] == a.key else (b, a)
        # Both coordinators see the missing poll.json for stale_s and start a worker in the same moment
        while not (r0.poller._workers and r1.poller._workers):
            for pc in (a, b):
                pc.poller._coordinate()
            sim.clock.advance(LOGIC.heartbeat_s)
        # Rank 1's coordinator is slow: its next pass comes only after its worker's settle
        r1.poller._next_coord = sim.clock.monotonic() + 3 * LOGIC.heartbeat_s
        r0.poller._next_coord = sim.clock.monotonic()
        sim.run(12)
        stopped = r1.entries("polling_stopped")
        self.assertEqual([(e["reason"], e["yielded_to"]) for e in stopped], [("rank", r0.key)])
        self.assertTrue(any(s.yielded_to == r0.key for s in [sim.p()] if sim.p().poller == r1.key) or
                        sim.p().poller == r0.key)
        client = r1.clients.client(r1.data.current.printers[sim.pid])
        self.assertIsNone(client.last_request_at)       # rank 1 never sent anything
        sim.run(30)
        self.assertEqual(sim.p().poller, r0.key)
        self.assertEqual(sim.code(), "running_confirmed")
        self.no_violations(sim)


    def disagreement_race(self, hiding, seconds=40.0, stagger=None):
        """Two PCs start a worker for a printer without P in the same moment while, for `seconds`, the
        rank-0 PC ("r0"), the rank-1 PC ("r1") or both don't see the other's heartbeat as live (a slow
        share). With `stagger`, the PC "akim" starts first and "bob" that many seconds later, its share
        still hiding akim's new poll.json from it at that moment (the SMB not-found cache, storage.md
        §19). Returns the sim, both PCs, when the disagreement ends and (time, computer) for every
        printer request each PC's client sent."""
        sim = self.sim()
        sim.expect()
        a, b = sim.pc("akim"), sim.pc("bob")
        r0, r1 = (a, b) if assign_order(sim, a, b)[0] == a.key else (b, a)
        hidden = {"both": (a, b), "r0": (r0,), "r1": (r1,)}[hiding]
        hide_until = sim.clock.monotonic() + seconds
        sent = []
        sim.polling_at_request = []         # (time, computer, PCs whose worker is past its rank check)

        def polling():
            return frozenset(pc.identity.computer for pc in (a, b)
                             if pc.worker() is not None and pc.worker().rank_checked and not pc.worker().done)
        for pc, other in ((a, b), (b, a)):
            if pc in hidden:
                real = pc.store.live_heartbeats

                def live(real=real, other=other):
                    found = real()
                    if sim.clock.monotonic() < hide_until:
                        found = [h for h in found if h.key != other.key]
                    return found
                pc.store.live_heartbeats = live
            real_client = pc.clients.client

            def client(printer, real_client=real_client, pc=pc):
                c = real_client(printer)
                if not getattr(c, "_logged", False):
                    for name in ("read", "command", "load_slot", "fonts", "firmware"):
                        def call(*args, _fn=getattr(c, name), **kw):
                            sent.append((sim.clock.monotonic(), pc.identity.computer))
                            sim.polling_at_request.append((sim.clock.monotonic(), pc.identity.computer, polling()))
                            return _fn(*args, **kw)
                        setattr(c, name, call)
                    c._logged = True
                return c
            pc.clients.client = client
        if stagger is not None:
            # bob's share doesn't show akim's new file until just after bob's worker has read it at its start
            shown_from = [None]
            real_read, real_age = b.store.read_poll, b.store.poll_age

            def hidden_file(mid):
                return mid == sim.mid and (shown_from[0] is None or sim.clock.monotonic() < shown_from[0])
            b.store.read_poll = lambda mid: None if hidden_file(mid) else real_read(mid)
            b.store.poll_age = lambda mid: 1e9 if hidden_file(mid) else real_age(mid)
            while not a.poller._workers:
                a.poller._coordinate()
                sim.clock.advance(LOGIC.heartbeat_s)
            sim.run(stagger, pcs=[a])
            shown_from[0] = sim.clock.monotonic() + 0.5
            while not b.poller._workers:
                b.poller._coordinate()
            b.poller._next_coord = sim.clock.monotonic() + LOGIC.heartbeat_s
            return sim, a, b, hide_until, sent
        # Both coordinators see the missing poll.json for stale_s and decide in the same moment
        while not (a.poller._workers and b.poller._workers):
            for pc in (a, b):
                pc.poller._coordinate()
            sim.clock.advance(LOGIC.heartbeat_s)
        return sim, a, b, hide_until, sent

    def test_start_race_while_pcs_disagree_about_who_is_live(self):
        # Each starter passes its rank check against its own live list; the one that finds the other's
        # write in P yields to it (or waits for its yield) before any request (architecture §7.3 rule 2)
        for hiding in ("both", "r0", "r1"):
            with self.subTest(hiding=hiding):
                sim, a, b, hide_until, sent = self.disagreement_race(hiding)
                self.assertTrue(sim.run(hide_until - sim.clock.monotonic() + 1))
                during = {pc for t, pc in sent if t < hide_until}
                self.assertEqual(len(during), 1, sorted(during))
                sim.run(90)
                active = [pc for pc in (a, b) if pc.worker() is not None and not pc.worker().done]
                self.assertEqual(len(active), 1)
                self.assertEqual(sim.p().poller, active[0].key)
                self.assertEqual(active[0].key, assign_order(sim, a, b)[0])     # rank 0 once they agree
                self.assertEqual(sim.code(), "running_confirmed")
                late = {pc for t, pc in sent if t >= sim.clock.monotonic() - 30}
                self.assertEqual(late, {active[0].identity.computer})
                self.no_violations(sim)

    def test_staggered_start_race_while_pcs_disagree(self):
        # Two PCs that don't see each other as live start a few seconds apart, the second one's share
        # still hiding the first one's new poll.json. The second starter's settle writes used to
        # overwrite the first starter's, so each found only its own write at its rank check and both
        # polled the printer: the first one stopped only at its next cycle, after the second one's
        # first request (on a real printer a cycle lasts seconds, so their requests overlapped). Each
        # starter now reads P before every settle write, and whichever finds the other's write first
        # yields before any request. At every request only one PC is past its rank check
        for stagger in (0.5, 2.0, 3.5, 6.5, 9.5):
            with self.subTest(stagger=stagger):
                sim, a, b, hide_until, sent = self.disagreement_race("both", seconds=60.0, stagger=stagger)
                self.assertTrue(sim.run(hide_until - sim.clock.monotonic() + 1))
                both = [(round(t, 1), who, sorted(p)) for t, who, p in sim.polling_at_request if len(p) > 1]
                self.assertEqual(both, [])
                self.assertTrue(all(p == {who} for _, who, p in sim.polling_at_request))
                sim.run(90)
                active = [pc for pc in (a, b) if pc.worker() is not None and not pc.worker().done]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0].key, assign_order(sim, a, b)[0])
                self.assertEqual(sim.code(), "running_confirmed")
                self.assertEqual({pc for t, pc in sent if t >= sim.clock.monotonic() - 30},
                                 {active[0].identity.computer})
                self.no_violations(sim)

    def test_worker_doesnt_start_over_a_new_write(self):
        # Between the coordinator's read and the worker's start, a PC this one doesn't see as live wrote P
        # (it started too): the worker doesn't start and leaves the file alone; the next pass decides again
        sim = self.sim()
        sim.expect()
        a, b = sim.pc("akim"), sim.pc("bob")
        while not a.poller._workers:
            a.poller._coordinate()
            sim.clock.advance(LOGIC.heartbeat_s)
        w = a.worker()
        b.store.write_poll(bare_poll(sim, b.key, counter=7))
        w.tick()
        self.assertTrue(w.done)
        self.assertEqual(a.entries("polling_started"), [])
        self.assertEqual((sim.p().poller, sim.p().counter), (b.key, 7))
        # The same file left alone for stale_s: the next pass starts a worker (a takeover)
        sim.run(LOGIC.stale_s + 2 * LOGIC.heartbeat_s, pcs=[a])
        self.assertEqual([e["reason"] for e in a.entries("polling_started")], ["takeover"])


    def test_another_writer_in_p_while_polling(self):
        # A poller finds another PC's write in P (that PC passed its own rank check): a live one ranked
        # below it is waited for, with no request and no P write, until P shows its yield or has been
        # quiet for stale_s; one it doesn't see as live is yielded to at once
        sim = self.sim()
        sim.expect()
        a, b = sim.pc("akim"), sim.pc("bob")
        r0, r1 = (a, b) if assign_order(sim, a, b)[0] == a.key else (b, a)

        def beat():
            r1.store.heartbeat().beat(can_poll=True, last_printer_reach=sim.clock.time(), polling=[sim.pid],
                                      monitor_open=False, floor_board=False)

        def run(seconds, *, beating=True):
            end = sim.clock.monotonic() + seconds
            while sim.clock.monotonic() < end - 1e-6:
                if beating:
                    beat()
                sim.run(min(2.5, end - sim.clock.monotonic()), pcs=[r0])

        def write_as_r1(**kw):
            p = sim.p()
            r1.store.write_poll(dataclasses.replace(p, counter=p.counter + 1, **kw))

        run(40)
        self.assertEqual(sim.code(), "running_confirmed")
        w = r0.worker()
        self.assertTrue(w.rank_checked)
        # 1. A live PC ranked below writes P: wait for it
        write_as_r1()
        t0 = sim.clock.monotonic()
        run(12)
        self.assertEqual(w.rival, r1.key)
        self.assertFalse(w.done)
        self.assertEqual(sim.requests(since=t0 + 0.5), [])
        self.assertEqual(sim.p().poller, r1.key)              # this worker didn't write over it
        # ... it yields to this PC: poll on, continuing from P
        write_as_r1(yielded_to=r0.key)
        t1 = sim.clock.monotonic()
        run(12)
        self.assertEqual(w.rival, "")
        self.assertTrue(sim.requests(since=t1))
        self.assertEqual(sim.p().poller, r0.key)
        self.assertEqual(sim.code(), "running_confirmed")
        # 2. It writes once more and then nothing: poll on once P has been quiet for stale_s
        write_as_r1()
        t2 = sim.clock.monotonic()
        run(LOGIC.stale_s + 2 * LOGIC.poll_fast_s)
        reqs = sim.requests(since=t2 + 0.5)
        self.assertTrue(reqs)
        self.assertGreaterEqual(reqs[0]["t"] - t2, LOGIC.stale_s - LOGIC.poll_fast_s)
        self.assertEqual(w.rival, "")
        # 3. A PC this one doesn't see as live writes P: yield to it at once, without a request
        run(30, beating=False)
        self.assertEqual(r0.poller.live and [h.key for h in r0.poller.live if h.key == r1.key], [])
        write_as_r1()
        t3 = sim.clock.monotonic()
        run(6, beating=False)
        self.assertTrue(w.done)
        self.assertEqual([(e["reason"], e["yielded_to"]) for e in r0.entries("polling_stopped")], [("rank", r1.key)])
        self.assertEqual(sim.requests(since=t3 + 0.5), [])
        self.assertEqual(sim.p().yielded_to, r1.key)
        self.no_violations(sim)


def machine_on_board(pc, machine_id):
    """The MachineView of a machine on the PC's latest board."""
    for line in pc.watcher.latest.lines:
        for m in line.machines:
            if m.machine_id == machine_id:
                return m
    return None


def bare_poll(sim, poller, *, counter):
    """A bare poll.json for the sim's machine, as a starting poller writes it."""
    return PollStatus(machine_id=sim.mid, printer_id=sim.pid, poller=poller, counter=counter)


def assign_order(sim, *pcs):
    hbs = [HeartbeatInfo(key=pc.key, computer=pc.identity.computer, user=pc.identity.user, pid=1, version="t",
                         can_poll=True) for pc in pcs]
    return hx_share.assign([sim.pid], hbs)[sim.pid]


class ConstantsTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((hx_poller.FOREIGN_MIN, hx_poller.FOREIGN_WINDOW_S, hx_poller.FOREIGN_CLEAR_S), (3, 600.0, 1800.0))
        self.assertEqual((hx_poller.MAX_BAGS_PER_S, hx_poller.DAILY_S, hx_poller.DST_KEEP_S, hx_poller.ACK_RETRY_S),
                         (5.0, 86_400.0, 86_400.0, 600.0))
        self.assertEqual((hx_poller.UNCHECKED_LOADS, hx_poller.UNCHECKED_SLOW_SETS), (2, 3))

    def test_dst_change_id(self):
        self.assertEqual(hx_poller.dst_change_id(CHANGE, -18000.0, offset_fn=DST), "20261101-18000")
        spring = float(calendar.timegm((2026, 3, 8, 7, 0, 0)))
        self.assertEqual(hx_poller.dst_change_id(spring, -14400.0, offset_fn=lambda u: -14400.0), "20260308-14400")
        self.assertEqual(hx_poller.dst_change_id(spring, 3600.0, offset_fn=lambda u: 3600.0), "20260308+3600")


class FakeCheckTests(unittest.TestCase):
    def test_every_fake_is_checked_when_a_test_ends(self):
        # A test that never looks at its fakes still fails when one saw a violation, two requests in
        # flight or a write outside the whitelist, also on a fake other than the first
        def spoil(fake, what):
            with fake.lock:
                if what == "violation":
                    fake.violations.append("two requests in flight")
                elif what == "in flight":
                    fake.max_in_flight = 2
                else:
                    fake.writes.append("SETPPARAM")

        for what in ("violation", "in flight", "write", None):
            class Inner(Base):
                def runTest(self):
                    sim = self.sim(printers=[{"offset_fn": EDT}, {"offset_fn": EDT}])
                    if what is not None:
                        spoil(sim.world.printers[1], what)
            with self.subTest(what=what):
                result = unittest.TestResult()
                Inner().run(result)
                self.assertEqual(len(result.failures) + len(result.errors), 0 if what is None else 1,
                                 result.failures + result.errors)


if __name__ == "__main__":
    unittest.main()
