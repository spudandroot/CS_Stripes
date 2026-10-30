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

import dataclasses
import tempfile
import threading
import time
import unittest

import hx_fake
import hx_message
import hx_poller
import hx_protocol
import hx_send
import hx_share
import hx_state
import hx_time
from hx_core import SYSTEM_CLOCK, EventBus
from hx_model import (AMBER, RED, STEP_BUILD, STEP_CHECK_MACHINE, STEP_LOCK, STEP_WAIT_POLLER, STEPS, TIMINGS,
                      DesignItem, ExpectedState, LineChoice, MessageDesign, SendEvent, SendRequest, new_id)
from hx_share import settle_s

F = 0.025
# Scaled waits; the printer requests keep a little more time, for a busy test machine. A Send sets the
# printer's clock to the minute (no seconds field), so the clock warning keeps its real 120 s
SCALED = dataclasses.replace(TIMINGS.scaled(F), http_timeout_s=0.08, clock_warn_s=TIMINGS.clock_warn_s)
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
        self.data.run_once()
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
            real_write(status)
            plant.note_write(t, status, self.key)
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
        self.data.run_once()
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
        self.sender = self.world.store(self.world.identity("jsmith"))
        self.pcs = []
        self.write_log = []
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

    def note_write(self, t, status, key):
        with self._lock:
            self.write_log.append((t, status.machine_id, key, status.yielded_to, status.counter))

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
        """poll.json changes writer only after a yield, after the old writer went quiet for about stale_s, or
        while a starting poller settles and yields at its rank check before any request."""
        t = self.timings
        with self._lock:
            log = sorted(self.write_log)
        for mid in self.mids:
            writes = [w for w in log if w[1] == mid]
            writer, last, yielded = None, None, ""
            for time_, _, key, yielded_to, _ in writes:
                if writer is not None and key != writer:
                    handed = yielded == key
                    quiet = time_ - last >= 0.8 * t.stale_s
                    # A start race (states.md §6.3 step 2): one of the two yields at its rank check, before any
                    # request, to the other or, when three PCs start together, to a third
                    racing = any(w[2] in (key, writer) and w[3] and w[3] != w[2]
                                 and time_ - t.stale_s <= w[0] <= time_ + t.stale_s for w in writes)
                    self.test.assertTrue(handed or quiet or racing, (mid, writer, key, round(time_ - last, 3)))
                if key != writer:
                    writer = key
                last, yielded = time_, yielded_to


class PlantTests(unittest.TestCase):
    def test_pcs_drop_out_and_return(self):
        plant = Plant(self)
        pcs = [plant.pc(name, fs=plant.world.fs.view(down=False)) for name in ("akim", "bob", "carol")]
        for pc in pcs:
            pc.start()
        t = plant.timings
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in range(3)), 8))
        # A warning on machine 2, so its alert id can be followed through a takeover
        plant.fakes[1].ink_level = 2
        self.assertTrue(wait(lambda: [a.kind for a in plant.p(1).alerts] == ["low_ink"], 6))
        alert_id = plant.p(1).alerts[0].id
        # The PC polling machine 2 loses the share: another PC takes over
        dropped = next(pc for pc in pcs if pc.key == plant.p(1).poller)
        dropped.store.fs.down = True
        t_cut = time.monotonic()
        self.assertTrue(wait(lambda: plant.p(1) is not None and plant.p(1).poller != dropped.key, 6))
        took = time.monotonic() - t_cut
        self.assertLess(took, t.takeover_s[1] + 3 * t.heartbeat_s + 1.0)
        self.assertGreaterEqual(took, t.takeover_s[0] * 0.8)
        self.assertTrue(wait(lambda: [a.id for a in plant.p(1).alerts] == [alert_id], 4))
        # A printer is switched off, and comes back
        plant.fakes[2].go_silent()
        self.assertTrue(wait(lambda: plant.code(2) == "off", 6))
        plant.fakes[2].go_silent(False)
        self.assertTrue(wait(lambda: plant.code(2) == "running_confirmed", 8))
        # The dropped PC returns
        dropped.store.fs.down = False
        self.assertTrue(wait(lambda: dropped.poller.mode == "share" and dropped.poller.polling, 8))
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
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in range(2)), 8))
        a.poller.stop()
        b.poller.stop()
        t0 = time.monotonic()
        watcher = b.watcher

        def codes():
            board = watcher.latest
            return [m.code for line in board.lines for m in line.machines] if board else []
        self.assertTrue(wait(lambda: codes() == ["not_monitored"] * 2, 4))
        self.assertGreaterEqual(time.monotonic() - t0, plant.timings.not_monitored_s * 0.9)
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
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8))
        with plant.fakes[0].lock:
            plant.fakes[0].slots[9] = design(9, plant.fakes[0].slots[9].name, "Product of Mexico")
        ringing = lambda pc: hx_state.sounding_alerts(pc.watcher.latest, pc.identity, floor_board=True)
        self.assertTrue(wait(lambda: all(pc.watcher.latest and ringing(pc) for pc in pcs), 10))
        ids = [a.id for a in ringing(pcs[0])]
        pcs[0].watcher.acknowledge(ids)
        self.assertEqual(ringing(pcs[0]), [])
        t0 = time.monotonic()
        self.assertTrue(wait(lambda: not any(ringing(pc) for pc in pcs), 3))
        self.assertLess(time.monotonic() - t0, 2 * plant.timings.watch_s + 0.5)
        self.assertIsNotNone(plant.observer.read_ack(ids[0]))
        self.assertTrue(wait(lambda: plant.p(0).alerts and plant.p(0).alerts[0].acked_by == pcs[0].identity.user, 3))
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
            th.join(20)
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
        self.assertTrue(wait(lambda: all(plant.code(n) == "unconfirmed" for n in ok), 6))
        sender.pipeline.confirm(result.send_id, [plant.mids[n] for n in ok], True)
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in ok), 6))
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
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in range(2)), 8))
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
        self.assertTrue(wait(lambda: all(plant.code(n) == "running_confirmed" for n in range(2)), 8))
        w, winner, ok = self.send_at_once(plant, pcs)
        self.confirm_green(plant, pcs[w], winner, ok)
        # Each printer's poller polls again after the Send
        t_end = time.monotonic()
        polled = lambda n: plant.requests_of(n, plant.pollers(n, pcs)[0].name, since=t_end)
        self.assertTrue(wait(lambda: all(polled(n) for n in range(2)), 4))
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
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8))
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
            first.store.fs.down = True
            held["t0"] = time.monotonic()
            held["ok"] = wait(lambda: took_over(held["lock"].token), t.takeover_s[1] + 3 * t.heartbeat_s + 2)
            held["t1"] = time.monotonic()
            time.sleep(2 * t.poll_fast_s)       # the new poller goes round a few times while the Send waits
        result = sender.send((0,), on_event=on_event)
        self.assertTrue(held.get("ok"), held)
        # The first poller had stepped aside for the Send before it was cut off
        self.assertEqual((held["p"].poller, held["p"].paused_for_lock), (first.key, held["lock"].token))
        took = held["t1"] - held["t0"]
        self.assertGreaterEqual(took, t.takeover_s[0] * 0.8)
        self.assertLess(took, t.takeover_s[1] + 3 * t.heartbeat_s + settle_s(t) + 1.0)
        self.assertEqual(second.entries("polling_started")[-1]["reason"], "takeover")
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
        self.assertTrue(wait(lambda: plant.requests_of(0, second.name, since=held["t0"]), 4))
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
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8))
        first, second = plant.pollers(0, pcs)
        first.store.fs.down = True
        t0 = time.monotonic()
        result = sender.send((0,))
        (r,) = result.machines
        self.assertTrue(wait(lambda: plant.p(0).poller == second.key, 4))
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
        self.assertTrue(wait(lambda: plant.requests_of(0, second.name, since=t0), 4))
        for pc in pcs:
            pc.stop()
        plant.check_one_request_at_a_time()
        plant.check_one_writer()

    def test_slow_everything(self):
        """A slow printer, a slow share and a silent time server; a rank-0 and a rank-1 poller, and a
        sender on a third store sending twice (states.md §12.3 case 1)."""
        plant = Plant(self, printers=1, printer_kw={"latency_s": 1.9 * F})
        plant.world.sntp.silent = True
        silent = lambda: hx_time.TimeSource([f"127.0.0.1:{plant.world.sntp.port}"], offset_fn=EDT)
        a, b = plant.pc("akim", time_source=silent()), plant.pc("bob", time_source=silent())
        sender = plant.sender_pc("jsmith", time_source=silent())
        plant.world.fs.slow_s = 3 * F                   # the records are loaded; from now on the share is slow
        a.start()
        b.start()
        self.assertTrue(wait(lambda: len(plant.fakes[0].requests) > 12, 8))
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8))
        results = []
        for lot in ("4417", "4418"):
            results.append(sender.send((0,), lot=lot))
            time.sleep(4 * plant.timings.poll_fast_s)        # the poller is back between the Sends
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
        self.assertTrue(wait(lambda: plant.code(0) == "running_confirmed", 8))
        a.store.fs.down = True
        t0 = time.monotonic()
        modes = []
        self.assertTrue(wait(lambda: modes.append(a.poller.mode) or a.poller.mode == "standalone",
                             t.lock_stale_s + 4))
        self.assertGreaterEqual(time.monotonic() - t0, t.lock_stale_s * 0.95)
        self.assertIn("idle", modes)                     # share, then idle until lock_stale_s, then standalone
        self.assertNotIn("share", modes[modes.index("idle"):])
        self.assertTrue(wait(lambda: a.poller.local_statuses().get(mid) is not None, t.standalone_poll_s + 2))
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
                                     and plant.p(0).expected_counter == plant.observer.read_expected(mid).counter, 6))
                self.assertEqual(plant.p(0).running.fingerprint, held_fingerprint(fake, r.slot))
        finally:
            stop.set()
            refresher.join()
        self.assertTrue(any(e.get("standalone") for e in a.entries("state_changed")))
        self.assertTrue(plant.requests_of(0, "akim", since=t0))     # A did check on its own meanwhile
        a.store.fs.down = False
        self.assertTrue(wait(lambda: a.poller.mode == "share", t.share_retry_s + 3))
        a.stop()
        b.stop()
        # Overlaps with A's standalone checks are allowed here (architecture §7.3 rule 7), but B, which sees
        # C's locks, never talks to the printer during a Send
        plant.check_sends_alone(0, ignore=("akim",))
        for fake in plant.fakes:
            self.assertEqual([v for v in fake.violations if v != "two requests in flight"], [])


if __name__ == "__main__":
    unittest.main()
