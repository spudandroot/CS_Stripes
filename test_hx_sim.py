"""Simulated plant tests: several PCs (each with its own identity, store, client pool and gates) on one
share with several fake printers, mixed-up replies on, all running their real threads with scaled
timings. PCs drop out and come back, printers are switched off, the share is slow, one PC loses the
share alone. Asserts: every fake sees one request at a time (max_in_flight == 1) and no violation,
poll.json has one writer at a time, takeovers happen within the takeover times, NOT MONITORED when every
poller is gone, alert ids survive a takeover, and an acknowledgement silences every PC within one watch_s.

With Sends (hx_send): two PCs sending to one line at once, both from PCs that don't poll and from the
two PCs that poll the line (one takes the locks, the other sends nothing); the polling PC dropping out in
the middle of a Send, and just as a Send waits for it to step aside; a sender on a slow share sending
twice; and a sender whose Sends overlap the standalone checks of a PC cut off from the share (every
read-back matched its own TN and XID, and no Send turns green with a content the printer doesn't hold).
Every request of every PC is noted, so each Send also checks that no other PC talked to its printer
from its first request to its last. Run: python -m unittest -v"""

import collections
import dataclasses
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import hx_fake
import hx_message
import hx_poller
import hx_protocol
import hx_send
import hx_share
import hx_state
import hx_time
from hx_core import SYSTEM_CLOCK, EventBus
from hx_model import (AMBER, GREEN, GREY, RED, STEP_BUILD, STEP_CHECK_MACHINE, STEP_LOCK, STEP_WAIT_POLLER, STEPS,
                      TIMINGS, DesignItem, ExpectedState, LineChoice, MessageDesign, SendEvent, SendRequest, new_id)
from hx_share import settle_s

# Every wait is scaled by F, as a whole, so the spec's ratios between them hold. A Windows runner gets twice
# the time: its clock ticks every 15.6 ms, and its file writes (NTFS, the virus scanner) are slower
F = 0.05 if sys.platform == "win32" else 0.025
# The test's own absolute waits and slacks were set for F = 0.025, and scale with F too
S = F / 0.025
# A printer request keeps http_timeout_s + 2 x read_retry_s within stale_s / 4, as the real timings do
# (2 + 1 s of 3.75 s): the poller's budget for a read after a keep-fresh write (states.md §6.4). A Send
# sets the printer's clock to the minute (no seconds field), so the clock warning keeps its real 120 s
SCALED = dataclasses.replace(TIMINGS.scaled(F), http_timeout_s=2.7 * F, clock_warn_s=TIMINGS.clock_warn_s)
EDT = lambda utc: -14400.0
ORIGIN = "Product of USA and Canada"
JSMITH = "EXAMPLE\\jsmith"


def design(slot, name, origin=ORIGIN):
    items = [DesignItem(tag="CTEXT", pairs=[("OBJID", "10001"), ("POS", "0,0"), ("TXT", origin), ("FONTIND", "2"),
                                            ("SIZE", "3"), ("MARG", "0,0")])]
    settings = {"ROTAT": "0", "MIRR": "0", "REVRS": "0", "AUTREP": "0", "DELAY": "480000", "DLYMM": "0",
                "PRNSPD": "2400", "BOLD": "1", "WDTH": "1", "QUANT": "1", "QDELAY": "0", "QDLYMM": "0",
                "RESMOD": "0", "RESNAM": "UltraFine", "ENCST": "0", "ENCSCL": "100"}
    return MessageDesign(slot=slot, name=name, empty=False, height=160, items=items, settings=settings)


# One P write of a simulated PC: start and end (time.monotonic(); end None if the write failed), and what it
# wrote
PWrite = collections.namedtuple("PWrite", "t mid key yielded_to counter judged state end")


def wait(until, timeout_s):
    """Real time, in short sleeps: True as soon as until() holds."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(0.02)
    return until()


class _Sending:
    """send() and tn() of a simulated PC with a SendPipeline (plant, identity, data, clients, pipeline)."""

    def send(self, ns, *, lot="4417", on_event=None):
        """One Send to the plant's machines ns (indexes), on the calling thread."""
        w = self.plant.world
        choice = LineChoice(line_id=w.ids["line"], machine_ids=[self.plant.mids[n] for n in ns],
                            product_id=w.ids["product"], film_id=w.ids["film"], print_text_id=w.ids["print_text"],
                            lot=lot, countries=["USA", "Canada"])
        request = SendRequest(send_id=new_id("sd"), sender=self.identity.user, computer=self.identity.computer,
                              created_at=time.time(), lines=[choice])
        plan = hx_message.merge_plan(request, self.data.current, self.identity)
        return self.pipeline.run(plan, on_event or (lambda event: None))

    def tn(self, n):
        """The TN of this PC's client for the plant's printer n."""
        return self.clients.client(self.data.current.printers[self.plant.pids[n]]).tn


class SimPC(_Sending):
    """One simulated PC with its own threads: poller, time thread and status watcher. It can send too,
    through the same store, client pool and gates as its poller (as the app does)."""

    def __init__(self, plant, name, *, fs=None, time_source=None):
        world, timings = plant.world, plant.timings
        self.plant = plant
        self.name = name
        self.identity = world.identity(name)
        self.key = self.identity.key
        self.store = hx_share.ShareStore(world.share_root, identity=self.identity, clock=SYSTEM_CLOCK,
                                         fs=fs if fs is not None else world.fs.view(), timings=timings, version="test")
        self.local = hx_share.LocalStore(world.paths(self.identity))
        self.bus = EventBus()
        self.data = hx_share.DataService(store=self.store, local=self.local, bus=self.bus, identity=self.identity,
                                         timings=timings)
        # Tried again if one slow share operation failed it (hx_fake.first_load)
        plant.test.assertIsNotNone(hx_fake.first_load(self.data), "the PC's first data load kept failing")
        plant.names[self.key] = name
        self.logs = []
        self._log_lock = threading.Lock()
        original = self.data.append_log

        def append_log(kind, entry):
            with self._log_lock:
                self.logs.append(dict(entry))
            return original(kind, entry)
        self.data.append_log = append_log
        real_write = self.store.write_poll

        def write_poll(status):
            t = time.monotonic()
            try:
                real_write(status)
            except BaseException:
                plant.note_write(t, status, self.key, None)     # failed: it may still have landed
                raise
            plant.note_write(t, status, self.key, time.monotonic())
        self.store.write_poll = write_poll
        self.clients = hx_protocol.ClientPool(key=hx_protocol.release_key(), gates=hx_protocol.GateRegistry(),
                                              timings=timings)
        plant.watch_requests(name, self.clients)
        self.time = time_source or hx_time.TimeSource(["time.example.local"], offset_fn=EDT,
                                                      query=lambda host, port=123, timeout_s=2.0: time.time())
        self.poller = hx_poller.Poller(store=self.store, data=self.data, clients=self.clients, time=self.time,
                                       bus=self.bus, identity=self.identity, version="test", timings=timings)
        self.watcher = hx_poller.StatusWatcher(store=self.store, data=self.data, poller=self.poller, bus=self.bus,
                                               identity=self.identity, timings=timings)
        self.pipeline = hx_send.SendPipeline(store=self.store, data=self.data, clients=self.clients, time=self.time,
                                             identity=self.identity, timings=timings)
        self.running = False

    def start(self):
        self.poller.start()
        self.watcher.start()
        self.running = True

    def stop(self):
        if self.running:
            self.running = False
            self.watcher.stop()
            self.poller.stop()

    def entries(self, kind):
        with self._log_lock:
            return [e for e in self.logs if e["type"] == kind]


class SimSender(_Sending):
    """A PC that only sends (no poller): its own identity, store, data, client pool with its own gates,
    time source and SendPipeline."""

    def __init__(self, plant, name, *, time_source=None):
        world, timings = plant.world, plant.timings
        self.plant = plant
        self.name = name
        self.identity = world.identity(name)
        self.key = self.identity.key
        self.store = hx_share.ShareStore(world.share_root, identity=self.identity, clock=SYSTEM_CLOCK,
                                         fs=world.fs.view(), timings=timings, version="test")
        self.local = hx_share.LocalStore(world.paths(self.identity))
        self.data = hx_share.DataService(store=self.store, local=self.local, bus=EventBus(), identity=self.identity,
                                         timings=timings)
        # Tried again if one slow share operation failed it (hx_fake.first_load)
        plant.test.assertIsNotNone(hx_fake.first_load(self.data), "the PC's first data load kept failing")
        plant.names[self.key] = name
        self.clients = hx_protocol.ClientPool(key=hx_protocol.release_key(), gates=hx_protocol.GateRegistry(),
                                              timings=timings)
        plant.watch_requests(name, self.clients)
        # The fake time server, in the plant's time zone (the pollers' and the printers')
        self.time = time_source or hx_time.TimeSource([f"127.0.0.1:{world.sntp.port}"], offset_fn=EDT)
        self.time.timeout_s = 4 * F
        self.pipeline = hx_send.SendPipeline(store=self.store, data=self.data, clients=self.clients, time=self.time,
                                             identity=self.identity, timings=timings)


def held_fingerprint(fake, slot):
    """The fingerprint of what the fake holds in slot now, rendered as a Load would read it (no request)."""
    with fake.lock:
        body = fake._result("Load", "1", "1", slot)
    return hx_message.fingerprint(hx_protocol.parse_sme_reply(body).design)


def load_requests(fake):
    """The bodies of every web Load the fake received."""
    with fake.lock:
        return {r["body"] for r in fake.requests if r["kind"] == "sme"}


class Plant:
    def __init__(self, test, *, printers=3, timings=SCALED, printer_kw=None):
        self.test = test
        self.timings = timings
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        kw = {"offset_fn": EDT, "mixup": True, **(printer_kw or {})}
        self.world = hx_fake.FakeWorld(tmp.name, printers=[dict(kw, seed=n) for n in range(printers)],
                                       timings=timings)
        test.addCleanup(self.world.close)
        self.fakes = self.world.printers
        self.mids = [self.world.ids[f"machine{n + 1}"] for n in range(printers)]
        self.pids = [self.world.ids[f"printer{n + 1}"] for n in range(printers)]
        # The test's own eye on the share: never slowed or cut off with the simulated PCs
        self.observer = hx_share.ShareStore(self.world.share_root,
                                            identity=self.world.identity("observer", sender=False),
                                            clock=SYSTEM_CLOCK, fs=self.world.fs.view(down=False, slow_s=0.0),
                                            timings=dataclasses.replace(timings, share_timeout_s=2.0), version="test")
        # The test's set-up writes (each machine's expected state) aren't under test: the real share timeout
        self.sender = self.world.setup_store(self.world.identity("jsmith"))
        self.pcs = []
        self.names = {}             # identity key -> simulated PC name
        self.write_log = []         # a PWrite for each P write
        self.exchanges = []         # (printer port, PC name, thread name, start, end) of every request
        self._lock = threading.Lock()
        for n, fake in enumerate(self.fakes):
            with fake.lock:
                fake.slots[9] = design(9, f"EXCO1-PKO07-BBB{n}-M9")
                fake.slot_style[9] = "verbose"
                fake.current_slot = 9
            self.expect(n)

    def expect(self, n):
        fake = self.fakes[n]
        client = hx_protocol.PrinterClient("127.0.0.1", fake.uid, hx_protocol.make_token("123321"), port=fake.port,
                                           gates=hx_protocol.GateRegistry())
        rb = client.load_slot(9)
        fake.reset_observations()
        e = ExpectedState(machine_id=self.mids[n], mode="running", printing_expected=True, send_id=f"sd_{n:010x}",
                          message_name=rb.name, slot=9, fingerprint=hx_message.fingerprint(rb),
                          origin_line=hx_message.origin_from_design(rb), confirmed=True, confirmed_by=JSMITH,
                          sender=JSMITH)
        self.sender.write_expected(e)

    def pc(self, name, **kw):
        pc = SimPC(self, name, **kw)
        self.pcs.append(pc)
        self.test.addCleanup(pc.stop)
        return pc

    def sender_pc(self, name, **kw):
        return SimSender(self, name, **kw)

    def pollers(self, n, pcs):
        """(the PC polling printer n now, the other one) of two PCs."""
        first = next(pc for pc in pcs if pc.key == self.p(n).poller)
        return first, next(pc for pc in pcs if pc is not first)

    # ---- every request of every simulated PC

    def watch_requests(self, name, clients):
        """Notes each exchange of this PC's printer clients (also a login) in self.exchanges."""
        make = clients.client

        def client(printer):
            c = make(printer)
            with self._lock:
                if not getattr(c, "_sim_watched", False):
                    c._sim_watched = True
                    c._exchange = self._noted(name, c.port, c._exchange)
                    c._login_locked = self._noted(name, c.port, c._login_locked)
            return c
        clients.client = client

    def _noted(self, name, port, call):
        def noted(*args, **kw):
            t0 = time.monotonic()
            try:
                return call(*args, **kw)
            finally:
                with self._lock:
                    self.exchanges.append((port, name, threading.current_thread().name, t0, time.monotonic()))
        return noted

    def requests_of(self, n, name, *, since=0.0, sends_only=False):
        """(start, end) of each exchange PC name made with printer n since a moment."""
        port = self.fakes[n].port
        with self._lock:
            return [(t0, t1) for p, who, thread, t0, t1 in self.exchanges
                    if p == port and who == name and t0 >= since and (thread.startswith("hx-send-") or not sends_only)]

    def check_sends_alone(self, n, *, ignore=()):
        """From each Send's first request to printer n to its last, no other PC (except those in ignore)
        talked to it. Returns {(PC name, Send thread): (first start, last end)}."""
        port = self.fakes[n].port
        with self._lock:
            ex = [e for e in self.exchanges if e[0] == port]
        sends = {}
        for _, who, thread, t0, t1 in ex:
            if thread.startswith("hx-send-"):
                a, b = sends.get((who, thread), (t0, t1))
                sends[(who, thread)] = (min(a, t0), max(b, t1))
        for (who, thread), (a, b) in sends.items():
            # This PC's own poller is kept off by local_locks and the shared gate, not by the handshake
            others = [(e[1], e[2], round(e[3] - a, 3)) for e in ex
                      if e[1] != who and e[1] not in ignore and e[3] < b and e[4] > a]
            self.test.assertEqual(others, [], (who, thread))
        return sends

    def check_own_read_backs(self, sender, result, n):
        """Every read-back of an ok machine matched a Load this sender's client sent (its TN and XID), and
        shows what the fake holds in that slot now."""
        fake = self.fakes[n]
        r = next(m for m in result.machines if m.machine_id == self.mids[n])
        if not r.ok:
            return None
        sent = load_requests(fake)
        held = held_fingerprint(fake, r.slot)
        self.test.assertEqual(len(r.readbacks), 3)
        for i, design in enumerate(r.readbacks):
            self.test.assertEqual(design.tn, sender.tn(n))
            self.test.assertIn(hx_protocol.load_body(design.tn, design.xid, r.slot if i == 0 else 0), sent)
            self.test.assertEqual(hx_message.fingerprint(design), held)
        self.test.assertEqual(r.fingerprint, held)
        return r

    def note_write(self, t, status, key, end):
        state = status.state.code if status.state is not None else ""
        with self._lock:
            self.write_log.append(PWrite(t, status.machine_id, key, status.yielded_to, status.counter, status.judged,
                                         state, end))

    def p(self, n):
        return self.observer.read_poll(self.mids[n])

    def code(self, n):
        p = self.p(n)
        return p.state.code if p is not None and p.state is not None else ""

    def check_one_request_at_a_time(self):
        for fake in self.fakes:
            with fake.lock:
                self.test.assertEqual(fake.violations, [])
                self.test.assertEqual(fake.max_in_flight, 1)

    def check_one_writer(self):
        """poll.json changes writer only after a yield, after the old writer went quiet for about stale_s,
        while a starting poller settles and yields at its rank check before any request, or when the old
        writer never talked to the printer while it wrote P: a start that settled and stopped before its
        first request (states.md §6.3 step 2, a failed share step §6.13). A writer that has polled must
        hand over or go quiet."""
        t = self.timings
        with self._lock:
            log = sorted(self.write_log)
            exchanges = list(self.exchanges)
        for n, mid in enumerate(self.mids):
            port = self.fakes[n].port
            writes = [w for w in log if w.mid == mid and w.end is not None]
            writer, since, last, yielded = None, None, None, ""
            for time_, _, key, yielded_to, *_ in writes:
                if writer is not None and key != writer:
                    handed = yielded == key
                    quiet = time_ - last >= 0.8 * t.stale_s
                    # A start race (states.md §6.3 step 2): one of the two yields at its rank check, before any
                    # request, to the other or, when three PCs start together, to a third
                    racing = any(w.key in (key, writer) and w.yielded_to and w.yielded_to != w.key
                                 and time_ - t.stale_s <= w.t <= time_ + t.stale_s for w in writes)
                    # The old writer only settled: no request of its own to this printer since it began writing
                    name = self.names.get(writer)
                    settling = name is not None and not any(
                        p == port and who == name and since <= t0 <= time_ for p, who, _, t0, _ in exchanges)
                    # For the message: the old writer's failed writes just before, which may still have landed
                    failed = [(round(w.t - time_, 3), w.yielded_to) for w in log
                              if w.mid == mid and w.key == writer and w.end is None and last <= w.t <= time_]
                    self.test.assertTrue(handed or quiet or racing or settling,
                                         (mid, writer, key, round(time_ - last, 3), round(time_ - since, 3), failed))
                if key != writer:
                    writer, since = key, time_
                last, yielded = time_, yielded_to


class PlantTests(unittest.TestCase):
    def test_pcs_drop_out_and_return(self):
        plant = Plant(self)
        pcs = [plant.pc(name, fs=plant.world.fs.view(down=False)) for name in ("akim", "bob", "carol")]
        for pc in pcs:
            pc.start()
        t = plant.timings
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in range(3)), 8 * S))
        # A warning on machine 2, so its alert id can be followed through a takeover
        plant.fakes[1].ink_level = 2
        self.assertTrue(wait(lambda: [a.kind for a in plant.p(1).alerts] == ["low_ink"], 6 * S))
        alert_id = plant.p(1).alerts[0].id
        # The PC polling machine 2 loses the share: another PC takes over
        dropped = next(pc for pc in pcs if pc.key == plant.p(1).poller)
        dropped.store.fs.down = True
        t_cut = time.monotonic()
        self.assertTrue(wait(lambda: plant.p(1) is not None and plant.p(1).poller != dropped.key, 6 * S))
        took = time.monotonic() - t_cut
        self.assertLess(took, t.takeover_s[1] + 3 * t.heartbeat_s + 1.0 * S)
        self.assertGreaterEqual(took, t.takeover_s[0] * 0.8)
        self.assertTrue(wait(lambda: [a.id for a in plant.p(1).alerts] == [alert_id], 4 * S))
        # A printer is switched off, and comes back
        plant.fakes[2].go_silent()
        self.assertTrue(wait(lambda: plant.code(2) == "off", 6 * S))
        plant.fakes[2].go_silent(False)
        self.assertTrue(wait(lambda: plant.code(2) == "running_confirmed", 8 * S))
        # The dropped PC returns
        dropped.store.fs.down = False
        self.assertTrue(wait(lambda: dropped.poller.mode == "share" and dropped.poller.polling, 8 * S))
        time.sleep(4 * t.poll_fast_s)
        self.assertEqual([a.id for a in plant.p(1).alerts], [alert_id])
        started = [e for pc in pcs for e in pc.entries("alert_started") if e["alert"]["id"] == alert_id]
        self.assertEqual(len(started), 1)
        for pc in pcs:
            pc.stop()
        plant.check_one_request_at_a_time()
        plant.check_one_writer()

    def test_not_monitored_when_every_poller_is_gone(self):
        plant = Plant(self, printers=2)
        a, b = plant.pc("akim"), plant.pc("bob")
        a.start()
        b.start()
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in range(2)), 8 * S))
        watcher = b.watcher
        onset = {}

        def codes():
            board = watcher.latest
            now = time.monotonic()
            machines = [m for line in board.lines for m in line.machines] if board else []
            for m in machines:
                if m.code == "not_monitored":
                    onset.setdefault(m.machine_id, now)
            return [m.code for m in machines]
        self.assertTrue(wait(lambda: codes() == ["running_confirmed"] * 2, 4 * S))
        onset.clear()
        a.poller.stop()
        b.poller.stop()
        self.assertTrue(wait(lambda: codes() == ["not_monitored"] * 2, 4 * S))
        # NOT MONITORED once nobody has judged the machine for not_monitored_s (states.md §5.1, §7 item 2),
        # so never sooner than that after the P write that last moved its judged counter. A stop's yield
        # and a start's writes move only the counter, and stopping takes a while on a slow runner, so the
        # time the stops returned isn't where it starts
        for n, mid in enumerate(plant.mids):
            judged = plant.p(n).judged
            with plant._lock:
                judged_at = min(w.t for w in plant.write_log if w.mid == mid and w.judged == judged)
            self.assertGreaterEqual(onset[mid] - judged_at, plant.timings.not_monitored_s * 0.9, mid)
        rows = hx_state.floor_board_rows(watcher.latest)
        self.assertEqual([r.status.text for r in rows], ["NOT MONITORED"] * 2)
        a.stop()
        b.stop()
        plant.check_one_request_at_a_time()

    def test_an_ack_silences_every_pc(self):
        plant = Plant(self, printers=1)
        pcs = [plant.pc(name) for name in ("akim", "bob", "carol")]
        for pc in pcs:
            pc.start()
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8 * S))
        with plant.fakes[0].lock:
            plant.fakes[0].slots[9] = design(9, plant.fakes[0].slots[9].name, "Product of Mexico")
        ringing = lambda pc: hx_state.sounding_alerts(pc.watcher.latest, pc.identity, floor_board=True)
        self.assertTrue(wait(lambda: all(pc.watcher.latest and ringing(pc) for pc in pcs), 10 * S))
        ids = [a.id for a in ringing(pcs[0])]
        pcs[0].watcher.acknowledge(ids)
        self.assertEqual(ringing(pcs[0]), [])
        t0 = time.monotonic()
        self.assertTrue(wait(lambda: not any(ringing(pc) for pc in pcs), 3 * S))
        self.assertLess(time.monotonic() - t0, 2 * plant.timings.watch_s + 0.5 * S)
        self.assertIsNotNone(plant.observer.read_ack(ids[0]))
        self.assertTrue(wait(lambda: plant.p(0).alerts and plant.p(0).alerts[0].acked_by == pcs[0].identity.user,
                             3 * S))
        self.assertEqual(plant.code(0), "drift")
        for pc in pcs:
            pc.stop()
        plant.check_one_request_at_a_time()
        plant.check_one_writer()

    # ---- Sends

    def send_at_once(self, plant, senders):
        """Both senders send to the whole line (machines 1 and 2) at the same moment. One is refused at its
        lock step and sends nothing; the other's pollers stepped aside before its first request. Returns
        the winner's index, its SendResult and the machines (indexes) that went through."""
        results = [None, None]
        seen = {}
        go = threading.Barrier(2)

        def watch(i):
            def on_event(event):
                # Right before the machine's first request: who polls it, and for which lock it paused
                if isinstance(event, SendEvent) and event.step == STEP_CHECK_MACHINE and event.status == "running":
                    n = plant.mids.index(event.machine_id)
                    lock, p = plant.observer.read_lock(plant.pids[n]), plant.p(n)
                    seen[(i, n)] = (p.poller, p.paused_for_lock, lock.token if lock is not None else "")
            return on_event

        def send(i):
            go.wait()
            results[i] = senders[i].send((0, 1), on_event=watch(i))
        threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(20 * S)
        self.assertTrue(all(r is not None for r in results))
        refused = [i for i, r in enumerate(results) if all(m.step == STEP_LOCK for m in r.machines)]
        self.assertEqual(len(refused), 1, [[(m.colour, m.step, m.msg) for m in r.machines] for r in results])
        w, lost = 1 - refused[0], refused[0]
        winner, loser = results[w], results[lost]
        # The loser is refused at its lock step, "Line 1 is being sent by ...", and sends nothing
        self.assertEqual([(m.colour, m.ok) for m in loser.machines], [(RED, False)] * 2)
        # The holder is the winner, or not known: the loser may read the lock file in the moment between
        # the winner's exclusive create and its write, and a damaged lock has user "" (storage.md §11.7)
        self.assertIn(loser.machines[0].msg.values.get("user"), (senders[w].identity.user, "", "?"))
        self.assertNotEqual(loser.machines[0].msg.values.get("user"), senders[lost].identity.user)
        for n in range(2):
            self.assertEqual(plant.requests_of(n, senders[lost].name, sends_only=True), [])
        # The winner went through (a timing hiccup at a printer may make one of its machines red)
        self.assertTrue(all(m.step != STEP_LOCK for m in winner.machines))
        # Each poller had stepped aside: this PC's own through local_locks (and the gate), another PC's by
        # writing paused_for_lock with the winner's token, which the winner waited for
        self.assertTrue(seen)
        for (i, n), (poller, paused, token) in seen.items():
            self.assertEqual(i, w)
            self.assertTrue(token)
            self.assertTrue(poller == senders[w].key or paused == token, (n, poller, paused, token))
        ok = [n for n in range(2) if winner.machines[n].ok]
        for n, fake in enumerate(plant.fakes):
            with fake.lock:
                writes = list(fake.writes)
            self.assertLessEqual(writes.count("CRTMSG"), 1)     # only the winner built and switched
            self.assertLessEqual(writes.count("PRINTMSG"), 1)
            if n in ok:
                self.assertEqual(winner.machines[n].colour, AMBER)
                plant.check_own_read_backs(senders[w], winner, n)
                self.assertEqual(plant.observer.read_expected(plant.mids[n]).send_id, winner.send_id)
        return w, winner, ok

    def confirm_green(self, plant, sender, result, ok):
        """The pollers judge the Send amber; the sender's confirmation turns it green with what the printer
        holds."""
        self.assertTrue(wait(lambda: all(plant.code(n) == "unconfirmed" for n in ok), 6 * S))
        sender.pipeline.confirm(result.send_id, [plant.mids[n] for n in ok], True)
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in ok), 6 * S))
        for n in ok:
            r = next(m for m in result.machines if m.machine_id == plant.mids[n])
            p = plant.p(n)
            self.assertEqual(p.running.fingerprint, held_fingerprint(plant.fakes[n], r.slot))
            self.assertEqual(p.alerts, [])

    def test_send_from_two_pcs_at_once(self):
        """Two PCs that don't poll send to the same line at the same moment while two others poll: one takes
        the locks and sends, the other is refused at its lock step and sends nothing."""
        plant = Plant(self, printers=2)
        pollers = [plant.pc("bob"), plant.pc("carol")]
        for pc in pollers:
            pc.start()
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in range(2)), 8 * S))
        senders = [plant.sender_pc("jsmith"), plant.sender_pc("akim")]
        w, winner, ok = self.send_at_once(plant, senders)
        self.assertTrue(all(c.last_request_at is None for _, c in senders[1 - w].clients._clients.values()))
        self.confirm_green(plant, senders[w], winner, ok)
        for pc in pollers:
            pc.stop()
        for n in range(2):
            plant.check_sends_alone(n)
        plant.check_one_request_at_a_time()
        plant.check_one_writer()

    def test_two_polling_pcs_send_at_once(self):
        """The two PCs that poll the line's printers send to it at the same moment, each through its own
        poller's store, pool and gates: one takes the locks and sends while both pollers step aside; the
        other is refused at its lock step, sends nothing, and its poller carries on."""
        plant = Plant(self, printers=2)
        pcs = [plant.pc("bob"), plant.pc("carol")]
        for pc in pcs:
            pc.start()
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in range(2)), 8 * S))
        w, winner, ok = self.send_at_once(plant, pcs)
        self.confirm_green(plant, pcs[w], winner, ok)
        # Each printer's poller polls again after the Send
        t_end = time.monotonic()
        polled = lambda n: plant.requests_of(n, plant.pollers(n, pcs)[0].name, since=t_end)
        self.assertTrue(wait(lambda: all(polled(n) for n in range(2)), 4 * S))
        for pc in pcs:
            pc.stop()
        for n in range(2):
            # Only the winner's Send talked to the printer, once it got that far
            sends = plant.check_sends_alone(n)
            self.assertLessEqual(len(sends), 1)
            if n in ok:
                self.assertEqual(len(sends), 1)
            self.assertTrue(all(who == pcs[w].name for who, _ in sends))
        plant.check_one_request_at_a_time()
        plant.check_one_writer()

    def test_takeover_during_a_send(self):
        """The PC polling the printer loses the share in the middle of a Send from a third PC, after it had
        stepped aside for the Send's lock. The Send waits between two steps until the other PC has taken
        over: that PC finds the lock and steps aside too, and makes no request until the Send is done; then
        it judges the Send, and the sender's confirmation turns it green (architecture §7.3)."""
        plant = Plant(self, printers=1)
        pcs = [plant.pc(name, fs=plant.world.fs.view(down=False)) for name in ("akim", "bob")]
        sender = plant.sender_pc("jsmith")
        for pc in pcs:
            pc.start()
        t = plant.timings
        mid, pid = plant.mids[0], plant.pids[0]
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8 * S))
        first, second = plant.pollers(0, pcs)
        held = {}

        def took_over(token):
            p = plant.p(0)
            return p is not None and p.poller == second.key and p.paused_for_lock == token

        def on_event(event):
            if held or not (isinstance(event, SendEvent) and event.machine_id == mid and event.step == STEP_BUILD
                            and event.status == "running"):
                return
            # The machine is checked and its clock set; nothing is built yet
            held["lock"], held["p"] = plant.observer.read_lock(pid), plant.p(0)
            held["starts"] = len(second.entries("polling_started"))     # log entries carry no time
            first.store.fs.down = True
            held["t0"] = time.monotonic()
            held["ok"] = wait(lambda: took_over(held["lock"].token), t.takeover_s[1] + 3 * t.heartbeat_s + 2 * S)
            held["t1"] = time.monotonic()
            time.sleep(2 * t.poll_fast_s)       # the new poller goes round a few times while the Send waits
        result = sender.send((0,), on_event=on_event)
        self.assertTrue(held.get("ok"), held)
        # The first poller had stepped aside for the Send before it was cut off
        self.assertEqual((held["p"].poller, held["p"].paused_for_lock), (first.key, held["lock"].token))
        took = held["t1"] - held["t0"]
        self.assertGreaterEqual(took, t.takeover_s[0] * 0.8)
        self.assertLess(took, t.takeover_s[1] + 3 * t.heartbeat_s + settle_s(t) + 1.0 * S)
        # Its first start after the cut is the takeover. On a busy machine it may hand back to the cut-off
        # PC once (its last heartbeat seen a pass late) and start again with reason "restart" (§6.3)
        self.assertEqual(second.entries("polling_started")[held["starts"]]["reason"], "takeover")
        # The Send went on after the hold (a timing hiccup at the printer may make it red)
        (r,) = result.machines
        self.assertGreaterEqual(STEPS.index(r.step), STEPS.index(STEP_BUILD), (r.step, r.msg))
        self.assertEqual(r.colour, AMBER if r.ok else RED)
        # The new poller made its first request only after the Send's last one
        ((_, (_, send_end)),) = plant.check_sends_alone(0).items()
        self.assertTrue(all(t0 >= send_end for t0, _ in plant.requests_of(0, second.name, since=held["t0"])))
        if r.ok:
            plant.check_own_read_backs(sender, result, 0)
            self.confirm_green(plant, sender, result, [0])
        self.assertTrue(wait(lambda: plant.requests_of(0, second.name, since=held["t0"]), 4 * S))
        self.assertEqual(plant.p(0).poller, second.key)
        for pc in pcs:
            pc.stop()
        plant.check_one_request_at_a_time()
        plant.check_one_writer()

    def test_takeover_while_a_send_waits_for_the_poller(self):
        """The PC polling the printer loses the share just as a Send starts, before it could step aside. The
        Send goes on once the poll file is stale or the PC taking over has stepped aside for its lock, and
        re-checks right before its first request; the PC taking over makes its first request only after a
        settled lock read, so it finds the lock first (storage.md §11.6). Whoever wins that race, nobody
        else talks to the printer while the Send does."""
        plant = Plant(self, printers=1)
        pcs = [plant.pc(name, fs=plant.world.fs.view(down=False)) for name in ("akim", "bob")]
        sender = plant.sender_pc("jsmith")
        for pc in pcs:
            pc.start()
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8 * S))
        first, second = plant.pollers(0, pcs)
        first.store.fs.down = True
        t0 = time.monotonic()
        result = sender.send((0,))
        (r,) = result.machines
        self.assertTrue(wait(lambda: plant.p(0).poller == second.key, 4 * S))
        sends = plant.check_sends_alone(0)
        if r.ok:
            self.assertEqual(r.colour, AMBER)
            plant.check_own_read_backs(sender, result, 0)
            ((_, (_, send_end)),) = sends.items()
            self.assertTrue(all(t >= send_end for t, _ in plant.requests_of(0, second.name, since=t0)))
            self.confirm_green(plant, sender, result, [0])
            self.assertEqual(plant.p(0).poller, second.key)
        else:
            # Nobody stepped aside within the wait (2 × stale_s), and the Send sent nothing; or a timing
            # hiccup at the printer later on
            self.assertEqual(r.colour, RED)
            if r.step == STEP_WAIT_POLLER:
                self.assertEqual(sends, {})
        self.assertTrue(wait(lambda: plant.requests_of(0, second.name, since=t0), 4 * S))
        for pc in pcs:
            pc.stop()
        plant.check_one_request_at_a_time()
        plant.check_one_writer()

    def test_confirm_at_once_never_shows_the_old_origin_green(self):
        """The sender confirms the moment the Send ends, before the poller has judged it: until the poller's
        Loads of the new message commit, the board shows the origin unknown in amber, never the previous
        message's origin, and never green with an origin the printer isn't printing (states.md §5.1 steps
        2.5 and 2.7, §5.3 rule 0c)."""
        old_origin = "Product of USA"
        plant = Plant(self, printers=1, printer_kw={"mixup": False})
        fake = plant.fakes[0]
        with fake.lock:
            fake.slots[9] = design(9, "EXCO1-PKO07-OLD0-M9", origin=old_origin)
            fake.current_slot = 9
        plant.expect(0)
        poller = plant.pc("bob")
        poller.start()
        sender = plant.sender_pc("jsmith")
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed" and plant.p(0).running is not None
                             and plant.p(0).running.origin_line == old_origin, 10 * S))
        machine = sender.data.current.machines[plant.mids[0]]
        samples, stop = [], threading.Event()

        def sample():
            while not stop.is_set():
                t = time.monotonic()
                try:
                    p = plant.observer.read_poll(plant.mids[0])
                    e = plant.observer.read_expected(plant.mids[0])
                    lk = plant.observer.read_lock(plant.pids[0])
                except Exception:
                    continue
                v = hx_state.machine_view(machine, p, 0.0, e, lk, 0.0 if lk is not None else None, {},
                                          plant.timings)
                with fake.lock:
                    held = hx_message.origin_from_design(fake.slots[fake.current_slot])
                samples.append((t, v.code, v.colour, v.origin_line, held))
                time.sleep(0.005)
        th = threading.Thread(target=sample, daemon=True)
        th.start()
        result = sender.send((0,))
        (m,) = result.machines
        self.assertTrue(m.ok, m)
        t_sent = time.monotonic()
        sender.pipeline.confirm(result.send_id, [plant.mids[0]], True)
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed"
                             and plant.p(0).running.origin_line == ORIGIN, 6 * S))
        time.sleep(0.2 * S)
        stop.set()
        th.join(2 * S)
        after = [s[1:] for s in samples if s[0] >= t_sent]     # read after the Send ended
        self.assertTrue(after)
        self.assertEqual([s for s in after if s[2] == old_origin], [])
        self.assertEqual([s for s in after if s[1] == "green" and s[2] != s[3]], [])
        self.assertEqual(after[-1][:3], ("running_confirmed", "green", ORIGIN))
        poller.stop()
        plant.check_one_request_at_a_time()

    def test_a_stuck_worker_is_replaced(self):
        """A polling PC's worker thread really blocks while its coordinator heart-beats (states.md §6.2
        step 3): its PC fences the worker and declines the printer, the other PC polls it within about
        70 scaled seconds, and no board shows a green that isn't freshly judged."""
        plant = Plant(self, printers=1)
        a, b = plant.pc("akim"), plant.pc("bob")
        a.start()
        b.start()
        self.addCleanup(b.stop)
        self.addCleanup(a.stop)
        pid = plant.pids[0]
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8 * S))
        poller, other = plant.pollers(0, [a, b])
        w = poller.poller._workers[pid]
        hang = threading.Event()
        self.addCleanup(hang.set)
        stuck = threading.Event()

        def blocked_cycle():
            stuck.set()
            hang.wait(30.0)
        w._cycle = blocked_cycle
        self.assertTrue(stuck.wait(2.0 * S))
        t0 = time.monotonic()
        views = []
        while time.monotonic() < t0 + 120 * F:
            time.sleep(5 * F)
            board = other.watcher.latest
            if board is not None:
                m = board.lines[0].machines[0]
                views.append(((time.monotonic() - t0) / F, m.colour, m.monitored, m.checked_age_s))
        with plant._lock:
            by_other = [e for e in plant.exchanges if e[1] == other.name and e[3] >= t0]
        self.assertTrue(by_other and (by_other[0][3] - t0) / F <= 70, by_other[:2])
        self.assertTrue(w.fenced)
        self.assertIn(pid, poller.poller._fenced)
        stale_green = [v for v in views if v[1] == GREEN and v[2] and v[3] is not None
                       and v[3] >= plant.timings.not_monitored_s]
        self.assertEqual(stale_green, [])
        hang.set()
        self.assertTrue(wait(lambda: w.done, 2 * S))
        plant.check_one_request_at_a_time()

    def block_worker(self, pc, pid):
        """The PC's worker for printer pid really blocks in its next cycle while its coordinator heart-beats.
        Returns the worker once it is stuck."""
        w = pc.poller._workers[pid]
        hang = threading.Event()
        self.addCleanup(hang.set)
        stuck = threading.Event()

        def blocked_cycle():
            stuck.set()
            hang.wait(30.0)
        w._cycle = blocked_cycle
        self.assertTrue(stuck.wait(2.0 * S))
        return w

    def test_a_backup_pc_takes_over_before_the_not_monitored_alarm(self):
        """states.md §7 item 7: a stuck worker whose printer the other PC polls within about 70 s never rings
        the NOT MONITORED alarm (after 120 s of NOT MONITORED) on any PC. Once nobody polls it, both ring."""
        self.enterContext(mock.patch.object(hx_poller, "NOT_MONITORED_ALARM_S", 120 * F))
        plant = Plant(self, printers=1)
        a, b = plant.pc("akim"), plant.pc("bob")
        a.start()
        b.start()
        self.addCleanup(b.stop)
        self.addCleanup(a.stop)
        pid = plant.pids[0]
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8 * S))
        time.sleep(4 * plant.timings.watch_s)           # both watchers have seen it judged and printing
        poller, other = plant.pollers(0, [a, b])
        w = self.block_worker(poller, pid)
        t0 = time.monotonic()
        codes, rang = set(), []
        while time.monotonic() < t0 + 300 * F:
            time.sleep(2 * F)
            for pc in (a, b):
                board = pc.watcher.latest
                m = board.lines[0].machines[0]
                codes.add(m.code)
                rang += [x.id for x in m.alerts if x.kind == "not_monitored"]
        with plant._lock:
            by_other = [e for e in plant.exchanges if e[1] == other.name and e[3] >= t0]
            log = list(plant.write_log)
        self.assertTrue(by_other and (by_other[0][3] - t0) / F <= 70, by_other[:2])
        self.assertTrue(w.fenced)
        # Nobody judged it for not_monitored_s: the backup polls only once the stuck PC has declined the
        # printer, after not_monitored_s with no good P write of its own (states.md §6.2 item 3.0)...
        judged = max(x.judged for x in log if x.key == poller.key and x.judged is not None)
        last = min(x.t for x in log if x.key == poller.key and x.judged == judged)
        backup = [x for x in log if x.key == other.key and x.judged is not None and x.judged > judged]
        self.assertTrue(backup)
        self.assertGreaterEqual((backup[0].t - last) / F, 0.9 * 30)
        # ...and the boards showed it unjudged: NOT MONITORED, or the backup's start write after that gap
        # first, "Checking..." (§6.3 step 1), which the alarm counts as the same spell (§7 item 7.5). Which
        # one a board shows first is a race between the two PCs' passes (on a slow runner the watcher sees
        # the last judgement a pass later, and the start write lands before its NOT MONITORED)
        restarted = any(x.key == other.key and x.state == "checking" and x.t >= t0 for x in log)
        self.assertTrue("not_monitored" in codes or ("checking" in codes and restarted), codes)
        self.assertEqual(rang, [])                      # ...but never long enough to ring
        self.assertTrue(wait(lambda: all(pc.watcher.latest.lines[0].machines[0].code == "running_confirmed"
                                         for pc in (a, b)), 2 * S))
        # The control: the backup's poller stops too, so nobody polls the printer (the stuck PC still
        # declines it). Both watchers saw it judged printing, and both ring, with one id
        time.sleep(4 * plant.timings.watch_s)
        other.poller.stop()
        alarms = lambda pc: [x.id for x in pc.watcher.latest.lines[0].machines[0].alerts if x.kind == "not_monitored"]
        self.assertTrue(wait(lambda: alarms(a) and alarms(b), 300 * F))
        self.assertEqual(alarms(a), alarms(b))
        plant.check_one_request_at_a_time()

    def test_a_stuck_worker_with_no_backup_pc_rings(self):
        """states.md §7 item 7: the only PC that can poll the printer has a stuck worker. Nobody judges the
        printing machine: NOT MONITORED after about 30 s, and the alarm about 120 s later, on that PC's own
        board (its floor board rings), the machine still grey NOT MONITORED."""
        self.enterContext(mock.patch.object(hx_poller, "NOT_MONITORED_ALARM_S", 120 * F))
        plant = Plant(self, printers=1)
        a = plant.pc("akim")
        a.start()
        self.addCleanup(a.stop)
        pid = plant.pids[0]
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8 * S))
        time.sleep(4 * plant.timings.watch_s)
        self.block_worker(a, pid)
        t0 = time.monotonic()
        ringing = lambda: [x for x in hx_state.sounding_alerts(a.watcher.latest, a.identity, floor_board=True)
                           if x.kind == "not_monitored"]
        self.assertTrue(wait(ringing, 300 * F))
        took = (time.monotonic() - t0) / F
        # The last judgement was at most one cycle before the worker blocked: 30 s + 120 s after it, give or
        # take a watcher pass and the test machine
        self.assertGreaterEqual(took, 140)
        self.assertLessEqual(took, 200)
        (alarm,) = ringing()
        p = plant.p(0)
        self.assertEqual(alarm.id, hx_state.not_monitored_alarm_id(plant.mids[0], p.judged, p.judged_at,
                                                                   "sd_0000000000"))
        m = a.watcher.latest.lines[0].machines[0]
        self.assertEqual((m.code, m.colour), ("not_monitored", GREY))
        self.assertIn(pid, a.poller._fenced)
        plant.check_one_request_at_a_time()

    def test_slow_everything(self):
        """A slow printer, a slow share and a silent time server; a rank-0 and a rank-1 poller, and a
        sender on a third store sending twice (states.md §12.3 case 1)."""
        slow, latency = 3 * F, 1.9 * F
        plant = Plant(self, printers=1, printer_kw={"latency_s": latency})
        t = plant.timings
        plant.world.sntp.silent = True
        silent = lambda: hx_time.TimeSource([f"127.0.0.1:{plant.world.sntp.port}"], offset_fn=EDT)
        a, b = plant.pc("akim", time_source=silent()), plant.pc("bob", time_source=silent())
        sender = plant.sender_pc("jsmith", time_source=silent())
        plant.world.fs.slow_s = slow                    # the records are loaded; from now on the share is slow
        # The waits come from the spec's budgets on this slow share and printer. Each request costs three
        # slow share operations (a lock read, a P read, a keep-fresh P write) and the printer's latency. A
        # cycle that reads the whole slow set is at most 18 requests, 6 more share operations and the
        # second reading (states.md §6.5). The first request comes after stale_s (rank 0 starts on a P
        # missing that long), a coordinator pass, the start's share operations and the settle (§6.3). Every
        # cut the spec makes (a write or lock read over stale_s / 4, a Load that doesn't fit before
        # stale_s / 2, the Load repeat window, a share timeout) costs a cycle: the first Load here barely
        # fits, so it is often cut and done again in the next cycle (§6.4), and a judgement needs its
        # second reading
        request = 3 * slow + latency
        cycle = 18 * request + 6 * slow + t.second_reading_s
        start_up = t.stale_s + t.heartbeat_s + 12 * slow + settle_s(t)
        a.start()
        b.start()
        self.assertTrue(wait(lambda: len(plant.fakes[0].requests) > 12, start_up + 2 * cycle))
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 4 * cycle))
        results = []
        for lot in ("4417", "4418"):
            results.append(sender.send((0,), lot=lot))
            time.sleep(4 * t.poll_fast_s)                    # the poller is back between the Sends
        a.stop()
        b.stop()
        plant.world.fs.slow_s = 0.0
        self.assertTrue(plant.fakes[0].requests)
        reached = 0
        for i, result in enumerate(results):
            (r,) = result.machines
            # Each Send reached the printer while both pollers ran. Near the timeouts a request may get
            # no reply, which makes that Send red; one that went through is amber on the PC's time. On a
            # loaded test machine the slow share can also cost the first Send its lock (its keeper
            # couldn't refresh in time): that lock is left for lock_stale_s, so the second Send, half a
            # second later, finds it held by this same sender and sends nothing. The pollers' step-aside
            # (a Load in flight, a slow lock read, a slow P write) takes most of the Send's wait of
            # 2 × stale_s here, so a busy machine may also see a Send refused before its first request
            if r.step == STEP_LOCK:
                (before,) = results[i - 1].machines if i else (None,)
                self.assertTrue(before is not None and not before.ok, (r.step, r.msg))
                self.assertEqual(r.msg.values.get("user"), sender.identity.user)
                continue
            if r.step == STEP_WAIT_POLLER:
                self.assertEqual(r.colour, RED)
                with plant._lock:
                    own = [e for e in plant.exchanges if e[2].startswith(f"hx-send-{result.send_id}-")]
                self.assertEqual(own, [])
                continue
            self.assertGreaterEqual(STEPS.index(r.step), STEPS.index(STEP_CHECK_MACHINE), (r.step, r.msg))
            reached += 1
            if r.ok:
                self.assertEqual(r.colour, AMBER)
                self.assertIn("pc_time", r.notes)           # the time server is silent
            else:
                self.assertEqual(r.colour, RED)
        self.assertGreaterEqual(reached, 1)
        plant.check_sends_alone(0)
        plant.check_one_request_at_a_time()
        plant.check_one_writer()

    def test_one_pc_cut_off(self):
        """PC A loses the share alone with its monitor open; B keeps polling and C sends. A checks on its
        own only after lock_stale_s, and those checks may overlap C's Sends, which A can't see (states.md
        §12.3 case 2): every read-back of C matched its own TN and XID, and no Send turns green with a
        content the printer doesn't hold."""
        plant = Plant(self, printers=1)
        a = plant.pc("akim", fs=plant.world.fs.view(down=False))
        b = plant.pc("bob")
        c = plant.sender_pc("jsmith")
        a.poller.set_monitor_open(True, False)
        a.start()
        b.start()
        t = plant.timings
        fake, mid = plant.fakes[0], plant.mids[0]
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8 * S))
        a.store.fs.down = True
        t0 = time.monotonic()
        modes = []
        self.assertTrue(wait(lambda: modes.append(a.poller.mode) or a.poller.mode == "standalone",
                             t.lock_stale_s + 4 * S))
        self.assertGreaterEqual(time.monotonic() - t0, t.lock_stale_s * 0.95)
        self.assertIn("idle", modes)                     # share, then idle until lock_stale_s, then standalone
        self.assertNotIn("share", modes[modes.index("idle"):])
        self.assertTrue(wait(lambda: a.poller.local_statuses().get(mid) is not None, t.standalone_poll_s + 2 * S))
        self.assertTrue(a.poller.local_statuses()[mid].standalone)
        self.assertEqual(plant.p(0).poller, b.key)
        # C sends twice while A keeps checking on its own, as often as Refresh now allows
        stop = threading.Event()

        def refresh():
            while not stop.is_set():
                a.poller.refresh_now()
                stop.wait(t.refresh_now_min_s / 2)
        refresher = threading.Thread(target=refresh)
        refresher.start()
        try:
            for lot in ("4417", "4418"):
                result = c.send((0,), lot=lot)
                r = plant.check_own_read_backs(c, result, 0)
                if r is None:
                    continue                             # a red Send is never green
                self.assertEqual(r.colour, AMBER)
                # C's share is fine: no "no_lock" note, although A can't see its lock (README sign-off 2)
                self.assertNotIn("no_lock", r.notes)
                e = plant.observer.read_expected(mid)
                self.assertEqual((e.send_id, e.fingerprint), (result.send_id, r.fingerprint))
                # Confirmed, B shows it green only with the content the printer holds
                c.pipeline.confirm(result.send_id, [mid], True)
                self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed"
                                     and plant.p(0).expected_counter == plant.observer.read_expected(mid).counter,
                                     6 * S))
                self.assertEqual(plant.p(0).running.fingerprint, held_fingerprint(fake, r.slot))
        finally:
            stop.set()
            refresher.join()
        self.assertTrue(any(e.get("standalone") for e in a.entries("state_changed")))
        self.assertTrue(plant.requests_of(0, "akim", since=t0))     # A did check on its own meanwhile
        a.store.fs.down = False
        self.assertTrue(wait(lambda: a.poller.mode == "share", t.share_retry_s + 3 * S))
        a.stop()
        b.stop()
        # Overlaps with A's standalone checks are allowed here (architecture §7.3 rule 7), but B, which sees
        # C's locks, never talks to the printer during a Send
        plant.check_sends_alone(0, ignore=("akim",))
        for fake in plant.fakes:
            self.assertEqual([v for v in fake.violations if v != "two requests in flight"], [])


if __name__ == "__main__":
    unittest.main()
