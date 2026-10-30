"""The base every MapleJet module builds on: clocks, cancelling, the event bus and Msg.

Pure and standard library only. Everything that waits or reads the time takes a
Clock, so tests can swap in a FakeClock and run the same code without real waits.
Worker threads hand results to the Tk thread only through EventBus.post().
A Msg is a piece of UI or log text kept in English; the UI translates it with tr().
"""

from __future__ import annotations

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
    """

    def __init__(self, start=1_793_275_200.0, *, auto_advance=True):
        # start = 29 Oct 2026 12:00 UTC
        self.auto_advance = auto_advance
        self._wall = float(start)
        self._mono = 1000.0
        self._cond = threading.Condition()
        self._sleeping = 0
        self._last_advance = time.monotonic()   # real time of the last advance(), for the watchdog

    def time(self):
        with self._cond:
            return self._wall

    def monotonic(self):
        with self._cond:
            return self._mono

    def advance(self, seconds):
        """Move time() and monotonic() on together and wake every sleeper that is due."""
        if seconds < 0:
            raise ValueError("FakeClock can't go backwards")
        with self._cond:
            self._wall += seconds
            self._mono += seconds
            self._last_advance = time.monotonic()
            self._cond.notify_all()

    def set_wall(self, epoch):
        """Move time() only: a PC clock jump. Sleepers follow monotonic(), so none wakes."""
        with self._cond:
            self._wall = float(epoch)

    def sleepers(self):
        """Threads blocked in sleep() right now (manual mode)."""
        with self._cond:
            return self._sleeping

    def sleep(self, seconds, cancel=None):
        seconds = max(0.0, seconds)
        if cancel is not None and cancel.cancelled:
            return False
        if self.auto_advance:
            self.advance(seconds)
            return True
        started = time.monotonic()
        with self._cond:
            deadline = self._mono + seconds
            self._sleeping += 1
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
                self._sleeping -= 1


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
        A callback that raises is printed with its traceback and the pump carries on."""
        delivered = 0
        while delivered < max_events:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            delivered += 1
            for token, (event_type, callback) in list(self._subscribers.items()):
                if token not in self._subscribers:
                    continue    # unsubscribed by an earlier callback for this event
                if isinstance(event, event_type):
                    try:
                        callback(event)
                    except Exception:
                        traceback.print_exc()
        return delivered
