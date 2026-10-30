"""Tests for hx_ui_send: the Send window, the result and confirmation window, the red failure window and
the Stop, Resume and Set clocks dialogs. Run: xvfb-run -a python -m unittest test_hx_ui_send -v

Every test drives FakeWorld printers through the real pipeline and share code, with scaled timings.
The services are built here the way hx_ui.make_services builds them, with a stub Navigator, so these
tests don't depend on hx_ui. The Tk tests need a display; they skip themselves when Tk can't start.
"""

import dataclasses
import gc
import itertools
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import tkinter as tk
import unittest
from dataclasses import dataclass
from unittest import mock

from PIL import Image, ImageDraw

import hx_message
import hx_send
import hx_share
import hx_ui_common
import hx_ui_send
import i18n
from hx_core import EventBus, Msg
from hx_fake import FakeWorld
from hx_model import (ALLOWED_WRITE_TAGS, AMBER, BLUE, GREEN, GREY, RED, STEP_CHECK_MACHINE, STEP_CONFIRM_2,
                      STEP_LOCK, STEP_VERIFY_IDLE, STEP_WAIT_POLLER, TIMINGS, ActionEvent, ActionFinished, ActionResult,
                      BoardUpdate, BoardView, Film, Line, LineChoice, LineView, MachineResult, MachineView, PollStatus,
                      PositionWorking, PrintText, PrintTextItem, ResumeCheck, SendEvent, SendFinished, SendRequest,
                      SendResult, ShareHealth, new_id)
from hx_protocol import ClientPool, GateRegistry, release_key
from hx_ui_common import COLOUR_HEX, DIM_FG, HIGHLIGHT_BG, BagText, PhotoWindow, SyncBackground, WorkFailed
from hx_ui_send import FailureWindow, ResultWindow, ResumeDialog, SendWindow, SetClocksDialog, StopDialog

# ui.md §17.1's scaled timings, with printer and share timeouts that leave room for a busy machine. The
# lock's stale time stays well above twice the longest printer call, as the lock budget needs (storage.md
# §11.3), so a slow test machine never judges a lock lost
SCALED = dataclasses.replace(TIMINGS.scaled(0.05), http_timeout_s=0.5, web_repeat_window_s=0.5, share_timeout_s=1.0,
                             lock_stale_s=30.0)


# --------------------------------------------------------------------------
# Services and a stub Navigator (architecture §6.10, ui.md §15.1 and §15.3)
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class Services:
    """What hx_ui.HxServices holds, for the screens under test."""
    version: str
    clock: object
    timings: object
    bus: EventBus
    bg: object
    identity: object
    paths: object
    store: object
    local: object
    data: object
    clients: object
    time: object
    poller: object
    watcher: object
    settings: object
    nav: object

    def _parts(self):
        return dict(store=self.store, data=self.data, clients=self.clients, time=self.time, identity=self.identity,
                    clock=self.clock, timings=self.timings)

    def pipeline(self):
        return hx_send.SendPipeline(**self._parts())

    def actions(self):
        return hx_send.PrinterActions(**self._parts())

    def test_run(self, plan):
        return hx_send.TestPrintRun(plan=plan, **self._parts())


class Watcher:
    """StatusWatcher.latest: the boards here are made by hand (ui.md §17.1 item 6)."""
    latest = None


class StubNav:
    """The Navigator interface the send screens use, opening the real windows of hx_ui_send."""

    def __init__(self, root):
        self.root = root
        self.services = None
        self.windows = {}
        self.calls = []
        self.offline_sends = []
        self.offline_answers = {}

    def window(self, key):
        w = self.windows.get(key)
        try:
            return w if w is not None and w.winfo_exists() else None
        except tk.TclError:
            return None

    def open_send(self, *, prefill=None, line_ids=None):
        self.calls.append(("open_send", prefill, line_ids))
        w = self.window("send")
        if w is None:
            return SendWindow(self.root, self.services, prefill=prefill, line_ids=line_ids)
        if w.page != "choose":
            hx_ui_common.tell(w, Msg("A Send is running. Wait for it to finish."))
            return w
        if prefill is not None:
            w.fill(prefill)
        elif line_ids:
            w.tick_lines(line_ids)
        return w

    def open_result(self, send_id):
        self.calls.append(("open_result", send_id))
        return ResultWindow.from_log(self.root, self.services, send_id)

    def open_failure(self, result, plan):
        self.calls.append(("open_failure", result.send_id))
        w = self.window(f"failure:{result.send_id}")
        if w is not None:
            w.show()
            return w
        return FailureWindow(self.root, self.services, result, plan)

    def stop(self, machine_ids, *, reason="user", send_id=""):
        return StopDialog(self.root, self.services, machine_ids=machine_ids, reason=reason, send_id=send_id)

    def resume(self, machine_ids):
        return ResumeDialog(self.root, self.services, machine_ids=machine_ids)

    def set_clocks(self, line_ids):
        return SetClocksDialog(self.root, self.services, line_ids=line_ids)

    def note_offline_send(self, send_id):
        self.offline_sends.append(send_id)

    def note_offline_answer(self, send_id, machine_ids, everything_right):
        self.offline_answers[send_id] = (list(machine_ids), everything_right)

    def update_monitor_flag(self):
        pass

    def floor_board_open(self):
        return False


def build_services(root, world, identity, *, timings=SCALED):
    """hx_ui.make_services' wiring on the world's share, with this identity and a stub Navigator."""
    bus = EventBus()
    store = world.store(identity)
    paths = world.paths(identity)
    local = hx_share.LocalStore(paths)
    data = hx_share.DataService(store=store, local=local, bus=bus, identity=identity, timings=timings)
    clients = ClientPool(key=release_key(), gates=GateRegistry(), timings=timings)
    time_source = world.time_source()
    time_source.timeout_s = 0.2
    nav = StubNav(root)
    services = Services(version="test", clock=world.clock, timings=timings, bus=bus, bg=SyncBackground(),
                        identity=identity, paths=paths, store=store, local=local, data=data, clients=clients,
                        time=time_source, poller=None, watcher=Watcher(), settings=None, nav=nav)
    nav.services = services
    hx_ui_common.setup_styles(root)
    data.run_once()
    bus.pump()
    return services


# --------------------------------------------------------------------------
# Hand-made boards, printer hooks and widget helpers
# --------------------------------------------------------------------------

def mview(mid, line_id, name, *, origin="Product of USA", code="printing", colour=GREEN, printing=True, **kw):
    return MachineView(machine_id=mid, line_id=line_id, name=name, colour=colour, code=code,
                       headline=Msg("Printing" if code == "printing" else "Printer off"), origin_line=origin,
                       printing=printing, monitored=True, checked_age_s=2.0, **kw)


def board_of(line_id, *views):
    return BoardView(lines=[LineView(line_id=line_id, name="Line 1", colour=GREEN, headline=Msg("Printing"),
                                     machines=list(views))], share_ok=True, generated_at=time.time())


def hook_load(fake, slot, nth, change):
    """Changes the nth Load reply of slot with change(body) (the idle-slot read-back for slot 9)."""
    seen = itertools.count(1)
    mark = b"<MSNO>%d</MSNO>" % slot

    def hook(body):
        if b"<TYPE>Load</TYPE>" in body and mark in body and next(seen) == nth:
            return change(body)
        return body
    fake.web_reply_hook = hook


def change_lot(body):
    return body.replace(b"<TXT>Lot 4417</TXT>", b"<TXT>Lot 4418</TXT>")


def shown(widget):
    """Packed or gridded (the root is withdrawn, so nothing is ever mapped)."""
    return widget.winfo_manager() != ""


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def label_texts(widget):
    return [w.cget("text") for w in descendants(widget) if w.winfo_class() in ("TLabel", "Label") and shown(w)]


def alive(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


@dataclass
class SimpleFilm:
    id: str
    code: str
    artwork_revision: str


def tall_photo(path, size, colour):
    """A made-up bag photo drawn with Pillow: a coloured bag with a white label."""
    img = Image.new("RGB", size, colour)
    ImageDraw.Draw(img).rectangle((size[0] // 8, size[1] // 10, size[0] * 7 // 8, size[1] // 4), fill="white")
    img.save(path, "JPEG", quality=90)
    return path


class UiCase(unittest.TestCase):
    """ui.md §17.1: a FakeWorld, services on its share, strict threads, and tell/ask patched on
    hx_ui_common."""
    root = None
    printers = [{}]

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest(f"Tk can't start: {e}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        for window in hx_ui_common.open_windows():
            window.destroy()
        cls.root.destroy()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hx_ui_send_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.world = FakeWorld(self.tmp, printers=self.printers, timings=SCALED)
        self.addCleanup(self.world.close)
        hx_ui_common.STRICT_THREADS = True
        # As in the app (architecture §7.1 rule 3): garbage is collected on the Tk thread only, so a
        # worker thread never frees a Tk object
        gc.disable()
        self.addCleanup(gc.enable)
        self.tell = self.patch(hx_ui_common, "tell")
        self.ask = self.patch(hx_ui_common, "ask", return_value=True)
        self.services = self.make()

    def tearDown(self):
        hx_ui_common.STRICT_THREADS = False
        i18n.set_language("en")
        for window in hx_ui_common.open_windows():
            window.destroy()
        for child in self.root.winfo_children():
            child.destroy()
        gc.collect()
        for fake in self.world.printers:
            self.assertLessEqual(set(fake.writes), set(ALLOWED_WRITE_TAGS))
            self.assertLessEqual(fake.max_in_flight, 1)
            self.assertEqual(fake.violations, [])

    def patch(self, target, name, **kw):
        patcher = mock.patch.object(target, name, **kw)
        found = patcher.start()
        self.addCleanup(patcher.stop)
        return found

    def make(self, name="jsmith", *, admin=True, sender=True, timings=SCALED):
        services = build_services(self.root, self.world, self.world.identity(name, admin=admin, sender=sender),
                                  timings=timings)
        for n in range(1, len(self.world.printers) + 1):
            # This PC "polls" the printers itself, so a Send's handshake answers at once
            services.store.write_poll(PollStatus(machine_id=self.mid(n), printer_id=self.world.ids[f"printer{n}"],
                                                 poller=services.identity.key, counter=1))
        return services

    def mid(self, n=1):
        return self.world.ids[f"machine{n}"]

    @property
    def line_id(self):
        return self.world.ids["line"]

    def pump(self, until, timeout_s=15):
        deadline = time.monotonic() + timeout_s
        while not until():
            if time.monotonic() > deadline:
                self.fail("timed out")
            self.root.update()
            self.services.bus.pump()
            time.sleep(0.01)

    def settle(self, seconds=0.05):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            self.services.bus.pump()
            time.sleep(0.01)

    def choice(self, machines=(1,), countries=("USA", "Canada"), lot="4417"):
        ids = self.world.ids
        return LineChoice(line_id=ids["line"], machine_ids=[self.mid(n) for n in machines], product_id=ids["product"],
                          film_id=ids["film"], print_text_id=ids["print_text"], lot=lot, countries=list(countries))

    def request(self, *choices, services=None):
        identity = (services or self.services).identity
        return SendRequest(send_id=new_id("sd"), sender=identity.user, computer=identity.computer,
                           created_at=time.time(), lines=list(choices) or [self.choice()])

    def send_window(self, machines=(1,)):
        """A Send window with a complete, valid choice for Line 1."""
        w = SendWindow(self.root, self.services, prefill=self.request(self.choice(machines)))
        block = w.blocks[self.line_id]
        if block.unticked_warnings:
            block.two_origins_check.invoke()        # the machines left out may still print
        self.assertTrue(w.send_button.instate(["!disabled"]), label_texts(w))
        return w

    def window_of(self, prefix):
        found = [w for k, w in self.services.nav.windows.items() if k.startswith(prefix) and alive(w)]
        return found[0] if found else None

    def good_send(self, machines=(1,)):
        """A Send that goes live; returns the result window."""
        w = self.send_window(machines)
        w.send_button.invoke()
        self.pump(lambda: self.window_of("result:") is not None)
        self.assertFalse(alive(w))
        return self.window_of("result:")


# --------------------------------------------------------------------------
# No Tk needed
# --------------------------------------------------------------------------

class PureTests(unittest.TestCase):
    def test_origin_preview_matches_plan(self):
        tmp = tempfile.mkdtemp(prefix="hx_ui_send_")
        self.addCleanup(shutil.rmtree, tmp, True)
        world = FakeWorld(tmp, timings=SCALED)
        self.addCleanup(world.close)
        identity = world.identity("jsmith", sender=True)
        store = world.store(identity)
        data_service = hx_share.DataService(store=store, local=hx_share.LocalStore(world.paths(identity)),
                                            bus=EventBus(), identity=identity, timings=SCALED)
        data_service.run_once()
        data = data_service.current
        ids = world.ids

        def plan_of(data, film_id, pt_id, countries):
            request = SendRequest(send_id=new_id("sd"), sender=identity.user, computer=identity.computer,
                                  created_at=time.time(), lines=[LineChoice(
                                      line_id=ids["line"], machine_ids=[ids["machine1"]], product_id=ids["product"],
                                      film_id=film_id, print_text_id=pt_id, lot="4417", countries=list(countries))])
            return hx_message.merge_plan(request, data, identity)

        film = data.films[ids["film"]]
        for countries in (["USA"], ["USA", "Canada"], ["Mexico", "USA", "Canada"]):
            plan = plan_of(data, film.id, ids["print_text"], countries)
            self.assertEqual(hx_ui_send.origin_preview(film, countries), plan.machines[0].origin_line)
        self.assertEqual(hx_ui_send.origin_preview(film, []), "")
        # A pre-printed film with a print text that has no origin item
        pt = PrintText(id=new_id("pt"), revision=1, name="Packed on + lot", code="PKL", status="active",
                       items=[PrintTextItem(id="i1", kind="packed_date", date_format="MM/DD/YY", before="Packed on "),
                              PrintTextItem(id="i2", kind="lot", before="Lot ", placement="below")])
        pre = dataclasses.replace(film, id=new_id("fm"), code="EXCO2", origin_preprinted=True,
                                  preprinted_origin="USA", print_text_ids=[pt.id], default_print_text_id=pt.id)
        data2 = dataclasses.replace(data, films={**data.films, pre.id: pre},
                                    print_texts={**data.print_texts, pt.id: pt})
        plan = plan_of(data2, pre.id, pt.id, [])
        self.assertEqual(plan.machines[0].origin_line, "USA (pre-printed)")
        self.assertEqual(hx_ui_send.origin_preview(pre, []), plan.machines[0].origin_line)
        self.assertEqual(hx_ui_send.origin_preview(pre, ["Canada"]), "USA (pre-printed)")

    def test_films_by_code_then_revision(self):
        # As the admin list shows them (films_for_line orders by revision first)
        films = [SimpleFilm("fm_1", "EXCO2", "Rev B"), SimpleFilm("fm_2", "EXCO10", "Rev A"),
                 SimpleFilm("fm_3", "EXCO1", "Rev D"), SimpleFilm("fm_4", "EXCO1", "Rev C"),
                 SimpleFilm("fm_5", "EXCO3", "")]
        self.assertEqual([(f.code, f.artwork_revision) for f in hx_ui_send.sorted_films(films)],
                         [("EXCO1", "Rev C"), ("EXCO1", "Rev D"), ("EXCO2", "Rev B"), ("EXCO3", ""),
                          ("EXCO10", "Rev A")])

    def test_placement_text(self):
        delay = PositionWorking(placement="delay", film_value=480.0, adjustment=0.0, sent=480000,
                                text="480 + 0 = 480 ms = 480000 µs")
        self.assertEqual(hx_ui_common.text(hx_ui_send.placement_text(delay)), "Delay 480 ms")
        adjusted = PositionWorking(placement="delay", film_value=480.5, adjustment=20.0, sent=500500,
                                   text="480.5 + 20 = 500.5 ms = 500500 µs")
        self.assertEqual(hx_ui_common.text(hx_ui_send.placement_text(adjusted)), "Delay 500.5 ms")
        encoder = PositionWorking(placement="encoder", film_value=150.0, adjustment=-3.0, head_distance_mm=412.0,
                                  film_length_mm=610.0, sent=345, wrapped=True, text="(150 - 412 - 3) mod 610 = 345 mm")
        self.assertEqual(hx_ui_common.text(hx_ui_send.placement_text(encoder)), "Print position 147 mm")
        try:
            i18n.set_language("es")
            self.assertEqual(hx_ui_common.text(hx_ui_send.placement_text(delay)), "Retardo de 480 ms")
            self.assertEqual(hx_ui_common.text(hx_ui_send.placement_text(encoder)), "Posición de impresión de 147 mm")
        finally:
            i18n.set_language("en")

    def test_joined_names_follow_the_language(self):
        joined = hx_ui_send._joined(["Line 1 Bagger 1", "Line 1 Bagger 2", "Line 1 Bagger 3"])
        self.assertEqual(hx_ui_common.text(Msg("Stop printing on {machines}?", {"machines": joined})),
                         "Stop printing on Line 1 Bagger 1, Line 1 Bagger 2 and Line 1 Bagger 3?")
        self.assertEqual(hx_ui_send._joined(["Line 1 Bagger 1"]), "Line 1 Bagger 1")
        try:
            i18n.set_language("es")
            self.assertEqual(hx_ui_common.text(Msg("Stop printing on {machines}?", {"machines": joined})),
                             "¿Detener la impresión en Line 1 Bagger 1, Line 1 Bagger 2 y Line 1 Bagger 3?")
        finally:
            i18n.set_language("en")


# --------------------------------------------------------------------------
# The Send window
# --------------------------------------------------------------------------

class SendWindowTests(UiCase):
    def test_send_flow(self):
        services, fake, mid = self.services, self.world.printers[0], self.mid()
        ids = self.world.ids
        services.watcher.latest = board_of(self.line_id, mview(mid, self.line_id, "Line 1 Bagger 1"))
        w = SendWindow(self.root, services)
        self.assertFalse(w.send_button.instate(["!disabled"]))
        w.line_checks[self.line_id].invoke()
        block = w.blocks[self.line_id]
        self.assertEqual(block.machine_vars[mid].get(), True)          # ticked by default
        block.product_box.set_record_id(ids["product"])
        block.product_box.event_generate("<<ComboboxSelected>>")
        self.assertEqual(block.film_var.get(), "")                     # no film is pre-selected (plan)
        self.assertIn(ids["film"], block.film_radios)
        self.assertEqual(block.film_radios[ids["film"]].cget("text"), "EXCO1 · Rev C")
        block.film_radios[ids["film"]].invoke()
        self.assertEqual(block.print_text_box.record_id(), ids["print_text"])   # the film's default
        block.lot_entry.insert(0, "4417")
        for country in ("USA", "Canada"):
            block.country_box.set(country)
            block.add_country_button.invoke()
        self.assertEqual(block.countries, ["USA", "Canada"])
        self.assertEqual(list(block.country_list.get(0, "end")), ["USA", "Canada"])
        self.assertEqual(block.origin_text.get(), "Product of USA and Canada")
        self.assertEqual(block.change_texts[mid].get(), "USA → USA and Canada")
        self.assertEqual(block.change_texts[mid].cget("background"), HIGHLIGHT_BG)
        self.assertIn("Lot 4417", block.bag_texts[mid].get())
        self.assertIn("480", block.position_labels[mid].cget("text"))
        self.assertEqual(w.send_button.cget("text"), "Send to 1 machine")
        self.assertTrue(w.send_button.instate(["!disabled"]))
        reads = []
        services.bus.subscribe(SendEvent, lambda e: reads.append(e) if e.step == STEP_CONFIRM_2 and e.design else None)
        w.send_button.invoke()
        self.assertEqual(w.page, "countdown")
        send_id = w.plan.send_id
        self.assertEqual(w.countdown_origins[self.line_id].get(), "Product of USA and Canada")
        self.assertIn(f"send:{send_id}", hx_ui_common._running)
        self.pump(lambda: services.nav.window(f"result:{send_id}") is not None)
        self.assertFalse(alive(w))
        self.assertNotIn(f"send:{send_id}", hx_ui_common._running)
        rw = services.nav.window(f"result:{send_id}")
        self.assertEqual(rw.pending, [mid])
        self.assertTrue(reads)
        self.assertEqual(rw.pictures[mid].bmp, reads[-1].design.preview_bmp)        # the second running read
        self.assertEqual(rw.origins[mid].get(), "Product of USA and Canada")
        self.assertIs(rw.request.lines[0].lot, rw.request.lines[0].lot)
        self.assertEqual(rw.request.lines[0].lot, "4417")
        self.assertEqual(rw.line_state[self.line_id], "pending")
        rw.right_buttons[self.line_id].invoke()
        self.assertEqual(rw.line_state[self.line_id], "confirmed")
        self.assertNotIn(self.line_id, rw.right_buttons)
        state = services.store.read_expected(mid)
        self.assertTrue(state.confirmed)
        self.assertEqual(state.send_id, send_id)
        self.assertLessEqual(set(fake.writes), set(ALLOWED_WRITE_TAGS))
        self.assertEqual(fake.max_in_flight, 1)
        self.assertEqual(fake.violations, [])
        # Nothing is waiting any more: closing says nothing
        self.tell.reset_mock()
        rw.close()
        self.tell.assert_not_called()
        self.assertFalse(alive(rw))

    def test_send_blocked_banners(self):
        services = self.services
        data = services.data.current
        services.data.current = dataclasses.replace(data, source="local", reached_at=time.time() - 600)
        w = self.send_window()
        self.assertTrue(shown(w.local_banner))
        self.assertIn("Using this PC's copy of the templates, as of", w.local_label.cget("text"))
        self.assertFalse(shown(w.blocked_banner))
        # Past the 24-hour rule: blocked, and Send can't be pressed
        services.data.current = dataclasses.replace(data, source="local", reached_at=time.time() - 2 * 86_400)
        services.bus.post(ShareHealth(ok=False, last_reached=None))
        self.settle()
        self.assertTrue(shown(w.blocked_banner))
        self.assertIn("since", w.blocked_label.cget("text"))
        self.assertFalse(w.send_button.instate(["!disabled"]))
        probe = self.patch(services.store, "probe_now")
        reload = self.patch(services.data, "reload")
        w.retry_button.invoke()
        probe.assert_called_once_with()
        reload.assert_called_once_with()
        # Back within the rule: the banners go
        services.data.current = data
        services.bus.post(ShareHealth(ok=True, last_reached=time.time()))
        self.settle()
        self.assertFalse(shown(w.blocked_banner) or shown(w.local_banner))
        self.assertTrue(w.send_button.instate(["!disabled"]))

    def test_queue_and_problems(self):
        services = self.services
        self.patch(services.data, "pending_logs", return_value=2)
        services.data.problems = [Msg("MapleJet isn't set up on the server yet.")]
        w = SendWindow(self.root, services)
        self.settle()
        self.assertEqual(w.queue_label.cget("text"), "2 log entries waiting to upload.")
        self.assertTrue(shown(w.queue_label))
        self.assertIn("MapleJet isn't set up on the server yet.", w.problems_label.cget("text"))
        services.data.pending_logs.return_value = 1
        w._load_queue()
        self.assertEqual(w.queue_label.cget("text"), "1 log entry waiting to upload.")
        services.data.pending_logs.return_value = 0
        w._load_queue()
        self.assertFalse(shown(w.queue_label))

    def test_countdown_cancel_sends_nothing(self):
        fake = self.world.printers[0]
        fake.reset_observations()
        w = self.send_window()
        w.send_button.invoke()
        w.cancel_button.invoke()
        self.assertEqual(w.countdown_caption.cget("text"), "Cancelling...")
        self.assertFalse(w.cancel_button.instate(["!disabled"]))
        self.pump(lambda: w.page == "choose")
        self.assertEqual(w.send_status_label.cget("text"), "Cancelled. Nothing was sent.")
        self.assertTrue(shown(w.send_status_label))
        self.assertEqual(fake.writes, [])
        self.assertIsNone(self.window_of("result:"))
        self.assertIsNone(self.window_of("failure:"))
        # Every choice is kept, so the sender can send again at once
        self.assertEqual(w.blocks[self.line_id].countries, ["USA", "Canada"])
        self.assertTrue(w.send_button.instate(["!disabled"]))
        self.assertEqual(hx_ui_common.running(), [])

    def test_cancel_disappears_at_go(self):
        services = self.services
        services.timings = dataclasses.replace(SCALED, countdown_s=30.0)    # the count can't run out here
        steps = queue.Queue()

        class Scripted:
            def run(self, plan, on_event, cancel=None):
                for event in iter(steps.get, None):
                    on_event(event)

        services.pipeline = lambda: Scripted()
        w = self.send_window()
        w.send_button.invoke()
        send_id, mid = w.plan.send_id, self.mid()
        self.assertEqual((w.page, w.countdown_caption.cget("text")), ("countdown", "Getting ready..."))
        self.assertEqual(w.countdown_label.cget("text"), "30")
        seen = []
        services.bus.subscribe(SendEvent, lambda e: seen.append((e.machine_id, e.step, e.status, w.page,
                                                                 shown(w.cancel_button))))

        def post(event):
            steps.put(event)
            self.pump(lambda: seen and seen[-1][:3] == (event.machine_id, event.step, event.status))

        post(SendEvent(send_id=send_id, step=STEP_LOCK, status="running", at=time.time()))
        self.assertEqual(w.countdown_caption.cget("text"), "Sending in")
        post(SendEvent(send_id=send_id, machine_id=mid, step=STEP_WAIT_POLLER, status="running"))
        self.assertEqual(seen[-1][3:], ("countdown", True))
        self.settle(0.3)                    # the 200 ms count keeps going
        self.assertIn(w.countdown_label.cget("text"), ("29", "30"))
        post(SendEvent(send_id=send_id, step=STEP_LOCK, status="ok"))
        self.assertEqual(seen[-1][3:], ("working", False))
        self.assertTrue(shown(w.working_label))
        self.assertEqual(w.working_label.cget("text"), "Hang on, we're working on it")
        post(SendEvent(send_id=send_id, machine_id=mid, step=STEP_CHECK_MACHINE, status="running"))
        self.assertEqual(w.progress_rows[mid].step_label.cget("text"), "Checking the machine")
        self.assertEqual(w.progress_rows[mid].mark_label.cget("text"), "…")
        # While it works, the window can't be closed
        w.close()
        self.assertEqual(self.tell.call_args[0][1].text, "The Send is running. Wait for it to finish.")
        self.assertTrue(alive(w))
        steps.put(None)
        services.bus.post(WorkFailed(ref=send_id, msg=Msg("Stripes hit a problem: {error}", {"error": "X"})))
        self.pump(lambda: w._work_failed)
        self.assertIn("Stripes hit a problem during the Send.", w.work_problem_label.cget("text"))
        self.assertEqual(hx_ui_common.running(), [])
        w.close()
        self.assertFalse(alive(w))

    def test_countdown_close_and_choice_close(self):
        services = self.services
        services.timings = dataclasses.replace(SCALED, countdown_s=30.0)
        steps = queue.Queue()

        class Scripted:
            def run(self, plan, on_event, cancel=None):
                for event in iter(steps.get, None):
                    on_event(event)

        services.pipeline = lambda: Scripted()
        w = self.send_window()
        w.send_button.invoke()
        w.close()
        self.assertEqual(self.tell.call_args[0][1].text, "Press Cancel to stop the Send.")
        self.assertTrue(alive(w))
        # A refusal before any printer request: back to the choices, with the reason
        steps.put(None)
        services.bus.post(WorkFailed(ref=w.plan.send_id, msg=Msg("The templates changed since you checked them. "
                                                                 "Look at the Send window again.")))
        self.pump(lambda: w.page == "choose")
        self.assertIn("The templates changed", w.problems_label.cget("text"))
        self.assertEqual(w.blocks[self.line_id].lot_entry.get(), "4417")
        # Closing with choices asks first
        self.ask.return_value = False
        w.close()
        self.assertEqual(self.ask.call_args[0][1].text, "Close the Send window? Your choices will be lost.")
        self.assertEqual(self.ask.call_args[1], {"yes": "Close", "no": "Keep it open"})
        self.assertTrue(alive(w))
        self.ask.return_value = True
        w.close()
        self.assertFalse(alive(w))

    def test_unticked_machine_warning(self):
        self.world.close()
        self.world = FakeWorld(tempfile.mkdtemp(dir=self.tmp), printers=[{}, {}], timings=SCALED)
        self.addCleanup(self.world.close)
        self.services = services = self.make()
        mid1, mid2 = self.mid(1), self.mid(2)
        services.watcher.latest = board_of(self.line_id, mview(mid1, self.line_id, "Line 1 Bagger 1"),
                                           mview(mid2, self.line_id, "Line 1 Bagger 2"))
        w = SendWindow(self.root, services, prefill=self.request(self.choice(machines=(1, 2), countries=("USA",))))
        block = w.blocks[self.line_id]
        self.assertEqual(w.send_button.cget("text"), "Send to 2 machines")
        block.machine_checks[mid2].invoke()
        self.assertFalse(block.machine_vars[mid2].get())
        self.assertEqual(block.unticked_warnings, {})                   # Bagger 2 prints the same origin
        self.assertFalse(shown(block.two_origins_check))
        self.assertTrue(w.send_button.instate(["!disabled"]))
        self.assertEqual(w.send_button.cget("text"), "Send to 1 machine")
        block.country_box.set("Canada")
        block.add_country_button.invoke()
        self.assertEqual(list(block.unticked_warnings), [mid2])
        row = block.unticked_warnings[mid2]
        self.assertEqual(row.cget("background"), HIGHLIGHT_BG)
        bags = [c for c in row.winfo_children() if isinstance(c, BagText)]
        self.assertEqual([b.get() for b in bags], ["Product of USA"])
        texts = [c.cget("text") for c in row.winfo_children() if not isinstance(c, BagText)]
        self.assertEqual(texts, ["Line 1 Bagger 2 isn't ticked and is still printing:",
                                 "This line will print two origins."])
        self.assertTrue(shown(block.two_origins_check))
        self.assertFalse(w.send_button.instate(["!disabled"]))
        block.two_origins_check.invoke()
        self.assertTrue(w.send_button.instate(["!disabled"]))
        # The same origin again: no warning, and the tick is taken back
        block.country_list.selection_set(1)
        block.remove_country_button.invoke()
        self.assertEqual(block.unticked_warnings, {})
        self.assertFalse(block.two_origins_var.get())
        # An unknown origin warns too, with "(origin unknown)" beside it
        services.bus.post(BoardUpdate(board=board_of(self.line_id, mview(mid1, self.line_id, "Line 1 Bagger 1"),
                                                     mview(mid2, self.line_id, "Line 1 Bagger 2", origin=""))))
        self.settle()
        self.assertEqual(list(block.unticked_warnings), [mid2])
        self.assertIn("(origin unknown)", [c.cget("text") for c in block.unticked_warnings[mid2].winfo_children()
                                           if not isinstance(c, BagText)])
        self.assertFalse(w.send_button.instate(["!disabled"]))
        # Bagger 2 off: no warning
        services.bus.post(BoardUpdate(board=board_of(self.line_id, mview(mid1, self.line_id, "Line 1 Bagger 1"),
                                                     mview(mid2, self.line_id, "Line 1 Bagger 2", code="off",
                                                           colour=GREY, printing=False))))
        self.settle()
        block.country_box.set("Canada")
        block.add_country_button.invoke()
        self.assertEqual(block.unticked_warnings, {})
        self.assertTrue(w.send_button.instate(["!disabled"]))
        self.assertEqual(block.state_labels[mid2].cget("text"), "Printer off")

    def test_not_set_up_machine_is_unticked(self):
        services = self.services
        mid = self.mid()
        services.watcher.latest = board_of(self.line_id, mview(mid, self.line_id, "Line 1 Bagger 1",
                                                               code="not_set_up", colour=GREY, origin=""))
        w = SendWindow(self.root, services, prefill=self.request())
        block = w.blocks[self.line_id]
        self.assertFalse(block.machine_vars[mid].get())
        self.assertTrue(block.machine_checks[mid].instate(["disabled"]))
        self.assertFalse(w.send_button.instate(["!disabled"]))
        self.assertIn("Tick at least one machine on Line 1.", block.problems_label.cget("text"))
        self.assertTrue(block.problems_label.cget("text").startswith("Still needed:"))

    def test_fill_keeps_lines_and_notes_inactive_ones(self):
        services = self.services
        w = SendWindow(self.root, services)
        request = self.request(self.choice(), LineChoice(line_id="ln_0000000000", machine_ids=[], product_id="",
                                                         film_id="", print_text_id=""))
        w.fill(request)
        self.assertEqual(list(w.blocks), [self.line_id])
        self.assertIn("ln_0000000000 isn't active any more.", w.problems_label.cget("text"))
        block = w.blocks[self.line_id]
        self.assertEqual((block.lot_entry.get(), block.countries), ("4417", ["USA", "Canada"]))
        self.assertEqual(w.request().lines[0].countries, ["USA", "Canada"])
        # Unticking keeps the choice; ticking again restores it
        w.line_checks[self.line_id].invoke()
        self.assertEqual(w.blocks, {})
        self.assertIn(self.line_id, w.kept)
        w.line_checks[self.line_id].invoke()
        self.assertEqual(w.blocks[self.line_id].countries, ["USA", "Canada"])
        # tick_lines: those lines, with empty choices
        w.tick_lines([self.line_id])
        self.assertEqual(w.blocks[self.line_id].product_box.record_id(), "")
        self.assertFalse(w.has_choices())

    def test_delay_for_senders_and_the_working_for_administrators(self):
        mid = self.mid()
        w = self.send_window()                  # jsmith is a sender and an administrator
        block = w.blocks[self.line_id]
        self.assertEqual(block.position_labels[mid].cget("text"), "Delay 480 ms")
        self.assertEqual(str(block.position_labels[mid].cget("style")), "Hx.Small.TLabel")
        self.assertEqual(block.working_labels[mid].cget("text"), "480 + 0 = 480 ms = 480000 µs")
        self.assertEqual(str(block.working_labels[mid].cget("style")), "Hx.Small.TLabel")
        w.destroy()
        # A sender who isn't an administrator sees the delay only
        akim = self.make("akim", admin=False, sender=True)
        w = SendWindow(self.root, akim, prefill=self.request(self.choice(), services=akim))
        block = w.blocks[self.line_id]
        self.assertTrue(w.send_button.instate(["!disabled"]))
        self.assertEqual(block.position_labels[mid].cget("text"), "Delay 480 ms")
        self.assertEqual(block.working_labels, {})
        self.assertFalse([t for t in label_texts(w) if "µs" in t or " = " in t])
        i18n.set_language("es")
        w.retranslate()
        self.assertEqual(block.position_labels[mid].cget("text"), "Retardo de 480 ms")

    def test_language_switch_keeps_bag_text(self):
        services = self.services
        services.watcher.latest = board_of(self.line_id, mview(self.mid(), self.line_id, "Line 1 Bagger 1"))
        w = self.send_window()
        block = w.blocks[self.line_id]
        bags = [(b, b.get()) for b in descendants(w) if isinstance(b, BagText)]
        self.assertIn("Product of USA and Canada", [t for _, t in bags])
        i18n.set_language("es")
        w.retranslate()
        self.assertEqual(w.send_button.cget("text"), "Enviar a 1 máquina")
        self.assertEqual([(b, b.get()) for b, _ in bags], bags)
        self.assertEqual([b.itemcget(b.text_item, "text") for b, _ in bags], [t for _, t in bags])
        self.assertEqual(block.origin_text.get(), "Product of USA and Canada")
        i18n.set_language("en")
        w.retranslate()
        self.assertEqual(w.send_button.cget("text"), "Send to 1 machine")


# --------------------------------------------------------------------------
# Result, red window, Try again
# --------------------------------------------------------------------------

class FilmCardTests(UiCase):
    """The films of the chosen product as cards with their photos (ui.md §5.3)."""

    def add_films(self):
        """Four films of the product, with Pillow-drawn photos saved the way the film editor saves
        them: EXCO1 Rev C (tall front and back), EXCO10 Rev A (none), EXCO2 Rev B (a wide front only),
        EXCO3 (no revision, none)."""
        services = self.services
        data = services.data
        base = data.current.films[self.world.ids["film"]]
        films = [base,
                 dataclasses.replace(base, id=new_id("fm"), code="EXCO10", artwork_revision="Rev A"),
                 dataclasses.replace(base, id=new_id("fm"), code="EXCO2", artwork_revision="Rev B", side="front"),
                 dataclasses.replace(base, id=new_id("fm"), code="EXCO3", artwork_revision="")]
        photos = {films[0].id: (("front_photo", (600, 1500)), ("back_photo", (560, 1400))),
                  films[2].id: (("front_photo", (900, 600)),)}
        self.refs = {}
        for film in films:
            os.makedirs(os.path.join(self.world.share_root, "templates", "films", film.id), exist_ok=True)
            refs = []
            for kind, size in photos.get(film.id, ()):
                path = tall_photo(os.path.join(self.tmp, f"{film.code} {kind}.jpg"), size, (180, 83, 9))
                ref = data.save_file(film.id, kind, path)
                data.save_preview(film.id, ref.sha256, hx_ui_common.png_preview(path))
                refs.append(ref)
                self.refs[(film.id, kind)] = ref
            hx_share.write_record(services.store, dataclasses.replace(film, files=refs))
        services.store.write_json("config/hx/changes.json", {"file_type": "changes", "counter": 2,
                                                            "written_at": time.time(),
                                                            "written_by": services.identity.user, "last": None})
        data.run_once()
        services.bus.pump()
        return [f.id for f in films]

    def test_film_cards(self):
        services, ids = self.services, self.add_films()
        exco1, exco10, exco2, exco3 = ids
        w = SendWindow(self.root, services)
        w.line_checks[self.line_id].invoke()
        block = w.blocks[self.line_id]
        block.product_box.set_record_id(self.world.ids["product"])
        block.product_box.event_generate("<<ComboboxSelected>>")
        # By code, then revision, as the admin list shows them
        self.assertEqual(list(block.film_radios), [exco1, exco2, exco3, exco10])
        self.assertEqual([r.cget("text") for r in block.film_radios.values()],
                         ["EXCO1 · Rev C", "EXCO2 · Rev B", "EXCO3", "EXCO10 · Rev A"])
        self.assertEqual(list(block.film_cards), list(block.film_radios))
        # The photos load (a worker each), fitted to the box with their shape kept
        self.pump(lambda: w.winfo_width() == 1000 and (exco1, "back_photo") in block._photos
                  and (exco2, "front_photo") in block._photos and block.films_frame.winfo_width() > 1)
        self.settle(0.1)
        front = block._photos[(exco1, "front_photo")][0]
        self.assertEqual((front.width(), front.height()), (60, 150))        # 600 x 1500
        back = block._photos[(exco1, "back_photo")][0]
        self.assertEqual((back.width(), back.height()), (60, 150))          # 560 x 1400
        wide = block._photos[(exco2, "front_photo")][0]
        self.assertEqual((wide.width(), wide.height()), (100, 67))          # 900 x 600
        self.assertEqual(hx_ui_send.THUMB_BOX, (100, 150))
        for key in ((exco2, "back_photo"), (exco3, "front_photo"), (exco3, "back_photo"), (exco10, "front_photo")):
            self.assertEqual(block.thumbs[key].cget("text"), "No photo")
            self.assertEqual(str(block.thumbs[key].cget("image")), "")
        # Four films side by side in a 1000 x 780 window, with the rest of the form in view
        self.assertEqual(block._film_cols, 4)
        self.assertEqual([int(card.grid_info()["row"]) for card in block.film_cards.values()], [0, 0, 0, 0])
        self.assertEqual([int(card.grid_info()["column"]) for card in block.film_cards.values()], [0, 1, 2, 3])
        widths = {card.winfo_width() for card in block.film_cards.values()}
        self.assertEqual(len(widths), 1)                                     # every card as wide
        # Clicking a card (not only its radio button) picks that film; its card is marked
        block.film_cards[exco2].event_generate("<Button-1>", x=2, y=2)
        self.assertEqual(block.film_var.get(), exco2)
        block.film_cards[exco1].winfo_children()[1].event_generate("<Button-1>", x=2, y=2)    # the side text
        self.assertEqual(block.film_var.get(), exco1)
        self.assertEqual(block.print_text_box.record_id(), self.world.ids["print_text"])
        self.assertEqual(block.film_cards[exco1].cget("highlightbackground"), COLOUR_HEX[BLUE])
        self.assertEqual({block.film_cards[f].cget("highlightbackground") for f in (exco2, exco3, exco10)}, {DIM_FG})
        self.settle(0.1)
        canvas = w.scroller.canvas
        fold = canvas.winfo_rooty() + canvas.winfo_height()
        for widget in (block.print_text_box, block.lot_entry, block.countries_frame, block.origin_text):
            self.assertLess(widget.winfo_rooty(), fold, widget)
        self.assertLess(block.lot_entry.winfo_rooty() + block.lot_entry.winfo_height(), fold)
        # A narrow window takes fewer cards to a row
        w.geometry("760x560")
        self.pump(lambda: block._film_cols < 4)
        rows = [int(card.grid_info()["row"]) for card in block.film_cards.values()]
        self.assertEqual(rows[:block._film_cols], [0] * block._film_cols)
        self.assertEqual(rows[-1], 1)
        # Clicking a photo opens it larger, once, and the window reads the original from the share
        thumb = block.thumbs[(exco1, "front_photo")]
        self.assertEqual(str(thumb.cget("cursor")), "hand2")
        thumb.event_generate("<Button-1>", x=2, y=2)
        thumb.event_generate("<Button-1>", x=2, y=2)
        photos = [x for x in hx_ui_common.open_windows() if isinstance(x, PhotoWindow)]
        self.assertEqual(len(photos), 1)
        photo = photos[0]
        self.assertEqual(photo.title(), "EXCO1 · Rev C: Front photo")
        self.assertEqual(photo.original, self.refs[(exco1, "front_photo")])
        self.pump(lambda: photo.source == "original")
        self.assertEqual(photo.image.size, (600, 1500))

    def test_photos_built_again_ignore_a_late_load(self):
        services, ids = self.services, self.add_films()
        exco1 = ids[0]
        w = SendWindow(self.root, services)
        w.line_checks[self.line_id].invoke()
        block = w.blocks[self.line_id]
        late = []
        services.bg = mock.Mock(run=lambda work, done: late.append((work, done)))
        block.product_box.set_record_id(self.world.ids["product"])
        block.product_box.event_generate("<<ComboboxSelected>>")
        self.settle(0.05)
        self.assertTrue(late)
        first = block.thumbs[(exco1, "front_photo")]
        block._build_films()                    # e.g. the product chosen again
        self.assertIsNot(block.thumbs[(exco1, "front_photo")], first)
        for work, done in late:
            done(work(), None)
        self.assertNotIn((exco1, "front_photo"), block._photos)


class WentLiveTests(UiCase):
    """The red window's line about the machines that went live, one or more (ui.md §7.2)."""
    printers = [{}, {}, {}]

    def red_window(self, live):
        services = self.services
        plan = hx_message.merge_plan(self.request(self.choice((1, 2, 3))), services.data.current, services.identity)
        machines = [MachineResult(machine_id=self.mid(n), colour=AMBER if n in live else RED, ok=n in live,
                                  step=STEP_CONFIRM_2 if n in live else STEP_VERIFY_IDLE) for n in (1, 2, 3)]
        result = SendResult(send_id=plan.send_id, sender=services.identity.user, computer=services.identity.computer,
                            started_at=time.time(), finished_at=time.time(), machines=machines, time_source="server")
        return FailureWindow(self.root, services, result, plan)

    def test_one_machine_went_live(self):
        fw = self.red_window({2})
        self.assertEqual(fw.passed_label.cget("text"), "Line 1 Bagger 2 went live. Confirm it in the result window.")
        i18n.set_language("es")
        fw.retranslate()
        self.assertEqual(fw.passed_label.cget("text"),
                         "Line 1 Bagger 2 quedó activa. Confírmela en la ventana de resultados.")

    def test_machines_went_live(self):
        fw = self.red_window({2, 3})
        self.assertEqual(fw.passed_label.cget("text"),
                         "Line 1 Bagger 2 and Line 1 Bagger 3 went live. Confirm them in the result window.")
        i18n.set_language("es")
        fw.retranslate()
        self.assertEqual(fw.passed_label.cget("text"), "Line 1 Bagger 2 y Line 1 Bagger 3 quedaron activas. "
                                                       "Confírmelas en la ventana de resultados.")


class DataChangeTests(UiCase):
    """The Send window while an administrator changes the templates on another PC (ui.md §5.2, §5.9)."""

    def publish(self, *records):
        store = self.world.store(self.world.identity("admin", admin=True, sender=False))
        for record in records:
            hx_share.write_record(store, record)
        raw = store.read_json("config/hx/changes.json")
        store.write_json("config/hx/changes.json", {"file_type": "changes", "counter": raw["counter"] + 1,
                                                    "written_at": time.time(), "written_by": "EXAMPLE\\admin",
                                                    "last": None})
        self.services.data.run_once()
        self.settle()

    def company_with(self, change):
        company = self.services.data.current.company
        countries = [change(c) if c.name == "Canada" else c for c in company.countries]
        return dataclasses.replace(company, countries=countries, revision=company.revision + 1)

    def test_country_taken_off_the_list_stays_and_blocks(self):
        # A chosen country that is no longer offered is never dropped without a word: it stays in the
        # list and problems() names it until the sender removes it
        w = self.send_window()
        block = w.blocks[self.line_id]
        self.assertEqual(block.origin_text.get(), "Product of USA and Canada")
        self.publish(self.company_with(lambda c: dataclasses.replace(c, active=False)))
        block = w.blocks[self.line_id]
        self.assertEqual(block.countries, ["USA", "Canada"])
        self.assertEqual(list(block.country_list.get(0, "end")), ["USA", "Canada"])
        self.assertNotIn("Canada", block.country_box.cget("values"))
        self.assertIn("Canada isn't on the country list.", block.problems_label.cget("text"))
        self.assertFalse(w.send_button.instate(["!disabled"]))
        # The sender takes it out and checks the new origin
        block.country_list.selection_set(1)
        block.remove_country_button.invoke()
        self.assertEqual(block.countries, ["USA"])
        self.assertEqual(block.origin_text.get(), "Product of USA")
        self.assertEqual(block.problems_label.cget("text"), "")
        self.assertTrue(w.send_button.instate(["!disabled"]))

    def test_country_renamed_stays_and_blocks(self):
        w = self.send_window()
        self.publish(self.company_with(lambda c: dataclasses.replace(c, name="Canada (CA)")))
        block = w.blocks[self.line_id]
        self.assertEqual(block.countries, ["USA", "Canada"])
        self.assertIn("Canada isn't on the country list.", block.problems_label.cget("text"))
        self.assertFalse(w.send_button.instate(["!disabled"]))

    def test_prefill_keeps_a_country_no_longer_offered(self):
        # Fix and send again, Try again: the old choice's country shows and is named, not dropped
        self.publish(self.company_with(lambda c: dataclasses.replace(c, active=False)))
        w = SendWindow(self.root, self.services, prefill=self.request(self.choice()))
        block = w.blocks[self.line_id]
        self.assertEqual(block.countries, ["USA", "Canada"])
        self.assertIn("Canada isn't on the country list.", block.problems_label.cget("text"))
        self.assertFalse(w.send_button.instate(["!disabled"]))

    def test_line_made_inactive_while_open(self):
        # A ticked line made inactive loses its check and its block, is noted, and doesn't block the
        # Send to the other lines; made active again, ticking it brings its choice back
        self.world.close()
        self.world = FakeWorld(tempfile.mkdtemp(dir=self.tmp), printers=[{}, {}], timings=SCALED)
        self.addCleanup(self.world.close)
        self.services = services = self.make()
        data = services.data.current
        ids = self.world.ids
        line2 = Line(id="ln_0000000099", revision=1, name="Line 2", code="L2", cartridge="plastic")
        machine2 = data.machines[self.mid(2)]
        self.publish(line2, dataclasses.replace(machine2, line_id=line2.id, revision=machine2.revision + 1))
        self.assertIn(line2.id, services.data.current.lines)
        c2 = LineChoice(line_id=line2.id, machine_ids=[self.mid(2)], product_id=ids["product"], film_id=ids["film"],
                        print_text_id=ids["print_text"], lot="4417", countries=["USA"])
        w = SendWindow(self.root, services, prefill=self.request(self.choice(machines=(1,)), c2))
        self.assertEqual(set(w.blocks), {self.line_id, line2.id})
        self.assertTrue(w.send_button.instate(["!disabled"]), label_texts(w))
        self.publish(dataclasses.replace(line2, active=False, revision=2))
        self.assertNotIn(line2.id, w.line_checks)
        self.assertEqual(list(w.blocks), [self.line_id])
        self.assertIn("Line 2 isn't active any more.", w.problems_label.cget("text"))
        self.assertTrue(w.send_button.instate(["!disabled"]), label_texts(w))
        self.assertEqual(w.send_button.cget("text"), "Send to 1 machine")
        self.assertEqual([ln.line_id for ln in w.request().lines], [self.line_id])
        # Active again: the check is back, unticked, and the note goes
        self.publish(dataclasses.replace(line2, active=True, revision=3))
        self.assertIn(line2.id, w.line_checks)
        self.assertFalse(w.line_vars[line2.id].get())
        self.assertNotIn(line2.id, w.blocks)
        self.assertNotIn("isn't active any more", w.problems_label.cget("text"))
        w.line_checks[line2.id].invoke()
        self.assertEqual(w.blocks[line2.id].countries, ["USA"])
        self.assertEqual(w.blocks[line2.id].lot_entry.get(), "4417")


class ResultTests(UiCase):
    def failed_send(self):
        """A Send whose idle-slot read-back differs: the red window opens."""
        fake = self.world.printers[0]
        hook_load(fake, 9, 1, change_lot)
        w = self.send_window()
        w.send_button.invoke()
        send_id = w.plan.send_id
        self.pump(lambda: self.services.nav.window(f"failure:{send_id}") is not None)
        self.settle()
        fake.web_reply_hook = None
        return self.services.nav.window(f"failure:{send_id}"), send_id

    def test_failure_window(self):
        services, mid = self.services, self.mid()
        fw, send_id = self.failed_send()
        self.assertTrue(fw.attributes("-fullscreen"))
        self.assertTrue(fw.attributes("-topmost"))
        self.assertIsNone(self.window_of("result:"))            # nothing went live
        self.assertEqual(len(fw.rows), 1)
        row = fw.rows[0]
        self.assertEqual((row.machine_id, row.step, row.after_switch), (mid, STEP_VERIFY_IDLE, False))
        self.assertTrue(any(d.sent == "Lot 4417" and d.read == "Lot 4418" for d in row.differences))
        texts = label_texts(fw)
        self.assertIn("Send failed", texts)
        self.assertIn("Failed at: Checking the new message before it goes live", texts)
        self.assertIn("Line 1 Bagger 1 · Line 1", texts)
        self.assertTrue(any(t.startswith("Printing before this Send (not confirmed): ") or
                            t == "Stripes doesn't know what this machine was last confirmed printing." for t in texts))
        self.assertIn(mid, fw.pictures)
        self.assertIsNone(fw.passed_label)
        fw.close()
        self.assertEqual(self.tell.call_args[0][1].text, "Choose Try again or Leave it.")
        self.assertEqual(self.tell.call_args[1], {"kind": "warning"})
        self.assertTrue(alive(fw))
        self.assertTrue(fw.attributes("-topmost"))              # on top again after the question
        # Leave it
        self.ask.return_value = False
        fw.leave_button.invoke()
        self.assertTrue(alive(fw))
        self.assertIn("Leave Line 1 Bagger 1 red?", hx_ui_common.text(self.ask.call_args[0][1]))
        self.assertEqual(self.ask.call_args[1], {"yes": "Leave it", "no": "Go back"})
        self.ask.return_value = True
        fw.leave_button.invoke()
        self.assertFalse(alive(fw))
        state = services.store.read_expected(mid)
        self.assertEqual(state.left_by, services.identity.user)
        self.assertEqual(state.send_id, send_id)

    def test_red_window_for_someone_else(self):
        fw, send_id = self.failed_send()
        akim = self.make("akim", admin=False, sender=True)
        other = FailureWindow(self.root, akim, fw.result, fw.plan)
        self.assertIsNone(other.try_button)
        self.assertIn("Only EXAMPLE\\jsmith can choose Try again or Leave it.", label_texts(other))

    def test_try_again_restricts_plan(self):
        services, mid = self.services, self.mid()
        fw, first_id = self.failed_send()
        seen = []
        services.bus.subscribe(SendEvent, lambda e: seen.append((e.send_id, e.machine_id, e.step, e.status, alive(fw))))
        fw.try_button.invoke()
        sw = services.nav.window("send")
        self.assertIsNotNone(sw)
        self.assertEqual(sw.page, "countdown")
        retry = sw.plan
        self.assertNotEqual(retry.send_id, first_id)
        self.assertEqual([m.machine.id for m in retry.machines], [mid])
        self.assertEqual([c.machine_ids for c in retry.request.lines], [[mid]])
        self.assertEqual(fw.retry_send_id, retry.send_id)
        self.assertTrue(alive(fw))
        self.assertEqual(fw.state(), "withdrawn")
        self.pump(lambda: not alive(fw))
        go = [s for s in seen if s[:4] == (retry.send_id, "", STEP_LOCK, "ok")]
        self.assertEqual(len(go), 1)
        before = seen[:seen.index(go[0])]
        self.assertTrue(before and all(s[4] for s in before))   # withdrawn, not destroyed, until the go
        self.assertFalse(go[0][4])
        self.pump(lambda: services.nav.window(f"result:{retry.send_id}") is not None)

    def test_try_again_refused_shows_the_red_window_again(self):
        services, mid = self.services, self.mid()
        fw, first_id = self.failed_send()
        # The templates change before the retry: the pipeline refuses it during its countdown
        data = services.data.current
        film = data.films[self.world.ids["film"]]
        services.data.current = dataclasses.replace(data, films={film.id: dataclasses.replace(film, delay_ms=500.0)})
        fw.try_button.invoke()
        sw = services.nav.window("send")
        self.assertEqual(fw.state(), "withdrawn")
        self.pump(lambda: fw.state() != "withdrawn")
        self.settle()
        self.assertTrue(fw.attributes("-fullscreen"))
        self.assertEqual(fw.message_label.cget("text"),
                         "The templates changed since you checked them. Look at the Send window again.")
        self.pump(lambda: sw.page == "choose")
        self.assertIn("The templates changed", sw.problems_label.cget("text"))
        # With the Send window open on its choice page, Try again asks and reuses it
        services.data.current = data
        self.ask.reset_mock()
        fw.try_button.invoke()
        self.assertEqual(self.ask.call_args[0][1].text, "Replace the choices in the Send window?")
        self.assertEqual(self.ask.call_args[1], {"yes": "Replace them", "no": "Keep them"})
        self.assertIs(services.nav.window("send"), sw)
        self.assertEqual(len([w for w in hx_ui_common.open_windows() if isinstance(w, SendWindow)]), 1)
        self.assertEqual(sw.page, "countdown")
        self.assertEqual([m.machine.id for m in sw.plan.machines], [mid])
        # A cancelled retry brings the red window back as well
        sw.cancel_button.invoke()
        self.pump(lambda: fw.state() != "withdrawn")
        self.assertEqual(fw.message_label.cget("text"), "Try again was cancelled.")
        # Keep them: the red window stays as it is
        self.ask.return_value = False
        fw.try_button.invoke()
        self.assertNotEqual(fw.state(), "withdrawn")
        self.assertEqual(fw.retry_send_id, "")

    def test_something_wrong_flow(self):
        services, mid = self.services, self.mid()
        rw = self.good_send()
        send_id = rw.result.send_id
        rw.wrong_buttons[self.line_id].invoke()
        self.assertEqual(rw.line_state[self.line_id], "rejected")
        state = services.store.read_expected(mid)
        self.assertTrue(state.rejected)
        self.assertTrue(state.resume_blocked)
        self.assertIn("You said something's wrong. These machines stay red until a new Send.", label_texts(rw))
        dialog = services.nav.window(f"stop:{mid}")
        self.assertIsInstance(dialog, StopDialog)
        self.assertEqual((dialog.reason, dialog.send_id, dialog.state), ("rejected", send_id, "ask"))
        self.assertEqual(dialog.question_label.cget("text"), "Stop printing on Line 1 Bagger 1?")
        self.assertTrue(dialog.explain_label.cget("text").startswith("You said something's wrong with the new"))
        dialog.stop_button.invoke()
        self.assertEqual(dialog.state, "working")
        dialog.close()
        self.assertEqual(self.tell.call_args[0][1].text, "Wait until Stripes has finished stopping printing.")
        self.pump(lambda: dialog.state == "done")
        self.assertEqual(dialog.explain_label.cget("text"), "Printing stopped on Line 1 Bagger 1. Line 1 needs a new "
                                                             "Send.")
        self.assertIsNotNone(dialog.fix_button)
        self.assertEqual(rw.line_state[self.line_id], "stopped")
        self.assertIn("Printing stopped. This line needs a new Send.", label_texts(rw))
        self.assertFalse(self.world.printers[0].printing)
        dialog.fix_button.invoke()
        self.assertFalse(alive(dialog))
        sw = services.nav.window("send")
        self.assertIsInstance(sw, SendWindow)
        block = sw.blocks[self.line_id]
        self.assertEqual((block.lot_entry.get(), block.countries), ("4417", ["USA", "Canada"]))
        self.assertEqual(block.film_var.get(), self.world.ids["film"])

    def test_stop_failure_goes_red(self):
        services, mid = self.services, self.mid()
        fake = self.world.printers[0]
        fake.go_silent(True)
        self.addCleanup(fake.go_silent, False)
        dialog = StopDialog(self.root, services, machine_ids=[mid])
        self.assertEqual(dialog.explain_label.cget("text"),
                         "Printing is turned off and read back twice to make sure it's off.")
        dialog.stop_button.invoke()
        self.pump(lambda: dialog.state == "failed", timeout_s=30)
        self.settle()
        self.assertTrue(dialog.attributes("-fullscreen"))
        self.assertTrue(dialog.attributes("-topmost"))
        texts = label_texts(dialog)
        self.assertIn("Stop it at the printer now", texts)
        self.assertIn("Stripes couldn't stop printing on Line 1 Bagger 1.", texts)
        self.assertIsNotNone(dialog.try_button)
        # Try again stops it again, with a new action id
        first = dialog.action_id
        fake.go_silent(False)
        dialog.try_button.invoke()
        self.assertNotEqual(dialog.action_id, first)
        self.assertEqual(dialog.state, "working")
        self.settle()
        self.assertFalse(dialog.attributes("-fullscreen"))
        self.pump(lambda: dialog.state == "done", timeout_s=30)
        self.assertEqual(dialog.explain_label.cget("text"), "Printing stopped on Line 1 Bagger 1.")
        self.assertIsNone(dialog.fix_button)
        self.assertFalse(fake.printing)

    def test_result_from_log(self):
        services, mid = self.services, self.mid()
        rw = self.good_send()
        send_id = rw.result.send_id
        picture = rw.pictures[mid].bmp
        rw.close()
        self.assertEqual(self.tell.call_args[0][1].text[:40], "Lines you haven't confirmed stay amber \"")
        self.assertFalse(alive(rw))
        again = services.nav.open_result(send_id)
        self.assertIsInstance(again, ResultWindow)
        self.assertEqual(again.pending, [mid])
        self.assertEqual(again.pictures[mid].bmp, picture)
        self.assertIn(self.line_id, again.right_buttons)
        self.assertEqual(again.request.lines[0].lot, "4417")
        self.assertIsNone(services.nav.open_result(send_id))      # already open: shown instead
        # Someone else sees it without the buttons
        akim = self.make("akim", admin=False, sender=True)
        theirs = ResultWindow.from_log(self.root, akim, send_id)
        self.assertEqual(theirs.pending, [mid])
        self.assertEqual(theirs.right_buttons, {})
        self.assertIn("Only EXAMPLE\\jsmith can confirm this Send.", label_texts(theirs))
        # The sender confirms from the log
        again.right_buttons[self.line_id].invoke()
        self.assertTrue(services.store.read_expected(mid).confirmed)
        # Nothing waits any more
        again.destroy()
        third = services.nav.open_result(send_id)
        self.assertEqual(third.pending, [])
        self.assertIn("Nothing in this Send is waiting for confirmation.", label_texts(third))
        self.assertEqual(third.right_buttons, {})


class NoEntryTests(UiCase):
    printers = [{}, {}]

    def sent_without_entry(self):
        """A good Send whose log entries never reached the share (the PC that sent it crashed); its
        send_id."""
        rw = self.good_send(machines=(1,))
        send_id = rw.result.send_id
        rw.destroy()
        # No send entry, no switch entry
        logs = os.path.join(self.world.share_root, "logs", "hx-sends")
        for folder, _, names in os.walk(logs):
            for name in names:
                if send_id.split("_")[1] in name or ".send." in name:
                    os.remove(os.path.join(folder, name))
        self.assertIsNone(self.services.data.find_send(send_id))
        return send_id

    def test_result_from_log_no_entry(self):
        services, mid1, mid2 = self.services, self.mid(1), self.mid(2)
        send_id = self.sent_without_entry()
        # The board reads what the printer runs: the message the Send loaded
        services.watcher.latest = board_of(self.line_id, mview(mid1, self.line_id, "Line 1 Bagger 1",
                                                               origin="Product of USA and Canada", colour=AMBER,
                                                               confirm_pending_by="EXAMPLE\\jsmith"))
        # A machine not in the Send whose expected state a newer Stripes wrote doesn't stop it
        rel = os.path.join(self.world.share_root, "status", "machines", f"{mid2}.expected.json")
        with open(rel, "w", encoding="utf-8") as f:
            json.dump({"format_version": 99, "file_type": "expected", "machine_id": mid2, "counter": 1}, f)
        picture = self.world.printers[0].slots[9].preview_bmp or b"BM"
        services.store.write_preview(mid1, picture)
        w = services.nav.open_result(send_id)
        self.assertEqual(w.pending, [mid1])
        self.assertEqual(w.pictures[mid1].bmp, picture)
        self.assertEqual(w.origins[mid1].get(), "Product of USA and Canada")
        self.assertIn("This PC can't find the full record of this Send. These are the printers' latest pictures.",
                      label_texts(w))
        self.assertIn(self.line_id, w.right_buttons)
        # Another person sees no buttons
        akim = self.make("akim", admin=False, sender=True)
        theirs = ResultWindow.from_log(self.root, akim, send_id)
        self.assertEqual(theirs.right_buttons, {})
        self.assertIn("Only EXAMPLE\\jsmith can confirm this Send.", label_texts(theirs))
        # The sender confirms it with the send_id alone
        w.right_buttons[self.line_id].invoke()
        self.assertEqual(w.line_state[self.line_id], "confirmed")
        self.assertTrue(services.store.read_expected(mid1).confirmed)

    def test_no_entry_shows_what_the_printer_runs(self):
        """ui.md §6.4 item 5: without the send entry the origin is the board's MachineView.origin_line (what
        the printer runs now), never the expected state's; the sender check uses confirm_pending_by."""
        services, mid1 = self.services, self.mid(1)
        send_id = self.sent_without_entry()
        self.assertEqual(services.store.read_expected(mid1).origin_line, "Product of USA and Canada")
        # The printer now runs another message
        services.watcher.latest = board_of(self.line_id, mview(mid1, self.line_id, "Line 1 Bagger 1",
                                                               origin="Product of USA", colour=AMBER,
                                                               message_name="EXCO1-PKO01-OTHER",
                                                               confirm_pending_by="EXAMPLE\\jsmith"))
        w = services.nav.open_result(send_id)
        self.assertEqual(w.pending, [mid1])
        self.assertEqual(w.origins[mid1].get(), "Product of USA")
        self.assertIn("Message EXCO1-PKO01-OTHER in slot ?.", label_texts(w))
        self.assertIn(self.line_id, w.right_buttons)
        w.destroy()
        # No board yet: the origin isn't known, rather than the one the Send meant to load
        services.watcher.latest = None
        w = services.nav.open_result(send_id)
        self.assertEqual(w.origins[mid1].get(), "")
        self.assertIn("(origin unknown)", label_texts(w))
        self.assertIn(self.line_id, w.right_buttons)            # the sender from the expected state


# --------------------------------------------------------------------------
# Resume, Set clocks and the action ids
# --------------------------------------------------------------------------

class ActionTests(UiCase):
    printers = [{}, {}]

    def test_resume_dialog_shows_picture(self):
        services, mid1, mid2 = self.services, self.mid(1), self.mid(2)
        rw = self.good_send(machines=(1, 2))
        send_id = rw.result.send_id
        rw.destroy()
        services.pipeline().confirm(send_id, [mid1], True)      # Bagger 2 is never confirmed
        services.actions().stop([mid1, mid2], services.bus.post)
        self.assertFalse(self.world.printers[0].printing or self.world.printers[1].printing)
        dialog = ResumeDialog(self.root, services, machine_ids=[mid1, mid2])
        self.assertEqual(dialog.state, "checking")
        self.assertEqual(dialog.status_label.cget("text"), "Loading what's on each printer...")
        self.pump(lambda: dialog.state == "ready")
        self.assertEqual([c.allowed for c in dialog.checks], [True, False])
        self.assertTrue(dialog.pictures[mid1].bmp)
        self.assertIsNone(dialog.pictures[mid2].bmp)
        self.assertEqual(dialog.origins[mid1].get(), "Product of USA and Canada")
        texts = label_texts(dialog)
        self.assertIn("This is the message of the last confirmed Send.", texts)
        self.assertIn("Line 1 Bagger 2: The last Send to Line 1 Bagger 2 wasn't confirmed. Send again instead.", texts)
        self.assertEqual(dialog.status_label.cget("text"), "Only the machines that passed the check are resumed.")
        self.assertTrue(dialog.resume_button.instate(["!disabled"]))
        calls = []
        real = hx_send.PrinterActions.resume

        def spy(actions, machine_ids, on_event, **kw):
            calls.append(list(machine_ids))
            return real(actions, machine_ids, on_event, **kw)
        self.patch(hx_send.PrinterActions, "resume", new=spy)
        dialog.resume_button.invoke()
        self.assertEqual(dialog.state, "working")
        self.pump(lambda: dialog.state == "done")
        self.assertEqual(calls, [[mid1]])
        self.assertEqual(dialog.status_label.cget("text"), "Printing resumed on Line 1 Bagger 1.")
        self.assertTrue(self.world.printers[0].printing)
        self.assertFalse(self.world.printers[1].printing)
        self.assertIsNotNone(dialog.close_button)

    def test_set_clocks(self):
        services = self.services
        dialog = SetClocksDialog(self.root, services, line_ids=[self.line_id])
        self.assertEqual(dialog.machine_ids, [self.mid(1), self.mid(2)])
        self.assertEqual(dialog.question_label.cget("text"),
                         "Set the clock of every printer on Line 1 from the time server?")
        dialog.set_button.invoke()
        self.assertEqual(dialog.state, "working")
        self.pump(lambda: dialog.state == "done")
        self.assertEqual([row.msg_label.cget("text") for row in dialog.rows.values()], ["Clock set", "Clock set"])
        self.assertIn("TD", self.world.printers[0].writes)

    def test_action_ids(self):
        services, mid = self.services, self.mid(1)
        log = []

        class FakeActions:
            def _go(self, kind, machine_ids, on_event, action_id):
                log.append((kind, threading.current_thread().name, action_id))
                on_event(ActionEvent(action_id="ac_0000000000", kind=kind, machine_id=mid, status="failed",
                                     msg=Msg("Stripes can't reach the server.")))
                on_event(ActionEvent(action_id=action_id, kind=kind, machine_id=mid, status="running"))
                on_event(ActionFinished(result=ActionResult(action_id=action_id, kind=kind, machines=[
                    MachineResult(machine_id=m, colour=GREEN, ok=True, step=STEP_LOCK) for m in machine_ids])))

            def stop(self, machine_ids, on_event, *, reason="user", send_id="", action_id=""):
                self._go("stop", machine_ids, on_event, action_id)

            def resume(self, machine_ids, on_event, *, action_id=""):
                self._go("resume", machine_ids, on_event, action_id)

            def set_clocks(self, machine_ids, on_event, *, action_id=""):
                self._go("clock", machine_ids, on_event, action_id)

            def check_resume(self, machine_id):
                return ResumeCheck(machine_id=machine_id, design=None, origin_line="Product of USA", allowed=True)

        services.actions = lambda: FakeActions()
        stop = StopDialog(self.root, services, machine_ids=[mid])
        resume = ResumeDialog(self.root, services, machine_ids=[mid])
        clocks = SetClocksDialog(self.root, services, line_ids=[self.line_id])
        self.pump(lambda: resume.state == "ready")
        handled = []
        for dialog in (stop, resume, clocks):
            # Every ActionEvent the dialog acts on is recorded with its id
            def spy(event, dialog=dialog, real=dialog.action_event):
                handled.append((dialog.action_id, event.action_id))
                real(event)
            dialog.action_event = spy
        stop.stop_button.invoke()
        resume.resume_button.invoke()
        clocks.set_button.invoke()
        self.pump(lambda: stop.state == "done" and resume.state == "done" and clocks.state == "done")
        self.assertEqual(sorted(log), sorted([("stop", f"hx-action-{stop.action_id}", stop.action_id),
                                              ("resume", f"hx-action-{resume.action_id}", resume.action_id),
                                              ("clock", f"hx-action-{clocks.action_id}", clocks.action_id)]))
        self.assertEqual(len({stop.action_id, resume.action_id, clocks.action_id}), 3)
        self.assertTrue(all(d.action_id.startswith("ac_") for d in (stop, resume, clocks)))
        self.assertEqual(sorted(handled), sorted((d.action_id, d.action_id) for d in (stop, resume, clocks)))
        self.assertEqual(stop.rows[mid].mark_label.cget("text"), "…")     # the other id's "failed" was ignored
        self.assertEqual(stop.explain_label.cget("text"), "Printing stopped on Line 1 Bagger 1.")
        self.assertEqual(hx_ui_common.running(), [])


if __name__ == "__main__":
    unittest.main()
