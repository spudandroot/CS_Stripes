"""The base every MapleJet module builds on: clocks, cancelling, the event bus and Msg.

Pure and standard library only. Everything that waits or reads the time takes a
Clock, so tests can swap in a FakeClock and run the same code without real waits.
Worker threads hand results to the Tk thread only through EventBus.post().
A Msg is a piece of UI or log text kept in English; the UI translates it with tr().
"""

from __future__ import annotations

import collections
import math
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

# A manual-mode FakeClock sleeper gives up after this many real seconds without an advance()
_NOBODY_ADVANCED_S = 10.0
# How often a manual-mode sleeper looks at its cancel token (real seconds)
_CANCEL_POLL_S = 0.01
# FakeClock keeps monotonic() to whole nanoseconds, so 600 x advance(0.1) is exactly 60 s
_FAKE_DIGITS = 9
# A FakeClock sleeper this close to its deadline (fake seconds) is due. Float sums, the test's
# steps or the code's own "monotonic() + s", can fall a hair short of each other.
_DUE_WITHIN_S = 1e-6


# --------------------------------------------------------------------------
# Cancelling
# --------------------------------------------------------------------------

class Cancelled(Exception):
    """Raised by CancelToken.check() once cancel() has been called."""


class CancelToken:
    """Set once to ask a worker to stop. Checked between steps and passed to every sleep."""

    def __init__(self):
        self.event = threading.Event()

    def cancel(self):
        self.event.set()

    @property
    def cancelled(self):
        return self.event.is_set()

    def check(self):
        if self.event.is_set():
            raise Cancelled()

    def wait(self, seconds):
        """Wait in real time; True if cancelled meanwhile (or already)."""
        return self.event.wait(max(0.0, seconds))


# --------------------------------------------------------------------------
# Clocks
# --------------------------------------------------------------------------

class Clock:
    """Wall time, monotonic time and sleeping. Every hx_ timer goes through one."""

    def time(self):
        """This PC's wall clock, epoch seconds (like time.time())."""
        raise NotImplementedError

    def monotonic(self):
        """Every interval, timeout and staleness check uses this."""
        raise NotImplementedError

    def now(self):
        """Local naive datetime of self.time(), by this PC's time-zone rules."""
        t = self.time()
        whole = math.floor(t)
        micro = min(999_999, int(round((t - whole) * 1_000_000)))
        return datetime(*time.localtime(whole)[:6], microsecond=micro)

    def sleep(self, seconds, cancel=None):
        """True when the time passed; False as soon as cancel fires (at once if already cancelled)."""
        raise NotImplementedError


class SystemClock(Clock):
    """The real clock."""

    def time(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds, cancel=None):
        seconds = max(0.0, seconds)
        if cancel is None:
            time.sleep(seconds)
            return True
        return not cancel.event.wait(seconds)


SYSTEM_CLOCK = SystemClock()


class FakeClock(Clock):
    """Deterministic time for tests.

    auto_advance=True: sleep(s) moves both clocks on by s and returns at once, for
    single-threaded logic tests. auto_advance=False: sleep(s) blocks until a test calls
    advance() past its deadline, or the cancel token fires.

    Float sums drift, so monotonic() is kept to whole nanoseconds and a sleeper within a
    microsecond of its deadline counts as due: advancing a sleeper's time in small steps
    (600 x 0.1 s) wakes it. The clock then moves onto that deadline exactly, so the
    sleeper's own "monotonic() >= deadline" check agrees with the wake-up.
    """

    def __init__(self, start=1_793_275_200.0, *, auto_advance=True):
        # start = 29 Oct 2026 12:00 UTC
        self.auto_advance = auto_advance
        self._mono = 1000.0
        # time() is _wall_base when monotonic() was _mono_base: worked out on each call, so
        # the wall clock never drifts from monotonic() however many steps are added
        self._wall_base = float(start)
        self._mono_base = self._mono
        self._cond = threading.Condition()
        self._deadlines = []        # monotonic() deadline of each thread blocked in sleep()
        self._last_advance = time.monotonic()   # real time of the last advance(), for the watchdog

    def time(self):
        with self._cond:
            return self._wall_base + (self._mono - self._mono_base)

    def monotonic(self):
        with self._cond:
            return self._mono

    def advance(self, seconds):
        """Move time() and monotonic() on together and wake every sleeper that is due."""
        if seconds < 0:
            raise ValueError("FakeClock can't go backwards")
        with self._cond:
            self._move_to(round(self._mono + seconds, _FAKE_DIGITS))

    def set_wall(self, epoch):
        """Move time() only: a PC clock jump. Sleepers follow monotonic(), so none wakes."""
        with self._cond:
            self._wall_base = float(epoch)
            self._mono_base = self._mono

    def sleepers(self):
        """Threads blocked in sleep() right now (manual mode)."""
        with self._cond:
            return len(self._deadlines)

    def sleep(self, seconds, cancel=None):
        seconds = max(0.0, seconds)
        if cancel is not None and cancel.cancelled:
            return False
        started = time.monotonic()
        with self._cond:
            deadline = self._mono + seconds
            if self.auto_advance:
                # At least the exact sum, so the caller's own "monotonic() + s" has passed too
                self._move_to(max(round(deadline, _FAKE_DIGITS), deadline))
                return True
            if deadline <= self._mono + _DUE_WITHIN_S:
                # Nothing left but float noise: count it as reached instead of waiting for an
                # advance() that the test thinks it has already made
                if deadline > self._mono:
                    self._move_to(deadline, advanced=False)
                return True
            self._deadlines.append(deadline)
            try:
                while self._mono < deadline:
                    if cancel is not None and cancel.cancelled:
                        return False
                    if time.monotonic() - max(started, self._last_advance) >= _NOBODY_ADVANCED_S:
                        raise RuntimeError("FakeClock: nobody advanced the clock")
                    # Short waits, so a cancel (which can't notify this condition) is seen quickly
                    self._cond.wait(_CANCEL_POLL_S if cancel is not None else 0.05)
                return True
            finally:
                self._deadlines.remove(deadline)

    def _move_to(self, mono, advanced=True):
        """Lock held: move both clocks on to mono (never back) and wake the sleepers due.
        A sleeper due within _DUE_WITHIN_S takes the clock onto its deadline exactly.
        advanced=False (a sleeper settling float noise) doesn't count for the watchdog."""
        due = [d for d in self._deadlines if d <= mono + _DUE_WITHIN_S]
        self._mono = max([self._mono, mono] + due)
        if advanced:
            self._last_advance = time.monotonic()
        self._cond.notify_all()


# --------------------------------------------------------------------------
# Text kept in English
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Msg:
    """A piece of UI or log text kept in English: text is marked with N_() where the Msg is made,
    values are filled in by tr() in the UI (or by english() in logs). Values must never contain
    bag text: bag text is shown in its own widgets."""
    text: str
    values: dict = field(default_factory=dict)       # str, int, float or a nested Msg

    def __hash__(self):
        # values is a dict, so hash the text only: equal Msgs still hash alike
        return hash(self.text)

    def english(self):
        """The text in English with its values filled in (nested Msgs in English too)."""
        if not self.values:
            return self.text
        values = {k: v.english() if isinstance(v, Msg) else v for k, v in self.values.items()}
        try:
            return self.text.format(**values)
        except (KeyError, IndexError, ValueError, AttributeError):
            # A badly made Msg must never stop a log or an error from being shown
            return self.text

    def to_json(self):
        return {"text": self.text,
                "values": {k: v.to_json() if isinstance(v, Msg) else v for k, v in self.values.items()}}

    @staticmethod
    def from_json(d):
        """A value that is a dict with "text" becomes a nested Msg. Raises ValueError for a wrong shape."""
        if not isinstance(d, dict) or not isinstance(d.get("text"), str):
            raise ValueError("Msg: expected an object with a text")
        values = d.get("values", {})
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValueError("Msg.values: expected an object")
        return Msg(d["text"], {k: Msg.from_json(v) if isinstance(v, dict) and "text" in v else v
                               for k, v in values.items()})


# --------------------------------------------------------------------------
# Event bus
# --------------------------------------------------------------------------

class EventBus:
    """Worker threads post events; the Tk thread pumps them to subscribers. Never blocks a poster."""

    def __init__(self):
        self._queue = queue.SimpleQueue()
        self._subscribers = {}      # token -> (event type, callback), in subscription order
        self._next_token = 1
        # (event, token, callback) still to run for events already taken off the queue
        self._deliveries = collections.deque()

    def post(self, event):
        """Any thread."""
        self._queue.put(event)

    def subscribe(self, event_type, callback: Callable[[object], None]):
        """Tk thread: callback(event) for every event that isinstance(event, event_type). Returns a token."""
        token = self._next_token
        self._next_token += 1
        self._subscribers[token] = (event_type, callback)
        return token

    def unsubscribe(self, token):
        """Tk thread; safe inside a callback. An unknown token is ignored."""
        self._subscribers.pop(token, None)

    def pump(self, max_events=1000):
        """Tk thread: deliver up to max_events queued events in order; returns how many.
        A callback that raises is printed with its traceback and the pump carries on.

        A callback may run pump() again (a modal dialog runs the Tk loop, and the 100 ms pump
        with it). That pump first finishes the event the outer one is part-way through, so
        every subscriber still sees the events in the order they were posted."""
        taken = 0
        while True:
            while self._deliveries:
                event, token, callback = self._deliveries.popleft()
                if token not in self._subscribers:
                    continue    # unsubscribed since the event was taken
                try:
                    callback(event)
                except Exception:
                    traceback.print_exc()
            if taken >= max_events:
                break
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            taken += 1
            # The subscribers as they are now: one that subscribes later gets the next event
            self._deliveries.extend((event, token, callback)
                                    for token, (event_type, callback) in self._subscribers.items()
                                    if isinstance(event, event_type))
        return taken
