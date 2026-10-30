"""Tests for hx_core: clocks, cancelling, Msg and the event bus. Run: python -m unittest -v"""

import io
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

import hx_core
from hx_core import CancelToken, Cancelled, EventBus, FakeClock, Msg, SYSTEM_CLOCK, SystemClock


def wait_until(condition, timeout_s=2.0):
    """Real-time wait for a condition another thread makes true (polls every 5 ms)."""
    end = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > end:
            return False
        time.sleep(0.005)
    return True


class Sleeper(threading.Thread):
    """Calls clock.sleep(seconds, cancel) on its own thread and keeps what it returned or raised."""

    def __init__(self, clock, seconds, cancel=None):
        super().__init__(daemon=True)
        self.clock, self.seconds, self.cancel = clock, seconds, cancel
        self.result = self.error = None

    def run(self):
        try:
            self.result = self.clock.sleep(self.seconds, self.cancel)
        except Exception as e:
            self.error = e


class FakeClockTests(unittest.TestCase):
    def test_start(self):
        clock = FakeClock()
        self.assertEqual(clock.time(), 1_793_275_200.0)   # 29 Oct 2026 12:00 UTC
        self.assertEqual(clock.monotonic(), 1000.0)
        self.assertEqual(FakeClock(5.0).time(), 5.0)

    def test_now_is_local_time(self):
        clock = FakeClock(1_793_275_200.25)
        self.assertEqual(clock.now(), datetime(*time.localtime(1_793_275_200)[:6], microsecond=250_000))
        self.assertIsNone(clock.now().tzinfo)

    def test_advance_and_set_wall(self):
        clock = FakeClock()
        clock.advance(15)
        self.assertEqual((clock.time(), clock.monotonic()), (1_793_275_215.0, 1015.0))
        clock.set_wall(100.0)     # a PC clock jump moves the wall clock only
        self.assertEqual((clock.time(), clock.monotonic()), (100.0, 1015.0))
        with self.assertRaises(ValueError):
            clock.advance(-1)

    def test_auto_advance(self):
        clock = FakeClock()
        self.assertTrue(clock.sleep(2.5))
        self.assertEqual((clock.time(), clock.monotonic()), (1_793_275_202.5, 1002.5))
        self.assertTrue(clock.sleep(-1))           # nothing to wait for
        self.assertEqual(clock.monotonic(), 1002.5)
        cancel = CancelToken()
        cancel.cancel()
        self.assertFalse(clock.sleep(5, cancel))   # already cancelled: no time passes
        self.assertEqual(clock.monotonic(), 1002.5)
        self.assertEqual(clock.sleepers(), 0)

    def test_manual_sleep_waits_for_advance(self):
        clock = FakeClock(auto_advance=False)
        sleeper = Sleeper(clock, 5.0)
        sleeper.start()
        self.assertTrue(wait_until(lambda: clock.sleepers() == 1))
        clock.advance(3)
        time.sleep(0.05)
        self.assertTrue(sleeper.is_alive())        # not due yet
        clock.advance(2)
        sleeper.join(2)
        self.assertFalse(sleeper.is_alive())
        self.assertIs(sleeper.result, True)
        self.assertEqual(clock.sleepers(), 0)
        self.assertEqual(clock.monotonic(), 1005.0)

    def test_manual_sleep_wakes_on_cancel(self):
        clock = FakeClock(auto_advance=False)
        cancel = CancelToken()
        sleeper = Sleeper(clock, 60.0, cancel)
        sleeper.start()
        self.assertTrue(wait_until(lambda: clock.sleepers() == 1))
        cancel.cancel()
        sleeper.join(2)
        self.assertIs(sleeper.result, False)
        self.assertEqual(clock.monotonic(), 1000.0)
        self.assertFalse(clock.sleep(1, cancel))   # already cancelled returns at once

    def test_manual_sleep_zero_returns_at_once(self):
        self.assertTrue(FakeClock(auto_advance=False).sleep(0))

    def test_several_sleepers_wake_in_turn(self):
        clock = FakeClock(auto_advance=False)
        short, long = Sleeper(clock, 1.0), Sleeper(clock, 10.0)
        short.start()
        long.start()
        self.assertTrue(wait_until(lambda: clock.sleepers() == 2))
        clock.advance(1)
        short.join(2)
        self.assertIs(short.result, True)
        self.assertTrue(long.is_alive())
        self.assertTrue(wait_until(lambda: clock.sleepers() == 1))
        clock.advance(9)
        long.join(2)
        self.assertIs(long.result, True)

    def test_small_steps_add_up_exactly(self):
        clock = FakeClock()
        for _ in range(600):
            clock.advance(0.1)
        self.assertEqual((clock.time(), clock.monotonic()), (1_793_275_260.0, 1060.0))
        for _ in range(20):
            clock.sleep(0.05)       # auto mode goes through the same sums
        self.assertEqual((clock.time(), clock.monotonic()), (1_793_275_261.0, 1061.0))

    def test_manual_sleeper_wakes_after_small_steps(self):
        # Float sums of the steps fall a hair short (600 x 0.1 is 59.99999999997 as plain floats)
        for seconds, steps in ((60.0, 600), (1.0, 20), (0.15, 3), (1.5, 5), (3.0, 10), (0.1 * 3, 5)):
            clock = FakeClock(auto_advance=False)
            sleeper = Sleeper(clock, seconds)
            sleeper.start()
            self.assertTrue(wait_until(lambda: clock.sleepers() == 1))
            for _ in range(steps):
                clock.advance(seconds / steps)
            sleeper.join(2)
            self.assertFalse(sleeper.is_alive(), (seconds, steps))
            self.assertIs(sleeper.result, True)
            self.assertGreaterEqual(clock.monotonic(), 1000.0 + seconds)

    def test_manual_sleeper_short_of_its_deadline_stays_asleep(self):
        clock = FakeClock(auto_advance=False)
        sleeper = Sleeper(clock, 1.0)
        sleeper.start()
        self.assertTrue(wait_until(lambda: clock.sleepers() == 1))
        clock.advance(0.999)
        time.sleep(0.05)
        self.assertTrue(sleeper.is_alive())
        clock.advance(0.001)
        sleeper.join(2)
        self.assertIs(sleeper.result, True)

    def test_code_deadline_loop_ends(self):
        # Code that works out its own deadline: 1000.1 + 0.7 is 1000.8000000000001 as a float,
        # one step above the 1000.8 the test advances to
        def wait_for(clock, deadline):
            while clock.monotonic() < deadline:
                if not clock.sleep(deadline - clock.monotonic()):
                    return False
            return True

        clock = FakeClock(auto_advance=False)
        clock.advance(0.1)
        deadline = clock.monotonic() + 0.7
        self.assertGreater(deadline, round(deadline, 9))      # the case this test is about
        result = []
        worker = threading.Thread(target=lambda: result.append(wait_for(clock, deadline)), daemon=True)
        worker.start()
        self.assertTrue(wait_until(lambda: clock.sleepers() == 1))
        for _ in range(7):
            clock.advance(0.1)
        worker.join(2)
        self.assertEqual(result, [True])
        self.assertEqual(clock.monotonic(), deadline)

        # The same when the test advances first and the code sleeps what's left (float noise)
        clock = FakeClock(auto_advance=False)
        clock.advance(0.1)
        deadline = clock.monotonic() + 0.7
        clock.advance(0.7)
        self.assertTrue(wait_for(clock, deadline))            # at once, no 10 s watchdog
        self.assertEqual(clock.monotonic(), deadline)

        clock = FakeClock()                                   # auto mode: one sleep reaches it
        clock.advance(0.1)
        deadline = clock.monotonic() + 0.7
        self.assertTrue(clock.sleep(deadline - clock.monotonic()))
        self.assertGreaterEqual(clock.monotonic(), deadline)

    def test_nobody_advancing_fails_instead_of_hanging(self):
        clock = FakeClock(auto_advance=False)
        with mock.patch.object(hx_core, "_NOBODY_ADVANCED_S", 0.15):
            with self.assertRaisesRegex(RuntimeError, "nobody advanced the clock"):
                clock.sleep(5)
        self.assertEqual(clock.sleepers(), 0)


class SystemClockTests(unittest.TestCase):
    def test_is_the_real_clock(self):
        self.assertIsInstance(SYSTEM_CLOCK, SystemClock)
        self.assertAlmostEqual(SYSTEM_CLOCK.time(), time.time(), delta=1.0)
        self.assertAlmostEqual(SYSTEM_CLOCK.monotonic(), time.monotonic(), delta=1.0)
        self.assertIsNone(SYSTEM_CLOCK.now().tzinfo)

    def test_sleep(self):
        start = time.monotonic()
        self.assertTrue(SYSTEM_CLOCK.sleep(0.02))
        self.assertTrue(SYSTEM_CLOCK.sleep(0.02, CancelToken()))
        self.assertTrue(SYSTEM_CLOCK.sleep(-1))
        self.assertGreaterEqual(time.monotonic() - start, 0.035)

    def test_sleep_cancelled(self):
        cancel = CancelToken()
        threading.Timer(0.05, cancel.cancel).start()
        start = time.monotonic()
        self.assertFalse(SYSTEM_CLOCK.sleep(5, cancel))
        self.assertLess(time.monotonic() - start, 1.0)
        start = time.monotonic()
        self.assertFalse(SYSTEM_CLOCK.sleep(5, cancel))    # already cancelled
        self.assertLess(time.monotonic() - start, 0.1)


class CancelTokenTests(unittest.TestCase):
    def test_cancel(self):
        cancel = CancelToken()
        self.assertFalse(cancel.cancelled)
        cancel.check()                       # nothing happens yet
        self.assertFalse(cancel.wait(0.01))
        cancel.cancel()
        self.assertTrue(cancel.cancelled)
        self.assertTrue(cancel.event.is_set())
        self.assertTrue(cancel.wait(5))      # returns at once
        with self.assertRaises(Cancelled):
            cancel.check()
        cancel.cancel()                      # twice is fine
        self.assertTrue(cancel.cancelled)

    def test_wait_is_woken(self):
        cancel = CancelToken()
        threading.Timer(0.05, cancel.cancel).start()
        start = time.monotonic()
        self.assertTrue(cancel.wait(5))
        self.assertLess(time.monotonic() - start, 1.0)


class MsgTests(unittest.TestCase):
    def test_english(self):
        self.assertEqual(Msg("Printing").english(), "Printing")
        self.assertEqual(Msg("Printing").values, {})
        self.assertEqual(Msg("{line} is being sent by {user}", {"line": "Line 1", "user": "jsmith"}).english(),
                         "Line 1 is being sent by jsmith")
        nested = Msg("{machine} is set for {ink} ink", {"machine": "Line 1 Bagger 1", "ink": Msg("black")})
        self.assertEqual(nested.english(), "Line 1 Bagger 1 is set for black ink")

    def test_english_never_raises(self):
        self.assertEqual(Msg("{missing} value", {"other": 1}).english(), "{missing} value")
        self.assertEqual(Msg("Braces {} without values").english(), "Braces {} without values")

    def test_json_round_trip(self):
        msg = Msg("{machine} is set for {ink} ink; {n} bags", {"machine": "Line 1 Bagger 1",
                                                             "ink": Msg("white"), "n": 3, "x": 1.5})
        d = msg.to_json()
        self.assertEqual(d, {"text": "{machine} is set for {ink} ink; {n} bags",
                             "values": {"machine": "Line 1 Bagger 1", "ink": {"text": "white", "values": {}},
                                        "n": 3, "x": 1.5}})
        self.assertEqual(Msg.from_json(d), msg)
        self.assertEqual(Msg.from_json({"text": "Printing"}), Msg("Printing"))

    def test_from_json_refuses_a_wrong_shape(self):
        for bad in (None, "Printing", {"values": {}}, {"text": 5}, {"text": "x", "values": []}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                Msg.from_json(bad)

    def test_frozen(self):
        with self.assertRaises(AttributeError):
            Msg("a").text = "b"

    def test_hashable(self):
        a = Msg("Lot {lot}", {"lot": "4417"})
        self.assertEqual({a, Msg("Lot {lot}", {"lot": "4417"}), Msg("Lot {lot}", {"lot": "5000"})},
                         {a, Msg("Lot {lot}", {"lot": "5000"})})
        self.assertNotEqual(a, Msg("Lot {lot}", {"lot": "5000"}))


class Ping:
    def __init__(self, n):
        self.n = n


class LoudPing(Ping):
    pass


class EventBusTests(unittest.TestCase):
    def test_order_and_types(self):
        bus = EventBus()
        pings, everything = [], []
        bus.subscribe(Ping, lambda e: pings.append(e.n))
        bus.subscribe(object, everything.append)
        bus.post(Ping(1))
        bus.post("text")
        bus.post(LoudPing(2))    # a subclass reaches Ping subscribers too
        self.assertEqual(pings, [])                  # nothing until pumped
        self.assertEqual(bus.pump(), 3)
        self.assertEqual(pings, [1, 2])
        self.assertEqual(len(everything), 3)
        self.assertEqual(everything[1], "text")
        self.assertEqual(bus.pump(), 0)

    def test_max_events(self):
        bus = EventBus()
        seen = []
        bus.subscribe(Ping, lambda e: seen.append(e.n))
        for n in range(5):
            bus.post(Ping(n))
        self.assertEqual(bus.pump(max_events=2), 2)
        self.assertEqual(seen, [0, 1])
        self.assertEqual(bus.pump(), 3)
        self.assertEqual(seen, [0, 1, 2, 3, 4])

    def test_subscriber_error_is_reported_and_pump_carries_on(self):
        bus = EventBus()
        seen = []

        def broken(event):
            raise RuntimeError("broken subscriber")

        bus.subscribe(Ping, broken)
        bus.subscribe(Ping, lambda e: seen.append(e.n))
        bus.post(Ping(1))
        bus.post(Ping(2))
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(bus.pump(), 2)
        self.assertEqual(seen, [1, 2])
        self.assertIn("broken subscriber", err.getvalue())
        self.assertIn("Traceback", err.getvalue())

    def test_unsubscribe(self):
        bus = EventBus()
        seen = []
        token = bus.subscribe(Ping, lambda e: seen.append(e.n))
        other = bus.subscribe(Ping, lambda e: None)
        self.assertNotEqual(token, other)
        bus.unsubscribe(token)
        bus.unsubscribe(token)         # twice is fine
        bus.unsubscribe(12345)         # unknown is fine
        bus.post(Ping(1))
        bus.pump()
        self.assertEqual(seen, [])

    def test_unsubscribe_during_pump(self):
        bus = EventBus()
        seen = []
        tokens = {}

        def once(event):
            seen.append(("once", event.n))
            bus.unsubscribe(tokens["once"])
            bus.unsubscribe(tokens["later"])       # a later subscriber, removed before its turn

        tokens["once"] = bus.subscribe(Ping, once)
        tokens["later"] = bus.subscribe(Ping, lambda e: seen.append(("later", e.n)))
        bus.subscribe(Ping, lambda e: seen.append(("last", e.n)))
        bus.post(Ping(1))
        bus.post(Ping(2))
        self.assertEqual(bus.pump(), 2)
        self.assertEqual(seen, [("once", 1), ("last", 1), ("last", 2)])

    def test_subscribe_during_pump(self):
        bus = EventBus()
        seen = []

        def first(event):
            if event.n == 1:
                bus.subscribe(Ping, lambda e: seen.append(("new", e.n)))

        bus.subscribe(Ping, first)
        bus.post(Ping(1))
        bus.post(Ping(2))
        bus.pump()
        self.assertEqual(seen, [("new", 2)])     # only events delivered after it subscribed

    def test_nested_pump_keeps_order(self):
        # A callback that opens a modal dialog runs the Tk loop, and the 100 ms pump inside it
        bus = EventBus()
        seen = []
        tokens = {}
        while_open = {}

        def dialog(event):
            seen.append(("dialog", event.n))
            if event.n == 1:
                bus.post(Ping(3))
                while_open["pumped"] = bus.pump()              # events 2 and 3, while open
                while_open["board"] = [n for who, n in seen if who == "board"]
                bus.unsubscribe(tokens["gone"])
                bus.post(Ping(4))
                bus.pump()

        def broken(event):
            raise RuntimeError("broken subscriber")

        tokens["dialog"] = bus.subscribe(Ping, dialog)
        tokens["broken"] = bus.subscribe(Ping, broken)
        tokens["board"] = bus.subscribe(Ping, lambda e: seen.append(("board", e.n)))
        tokens["gone"] = bus.subscribe(Ping, lambda e: seen.append(("gone", e.n)))
        bus.post(Ping(1))
        bus.post(Ping(2))
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(bus.pump(), 1)
        self.assertEqual(while_open, {"pumped": 2, "board": [1, 2, 3]})   # updates go on meanwhile
        for name in ("dialog", "board"):
            self.assertEqual([n for who, n in seen if who == name], [1, 2, 3, 4], name)
        self.assertEqual([n for who, n in seen if who == "gone"], [1, 2, 3])
        self.assertEqual(err.getvalue().count("RuntimeError: broken subscriber"), 4)   # one per event
        self.assertEqual(bus.pump(), 0)

    def test_nested_pump_leaves_the_newest_last(self):
        bus = EventBus()
        shown = []
        tokens = {}

        def first(event):
            if event.n == 1:
                bus.unsubscribe(tokens["later"])    # still owed event 1: skipped, not delivered late
                bus.pump()

        bus.subscribe(Ping, first)
        tokens["later"] = bus.subscribe(Ping, lambda e: shown.append(("later", e.n)))
        bus.subscribe(Ping, lambda e: shown.append(("board", e.n)))   # e.g. a window showing the board
        for n in (1, 2, 3):
            bus.post(Ping(n))
        self.assertEqual(bus.pump(), 1)
        self.assertEqual(shown, [("board", 1), ("board", 2), ("board", 3)])

    def test_post_from_many_threads(self):
        bus = EventBus()
        seen = []
        bus.subscribe(Ping, lambda e: seen.append((threading.current_thread().name, e.n)))

        def post_many(base):
            for n in range(100):
                bus.post(Ping(base + n))

        threads = [threading.Thread(target=post_many, args=(i * 1000,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(bus.pump(), 400)
        self.assertEqual(sorted(n for _, n in seen), sorted(i * 1000 + n for i in range(4) for n in range(100)))
        self.assertEqual({name for name, _ in seen}, {threading.current_thread().name})   # pumped here
        for i in range(4):     # each poster's events stay in its order
            mine = [n for _, n in seen if i * 1000 <= n < i * 1000 + 100]
            self.assertEqual(mine, sorted(mine))


if __name__ == "__main__":
    unittest.main()
