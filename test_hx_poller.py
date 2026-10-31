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
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock

import hx_fake
import hx_message
import hx_model
import hx_poller
import hx_protocol
import hx_share
import hx_state
import hx_time
from hx_core import SYSTEM_CLOCK, EventBus, FakeClock, Msg
from hx_model import (ALLOWED_WRITE_TAGS, AMBER, GREY, RED, TIMINGS, Ack, AuthFailed, BoardUpdate, DesignItem,
                      ExpectedState, FailureInfo, HeartbeatInfo, MessageDesign, PollerUpdate, PollStatus)

EDT = lambda utc: -14400.0               # a fixed offset: the fake printer's clock doesn't follow the host
# Logic tests: default timings on a FakeClock, with short real network and share limits
LOGIC = dataclasses.replace(TIMINGS, http_timeout_s=0.1, read_retry_s=0.01, share_timeout_s=2.0)
# Threaded tests on the system clock: TIMINGS scaled by RT as a whole, so the spec's ratios between the
# waits hold. A Windows runner gets twice the time: its clock ticks every 15.6 ms, and its file writes
# (NTFS, the virus scanner) are slower. RS scales the tests' own absolute waits, set for RT = 0.02
RT = 0.04 if sys.platform == "win32" else 0.02
RS = RT / 0.02
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
        # Tried again if one slow share operation failed it (hx_fake.first_load)
        sim.test.assertIsNotNone(hx_fake.first_load(self.data), "the PC's first data load kept failing")
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
        # The test's own reads and set-up writes aren't under test: the real share timeout
        self.observer = self.world.setup_store(self.world.identity("observer", sender=False))
        self.sender = self.world.setup_store(self.world.identity("jsmith"))
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
        """The fake's message as a Load reads it; the fake's observations are cleared afterwards. Its requests
        wait as long as this sim's do, so a slow fake (more than LOGIC's 0.1 s) still answers in time."""
        timings = dataclasses.replace(LOGIC, http_timeout_s=max(LOGIC.http_timeout_s, self.timings.http_timeout_s))
        client = hx_protocol.PrinterClient("127.0.0.1", self.fake.uid, hx_protocol.make_token("123321"),
                                           port=self.fake.port, clock=SYSTEM_CLOCK, gates=hx_protocol.GateRegistry(),
                                           timings=timings)
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

    def drift_with_failing_loads(self, sim, fails, attempts=None, *, origin="Product of Mexico"):
        """Same name, other origin at the printer; fails(n) says whether the n-th Load from now (1 first)
        fails (junk in its reply: WebReadFailed). Returns the moments of the good Loads that showed the
        other origin; attempts, when given, gets the moment of every Load, good or not."""
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, origin)
        n, seen = [0], []

        def hook(body):
            if b"<FCDOC>" not in body:
                return body
            n[0] += 1
            if attempts is not None:
                attempts.append(sim.clock.monotonic())
            if fails(n[0]):
                return body.replace(b"<FCDOC>", b"<FCDOC>junk", 1)
            if b"Product of Mexico" in body:
                seen.append(sim.clock.monotonic())
            return body
        sim.fake.web_reply_hook = hook
        return seen

    def green_after(self, sim, a, seen, seconds, until=None):
        """Run; the latest moment the machine was green with the old origin after the first good Load that
        showed the other one (None when never), checked on P and on the board after every step."""
        last = [None]
        machine = a.data.current.machines[sim.mid]
        e = sim.sender.read_expected(sim.mid)

        def check():
            p = sim.p()
            v = hx_state.machine_view(machine, p, 0.0, e, None, None, {})
            if seen and (p.state.code == "running_confirmed" or v.colour == "green"
                         or v.origin_line == ORIGIN):
                last[0] = sim.clock.monotonic()
            return until is not None and until()
        sim.run(seconds, until=check)
        return None if last[0] is None else last[0] - seen[0]

    def test_a_failed_confirming_load_is_tried_again_at_the_next_cycle(self):
        # Owner decision (31 Oct): a failed confirming Load sets need_load, so drift is red about one cycle
        # later instead of a slow set later
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        seen = self.drift_with_failing_loads(sim, lambda n: n == 2)     # the first confirming Load
        green = self.green_after(sim, a, seen, 90, until=lambda: sim.code() == "drift")
        self.assertEqual(sim.code(), "drift")
        self.assertLessEqual(green, LOGIC.poll_fast_s + 2 * LOGIC.second_reading_s + 0.5)
        self.assertLessEqual(sim.clock.monotonic() - seen[0], LOGIC.poll_fast_s + 3 * LOGIC.second_reading_s)
        self.assertEqual(sim.p().running.origin_line, "Product of Mexico")
        self.no_violations(sim)

    def test_every_confirming_load_failing_turns_amber(self):
        # Every first Load reads the other origin and every confirming Load fails: never confirmed, so
        # never red, but after UNCHECKED_CONFIRMS failed confirmations amber message_unchecked with the
        # origin unknown, never green "Printing" with the old origin
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        seen = self.drift_with_failing_loads(sim, lambda n: n % 2 == 0)
        green = self.green_after(sim, a, seen, 120, until=lambda: sim.code() == "message_unchecked")
        self.assertEqual(sim.code(), "message_unchecked")
        bound = hx_poller.UNCHECKED_CONFIRMS * (LOGIC.poll_fast_s + 2 * LOGIC.second_reading_s)
        self.assertLessEqual(green, bound)
        p = sim.p()
        self.assertEqual(p.state.colour, AMBER)
        self.assertTrue(p.running_unchecked)
        self.assertIn("message_unreadable", [x.kind for x in p.alerts])
        machine = a.data.current.machines[sim.mid]
        self.assertEqual(hx_state.machine_view(machine, p, 0.0, sim.sender.read_expected(sim.mid), None, None,
                                               {}).origin_line, "")
        # It stays amber, and the Loads go back to the slow set's pace
        t0 = sim.clock.monotonic()
        self.assertIsNone(self.green_after(sim, a, [t0], 300))
        loads = len(sim.requests(since=t0, kind="sme"))
        self.assertLessEqual(loads, 2 * (300 / LOGIC.poll_slow_s + 1))
        self.assertEqual(sim.code(), "message_unchecked")
        # Loads that work again: red drift, which two agreeing Loads confirm
        sim.fake.web_reply_hook = None
        self.assertTrue(sim.run(130, until=lambda: sim.code() == "drift"))
        self.assertEqual(sim.p().running.origin_line, "Product of Mexico")
        self.no_violations(sim)

    def test_random_load_failures_never_leave_a_drift_green(self):
        # 40 % of Loads fail at random (the wave-5 probe, 10 seeds): once a good Load has shown the other
        # origin, the machine is red or amber within a few cycles; and within about 20 s of the first Load
        # after the change, good or not (a failed one is tried again at the next cycle), so within one
        # slow set plus that of the change, never two slow sets
        worst = worst_attempt = worst_change = 0.0
        for seed in range(10):
            sim = self.sim()
            sim.expect()
            a = sim.pc("akim")
            self.steady(sim)
            rng = random.Random(seed)
            attempts = []
            changed = sim.clock.monotonic()
            seen = self.drift_with_failing_loads(sim, lambda n: rng.random() < 0.4, attempts)
            green = self.green_after(sim, a, seen, 400,
                                     until=lambda: sim.code() in ("drift", "message_unchecked"))
            self.assertIn(sim.code(), ("drift", "message_unchecked"), seed)
            worst = max(worst, green or 0.0)
            worst_attempt = max(worst_attempt, sim.clock.monotonic() - attempts[0])
            worst_change = max(worst_change, sim.clock.monotonic() - changed)
        self.assertLessEqual(worst, hx_poller.UNCHECKED_CONFIRMS * (LOGIC.poll_fast_s + 2 * LOGIC.second_reading_s))
        self.assertLessEqual(worst_attempt, 20.0 + LOGIC.second_reading_s)
        self.assertLessEqual(worst_change, LOGIC.poll_slow_s + 15.0)

    def test_a_failed_first_load_is_tried_again_at_the_next_cycle(self):
        # Owner decision (31 Oct): the slow set's Load fails right after the message changed at the printer:
        # the next cycle loads again, so drift is red about one cycle later, not a slow set later
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        attempts = []
        seen = self.drift_with_failing_loads(sim, lambda n: n == 1, attempts)
        green = self.green_after(sim, a, seen, 130, until=lambda: sim.code() == "drift")
        self.assertEqual(sim.code(), "drift")
        self.assertLessEqual(green or 0.0, 2 * LOGIC.second_reading_s + 0.5)     # from the first good Load
        self.assertEqual(sim.p().running.origin_line, "Product of Mexico")
        self.assertLessEqual(attempts[1] - attempts[0], LOGIC.poll_fast_s + 0.5)
        self.assertLessEqual(sim.clock.monotonic() - attempts[0], LOGIC.poll_fast_s + 3 * LOGIC.second_reading_s)
        self.no_violations(sim)

    def test_a_failed_load_of_an_unchanged_message_is_tried_again_and_stays_green(self):
        # One Load fails while nothing changed: one extra Load at the next cycle, still green, no alert,
        # then the slow set's pace again
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        attempts = []
        self.drift_with_failing_loads(sim, lambda n: n == 1, attempts, origin=ORIGIN)
        codes = set()
        sim.run(200, until=lambda: codes.add(sim.code()) or False)
        self.assertEqual(codes, {"running_confirmed"})
        self.assertFalse(a.entries("alert_started"))
        self.assertLessEqual(attempts[1] - attempts[0], LOGIC.poll_fast_s + 0.5)
        # Then the slow set's own pace again, counted from the failed Load's slow set
        later = [x - attempts[0] for x in attempts[2:]]
        self.assertTrue(later, attempts)
        self.assertTrue(all(abs(x / LOGIC.poll_slow_s - round(x / LOGIC.poll_slow_s)) * LOGIC.poll_slow_s < 1.5
                            and x >= LOGIC.poll_slow_s - 1 for x in later), later)

    def test_loads_that_keep_failing_go_back_to_the_slow_set(self):
        # Every Load fails: one retry, then amber message_unchecked, and the Loads at the slow set's pace
        # (the budget), not one every cycle
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim)
        attempts = []
        self.drift_with_failing_loads(sim, lambda n: True, attempts, origin=ORIGIN)
        self.assertTrue(sim.run(130, until=lambda: sim.code() == "message_unchecked"))
        self.assertLessEqual(sim.clock.monotonic() - attempts[0], LOGIC.poll_fast_s + 2 * LOGIC.second_reading_s)
        t0 = sim.clock.monotonic()
        sim.run(600)
        self.assertEqual(sim.code(), "message_unchecked")
        after = [x for x in attempts if x >= t0]
        self.assertLessEqual(len(after), 600 / LOGIC.poll_slow_s + 1)
        self.no_violations(sim)

    def test_a_skipped_confirming_load_confirms_nothing(self):
        # The second reading re-reads PRIN first; when that gets no reply, the confirming Load is never made,
        # and the first Load's R (unconfirmed) must not be judged as if it had been read twice
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim)
        prin = []

        def prin_reply():
            # At a slow set (HXUI right before PRIN) the message changes and PRIN reads 0 once; the
            # second reading's PRIN then gets no reply, so its Load is never made
            with sim.fake.lock:
                slow_set = len(sim.fake.requests) >= 2 and sim.fake.requests[-2]["tag"] == "HXUI"
                if slow_set and not prin:
                    sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
            if slow_set and not prin:
                prin.append("0")
                return "0"
            if prin and len(prin) < 3:
                prin.append("silent")                               # the read and the client's own retry
                return hx_fake.FakePrinter.SILENT
            return None
        sim.fake.override("PRIN", prin_reply)
        codes = []
        self.assertTrue(sim.run(70, until=lambda: codes.append(sim.code()) or len(prin) == 3))
        self.assertEqual(sim.requests()[-1]["tag"], "PRIN")        # the second reading's: no Load after it
        loads = len(sim.requests(kind="sme"))
        self.assertNotIn("printing_off", codes)                     # PRIN was read 0 only once
        self.assertNotIn("drift", codes)                            # and Mexico only once
        self.assertEqual(sim.p().running.origin_line, ORIGIN)
        # The next cycle loads again (need_load), and two agreeing Loads give drift
        self.assertTrue(sim.run(LOGIC.poll_fast_s + 3, until=lambda: sim.code() == "drift"))
        self.assertGreaterEqual(len(sim.requests(kind="sme")), loads + 2)
        self.assertEqual(sim.code(), "drift")

    def acked_drift(self, sim):
        """A same-name drift committed and acknowledged on the floor; returns its alert."""
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
        drift = next(x for x in sim.p().alerts if x.kind == "drift")
        sim.observer.write_ack(Ack(alert_id=drift.id, by=JSMITH, computer="FLOOR01", at=sim.clock.time()))
        self.assertTrue(sim.run(15, until=lambda: any(x.id == drift.id and x.acked_by for x in sim.p().alerts)))
        return drift

    def test_failed_retries_keep_a_known_drift_and_its_alert(self):
        # Owner decision (31 Oct), second review: a failed Load's retry at the next cycle isn't a required
        # Load, so when it fails too R stays, as for the slow set's own Load. Two failed Loads in a row
        # leave a committed, acknowledged drift red with the same alert id, still acknowledged, and R's
        # read-back: nothing ends, nothing rings again, no new alert id once the Loads work again
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        drift = self.acked_drift(sim)
        attempts = []
        self.drift_with_failing_loads(sim, lambda n: n <= 2, attempts)
        seen = set()

        def look():
            p = sim.p()
            seen.add((p.state.code, tuple((x.id, x.acked_by) for x in p.alerts if x.active and x.kind == "drift"),
                      p.running.origin_line if p.running is not None else None))
            return False
        sim.run(200, until=look)
        self.assertGreaterEqual(len(attempts), 3)
        self.assertLessEqual(attempts[1] - attempts[0], LOGIC.poll_fast_s + 0.5)       # the retry, next cycle
        self.assertEqual(seen, {("drift", ((drift.id, JSMITH),), "Product of Mexico")})
        self.assertFalse([e for e in a.entries("alert_ended") if e["alert_id"] == drift.id])
        self.assertEqual([e["alert"]["kind"] for e in a.entries("alert_started")], ["drift", "message_unreadable"])
        self.no_violations(sim)

    def test_random_load_failures_never_end_an_acknowledged_drift(self):
        # The reviewer's realistic rate: 40 % of Loads fail at random for 15 minutes after an acknowledged
        # drift. Every seed keeps one drift alert id, acknowledged, and the machine red all along
        for seed in range(3):
            sim = self.sim()
            sim.expect()
            sim.pc("akim")
            self.steady(sim)
            drift = self.acked_drift(sim)
            rng = random.Random(seed)
            self.drift_with_failing_loads(sim, lambda n: rng.random() < 0.4)
            bad = []

            def look():
                p = sim.p()
                ids = [(x.id, x.acked_by) for x in p.alerts if x.active and x.kind == "drift"]
                if p.state.code != "drift" or ids != [(drift.id, JSMITH)]:
                    bad.append((round(sim.clock.monotonic()), p.state.code, ids))
                return False
            sim.run(900, until=look)
            self.assertEqual(bad, [], seed)

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
        timings = TIMINGS.scaled(RT)
        sim = self.sim(clock=SYSTEM_CLOCK, timings=timings)
        sim.expect()
        a = sim.pc("akim", time_source=hx_time.TimeSource(["time.example.local"], clock=SYSTEM_CLOCK,
                                                           query=lambda host, port=123, timeout_s=2.0: time.time(),
                                                           cache_s=timings.poll_slow_s))
        with mock.patch.object(hx_time.TimeSource, "now", now):
            a.poller.start()
            self.addCleanup(a.poller.stop)
            deadline = time.monotonic() + 5 * RS
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

    def test_idle_on_a_pc_that_cant_poll(self):
        # C-2 (final review, 31 Oct): an office PC (the firewall keeps it from the printers, so it never
        # reached one and can't poll) with the floor board open. During a share outage it makes no
        # standalone checks: they would be blocked, and show printing machines as "Printer off". Its board
        # says it isn't checking any printers. A floor PC that can poll still checks (the control)
        import socket
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        office = sim.pc("office", local=True)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        d = office.data.current
        office.data.current = dataclasses.replace(d, printers={k: dataclasses.replace(v, port=closed)
                                                               for k, v in d.printers.items()})
        self.steady(sim)
        self.assertFalse(office.poller.reach.can_poll())
        for pc in (a, office):
            pc.poller.set_monitor_open(True, True)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(120, until=lambda: a.poller.mode == "standalone"))
        codes = set()
        for _ in range(24):
            sim.fake.print_bags(3)                      # the bagger runs
            sim.run(5)
            office.watcher.run_once()
            codes.add(machine_on_board(office, sim.mid).code)
            self.assertEqual(office.poller.mode, "idle")
        self.assertIsNone(office.poller._standalone)
        self.assertEqual(codes, {"not_monitored"})       # never "Printer off"
        board = office.watcher.latest
        self.assertEqual((board.share_ok, board.standalone), (False, False))
        self.assertEqual(hx_state.board_banner(board).text,
                         "Server unreachable: this PC isn't checking any printers.")
        self.assertEqual(a.poller.mode, "standalone")

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

    def known_drift_then_down(self, sim, *, ack=False, poller=True):
        """A same-name drift committed in share mode (red, ringing), then the share goes down with the floor
        board open on PC a until a is standalone. poller=True: a polls the printer, and its coordinator
        hasn't read the drift P yet (only a's worker wrote it). poller=False: another PC polls, and a's
        coordinator read the drift P before the share went down."""
        sim.expect()
        pcs = [sim.pc("akim"), sim.pc("bob")]
        r0 = next(pc for pc in pcs if pc.key == assign_order(sim, *pcs)[0])
        a = r0 if poller else next(pc for pc in pcs if pc is not r0)
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
        self.assertEqual(sim.p().poller, r0.key)
        if poller:
            seen = (a.poller.seen_polls().get(sim.mid) or (None, 0))[0]
            self.assertNotEqual(seen.state.code if seen is not None else "", "drift")
            sim.clock.advance(LOGIC.heartbeat_s + 0.1)      # the watcher reads the share, not seen_polls
        else:
            sim.run(LOGIC.heartbeat_s + 0.1)
            self.assertEqual(a.poller.seen_polls()[sim.mid][0].state.code, "drift")
        drift = next(x for x in sim.p().alerts if x.kind == "drift")
        if ack:
            a.watcher.run_once()
            a.watcher.acknowledge([drift.id])
            a.watcher.run_once()                              # writes the ack file
            self.assertTrue(sim.run(10, until=lambda: sim.p().alerts[0].acked_by == a.identity.user))
            if poller:
                sim.clock.advance(LOGIC.heartbeat_s + 0.1)
        a.watcher.run_once()
        self.assertEqual(machine_on_board(a, sim.mid).colour, RED)
        a.poller.set_monitor_open(True, True)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        return a, drift

    def test_a_known_drift_stays_red_while_the_share_is_down(self):
        # Owner decision (31 Oct): standalone checks can't see a same-name drift, so the drift known from
        # the share stays red, with the same alert, and R's origin, until share mode clears it
        sim = self.sim()
        a, drift = self.known_drift_then_down(sim)
        t0 = sim.clock.monotonic()
        codes = set()

        def look():
            st = a.poller.local_statuses().get(sim.mid)
            if st is not None:
                codes.add(st.state.code)
            return False
        sim.run(3 * LOGIC.standalone_poll_s, until=look)
        self.assertEqual(codes, {"drift"})
        self.assertEqual([r for r in sim.requests(since=t0) if r["kind"] != "autopilot"], [])     # no Load
        st = a.poller.local_statuses()[sim.mid]
        self.assertTrue(st.standalone)
        self.assertEqual([(x.id, x.kind) for x in st.alerts if x.active], [(drift.id, "drift")])
        self.assertFalse([e for e in a.entries("alert_ended") if e["alert_id"] == drift.id])
        a.watcher.run_once()
        mv = machine_on_board(a, sim.mid)
        self.assertEqual((mv.code, mv.colour, mv.origin_line), ("drift", RED, "Product of Mexico"))
        row = next(r for r in hx_state.floor_board_rows(a.watcher.latest) if r.machine_id == sim.mid)
        self.assertEqual((row.colour, row.origin_line), (RED, "Product of Mexico"))
        self.assertEqual([x.id for x in hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)],
                         [drift.id])
        # The message is fixed at the printer (same name again): standalone still can't tell, so red
        install(sim.fake)
        sim.run(LOGIC.standalone_poll_s + 1)
        self.assertEqual(a.poller.local_statuses()[sim.mid].state.code, "drift")
        # Only share mode clears it: its Loads see the fixed message
        sim.world.fs.down = False
        self.assertTrue(sim.run(120, until=lambda: sim.code() == "running_confirmed"))

    def test_a_known_drift_seen_only_by_the_coordinator(self):
        # A PC that doesn't poll the printer seeds from the P its coordinator last read
        sim = self.sim()
        a, drift = self.known_drift_then_down(sim, poller=False)
        sim.run(LOGIC.standalone_poll_s + 1)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual((st.state.code, [x.id for x in st.alerts if x.active]), ("drift", [drift.id]))

    def test_a_known_drift_seen_only_by_the_watcher(self):
        # Integrator, from the owner-decision review's probe: a's coordinator stalls (a slow share) while
        # another PC commits the drift, so only a's watcher reads the drift P (its seen_polls copy is older
        # than heartbeat_s). The share then goes down: the seed uses that read too, so a's board stays red
        sim = self.sim()
        sim.expect()
        pcs = [sim.pc("akim"), sim.pc("bob")]
        r0 = assign_order(sim, *pcs)[0]
        b = next(pc for pc in pcs if pc.key == r0)
        a = next(pc for pc in pcs if pc is not b)
        self.steady(sim)
        a.watcher.run_once()
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, pcs=[b], until=lambda: sim.code() == "drift"))
        sim.run(LOGIC.heartbeat_s + 0.5, pcs=[b])
        a.watcher.run_once()
        self.assertNotEqual(a.poller.seen_polls()[sim.mid][0].state.code, "drift")
        self.assertEqual(machine_on_board(a, sim.mid).colour, RED)
        drift = next(x for x in sim.p().alerts if x.kind == "drift")
        a.poller.set_monitor_open(True, True)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        sim.run(2 * LOGIC.standalone_poll_s)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual((st.state.code, [x.id for x in st.alerts if x.active]), ("drift", [drift.id]))
        a.watcher.run_once()
        mv = machine_on_board(a, sim.mid)
        self.assertEqual((mv.colour, mv.origin_line), (RED, "Product of Mexico"))
        self.assertEqual([x.id for x in hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)],
                         [drift.id])

    def test_an_acknowledged_known_drift_stays_quiet_and_red(self):
        sim = self.sim()
        a, drift = self.known_drift_then_down(sim, ack=True)
        sim.run(2 * LOGIC.standalone_poll_s)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual(st.state.code, "drift")
        self.assertEqual([(x.id, x.acked_by) for x in st.alerts if x.active], [(drift.id, a.identity.user)])
        a.watcher.run_once()
        self.assertEqual(machine_on_board(a, sim.mid).colour, RED)
        self.assertEqual(hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True), [])

    def test_a_known_drift_renamed_at_the_printer_stays_red_with_the_origin_unknown(self):
        # Owner decision (31 Oct): another message selected at the printer while the share is down is at
        # best amber unknown_while_down, and the drift alert stays open (so the board stays red and rings)
        sim = self.sim()
        a, drift = self.known_drift_then_down(sim)
        install(sim.fake, 10, "SOMETHING-ELSE", "Product of Peru")
        sim.run(LOGIC.standalone_poll_s + 1)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual(st.state.code, "unknown_while_down")
        self.assertEqual([x.id for x in st.alerts if x.active], [drift.id])
        a.watcher.run_once()
        mv = machine_on_board(a, sim.mid)
        self.assertEqual((mv.colour, mv.origin_line), (RED, ""))         # not Mexico any more, not E's either
        row = next(r for r in hx_state.floor_board_rows(a.watcher.latest) if r.machine_id == sim.mid)
        self.assertEqual((row.colour, row.origin_line), (RED, ""))
        self.assertEqual([x.id for x in hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)],
                         [drift.id])
        # E's message put back at the printer: standalone can't check its content, so still not green
        install(sim.fake)
        sim.run(2 * LOGIC.standalone_poll_s + 1)
        self.assertEqual(a.poller.local_statuses()[sim.mid].state.code, "drift")
        a.watcher.run_once()
        self.assertEqual(machine_on_board(a, sim.mid).colour, RED)
        # Share mode's own Loads judge it: the fixed message, two Loads agree, green
        sim.world.fs.down = False
        self.assertTrue(sim.run(120, until=lambda: sim.code() == "running_confirmed"))
        self.assertFalse([x for x in sim.p().alerts if x.active and x.kind == "drift"])

    def test_a_known_drift_stays_red_over_the_change_of_mode(self):
        # Owner decision (31 Oct), the wave-5 probe end to end: from the moment the share goes down, through
        # the change to standalone mode and the seconds before the first standalone check, over several
        # checks, and until share mode judges again, every board sample (card, floor row, sound) is red,
        # rings for the drift and shows what prints, never E's origin
        for seed in range(3):
            sim = self.sim()
            sim.expect()
            a = sim.pc("akim")
            a.poller._rng.seed(seed)                # where the first standalone check falls
            self.steady(sim)
            with sim.fake.lock:
                sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
            self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
            for _ in range(int(LOGIC.heartbeat_s + LOGIC.watch_s)):     # the watcher's passes see P move
                sim.run(1)
                a.watcher.run_once()
            drift = next(x.id for x in sim.p().alerts if x.kind == "drift")
            a.poller.set_monitor_open(True, True)
            sim.world.fs.down = True
            self.addCleanup(setattr, sim.world.fs, "down", False)
            samples, modes = [], set()
            for _ in range(int(LOGIC.lock_stale_s + 3 * LOGIC.standalone_poll_s)):
                sim.run(1)
                a.watcher.run_once()
                modes.add(a.poller.mode)
                mv = machine_on_board(a, sim.mid)
                row = next(r for r in hx_state.floor_board_rows(a.watcher.latest) if r.machine_id == sim.mid)
                ringing = [x.id for x in hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)]
                card = next(ln for ln in a.watcher.latest.lines if ln.line_id == mv.line_id)     # the line card
                samples.append((a.poller.mode, mv.colour, mv.origin_line, row.colour, row.origin_line, card.colour,
                                ringing))
            self.assertIn("standalone", modes)
            self.assertEqual([s for s in samples if s[1:] != (RED, "Product of Mexico", RED, "Product of Mexico", RED,
                                                              [drift])], [], seed)
            self.assertEqual(a.poller.local_statuses()[sim.mid].state.code, "drift")
            # The share is back: red until share mode's own Loads judge it, never green before
            sim.world.fs.down = False
            a.store.probe_now()
            deadline = time.monotonic() + 5
            while not a.store.available and time.monotonic() < deadline:
                time.sleep(0.01)
            for _ in range(30):
                sim.run(1)
                a.watcher.run_once()
                self.assertEqual(machine_on_board(a, sim.mid).colour, RED, seed)
            self.assertEqual(sim.code(), "drift")
            self.assertEqual(next(x.id for x in sim.p().alerts if x.kind == "drift"), drift)

    def test_a_drift_committed_just_before_the_outage_shows_red(self):
        # This PC's worker commits the drift and the share goes down before any watcher pass read it: the
        # board shows the P this PC wrote, red and ringing with R's origin, not the older green it read
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        for _ in range(int(LOGIC.watch_s * 2)):
            sim.run(1)
            a.watcher.run_once()
        self.assertEqual(machine_on_board(a, sim.mid).colour, "green")
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))      # no watcher pass meanwhile
        drift = next(x.id for x in sim.p().alerts if x.kind == "drift")
        a.poller.set_monitor_open(True, True)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        bad = []
        for _ in range(int(LOGIC.lock_stale_s + 2 * LOGIC.standalone_poll_s)):
            sim.run(1)
            a.watcher.run_once()
            mv = machine_on_board(a, sim.mid)
            ringing = [x.id for x in hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)]
            if (mv.colour, mv.origin_line, ringing) != (RED, "Product of Mexico", [drift]):
                bad.append((a.poller.mode, mv.code, mv.colour, mv.origin_line, ringing))
        self.assertEqual(a.poller.mode, "standalone")
        self.assertEqual(bad, [])

    def test_an_ack_read_from_the_share_before_the_outage_stays_quiet(self):
        # Another PC acknowledged the drift; this PC's watcher read the ack, the share went down before the
        # poller wrote it into P: the carried alert still never rings again here
        sim = self.sim()
        sim.expect()
        pcs = [sim.pc("akim"), sim.pc("bob")]
        a = next(pc for pc in pcs if pc.key != assign_order(sim, *pcs)[0])       # a only watches
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
        sim.run(LOGIC.heartbeat_s + 0.1)
        drift = next(x for x in sim.p().alerts if x.kind == "drift")
        a.watcher.run_once()
        sim.observer.write_ack(Ack(alert_id=drift.id, by=JSMITH, computer="FLOOR01", at=sim.clock.time()))
        a.watcher.run_once()                                    # reads the ack file
        self.assertEqual(a.watcher._found_acks[drift.id].by, JSMITH)
        a.poller.set_monitor_open(True, True)
        sim.world.fs.down = True                                # before the poller saw the ack
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        sim.run(2 * LOGIC.standalone_poll_s)
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual(st.state.code, "drift")
        self.assertEqual([(x.id, x.acked_by) for x in st.alerts if x.active], [(drift.id, "")])
        a.watcher.run_once()
        mv = machine_on_board(a, sim.mid)
        self.assertEqual((mv.colour, mv.unacked_sound), (RED, False))
        self.assertEqual(hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True), [])

    def outage_board(self, sim, a, checks=3):
        """The share goes down with the floor board open on PC a. a's board once a second from then until
        `checks` standalone checks have run: (mode, code, colour, origin, floor row colour, its origin, line
        card colour, ringing alert ids)."""
        a.poller.set_monitor_open(True, True)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        samples = []
        for _ in range(int(LOGIC.lock_stale_s + checks * LOGIC.standalone_poll_s)):
            sim.run(1)
            a.watcher.run_once()
            mv = machine_on_board(a, sim.mid)
            row = next(r for r in hx_state.floor_board_rows(a.watcher.latest) if r.machine_id == sim.mid)
            card = next(ln for ln in a.watcher.latest.lines if ln.line_id == mv.line_id)
            ringing = [x.id for x in hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)]
            samples.append((a.poller.mode, mv.code, mv.colour, mv.origin_line, row.colour, row.origin_line,
                            card.colour, ringing))
        self.assertEqual(samples[-1][0], "standalone")
        return samples

    def share_back(self, sim, a):
        sim.world.fs.down = False
        a.store.probe_now()
        deadline = time.monotonic() + 5
        while not a.store.available and time.monotonic() < deadline:
            time.sleep(0.01)

    @staticmethod
    def failing_loads(sim, fails):
        """fails(n): the n-th Load from now (1 first) gets junk in its reply. Returns the Load count."""
        n = [0]

        def hook(body):
            if b"<FCDOC>" not in body:
                return body
            n[0] += 1
            return body.replace(b"<FCDOC>", b"<FCDOC>junk", 1) if fails(n[0]) else body
        sim.fake.web_reply_hook = hook
        return n

    def test_a_known_drift_whose_loads_failed_stays_red_while_the_share_is_down(self):
        # Owner decision (31 Oct), second review: a committed drift, then two Loads in a row fail just before
        # the outage. The failed retry keeps R, so P still holds the drift, and the standalone checks carry
        # it: red and ringing on the card, the floor row and the line card; the message check counts as
        # failed (two failed Loads), so the origin shows as unknown, as in share mode, never E's
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
        drift = next(x.id for x in sim.p().alerts if x.kind == "drift")
        n = self.failing_loads(sim, lambda k: k <= 2)
        self.assertTrue(sim.run(100, until=lambda: n[0] >= 2))
        p = sim.p()
        self.assertEqual((p.state.code, p.running.origin_line, p.running_unchecked), ("drift", "Product of Mexico", True))
        samples = self.outage_board(sim, a)
        self.assertEqual([s for s in samples if s[2:] != (RED, "", RED, "", RED, [drift])], [])
        self.assertEqual(a.poller.local_statuses()[sim.mid].state.code, "drift")

    def test_a_carried_drift_survives_a_failed_first_load_when_the_share_is_back(self):
        # Owner decision (31 Oct), second review: the share is back and the new worker's first Load (a
        # required one) fails. R unknown reads nothing about the message: the carried drift keeps its alert
        # (the same id, still acknowledged), the machine stays red with the origin unknown meanwhile, and only
        # a Load and its second reading decide. Nothing rings again, no new alert id
        sim = self.sim()
        a, drift = self.known_drift_then_down(sim, ack=True)
        sim.run(2 * LOGIC.standalone_poll_s)
        self.assertEqual(a.poller.local_statuses()[sim.mid].state.code, "drift")
        n = self.failing_loads(sim, lambda k: k == 1)
        self.share_back(sim, a)
        codes, bad = set(), []
        for _ in range(120):
            sim.run(1)
            a.watcher.run_once()
            p = sim.p()
            mv = machine_on_board(a, sim.mid)
            ringing = hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)
            ids = [(x.id, x.acked_by) for x in p.alerts if x.active and x.kind == "drift"]
            codes.add(p.state.code)
            if ids != [(drift.id, a.identity.user)] or mv.colour != RED or ringing or mv.origin_line == ORIGIN:
                bad.append((a.poller.mode, p.state.code, ids, mv.colour, mv.origin_line, ringing))
        self.assertGreaterEqual(n[0], 3)
        self.assertIn("message_unchecked", codes)              # the failed first Load: R unknown
        self.assertEqual(bad, [])
        self.assertEqual((sim.code(), sim.p().running.origin_line), ("drift", "Product of Mexico"))
        self.assertFalse([e for e in a.entries("alert_ended") if e["alert_id"] == drift.id])

    def test_a_known_drift_whose_r_is_unknown_stays_red_while_the_share_is_down(self):
        # The same, and every Load fails after the share is back, so P holds the drift alert with R unknown
        # when the share goes down again: still carried, red and ringing, the origin unknown, never green
        sim = self.sim()
        a, drift = self.known_drift_then_down(sim)
        sim.run(LOGIC.standalone_poll_s)
        self.failing_loads(sim, lambda k: True)
        self.share_back(sim, a)
        self.assertTrue(sim.run(120, until=lambda: sim.code() == "message_unchecked"))
        p = sim.p()
        self.assertIsNone(p.running)
        self.assertEqual([x.id for x in p.alerts if x.active and x.kind == "drift"], [drift.id])
        sim.run(LOGIC.heartbeat_s + 0.1)
        samples = self.outage_board(sim, a)
        self.assertEqual([s for s in samples if s[2:] != (RED, "", RED, "", RED, [drift.id])], [])
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual((st.state.code, [x.id for x in st.alerts if x.active and x.kind == "drift"]),
                         ("unknown_while_down", [drift.id]))
        # Back with working Loads: share mode's two agreeing Loads give the drift again, the same alert
        sim.fake.web_reply_hook = None
        self.share_back(sim, a)
        self.assertTrue(sim.run(120, until=lambda: sim.code() == "drift"))
        self.assertEqual([x.id for x in sim.p().alerts if x.active and x.kind == "drift"], [drift.id])

    def test_a_message_share_mode_couldnt_check_stays_amber_while_the_share_is_down(self):
        # Owner decision (31 Oct), second review: every first Load reads another origin and every confirming
        # Load fails, so share mode says amber "can't check the message" with the origin unknown. The
        # standalone checks can't check it either (no Loads, and MSGNAM is E's name): at best amber
        # unknown_while_down with the origin unknown on the card, the floor row and the line card, never
        # green with E's origin. Share mode decides again once the share is back
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.failing_loads(sim, lambda k: k % 2 == 0)
        self.assertTrue(sim.run(120, until=lambda: sim.code() == "message_unchecked"))
        self.assertIsNone(sim.p().running)
        loads = len(sim.requests(kind="sme"))
        samples = self.outage_board(sim, a)
        self.assertEqual([s for s in samples if s[2:] != (AMBER, "", AMBER, "", AMBER, [])], [])
        self.assertEqual(len(sim.requests(kind="sme")), loads)                 # standalone never Loads
        st = a.poller.local_statuses()[sim.mid]
        self.assertEqual((st.state.code, st.running_unchecked), ("unknown_while_down", True))
        self.assertIn("message_unreadable", [x.kind for x in st.alerts if x.active])
        # Back with working Loads: two agreeing Loads give red drift
        sim.fake.web_reply_hook = None
        self.share_back(sim, a)
        self.assertTrue(sim.run(130, until=lambda: sim.code() == "drift"))
        self.assertEqual(sim.p().running.origin_line, "Product of Mexico")

    def test_random_load_failures_then_an_outage_are_never_green(self):
        # The reviewer's rate (40 % of Loads failing) on 10 seeds, the share going down as soon as share mode
        # says red drift or amber "can't check the message": the standalone board is never green and never
        # shows E's origin, the drift seeds red and ringing
        out = []
        for seed in range(10):
            sim = self.sim()
            sim.expect()
            a = sim.pc("akim")
            self.steady(sim)
            with sim.fake.lock:
                sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
            rng = random.Random(seed)
            self.failing_loads(sim, lambda k: rng.random() < 0.4)
            self.assertTrue(sim.run(600, until=lambda: sim.code() in ("drift", "message_unchecked")), seed)
            code = sim.code()
            samples = self.outage_board(sim, a, checks=2)
            out.append(code)
            want = {(RED, RED)} if code == "drift" else {(AMBER, AMBER), (RED, RED)}
            bad = [s for s in samples if (s[2], s[4]) not in want or ORIGIN in (s[3], s[5])]
            self.assertEqual(bad, [], (seed, code))
        self.assertIn("message_unchecked", out)                 # both kinds of seed were seen
        self.assertIn("drift", out)

    def test_a_pc_that_never_read_p_starts_its_checks_afresh(self):
        # §6.12 item 3: nothing to carry on a PC whose poller never read P (Stripes started while the share
        # was already down; here its data comes from before): its checks start from nothing, as before
        sim = self.sim()
        sim.expect()
        sim.pc("bob")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
        a = sim.pc("akim")
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        a.poller.set_monitor_open(True, False)
        self.assertEqual(a.poller.seen_polls(), {})
        self.assertTrue(sim.run(150, until=lambda: sim.mid in a.poller.local_statuses()))
        check = a.poller._standalone.checks[sim.pid]
        self.assertIsNone(check._seed())
        a.watcher.run_once()
        self.assertNotIn("drift", [x.kind for x in machine_on_board(a, sim.mid).alerts])

    def board_after(self, sim, a, seconds):
        """(mode, code, colour, origin) on PC a's board once a second."""
        seen = []
        for _ in range(int(seconds)):
            sim.run(1)
            a.watcher.run_once()
            mv = machine_on_board(a, sim.mid)
            seen.append((a.poller.mode, mv.code, mv.colour, mv.origin_line))
        return seen

    def share_returns_after(self, change, *, monitor=True):
        """Standalone (or idle) for a while, change() at the printer meanwhile, then the share comes back.
        Returns PC a and the board samples from the moment the share is back."""
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.watcher.run_once()
        if monitor:
            a.poller.set_monitor_open(True, False)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        sim.run(LOGIC.lock_stale_s + 5)
        change(sim)
        sim.run(2 * LOGIC.standalone_poll_s + 5)
        a.watcher.run_once()
        sim.world.fs.down = False
        a.store.probe_now()                 # the store's probe runs on a real thread: wait for it
        deadline = time.monotonic() + 5
        while not a.store.available and time.monotonic() < deadline:
            time.sleep(0.01)
        return sim, a, self.board_after(sim, a, 45)

    def assert_never_green_before_judged(self, samples, final):
        self.assertEqual([s for s in samples if s[2] == "green"], [])
        self.assertEqual(samples[-1][1], final)

    def test_share_back_after_printing_off_is_never_green_first(self):
        # P on the share still says green "Printing" from before the outage; the worker that starts when
        # the share is back shows this PC's standalone finding (red), not P's green, until it judges
        def off(sim):
            with sim.fake.lock:
                sim.fake.printing = False
        sim, a, samples = self.share_returns_after(off)
        self.assert_never_green_before_judged(samples, "printing_off")
        shared = [s for s in samples if s[0] == "share"]
        self.assertEqual(shared[-1][2], RED)
        self.assertLessEqual({s[2] for s in shared}, {GREY, AMBER, RED})

    def test_share_back_after_a_message_change_is_never_green_first(self):
        sim, a, samples = self.share_returns_after(
            lambda sim: install(sim.fake, 10, "SOMETHING-ELSE", "Product of Mexico"))
        self.assert_never_green_before_judged(samples, "drift")
        self.assertEqual([s for s in samples if s[3] == ORIGIN], [])      # the old origin is never shown
        self.assertEqual(samples[-1][3], "Product of Mexico")

    def test_share_back_without_a_monitor_is_never_green_first(self):
        # No standalone checks: NOT MONITORED during the outage, then "Checking..." until judged
        sim, a, samples = self.share_returns_after(
            lambda sim: install(sim.fake, 10, "SOMETHING-ELSE", "Product of Mexico"), monitor=False)
        self.assert_never_green_before_judged(samples, "drift")
        self.assertIn("checking", [s[1] for s in samples])
        self.assertEqual([s for s in samples if s[0] == "share" and s[3] == ORIGIN], [])

    def test_share_back_after_another_message_never_shows_the_old_drifts_origin(self):
        # A drift to Mexico is carried into standalone; another message is selected at the printer meanwhile,
        # so standalone shows the origin unknown. When the share is back the worker starts from the share's
        # P (red drift, R = Mexico): R no longer says what prints, so it starts unknown until its first Load
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
        for _ in range(int(LOGIC.heartbeat_s + LOGIC.watch_s)):
            sim.run(1)
            a.watcher.run_once()
        a.poller.set_monitor_open(True, True)
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        install(sim.fake, 10, "SOMETHING-ELSE", "Product of Peru")
        sim.run(2 * LOGIC.standalone_poll_s + 1)
        a.watcher.run_once()
        self.assertEqual(machine_on_board(a, sim.mid).origin_line, "")
        sim.world.fs.down = False
        a.store.probe_now()
        deadline = time.monotonic() + 5
        while not a.store.available and time.monotonic() < deadline:
            time.sleep(0.01)
        samples = []
        for _ in range(40):
            sim.run(1)
            a.watcher.run_once()
            mv = machine_on_board(a, sim.mid)
            row = next(r for r in hx_state.floor_board_rows(a.watcher.latest) if r.machine_id == sim.mid)
            samples.append((a.poller.mode, mv.code, mv.colour, mv.origin_line, row.origin_line))
        self.assertEqual([s for s in samples if "Mexico" in s[3] or "Mexico" in s[4]], [])
        self.assertEqual({s[2] for s in samples if s[0] == "share"} - {RED, AMBER, GREY}, set())
        self.assertEqual(samples[-1][1:4], ("drift", RED, "Product of Peru"))

    def test_share_back_with_the_printer_silent(self):
        def silent(sim):
            sim.fake.go_silent()
            self.addCleanup(sim.fake.go_silent, False)
        sim, a, samples = self.share_returns_after(silent)
        self.assertEqual([s for s in samples if s[2] == "green"], [])
        self.assertIn(samples[-1][1], ("off", "message_unchecked"))

    def test_share_back_unchanged_turns_green_after_its_checks(self):
        sim, a, samples = self.share_returns_after(lambda sim: None)
        self.assertEqual(samples[-1][:3], ("share", "running_confirmed", "green"))
        first_green = next(i for i, s in enumerate(samples) if s[2] == "green")
        self.assertTrue(all(s[3] == "" for s in samples[:first_green] if s[0] == "share" and s[1] == "checking"))

    def test_first_start_after_a_long_gap_is_never_green_first(self):
        # Every PC closed overnight; the message was changed at the panel. The first PC that starts sees
        # last night's P, written long ago: "Checking..." until it has judged, never last night's green
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.poller.stop()
        sim.pcs.remove(a)
        install(sim.fake, 10, "SOMETHING-ELSE", "Product of Mexico")
        sim.clock.advance(3600)
        b = sim.pc("jsmith")
        samples = self.board_after(sim, b, 40)
        self.assert_never_green_before_judged(samples, "drift")
        self.assertEqual([s for s in samples if s[3] == ORIGIN], [])
        # Fixed at the panel overnight: last night's red stays until share mode has judged it green
        b.poller.stop()
        sim.pcs.remove(b)
        install(sim.fake)
        sim.clock.advance(3600)
        c = sim.pc("jsmith", pid=2)
        samples = self.board_after(sim, c, 40)
        self.assertEqual(samples[-1][:3], ("share", "running_confirmed", "green"))
        first_green = next(i for i, s in enumerate(samples) if s[2] == "green")
        self.assertEqual({s[2] for s in samples[:first_green]}, {RED})

    def test_a_quick_takeover_keeps_the_state(self):
        # A handover within not_monitored_s keeps P's state: nothing turns "Checking..." on the board
        sim = self.sim()
        sim.expect()
        r0, r1 = self.ranked(sim)
        self.steady(sim)
        r0.poller.stop()
        sim.pcs.remove(r0)
        codes = []
        self.assertTrue(sim.run(30, until=lambda: codes.append(sim.code()) or sim.p().poller == r1.key))
        sim.run(20, until=lambda: codes.append(sim.code()) or False)
        self.assertEqual(set(codes), {"running_confirmed"})

    def ranked(self, sim):
        a, b = sim.pc("akim"), sim.pc("bob")
        return (a, b) if assign_order(sim, a, b)[0] == a.key else (b, a)

    def test_a_known_drift_from_before_a_new_send_isnt_carried(self):
        # P's drift was judged against an older E (a Send came after it): nothing to carry
        sim = self.sim()
        e = sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(70, until=lambda: sim.code() == "drift"))
        a.poller.set_monitor_open(True, True)
        sim.sender.write_expected(dataclasses.replace(e, send_id=SID2, origin_line="Product of Mexico",
                                                      fingerprint=hx_message.fingerprint(sim.read_back(9))))
        a.store.read_expected(sim.mid)                       # the PC's cached E is the new one
        sim.world.fs.down = True
        self.addCleanup(setattr, sim.world.fs, "down", False)
        self.assertTrue(sim.run(90, until=lambda: a.poller.mode == "standalone"))
        sim.run(LOGIC.standalone_poll_s + 1)
        st = a.poller.local_statuses()[sim.mid]
        self.assertNotIn("drift", [x.kind for x in st.alerts])
        self.assertEqual(st.state.code, "running_confirmed")


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
        # write at the end; inside that write (it lands late, over rank 1's P, and proves nothing: share
        # mode stops, §6.4 step 7.2); and in a daylight-saving clock set, before it records the TD in P
        real_judge = hx_state.judge
        for where in ("PRIN", "judge", "P write", "clock set"):
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
                real_write_poll = r0.store.write_poll

                def write_poll(status, real_write_poll=real_write_poll):
                    callers = [f.name for f in traceback.extract_stack()]
                    if freeze.get("at") == "P write" and "_write_judgement" in callers:
                        frozen()            # its clock reading before the write is already taken
                    return real_write_poll(status)
                r0.store.write_poll = write_poll
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
                self.assertEqual(r0.poller._fenced, {})     # a paused PC isn't a stuck worker
                if where == "P write":
                    # Its late write landed over rank 1's P (a share can't refuse it), and stopped share mode
                    self.assertEqual(sim.p().poller, r0.key)
                    self.assertEqual(r0.entries("polling_stopped")[-1]["reason"], "share")
                else:
                    self.assertEqual(sim.p().poller, r1.key)
                # Both PCs run again: rank 1 sees rank 0's heartbeat and hands back; rank 0 polls on only then
                self.assertTrue(sim.run(30, until=lambda: r1.worker() is None and sim.p().poller == r0.key))
                last = r1.entries("polling_stopped")[-1]
                self.assertEqual(last["yielded_to"], r0.key)
                self.assertIn(last["reason"], ("handover", "rank") if where == "P write" else ("handover",))
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
        # The fake answers at once, so a request keeps http_timeout_s + 2 x read_retry_s within stale_s / 4,
        # as the real timings do (states.md §6.4: a read fits after a keep-fresh write)
        timings = dataclasses.replace(TIMINGS.scaled(RT), http_timeout_s=2.7 * RT)
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
        self.assertTrue(wait_for(lambda: sim.code() == "running_confirmed", 3.0 * RS))
        w = a.worker()
        with mock.patch("traceback.print_exc"):
            a.broken[0] = 2
            self.assertTrue(wait_for(lambda: a.broken[0] == 0, 1.0 * RS))
            before = sim.p().counter
            self.assertTrue(wait_for(lambda: sim.p().counter > before + 2, 1.0 * RS))
        self.assertTrue(w.thread.is_alive())
        self.assertFalse(w.done)
        self.assertIs(a.worker(), w)
        self.assertTrue(wait_for(lambda: sim.code() == "running_confirmed", 1.0 * RS))

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

    def ringing_drift(self, sim):
        """A same-name drift with its alert ringing on PC akim's board (the watcher not yet run)."""
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(80, until=lambda: sim.code() == "drift"))
        sim.run(5)
        return a

    def test_an_ack_during_a_pass_stays_quiet(self):
        # §4.5 item 2: the UI acknowledges while hx-watch builds its board from reads made before the ack.
        # That pass's board must carry the ack too, or the sound starts again until the next pass
        sim = self.sim()
        a = self.ringing_drift(sim)
        a.watcher.run_once()
        ringing = lambda board: hx_state.sounding_alerts(board, a.identity, floor_board=True)
        ids = [x.id for x in ringing(a.watcher.latest)]
        self.assertTrue(ids)
        boards = []
        a.bus.pump()
        a.bus.subscribe(BoardUpdate, lambda u: boards.append(u.board))
        real = hx_state.board_view

        def board_view(*args, **kw):
            a.watcher.acknowledge(ids)
            return real(*args, **kw)
        with mock.patch.object(hx_state, "board_view", board_view):
            a.watcher.run_once()
        a.bus.pump()
        self.assertEqual([bool(ringing(b)) for b in boards], [False, False])
        self.assertEqual(ringing(a.watcher.latest), [])
        self.assertEqual(machine_on_board(a, sim.mid).code, "drift")       # the problem is still shown

    def test_an_ack_during_a_slow_share_read_stays_quiet(self):
        # The same with real threads: hx-watch is in a slow lock read when the operator acknowledges
        sim = self.sim()
        a = self.ringing_drift(sim)
        a.watcher.run_once()
        ringing = lambda board: hx_state.sounding_alerts(board, a.identity, floor_board=True)
        ids = [x.id for x in ringing(a.watcher.latest)]
        self.assertTrue(ids)
        boards = []
        a.bus.pump()
        a.bus.subscribe(BoardUpdate, lambda u: boards.append(u.board))
        real = a.store.read_lock
        reading = threading.Event()

        def slow_read_lock(pid):
            reading.set()
            time.sleep(0.3)
            return real(pid)
        a.store.read_lock = slow_read_lock
        t = threading.Thread(target=a.watcher.run_once)
        t.start()
        self.assertTrue(reading.wait(5))
        a.watcher.acknowledge(ids)
        t.join(10)
        self.assertFalse(t.is_alive())
        a.bus.pump()
        self.assertEqual(len(boards), 2)
        self.assertEqual([bool(ringing(b)) for b in boards], [False, False])
        self.assertEqual(ringing(a.watcher.latest), [])

    def test_a_pass_ending_inside_acknowledge_is_never_undone(self):
        # acknowledge() applies its acks to latest and posts it. A pass that ends in the middle of that
        # must not be overwritten by the older board (a fresh drift shown green with the old origin)
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.watcher.run_once()
        code = lambda b: b.lines[0].machines[0].code
        self.assertEqual(code(a.watcher.latest), "running_confirmed")
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(80, until=lambda: sim.code() == "drift"))
        sim.run(5)
        boards = []
        a.bus.pump()
        a.bus.subscribe(BoardUpdate, lambda u: boards.append(u.board))
        real = hx_poller._with_acks
        passes = []

        def with_acks(board, acks):
            if not passes:
                # hx-watch runs a whole pass while the UI thread is inside acknowledge()
                t = threading.Thread(target=a.watcher.run_once)
                passes.append(t)
                t.start()
                t.join(0.5)
            return real(board, acks)
        with mock.patch.object(hx_poller, "_with_acks", with_acks):
            a.watcher.acknowledge(["some.other.alert"])
        passes[0].join(10)
        self.assertFalse(passes[0].is_alive())
        a.bus.pump()
        self.assertEqual(code(a.watcher.latest), "drift")
        self.assertEqual(code(boards[-1]), "drift")
        self.assertIs(boards[-1], a.watcher.latest)
        self.assertEqual(machine_on_board(a, sim.mid).origin_line, "Product of Mexico")

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
        """run_s: the run's length at RT = 0.02 (scaled by RS). The fake's latency is 0.4 x stale_s / 2, which
        http_timeout_s must exceed, so a request can't keep http_timeout_s + 2 x read_retry_s within
        stale_s / 4 here: the keep-fresh rule is tested at its limits."""
        timings = dataclasses.replace(TIMINGS.scaled(RT), http_timeout_s=4.5 * RT, web_repeat_window_s=10 * RT)
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
                got.wait(1.0 * RS)
            return real_lock(printer_id)
        a.store.write_poll, a.store.read_lock = write_poll, read_lock
        a.poller.start()
        self.addCleanup(a.poller.stop)
        deadline = time.monotonic() + run_s * RS
        while time.monotonic() < deadline:
            time.sleep(0.2)
        a.poller.stop()
        return sim, timings, writes, grabbed

    def check(self, sim, timings, writes, *, lock_s=0.0, strict=True):
        half = timings.stale_s / 2
        latency = 0.4 * half
        bound = half + timings.http_timeout_s
        slack = 1.5 * RT                                # thread scheduling on a busy test machine
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
        slow = TIMINGS.scaled(RT).stale_s / 4 - RT
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


def ranked_names(sim, names):
    """names in rank order for the sim's printer (rank 0 first), as assign() gives it."""
    return sorted(names, key=lambda n: (-hx_share.score(sim.pid, f"PC-{n.upper()}"), sim.world.identity(n).key))


def record_calls(sim, pcs):
    """{PC key: [(clock.monotonic(), call, tag)]} of every printer call each PC's client makes."""
    sent = {pc.key: [] for pc in pcs}
    for pc in pcs:
        real_client = pc.clients.client

        def client(printer, real_client=real_client, pc=pc):
            c = real_client(printer)
            if not getattr(c, "_calls_recorded", False):
                for name in ("read", "command", "load_slot", "fonts", "firmware", "login"):
                    def call(*args, _fn=getattr(c, name), _name=name, **kw):
                        sent[pc.key].append((sim.clock.monotonic(), _name, args[0] if args else ""))
                        return _fn(*args, **kw)
                    setattr(c, name, call)
                c._calls_recorded = True
            return c
        pc.clients.client = client
    return sent


def record_writes(sim, pc):
    """[(clock.monotonic(), PollStatus)] of every P write by the PC."""
    writes = []
    real = pc.store.write_poll

    def write_poll(status):
        real(status)
        writes.append((sim.clock.monotonic(), status))
    pc.store.write_poll = write_poll
    return writes


def stall(sim, w):
    """The worker's thread is stuck in some call for good: its tick never runs again. Returns un-stall."""
    def stuck():
        w.next_wake = sim.clock.monotonic() + 1.0
    w.tick = stuck

    def restore():
        del w.tick
        w.next_wake = sim.clock.monotonic()
    return restore


# --------------------------------------------------------------------------
# A stuck worker (§6.2 item 3.0) and judged freshness (§6.14)
# --------------------------------------------------------------------------

class StuckWorkerTests(Base):
    RECOVERY_S = 70.0           # 30 s to notice + a beat + a pass + 15 s quiet P + settle + a cycle (§6.2 item 3.0)

    def two(self, sim):
        n0, n1 = ranked_names(sim, ["akim", "bob"])
        return sim.pc(n0), sim.pc(n1)

    def test_a_stuck_worker_is_fenced_and_its_printer_declined(self):
        # Rank 0's worker is stuck while its PC heart-beats. Without the watchdog the backup took over and
        # handed straight back, forever, and its bookkeeping writes kept the old green looking fresh
        sim = self.sim()
        sim.expect()
        r0, r1 = self.two(sim)
        sent = record_calls(sim, [r0, r1])
        self.steady(sim, 60)
        self.assertEqual(sim.p().poller, r0.key)
        w = r0.worker()
        writes = record_writes(sim, r0)
        restore = stall(sim, w)
        t0 = sim.clock.monotonic()
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")    # edited at the panel meanwhile
        board = PC(sim, "carol")                                        # a PC that only watches
        views = []

        def look():
            board.watcher.run_once()
            m = machine_on_board(board, sim.mid)
            views.append((sim.clock.monotonic() - t0, m.code, m.colour, m.monitored, m.checked_age_s, m.origin_line))
            return False
        for _ in range(int(self.RECOVERY_S)):
            sim.run(1)
            look()
        replies = [c for c in sent[r1.key] if c[0] >= t0]
        self.assertTrue(replies and replies[0][0] - t0 <= self.RECOVERY_S, replies[:3])
        (stuck,) = [e for e in r0.entries("internal_error") if e.get("error") == "worker_stuck"]
        self.assertEqual((stuck["printer_id"], stuck["machine_id"]), (sim.pid, sim.mid))
        self.assertIn(sim.pid, next(h for h in sim.observer.live_heartbeats() if h.key == r0.key).declined)
        self.assertNotIn(sim.pid, r0.poller._running_printers())
        self.assertTrue(w.fenced)
        # The board said NOT MONITORED from not_monitored_s after the last judgement, never the old green
        late = [v for v in views if v[0] >= LOGIC.not_monitored_s + LOGIC.watch_s]
        self.assertTrue(late and late[0][1] in ("not_monitored", "checking"), views[:40])
        self.assertEqual([v for v in late if v[2] == "green" and v[3]], [])
        # The stuck call returns (before the decline ends): the fenced worker makes no request and no P write
        before = len(sent[r0.key])
        n_writes = len([x for x in writes if x[1].machine_id == sim.mid])
        restore()
        sim.run(10)
        self.assertTrue(w.done)
        self.assertEqual(sent[r0.key][before:], [])
        self.assertEqual(len([x for x in writes if x[1].machine_id == sim.mid]), n_writes)
        self.assertIn(sim.pid, r0.poller._fenced)
        # Rank 1 finds the drift: red, with a sounding alert
        self.assertTrue(sim.run(60, until=lambda: sim.code() == "drift"))
        self.assertTrue(any(a.kind == "drift" and a.sound for a in sim.p().alerts))
        self.no_violations(sim)

    def test_a_declined_printer_comes_back_once_its_worker_has_ended(self):
        # The decline lasts until the fenced worker has ended and lock_stale_s has passed: then the normal
        # handover gives the printer back to rank 0
        sim = self.sim()
        sim.expect()
        r0, r1 = self.two(sim)
        self.steady(sim, 60)
        # Fences more than DECLINE_MEMORY_S apart don't add up: this one declines for lock_stale_s again
        r0.poller._strikes[sim.pid] = (4, sim.clock.monotonic() - hx_poller.DECLINE_MEMORY_S - 1)
        restore = stall(sim, r0.worker())
        self.assertTrue(sim.run(60, until=lambda: sim.pid in r0.poller._fenced))
        self.assertEqual(r0.poller._strikes[sim.pid][0], 1)
        restore()
        self.assertTrue(sim.run(LOGIC.lock_stale_s + 60, until=lambda: sim.p().poller == r0.key
                                and r0.worker() is not None and r0.worker().rank_checked))
        self.assertEqual(r0.poller._fenced, {})
        self.assertTrue(sim.run(30, until=lambda: sim.code() == "running_confirmed"))
        self.no_violations(sim)

    def test_a_pc_that_cant_write_p_gives_the_printer_away(self):
        # No hang at all: every P write of rank 0's worker fails while its heartbeat works, because of a file
        # only that PC can't write, or a bug
        for fault in ("permission", "bug"):
            with self.subTest(fault=fault):
                sim = self.sim()
                sim.expect()
                r0, r1 = self.two(sim)
                sent = record_calls(sim, [r0, r1])
                self.steady(sim, 60)
                if fault == "permission":
                    real = r0.store.fs.write_whole

                    def write_whole(path, data, real=real):
                        if path.endswith(f"{sim.mid}.poll.json"):
                            raise PermissionError(13, "access denied (test)", path)
                        return real(path, data)
                    r0.store.fs.write_whole = write_whole
                else:
                    def broken(status):
                        raise RuntimeError("test: a bug in the P write")
                    r0.store.write_poll = broken
                t0 = sim.clock.monotonic()
                with mock.patch("traceback.print_exc"):
                    self.assertTrue(sim.run(self.RECOVERY_S + 15,
                                            until=lambda: any(c[0] >= t0 + 1 for c in sent[r1.key])))
                self.assertIn(sim.pid, r0.poller._fenced)
                self.assertEqual(len([e for e in r0.entries("internal_error") if e.get("error") == "worker_stuck"]), 1)
                self.no_violations(sim)

    def test_a_fault_that_stays_keeps_the_printer_with_the_backup(self):
        # Rank 0 can't write this one P, for ten minutes. The decline used to end every lock_stale_s:
        # rank 0 took the printer back, failed, and was fenced 30 s later, a 45 s gap every ~95 s. Now each
        # fence in a row doubles the decline, and a decline that ends on probation is renewed at once
        sim = self.sim()
        sim.expect()
        r0, r1 = self.two(sim)
        sent = record_calls(sim, [r0, r1])
        self.steady(sim, 60)
        real = r0.store.fs.write_whole

        def write_whole(path, data):
            if path.endswith(f"{sim.mid}.poll.json"):
                raise PermissionError(13, "access denied (test)", path)
            return real(path, data)
        r0.store.fs.write_whole = write_whole
        t0 = sim.clock.monotonic()
        board = PC(sim, "carol")
        views = []
        for _ in range(600):
            sim.run(1)
            board.watcher.run_once()
            m = machine_on_board(board, sim.mid)
            views.append((sim.clock.monotonic() - t0, m.code))
        calls = sorted(c[0] for c in sent[r0.key] + sent[r1.key] if c[0] >= t0)
        backup = [c[0] for c in sent[r1.key] if c[0] >= t0]
        recovered = backup[0]
        self.assertLessEqual(recovered - t0, self.RECOVERY_S)
        later = [c for c in calls if c >= recovered] + [sim.clock.monotonic()]
        gaps = [(round(a - t0), round(b - a)) for a, b in zip(later, later[1:]) if b - a >= LOGIC.not_monitored_s]
        self.assertEqual(gaps, [])
        self.assertEqual([v for v in views if v[0] >= recovered - t0 + 1 and v[1] in ("not_monitored", "checking")],
                         [])
        stuck = [e for e in r0.entries("internal_error") if e.get("error") == "worker_stuck"]
        self.assertEqual([e["declined_s"] for e in stuck], [60.0, 120.0, 240.0, 480.0][:len(stuck)])
        self.assertLessEqual(len(stuck), 4)
        self.assertIn(sim.pid, r0.poller._fenced)
        self.no_violations(sim)
        # The fault ends: after the decline the printer comes back to rank 0 and stays
        r0.store.fs.write_whole = real
        self.assertTrue(sim.run(hx_poller.DECLINE_MAX_S + 120, until=lambda: sim.p().poller == r0.key
                                and r0.worker() is not None and r0.worker().rank_checked))
        self.assertTrue(sim.run(60, until=lambda: sim.pid not in r0.poller._probation))
        n = len(r0.entries("internal_error"))
        sim.run(120)
        self.assertEqual((sim.p().poller, len(r0.entries("internal_error"))), (r0.key, n))
        self.no_violations(sim)

    def test_the_decline_doubles_up_to_a_cap(self):
        sim = self.sim()
        poller = sim.pc("akim").poller
        self.assertEqual(poller._decline_s("pr_x"), LOGIC.lock_stale_s)
        cap = hx_poller.DECLINE_MAX_S
        for count, want in ((1, 60.0), (2, 120.0), (4, 480.0), (5, cap), (9, cap)):
            poller._strikes["pr_x"] = (count, 0.0)
            self.assertEqual(poller._decline_s("pr_x"), want)

    def test_a_coordinator_that_was_paused_too_isnt_a_stuck_worker(self):
        # The whole PC slept: its workers didn't run either. The first pass after it gives them time
        sim = self.sim()
        sim.expect()
        r0, r1 = self.two(sim)
        self.steady(sim, 60)
        sim.run(60, pcs=[r1])                   # r0's whole PC is paused for a minute
        sim.run(60)
        self.assertEqual([e for e in r0.entries("internal_error") if e.get("error") == "worker_stuck"], [])
        self.assertEqual(r0.poller._fenced, {})
        self.assertEqual(sim.p().poller, r0.key)

    def test_bookkeeping_writes_dont_refresh_the_board(self):
        # judged moves only with a judgement or a paused write: a start, a settle and a yield keep it
        sim = self.sim()
        sim.expect()
        r0, r1 = self.two(sim)
        self.steady(sim, 60)
        judged = sim.p().judged
        self.assertIsNotNone(judged)
        stall(sim, r0.worker())
        r1_writes = record_writes(sim, r1)
        sim.run(25)                             # rank 1 takes over and hands straight back, before the watchdog
        self.assertTrue(r1_writes)
        self.assertEqual({x[1].judged for x in r1_writes}, {judged})
        board = PC(sim, "carol")
        sim.run(10)
        board.watcher.run_once()
        m = machine_on_board(board, sim.mid)
        self.assertNotEqual(m.colour, "green")
        self.assertIsNone(m.checked_age_s)      # "Checking...": judged long ago by its writer's clock

    def test_a_long_lock_shows_no_not_monitored_after_it(self):
        # Paused writes move judged: a Send holding the lock for 50 s doesn't show NOT MONITORED when it ends
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        lock = sim.observer.lock(sim.pid, purpose="send", run_id=SID)
        self.assertEqual(lock.acquire(), "held")
        codes = []
        for _ in range(50):
            sim.run(1)
            a.watcher.run_once()
            codes.append(machine_on_board(a, sim.mid).code)
        lock.release()
        for _ in range(15):
            sim.run(1)
            a.watcher.run_once()
            codes.append(machine_on_board(a, sim.mid).code)
        self.assertNotIn("not_monitored", codes)
        self.assertEqual(codes[-1], "running_confirmed")

    def test_an_older_writers_p_keeps_the_counter_age(self):
        # A P without `judged` (an older Stripes wrote it): its counter's age counts, as before
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        a.poller.stop()
        sim.pcs.remove(a)
        old = sim.world.store(sim.world.identity("old", sender=False))
        base = dataclasses.replace(sim.p(), judged=None, judged_at=0.0)
        for i in range(8):
            old.write_poll(dataclasses.replace(base, counter=base.counter + 1 + i, poller=old.identity.key))
            sim.clock.advance(5.0)
            a.watcher.run_once()
            m = machine_on_board(a, sim.mid)
            self.assertEqual((m.code, m.monitored), ("running_confirmed", True))
            self.assertLess(m.checked_age_s, 10.0)


# --------------------------------------------------------------------------
# A superseded worker's writes (§6.4 step 7.2) and late writes (storage.md §12.6 rule 6)
# --------------------------------------------------------------------------

class SupersededTests(Base):
    def test_a_pc_paused_between_ticks_never_writes_over_a_newer_poller(self):
        # r0's whole PC sleeps; r2, ranked above it, starts meanwhile, takes over and commits a drift. r0
        # wakes and is asked to hand over: its yield write would put its old green over r2's drift
        sim = self.sim()
        sim.expect()
        n2, n0, n1 = ranked_names(sim, ["akim", "bob", "carol"])
        r0, r1 = sim.pc(n0), sim.pc(n1)
        self.steady(sim, 60)
        self.assertEqual(sim.p().poller, r0.key)
        r2 = sim.pc(n2)
        r0_writes = record_writes(sim, r0)
        sent = record_calls(sim, [r0])
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(120, pcs=[r1, r2], until=lambda: sim.code() == "drift" and sim.p().poller == r2.key))
        drift_id = next(a.id for a in sim.p().alerts if a.kind == "drift")
        woke = sim.clock.monotonic()
        codes = []
        sim.run(60, until=lambda: codes.append(sim.code()) or False)
        self.assertEqual([x for x in r0_writes if x[0] >= woke], [])
        self.assertEqual([c for c in sent[r0.key] if c[0] >= woke], [])
        self.assertNotIn("running_confirmed", codes)
        self.assertIn(drift_id, [a.id for a in sim.p().alerts])
        self.no_violations(sim)

    def pause_inside(self, sim, pc, where, others, seconds):
        """pc's whole PC pauses for seconds at `where` in its worker's cycle while `others` run."""
        w = pc.worker()
        state = {"armed": True}

        def pause():
            state["armed"] = False
            sim.run(seconds, pcs=others)
            state["woke"] = sim.clock.monotonic()
        if where == "after the lock check":
            real = w._lock_check

            def lock_check():
                out = real()
                if state["armed"] and sys._getframe(1).f_code.co_name == "_call":
                    pause()
                return out
            w._lock_check = lock_check
        else:                                   # inside the cycle's step-3 lock read, which is then too slow
            real = pc.store.read_lock

            def read_lock(printer_id):
                out = real(printer_id)
                if (state["armed"] and sys._getframe(1).f_code.co_name == "_lock_check"
                        and sys._getframe(2).f_code.co_name == "_cycle"):
                    pause()
                return out
            pc.store.read_lock = read_lock
        return state

    def test_a_pc_paused_mid_cycle_neither_writes_nor_sends_over_a_takeover(self):
        for where in ("after the lock check", "in the step-3 lock read"):
            with self.subTest(pause=where):
                sim = self.sim()
                sim.expect()
                n0, n1 = ranked_names(sim, ["akim", "bob"])
                r0, r1 = sim.pc(n0), sim.pc(n1)
                sent = record_calls(sim, [r0, r1])
                self.steady(sim, 60)
                r0_writes = record_writes(sim, r0)
                state = self.pause_inside(sim, r0, where, [r1], 45.0)
                self.assertTrue(sim.run(10, pcs=[r0], until=lambda: "woke" in state))
                woke = state["woke"]
                self.assertTrue([c for c in sent[r1.key] if c[0] < woke])      # rank 1 took over and polled
                self.assertTrue(sim.run(30, until=lambda: sim.p().poller == r1.key and sim.p().yielded_to == r0.key))
                back = sim.clock.monotonic()
                self.assertEqual([c for c in sent[r0.key] if woke <= c[0] < back], [])
                self.assertEqual([x for x in r0_writes if woke <= x[0] < back], [])
                back_green = lambda: (sim.code(), sim.p().poller) == ("running_confirmed", r0.key)
                self.assertTrue(sim.run(30, until=back_green))
                self.no_violations(sim)

    def test_a_resumed_worker_adopts_a_newer_yield(self):
        # Another PC polled, committed a drift and yielded back while this worker was paused: it continues
        # from that PC's P, never from its own older green, and the drift alert keeps its id
        sim = self.sim()
        sim.expect()
        n0, n1 = ranked_names(sim, ["akim", "bob"])
        r0, r1 = sim.pc(n0), sim.pc(n1)
        self.steady(sim, 60)
        w = r0.worker()
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(120, pcs=[r1], until=lambda: sim.code() == "drift" and sim.p().poller == r1.key))
        drift_id = next(a.id for a in sim.p().alerts if a.kind == "drift")
        r0.poller._coordinate()                # its coordinator wakes first; the worker lags behind
        self.assertTrue(sim.run(10, pcs=[r1], until=lambda: sim.p().yielded_to == r0.key))
        self.assertIs(r0.worker(), w)
        r0_writes = record_writes(sim, r0)
        seen = []
        drift_ids = lambda: [a.id for a in sim.p().alerts if a.kind == "drift"]
        sim.run(30, until=lambda: seen.append((sim.code(), drift_ids())) or False)
        self.assertNotIn("running_confirmed", [c for c, _ in seen])
        self.assertNotIn("running_confirmed", [x[1].state.code for x in r0_writes])
        self.assertEqual({tuple(ids) for c, ids in seen if c == "drift"}, {(drift_id,)})
        self.no_violations(sim)

    def test_a_late_write_by_a_superseded_owner_is_ignored(self):
        # A write r0 gave up on (share timeout) lands after r1 took over: r1 neither adopts it nor yields to
        # a PC that isn't polling, and its next write puts its own P back
        sim = self.sim()
        sim.expect()
        n0, n1 = ranked_names(sim, ["akim", "bob"])
        r0, r1 = sim.pc(n0), sim.pc(n1)
        self.steady(sim, 60)
        stale = sim.p()                         # r0's last P before its PC dropped out
        r0.poller.stop()
        sim.pcs.remove(r0)
        with sim.fake.lock:
            sim.fake.slots[9] = design(9, NAME, "Product of Mexico")
        self.assertTrue(sim.run(120, until=lambda: sim.code() == "drift" and sim.p().poller == r1.key))
        drift_id = next(a.id for a in sim.p().alerts if a.kind == "drift")
        sim.run(LOGIC.poll_fast_s)
        # The abandoned write lands now: r0's content and counter from before, written long ago
        landed = dataclasses.replace(stale, counter=stale.counter + 1)
        sim.world.store(r0.identity).write_json(f"status/machines/{sim.mid}.poll.json",
                                                {"file_type": "poll", **hx_model.to_json(landed)})
        codes = []
        sim.run(LOGIC.poll_fast_s + 1, until=lambda: codes.append(sim.code()) or False)
        self.assertEqual(sim.code(), "drift")
        self.assertEqual(sim.p().poller, r1.key)
        self.assertIn(drift_id, [a.id for a in sim.p().alerts])
        self.assertTrue(r1.entries("late_write_ignored"))
        self.assertEqual([e for e in r1.entries("polling_stopped")], [])
        sim.run(30)
        self.assertEqual((sim.code(), sim.p().poller), ("drift", r1.key))
        self.no_violations(sim)

    def test_a_late_write_with_the_backups_start_counter_is_ignored(self):
        # The likeliest collision: r0's late write and r1's start write both counted on from the same P,
        # so they carry the same counter. Landing before r1's next write, it used to be adopted and
        # yielded to (r0 isn't live): nobody polled until P went stale again
        sim = self.sim()
        sim.expect()
        n0, n1 = ranked_names(sim, ["akim", "bob"])
        r0, r1 = sim.pc(n0), sim.pc(n1)
        self.steady(sim, 60)
        stale = sim.p()
        r0.poller.stop()
        sim.pcs.remove(r0)
        self.assertTrue(sim.run(60, until=lambda: r1.worker() is not None and r1.worker().started))
        start = sim.p()
        self.assertEqual((start.poller, r1.worker().p.counter), (r1.key, start.counter))
        sent = record_calls(sim, [r1])
        landed = dataclasses.replace(stale, counter=start.counter)
        self.assertGreaterEqual(sim.clock.time() - landed.written_at, LOGIC.stale_s / 2)
        sim.world.store(r0.identity).write_json(f"status/machines/{sim.mid}.poll.json",
                                                {"file_type": "poll", **hx_model.to_json(landed)})
        at = sim.clock.monotonic()
        sim.run(hx_share.settle_s(LOGIC) + LOGIC.poll_fast_s + 1)
        self.assertEqual(r1.entries("polling_stopped"), [])
        self.assertTrue(r1.entries("late_write_ignored"))
        self.assertTrue([c for c in sent[r1.key] if c[0] >= at])
        self.assertEqual(sim.p().poller, r1.key)
        sim.run(30)
        self.assertEqual((sim.code(), sim.p().poller), ("running_confirmed", r1.key))
        self.no_violations(sim)


# --------------------------------------------------------------------------
# The second reading (§3.8, §6.4 step 10) and printing that can't be read (§6.6)
# --------------------------------------------------------------------------

class SecondReadingTests(Base):
    def test_a_stray_prin_then_no_reply_never_commits(self):
        # One stray PRIN 0, and the second reading's PRIN gets no reply: the first value confirms nothing
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        answers = [sim.fake.DROP, sim.fake.DROP, "0"]
        sim.fake.override("PRIN", lambda: answers.pop() if answers else None)
        codes = []
        sim.run(30, until=lambda: codes.append(sim.code()) or False)
        self.assertEqual(answers, [])
        self.assertNotIn("printing_off", codes)
        self.assertEqual(a.entries("alert_started"), [])
        self.assertEqual(sim.code(), "running_confirmed")

    def test_a_re_read_that_cant_be_made_takes_prevs_value(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        w = a.worker()
        p = w.p
        cyc = hx_poller._Cycle(a.clients.client(a.data.current.printers[sim.pid]), 0)
        r = hx_state.Reading(at=sim.clock.time(), reachable=True, printing=False, ink_level=4, clock_offset_s=900.0,
                             daily={"values": {"ENWHD": 99}})
        for tag in ("PRIN", "INKL", "DATE", "ENWHD"):
            sim.fake.override(tag, "7")         # busy
        w._second_reading(cyc, r, ["PRIN", "INKL", "clock", "daily:ENWHD"], a.data.current)
        for tag in ("PRIN", "INKL", "DATE", "ENWHD"):
            sim.fake.override(tag, None)
        self.assertEqual((r.printing, r.printing_unchecked, r.ink_level, r.clock_offset_s), (None, None, None, None))
        self.assertEqual(r.daily["values"].get("ENWHD"), (p.daily or {}).get("values", {}).get("ENWHD"))

    def hold_gate(self, sim, client, seconds):
        held = threading.Event()

        def holder():
            with client._gate:
                held.set()
                time.sleep(0.3)                 # the poller's call is waiting for the gate now
                sim.clock.advance(seconds)
        t = threading.Thread(target=holder, daemon=True)
        t.start()
        held.wait(2)
        return t

    def test_a_second_reading_login_keeps_the_cycle_deadline(self):
        # A Stop, Resume or clock set on this PC holds the gate: the login waits no longer than `end` and
        # sends nothing late (states.md §6.4 step 7.3); before, it waited gate_wait_s and then logged in
        timings = dataclasses.replace(LOGIC, gate_wait_s=3.0)
        sim = self.sim(timings=timings)
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim, 60)
        w = a.worker()
        client = a.clients.client(a.data.current.printers[sim.pid])
        before = len(sim.requests())
        cyc = hx_poller._Cycle(client, 0)
        r = hx_state.Reading(at=sim.clock.time(), reachable=True)
        t = self.hold_gate(sim, client, 20.0)
        w._second_reading(cyc, r, ["login"], a.data.current)
        t.join()
        self.assertEqual(sim.requests()[before:], [])
        self.assertFalse(cyc.login_done)
        # The client: a deadline it can't meet raises PrinterBusy at once and sends nothing
        t = self.hold_gate(sim, client, 20.0)
        with self.assertRaises(hx_model.PrinterBusy):
            client.login(deadline=sim.clock.monotonic() + 1.0)
        t.join()
        self.assertEqual(sim.requests()[before:], [])

    def login_change(self, sim, a, second_login):
        """The printer refuses the token (a read answers AuthFailed), and the second reading's login does
        `second_login`. Returns the codes P went through."""
        sim.fake.set_password("other")
        real = hx_protocol.PrinterClient.login

        def login(client, *, deadline=None):
            second_login()
        codes = []
        with mock.patch.object(hx_protocol.PrinterClient, "login", login):
            sim.run(40, until=lambda: codes.append(sim.code()) or False)
        self.assertIs(hx_protocol.PrinterClient.login, real)
        return codes

    def test_a_login_reply_that_cant_be_used_is_login_failed(self):
        for error in (hx_model.BadReply, hx_model.WebReadFailed):
            with self.subTest(error=error.__name__):
                sim = self.sim()
                sim.expect()
                a = sim.pc("akim")
                self.steady(sim)

                def second_login(error=error):
                    raise error(Msg("test"))
                codes = self.login_change(sim, a, second_login)
                self.assertEqual(codes[-1], "login_failed")

    def test_a_login_that_couldnt_be_made_confirms_nothing(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)

        def second_login():
            raise hx_model.PrinterBusy(Msg("test: this PC's gate"))
        codes = self.login_change(sim, a, second_login)
        self.assertNotIn("login_failed", codes)


class PrinUnreadTests(Base):
    AUTOPILOT = ("HXUI", "PRIN", "MSGNAM", "PMCNT", "INKL", "INKT", "PHTP", "DATE", "TIME")

    def busy(self, sim, on=True):
        for tag in self.AUTOPILOT:
            sim.fake.override(tag, "7" if on else None)

    def test_printing_that_cant_be_read_is_never_green(self):
        # Every Autopilot read answers busy while Loads work, and printing is switched off at the panel:
        # after PRIN_UNREAD_LIMIT cycles printing is unknown and the machine amber, never green
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        self.busy(sim)
        with sim.fake.lock:
            sim.fake.printing = False
        t0 = sim.clock.monotonic()
        board = []
        for _ in range(90):
            sim.run(1)
            a.watcher.run_once()
            m = machine_on_board(a, sim.mid)
            board.append((sim.clock.monotonic() - t0, m.code, m.colour, m.printing))
        late = [b for b in board if b[0] >= (hx_poller.PRIN_UNREAD_LIMIT + 1) * LOGIC.poll_fast_s]
        self.assertEqual([b for b in late if b[2] == "green"], [])
        self.assertIn(("printing_unchecked", "amber"), {(b[1], b[2]) for b in late})
        # P keeps the last known value (on); the board shows printing as unknown
        self.assertIs(sim.p().printing, True)
        self.assertTrue(sim.p().printing_unchecked)
        self.assertEqual({b[3] for b in late if b[1] == "printing_unchecked"}, {None})
        # A busy-only cycle moves no judged counter: only the cycles with a good Load do
        self.assertIn("not_monitored", {b[1] for b in late})
        self.assertEqual({b[2] for b in late if b[1] == "not_monitored"}, {"amber"})
        # E says stopped, and PRIN is read again: on. Red printing_on_unexpected, as for any reading
        sim.sender.write_expected(dataclasses.replace(sim.sender.read_expected(sim.mid), mode="stopped_by_user",
                                                      printing_expected=False, stopped_by=JSMITH,
                                                      counter=sim.sender.read_expected(sim.mid).counter + 1))
        self.busy(sim, False)
        with sim.fake.lock:
            sim.fake.printing = True
        self.assertTrue(sim.run(30, until=lambda: sim.code() == "printing_on_unexpected"))
        self.assertFalse(sim.p().printing_unchecked)

    def test_a_red_printing_on_stays_red_while_prin_is_busy(self):
        # Switched on at the panel after a Stop: red printing_on_unexpected. Then only PRIN answers busy:
        # the red state and its alert stay, the same id, while the printer still prints
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        e = sim.sender.read_expected(sim.mid)
        sim.sender.write_expected(dataclasses.replace(e, mode="stopped_by_user", printing_expected=False,
                                                      stopped_by=JSMITH, counter=e.counter + 1))
        with sim.fake.lock:
            sim.fake.printing = False
        self.assertTrue(sim.run(30, until=lambda: sim.code() == "stopped"))
        with sim.fake.lock:
            sim.fake.printing = True
        self.assertTrue(sim.run(30, until=lambda: sim.code() == "printing_on_unexpected"), sim.code())
        (first,) = [x for x in sim.p().alerts if x.kind == "printing_on" and x.active]
        sim.fake.override("PRIN", "7")
        seen = []
        for _ in range(40):
            sim.run(1)
            a.watcher.run_once()
            m = machine_on_board(a, sim.mid)
            seen.append((sim.p().state.code, m.colour,
                         tuple(x.id for x in sim.p().alerts if x.kind == "printing_on" and x.active)))
        self.assertTrue(sim.p().printing_unchecked)
        self.assertEqual(set(seen), {("printing_on_unexpected", "red", (first.id,))})

    def test_an_acked_ink_out_keeps_its_id_through_a_prin_busy_spell(self):
        sim = self.sim()
        sim.expect()
        sim.pc("akim")
        self.steady(sim)
        with sim.fake.lock:
            sim.fake.ink_level = 4                  # out of ink while printing
        self.assertTrue(sim.run(120, until=lambda: any(x.kind == "ink_out" and x.active for x in sim.p().alerts)))
        first = next(x for x in sim.p().alerts if x.kind == "ink_out" and x.active)
        sim.observer.write_ack(Ack(alert_id=first.id, by=JSMITH, computer="FLOOR01", at=sim.clock.time()))
        self.assertTrue(sim.run(30, until=lambda: any(x.id == first.id and x.acked_by for x in sim.p().alerts)))
        sim.fake.override("PRIN", "7")              # PRIN busy, everything else answers
        sim.run(30)
        self.assertTrue(sim.p().printing_unchecked)
        during = [(x.id, x.active, x.acked_by) for x in sim.p().alerts if x.kind == "ink_out"]
        sim.fake.override("PRIN", None)
        sim.run(30)
        self.assertFalse(sim.p().printing_unchecked)
        after = [(x.id, x.active, x.acked_by) for x in sim.p().alerts if x.kind == "ink_out"]
        self.assertEqual((during, after), ([(first.id, True, JSMITH)], [(first.id, True, JSMITH)]))

    def test_a_busy_cycle_or_two_keeps_the_colour(self):
        sim = self.sim()
        sim.expect()
        a = sim.pc("akim")
        self.steady(sim)
        self.busy(sim)
        codes = []
        sim.run((hx_poller.PRIN_UNREAD_LIMIT - 1) * LOGIC.poll_fast_s - 0.5,
                until=lambda: codes.append(sim.code()) or False)
        self.busy(sim, False)
        sim.run(20, until=lambda: codes.append(sim.code()) or False)
        self.assertEqual(set(codes), {"running_confirmed"})


# --------------------------------------------------------------------------
# The NOT MONITORED alarm (§7 item 7; owner decision, 31 Oct 2026)
# --------------------------------------------------------------------------

def nm_alarms(pc, machine_id):
    """The not_monitored alarms of a machine on the PC's latest board."""
    m = machine_on_board(pc, machine_id)
    return [a for a in m.alerts if a.kind == "not_monitored"] if m is not None else []


def ringing_on(pc):
    """What this PC rings for with its floor board open."""
    return hx_state.sounding_alerts(pc.watcher.latest, pc.identity, floor_board=True)


class NotMonitoredAlarmTests(Base):
    """Every PC's watcher rings for a machine that was printing when nobody has judged it for
    NOT_MONITORED_ALARM_S of NOT MONITORED. The watching PCs don't poll (FakeClock)."""

    def printing(self, sim, *watchers):
        """PC akim polls the printing machine (E running, confirmed) while the watchers look. Then akim's PC
        freezes for good (no request, no P write, no heartbeat), and nobody else can poll the printer."""
        sim.expect()
        a = sim.pc("akim")
        for _ in range(8):
            sim.run(5)
            for w in watchers:
                w.watcher.run_once()
        self.assertEqual(sim.code(), "running_confirmed")
        for w in watchers:
            self.assertEqual(machine_on_board(w, sim.mid).code, "running_confirmed")
        sim.pcs.remove(a)
        return a

    def passes(self, sim, seconds, *watchers, step=1):
        """A pass of every watcher each step seconds: per pass, (the code on the first watcher's board,
        every watcher's not_monitored alarm ids)."""
        out = []
        for _ in range(int(seconds // step)):
            sim.run(step)
            for w in watchers:
                w.watcher.run_once()
            out.append((machine_on_board(watchers[0], sim.mid).code,
                        [a.id for w in watchers for a in nm_alarms(w, sim.mid)]))
        return out

    def ring(self, sim, *watchers):
        """Passes each second until the watchers ring: NOT MONITORED for 119 s is quiet, 120 s rings on
        every watcher with one id, which it returns."""
        out = self.passes(sim, 60, *watchers)
        start = next(i for i, (code, _) in enumerate(out) if code == "not_monitored")
        out = out[start:] + self.passes(sim, 120 + start - len(out) + 1, *watchers)
        self.assertEqual([x for x in out[:120] if x[1]], [])
        self.assertEqual(len(out), 121)
        code, ids = out[120]
        self.assertEqual(code, "not_monitored")
        self.assertEqual(len(ids), len(watchers))
        self.assertEqual(len(set(ids)), 1)
        return ids[0]

    def quiet(self, rows):
        return [r for r in rows if r[1]]

    def test_two_minutes_after_not_monitored(self):
        sim = self.sim()
        carol, dave = PC(sim, "carol"), PC(sim, "dave")
        self.printing(sim, carol, dave)
        aid = self.ring(sim, carol, dave)
        p = sim.p()
        self.assertTrue(p.printing)
        self.assertEqual(aid, f"{sim.mid}.not_monitored.{p.judged}_{SID}.{int(p.judged_at * 1000)}")
        (a,) = nm_alarms(carol, sim.mid)
        self.assertEqual((a.sound, a.colour, a.recipients), (True, "", ["user:" + JSMITH, "floor"]))
        self.assertEqual(a.msg.english(), "No PC has checked Line 1 Bagger 1 for 2 min")
        self.assertEqual([x.id for x in ringing_on(carol)], [aid])
        self.assertEqual([x.id for x in ringing_on(dave)], [aid])
        jsmith = PC(sim, "jsmith")                      # the line's notify user and the last sender
        jsmith.watcher.run_once()
        self.assertEqual(hx_state.sounding_alerts(dave.watcher.latest, jsmith.identity, floor_board=False), [a])
        # Sound only: the machine stays grey NOT MONITORED, the floor row too, and P is unchanged
        m = machine_on_board(carol, sim.mid)
        self.assertEqual((m.code, m.colour, m.headline.text, m.unacked_sound, m.origin_line),
                         ("not_monitored", GREY, "NOT MONITORED", True, ORIGIN))
        row = next(r for r in hx_state.floor_board_rows(carol.watcher.latest) if r.machine_id == sim.mid)
        self.assertEqual((row.colour, row.status.text), (GREY, "NOT MONITORED"))
        self.assertEqual(sim.p(), p)
        self.assertEqual(sim.p().alerts, [])
        # It goes on, the same id, until something ends it
        self.assertEqual({tuple(ids) for _, ids in self.passes(sim, 300, carol, dave, step=5)}, {(aid, aid)})

    def test_a_pc_started_over_last_nights_p_never_rings(self):
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        sim.run(8 * 3600)                               # the night: nobody polls, and carol makes no pass
        erin = PC(sim, "erin")                          # the first PC opened in the morning
        self.assertTrue(sim.p().printing)
        self.assertEqual(sim.observer.read_expected(sim.mid).mode, "running")
        rows = self.passes(sim, 3600, erin, step=5)
        self.assertEqual(self.quiet(rows), [])
        self.assertEqual(rows[-1][0], "not_monitored")
        # Unlike a PC that watched it printing last night and stayed open: once its board has shown NOT
        # MONITORED for two minutes (its own long pause starts the 30 s again, §6.2 item 3.0), it rings
        rows = self.passes(sim, 160, carol, step=5)
        self.assertTrue(rows[-1][1])
        self.assertEqual(nm_alarms(erin, sim.mid), [])

    def test_not_while_this_pc_cant_read_the_share(self):
        sim = self.sim()
        carol = PC(sim, "carol", fs=sim.world.fs.view(down=False))
        self.printing(sim, carol)
        aid = self.ring(sim, carol)
        carol.store.fs.down = True                      # only carol: "Server unreachable" is its banner
        rows = self.passes(sim, 600, carol, step=5)
        self.assertEqual(self.quiet(rows), [])
        self.assertFalse(carol.watcher.latest.share_ok)
        self.assertEqual(ringing_on(carol), [])
        self.assertEqual(rows[-1][0], "not_monitored")
        # The share back: two more minutes first (pollers come back within that), then the same occurrence
        carol.store.fs.down = False
        carol.store.probe_now()                         # the store's probe runs on a real thread
        deadline = time.monotonic() + 5
        while not carol.store.available and time.monotonic() < deadline:
            time.sleep(0.01)
        rows = self.passes(sim, 125, carol)
        first = next(i for i, r in enumerate(rows) if r[1])
        self.assertEqual((first, rows[first][1]), (120, [aid]))

    def test_not_in_standalone_mode(self):
        sim = self.sim()
        carol = PC(sim, "carol", fs=sim.world.fs.view(down=False))
        self.printing(sim, carol)
        self.ring(sim, carol)
        carol.store.fs.down = True
        carol.poller.mode = "standalone"                # its own checks haven't reached this machine yet
        rows = self.passes(sim, 600, carol, step=5)
        self.assertEqual(self.quiet(rows), [])
        self.assertTrue(carol.watcher.latest.standalone)
        self.assertEqual(rows[-1][0], "not_monitored")
        self.assertEqual(ringing_on(carol), [])

    def test_only_while_e_is_running(self):
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        first = self.ring(sim, carol)
        e = sim.observer.read_expected(sim.mid)
        at = sim.clock.time()
        for stopped in (dataclasses.replace(e, mode="stopped_by_user", printing_expected=False, stop_reason="user",
                                            stopped_by=JSMITH, stopped_at=at),
                        dataclasses.replace(e, mode="test_finished", printing_expected=False,
                                            test={"run_id": RUN_ID, "kind": "test", "by": JSMITH, "step": 3,
                                                  "of": 3}),
                        dataclasses.replace(e, mode="none", printing_expected=False, send_id="", confirmed=False)):
            with self.subTest(mode=stopped.mode):
                sim.sender.write_expected(stopped)
                rows = self.passes(sim, 300, carol, step=5)
                self.assertEqual(self.quiet(rows), [])           # it ends at once and stays away
                self.assertEqual(rows[-1][0], "not_monitored")
        # E running again for a new Send, and nobody checks the printer: a new occurrence, a new id
        sim.sender.write_expected(dataclasses.replace(e, send_id=SID2))
        rows = self.passes(sim, 5, carol)
        (second,) = rows[0][1]
        self.assertNotEqual(second, first)
        self.assertEqual(second, first.replace(SID, SID2))

    def test_not_for_an_inactive_machine(self):
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        aid = self.ring(sim, carol)
        data = carol.data.current
        off = dataclasses.replace(data.machines[sim.mid], active=False)
        carol.data.current = dataclasses.replace(data, machines={**data.machines, sim.mid: off})
        carol.watcher.run_once()
        self.assertIsNone(machine_on_board(carol, sim.mid))
        self.assertEqual(ringing_on(carol), [])
        sim.run(600)
        carol.watcher.run_once()
        self.assertEqual(ringing_on(carol), [])
        self.assertNotIn(sim.mid, carol.watcher._judged_seen)          # its record is dropped
        # Active again: a fresh start. Nobody judged it meanwhile, and nobody acknowledged the alarm this
        # PC rang: the shared marker still holds that occurrence, so it rings again with its id, as on a
        # PC started later (states.md §7 item 7.6)
        carol.data.current = data
        rows = self.passes(sim, 300, carol, step=5)
        self.assertEqual({tuple(ids) for _, ids in self.quiet(rows)}, {(aid,)})
        self.assertEqual(rows[-1][0], "not_monitored")

    def test_not_for_an_inactive_machine_that_never_rang(self):
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        self.passes(sim, 60, carol, step=5)                            # NOT MONITORED, not yet ringing
        data = carol.data.current
        off = dataclasses.replace(data.machines[sim.mid], active=False)
        carol.data.current = dataclasses.replace(data, machines={**data.machines, sim.mid: off})
        carol.watcher.run_once()
        sim.run(600)
        # Active again: a fresh start, so with no marker it rings only after this PC has seen it judged again
        carol.data.current = data
        rows = self.passes(sim, 600, carol, step=5)
        self.assertEqual(self.quiet(rows), [])
        self.assertEqual(rows[-1][0], "not_monitored")
        self.assertIsNone(sim.observer.read_alarm_marker(sim.mid))

    def test_not_while_a_lock_shows(self):
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        aid = self.ring(sim, carol)
        lock = sim.observer.lock(sim.pid, purpose="send", run_id=SID2)
        self.assertEqual(lock.acquire(), "held")
        codes = set()
        for i in range(1, 301):                         # a Send holding the lock for 5 minutes
            sim.run(1)
            if i % 5 == 0:
                self.assertTrue(lock._keep_once())
            carol.watcher.run_once()
            codes.add(machine_on_board(carol, sim.mid).code)
            self.assertEqual(nm_alarms(carol, sim.mid), [])
        self.assertEqual(codes, {"send_in_progress"})
        lock.release()
        # No poller after it either: NOT MONITORED again, and two minutes later the same occurrence
        rows = self.passes(sim, 125, carol)
        start = next(i for i, r in enumerate(rows) if r[0] == "not_monitored")
        first = next(i for i, r in enumerate(rows) if r[1])
        self.assertEqual((first - start, rows[first][1]), (120, [aid]))

    def test_ends_when_judged_moves_and_a_later_one_has_a_new_id(self):
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        first = self.ring(sim, carol)
        judged = sim.p().judged
        bob = sim.pc("bob")                             # a PC polls the printer again
        rows = []
        for _ in range(90):
            sim.run(1)
            carol.watcher.run_once()
            rows.append((machine_on_board(carol, sim.mid).code, sim.p().judged, nm_alarms(carol, sim.mid)))
            if rows[-1][0] == "running_confirmed":
                break
        self.assertEqual(rows[-1][0], "running_confirmed")
        # It ended when judged moved. bob's start write ("Checking...") judges nothing, so it goes on
        # through that (states.md §7 item 7.5), and through nothing else
        self.assertEqual([r for r in rows if r[0] not in ("not_monitored", "checking") and r[2]], [])
        self.assertEqual([r for r in rows if r[1] != judged and r[2]], [])
        self.assertTrue([r for r in rows if r[0] == "checking" and r[2]])
        sim.pcs.remove(bob)                             # bob's PC freezes too
        second = self.ring(sim, carol)
        self.assertNotEqual(second, first)
        p = sim.p()
        self.assertGreater(p.judged, judged)
        self.assertEqual(second, hx_state.not_monitored_alarm_id(sim.mid, p.judged, p.judged_at, SID))

    def test_an_ack_on_one_pc_silences_every_pc(self):
        sim = self.sim()
        carol, dave = PC(sim, "carol"), PC(sim, "dave")
        self.printing(sim, carol, dave)
        aid = self.ring(sim, carol, dave)
        carol.watcher.acknowledge([aid])
        self.assertEqual(ringing_on(carol), [])         # at once
        carol.watcher.run_once()                        # writes the ack file
        ack = sim.observer.read_ack(aid)
        self.assertEqual(ack.by, carol.identity.user)
        dave.watcher.run_once()                         # reads it within one watch_s
        self.assertEqual(ringing_on(dave), [])
        for pc in (carol, dave):
            m = machine_on_board(pc, sim.mid)
            self.assertEqual((m.code, m.colour, m.unacked_sound), ("not_monitored", GREY, False))
            self.assertEqual([(a.id, a.acked_by) for a in nm_alarms(pc, sim.mid)], [(aid, carol.identity.user)])
        # It stays quiet: the same occurrence keeps its id
        rows = self.passes(sim, 600, carol, dave, step=5)
        self.assertEqual({tuple(ids) for _, ids in rows}, {(aid, aid)})
        self.assertEqual(ringing_on(carol) + ringing_on(dave), [])

    # Final review, 31 Oct: A-1 (the shared marker), A-2, A-3, A-4

    def marker_calls(self, pc):
        """Every read_alarm_marker and write_alarm_marker of the PC's store: [(kind, machine id, pass)]."""
        calls, store = [], pc.store
        pc.passes_made = 0
        original = pc.watcher.run_once

        def run_once():
            pc.passes_made += 1
            original()
        pc.watcher.run_once = run_once
        read, write = store.read_alarm_marker, store.write_alarm_marker

        def reading(mid):
            calls.append(("read", mid, pc.passes_made))
            return read(mid)

        def writing(marker):
            calls.append(("write", marker.machine_id, pc.passes_made))
            return write(marker)
        store.read_alarm_marker, store.write_alarm_marker = reading, writing
        return calls

    def test_a_restarted_pc_rings_again(self):
        # A-1: the floor-board PC rang, and someone restarted Stripes there (a new process: no record of the
        # machine moving). It rings again for the same occurrence, through the shared marker
        sim = self.sim()
        floor = PC(sim, "floor")
        self.printing(sim, floor)
        aid = self.ring(sim, floor)
        marker = sim.observer.read_alarm_marker(sim.mid)
        (alarm,) = nm_alarms(floor, sim.mid)
        self.assertEqual((marker.machine_id, marker.alarm_id, marker.started_at, marker.by, marker.computer),
                         (sim.mid, aid, alarm.started_at, floor.identity.user, floor.identity.computer))
        floor2 = PC(sim, "floor", pid=2)
        rows = self.passes(sim, 200, floor2, step=5)
        ringing = [i for i, (code, ids) in enumerate(rows) if ids]
        self.assertTrue(ringing, rows)
        self.assertEqual({tuple(rows[i][1]) for i in ringing}, {(aid,)})
        self.assertEqual(ringing, list(range(ringing[0], len(rows))))            # and it goes on
        # 30 s of "Checking..." (a P it hasn't seen move, §7 item 6), then 120 s of NOT MONITORED
        self.assertEqual(rows[ringing[0] - 1][0], "not_monitored")
        self.assertGreaterEqual(5 * (ringing[0] + 1), 150)
        self.assertEqual([x.id for x in ringing_on(floor2)], [aid])
        self.assertFalse(floor2.watcher._judged_seen[sim.mid].moved)
        self.assertEqual(sim.observer.read_alarm_marker(sim.mid), marker)       # read, never rewritten

    def test_a_pc_started_later_rings_too_and_an_ack_silences_it(self):
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        aid = self.ring(sim, carol)
        erin = PC(sim, "erin")                          # opened after the machine stopped being checked
        calls = self.marker_calls(erin)
        rows = self.passes(sim, 200, erin, step=5)
        self.assertEqual({tuple(ids) for _, ids in self.quiet(rows)}, {(aid,)})
        self.assertEqual([x.id for x in ringing_on(erin)], [aid])
        # erin only read the marker, once per pass and only while it was NOT MONITORED, until it found it
        self.assertEqual({c[0] for c in calls}, {"read"})
        passes = [c[2] for c in calls]
        self.assertEqual(len(passes), len(set(passes)))
        self.assertEqual(passes[-1], next(i for i, r in enumerate(rows, 1) if r[1]))
        self.assertTrue(all(rows[i - 1][0] == "not_monitored" for i in passes))
        # One ack silences both PCs, as for any alarm
        carol.watcher.acknowledge([aid])
        carol.watcher.run_once()
        erin.watcher.run_once()
        self.assertEqual(ringing_on(carol) + ringing_on(erin), [])
        self.assertEqual([(a.id, a.acked_by) for a in nm_alarms(erin, sim.mid)], [(aid, carol.identity.user)])

    def test_the_marker_counts_only_for_its_own_occurrence(self):
        # A-1: the marker holds the last occurrence that rang. A PC started after a newer judgement never
        # rings for it (the morning rule); once a PC that saw the new judgement rings, the marker is
        # overwritten with the new id and the late PC rings too
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        first = self.ring(sim, carol)
        bob = sim.pc("bob")                             # a PC polls the printer again, then freezes
        for _ in range(60):
            sim.run(1)
            carol.watcher.run_once()
            if machine_on_board(carol, sim.mid).code == "running_confirmed":
                break
        sim.run(10)
        carol.watcher.run_once()
        sim.pcs.remove(bob)
        erin = PC(sim, "erin")                          # opened after bob's judgements
        calls = self.marker_calls(carol)
        rows = []
        for _ in range(40):
            sim.run(5)
            erin.watcher.run_once()
            carol.watcher.run_once()
            rows.append((nm_alarms(erin, sim.mid), nm_alarms(carol, sim.mid)))
            if nm_alarms(carol, sim.mid):
                break
        (second,) = [a.id for a in rows[-1][1]]
        self.assertNotEqual(second, first)
        self.assertEqual([r for r in rows if r[0]], [])        # erin: quiet while the marker held the old id
        self.assertEqual(sim.observer.read_alarm_marker(sim.mid).alarm_id, second)   # overwritten
        self.assertEqual([c[0] for c in calls], ["read", "write"])   # carol: one check, one write
        later = self.passes(sim, 60, erin, step=5)
        self.assertTrue(self.quiet(later))
        self.assertEqual({tuple(ids) for _, ids in self.quiet(later)}, {(second,)})

    def test_the_marker_is_read_only_for_a_machine_not_monitored(self):
        sim = self.sim()
        carol = PC(sim, "carol")
        calls = self.marker_calls(carol)
        sim.expect()
        a = sim.pc("akim")
        for _ in range(60):                             # monitored: never
            sim.run(5)
            carol.watcher.run_once()
        self.assertEqual(machine_on_board(carol, sim.mid).code, "running_confirmed")
        self.assertEqual(calls, [])
        sim.pcs.remove(a)
        aid = self.ring(sim, carol)
        # The PC that rang read it once and wrote it once, at the pass it started ringing; no more after
        self.assertEqual([c[0] for c in calls], ["read", "write"])
        self.assertEqual(calls[0][2], calls[1][2])
        self.passes(sim, 300, carol, step=5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sim.observer.read_alarm_marker(sim.mid).alarm_id, aid)

    def test_a_resume_the_poller_never_judged_rings(self):
        # A-2: the machine was stopped at its last judgement. A Resume makes E running and the printer prints,
        # and the poller dies before judging it: nobody checks a printing machine
        sim = self.sim()
        install(sim.fake, printing=False)
        sim.expect(mode="stopped_by_user", printing_expected=False, stop_reason="user", stopped_by=JSMITH,
                   stopped_at=sim.clock.time(), confirmed=True, confirmed_by=JSMITH)
        a = sim.pc("akim")
        carol = PC(sim, "carol")
        for _ in range(12):
            sim.run(5)
            carol.watcher.run_once()
        self.assertEqual((sim.code(), sim.p().printing), ("stopped", False))
        sim.pcs.remove(a)
        e = sim.observer.read_expected(sim.mid)
        sim.sender.write_expected(dataclasses.replace(e, mode="running", printing_expected=True, stop_reason="",
                                                      stopped_by="", stopped_at=None))
        with sim.fake.lock:
            sim.fake.printing = True
        rows = self.passes(sim, 200, carol, step=5)
        p = sim.p()
        self.assertEqual({tuple(ids) for _, ids in self.quiet(rows)},
                         {(hx_state.not_monitored_alarm_id(sim.mid, p.judged, p.judged_at, SID),)})
        start = next(i for i, r in enumerate(rows) if r[0] == "not_monitored")
        self.assertEqual(next(i for i, r in enumerate(rows) if r[1]) - start, 24)    # 120 s of 5 s passes

    def test_a_start_write_that_judges_nothing_doesnt_end_it(self):
        # A-3: after the alarm rang, a PC writes its start ("Checking...") and freezes before its first
        # judgement. The alarm goes on, the same occurrence, with no 120 s restart
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        aid = self.ring(sim, carol)
        judged = sim.p().judged
        bob = sim.pc("bob")
        rows = []
        for _ in range(120):
            sim.run(0.5)
            carol.watcher.run_once()
            rows.append((machine_on_board(carol, sim.mid).code, [x.id for x in ringing_on(carol)]))
            if sim.code() == "checking":
                break
        self.assertEqual(sim.code(), "checking")
        sim.pcs.remove(bob)                             # bob's PC freezes right after its start write
        rows += self.passes(sim, 200, carol)
        self.assertEqual(sim.p().judged, judged)
        self.assertIn("checking", {code for code, _ in rows})
        self.assertEqual({tuple(ids) for _, ids in rows}, {(aid,)})     # rings on every pass

    def test_a_printer_not_set_up_drops_the_record(self):
        # A-4: the PRINTER made inactive ("No printer set up") before the alarm rang. An hour later it is
        # active again and nobody polls: a fresh start, so no alarm with the old id
        sim = self.sim()
        carol = PC(sim, "carol")
        self.printing(sim, carol)
        data = carol.data.current
        m = data.machines[sim.mid]
        off = dataclasses.replace(data.printers[m.printer_id], active=False)
        carol.data.current = dataclasses.replace(data, printers={**data.printers, m.printer_id: off})
        rows = self.passes(sim, 3600, carol, step=10)
        self.assertEqual({code for code, _ in rows}, {"not_set_up"})
        self.assertNotIn(sim.mid, carol.watcher._judged_seen)
        carol.data.current = data
        rows = self.passes(sim, 600, carol, step=5)
        self.assertEqual(self.quiet(rows), [])
        self.assertEqual(rows[-1][0], "not_monitored")


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
        self.assertEqual(hx_poller.NOT_MONITORED_ALARM_S, 120.0)

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
