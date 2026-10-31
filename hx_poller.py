"""MapleJet poller and status watcher.

Every PC running Stripes runs one Poller, also while Stripes is hidden in the tray. Its
coordinator thread ('hx-poller') writes this PC's heartbeat, works out from every
live heartbeat which printers this PC polls (one assigned PC per printer across the
plant), and runs one worker thread ('hx-poll-<printer>') for each of them. A worker
reads the printer one request at a time, steps aside whenever a printer lock is
held, keeps its poll.json fresh, and turns each cycle's reading into the machine's
state and alerts with hx_state.judge. The 'hx-time' thread keeps the time server's
offset fresh, so no worker ever waits on the time server.

While the share is down, only PCs with the MapleJet monitor or the floor board open
keep checking, every standalone_poll_s, quick items only, results in memory.

The StatusWatcher ('hx-watch') reads the status files every watch_s, builds the
BoardView the UI shows, and writes this PC's acknowledgements.

Tests drive both without threads: run_once() makes one pass on the calling thread.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import random
import sys
import threading
import traceback
from datetime import timedelta

import hx_message
import hx_protocol
import hx_state
import hx_time
from hx_core import SYSTEM_CLOCK, CancelToken, Clock, Msg
from hx_model import (AMBER, GREY, RED, TIMINGS, Ack, AuthFailed, BadReply, BoardUpdate, BoardView, CommandRefused,
                      FormatTooNew, HxError, MachineSnapshot, NoReply, PollerUpdate, PollStatus, PrinterBusy,
                      RunningMessage, ShareError, ShareUnavailable, Timings, WebReadFailed, WrongPrinter, same_user,
                      to_json)
from hx_share import AlarmMarker, CounterWatch, ReachMemory, assign, settle_s, takeover_after_s
from hx_state import Reading
from i18n import N_, add_spanish

FOREIGN_MIN = 3                # nonsense or busy replies ...
FOREIGN_WINDOW_S = 600.0       # ... within this long raise foreign_client
FOREIGN_CLEAR_S = 1800.0       # cleared after this long without any
MAX_BAGS_PER_S = 5.0           # a bag counter may rise by at most this per second (+10)
DAILY_S = 86_400.0             # daily checks
DST_KEEP_S = 86_400.0          # a pending daylight-saving clock set is kept this long
ACK_RETRY_S = 600.0            # the watcher retries a failed ack write this long
NOT_MONITORED_ALARM_S = 120.0  # a machine that was printing rings after this long NOT MONITORED (states.md §7 item 7)
UNCHECKED_LOADS = 2            # failed running-message Loads in a row before running_unchecked ...
UNCHECKED_SLOW_SETS = 3        # ... or this many poll_slow_s without a successful one (§6.5)
UNCHECKED_CONFIRMS = 3         # ... or this many failed confirming Loads in a row (owner decision, 31 Oct)
PRIN_UNREAD_LIMIT = 3          # cycles in a row without a good PRIN value before printing counts as unknown (§6.6)
DECLINE_MAX_S = 900.0          # a stuck-worker decline doubles from lock_stale_s up to this (§6.2 item 3.0) ...
DECLINE_MEMORY_S = 3600.0      # ... each time the printer is fenced again within this of the last fence

_GONE = object()               # the watcher's marker read: the share went away

EXTRA_LOAD_GAP_S = 30.0        # at most one MSGNAM-triggered Load per printer this often (§6.4 step 9)
DAILY_ITEMS = ("ECOMOD", "PLDEL", "MNPL", "PRPL", "PLVO", "PLWR", "NZRO")
ENCODER_DAILY_ITEMS = ("ENWHD", "ENPPR", "ENTPM")
SLOW_ITEMS = ("INKL", "INKT", "PHTP", "DATE", "TIME")

T_CLOCK_READS = N_("{machine}'s clock reads {printer_time} after it was set to {expected}.")

ES = {
    # send.md's text and Spanish, used by the daylight-saving clock set (states.md §6.11)
    "{machine}'s clock reads {printer_time} after it was set to {expected}.":
        "El reloj de {machine} marca {printer_time} después de ajustarlo a {expected}.",
}
add_spanish(ES)

_MISSING = object()            # a request that gave no good value (None is a valid MSGNAM or INKT)
_DUE_S = 1e-6                  # a wake-up this close to its time is due
# Standalone checks are standalone_poll_s (30 s) apart, so one check that gets no reply (each read
# already retried once) and its second reading a second later stand for share mode's three failed
# cycles (§6.6): the printer counts as not answering at once instead of after a minute and a half,
# and a first check that never got a reply is never judged green.
_STANDALONE_UNREACHABLE_AFTER = 1


def dst_change_id(at: float, new_offset_s: float, *, offset_fn) -> str:
    """The id of a daylight-saving change, the same on every PC: "20261101-18000"."""
    local = hx_time.to_local(at, offset_fn=offset_fn)
    return f"{local:%Y%m%d}{'+' if new_offset_s >= 0 else '-'}{abs(int(new_offset_s))}"


def _nearest_day(seconds):
    # A DATE and TIME pair read across midnight is a day out
    if seconds > 43_200:
        return seconds - 86_400
    if seconds < -43_200:
        return seconds + 86_400
    return seconds


def _one_line(e):
    return (f"{type(e).__name__}: {e}").splitlines()[0][:500]


class _AlwaysReach(ReachMemory):
    """Tests without a LocalStore: a reach memory that can always poll."""

    def can_poll(self):
        return True


class _Paused(Exception):
    """A fresh lock: no request; write P with paused_for_lock."""

    def __init__(self, token):
        super().__init__(token)
        self.token = token


class _NoMore(Exception):
    """No further request this cycle; what was read is still judged."""


class _Abandon(Exception):
    """End the cycle at once without judging (stop, handover, or no lock check possible)."""


class _ShareStop(Exception):
    """§6.13: this worker's share operations failed for stale_s / 2: stop share-mode polling."""


def _printers(data):
    """(printer, machine, line) for every active printer with a token, of the active machines of active lines."""
    out, seen = [], set()
    lines = sorted((ln for ln in data.lines.values() if ln.active), key=lambda ln: (hx_state.natural_key(ln.name), ln.id))
    for line in lines:
        for machine in data.line_machines(line.id):
            printer = data.printers.get(machine.printer_id)
            if printer is None or not printer.active or not printer.token or printer.id in seen:
                continue
            seen.add(printer.id)
            out.append((printer, machine, line))
    return out


def _running_message(design):
    bmp = design.preview_bmp or b""
    return RunningMessage(slot=design.slot, name=design.name or "", fingerprint=hx_message.fingerprint(design),
                          origin_line=hx_message.origin_from_design(design), read_at=design.read_at,
                          preview_sha256=hashlib.sha256(bmp).hexdigest() if bmp else "")


# --------------------------------------------------------------------------
# One cycle's requests
# --------------------------------------------------------------------------

class _Cycle:
    """The requests of one reading: the reply bookkeeping of §6.6."""

    def __init__(self, client, failures):
        self.client = client
        self.failures = failures
        self.replied = False
        self.stopped = False
        self.loaded = False          # a running-message Load was made
        self.load_ok = False         # ... and succeeded
        self.load_failed = False     # ... and failed: no reply, busy, or a reply that can't be used
        self.busy_rose = False       # the last call got a busy reply from the printer
        self.uid_read = False        # an HXUI value was read
        self.login_done = False      # a login got a reply (accepted or refused)


# --------------------------------------------------------------------------
# A share-mode worker (§6.3, §6.4)
# --------------------------------------------------------------------------

class _Worker:
    """Polls one printer for the coordinator. tick() is one cycle; the thread runs it every poll_fast_s."""

    def __init__(self, poller, printer_id, machine_id, reason, standalone=None):
        self.poller = poller
        self.standalone_seen = standalone   # this PC's last standalone status, when the share just came back
        self.store = poller.store
        self.clock = poller.clock
        self.timings = poller.timings
        self.key = poller.identity.key
        self.printer_id = printer_id
        self.machine_id = machine_id
        self.reason = reason
        self.thread = None
        self.wake = CancelToken()
        self.handover_to = ""        # rank 0's key, when the coordinator asked for a handover
        self.stop_reason = ""        # "stop" | "share", when the coordinator asked to stop
        self.done = False
        self.next_wake = self.clock.monotonic()
        self.p = None                # P as last written
        self.p_ok = None             # clock.monotonic() just before the last good write_poll
        self.counter = 0
        self.started = False
        self.settle_from = None
        self.rank_checked = False
        self.rival = ""              # a live PC ranked below this one that was seen writing P (a start race)
        self.fail_since = {"write": None, "lock": None}   # §6.13: when P writes / lock reads started failing
        self.lock_seen = False
        self.need_hxui = True        # first cycle after a start, a pause or being unreachable
        self.need_load = True        # a Load is required this cycle
        self.retry_load = False      # a Load this cycle, not a required one: a failed Load's retry (§6.4)
        self.r_stale = True          # R may no longer describe the printer until a judgement commits
        self.last_slow = None
        self.load_ok_mono = None
        self.loads_failed = 0
        self.confirms_failed = 0     # confirming Loads (second readings, §3.8) that failed in a row (§6.5)
        self.last_extra_load = None
        self.failures = 0
        self.backoff_i = 0
        self.next_attempt = None
        self.problem = ""
        self.samples = []
        self.started_mono = None
        self.inherited_bags = 0
        self.events = []
        self.foreign = False
        self.last_event = None
        self.daily_work = None
        self.clock_source = ""
        self.previews = {}
        self.waiting = {}            # alert id -> waiting list, handed over by the coordinator (§6.15)
        self.dst = {}
        self.reread_seen = ""
        self.running_ok_at = None
        self.last_answer_at = None
        self.acks_logged = set()
        self.fail_end = None         # clock.monotonic() when the last failed request ended
        self.replied = False         # a request got a reply this tick
        self.informed = False        # this tick learnt something: a good value, a finding, or no reply (§6.14 judged)
        self.prin_unread = 0         # cycles in a row whose PRIN read got no good value (§6.6)
        self.p_read = None           # clock.monotonic() just before the last rival check's read of P
        self.superseded = False      # a write found a newer P by another PC: the next cycle's rival check decides
        self.write_failed = False    # the last write_poll failed or raised
        self.fenced = False          # the coordinator gave up on this worker (stuck): it must never act again

    # ---- control from the coordinator (any thread)

    def request_handover(self, key):
        if not self.handover_to:
            self.handover_to = key
            self.wake.cancel()

    def request_stop(self, reason):
        if not self.stop_reason:
            self.stop_reason = reason
            self.wake.cancel()

    def set_waiting(self, alert_id, names):
        self.waiting = {**self.waiting, alert_id: list(names)}

    def fence(self):
        """The coordinator's watchdog found no progress (states.md §6.2 item 3.0): from now on this worker
        sends nothing and writes no P; its next tick ends it."""
        self.fenced = True
        self.wake.cancel()

    def wants_out(self):
        return bool(self.fenced or self.handover_to or self.stop_reason or self.poller._cancel.cancelled)

    # ---- the thread

    def run(self):
        while not self.done:
            try:
                self.tick()
            except Exception:       # tick() catches everything itself; this thread must never die anyway
                traceback.print_exc()
                self.next_wake = self.clock.monotonic() + self.timings.poll_fast_s
            if self.done:
                break
            wait = self.next_wake - self.clock.monotonic()
            if wait > 0 and not self.wants_out():
                wake = self.wake
                self.clock.sleep(wait, wake)
                if wake.cancelled:
                    self.wake = CancelToken()

    # ---- one tick

    def tick(self):
        if self.fenced:
            self.done = True        # given up on by the coordinator: no request, no write
            return
        now = self.clock.monotonic()
        self.next_wake = now + self.timings.poll_fast_s
        self.replied = self.informed = False
        try:
            try:
                if not self.started:
                    self._start()
                    return
                if self.wants_out():
                    self._leave()
                    return
                if now - self.settle_from < settle_s(self.timings):
                    # Settle: keep P fresh, no request yet. P is read before each write, so a write by
                    # another PC that starts at about the same time is never overwritten unseen
                    if self._rival_check(self.poller.live):
                        self._write_status()
                    if not self.done:
                        self.next_wake = min(self.next_wake, self.settle_from + settle_s(self.timings))
                    return
                self._cycle()
            except _Abandon:
                if self.fenced:
                    self.done = True
                elif self.wants_out():
                    self._leave()
            except _ShareStop:
                self._share_stop()
            except HxError as e:    # expected, but not handled where it happened: carry on at the next tick
                print(f"hx-poll-{self.printer_id}: {e}", file=sys.stderr)
        except Exception as e:      # a bug, also one in the handlers above: red internal_error, carry on at the
            traceback.print_exc()   # next tick (architecture §8.1)
            self._internal_error(e)
        if self.next_attempt is not None and not self.done:
            self.next_wake = min(self.next_wake, max(self.next_attempt, now))

    # ---- logging

    def _log(self, entry):
        data = self.poller.data.current
        machine = data.machines.get(self.machine_id) if data is not None else None
        entry = {"machine_id": self.machine_id, "printer_id": self.printer_id,
                 "line_id": machine.line_id if machine is not None else "", **entry}
        self.poller._log(entry)

    # ---- P writes

    def _next_counter(self):
        self.counter = max(self.counter, self.poller._max_counter.get(self.machine_id, 0)) + 1
        return self.counter

    def _share_trouble(self, kind="write"):
        """A P write or lock read failed, or took longer than stale_s / 4 (§6.13)."""
        now = self.clock.monotonic()
        if self.fail_since[kind] is None:
            self.fail_since[kind] = now
        if now - self.fail_since[kind] >= self.timings.stale_s / 2:
            raise _ShareStop()

    def _share_fine(self, kind):
        self.fail_since[kind] = None

    def _superseded(self):
        """§6.4 step 7.2, for every write: once this worker's last good P write is stale_s / 2 old (the PC
        slept, or the worker was stuck), another PC may have taken over meanwhile. P is read first, and a
        newer P by another PC, a yield to this one included, is never written over: _Abandon, and the next
        cycle's rival check adopts it, waits or yields."""
        now = self.clock.monotonic()
        if self.p_ok is None or now - self.p_ok < self.timings.stale_s / 2:
            return
        if self.p_read is not None and self.p_read >= self.p_ok and now - self.p_read < self.timings.stale_s / 4:
            return                  # a rival check read P just now (_still_alone): no second read on a slow share
        try:
            found = self.store.read_poll(self.machine_id)
        except FormatTooNew:
            self.done = True
            self._log({"type": "polling_stopped", "reason": "rank", "yielded_to": ""})
            raise _Abandon() from None
        except ShareUnavailable:
            self._share_trouble()
            raise _Abandon() from None
        except (ShareError, ValueError):
            return
        if found is None or found.machine_id != self.machine_id:
            return
        self.poller._note_counter(self.machine_id, found.counter)
        if found.poller and found.poller != self.key and found.counter > self.p.counter:
            self.superseded = True
            raise _Abandon()

    def _write(self, status):
        """One write_poll; True when it succeeded. A failure (or a slow success) counts for §6.13."""
        self._superseded()
        waiting = self.waiting
        if waiting:
            status = dataclasses.replace(status, alerts=[
                dataclasses.replace(a, waiting=list(waiting[a.id])) if a.id in waiting else a for a in status.alerts])
        for attempt in (1, 2):
            status = dataclasses.replace(status, counter=self._next_counter(), poller=self.key)
            if self.fenced:
                raise _Abandon()
            t0 = self.clock.monotonic()
            self.write_failed = True    # until it returns: a bug raised from it counts too
            try:
                self.store.write_poll(status)
            except ValueError:
                # Another read through this store raised its counter floor meanwhile: once more above it
                if attempt == 2:
                    raise
                try:
                    found = self.store.read_poll(self.machine_id)
                except (ShareError, FormatTooNew):
                    found = None
                if found is not None:
                    self.poller._note_counter(self.machine_id, found.counter)
                continue
            except (ShareUnavailable, ShareError):
                self._share_trouble()
                return False
            break
        took = self.clock.monotonic() - t0
        self.write_failed = False
        self.p = dataclasses.replace(status, written_at=self.clock.time())
        self.p_ok = t0
        self.poller._note_written(self.p)
        if took >= self.timings.stale_s / 2:
            # It landed, but so late that another PC may have taken over meanwhile, and this write may
            # have replaced its P: it proves nothing about now (§6.4 step 7.2). Stop share-mode polling;
            # the rank and rival rules decide afresh
            raise _ShareStop()
        if took > self.timings.stale_s / 4:
            self._share_trouble("write")
        else:
            self._share_fine("write")
        return True

    def _write_status(self, **changes):
        """P as last written with changes, counter + 1 and the current dst."""
        changes.setdefault("dst", self.dst)
        return self._write(dataclasses.replace(self.p, **changes))

    # ---- starting (§6.3 step 1)

    def _start(self):
        store, t = self.store, self.timings
        try:
            found = store.read_poll(self.machine_id)
        except FormatTooNew:
            self.done = True        # a newer Stripes owns this file: never overwrite it
            self.poller._note_progress(self.printer_id)
            return
        except (ShareUnavailable, ShareError):
            self.done = True
            return
        age = store.poll_age(self.machine_id) or 0.0
        if found is not None:
            self.poller._note_counter(self.machine_id, found.counter)
            if (found.poller and found.poller != self.key and found.yielded_to != self.key and age < t.stale_s
                    and found.poller not in {hb.key for hb in self.poller.live}):
                # The invariant (storage.md §12.6 rule 1) on the file as it is now: a PC this one doesn't
                # see as live wrote it since the coordinator's read (a slow share, or a new file the share
                # hid from this PC for a moment), so it polls or is starting too, and no rank check can
                # settle it. Don't start; the next pass decides again. Between two PCs that both see each
                # other the rank check settles a start race (§6.3 step 2)
                self.done = True
                self.poller._note_progress(self.printer_id)     # it stood aside on purpose (§6.2 item 3.0)
                return
            base = found
        else:
            base = PollStatus(machine_id=self.machine_id, printer_id=self.printer_id, poller=self.key)
        bags = 0 if age >= t.silent_window_s else base.bags_recent
        start = dataclasses.replace(base, printer_id=self.printer_id, paused_for_lock="", yielded_to="",
                                    standalone=False, bags_recent=bags)
        seen = self.standalone_seen
        if (found is not None and found.running is not None and seen is not None and seen.message_name is not None
                and hx_state.norm_name(seen.message_name) != hx_state.norm_name(found.running.name)):
            # This PC's standalone checks read another message name than R's while the share was down:
            # R no longer says what prints, so it is unknown until this worker's first Load (a known drift
            # keeps its alert, C11e), never shown as printing again
            start = dataclasses.replace(start, running=None)
        # The long-gap rule judges by P's judged counter: bookkeeping writes (a start, a settle, a yield)
        # move the counter and written_at without reading the printer (§6.3 step 1)
        if found is not None and found.judged is not None:
            gap = self.poller._judged_age(found) or 0.0
            gap_at = found.judged_at
        else:
            gap, gap_at = age, (found.written_at if found is not None else 0.0)
        if found is not None and found.state is not None and found.state.colour not in (RED, AMBER) and (
                gap >= t.not_monitored_s or self.clock.time() - gap_at >= t.not_monitored_s):
            # Nobody judged this printer for long (a share outage, every PC closed overnight): the copied
            # state, under a counter that moves again, would show as just checked. Until this worker's
            # first judgement it is "Checking...", or the red or amber this PC's standalone checks found
            # (only share mode clears those). A red or amber in P is kept as it is (§6.3 step 1;
            # owner-approved divergence, 31 Oct). The facts stay, so judge still needs its evidence
            seen_state = seen.state if seen is not None else None
            if seen_state is not None and seen_state.colour in (RED, AMBER):
                state = seen_state
            else:
                state = MachineSnapshot(code="checking", colour=GREY, headline=Msg(hx_state.STATE_TEXTS["checking"]),
                                        since=self.clock.time())
            start = dataclasses.replace(start, state=state)
        self.counter = max(self.counter, base.counter)
        self.dst = dict(start.dst or {})
        self.p = start
        moment = self.clock.monotonic()
        try:
            ok = self._write(start)
        except _ShareStop:
            ok = False
        if not ok:
            self.done = True
            return
        now = self.clock.monotonic()
        self.started = True
        self.settle_from = now
        self.started_mono = now
        self.load_ok_mono = moment
        self.loads_failed = UNCHECKED_LOADS if start.running_unchecked else 0
        self.prin_unread = PRIN_UNREAD_LIMIT if start.printing_unchecked else 0
        self.failures = start.failures if not start.reachable else 0
        self.problem = start.problem
        self.foreign = start.foreign_client
        self.last_event = now if self.foreign else None
        self.inherited_bags = start.bags_recent or 0
        self.reread_seen = start.reread_seen
        self.running_ok_at = start.running_ok_at
        self.last_answer_at = start.last_answer_at
        self.next_wake = min(self.next_wake, now + settle_s(t))
        self._log({"type": "polling_started", "reason": self.reason, "counter": self.p.counter})

    # ---- stopping

    def _finish(self, reason, yielded_to):
        if self.rival:
            yielded_to = ""     # waiting for another PC that polls (_rival_check): its P is left alone
        if yielded_to:
            try:
                self._write_status(yielded_to=yielded_to, paused_for_lock="")
            except (_ShareStop, _Abandon):
                pass                # a newer P by another PC (or a stuck worker's fence): left alone
            except Exception:       # a bug in the write: stop all the same; P goes stale and is taken over
                traceback.print_exc()
        self.done = True
        self._standing_aside()      # it gave the printer away on purpose
        self._log({"type": "polling_stopped", "reason": reason, "yielded_to": yielded_to})

    def _leave(self):
        """The coordinator asked to stop or hand over, or the poller stops."""
        if self.fenced:
            self.done = True
            return
        if self.stop_reason == "share":
            self.done = True
            self._log({"type": "polling_stopped", "reason": "share", "yielded_to": ""})
            return
        if self.handover_to and not self.poller._cancel.cancelled and self.stop_reason != "stop":
            self._finish("handover", self.handover_to)
            return
        # A clean stop yields to the next-ranked live poller (storage.md §12.6 rule 5)
        order = assign(self.poller.printer_ids, self.poller.live).get(self.printer_id) or []
        others = [k for k in order if k != self.key]
        self._finish("stop", others[0] if others else "")

    def _standing_aside(self):
        """A worker that read P and stands aside for another PC is working, not stuck (§6.2 item 3.0), as
        long as its own last P write didn't fail: one whose every write fails must still be found."""
        if not self.fenced and not self.write_failed:
            self.poller._note_progress(self.printer_id)

    def _share_stop(self):
        self.done = True
        self._log({"type": "polling_stopped", "reason": "share", "yielded_to": ""})
        self.poller._suspend_share()

    def _internal_error(self, e):
        """A bug: log it and write P red. Never raises, so the worker always carries on (§6.4)."""
        try:
            self._log({"type": "internal_error", "error": _one_line(e), "traceback": traceback.format_exc()})
        except Exception:
            traceback.print_exc()
        if self.p is None:
            return
        old = self.p.state
        since = old.since if old is not None and old.code == "internal_error" else self.clock.time()
        state = MachineSnapshot(code="internal_error", colour=RED,
                                headline=Msg(hx_state.STATE_TEXTS["internal_error"]), since=since)
        try:
            self._write_status(state=state, paused_for_lock="")
        except _ShareStop:
            self._share_stop()
        except _Abandon:
            pass                    # a newer P by another PC: the next cycle's rival check decides
        except Exception:
            # P can't be written either: the next tick tries again. Meanwhile p_ok stands still, so no
            # request fits its deadline after stale_s / 2 (§6.4 step 7.2), and another PC can take over
            traceback.print_exc()

    # ---- the lock (§6.4 step 3, step 7.1)

    def _lock_check(self):
        """None, or the stale lock to poll past; _Paused for a fresh one."""
        store, pid = self.store, self.printer_id
        if pid in store.local_locks:
            token = "local"
            try:
                found = store.read_lock(pid)
                if found is not None:
                    token = found.token
            except (ShareError, ValueError):
                pass
            raise _Paused(token)
        t0 = self.clock.monotonic()
        try:
            info = store.read_lock(pid)
        except ShareUnavailable:
            self._share_trouble("lock")
            raise _Abandon()
        except ShareError:
            self._share_trouble("lock")
            raise _NoMore()
        if self.clock.monotonic() - t0 > self.timings.stale_s / 4:
            self._share_trouble("lock")
            raise _NoMore()         # too slow to trust: no further request this cycle
        self._share_fine("lock")
        if info is None:
            return None
        age = store.lock_age(pid)
        if age is None or age < self.timings.lock_stale_s:
            raise _Paused(info.token)
        return info

    def _paused(self, token):
        self.lock_seen = True
        self.loads_failed = self.confirms_failed = 0
        # A paused write counts as judged (§6.14): the board shows the lock while it is fresh, and a long
        # Send doesn't show NOT MONITORED when it ends
        self._write_status(paused_for_lock=token, **self._judged_now())

    def _judged_now(self):
        return {"judged": (self.p.judged or 0) + 1, "judged_at": self.clock.time()}

    # ---- one request (§6.4 step 7)

    def _call(self, cyc, kind, fn):
        """Check the lock, keep P fresh, then send with the deadline. fn(end) makes the call."""
        if cyc.stopped:
            raise _NoMore()
        if self.wants_out():
            raise _Abandon()
        self._lock_check()
        # Right before the keep-fresh write and the request, so a pause anywhere before them (the lock
        # read included) is seen (§6.4 step 7.2)
        if not self._still_alone():
            raise _Abandon()
        t = self.timings
        budget = t.http_timeout_s if kind == "web" else hx_protocol.worst_case_s(kind, t)
        now = self.clock.monotonic()
        end = self.p_ok + t.stale_s / 2 + t.http_timeout_s
        # A web call may run until its deadline, so P is written right before it too
        if now - self.p_ok >= t.poll_fast_s or now + budget > end or (kind == "web" and now > self.p_ok):
            self._write_status()
            now = self.clock.monotonic()
            end = self.p_ok + t.stale_s / 2 + t.http_timeout_s
        if now + budget > end:
            if now - self.p_ok >= t.stale_s / 2:
                raise _ShareStop()  # P can't prove this worker is alive any more
            cyc.stopped = True
            raise _NoMore()
        client = cyc.client
        nonsense, busy = client.nonsense, client.busy_replies
        if self.fenced:
            raise _Abandon()
        try:
            return fn(end)
        finally:
            self._note_events(client.nonsense - nonsense + client.busy_replies - busy)
            cyc.busy_rose = client.busy_replies > busy

    def _still_alone(self):
        """False when this worker must neither send nor write P. Once its last good P write is stale_s / 2
        old (the PC slept or was paused mid-cycle), P no longer proves it alive and another PC may have
        taken over meanwhile: it reads P first, as at the start of a cycle (_rival_check), so it never
        writes over that PC's P or polls alongside it (§6.4 step 7.2)."""
        if self.clock.monotonic() - self.p_ok < self.timings.stale_s / 2:
            return True
        before = self.p
        if not self._rival_check(self.poller.live):
            return False
        # It adopted another PC's newer P (a yield back to this PC): this cycle's readings predate it, so the
        # cycle ends unjudged and the next one starts from that P
        return self.p is before

    def _replied(self, cyc, *, useful=True):
        """useful=False: a busy or nonsense reply, which tells nothing about the printer's state."""
        cyc.failures = 0
        cyc.replied = True
        self.replied = True
        self.informed = self.informed or useful
        self.backoff_i = 0
        self.last_answer_at = self.clock.time()

    def _no_reply(self, cyc):
        self.informed = True        # not answering is a finding too
        cyc.failures += 1
        cyc.stopped = True
        self.failures = cyc.failures
        self.next_attempt = None
        self.fail_end = self.clock.monotonic()

    def _read(self, cyc, tag):
        """One read: its value, or _MISSING. NoReply, AuthFailed and WrongPrinter end the requests."""
        try:
            value = self._call(cyc, "read", lambda end: cyc.client.read(tag, deadline=end))
        except NoReply:
            self._no_reply(cyc)
            raise _NoMore()
        except PrinterBusy:
            if cyc.busy_rose:
                self._replied(cyc, useful=False)    # the printer answered busy
            return _MISSING
        except BadReply:
            self._replied(cyc, useful=False)        # it answered nonsense, which the client threw away
            return _MISSING
        except AuthFailed:
            self._replied(cyc)
            self.problem = "login_failed"
            cyc.stopped = True
            raise _NoMore()
        except WrongPrinter:
            self._replied(cyc)
            self.problem = "wrong_printer"
            cyc.stopped = True
            raise _NoMore()
        self._replied(cyc)
        if self.problem == "login_failed":
            self.problem = ""
        return value

    def _hxui(self, cyc, printer):
        value = self._read(cyc, "HXUI")
        if value is _MISSING:
            return
        cyc.uid_read = True
        self.need_hxui = False
        if value != printer.uid:
            self.problem = "wrong_printer"
            cyc.stopped = True
            raise _NoMore()
        if self.problem == "wrong_printer":
            self.problem = ""

    def _prin(self, cyc, r):
        """PRIN into r.printing; a busy or nonsense reply counts towards PRIN_UNREAD_LIMIT (§6.6)."""
        value = self._read(cyc, "PRIN")
        if value is _MISSING:
            self.prin_unread += 1
            return False
        self.prin_unread = 0
        r.printing = bool(value)
        return True

    def _load(self, cyc, r, *, required):
        """The running message (slot 0) into r.running; a failed Load counts in the message check."""
        cyc.loaded = True
        try:
            design = self._call(cyc, "web", lambda end: cyc.client.load_slot(0, deadline=end))
        except (WebReadFailed, PrinterBusy, BadReply):
            self.loads_failed += 1
            cyc.load_failed = True
            if required:
                r.running_reset = True
            return
        except NoReply:
            self.loads_failed += 1
            cyc.load_failed = True
            if required:
                r.running_reset = True
            self._no_reply(cyc)
            raise _NoMore()
        except AuthFailed:
            self._replied(cyc)
            self.problem = "login_failed"
            cyc.stopped = True
            if required:
                r.running_reset = True
            raise _NoMore()
        self._replied(cyc)
        running = _running_message(design)
        r.running = running
        r.running_reset = False
        cyc.load_ok = True
        self.load_ok_mono = self.clock.monotonic()
        self.loads_failed = 0
        self.running_ok_at = self.clock.time()
        if design.preview_bmp:
            self.previews = {running.preview_sha256: design.preview_bmp,
                             **{k: v for k, v in list(self.previews.items())[-2:]}}

    # ---- events (§6.8), bags (§6.7), ink names, the clock

    def _note_events(self, n):
        if n > 0:
            now = self.clock.monotonic()
            self.events.extend([now] * n)
            self.last_event = now

    def _foreign_now(self):
        now = self.clock.monotonic()
        self.events = [t for t in self.events if now - t < FOREIGN_WINDOW_S]
        if len(self.events) >= FOREIGN_MIN:
            self.foreign = True
        elif self.foreign and (self.last_event is None or now - self.last_event >= FOREIGN_CLEAR_S):
            self.foreign = False
        return self.foreign

    def _bags(self, count):
        """(count or None when thrown away, bags within silent_window_s)."""
        now = self.clock.monotonic()
        window = self.timings.silent_window_s
        s = self.samples
        kept = count
        if s and count < s[-1][1]:
            s[:] = [(now, count)]               # a reset: no bags added
        elif s and count - s[-1][1] > MAX_BAGS_PER_S * (now - s[-1][0]) + 10:
            self._note_events(1)                # an unbelievable rise: thrown away
            kept = None
        else:
            s.append((now, count))
        while len(s) >= 2 and s[1][0] < now - window:
            s.pop(0)
        bags = sum(b[1] - a[1] for a, b in zip(s, s[1:]) if b[0] >= now - window)
        if now - self.started_mono < window:
            bags = max(bags, self.inherited_bags)
        return kept, bags

    def _ink_name(self, cyc, company):
        first = self._read(cyc, "INKT")
        if first is _MISSING or first is None:
            return None
        names = [ink.name for ink in company.inks]
        if not names or first in names:
            return first
        second = self._read(cyc, "INKT")       # not on the list: asked once more at once
        if second is not _MISSING and second in names:
            self._note_events(1)
            return second
        if second is not _MISSING and second != first:
            self._note_events(1)
        return None                             # an unknown ink: the previous name stays

    def _clock(self, cyc, r):
        date = self._read(cyc, "DATE")
        time_ = self._read(cyc, "TIME")
        if date is _MISSING or time_ is _MISSING:
            return
        t = self.poller.time.cached()
        try:
            printer = hx_protocol.clock_from(date, time_)
        except ValueError:
            return
        r.clock_offset_s = round(_nearest_day((printer - t.local).total_seconds()), 1)
        self.clock_source = t.source

    # ---- daily checks (§6.10)

    def _daily_due(self, p, machine, at):
        d = p.daily or {}
        if not d:
            return True
        when = d.get("at")
        if not isinstance(when, (int, float)) or isinstance(when, bool):
            return True
        if at - when >= DAILY_S or at - when < -300:
            return True
        return d.get("machine_revision") != machine.revision

    def _daily_item(self, cyc, r, p, machine):
        work = self.daily_work
        if work is None or work["revision"] != machine.revision:
            items = DAILY_ITEMS + (ENCODER_DAILY_ITEMS if machine.placement == "encoder" else ()) + ("fonts", "firmware")
            work = self.daily_work = {"revision": machine.revision, "items": items, "index": 0, "values": {},
                                      "errors": [], "fonts": None, "firmware": None, "firmware_read": False}
        item = work["items"][work["index"]]
        if item == "fonts":
            try:
                fonts = self._call(cyc, "web", lambda end: cyc.client.fonts(deadline=end))
                self._replied(cyc)
                work["fonts"] = list(fonts)
            except NoReply:
                self._no_reply(cyc)
                raise _NoMore()
            except (BadReply, WebReadFailed, PrinterBusy, AuthFailed):
                work["errors"].append("fonts")
        elif item == "firmware":
            try:
                work["firmware"] = self._call(cyc, "web", lambda end: cyc.client.firmware(deadline=end))
                work["firmware_read"] = True
                self._replied(cyc)
            except NoReply:
                self._no_reply(cyc)
                raise _NoMore()
            except (BadReply, WebReadFailed, PrinterBusy, AuthFailed):
                work["errors"].append("firmware")
        else:
            value = self._read(cyc, item)
            if value is _MISSING:
                work["errors"].append(item)
            else:
                work["values"][item] = value
        work["index"] += 1
        if work["index"] < len(work["items"]):
            return
        self.daily_work = None
        prev = p.daily or {}
        values = dict(prev.get("values") or {})
        values.update(work["values"])
        ref = dict(prev.get("encoder_ref") or {}) if machine.placement == "encoder" else {}
        if machine.placement == "encoder":
            record = {"ENWHD": int(round(machine.encoder_wheel_mm)) if machine.encoder_wheel_mm is not None else None,
                      "ENPPR": machine.encoder_ppr}
            for tag in ("ENWHD", "ENPPR"):
                if record[tag] is not None:
                    ref[tag] = record[tag]
                elif tag not in ref and values.get(tag) is not None:
                    ref[tag] = values[tag]  # the first daily check that read it
        r.daily = {"at": r.at, "by": self.key, "machine_revision": machine.revision, "values": values,
                   "encoder_ref": ref, "fonts": work["fonts"] if work["fonts"] is not None else prev.get("fonts"),
                   "firmware": work["firmware"] if work["firmware_read"] else prev.get("firmware"),
                   "errors": list(work["errors"])}

    # ---- the daylight-saving clock set (§6.11)

    def _dst_due(self, change):
        p = self.p
        return (change is not None and p.reachable and p.problem == "" and (p.dst or {}).get("change") != change[0]
                and (self.dst or {}).get("change") != change[0])

    def _clock_set(self, cyc, r, machine, change):
        change_id, old_s, new_s, change_at = change
        time_source = self.poller.time
        t = time_source.cached()
        d0 = self._read(cyc, "DATE")
        t0 = self._read(cyc, "TIME")
        if d0 is _MISSING or t0 is _MISSING:
            raise _NoMore()         # nothing recorded: the steps run again at the next cycle
        before = hx_protocol.clock_from(d0, t0)
        expected = time_source.now_from(t).local
        diff = _nearest_day((before - expected).total_seconds())
        record = {"change": change_id, "at": self.clock.time(), "ok": False, "skipped": False, "sent": False,
                  "by": self.key, "source": t.source}
        clock = {"sent": None, "before": before.strftime("%Y-%m-%d %H:%M:%S"), "after": None,
                 "difference_s": round(diff, 1), "source": t.source, "server": t.server,
                 "offset_s": round(t.offset_s, 3)}
        msg = None
        if abs(diff) <= self.timings.clock_warn_s:
            record.update(ok=True, skipped=True)    # already right: no TD
            r.clock_offset_s = round(diff, 1)
            self.dst = record
            self._log_dst(change, record, clock, msg)
            return
        # Record first: once a TD may have gone out, P already says so
        previous = self.dst
        first = dict(record, sent=True)
        if not self._still_alone():
            raise _Abandon()
        if not self._write_status(dst=first):
            raise _NoMore()
        self.dst = first
        local = time_source.now_from(t).local
        command = hx_message.clock_command(local)
        clock["sent"] = (local + timedelta(seconds=30)).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        try:
            self._call(cyc, "command", lambda end: cyc.client.command(command, deadline=end))
        except (_Paused, _NoMore, _Abandon, PrinterBusy):
            # Nothing was sent: the next P write puts dst back, so the steps run again
            self.dst = previous
            raise
        except _ShareStop:
            raise
        except (NoReply, CommandRefused, WrongPrinter, BadReply, AuthFailed) as e:
            if isinstance(e, NoReply):
                self._no_reply(cyc)
            else:
                self._replied(cyc)
            self.dst = dict(first, ok=False)
            self._log_dst(change, self.dst, clock, e.msg)
            raise _NoMore()
        self._replied(cyc)
        ok, after, msg = False, None, None
        try:
            for attempt in (1, 2):
                d1 = self._read(cyc, "DATE")
                t1 = self._read(cyc, "TIME")
                if d1 is _MISSING or t1 is _MISSING:
                    break
                after = hx_protocol.clock_from(d1, t1)
                expected = time_source.now_from(t).local
                diff = (after - expected).total_seconds()
                clock["after"] = after.strftime("%Y-%m-%d %H:%M:%S")
                clock["difference_s"] = round(diff, 1)
                r.clock_offset_s = round(_nearest_day(diff), 1)
                if abs(diff) <= hx_message.CLOCK_SET_TOLERANCE_S:
                    ok = True
                    break
        finally:
            if not ok:
                if after is not None:
                    msg = Msg(T_CLOCK_READS, {"machine": machine.name,
                                              "printer_time": after.strftime("%Y-%m-%d %H:%M:%S"),
                                              "expected": expected.strftime("%Y-%m-%d %H:%M")})
            self.dst = dict(first, ok=ok)
            self._log_dst(change, self.dst, clock, msg)

    def _log_dst(self, change, record, clock, msg):
        change_id, old_s, new_s, change_at = change
        self._log({"type": "dst_clock_set", "change": change_id, "change_at": change_at, "previous_offset_s": old_s,
                   "offset_s": new_s, "ok": bool(record.get("ok")), "skipped": bool(record.get("skipped")),
                   "msg": msg.to_json() if msg is not None else None, "clock": clock})

    # ---- the cycle (§6.4)

    def _cycle(self):
        store, t, poller = self.store, self.timings, self.poller
        data = poller.data.current
        machine = data.machines.get(self.machine_id) if data is not None else None
        printer = data.printers.get(self.printer_id) if data is not None else None
        line = data.lines.get(machine.line_id) if machine is not None else None
        if machine is None or printer is None or line is None:
            self._write_status()        # the coordinator stops this worker at its next pass
            return

        # 1. Still mine? Also before the first request when a write found a newer P by another PC
        # (_superseded): this cycle adopts it, waits or yields first (§6.4 step 7.2)
        order = assign(poller.printer_ids, poller.live).get(self.printer_id) or []
        if order and order[0] != self.key:
            self._finish("rank", order[0])
            return
        if (self.rank_checked or self.superseded) and not self._rival_check(poller.live):
            return

        # 2. E
        try:
            e = store.read_expected(self.machine_id)
        except FormatTooNew:
            state = MachineSnapshot(code="update_stripes", colour=GREY,
                                    headline=Msg(hx_state.STATE_TEXTS["update_stripes"]), since=self.clock.time())
            self._write_status(state=state, alerts=[], paused_for_lock="")
            return
        except ShareUnavailable:
            self._share_trouble()
            return
        except ShareError:
            e = store.cached_expected(self.machine_id)

        # 3. The lock
        lock = None
        try:
            lock = self._lock_check()
        except _Paused as paused:
            self._paused(paused.token)
            return
        except _NoMore:
            self._write_status(paused_for_lock="")
            return

        # The rank check from fresh heartbeats, right before this worker's first request (§6.3 step 2)
        if not self.rank_checked:
            try:
                live = store.live_heartbeats()
            except (ShareUnavailable, ShareError):
                self._share_trouble()
                return
            order = assign(poller.printer_ids, live).get(self.printer_id) or []
            if order and order[0] != self.key:
                self._finish("rank", order[0])
                return
            if not self._rival_check(live):
                return
            self.rank_checked = True

        p = self.p
        e_counter = e.counter if e is not None else 0
        if self.lock_seen or e_counter != p.expected_counter:
            # R may no longer describe the printer: load it, and don't judge with the old one
            self.need_load = self.r_stale = True
            self.need_hxui = self.need_hxui or self.lock_seen
            self.loads_failed = self.confirms_failed = 0
            self.lock_seen = False
        token = e.reread_token if e is not None else ""
        if token and token != self.reread_seen:
            self.need_load = self.r_stale = True
            self.reread_seen = token

        # 4. Acks
        acked = {}
        for a in p.alerts:
            if a.active and not a.acked_by:
                try:
                    ack = store.read_ack(a.id)
                except ShareUnavailable:
                    self._share_trouble()
                    return
                except (ShareError, ValueError):
                    ack = None
                if ack is not None:
                    acked[a.id] = ack
                    if a.id not in self.acks_logged:
                        self.acks_logged.add(a.id)
                        self._log({"type": "alert_acknowledged", "alert_id": a.id, "kind": a.kind, "by": ack.by,
                                   "computer": ack.computer, "at": ack.at})

        now = self.clock.monotonic()
        at = self.clock.time()
        unreachable = not p.reachable and self.failures >= t.unreachable_after

        # 5. Backoff
        if unreachable:
            if self.next_attempt is None:
                gap = t.backoff_s[min(self.backoff_i, len(t.backoff_s) - 1)]
                self.next_attempt = (self.fail_end if self.fail_end is not None else now) + gap
            if now < self.next_attempt - _DUE_S:
                r = Reading(at=at, reachable=False, failures=self.failures, acked=acked, problem=self.problem,
                            clock_source=self.clock_source)
                self._judge_and_write(r, e, lock, machine, line, data, None)
                self.next_wake = min(self.next_wake, self.next_attempt)
                return

        try:
            client = poller.clients.client(printer)
        except AuthFailed:
            self.problem = "login_failed"
            r = Reading(at=at, reachable=self.failures < t.unreachable_after, failures=self.failures, acked=acked,
                        problem=self.problem, clock_source=self.clock_source)
            self._judge_and_write(r, e, lock, machine, line, data, None)
            return

        # 6 and 7. Plan and read
        r = Reading(at=at, reachable=True, acked=acked)
        cyc = _Cycle(client, self.failures)
        slow_due = self.last_slow is None or now - self.last_slow >= t.poll_slow_s - _DUE_S
        change = poller._dst
        try:
            try:
                if unreachable:
                    self.backoff_i += 1
                    self.next_attempt = None
                    self._prin(cyc, r)                       # one attempt
                else:
                    if self.need_hxui or slow_due or self.problem:
                        self._hxui(cyc, printer)
                    self._prin(cyc, r)
                    if self._dst_due(change):
                        self._clock_set(cyc, r, machine, change)
                    else:
                        value = self._read(cyc, "MSGNAM")
                        if value is not _MISSING:
                            r.message_name = "" if value is None else hx_state.norm_name(value)
                        counter_tag = data.company.bag_counter
                        if counter_tag and counter_tag != "none":
                            value = self._read(cyc, counter_tag)
                            if value is not _MISSING and isinstance(value, int):
                                r.bag_count, r.bags_recent = self._bags(value)
                        if slow_due:
                            self.last_slow = now
                            value = self._read(cyc, "INKL")
                            if value is not _MISSING:
                                r.ink_level = value
                            r.ink_name = self._ink_name(cyc, data.company)
                            value = self._read(cyc, "PHTP")
                            if value is not _MISSING:
                                r.head_temp_c = value
                            self._clock(cyc, r)
                        if self._daily_due(p, machine, at):
                            self._daily_item(cyc, r, p, machine)
                        if self.need_load or self.retry_load or slow_due:
                            required = self.need_load
                            self.need_load = self.retry_load = False
                            self._load(cyc, r, required=required)
            except _NoMore:
                pass
        except _Paused as paused:
            self._paused(paused.token)
            return
        if unreachable and cyc.replied:
            # Back after being unreachable: HXUI and the running message again, and no old R
            self.need_hxui = self.need_load = self.r_stale = True
        self._judge_and_write(r, e, lock, machine, line, data, cyc)

    def _rival_check(self, live):
        """True when this cycle may make requests (or, while settling, write P). Two PCs can both pass
        their rank check while they disagree about who is live (on a slow share each can see the
        other's heartbeat as stale), and architecture §7.3 rule 2 says two pollers never overlap even
        then. So before every settle write, right before its first request, at every cycle after it,
        and mid-cycle once its last P write is stale_s / 2 old (_still_alone), the worker reads P.
        Reading before each settle write matters: two starters a few seconds apart would otherwise each
        overwrite the other's writes and find only their own at the rank check. A write by another PC
        since this worker's own (and not a yield to this PC) means that PC polls, or is starting, too:
        - one this PC doesn't see as live is yielded to at once, without a request;
        - a live one that ranks above this PC (possible while settling, before the rank check): this
          PC hands over to rank 0 at once, without a request;
        - a live one ranks below this PC and yields at its own rank check. Until P shows that yield,
          or P has been quiet for stale_s, this worker makes no request and doesn't write P (P keeps
          one writer), and continues from what that PC wrote."""
        read_at = self.clock.monotonic()
        self.superseded = False
        try:
            found = self.store.read_poll(self.machine_id)
        except FormatTooNew:
            # A newer Stripes polls this printer: stop without overwriting its file
            self.done = True
            self.poller._note_progress(self.printer_id)
            self._log({"type": "polling_stopped", "reason": "rank", "yielded_to": ""})
            return False
        except ShareUnavailable:
            self._share_trouble()
            return False
        except (ShareError, ValueError):
            found = None
        if found is not None and found.machine_id != self.machine_id:
            found = None
        if found is not None:
            self.poller._note_counter(self.machine_id, found.counter)
        me = self.key
        age = self.store.poll_age(self.machine_id)
        quiet = age is not None and age >= self.timings.stale_s      # nobody has written P for stale_s
        live_keys = {hb.key for hb in live}
        if (found is not None and found.poller and found.poller != me and found.poller not in live_keys
                and self.p_ok is not None and self.p is not None and found.counter <= self.p.counter
                and self.clock.time() - found.written_at >= self.timings.stale_s / 2):
            # A late write by a superseded owner that isn't live: a share write it gave up on that landed
            # later, or a PC that paused inside its write. Its counter isn't above this worker's last write
            # (equal when it lands before this worker's second write: both counted on from the same P) and
            # it was made long ago, unlike a poller's that is starting or polling too (storage.md §12.6
            # rule 9). Neither adopted nor yielded to: this worker carries on, and its next write puts its
            # own P back
            self._log({"type": "late_write_ignored", "writer": found.poller, "counter": found.counter})
            found = None
        if (found is not None and found.poller and found.poller != me and found.yielded_to == me
                and self.p is not None and found.counter > self.p.counter and not self.rival):
            # Another PC polled meanwhile and yielded back: continue from its newer P (its state, alerts
            # and their ids, its clock set), never from this worker's older copy (§6.4 step 7.2)
            self._adopt(found)
        other = ""
        if found is not None and found.poller and found.poller != me and found.yielded_to != me and not quiet:
            other = found.poller
        if other:
            if other not in live_keys:
                self._adopt(found)      # the yield carries that PC's latest P, not this worker's older copy
                self._finish("rank", other)
                return False
            order = assign(self.poller.printer_ids, live).get(self.printer_id) or []
            if order and order[0] != me:
                self._finish("rank", order[0])
                return False
            self.rival = other
        if not self.rival:
            self.p_read = read_at       # P is this worker's (or was yielded to it): _superseded needn't read again
            return True
        if found is not None and found.poller == self.rival:
            self._adopt(found)
        if not other and (quiet or (found is not None and found.poller == self.rival and found.yielded_to == me)):
            self.rival = ""             # it yielded, or nobody is writing P any more: poll on
            self.p_read = read_at
            return True
        self._standing_aside()      # waiting for that PC to yield
        return False

    def _adopt(self, found):
        """Continue from P as another PC last wrote it, as a takeover does (§6.3 step 1): its state,
        alerts and facts, and its daylight-saving record, so a clock set it made is never repeated."""
        self.p = dataclasses.replace(found, printer_id=self.printer_id, paused_for_lock="", yielded_to="",
                                     standalone=False)
        self.counter = max(self.counter, found.counter)
        self.dst = dict(found.dst or {})
        self.reread_seen = found.reread_seen
        self.running_ok_at = found.running_ok_at
        self.last_answer_at = found.last_answer_at
        self.failures = found.failures if not found.reachable else 0
        self.problem = found.problem
        self.inherited_bags = max(self.inherited_bags, found.bags_recent or 0)
        self.loads_failed = UNCHECKED_LOADS if found.running_unchecked else 0
        self.prin_unread = PRIN_UNREAD_LIMIT if found.printing_unchecked else 0
        self.confirms_failed = 0
        self.need_hxui = self.need_load = self.r_stale = True

    def _finish_reading(self, r, cyc):
        t = self.timings
        if cyc is not None:
            self.failures = cyc.failures
        r.failures = self.failures
        r.reachable = self.failures < t.unreachable_after
        r.problem = self.problem
        r.clock_source = self.clock_source
        if cyc is not None:
            now = self.clock.monotonic()
            r.foreign_client = self._foreign_now()
            r.running_unchecked = (self.loads_failed >= UNCHECKED_LOADS
                                   or self.confirms_failed >= UNCHECKED_CONFIRMS
                                   or now - self.load_ok_mono >= UNCHECKED_SLOW_SETS * t.poll_slow_s)
            if not r.second:
                # A second reading sets it itself: from its own PRIN read, else prev's (_second_reading)
                r.printing_unchecked = self.prin_unread >= PRIN_UNREAD_LIMIT
            if r.bags_recent is None and self.samples:
                r.bags_recent = self._recent_bags()
            if self.r_stale and r.running is None:
                r.running_reset = True

    def _recent_bags(self):
        now = self.clock.monotonic()
        window = self.timings.silent_window_s
        s = self.samples
        bags = sum(b[1] - a[1] for a, b in zip(s, s[1:]) if b[0] >= now - window)
        if now - self.started_mono < window:
            bags = max(bags, self.inherited_bags)
        return bags

    def _judge_and_write(self, r, e, lock, machine, line, data, cyc):
        """Steps 8 to 13: judge, the extra Load, the second reading, then write and log."""
        t = self.timings
        self._finish_reading(r, cyc)
        j = hx_state.judge(self.p, r, e, lock, machine, line, data, t)
        load_failed = cyc is not None and cyc.load_failed      # a Load of this tick failed (§6.5)

        # 9. Load now: MSGNAM differs from the running message's name
        if (cyc is not None and j.read_running_now and not cyc.loaded
                and (self.last_extra_load is None or self.clock.monotonic() - self.last_extra_load >= EXTRA_LOAD_GAP_S)):
            if not self.clock.sleep(t.second_reading_s, self.poller._cancel):
                raise _Abandon()
            self.last_extra_load = self.clock.monotonic()
            r = dataclasses.replace(r, at=self.clock.time())
            extra = _Cycle(cyc.client, self.failures)
            try:
                self._load(extra, r, required=False)
            except _NoMore:
                pass
            except _Paused as paused:
                self._paused(paused.token)
                return
            cyc = extra
            load_failed = load_failed or extra.load_failed
            self._finish_reading(r, cyc)
            j = hx_state.judge(self.p, r, e, lock, machine, line, data, t)

        # 10. The second reading
        if j.recheck and cyc is None:
            # Nothing can be read again (a password that can't be unscrambled): it commits at once
            r = dataclasses.replace(r, second=True, pending_code=j.candidate.code, pending_alerts=j.candidate_alerts)
            j = hx_state.judge(self.p, r, e, lock, machine, line, data, t)
        elif j.recheck:
            confirming = "load" in j.recheck
            if not self.clock.sleep(t.second_reading_s, self.poller._cancel):
                raise _Abandon()
            r2 = dataclasses.replace(r, second=True, at=self.clock.time(), pending_code=j.candidate.code,
                                     pending_alerts=j.candidate_alerts, daily=dict(r.daily) if r.daily else r.daily)
            again = _Cycle(cyc.client, self.failures)
            try:
                self._second_reading(again, r2, j.recheck, data)
            except _Paused as paused:
                self._paused(paused.token)
                return
            # A confirming Load that failed (or was never made) is a failed message check that the
            # first Load, unconfirmed, can't hide (§6.5, owner decision 31 Oct)
            failed = confirming and not again.load_ok
            if failed:
                self.confirms_failed += 1
                load_failed = True
            self._finish_reading(r2, again)
            if (("uid" in j.recheck and not again.uid_read)
                    or ("login" in j.recheck and not again.login_done)):
                r2.problem = self.p.problem     # not read again: the second reading confirms nothing (§3.8)
            r, cyc = r2, again
            j = hx_state.judge(self.p, r, e, lock, machine, line, data, t)
            if failed and not j.committed and self.confirms_failed >= UNCHECKED_CONFIRMS:
                # The UNCHECKED_CONFIRMSth in a row: R is unknown and the message check failed. Neither
                # needs a second reading (§3.8 steps 3 and 4), so it commits amber message_unchecked
                # now, also when the first Load's candidate can't be confirmed
                j = hx_state.judge(self.p, dataclasses.replace(r, second=False, pending_code="",
                                                               pending_alerts=()),
                                   e, lock, machine, line, data, t)
            elif confirming and not failed:
                self.confirms_failed = 0
        if (cyc is not None and cyc.load_ok and j.committed and r.running is not None
                and hx_state.same_running(j.facts.running, r.running)):
            self.confirms_failed = 0            # a Load this cycle read the R now committed
        if load_failed and not (j.committed and r.running_unchecked):
            # A failed Load, or a confirming Load that failed or wasn't made, is tried again at the next
            # cycle (about 5 s), not the next slow set, until the message check counts as failed and that
            # has committed (amber message_unchecked; then the slow set's pace again). Owner decision, 31 Oct.
            # The retry isn't a required Load: when it fails too, R stays as for a slow set's Load, so a
            # committed drift keeps its R, code and alert (a required Load is need_load's, R stale)
            self.retry_load = True
        self._write_judgement(j, r, e, cyc)

    def _second_reading(self, cyc, r, items, data):
        """Each rechecked item is read again. One that can't be (no reply, busy, nonsense, too late) takes
        prev's value, never the first reading's, so it confirms nothing (§3.8): a stray value read once
        never commits on its own."""
        printer = data.printers.get(self.printer_id)
        p = self.p
        done = set()
        if "load" in items:
            r.running = None        # never the first Load's R: a confirming Load that isn't made confirms nothing
        prev_daily = p.daily or {}
        for item in items:
            if item in ("reach", "PRIN"):
                r.printing = r.printing_unchecked = None
            elif item == "INKL":
                r.ink_level = None
            elif item == "clock":
                r.clock_offset_s = None
            elif item == "MSGNAM":
                r.message_name = None
            elif item.startswith("daily:") and r.daily is not None:
                tag = item[len("daily:"):]
                values = dict(r.daily.get("values") or {})
                before = prev_daily.get("values") or {}
                if tag in before:
                    values[tag] = before[tag]
                else:
                    values.pop(tag, None)
                r.daily = dict(r.daily, values=values)
            elif item == "fonts" and r.daily is not None:
                r.daily = dict(r.daily, fonts=prev_daily.get("fonts"))
        try:
            for item in items:
                if item in ("reach", "PRIN"):
                    if "PRIN" in done:
                        continue
                    done.add("PRIN")
                    if self._prin(cyc, r):
                        r.printing_unchecked = False
                elif item == "uid":
                    self._hxui(cyc, printer)
                elif item == "login":
                    try:
                        self._call(cyc, "login", lambda end: cyc.client.login(deadline=end))
                        cyc.login_done = True
                        self._replied(cyc)
                        if self.problem == "login_failed":
                            self.problem = ""
                    except (AuthFailed, BadReply, WebReadFailed):
                        # Refused, or a login reply that can't be used (§10.1 rule 3): login_failed
                        cyc.login_done = True
                        self._replied(cyc)
                        self.problem = "login_failed"
                    except PrinterBusy:
                        pass        # nothing sent (this PC's gate was busy until too late): confirms nothing
                    except NoReply:
                        self._no_reply(cyc)
                        raise _NoMore()
                elif item == "load":
                    self._load(cyc, r, required=True)
                elif item == "INKL":
                    value = self._read(cyc, "INKL")
                    if value is not _MISSING:
                        r.ink_level = value
                elif item == "clock":
                    self._clock(cyc, r)
                elif item == "MSGNAM":
                    value = self._read(cyc, "MSGNAM")
                    if value is not _MISSING:
                        r.message_name = "" if value is None else hx_state.norm_name(value)
                elif item.startswith("daily:"):
                    tag = item[len("daily:"):]
                    value = self._read(cyc, tag)
                    if value is not _MISSING and r.daily is not None:
                        r.daily = dict(r.daily, values={**(r.daily.get("values") or {}), tag: value})
                elif item == "fonts":
                    try:
                        fonts = self._call(cyc, "web", lambda end: cyc.client.fonts(deadline=end))
                        self._replied(cyc)
                        if r.daily is not None:
                            r.daily = dict(r.daily, fonts=list(fonts))
                    except NoReply:
                        self._no_reply(cyc)
                        raise _NoMore()
                    except (BadReply, WebReadFailed, PrinterBusy, AuthFailed):
                        pass
        except _NoMore:
            pass

    def _write_judgement(self, j, r, e, cyc):
        """Step 11 and 12: the picture, P, then the log entries."""
        p = self.p
        f = j.facts
        if f.reachable:
            unreachable_since = None
        elif not p.reachable and p.unreachable_since is not None:
            unreachable_since = p.unreachable_since
        else:
            unreachable_since = r.at
        new = dataclasses.replace(
            p, paused_for_lock="", yielded_to="", reachable=f.reachable, failures=f.failures,
            last_answer_at=self.last_answer_at, printing=f.printing, message_name=f.message_name,
            bag_count=f.bag_count, bags_recent=f.bags_recent, ink_name=f.ink_name, ink_level=f.ink_level,
            head_temp_c=f.head_temp_c, clock_offset_s=f.clock_offset_s, running=f.running, daily=f.daily,
            state=j.state, alerts=j.alerts, foreign_client=f.foreign_client, problem=f.problem,
            expected_counter=(e.counter if e is not None else 0) if j.committed else p.expected_counter,
            reread_seen=self.reread_seen, unreachable_since=unreachable_since, running_ok_at=self.running_ok_at,
            running_unchecked=f.running_unchecked, printing_unchecked=f.printing_unchecked, dst=self.dst,
            standalone=False)
        if cyc is None or self.informed:
            # Judged freshness (§6.14): only a judgement that learnt something moves `judged`. A cycle whose
            # every reply was busy or nonsense keeps it, so the board's age goes on growing
            new = dataclasses.replace(new, **self._judged_now())
        if not self._still_alone():
            return
        sha = new.running.preview_sha256 if new.running is not None else ""
        old_sha = p.running.preview_sha256 if p.running is not None else ""
        if sha and sha != old_sha and sha in self.previews:
            try:
                self.store.write_preview(self.machine_id, self.previews[sha])
            except (ShareUnavailable, ShareError, ValueError):
                self._share_trouble()
        if not self._write(new):
            return
        if j.committed:
            self.r_stale = False
        if self.replied:
            self.poller.reach.reached()
        self._log_judgement(j, p, new, r)

    def _log_judgement(self, j, old, new, r):
        if j.changed and j.state is not None:
            self._log({"type": "state_changed", "from_code": old.state.code if old.state is not None else "",
                       "to_code": j.state.code, "colour": j.state.colour, "headline": j.state.headline.to_json(),
                       "detail": [m.to_json() for m in j.state.detail], "standalone": False})
        for a in j.started:
            self._log({"type": "alert_started", "alert": to_json(a)})
        for a in j.ended:
            self._log({"type": "alert_ended", "alert_id": a.id, "kind": a.kind, "started_at": a.started_at,
                       "ended_at": a.ended_at, "acked_by": a.acked_by})
        if j.committed and r.daily is not None and new.daily == r.daily:
            before = old.daily or {}
            changed = {}
            for name, value in (r.daily.get("values") or {}).items():
                was = (before.get("values") or {}).get(name)
                if was != value:
                    changed[name] = [was, value]
            for name in ("fonts", "firmware"):
                if before.get(name) != r.daily.get(name):
                    changed[name] = [before.get(name), r.daily.get(name)]
            if not before or changed:
                self._log({"type": "daily_check", "daily": r.daily, "changed": changed})


# --------------------------------------------------------------------------
# Standalone checks (§6.12)
# --------------------------------------------------------------------------

class _StandaloneCheck:
    """This PC's own quick check of one printer while the share is down; results in memory only."""

    def __init__(self, poller, printer_id, machine_id):
        self.poller = poller
        self.printer_id = printer_id
        self.machine_id = machine_id
        self.status = self._seed()
        self.failures = 0
        self.problem = ""
        self.counter = 0
        self.samples = []
        self.started_mono = poller.clock.monotonic()
        self.thread = None

    def _seed(self):
        """prev for the first check (§6.12 item 3): None, or the last share P of a machine with a known
        origin mismatch (owner decision, 31 Oct), judged against the E this PC holds:
        - a drift (its code or an active drift alert) whose R isn't E's message, or is unknown (a Load
          failed). Standalone checks never load the message and can't tell a same-name drift from E, so
          it stays red, with its alert, acks and R, until a share-mode judgement clears it (§3.4 C11c);
        - a message share mode couldn't check (message_unchecked, or running_unchecked: the Loads, or
          their confirmations, failed, maybe while each good first Load read another origin). Standalone
          can't check it either, so it is carried with running_unchecked set: at best amber, the origin
          unknown (C11d).
        Only those are carried; every other machine starts afresh as before. The newest P this PC has: the
        coordinator's last read, any other read of P on this PC (the watcher reads P itself when the
        coordinator's copy is older than heartbeat_s), or what this PC's own worker last wrote."""
        poller = self.poller
        found = [p for p in ((poller.seen_polls().get(self.machine_id) or (None, 0.0))[0],
                             poller.store.cached_poll(self.machine_id),
                             poller._written_polls().get(self.machine_id)) if p is not None]
        if not found:
            return None
        p = max(found, key=lambda x: x.counter)
        e = poller._cached_expected(self.machine_id)
        if e is None or p.expected_counter != e.counter or p.machine_id != self.machine_id:
            return None
        drift = hx_state.known_drift(p) and not hx_state.matches_expected(
            p.running, name=e.message_name, slot=e.slot, fingerprint=e.fingerprint)
        unchecked = p.running_unchecked or (p.state is not None and p.state.code == "message_unchecked")
        if not (drift or unchecked):
            return None
        return dataclasses.replace(p, printer_id=self.printer_id, poller=poller.identity.key, counter=0,
                                   paused_for_lock="", yielded_to="", running_unchecked=unchecked, standalone=True)

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def _read(self, cyc, client, tag):
        poller = self.poller
        if self.printer_id in poller.store.local_locks:     # this PC's own Send still pauses it
            raise _NoMore()
        try:
            value = client.read(tag)
        except NoReply:
            cyc.failures += 1
            raise _NoMore()
        except (PrinterBusy, BadReply):
            cyc.failures = 0
            return _MISSING
        except AuthFailed:
            cyc.failures = 0
            self.problem = "login_failed"
            raise _NoMore()
        cyc.failures = 0
        cyc.replied = True
        if self.problem == "login_failed":
            self.problem = ""
        return value

    def _quick(self, cyc, client, r, printer, data, items):
        try:
            for item in items:
                if item == "HXUI":
                    value = self._read(cyc, client, "HXUI")
                    if value is not _MISSING:
                        if value != printer.uid:
                            self.problem = "wrong_printer"
                            raise _NoMore()
                        if self.problem == "wrong_printer":
                            self.problem = ""
                elif item == "PRIN":
                    value = self._read(cyc, client, "PRIN")
                    if value is not _MISSING:
                        r.printing = bool(value)
                elif item == "MSGNAM":
                    value = self._read(cyc, client, "MSGNAM")
                    if value is not _MISSING:
                        r.message_name = "" if value is None else hx_state.norm_name(value)
                elif item == "counter":
                    tag = data.company.bag_counter
                    if tag and tag != "none":
                        value = self._read(cyc, client, tag)
                        if value is not _MISSING and isinstance(value, int):
                            r.bag_count, r.bags_recent = self._bags(value)
        except _NoMore:
            pass
        r.failures = cyc.failures
        r.reachable = cyc.failures < _STANDALONE_UNREACHABLE_AFTER
        r.problem = self.problem

    def _bags(self, count):
        now = self.poller.clock.monotonic()
        window = self.poller.timings.silent_window_s
        s = self.samples
        if s and count < s[-1][1]:
            s[:] = [(now, count)]
        else:
            s.append((now, count))
        while len(s) >= 2 and s[1][0] < now - window:
            s.pop(0)
        return count, sum(b[1] - a[1] for a, b in zip(s, s[1:]) if b[0] >= now - window)

    def run(self):
        try:
            self._check()
        except Exception as e:
            traceback.print_exc()
            self.poller._log({"type": "internal_error", "machine_id": self.machine_id, "printer_id": self.printer_id,
                              "error": _one_line(e), "traceback": traceback.format_exc()}, standalone=True)

    def _check(self):
        poller = self.poller
        clock, t = poller.clock, poller.timings
        data = poller.data.current
        if data is None or self.printer_id in poller.store.local_locks:
            return
        printer = data.printers.get(self.printer_id)
        machine = data.machines.get(self.machine_id)
        line = data.lines.get(machine.line_id) if machine is not None else None
        if printer is None or machine is None or line is None:
            return
        e = poller._cached_expected(self.machine_id)
        try:
            client = poller.clients.client(printer)
        except AuthFailed:
            client = None
            self.problem = "login_failed"
        answered = None              # wall time of the last reading that got a reply
        for attempt in (1, 2):
            # Standalone has its own rule for printing it couldn't read (C12), not PRIN_UNREAD_LIMIT
            r = Reading(at=clock.time(), reachable=True, standalone=True, printing_unchecked=False)
            cyc = _Cycle(None, self.failures)
            if client is None:
                r.problem, r.failures = self.problem, self.failures
                r.reachable = self.failures < _STANDALONE_UNREACHABLE_AFTER
            else:
                self._quick(cyc, client, r, printer, data, ("HXUI", "PRIN", "MSGNAM", "counter"))
                if cyc.replied:
                    answered = r.at
            self.failures = r.failures
            j = hx_state.judge(self.status, r, e, None, machine, line, data, t)
            if j.recheck and client is None:
                # Nothing can be read again (a password that can't be unscrambled): it commits at once,
                # as in share mode (§6.4 step 10)
                r = dataclasses.replace(r, second=True, pending_code=j.candidate.code,
                                        pending_alerts=j.candidate_alerts)
                j = hx_state.judge(self.status, r, e, None, machine, line, data, t)
            elif j.recheck:
                if not clock.sleep(t.second_reading_s, poller._cancel):
                    return
                r2 = dataclasses.replace(r, second=True, at=clock.time(), pending_code=j.candidate.code,
                                         pending_alerts=j.candidate_alerts)
                items = []
                for item in j.recheck:
                    mapped = {"reach": "PRIN", "PRIN": "PRIN", "uid": "HXUI", "MSGNAM": "MSGNAM"}.get(item)
                    if mapped and mapped not in items:
                        items.append(mapped)
                again = _Cycle(None, self.failures)
                self._quick(again, client, r2, printer, data, items)
                if again.replied:
                    answered = r2.at
                self.failures = r2.failures
                r = r2
                j = hx_state.judge(self.status, r, e, None, machine, line, data, t)
                if not j.committed and attempt == 1:
                    # The second reading contradicts the first (say printing read off, then no reply):
                    # nothing commits (§3.8 step 6). Share mode starts afresh at its next cycle, 5 s
                    # later; the next standalone check is standalone_poll_s away, so check again now
                    # rather than leave the machine as it was (green, say) until then
                    if not clock.sleep(t.second_reading_s, poller._cancel):
                        return
                    continue
            break
        old = self.status
        f = j.facts
        self.counter += 1
        base = old or PollStatus(machine_id=self.machine_id, printer_id=self.printer_id, poller=poller.identity.key)
        if f.reachable:
            since = None
        elif old is not None and not old.reachable and old.unreachable_since is not None:
            since = old.unreachable_since
        else:
            since = r.at
        self.status = dataclasses.replace(
            base, printer_id=self.printer_id, poller=poller.identity.key, counter=self.counter, written_at=r.at,
            reachable=f.reachable, failures=f.failures,
            last_answer_at=answered if answered is not None else base.last_answer_at,
            printing=f.printing, message_name=f.message_name, bag_count=f.bag_count, bags_recent=f.bags_recent,
            ink_name=f.ink_name, ink_level=f.ink_level, head_temp_c=f.head_temp_c, clock_offset_s=f.clock_offset_s,
            running=f.running, daily=f.daily, state=j.state, alerts=j.alerts, foreign_client=f.foreign_client,
            problem=f.problem, expected_counter=e.counter if e is not None else 0, unreachable_since=since,
            running_unchecked=f.running_unchecked, printing_unchecked=f.printing_unchecked, standalone=True)
        poller._set_local(self.machine_id, self.status)
        base_entry = {"machine_id": self.machine_id, "printer_id": self.printer_id, "line_id": machine.line_id}
        if j.changed and j.state is not None:
            poller._log({**base_entry, "type": "state_changed",
                         "from_code": old.state.code if old is not None and old.state is not None else "",
                         "to_code": j.state.code, "colour": j.state.colour, "headline": j.state.headline.to_json(),
                         "detail": [m.to_json() for m in j.state.detail], "standalone": True}, standalone=True)
        for a in j.started:
            poller._log({**base_entry, "type": "alert_started", "alert": to_json(a)}, standalone=True)
        for a in j.ended:
            poller._log({**base_entry, "type": "alert_ended", "alert_id": a.id, "kind": a.kind,
                         "started_at": a.started_at, "ended_at": a.ended_at, "acked_by": a.acked_by}, standalone=True)


class _Standalone:
    def __init__(self, entered, offset):
        self.entered = entered
        self.next_check = entered + offset
        self.checks = {}             # printer id -> _StandaloneCheck


# --------------------------------------------------------------------------
# The Poller
# --------------------------------------------------------------------------

class Poller:
    """The shared polling service: a coordinator thread 'hx-poller' plus one worker thread
    'hx-poll-<printer_id>' per printer this PC currently polls (architecture §7.1)."""

    def __init__(self, *, store, data, clients, time, bus, identity, version, clock: Clock = SYSTEM_CLOCK,
                 timings: Timings = TIMINGS, local=None):
        self.store = store
        self.data = data
        self.clients = clients
        self.time = time
        self.bus = bus
        self.identity = identity
        self.version = version
        self.clock = clock
        self.timings = timings
        if local is not None:
            self.reach = ReachMemory(local, clock=clock, timings=timings)
        else:
            self.reach = _AlwaysReach(None, clock=clock, timings=timings)
        self.mode = "idle"
        self.polling: list[str] = []
        self.live = []
        self.printer_ids: list[str] = []
        self._lock = threading.Lock()
        self._workers = {}
        self._seen = {}
        self._written = {}           # machine id -> the P this PC's worker last wrote
        self._max_counter = {}
        self._cancel = CancelToken()
        self._wake = CancelToken()
        self._thread = None
        self._time_thread = None
        self._threaded = False
        self._monitor_open = False
        self._floor_board = False
        self._suspended = False
        self._avail_since = None
        self._down_since = None
        self._last_reached = None
        self._standalone = None
        self._local = {}
        self._expected_read_at = None
        self._update = PollerUpdate(mode="idle")
        self._update_key = None
        self._dst = None
        self._last_probe_id = ""
        self._waiting_state = {}
        self._next_coord = None
        self._refresh_wanted = False
        self._last_refresh = None
        self._rng = random.Random()
        # The stuck-worker watchdog (§6.2 item 3.0)
        self._progress = {}          # printer id -> clock.monotonic() of this PC's last good P write for it
        self._rank0 = {}             # printer id -> clock.monotonic() since this PC has ranked first for it
        self._started = {}           # printer id -> clock.monotonic() when this PC last started a worker for it
        self._fenced = {}            # printer id -> (fenced worker, clock.monotonic() when it was fenced)
        self._strikes = {}           # printer id -> (fences in a row, clock.monotonic() of the last one)
        self._probation = {}         # printer id -> clock.monotonic() its decline ended, until a good P write
        self._last_share_pass = None
        self._resumed_at = None      # the first share pass after this coordinator itself was paused
        self._judged = CounterWatch(clock)      # P's judged counters, for a worker's long-gap rule (§6.3)

    # ---- the public calls

    def start(self) -> None:
        if self._thread is not None:
            return
        self._threaded = True
        self._thread = threading.Thread(target=self._run, name="hx-poller", daemon=True)
        self._time_thread = threading.Thread(target=self._time_loop, name="hx-time", daemon=True)
        self._thread.start()
        self._time_thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        """Hands each polled printer to the next ranked poller (yielded_to), removes the heartbeat."""
        deadline = self.clock.monotonic() + timeout_s
        self._cancel.cancel()
        self._wake.cancel()
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.wake.cancel()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, deadline - self.clock.monotonic()))
        for w in workers:
            if w.thread is not None:
                w.thread.join(max(0.0, deadline - self.clock.monotonic()) + self.timings.http_timeout_s)
            elif not w.done:
                w.tick()            # tests: the yield on the calling thread
        if self._time_thread is not None:
            self._time_thread.join(max(0.0, deadline - self.clock.monotonic()))
        self.store.heartbeat().remove()

    def set_monitor_open(self, monitor_open: bool, floor_board: bool) -> None:
        self._monitor_open = bool(monitor_open) or bool(floor_board)
        self._floor_board = bool(floor_board)
        self._wake.cancel()

    def refresh_now(self) -> bool:
        """Standalone mode: check now. False when used within refresh_now_min_s, or not standalone."""
        with self._lock:
            if self.mode != "standalone":
                return False
            now = self.clock.monotonic()
            if self._last_refresh is not None and now - self._last_refresh < self.timings.refresh_now_min_s:
                return False
            self._last_refresh = now
            self._refresh_wanted = True
        self._wake.cancel()
        return True

    def local_statuses(self) -> dict[str, PollStatus]:
        """Standalone: this PC's own checks; share mode: the P each of this PC's workers last wrote."""
        with self._lock:
            if self.mode == "standalone":
                return dict(self._local)
            return {w.machine_id: w.p for w in self._workers.values() if w.p is not None and not w.done}

    def seen_polls(self) -> dict[str, tuple[PollStatus | None, float]]:
        with self._lock:
            return dict(self._seen)

    def last_update(self) -> PollerUpdate:
        return self._update

    def run_once(self) -> float:
        """Tests: one coordinator pass when due and one tick of every worker that is due, on the calling
        thread. Returns the clock.monotonic() of the next moment something is due."""
        now = self.clock.monotonic()
        if self._next_coord is None or now >= self._next_coord - _DUE_S or self._wake.cancelled:
            if self._wake.cancelled:
                self._wake = CancelToken()
            self._coordinate()
            self._next_coord = now + self._coordinator_wait(now)
        with self._lock:
            workers = list(self._workers.values()) + [w for w, _ in self._fenced.values() if w is not None]
        for w in workers:
            if not w.done and (self.clock.monotonic() >= w.next_wake - _DUE_S or w.wants_out()):
                w.tick()
        due = [self._next_coord]
        with self._lock:
            due += [w.next_wake for w in self._workers.values() if not w.done]
        return min(due)

    # ---- threads

    def _run(self):
        while not self._cancel.cancelled:
            now = self.clock.monotonic()
            try:
                self._coordinate()
            except Exception:
                traceback.print_exc()
            wait = self._coordinator_wait(now) - (self.clock.monotonic() - now)
            wake = self._wake
            if wait > 0:
                self.clock.sleep(wait, wake)
            if wake.cancelled and not self._cancel.cancelled:
                self._wake = CancelToken()

    def _coordinator_wait(self, now):
        t = self.timings
        if self.mode == "standalone" and self._standalone is not None:
            return max(0.0, min(1.0, t.heartbeat_s, self._standalone.next_check - now))
        return t.heartbeat_s

    def _time_loop(self):
        while not self._cancel.cancelled:
            hosts = []
            try:
                data = self.data.current
                if data is not None:
                    hosts = [data.company.time_server, data.company.time_server_fallback]
                    self.time.set_hosts(hosts)
                self.time.now(fresh=True)
            except Exception:       # never let the thread die (architecture §8.1)
                traceback.print_exc()
            hosts = [h for h in hosts if h]
            self.clock.sleep(self.time.cache_s if hosts else self.timings.heartbeat_s, self._cancel)

    def _start_thread(self, target, name):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        return thread

    # ---- bookkeeping shared with the workers

    def _log(self, entry, *, standalone=False):
        entry = dict(entry)
        entry.setdefault("poller", self.identity.key)
        if standalone:
            entry["standalone"] = True
        try:
            self.data.append_log("hx-events", entry)
        except (HxError, ValueError, OSError) as e:
            print(f"Could not log {entry.get('type')}: {e}", file=sys.stderr)

    def _note_counter(self, machine_id, counter):
        with self._lock:
            if counter > self._max_counter.get(machine_id, 0):
                self._max_counter[machine_id] = counter

    def _suspend_share(self):
        with self._lock:
            self._suspended = True
            self._avail_since = None
        self._wake.cancel()

    def _set_local(self, machine_id, status):
        with self._lock:
            self._local[machine_id] = status

    def _note_written(self, status):
        with self._lock:
            self._written[status.machine_id] = status
        self._note_progress(status.printer_id)

    def _note_progress(self, printer_id):
        """A good P write by this PC's worker, or a worker waiting for another PC that writes P (§6.2 item 3.0)."""
        with self._lock:
            self._progress[printer_id] = self.clock.monotonic()

    def _judged_age(self, p):
        """How long this PC has seen P's judged counter at its value (0.0 on a change or a first sight);
        None for a P without one (an older Stripes wrote it)."""
        if p is None or p.judged is None:
            return None
        return self._judged.see("judged:" + p.machine_id, p.judged)

    def _written_polls(self):
        with self._lock:
            return dict(self._written)

    def _cached_expected(self, machine_id):
        # The last E this PC read or wrote (§6.12 item 3): the store keeps it for every read, the
        # coordinator's, a worker's, the watcher's or a Send's. The watcher overlays the same copy in
        # standalone mode, so a check and the board never judge against two different Es.
        return self.store.cached_expected(machine_id)

    # ---- the coordinator pass (§6.2)

    def _coordinate(self):
        if self._cancel.cancelled:
            return
        now = self.clock.monotonic()
        data = self.data.current
        if data is None:
            self._stop_workers("share")
            self._set_mode("idle")
            self._post_update()
            return
        printers = _printers(data)
        self.printer_ids = sorted(p.id for p, _, _ in printers)
        self._track_share(now)
        share = self.store.available and not self._suspended
        if share:
            try:
                self._share_pass(data, printers, now)
                self._set_mode("share")
            except ShareError:
                share = False
        if not share:
            self._stop_workers("share")
            if not self.store.available:
                self._try_beat()    # a call while the share is down starts the store's probe
            # Only a PC that can poll (it reached a printer within a day, storage.md §12.3): an office PC's
            # checks would be blocked and show printing machines as off (§6.12 item 1)
            if self._monitor_open and self._down_since is not None \
                    and now - self._down_since >= self.timings.lock_stale_s and self.reach.can_poll():
                self._standalone_pass(data, printers, now)
            else:
                self._set_mode("idle")
        self._dst_check()
        self._reap()
        self._post_update()

    def _track_share(self, now):
        store = self.store
        reached = store.last_reached
        if store.available:
            self._down_since = None
            if self._suspended:
                if self._avail_since is None:
                    self._avail_since = now
                elif now - self._avail_since >= self.timings.heartbeat_s and self._try_beat():
                    with self._lock:
                        self._suspended = False
                        self._avail_since = None
        else:
            self._avail_since = None
            if self._down_since is None or reached != self._last_reached:
                self._down_since = now
        self._last_reached = reached

    def _try_beat(self):
        try:
            self._beat()
            return True
        except (ShareError, ValueError):
            return False

    def _beat(self):
        with self._lock:
            declined = sorted(self._fenced)
        self.store.heartbeat().beat(can_poll=self.reach.can_poll(), last_printer_reach=self.reach.last_printer_reach,
                                    polling=self._running_printers(), monitor_open=self._monitor_open,
                                    floor_board=self._floor_board, declined=declined)

    def _running_printers(self):
        with self._lock:
            return sorted(pid for pid, w in self._workers.items() if not w.done)

    def _stuck_check(self, now):
        """§6.2 item 3.0: a printer this PC ranks first for, and has kept a worker for (running, or started
        again after failed starts), with no good P write by this PC for not_monitored_s: its worker is
        fenced and the printer declined in the heartbeat, so every PC ranks the next PC first and the
        backups stop handing it back. It stays declined until the fenced worker has ended and the
        decline's time has passed: lock_stale_s, doubled for each fence within DECLINE_MEMORY_S of the
        last, up to DECLINE_MAX_S. Then the normal handover brings it back, on probation: if the first
        worker started after that ends without a good P write (the fault is still there), the printer is
        declined again at once, without another not_monitored_s wait."""
        t = self.timings
        me = self.identity.key
        last, self._last_share_pass = self._last_share_pass, now
        if last is None or now - last >= t.stale_s:
            self._resumed_at = now      # share mode just began, or this PC itself was paused: no worker's fault
        with self._lock:
            for pid, (w, at) in list(self._fenced.items()):
                ended = w is None or w.done or (w.thread is not None and not w.thread.is_alive())
                if ended and now - at >= self._decline_s(pid):
                    del self._fenced[pid]
                    self._probation[pid] = now
            for pid, ended_at in list(self._probation.items()):
                if self._progress.get(pid, -math.inf) >= ended_at:
                    del self._probation[pid]        # a good P write since: it works again
            workers = dict(self._workers)
            fenced = set(self._fenced)
            probation = dict(self._probation)
        ranks = assign(self.printer_ids, self.live)
        data = self.data.current
        for pid in self.printer_ids:
            order = ranks.get(pid) or []
            if pid in fenced or not order or order[0] != me:
                self._rank0.pop(pid, None)  # another PC polls it by rank: not this PC's to watch
                continue
            since = max(self._rank0.setdefault(pid, now), self._progress.get(pid, -math.inf), self._resumed_at)
            w = workers.get(pid)
            kept = w is not None or self._started.get(pid, -math.inf) >= since
            # On probation: the first worker started since the decline ended, ended without a good P write
            again = (pid in probation and self._started.get(pid, -math.inf) >= probation[pid]
                     and (w is None or w.done))
            if not again and (not kept or now - since < t.not_monitored_s):
                continue
            if w is not None:
                w.fence()
            with self._lock:
                if w is not None and self._workers.get(pid) is w:
                    del self._workers[pid]
                self._fenced[pid] = (w, now)
                self._probation.pop(pid, None)
                count, at = self._strikes.get(pid, (0, -math.inf))
                self._strikes[pid] = (count + 1 if now - at < DECLINE_MEMORY_S else 1, now)
                decline = self._decline_s(pid)
            self._rank0.pop(pid, None)
            mid = w.machine_id if w is not None else ""
            machine = data.machines.get(mid) if data is not None and mid else None
            self._log({"type": "internal_error", "error": "worker_stuck", "machine_id": mid, "printer_id": pid,
                       "line_id": machine.line_id if machine is not None else "", "stuck_s": round(now - since, 1),
                       "declined_s": decline})

    def _decline_s(self, pid):
        """How long a fenced printer stays declined (§6.2 item 3.0): lock_stale_s, doubled for each fence in
        a row (fences less than DECLINE_MEMORY_S apart), up to DECLINE_MAX_S."""
        count, _ = self._strikes.get(pid, (1, 0.0))
        return min(self.timings.lock_stale_s * 2 ** max(0, count - 1), max(DECLINE_MAX_S, self.timings.lock_stale_s))

    def _share_pass(self, data, printers, now):
        store, t = self.store, self.timings
        me = self.identity.key
        self._stuck_check(now)
        self._beat()
        live = store.live_heartbeats()
        self.live = live
        ranks = assign(self.printer_ids, live)
        unpolled = []
        for printer, machine, line in printers:
            newer = False
            try:
                p = store.read_poll(machine.id)
            except FormatTooNew:
                p, newer = None, True
            except ShareError:
                continue
            read_at = self.clock.monotonic()
            if not newer:
                with self._lock:
                    self._seen[machine.id] = (p, read_at)
            if p is not None:
                self._note_counter(machine.id, p.counter)
                self._judged_age(p)
            age = store.poll_age(machine.id)
            age = 0.0 if age is None else age
            order = ranks.get(printer.id) or []
            with self._lock:
                w = self._workers.get(printer.id)
                declined = printer.id in self._fenced
            if declined:
                continue            # its fenced worker may still be stuck in a call: never a second one
            if age >= t.stale_s and w is None:
                unpolled.append((printer, machine))
            if w is not None and not w.done:
                if w.machine_id != machine.id:
                    w.request_stop("stop")
                elif order and order[0] != me:
                    w.request_handover(order[0])
                continue
            if newer or me not in order:
                continue
            rank = order.index(me)
            # This PC's own yield to a PC whose heartbeat now declines the printer (§6.2 item 3.0) is void:
            # P is still this PC's write, so that PC never polled after it
            void = (p is not None and p.poller == me and bool(p.yielded_to) and p.yielded_to != me
                    and any(hb.key == p.yielded_to and printer.id in (hb.declined or ()) for hb in live))
            names_me = p is not None and ((p.poller == me and (not p.yielded_to or void)) or p.yielded_to == me)
            if p is not None and age < t.stale_s and not names_me:
                continue            # fresh and another PC's, which hasn't yielded to this one
            if rank == 0:
                start = names_me or age >= t.stale_s
            else:
                start = age >= takeover_after_s(rank, t)
            if start:
                if p is None:
                    reason = "assigned"
                elif p.yielded_to == me:
                    reason = "handover"
                elif p.poller == me:
                    reason = "restart"
                else:
                    reason = "takeover"
                self._start_worker(printer.id, machine.id, reason)
        with self._lock:
            workers = list(self._workers.items())
        for pid, w in workers:
            if pid not in self.printer_ids and not w.done:
                w.request_stop("stop")
        # The standalone cache of E (§6.12): every read_expected leaves its copy in the store
        if self._expected_read_at is None or now - self._expected_read_at >= t.poll_slow_s:
            self._expected_read_at = now
            for printer, machine, line in printers:
                try:
                    store.read_expected(machine.id)
                except (FormatTooNew, ShareError):
                    continue
        self._probe(data, unpolled)
        self._waiting_check(data, live)

    def _start_worker(self, printer_id, machine_id, reason):
        if self._cancel.cancelled:
            return
        with self._lock:
            # The first share pass after an outage runs before the mode leaves standalone (_coordinate)
            seen = self._local.get(machine_id) if self.mode == "standalone" else None
        w = _Worker(self, printer_id, machine_id, reason, standalone=seen)
        with self._lock:
            self._workers[printer_id] = w
            self._started[printer_id] = self.clock.monotonic()
        if self._threaded:
            w.thread = self._start_thread(w.run, f"hx-poll-{printer_id}")

    def _stop_workers(self, reason):
        with self._lock:
            workers = [w for w in self._workers.values() if not w.done]
        for w in workers:
            w.request_stop(reason)

    def _reap(self):
        # A finished worker, or one whose thread ended anyway: the next pass may start a new one
        with self._lock:
            for pid in [pid for pid, w in self._workers.items()
                        if (w.done and (w.thread is None or not w.thread.is_alive()))
                        or (w.thread is not None and not w.thread.is_alive())]:
                del self._workers[pid]

    def _set_mode(self, mode):
        with self._lock:
            old, self.mode = self.mode, mode
            if mode != "standalone":
                self._standalone = None
                self._local = {}
                self._refresh_wanted = False
        if old != mode:
            self._log({"type": "poller_mode", "from_mode": old, "to_mode": mode})

    def _post_update(self):
        polling = self._running_printers() if self.mode == "share" else (
            sorted(self._standalone.checks) if self.mode == "standalone" and self._standalone is not None else [])
        next_check = None
        if self.mode == "standalone" and self._standalone is not None:
            next_check = max(0.0, self._standalone.next_check - self.clock.monotonic())
        self.polling = polling
        update = PollerUpdate(mode=self.mode, polling=list(polling), next_check_s=next_check)
        key = (update.mode, tuple(update.polling))
        if key != self._update_key or self.mode == "standalone":
            self._update_key = key
            self._update = update
            self.bus.post(update)
        else:
            self._update = update

    # ---- the reachability probe (storage.md §12.3)

    def _probe(self, data, unpolled):
        reach = self.reach
        if not reach.probe_due(unpolled=bool(unpolled)):
            return
        targets = []
        for printer, machine in sorted(unpolled, key=lambda pm: pm[0].id):
            if printer.id in self.store.local_locks:
                continue
            try:
                if self.store.read_lock(printer.id) is not None:
                    continue
            except ShareError:
                continue
            targets.append((printer, machine))
        if not targets:
            return
        later = [pm for pm in targets if pm[0].id > self._last_probe_id]
        target, machine = (later or targets)[0]
        self._last_probe_id = target.id
        # Its P once more, right before the probe: on a slow share a poller may have started since this
        # pass read it, and a printer someone polls is never probed (storage.md §12.3). A poller that
        # starts after this read makes its first request only settle_s after its first write.
        try:
            self.store.read_poll(machine.id)
        except (FormatTooNew, ShareError):
            return
        age = self.store.poll_age(machine.id)
        if age is None or age < self.timings.stale_s:
            return
        try:
            client = self.clients.client(target)
        except AuthFailed:
            return
        ok = client.probe()
        if ok is True:
            reach.reached()
            reach.probed(True)
        elif ok is False:
            reach.probed(False)

    # ---- alert bookkeeping (§4.6, §6.15)

    def _waiting_check(self, data, live):
        with self._lock:
            workers = [w for w in self._workers.values() if not w.done and w.p is not None]
        for w in workers:
            machine = data.machines.get(w.machine_id)
            for a in w.p.alerts:
                if not a.active or not a.sound or a.acked_by:
                    continue
                waiting = list(w.waiting.get(a.id, a.waiting))
                changed = False
                for r in a.recipients:
                    if not r.startswith("user:"):
                        continue
                    name = r[5:]
                    state_key = (a.id, name.lower())
                    state = self._waiting_state.get(state_key)
                    if state is None and any(same_user(name, x) for x in waiting):
                        state = self._waiting_state[state_key] = "waiting"     # logged by an earlier poller
                    hb = next((h for h in live if same_user(h.user, name)), None)
                    entry = {"machine_id": w.machine_id, "printer_id": w.printer_id,
                             "line_id": machine.line_id if machine is not None else a.line_id,
                             "alert_id": a.id, "kind": a.kind, "user": name}
                    if hb is None:
                        if state != "waiting":
                            self._waiting_state[state_key] = "waiting"
                            self._log({**entry, "type": "alert_waiting"})
                        if not any(same_user(name, x) for x in waiting):
                            waiting.append(name)
                            changed = True
                    else:
                        if state == "waiting":
                            self._waiting_state[state_key] = "reached"
                            self._log({**entry, "type": "alert_reached", "computer": hb.computer})
                        if any(same_user(name, x) for x in waiting):
                            waiting = [x for x in waiting if not same_user(x, name)]
                            changed = True
                if changed:
                    w.set_waiting(a.id, waiting)

    # ---- daylight saving (§6.11 item 1)

    def _dst_check(self):
        offset_fn = getattr(self.time, "offset_fn", hx_time.local_utc_offset)
        rc = hx_time.recent_dst_change(self.clock.time(), within_s=DST_KEEP_S, offset_fn=offset_fn)
        if rc is None:
            self._dst = None
            return
        old_s, new_s, at = rc
        self._dst = (dst_change_id(at, new_s, offset_fn=offset_fn), old_s, new_s, at)

    # ---- standalone (§6.12)

    def _standalone_pass(self, data, printers, now):
        t = self.timings
        if self.mode != "standalone" or self._standalone is None:
            self._set_mode("standalone")
            with self._lock:
                self._standalone = _Standalone(now, self._rng.uniform(0.0, t.standalone_poll_s))
        st = self._standalone
        with self._lock:
            wanted, self._refresh_wanted = self._refresh_wanted, False
        if now >= st.next_check - _DUE_S:
            # At least one step: a wake-up within _DUE_S before the time is due too
            steps = max(1, math.floor((now - st.next_check) / t.standalone_poll_s) + 1)
            st.next_check += steps * t.standalone_poll_s
            wanted = True
        if not wanted:
            return
        for printer, machine, line in printers:
            if printer.id in self.store.local_locks:
                continue
            check = st.checks.get(printer.id)
            if check is None or check.machine_id != machine.id:
                check = st.checks[printer.id] = _StandaloneCheck(self, printer.id, machine.id)
            if check.busy():
                continue
            if self._threaded:
                check.thread = self._start_thread(check.run, f"hx-poll-{printer.id}")
            else:
                check.run()


# --------------------------------------------------------------------------
# The status watcher (§7)
# --------------------------------------------------------------------------

class StatusWatcher:
    """'hx-watch' thread: every watch_s reads the machine, line, lock and ack files through the store
    (or, in standalone mode, the poller's local_statuses), builds a BoardView with hx_state and posts
    BoardUpdate."""

    def __init__(self, *, store, data, poller, bus, identity, clock: Clock = SYSTEM_CLOCK,
                 timings: Timings = TIMINGS):
        self.store = store
        self.data = data
        self.poller = poller
        self.bus = bus
        self.identity = identity
        self.clock = clock
        self.timings = timings
        self.latest = None
        self._lock = threading.Lock()
        self._own_acks = {}
        self._ack_queue = []         # [(Ack, clock.monotonic() to give up at)]
        self._polls, self._expected, self._locks, self._lines = {}, {}, {}, {}
        self._too_new = set()
        self._found_acks = {}
        self._counters = {}          # machine id -> [P counter first seen, seen moving since]
        self._judged_seen = {}       # machine id -> hx_state.JudgedSeen, for the NOT MONITORED alarm (§7 item 7)
        self._watch = CounterWatch(clock)
        self._cancel = CancelToken()
        self._wake = CancelToken()
        self._thread = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hx-watch", daemon=True)
            self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._cancel.cancel()
        self._wake.cancel()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout_s)

    def refresh_now(self) -> None:
        self._wake.cancel()

    def acknowledge(self, alert_ids: list[str]) -> None:
        """Any thread; never blocks. This PC goes quiet at once; the share writes follow on hx-watch."""
        standalone = self.poller.mode == "standalone"
        with self._lock:
            for aid in alert_ids:
                if aid in self._own_acks:
                    continue
                ack = Ack(alert_id=aid, by=self.identity.user, computer=self.identity.computer, at=self.clock.time())
                self._own_acks[aid] = ack
                if not standalone:
                    self._ack_queue.append((ack, self.clock.monotonic() + ACK_RETRY_S))
            # latest and its BoardUpdate change together under the lock (_publish), so a pass that
            # ends meanwhile can neither be overwritten by the older board read here nor post its own
            # board without these acks. The lock is never held across share I/O: _with_acks is pure
            # and post only queues
            board = self.latest
            if board is not None:
                self._publish(_with_acks(board, self._own_acks))
        self._wake.cancel()

    def _publish(self, board):
        """With self._lock held: the board becomes latest and is posted."""
        self.latest = board
        self.bus.post(BoardUpdate(board=board))

    def _publish_pass(self, board, own):
        """A pass's board, with every own ack made since the pass copied them (own) applied to it."""
        with self._lock:
            if self._own_acks.keys() - own.keys():
                board = _with_acks(board, self._own_acks)
            self._publish(board)

    def run_once(self) -> None:
        """Tests: one pass on the calling thread."""
        self._pass()

    def _run(self):
        while not self._cancel.cancelled:
            try:
                self._pass()
            except Exception:
                traceback.print_exc()
            wake = self._wake
            if not wake.cancelled:
                self.clock.sleep(self.timings.watch_s, wake)
            if wake.cancelled and not self._cancel.cancelled:
                self._wake = CancelToken()

    def _pass(self):
        store, t = self.store, self.timings
        data = self.data.current
        if data is None:
            board = BoardView(lines=[], share_ok=store.available, generated_at=self.clock.time())
            with self._lock:
                self._publish(board)
            return
        machines = []
        for line in data.lines.values():
            if line.active:
                machines += [m for m in data.line_machines(line.id)]
        with self._lock:
            own = dict(self._own_acks)
        standalone = self.poller.mode == "standalone"
        share_ok = store.available
        polls, poll_ages, locks, lock_ages = {}, {}, {}, {}
        if standalone:
            share_ok = False
            local = self.poller.local_statuses()
            for m in machines:
                status = local.get(m.id)
                if status is not None:
                    polls[m.id] = status
                    poll_ages[m.id] = self._watch.see("poll:" + m.id, status.counter)
                elif self._last_good(m.id) is not None:
                    # Until this PC's first standalone check of the machine: the last good P, its age
                    # still growing, as while the share is down and the poller idle (item 4). So a red
                    # machine keeps its colour, alerts, sound and origin over the change of mode instead
                    # of showing "Checking..." (owner decision, 31 Oct)
                    polls[m.id] = self._last_good(m.id)
                    age = store.poll_age(m.id)
                    if age is not None:
                        poll_ages[m.id] = self._judged_age(m.id, polls[m.id], age)
                else:
                    self._watch.missing("poll:" + m.id)
                    poll_ages[m.id] = self._watch.age("poll:" + m.id)
                info = store.local_lock_info(m.printer_id) if m.printer_id in store.local_locks else None
                if info is not None:
                    locks[m.printer_id] = info
                    lock_ages[m.printer_id] = 0.0
            # The same E the standalone checks judge with (§6.12 item 3): with the watcher's own last
            # read instead, an E that changed shortly before the outage would keep "E not judged yet"
            # (§5.1 step 2.5) showing expected_code(E), green, over every check for the whole outage
            expected = {}
            for m in machines:
                e = self.poller._cached_expected(m.id)
                if e is not None:
                    expected[m.id] = e
            lines = dict(self._lines)
            # Acks read from the share before the outage still count: a carried alert (§6.12 item 3)
            # never rings again for an occurrence someone already acknowledged
            acks = {**self._found_acks, **own}
        else:
            if share_ok:
                share_ok = self._read_share(data, machines)
            for m in machines:
                p = self._polls.get(m.id) if share_ok else self._last_good(m.id)
                if p is not None:
                    polls[m.id] = p
                age = store.poll_age(m.id)
                if age is not None and p is not None:
                    age = self._judged_age(m.id, p, age)
                    if self._unproven(m.id, p, age):
                        age = None
                if age is not None:
                    poll_ages[m.id] = age
                lk = self._locks.get(m.printer_id)
                if lk is not None:
                    age = store.lock_age(m.printer_id)
                    if age is not None:
                        locks[m.printer_id] = lk
                        lock_ages[m.printer_id] = age
            expected = {k: v for k, v in self._expected.items() if v is not None}
            lines = dict(self._lines)
            acks = {**self._found_acks, **own}
        board = hx_state.board_view(data, polls, poll_ages, expected, locks, lock_ages, lines, acks,
                                    share_ok=share_ok, standalone=standalone, now=self.clock.time(), timings=t)
        board = self._with_alarms(data, board, polls, expected, acks)
        board.next_check_s = self.poller.last_update().next_check_s
        # An acknowledge() during this pass (most of which is share reads) already posted its board:
        # this one carries its acks too, so the sound doesn't start again (§4.5 item 2)
        self._publish_pass(board, own)
        self._write_acks()

    def _with_alarms(self, data, board, polls, expected, acks):
        """The board with each machine's NOT MONITORED alarm (§7 item 7): no poller judges such a machine, so
        every PC's watcher raises it itself, with the same id on every PC, and reads its ack from the share
        like any other. A PC that rings through its own record writes the shared marker, so a PC started
        later (or restarted) rings for the same occurrence too; the marker is read at most once per pass for
        a machine shown NOT MONITORED, and never otherwise. Machines no longer on the board are forgotten.
        Not while the share is down or in standalone mode, which have their own banner."""
        mono, now = self.clock.monotonic(), self.clock.time()
        share_ok = board.share_ok and not board.standalone
        seen, alarms = {}, {}
        gone = False                    # the share went away part-way: no more marker reads or writes
        for line in board.lines:
            record = data.lines.get(line.line_id)
            for view in line.machines:
                mid = view.machine_id
                prev = self._judged_seen.get(mid)
                args = (prev, polls.get(mid), view, expected.get(mid), record)
                kw = {"share_ok": share_ok, "mono": mono, "now": now, "alarm_s": NOT_MONITORED_ALARM_S}
                s, alarm = hx_state.not_monitored_alarm(*args, **kw)
                read, found = False, None
                if s is not None and s.ask and not gone:
                    # Its own record can't tell: a PC that saw the machine judged may have rung already
                    read = True
                    try:
                        found = self.store.read_alarm_marker(mid)
                    except ShareUnavailable:
                        gone = True
                    except (ShareError, ValueError):
                        found = None
                    if found is not None:
                        s, alarm = hx_state.not_monitored_alarm(*args, marker=found.alarm_id, **kw)
                if (alarm is not None and s.shared != alarm.id and view.code == "not_monitored"
                        and not gone):
                    # Rang through its own record: make sure the marker holds this occurrence
                    s, gone = self._mark(mid, alarm, s, found if read else self._read_marker(mid))
                if s is not None:
                    seen[mid] = s
                if alarm is not None:
                    alarms[mid] = alarm
        self._judged_seen = seen
        if gone:
            return board                # gone part-way: no sound from this pass; the next one is share down
        acks = dict(acks)
        for alarm in alarms.values():
            if alarm.id in acks:
                continue
            try:
                ack = self.store.read_ack(alarm.id)
            except ValueError:
                ack = None
            except ShareUnavailable:
                return board            # gone part-way: no sound from this pass; the next one is share down
            except ShareError:
                ack = None
            if ack is not None:
                self._found_acks[alarm.id] = ack
                acks[alarm.id] = ack
        return hx_state.with_board_alerts(board, alarms, acks)

    def _read_marker(self, machine_id):
        """The shared marker, None when missing or unreadable, or _GONE when the share went away."""
        try:
            return self.store.read_alarm_marker(machine_id)
        except ShareUnavailable:
            return _GONE
        except (ShareError, ValueError):
            return None

    def _mark(self, machine_id, alarm, s, found):
        """(the record, gone): the marker written for this occurrence unless it holds it already. A
        new occurrence overwrites the old one; it is never deleted. A failed write is tried at the next
        NOT MONITORED pass."""
        if found is _GONE:
            return s, True
        if found is None or found.alarm_id != alarm.id:
            try:
                self.store.write_alarm_marker(AlarmMarker(machine_id=machine_id, alarm_id=alarm.id,
                                                          started_at=alarm.started_at))
            except ShareUnavailable:
                return s, True
            except (ShareError, ValueError):
                return s, False
        return dataclasses.replace(s, shared=alarm.id), False

    def _last_good(self, machine_id):
        """While the share is down: the newest P this PC has, the watcher's last read or what this PC's
        own worker last wrote (a drift it committed just before the outage that no pass read yet), so
        the board shows what the standalone checks carry (§6.12 item 3, §7 items 3 and 4)."""
        p = self._polls.get(machine_id)
        own = self.poller._written_polls().get(machine_id)
        if own is not None and own.machine_id == machine_id and (p is None or own.counter > p.counter):
            return own
        return p

    def _judged_age(self, machine_id, p, age):
        """The age the board shows (§7 item 2): how long P's judged counter has stood still, never less than
        the counter's own age. Bookkeeping writes (a takeover's start, settle and yield) move the counter
        without reading the printer. A P without the field (an older Stripes) has the counter's age, and so
        does P's own "Checking..." (a worker starting after a long gap, §6.3 step 1), which says just that."""
        if p.judged is None or (p.state is not None and p.state.code == "checking"):
            return age
        return max(age, self._watch.see("judged:" + machine_id, p.judged))

    def _unproven(self, machine_id, p, age):
        """A P this watcher hasn't seen move yet, judged long ago by its writer's clock: the share's counter
        rule counts a counter seen for the first time as fresh (storage.md §10 rule 1), so a P left
        overnight would show as just checked for not_monitored_s after Stripes starts. Its age is
        unknown instead ("Checking...") until it moves or that rule makes it NOT MONITORED. Only before
        the first move, so a PC whose clock is off never loses a P that is being written. P's judged
        counter and judged_at count, or for an older file its counter and written_at."""
        value, at = (p.counter, p.written_at) if p.judged is None else (p.judged, p.judged_at)
        seen = self._counters.get(machine_id)
        if seen is None or (seen[0] != value and not seen[1]):
            seen = self._counters[machine_id] = [value, seen is not None]
        if seen[1]:
            return False
        return age < self.timings.not_monitored_s and self.clock.time() - at >= self.timings.not_monitored_s

    def _read_share(self, data, machines):
        """Fresh reads into the last-good caches; False when the share went away part-way."""
        store = self.store
        seen = self.poller.seen_polls()
        now = self.clock.monotonic()
        placeholder = lambda m: PollStatus(machine_id=m.id, printer_id=m.printer_id, poller="", state=MachineSnapshot(
            code="update_stripes", colour=GREY, headline=Msg(hx_state.STATE_TEXTS["update_stripes"]), since=0.0))
        try:
            written = self.poller._written_polls()
            for m in machines:
                s = seen.get(m.id)
                if s is not None and now - s[1] < self.timings.heartbeat_s:
                    p = s[0]
                    own = written.get(m.id)
                    if own is not None and own.machine_id == m.id and (p is None or own.counter > p.counter):
                        p = own     # this PC's worker wrote P since the coordinator read it (a start, say)
                else:
                    try:
                        p = store.read_poll(m.id)
                    except FormatTooNew:
                        p = placeholder(m)
                try:
                    e = store.read_expected(m.id)
                except FormatTooNew:
                    e, p = None, placeholder(m)
                self._polls[m.id] = p
                self._expected[m.id] = e
                self._locks[m.printer_id] = store.read_lock(m.printer_id)
            for line in data.lines.values():
                if line.active:
                    try:
                        self._lines[line.id] = store.read_line_status(line.id)
                    except FormatTooNew:
                        self._lines[line.id] = None
            for p in self._polls.values():
                if p is None:
                    continue
                for a in p.alerts:
                    if a.active and not a.acked_by and a.id not in self._found_acks:
                        try:
                            ack = store.read_ack(a.id)
                        except ValueError:
                            ack = None
                        if ack is not None:
                            self._found_acks[a.id] = ack
        except ShareUnavailable:
            return False
        except ShareError:
            return store.available
        return True

    def _write_acks(self):
        with self._lock:
            queue, self._ack_queue = self._ack_queue, []
        keep = []
        for ack, give_up in queue:
            if self.clock.monotonic() >= give_up:
                continue
            try:
                self.store.write_ack(ack)
            except ValueError:
                continue
            except (ShareUnavailable, ShareError):
                keep.append((ack, give_up))
        if keep:
            with self._lock:
                self._ack_queue = keep + self._ack_queue


def _with_acks(board, acks):
    """The board with this PC's acks applied to every alert, at once (§4.5 item 2)."""
    lines = []
    for line in board.lines:
        machines = []
        for m in line.machines:
            alerts = [dataclasses.replace(a, acked_by=acks[a.id].by, acked_at=acks[a.id].at)
                      if a.id in acks and not a.acked_by else a for a in m.alerts]
            machines.append(dataclasses.replace(m, alerts=alerts,
                                                unacked_sound=any(a.sound and not a.acked_by for a in alerts)))
        lines.append(dataclasses.replace(line, machines=machines))
    return dataclasses.replace(board, lines=lines)
