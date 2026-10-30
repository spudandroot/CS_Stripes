"""Tests for hx_ui_admin: the admin window and its pages, the film editor, the print-text designer, the
test print and calibration windows, and the admin screens' pure helpers.
Run: xvfb-run -a python -m unittest test_hx_ui_admin -v

The Tk tests drive FakeWorld printers and its share through the real code, with scaled timings. The
services are built here the way hx_ui.make_services builds them, with a stub Navigator, so these tests
don't depend on hx_ui. The Tk tests need a display; they skip themselves when Tk can't start.
"""

import dataclasses
import gc
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import tkinter as tk
import unittest
from dataclasses import dataclass
from datetime import date
from unittest import mock

from PIL import Image

import hx_message
import hx_send
import hx_share
import hx_ui_admin
import hx_ui_common
import i18n
from hx_core import EventBus, Msg
from hx_fake import FakeWorld
from hx_model import (TIMINGS, Calibration, Company, Country, Film, HxData, Line, Machine, MachineFilmAdjustment,
                      NotAllowed, PollStatus, Printer, PrintText, PrintTextItem, Product, TestFinished, TestResult,
                      from_json)
from hx_protocol import ClientPool, GateRegistry, make_token, release_key, unscramble
from hx_ui_admin import (AdjustmentDialog, AdminWindow, CalibrationWindow, FilmEditor, PrintTextEditor,
                         TestPrintWindow)
from hx_ui_common import SyncBackground

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
    latest = None


class StubNav:
    """The Navigator interface the admin screens use."""

    def __init__(self, root):
        self.root = root
        self.services = None
        self.windows = {}
        self.calls = []

    def window(self, key):
        w = self.windows.get(key)
        try:
            return w if w is not None and w.winfo_exists() else None
        except tk.TclError:
            return None

    def open_admin(self, *, page="", record_id=""):
        self.calls.append(("open_admin", page, record_id))
        w = self.window("admin")
        if w is not None:
            w.select(page, record_id)
            return w
        return AdminWindow(self.root, self.services, page=page, record_id=record_id)

    def open_test_print(self, *, film_id, machine_id="", kind="test"):
        self.calls.append(("open_test_print", film_id, machine_id, kind))
        return TestPrintWindow(self.root, self.services, film_id=film_id, machine_id=machine_id, kind=kind)

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


def shown(widget):
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


def enabled(widget):
    return widget.instate(["!disabled"])


class LaterBackground:
    """stripes.Background's timing: the work runs at once, done() later, on the Tk thread, at flush()."""

    def __init__(self):
        self.queued = []

    def run(self, work, done):
        try:
            result, error = work(), None
        except Exception as e:
            result, error = None, e
        self.queued.append((done, result, error))

    def flush(self):
        while self.queued:
            done, result, error = self.queued.pop(0)
            done(result, error)


# --------------------------------------------------------------------------
# No Tk needed
# --------------------------------------------------------------------------

def small_data():
    """Two films (one pre-printed) and three print texts (none, one origin item)."""
    company = Company(id="company", countries=[Country(name="USA", aliases=["US"]), Country(name="Canada"),
                                               Country(name="Mexico")])
    product = Product(id="pd_0000000001", name="Example Co 2lb Onion", customer="Example Co")
    dated = PrintTextItem(id="i1", kind="packed_date", date_format="MM/DD/YY", before="Packed on ")
    pko = PrintText(id="pt_0000000001", name="Packed on + origin", code="PKO", status="active",
                    items=[dated, PrintTextItem(id="i2", kind="origin", placement="below"),
                           PrintTextItem(id="i3", kind="lot", before="Lot ", gap=4)])
    pkl = PrintText(id="pt_0000000002", name="Packed on + lot", code="PKL", status="active",
                    items=[dated, PrintTextItem(id="i2", kind="lot", before="Lot ", placement="below")])
    pkd = PrintText(id="pt_0000000003", name="Packed on", code="PKD", status="development", items=[dated])
    plain = Film(id="fm_0000000001", product_id=product.id, code="EXCO1", artwork_revision="Rev C", status="active",
                 length_mm=610.0, position_mm=150.0, delay_ms=480.0, print_text_ids=[pko.id],
                 default_print_text_id=pko.id)
    pre = Film(id="fm_0000000002", product_id=product.id, code="EXCO2", artwork_revision="Rev D",
               origin_preprinted=True, preprinted_origin="USA", print_text_ids=[pkl.id], default_print_text_id=pkl.id)
    return HxData(company=company, products={product.id: product}, films={plain.id: plain, pre.id: pre},
                  print_texts={pko.id: pko, pkl.id: pkl, pkd.id: pkd})


class PureTests(unittest.TestCase):
    def test_longest_values_preview(self):
        # storage.md's print text: the lot has a gap of 4, so it follows the origin after a space
        pt = PrintText(id="pt_0b1c2d3e4f", revision=7, name="Packed on + origin", code="PKO", status="active", items=[
            PrintTextItem(id="i1", kind="packed_date", before="Packed on ", date_format="MM/DD/YY"),
            PrintTextItem(id="i2", kind="origin", placement="below"),
            PrintTextItem(id="i3", kind="lot", before="Lot ", placement="right", gap=4)])
        company = Company(id="company", countries=[Country(name="USA", aliases=["US", "U.S.A.", "United States"]),
                                                   Country(name="Canada", aliases=["CAN"]),
                                                   Country(name="Mexico", aliases=["MEX"])])
        values = hx_ui_admin.longest_values(pt, company, date(2026, 10, 29))
        self.assertEqual(hx_ui_admin.preview_lines(pt, values),
                         ["Packed on 10/29/26", "Product of USA, Canada, and Mexico Lot 88888888"])
        # Without the gap the lot follows straight on; below starts a new line
        pt.items[2].gap = 0
        self.assertEqual(hx_ui_admin.preview_lines(pt, values)[1], "Product of USA, Canada, and MexicoLot 88888888")
        pt.items[2].placement = "below"
        self.assertEqual(len(hx_ui_admin.preview_lines(pt, values)), 3)
        # Every kind, and what can't be worked out
        items = [PrintTextItem(id="i1", kind="fixed", text="Keep cool"),
                 PrintTextItem(id="i2", kind="best_by_date", date_format="MM/DD/YY", offset=2, offset_unit="months",
                               before="Best by "),
                 PrintTextItem(id="i3", kind="free_text", max_len=5, before="[", after="]"),
                 PrintTextItem(id="i4", kind="machine_code", before="M"),
                 PrintTextItem(id="i5", kind="line_code"),
                 PrintTextItem(id="i6", kind="packed_date", date_format="NOPE")]
        values = hx_ui_admin.longest_values(PrintText(id="pt_x", name="x", code="X", items=items), company,
                                            date(2026, 10, 29))
        self.assertEqual(values, {"i1": "Keep cool", "i2": "Best by 12/29/26", "i3": "[WWWWW]", "i4": "MWWW",
                                  "i5": "WWW", "i6": "?"})
        none = dataclasses.replace(company, countries=[])
        origin = PrintText(id="pt_y", name="y", code="Y", items=[PrintTextItem(id="i1", kind="origin")])
        self.assertEqual(hx_ui_admin.longest_values(origin, none, date(2026, 10, 29)), {"i1": ""})

    def test_origin_rule_ok_matches_validate_film(self):
        data = small_data()
        texts = ("Print text {print_text} has an origin item, but this film's origin is pre-printed.",
                 "Print text {print_text} has no origin item, but this film needs one.")
        pairs = 0
        for film in data.films.values():
            for pt in data.print_texts.values():
                changed = dataclasses.replace(film, print_text_ids=[pt.id], default_print_text_id=pt.id)
                problems = hx_message.validate_film(changed, data)
                origin_problem = any(p.text in texts for p in problems)
                self.assertEqual(not hx_ui_admin.origin_rule_ok(film, pt), origin_problem, (film.code, pt.code))
                pairs += 1
        self.assertEqual(pairs, 6)
        plain, pre = data.films["fm_0000000001"], data.films["fm_0000000002"]
        self.assertEqual([p.code for p in hx_ui_admin.print_text_choices(plain, data)], ["PKO"])
        self.assertEqual([p.code for p in hx_ui_admin.print_text_choices(pre, data)], ["PKD", "PKL"])
        self.assertEqual([f.code for f in hx_ui_admin.film_choices(data.print_texts["pt_0000000001"], data)],
                         ["EXCO1"])
        self.assertEqual([f.code for f in hx_ui_admin.film_choices(data.print_texts["pt_0000000003"], data)],
                         ["EXCO2"])
        # Two origin items suit no film
        two = PrintText(id="pt_z", name="z", code="Z", items=[PrintTextItem(id="i1", kind="origin"),
                                                              PrintTextItem(id="i2", kind="origin")])
        self.assertFalse(hx_ui_admin.origin_rule_ok(plain, two) or hx_ui_admin.origin_rule_ok(pre, two))

    def test_films_by_code_then_revision(self):
        """The Send window's order: by code, then artwork revision, both naturally ("EXCO2" before
        "EXCO10", "Rev 9" before "Rev 10"); film_choices keeps the product first."""
        data = small_data()
        product_id = next(iter(data.products))
        films = [Film(id=f"fm_00000000a{n}", product_id=product_id, code=code, artwork_revision=revision)
                 for n, (code, revision) in enumerate((("EXCO10", "Rev A"), ("EXCO2", "Rev B"), ("EXCO2", "Rev 10"),
                                                       ("EXCO2", "Rev 9")))]
        ordered = sorted(films, key=hx_ui_admin._film_key)
        self.assertEqual([hx_ui_admin.film_name(f) for f in ordered],
                         ["EXCO2 · Rev 9", "EXCO2 · Rev 10", "EXCO2 · Rev B", "EXCO10 · Rev A"])
        other = Product(id="pd_0000000002", name="Example Co 5lb Onion", customer="Example Co")
        last = Film(id="fm_00000000b0", product_id=other.id, code="AA1", artwork_revision="Rev A")
        data = dataclasses.replace(data, products={**data.products, other.id: other},
                                   films={**data.films, last.id: last, **{f.id: f for f in films}})
        pko = data.print_texts["pt_0000000001"]
        self.assertEqual([hx_ui_admin.film_name(f) for f in hx_ui_admin.film_choices(pko, data)],
                         ["EXCO1 · Rev C", "EXCO2 · Rev 9", "EXCO2 · Rev 10", "EXCO2 · Rev B", "EXCO10 · Rev A",
                          "AA1 · Rev A"])

    def test_membership_saves(self):
        data = small_data()
        pkd = data.print_texts["pt_0000000003"]
        films, problems = hx_ui_admin.membership_saves(pkd, {"fm_0000000002"}, data)
        self.assertEqual(problems, [])
        self.assertEqual([(f.id, f.print_text_ids) for f in films],
                         [("fm_0000000002", ["pt_0000000002", "pt_0000000003"])])
        self.assertEqual(data.films["fm_0000000002"].print_text_ids, ["pt_0000000002"])     # copies only
        # Removing a film's default is refused
        pko = data.print_texts["pt_0000000001"]
        films, problems = hx_ui_admin.membership_saves(pko, set(), data)
        self.assertEqual(films, [])
        self.assertEqual([hx_ui_common.text(p) for p in problems],
                         ["EXCO1 · Rev C uses this print text as its default. Choose another default in that film "
                          "first."])
        # A copy that wouldn't validate gives its problems, prefixed with the film
        broken = dataclasses.replace(data.films["fm_0000000002"], code="exco2")
        data2 = dataclasses.replace(data, films={**data.films, broken.id: broken})
        films, problems = hx_ui_admin.membership_saves(pkd, {"fm_0000000002"}, data2)
        self.assertEqual(films, [])
        self.assertEqual([hx_ui_common.text(p) for p in problems],
                         ["exco2 · Rev D: The film code must be 1 to 6 capital letters or digits."])
        # Nothing changes: nothing to save
        self.assertEqual(hx_ui_admin.membership_saves(pko, {"fm_0000000001"}, data), ([], []))

    def test_membership_saves_refuses_stranding_a_film(self):
        """A print text that loses (or gains) its origin item while films allow it is refused, naming
        each film, so no film is left failing validate_film."""
        data = small_data()
        pko = data.print_texts["pt_0000000001"]
        no_origin = dataclasses.replace(pko, items=[it for it in pko.items if it.kind != "origin"])
        films, problems = hx_ui_admin.membership_saves(no_origin, {"fm_0000000001"}, data)
        self.assertEqual(films, [])
        self.assertEqual([hx_ui_common.text(p) for p in problems],
                         ["EXCO1 · Rev C: Print text Packed on + origin has no origin item, but this film needs one."])
        pkl = data.print_texts["pt_0000000002"]
        with_origin = dataclasses.replace(pkl, items=pkl.items + [PrintTextItem(id="i3", kind="origin")])
        films, problems = hx_ui_admin.membership_saves(with_origin, set(), data)
        self.assertEqual(films, [])
        self.assertEqual([hx_ui_common.text(p) for p in problems],
                         ["EXCO2 · Rev D: Print text Packed on + lot has an origin item, but this film's origin is "
                          "pre-printed."])
        # A film that was already broken doesn't block the print text (its film editor lists it to untick)
        broken = dataclasses.replace(data.films["fm_0000000001"], print_text_ids=["pt_0000000001", pkl.id])
        data3 = dataclasses.replace(data, films={**data.films, broken.id: broken})
        self.assertEqual(hx_ui_admin.membership_saves(pkl, {"fm_0000000002"}, data3), ([], []))

    def test_parse_number(self):
        parse = hx_ui_admin.parse_number
        self.assertIsNone(parse("  "))
        self.assertEqual(parse(" 150 "), 150.0)
        self.assertEqual(parse("152,5"), 152.5)              # a decimal comma
        self.assertEqual(parse("-3"), -3.0)
        self.assertEqual(parse("+20"), 20.0)
        self.assertEqual(parse("2400", whole=True), 2400)
        self.assertIs(type(parse("2400", whole=True)), int)
        self.assertEqual(parse("12.0", whole=True), 12)
        for bad in ("1,000.5", "1,2,3", "abc", "nan", "inf", "1e3", "1.2.3"):
            with self.assertRaises(ValueError, msg=bad):
                parse(bad)
        with self.assertRaises(ValueError):
            parse("2.5", whole=True)

    def test_printer_problems(self):
        p1 = Printer(id="pr_0000000001", name="Printer 1", host="192.0.2.10", port=80, uid=1109)
        data = HxData(company=Company(id="company"), printers={p1.id: p1})

        def problems(**kw):
            values = dict(id="pr_0000000002", name="Printer 2", host="192.0.2.11", port=80, uid=1109)
            values.update(kw)
            return [hx_ui_common.text(m) for m in hx_ui_admin.printer_problems(Printer(**values), data)]
        self.assertEqual(problems(), [])
        self.assertEqual(problems(name=" "), ["Give the printer a name."])
        self.assertEqual(problems(name="x" * 41), ["Give the printer a name."])
        self.assertEqual(problems(name="PRINTER 1"), ["Another printer already has that name."])
        self.assertEqual(problems(host="printer.example.local"),
                         ["Type the printer's IP address, for example 192.0.2.10."])
        # As saved: no socket reaches an address with spaces around it (the form strips them)
        self.assertEqual(problems(host=" 192.0.2.11 "), ["Type the printer's IP address, for example 192.0.2.10."])
        self.assertEqual(problems(host="192.0.2.10"), ["Printer 1 already has this address."])
        self.assertEqual(problems(host="192.0.2.10", port=8080), [])
        self.assertEqual(problems(host="192.0.2.10", active=False), [])
        self.assertEqual(problems(port=0), ["The port must be a number from 1 to 65535."])
        self.assertEqual(problems(uid=0), ["The UID must be a whole number from 1 to 4294967295."])
        self.assertEqual(problems(uid=4294967296), ["The UID must be a whole number from 1 to 4294967295."])
        self.assertEqual(problems(id=p1.id, name="Printer 1", host="192.0.2.10"), [])    # itself

    def test_other_problems(self):
        data = small_data()
        product = data.products["pd_0000000001"]
        pp = lambda **kw: [hx_ui_common.text(m) for m in hx_ui_admin.product_problems(
            dataclasses.replace(product, id="pd_0000000002", **kw), data)]
        self.assertEqual(pp(name="Example Co 5lb Onion"), [])
        self.assertEqual(pp(name=""), ["Give the product a name."])
        self.assertEqual(pp(name="example co 2LB onion"), ["Another product already has that name."])
        self.assertEqual(pp(name="Other", customer="x" * 81), ["The customer name is too long."])
        data2 = dataclasses.replace(data, products={product.id: dataclasses.replace(product, active=False)})
        self.assertEqual(hx_ui_admin.product_problems(dataclasses.replace(product, id="pd_0000000002"), data2), [])
        # Notify users
        for good in ("", "EXAMPLE\\jsmith", "EXAMPLE\\akim smith"):
            self.assertEqual(hx_ui_admin.notify_user_problems(good), [], good)
        for bad in ("jsmith", "\\jsmith", "EXAMPLE\\", " EXAMPLE\\jsmith", "EXAMPLE/jsmith", "A\\B\\C"):
            self.assertEqual([hx_ui_common.text(m) for m in hx_ui_admin.notify_user_problems(bad)],
                             ["Type the notify user as DOMAIN\\name, for example EXAMPLE\\jsmith."], bad)
        # Adjustments
        machine = Machine(id="mc_0000000001", name="Line 1 Bagger 1", code="B1", line_id="ln_0000000001",
                          printer_id="pr_0000000001", placement="delay")
        data3 = dataclasses.replace(data, machines={machine.id: machine})
        adj = lambda **kw: [hx_ui_common.text(m) for m in hx_ui_admin.adjustment_problems(MachineFilmAdjustment(
            id="x", **{"machine_id": machine.id, "film_id": "fm_0000000001", **kw}), data3)]
        self.assertEqual(adj(delay_ms=20.0, position_mm=-3.0), [])
        self.assertEqual(adj(machine_id=""), ["Choose a machine and a film."])
        self.assertEqual(adj(film_id="fm_0000000009"), ["Choose a machine and a film."])
        self.assertEqual(adj(position_mm=1000.5), ["The position adjustment must be between -1000 and 1000 mm."])
        self.assertEqual(adj(delay_ms=-10001.0), ["The delay adjustment must be between -10000 and 10000 ms."])
        film = hx_ui_admin.new_film("pd_0000000001")
        self.assertTrue(film.id.startswith("fm_"))
        self.assertEqual((film.product_id, film.code, film.status, film.files), ("pd_0000000001", "", "development",
                                                                                 []))


# --------------------------------------------------------------------------
# Tk
# --------------------------------------------------------------------------

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
        self.tmp = tempfile.mkdtemp(prefix="hx_ui_admin_")
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
            self.assertEqual(fake.violations, [])
            self.assertLessEqual(fake.max_in_flight, 1)

    def patch(self, target, name, **kw):
        patcher = mock.patch.object(target, name, **kw)
        found = patcher.start()
        self.addCleanup(patcher.stop)
        return found

    def make(self, name="jsmith", *, admin=True, sender=True):
        services = build_services(self.root, self.world, self.world.identity(name, admin=admin, sender=sender))
        for n in range(1, len(self.world.printers) + 1):
            services.store.write_poll(PollStatus(machine_id=self.mid(n), printer_id=self.world.ids[f"printer{n}"],
                                                 poller=services.identity.key, counter=1))
        return services

    def mid(self, n=1):
        return self.world.ids[f"machine{n}"]

    def admin_store(self):
        return self.world.store(self.world.identity("admin", admin=True, sender=False))

    def bump_changes(self, store):
        raw = store.read_json("config/hx/changes.json")
        store.write_json("config/hx/changes.json", {"file_type": "changes", "counter": raw["counter"] + 1,
                                                    "written_at": time.time(), "written_by": "EXAMPLE\\admin",
                                                    "last": None})

    def reload(self, services=None):
        services = services or self.services
        services.data.run_once()
        self.settle(services=services)

    def settle(self, seconds=0.05, services=None):
        services = services or self.services
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            services.bus.pump()
            time.sleep(0.01)

    def pump(self, until, timeout_s=30, printing=None, services=None):
        """Pump until until(); a fake printer given as printing prints a bag on every round."""
        services = services or self.services
        deadline = time.monotonic() + timeout_s
        while not until():
            if time.monotonic() > deadline:
                self.fail("timed out")
            if printing is not None:
                printing.print_bags(1)
            self.root.update()
            services.bus.pump()
            time.sleep(0.01)


class AdminTests(UiCase):
    def test_admin_save_and_conflict(self):
        services, line_id = self.services, self.world.ids["line"]
        w = AdminWindow(self.root, services, page="lines", record_id=line_id)
        self.assertEqual(services.nav.window("admin"), w)
        page = w.pages["lines"]
        self.assertIs(w.current_page(), page)
        self.assertEqual(page.working.id, line_id)
        self.assertEqual(page.tree.selection(), (line_id,))
        self.assertEqual(page.tree.item(line_id, "values")[:2], ("Line 1", "L1"))
        revision = page.expected_revision
        self.assertFalse(page.dirty)
        self.assertTrue(page.revision_label.cget("text").startswith(
            f"Revision {revision} · saved by EXAMPLE\\admin at "))
        page.fields["name"].set("Line 1 North")
        self.assertTrue(page.dirty)
        self.assertTrue(enabled(page.save_button))
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saved Line 1 North.")
        self.assertEqual((page.expected_revision, page.dirty), (revision + 1, False))
        rel = hx_share.record_rel_for(Line, line_id)
        on_share = from_json(Line, self.admin_store().read_json(rel))
        self.assertEqual((on_share.name, on_share.revision, on_share.updated_by),
                         ("Line 1 North", revision + 1, "EXAMPLE\\jsmith"))
        # Another administrator saves meanwhile: this save is refused, with Reload
        admin = self.admin_store()
        hx_share.write_record(admin, dataclasses.replace(on_share, revision=revision + 2, updated_by="EXAMPLE\\akim",
                                                         notify_user="EXAMPLE\\akim"))
        self.bump_changes(admin)
        page.fields["notify_user"].set("EXAMPLE\\jsmith")
        page.save_button.invoke()
        self.assertIn("EXAMPLE\\akim changed this while you were editing it.", page.problems_label.cget("text"))
        self.assertTrue(shown(page.reload_button))
        self.assertTrue(page.dirty)
        # The next DataUpdate says so too
        self.reload()
        self.assertTrue(shown(page.changed_banner))
        self.assertTrue(page.changed_banner.cget("text").startswith("EXAMPLE\\akim changed this record at"))
        self.ask.return_value = False
        page.reload_button.invoke()
        self.assertEqual(self.ask.call_args[0][1].text, "Discard your changes to {name}?")
        self.assertEqual(self.ask.call_args[1], {"yes": "Discard", "no": "Keep editing"})
        self.assertTrue(page.dirty)
        self.ask.return_value = True
        page.reload_button.invoke()
        self.assertEqual((page.working.revision, page.working.notify_user, page.dirty),
                         (revision + 2, "EXAMPLE\\akim", False))
        self.assertFalse(shown(page.changed_banner))

    def test_checks_block_and_leaving_asks(self):
        services = self.services
        w = AdminWindow(self.root, services, page="lines")
        page = w.pages["lines"]
        page.new_button.invoke()
        self.assertEqual(page.revision_label.cget("text"), "Not saved yet.")
        self.assertTrue(page.dirty)
        page.fields["name"].set("Line 2")
        page.fields["code"].set("l2")
        page.fields["notify_user"].set("akim")
        page.save_button.invoke()
        text = page.problems_label.cget("text")
        self.assertIn("The code must be 1 to 3 capital letters or digits.", text)
        self.assertIn("The cartridge type isn't valid.", text)
        self.assertIn("Type the notify user as DOMAIN\\name, for example EXAMPLE\\jsmith.", text)
        page.fields["code"].set("L2")
        page.fields["cartridge"].set_key("plastic")
        page.editor.me_button.invoke()
        self.assertEqual(page.fields["notify_user"].get(), "EXAMPLE\\jsmith")
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saved Line 2.")
        self.reload()
        self.assertEqual(len(page.tree.get_children()), 2)
        # Leaving a changed record asks; "Keep editing" stays on it
        page.fields["name"].set("Line 2 South")
        self.ask.return_value = False
        page.tree.selection_set(self.world.ids["line"])
        self.settle()
        self.assertEqual(page.working.name, "Line 2")
        self.assertEqual(page.fields["name"].get(), "Line 2 South")
        self.assertEqual(page.tree.selection(), (page.working.id,))
        # A whole number field that isn't one
        w2 = w.pages["machines"]
        w.select("machines", self.mid())
        self.assertIs(w.current_page(), page)      # the leaving question said no
        self.ask.return_value = True
        w.select("machines", self.mid())
        self.assertIs(w.current_page(), w2)
        w2.fields["print_speed"].set("24.5")
        w2.fields["seconds_per_bag"].set("")
        w2.save_button.invoke()
        self.assertEqual(w2.problems_label.cget("text").split("\n"),
                         ["Print speed (cm/min): type a whole number.", "Seconds per bag is needed."])

    def test_admin_refuses_non_admin(self):
        try:
            import hx_ui
        except Exception as e:         # built by another package
            self.skipTest(f"hx_ui isn't available: {e}")
        services = self.make("jsmith", admin=False, sender=True)
        nav = hx_ui.Navigator(self.root)
        services.nav = nav
        nav.services = services
        nav.open_admin()
        self.assertEqual(self.tell.call_args[0][1].text, "Only Stripes Administrators can do this.")
        self.assertIsNone(nav.window("admin"))
        self.assertFalse(any(isinstance(w, AdminWindow) for w in hx_ui_common.open_windows()))

    def test_role_and_share_disable_saving(self):
        services = self.services
        w = AdminWindow(self.root, services, page="lines", record_id=self.world.ids["line"])
        page = w.pages["lines"]
        page.fields["name"].set("Line 1 North")
        self.assertTrue(enabled(page.save_button))
        self.assertFalse(shown(w.share_banner) or shown(w.role_label))
        data = services.data.current
        services.data.current = dataclasses.replace(data, source="local")
        w._show_state()
        self.assertTrue(shown(w.share_banner))
        self.assertFalse(enabled(page.save_button))
        services.data.current = dataclasses.replace(data, company=dataclasses.replace(data.company,
                                                                                      admin_group_sid="S-1-0"))
        w._show_state()
        self.assertTrue(shown(w.role_label))
        self.assertEqual(w.role_label.cget("text"), "Only Stripes Administrators can change this.")
        self.assertFalse(enabled(page.save_button))

    def test_printer_password_once(self):
        services = self.services
        w = AdminWindow(self.root, services, page="printers")
        page = w.pages["printers"]
        page.new_button.invoke()
        self.assertEqual(page.editor.password_status.cget("text"),
                         "No password saved yet. Stripes can't check this printer until one is.")
        self.assertEqual(str(page.editor.password_entry.cget("textvariable")), "")
        for name, value in (("name", "Printer 9"), ("host", "192.0.2.19"), ("port", "80"), ("uid", "1109")):
            page.fields[name].set(value)
        page.editor.password_entry.insert(0, "123321")
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saved Printer 9.")
        self.assertEqual(page.editor.password_entry.get(), "")
        token = page.working.token
        self.assertEqual(unscramble(token, release_key()), make_token("123321"))
        self.assertEqual(page.editor.password_status.cget("text"),
                         "A password is saved. Type a new one only to change it.")
        secrets = ("123321", hashlib.md5(b"123321").hexdigest(), make_token("123321"))
        rel = hx_share.record_rel_for(Printer, page.working.id)
        with open(os.path.join(self.world.share_root, *rel.split("/")), encoding="utf-8") as f:
            record = f.read()
        self.assertEqual(json.loads(record)["token"], token)
        logs = []
        for folder, _, names in os.walk(os.path.join(self.world.share_root, "logs", "hx-admin")):
            for name in names:
                with open(os.path.join(folder, name), "rb") as f:
                    logs.append(f.read().decode("utf-8", "replace"))
        self.assertTrue(logs)
        self.assertTrue(any('"(set)"' in log for log in logs))
        for secret in secrets:
            self.assertNotIn(secret, record)
            for log in logs:
                self.assertNotIn(secret, log)
        # Changing the address keeps the token; a failed save keeps it too, without typing it again
        fake = self.world.printers[0]
        page.fields["host"].set(fake.host)                  # Printer 1's address and port
        page.fields["port"].set(str(fake.port))
        page.save_button.invoke()
        self.assertEqual(page.problems_label.cget("text"), "Printer 1 already has this address.")
        page.fields["host"].set("192.0.2.29")
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saved Printer 9.")
        self.assertEqual(page.working.token, token)
        # A token from another build's key
        page.working.token = "hx1:00000000:AAAA"
        page.editor._show_status(page.working)
        self.assertEqual(page.editor.password_status.cget("text"),
                         "The saved password was made by a different Stripes build. Type it again.")

    def test_film_files_and_previews(self):
        services, film_id = self.services, self.world.ids["film"]
        w = AdminWindow(self.root, services, page="products", record_id=film_id)
        page = w.pages["products"]
        editor = page.editor
        self.assertIsInstance(editor, FilmEditor)
        self.assertEqual(page.working.id, film_id)
        self.assertEqual(editor.add_kind_box.key(), "front_photo")
        self.assertEqual(editor.thumbs["front_photo"].cget("text"), "No photo")
        pictures = []
        for n, colour in enumerate(("red", "blue")):
            path = os.path.join(self.tmp, f"front{n}.jpg")
            Image.new("RGB", (900, 600), colour).save(path, "JPEG")
            pictures.append(path)
        with mock.patch.object(hx_ui_admin.filedialog, "askopenfilename", return_value=pictures[0]) as ask_file:
            editor.add_file_button.invoke()
        self.assertEqual(ask_file.call_args[1]["title"], "Choose a file to add")
        first = page.working.files[-1]
        self.assertEqual((first.kind, first.name, first.added_by), ("front_photo", "front0.jpg", "EXAMPLE\\jsmith"))
        templates = os.path.join(self.world.share_root, "templates")
        self.assertTrue(os.path.exists(os.path.join(templates, *first.path.split("/"))))      # save_file ran
        preview = os.path.join(templates, "films", film_id, "previews", f"{first.sha256}.png")
        self.assertTrue(os.path.exists(preview))                                               # save_preview ran
        self.assertNotEqual(str(editor.thumbs["front_photo"].cget("image")), "")
        self.assertTrue(page.dirty)
        self.assertEqual(editor.files_note.cget("text"), "The file is added. Save the film to keep it.")
        self.assertEqual(editor.add_kind_box.key(), "back_photo")
        # A second front photo replaces the first reference
        editor.add_kind_box.set_key("front_photo")
        with mock.patch.object(hx_ui_admin.filedialog, "askopenfilename", return_value=pictures[1]):
            editor.add_file_button.invoke()
        fronts = [f for f in page.working.files if f.kind == "front_photo"]
        self.assertEqual([f.name for f in fronts], ["front1.jpg"])
        # A PDF gets no preview
        pdf = os.path.join(self.tmp, "spec.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4\n% not a picture\n")
        editor.add_kind_box.set_key("spec")
        with mock.patch.object(hx_ui_admin.filedialog, "askopenfilename", return_value=pdf):
            editor.add_file_button.invoke()
        spec = page.working.files[-1]
        self.assertEqual(spec.kind, "spec")
        self.assertFalse(os.path.exists(os.path.join(templates, "films", film_id, "previews", f"{spec.sha256}.png")))
        self.assertEqual(len(editor.files_tree.get_children()), 2)
        # Nothing chosen: nothing happens
        with mock.patch.object(hx_ui_admin.filedialog, "askopenfilename", return_value=""):
            editor.add_file_button.invoke()
        self.assertEqual(len(page.working.files), 2)
        # Saved with the film; the test print waits for the save
        self.assertFalse(enabled(editor.test_button))
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saved EXCO1 · Rev C.")
        saved = from_json(Film, self.admin_store().read_json(hx_share.record_rel_for(Film, film_id)))
        self.assertEqual([f.kind for f in saved.files], ["front_photo", "spec"])
        self.assertTrue(enabled(editor.test_button))
        editor.test_button.invoke()
        self.assertEqual(services.nav.calls[-1], ("open_test_print", film_id, "", "test"))
        # Remove from film: the original stays on the share
        editor.files_tree.selection_set("1")
        editor.remove_file_button.invoke()
        self.assertEqual([f.kind for f in page.working.files], ["front_photo"])
        self.assertTrue(os.path.exists(os.path.join(templates, *spec.path.split("/"))))

    def test_film_origin_setting_unticks_print_texts(self):
        services, film_id = self.services, self.world.ids["film"]
        w = AdminWindow(self.root, services, page="products", record_id=film_id)
        page = w.pages["products"]
        editor = page.editor
        pt_id = self.world.ids["print_text"]
        self.assertEqual(list(editor.pt_vars), [pt_id])
        self.assertTrue(editor.pt_vars[pt_id].get())
        self.assertEqual(editor.default_box.record_id(), pt_id)
        self.assertFalse(enabled(editor.preprinted_entry))
        editor.fields["origin_preprinted"].set(True)
        self.assertEqual(editor.pt_ids, [])
        self.assertEqual(editor.unticked_note.cget("text"), "Print texts that don't suit this setting were unticked.")
        self.assertEqual(list(editor.pt_vars), [])
        self.assertEqual(editor.default_box.record_id(), "")
        self.assertTrue(enabled(editor.preprinted_entry))
        editor.fields["preprinted_origin"].set("USA")
        self.assertEqual(editor.origin_preview.get(), "USA (pre-printed)")
        page.save_button.invoke()
        text = page.problems_label.cget("text")
        self.assertIn("An active film needs at least one print text and a default print text.", text)
        # Warnings that don't block: an active film without a delay
        self.ask.return_value = True
        page.revert_button.invoke()
        self.assertFalse(page.dirty)
        editor.fields["delay_ms"].set("")
        page.save_button.invoke()
        self.assertEqual(page.warnings_label.cget("text"), "Delay machines can't send this film until it has a delay.")
        self.assertEqual(page.status_label.cget("text"), "Saved EXCO1 · Rev C.")

    def test_new_version_is_a_blank_film(self):
        services = self.services
        w = AdminWindow(self.root, services, page="products", record_id=self.world.ids["product"])
        page = w.pages["products"]
        self.assertEqual(page.editor.__class__.__name__, "ProductEditor")
        self.assertEqual(page.editor.films_label.cget("text"), "EXCO1 · Rev C")
        page.new_version_button.invoke()
        self.assertIsInstance(page.editor, FilmEditor)
        film = page.working
        self.assertEqual((film.product_id, film.code, film.status), (self.world.ids["product"], "", "development"))
        self.assertTrue(shown(page.editor.new_note))
        page.fields["code"].set("EXCO2")
        page.fields["artwork_revision"].set("Rev D")
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saved EXCO2 · Rev D.")
        self.reload()
        self.assertIn(film.id, page.tree.get_children(self.world.ids["product"]))

    def test_print_text_membership(self):
        services, film_id, pko = self.services, self.world.ids["film"], self.world.ids["print_text"]
        w = AdminWindow(self.root, services, page="print_texts")
        page = w.pages["print_texts"]
        editor = page.editor
        self.assertIsInstance(editor, PrintTextEditor)
        page.new_button.invoke()
        page.fields["name"].set("Packed on + origin + best-by")
        page.fields["code"].set("PKB")
        page.fields["status"].set_key("active")
        for kind in ("packed_date", "origin", "best_by_date"):
            editor.add_kind_box.set_key(kind)
            editor.add_item_button.invoke()
        self.assertEqual([it.id for it in editor.items], ["i1", "i2", "i3"])
        editor.add_kind_box.set_key("origin")
        editor.add_item_button.invoke()
        self.assertEqual(editor.add_problem.cget("text"), "A print text has at most one origin item.")
        self.assertEqual(len(editor.items), 3)
        editor.items_tree.selection_set("i2")
        self.settle()
        editor.item_fields["placement"].set("below")
        self.assertEqual(editor.items_tree.item("i2", "values")[5], "Below")
        editor.items_tree.selection_set("i3")
        self.settle()
        editor.item_fields["before"].set("Best by ")
        self.assertEqual(editor.items_tree.item("i3", "values")[2], '"Best by "')
        self.assertIn("Best by", editor.preview.get())
        self.assertIn(film_id, editor.film_checks)
        self.assertFalse(editor.film_vars[film_id].get())
        editor.film_checks[film_id].invoke()
        self.assertTrue(editor.film_vars[film_id].get())
        page.save_button.invoke()
        self.assertEqual(page.problems_label.cget("text"), "")
        self.assertEqual(page.status_label.cget("text"), "Saved Packed on + origin + best-by.")
        new_id = page.working.id
        store = self.admin_store()
        film = from_json(Film, store.read_json(hx_share.record_rel_for(Film, film_id)))
        self.assertEqual(film.print_text_ids, [pko, new_id])
        self.assertTrue(editor.film_vars[film_id].get())       # what was saved, before the next DataUpdate
        self.reload()
        self.assertTrue(editor.film_vars[film_id].get())
        # Unticking a film's default is refused, and nothing is saved
        page.select_record(pko)
        self.assertEqual(page.working.id, pko)
        revision = page.expected_revision
        self.assertIn("Default for this film", label_texts(editor.films_frame))
        editor.film_checks[film_id].invoke()
        page.save_button.invoke()
        self.assertEqual(page.problems_label.cget("text"), "EXCO1 · Rev C uses this print text as its default. Choose "
                                                           "another default in that film first.")
        self.assertEqual(from_json(PrintText, store.read_json(hx_share.record_rel_for(PrintText, pko))).revision,
                         revision)
        film = from_json(Film, store.read_json(hx_share.record_rel_for(Film, film_id)))
        self.assertEqual(film.print_text_ids, [pko, new_id])

    def test_typing_in_an_item_keeps_its_editor(self):
        """ui.md §11.8.2 items 4-5: typing in an item's field updates its row in place; the item editor
        (and the Entry being typed in) stays."""
        services, pt_id = self.services, self.world.ids["print_text"]
        w = AdminWindow(self.root, services, page="print_texts", record_id=pt_id)
        w.deiconify()
        self.settle(0.2)
        page = w.pages["print_texts"]
        editor = page.editor
        lot = [it for it in editor.items if it.kind == "lot"][0]
        editor.items_tree.selection_set(lot.id)
        self.settle()
        before_var = editor.item_fields["before"]
        entry = [c for c in descendants(editor.item_editor) if c.winfo_class() == "TEntry"][0]
        before_var.set("Lot x")
        self.settle()
        self.assertIs(editor.item_fields["before"], before_var)
        self.assertTrue(alive(entry))
        self.assertEqual(editor.items_tree.item(lot.id, "values")[2], '"Lot x"')
        self.assertEqual(editor.items_tree.selection(), (lot.id,))
        self.assertTrue(page.dirty)
        # Real key presses, one after the other, all reach the item
        before_var.set("Lot ")
        entry.focus_force()
        entry.icursor("end")
        self.settle(0.1)
        for key in ("A", "B"):
            (w.focus_get() or entry).event_generate("<KeyPress>", keysym=key)
            self.settle(0.05)
        self.assertEqual(lot.before, "Lot AB")
        self.assertTrue(alive(entry))
        # The gap of an item (a Spinbox) the same way
        gap_var = editor.item_fields["gap"]
        gap_var.set("12")
        self.settle()
        self.assertIs(editor.item_fields["gap"], gap_var)
        self.assertEqual(lot.gap, 12)
        self.assertEqual(str(editor.items_tree.item(lot.id, "values")[6]), "12")
        # Moving an item builds its editor again: the first item has no placement or gap
        first = editor.items[0]
        editor.items_tree.selection_set(editor.items[1].id)
        self.settle()
        self.assertIn("placement", editor.item_fields)
        editor.up_button.invoke()
        self.settle()
        self.assertNotIn("placement", editor.item_fields)
        self.assertNotIn("gap", editor.item_fields)
        self.assertIsNot(editor.items[0], first)
        # Another item still opens its own fields
        editor.items_tree.selection_set(first.id)
        self.settle()
        self.assertIn("placement", editor.item_fields)
        self.assertEqual(editor.item_fields["before"].get(), "Packed on ")

    def test_bad_item_number_is_a_problem(self):
        """ui.md §11.1 step 5.1, §11.8.2 item 4: a bad or empty Gap, Most characters or Offset is a
        problem, shown live and on Save, and nothing is saved; the field keeps what was typed."""
        services, pt_id = self.services, self.world.ids["print_text"]
        store = self.admin_store()
        w = AdminWindow(self.root, services, page="print_texts", record_id=pt_id)
        page = w.pages["print_texts"]
        editor = page.editor
        revision = page.expected_revision
        lot = [it for it in editor.items if it.kind == "lot"][0]
        old_gap = lot.gap
        editor.items_tree.selection_set(lot.id)
        self.settle()
        editor.item_fields["gap"].set("4O")         # a letter O for a zero
        self.settle()
        self.assertEqual(lot.gap, old_gap)
        self.assertEqual(page.problems_label.cget("text"), "Gap: type a whole number.")
        # Shown again when the item is
        editor.items_tree.selection_set(editor.items[0].id)
        self.settle()
        self.assertNotIn("gap", editor.item_fields)
        page.save_button.invoke()
        self.settle()
        self.assertEqual(page.problems_label.cget("text"), "Gap: type a whole number.")
        self.assertEqual(editor.items_tree.selection(), (lot.id,))      # Save shows the item with the problem
        self.assertEqual(editor.item_fields["gap"].get(), "4O")
        on_share = from_json(PrintText, store.read_json(hx_share.record_rel_for(PrintText, pt_id)))
        self.assertEqual(on_share.revision, revision)
        # Empty
        editor.item_fields["gap"].set("")
        self.settle()
        page.save_button.invoke()
        self.settle()
        self.assertEqual(page.problems_label.cget("text"), "Gap is needed.")
        on_share = from_json(PrintText, store.read_json(hx_share.record_rel_for(PrintText, pt_id)))
        self.assertEqual(on_share.revision, revision)
        # Fixed: saved
        editor.item_fields["gap"].set("40")
        self.settle()
        self.assertEqual(page.problems_label.cget("text"), "")
        page.save_button.invoke()
        self.settle()
        on_share = from_json(PrintText, store.read_json(hx_share.record_rel_for(PrintText, pt_id)))
        self.assertEqual([it.gap for it in on_share.items if it.id == lot.id], [40])
        # Most characters and Offset the same way
        editor.add_kind_box.set_key("free_text")
        editor.add_item_button.invoke()
        self.settle()
        editor.item_fields["max_len"].set("x")
        self.settle()
        self.assertIn("Most characters: type a whole number.", page.problems_label.cget("text"))
        editor.remove_item_button.invoke()
        self.settle()
        self.assertNotIn("Most characters", page.problems_label.cget("text"))
        editor.add_kind_box.set_key("best_by_date")
        editor.add_item_button.invoke()
        self.settle()
        best_by = editor.items[-1]
        self.assertEqual(editor.item_fields["offset"].get(), "1")      # not the removed item's typing
        editor.item_fields["offset"].set("")
        self.settle()
        self.assertIn("Offset is needed.", page.problems_label.cget("text"))
        self.assertEqual(best_by.offset, 1)
        # Revert drops what was typed
        self.ask.return_value = True
        page.revert_button.invoke()
        self.settle()
        self.assertEqual(page.problems_label.cget("text"), "")
        self.assertEqual(editor.number_problems(), [])

    def test_discard_when_leaving_by_another_tab(self):
        """ui.md §11.1 step 4: "Discard" when leaving by another page or tab drops the edit, so the page
        shows the saved record again and a later Save can't write it."""
        services, line_id = self.services, self.world.ids["line"]
        store = self.admin_store()
        w = AdminWindow(self.root, services, page="lines", record_id=line_id)
        page = w.pages["lines"]
        # select() with the leaving question ("Open the film", "Add a machine")
        page.fields["name"].set("Line 1 DISCARDED")
        self.assertTrue(page.dirty)
        self.ask.return_value = True
        w.select("machines")
        self.assertIs(w.current_page(), w.pages["machines"])
        self.assertEqual(self.ask.call_args[0][1].text, "Discard your changes to {name}?")
        w.select("lines")
        self.assertEqual(page.fields["name"].get(), "Line 1")
        self.assertEqual(page.working.name, "Line 1")
        self.assertFalse(page.dirty)
        # Clicking another tab
        page.fields["name"].set("Line 1 DISCARDED")
        w.notebook.select(w.pages["machines"])
        self.settle()
        self.assertIs(w._shown_page, w.pages["machines"])
        w.notebook.select(page)
        self.settle()
        self.assertEqual(page.fields["name"].get(), "Line 1")
        self.assertFalse(page.dirty)
        page.save_button.invoke()
        on_share = from_json(Line, store.read_json(hx_share.record_rel_for(Line, line_id)))
        self.assertEqual(on_share.name, "Line 1")
        # "Keep editing" keeps the page and the edit
        page.fields["name"].set("Line 1 North")
        self.ask.return_value = False
        w.notebook.select(w.pages["machines"])
        self.settle()
        self.assertIs(w.current_page(), page)
        self.assertEqual(page.fields["name"].get(), "Line 1 North")
        self.assertTrue(page.dirty)
        # A new record that is discarded is gone
        self.ask.return_value = True
        printers = w.pages["printers"]
        w.select("printers")
        printers.new_button.invoke()
        printers.fields["name"].set("Printer 9")
        w.select("lines")
        self.assertIsNone(printers.working)
        self.assertFalse(printers.dirty)

    def test_discard_when_closing_the_window(self):
        """ui.md §11.1 step 4, by the close box: "Discard" for one page then "Keep editing" for another
        leaves the window open, with the first page showing its record as saved, so a later Save can't
        write the discarded edit."""
        services, line_id, mid = self.services, self.world.ids["line"], self.mid()
        store = self.admin_store()
        w = AdminWindow(self.root, services, page="lines", record_id=line_id)
        lines, machines = w.pages["lines"], w.pages["machines"]
        machines.select_record(mid)
        machines.fields["name"].set("Bagger edit")
        lines.fields["name"].set("Line 1 DISCARDED")
        self.assertTrue(lines.dirty and machines.dirty)
        answers = iter([True, False])           # lines (first in page order): Discard; machines: Keep editing
        self.ask.side_effect = lambda *a, **k: next(answers)
        w.close()
        self.ask.side_effect = None
        self.assertTrue(alive(w))
        self.assertIs(w.current_page(), machines)
        self.assertEqual(machines.fields["name"].get(), "Bagger edit")
        self.assertTrue(machines.dirty)
        self.assertEqual(lines.fields["name"].get(), "Line 1")
        self.assertEqual(lines.working.name, "Line 1")
        self.assertFalse(lines.dirty)
        lines.save_button.invoke()
        self.settle()
        on_share = from_json(Line, store.read_json(hx_share.record_rel_for(Line, line_id)))
        self.assertEqual(on_share.name, "Line 1")
        # Discard for both: the window closes
        self.ask.return_value = True
        w.close()
        self.assertFalse(alive(w))

    def test_close_while_saving_waits_for_the_save(self):
        """Closing while a save is in flight asks nothing (the edit is being saved, not discarded); the
        window closes once the save has its answer."""
        services, line_id = self.services, self.world.ids["line"]
        store = self.admin_store()
        bg = services.bg = LaterBackground()
        w = AdminWindow(self.root, services, page="lines", record_id=line_id)
        bg.flush()
        page = w.pages["lines"]
        page.fields["name"].set("Line 1 North")
        page.save_button.invoke()
        self.ask.reset_mock()
        w.close()
        self.ask.assert_not_called()
        self.assertTrue(alive(w))
        bg.flush()
        self.settle()
        self.assertFalse(alive(w))
        on_share = from_json(Line, store.read_json(hx_share.record_rel_for(Line, line_id)))
        self.assertEqual(on_share.name, "Line 1 North")

    def test_close_while_a_save_fails_stays_open(self):
        """A save that fails after the close box was pressed leaves the window open on that page, with
        the problem and the edit, so the admin knows it wasn't saved."""
        services, line_id = self.services, self.world.ids["line"]
        store = self.admin_store()
        bg = services.bg = LaterBackground()
        w = AdminWindow(self.root, services, page="lines", record_id=line_id)
        bg.flush()
        page = w.pages["lines"]
        line = services.data.current.lines[line_id]
        # Someone else saves first, so this save is refused
        hx_share.write_record(store, dataclasses.replace(line, name="Line 1 South", revision=line.revision + 1))
        self.bump_changes(store)
        page.fields["name"].set("Line 1 North")
        page.save_button.invoke()
        w.select("machines")                    # no question while saving
        self.assertIs(w.current_page(), w.pages["machines"])
        w.close()
        self.assertTrue(alive(w))
        bg.flush()
        self.settle()
        self.assertTrue(alive(w))
        self.assertIs(w.current_page(), page)
        self.assertTrue(page.problems_label.cget("text"))
        self.assertTrue(page.dirty)
        self.assertEqual(page.fields["name"].get(), "Line 1 North")
        self.assertFalse(w._close_after_save)

    def test_no_record_change_while_saving(self):
        """ui.md §11.1 steps 2-5 with the app's Background (done runs later): while a save is in flight the
        editor doesn't move to another record, so its answer can't land under another row."""
        services, line1 = self.services, self.world.ids["line"]
        admin = self.admin_store()
        line2 = Line(id="ln_0000000099", revision=1, name="Line 2", code="L2", cartridge="plastic")
        hx_share.write_record(admin, line2)
        self.bump_changes(admin)
        self.reload()
        bg = services.bg = LaterBackground()
        w = AdminWindow(self.root, services, page="lines", record_id=line1)
        page = w.pages["lines"]
        page.fields["name"].set("Line 1 North")
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saving...")
        self.assertFalse(enabled(page.new_button))
        self.assertFalse(enabled(page.revert_button))
        self.ask.reset_mock()
        page.tree.selection_set(line2.id)
        self.settle()
        self.assertEqual(page.working.id, line1)
        self.assertEqual(page.tree.selection(), (line1,))
        self.ask.assert_not_called()
        page.new_button.invoke()
        page.select_record(line2.id)
        self.assertEqual(page.working.id, line1)
        bg.flush()
        self.settle()
        self.assertEqual(page.status_label.cget("text"), "Saved Line 1 North.")
        self.assertEqual(page.tree.selection(), (page.working.id,))
        self.assertEqual(page.fields["name"].get(), "Line 1 North")
        self.assertFalse(page.dirty)
        self.assertTrue(enabled(page.new_button))
        # Now another row opens as usual
        page.tree.selection_set(line2.id)
        self.settle()
        self.assertEqual(page.working.id, line2.id)
        self.assertEqual(page.fields["name"].get(), "Line 2")

    def test_printer_address_spaces_are_dropped(self):
        """ui.md §11.5 item 4: the address is saved without the spaces typed around it."""
        services = self.services
        w = AdminWindow(self.root, services, page="printers")
        page = w.pages["printers"]
        page.new_button.invoke()
        page.fields["name"].set("Printer 9")
        page.fields["host"].set(" 192.0.2.50 ")
        page.fields["port"].set("80")
        page.fields["uid"].set("1109")
        page.save_button.invoke()
        self.assertEqual(page.problems_label.cget("text"), "")
        self.assertEqual(page.working.host, "192.0.2.50")
        self.assertEqual(page.fields["host"].get(), "192.0.2.50")
        saved = from_json(Printer, self.admin_store().read_json(hx_share.record_rel_for(Printer, page.working.id)))
        self.assertEqual(saved.host, "192.0.2.50")

    def test_print_text_origin_change_cant_strand_films(self):
        """ui.md §11.8.5 and send.md §2.4: removing the origin item from a print text that films allow is
        refused, naming the film; a film already left with a print text that doesn't suit it lists that
        print text, so it can be unticked (§11.7.1)."""
        services, pt_id, film_id = self.services, self.world.ids["print_text"], self.world.ids["film"]
        store = self.admin_store()
        revision = from_json(PrintText, store.read_json(hx_share.record_rel_for(PrintText, pt_id))).revision
        w = AdminWindow(self.root, services, page="print_texts", record_id=pt_id)
        page = w.pages["print_texts"]
        editor = page.editor
        origin = [it for it in editor.items if it.kind == "origin"][0]
        editor.items_tree.selection_set(origin.id)
        self.settle()
        editor.remove_item_button.invoke()
        stranded = ("EXCO1 · Rev C: Print text Packed on + origin has no origin item, but this film needs one.")
        self.assertIn(stranded, page.problems_label.cget("text"))         # shown live
        page.save_button.invoke()
        self.assertIn(stranded, page.problems_label.cget("text"))
        on_share = from_json(PrintText, store.read_json(hx_share.record_rel_for(PrintText, pt_id)))
        self.assertEqual(on_share.revision, revision)
        self.assertTrue(any(it.kind == "origin" for it in on_share.items))
        # A film that already allows a print text without an origin item (written before this check)
        self.ask.return_value = True
        page.revert_button.invoke()
        data = services.data.current
        film = data.films[film_id]
        pkl = PrintText(id="pt_0000000099", revision=1, name="Packed on + lot", code="PKL", status="active",
                        items=[PrintTextItem(id="i1", kind="lot", before="Lot ")])
        hx_share.write_record(store, pkl)
        hx_share.write_record(store, dataclasses.replace(film, print_text_ids=[pt_id, pkl.id]))
        self.bump_changes(store)
        self.reload()
        w.select("products", film_id)
        fe = w.pages["products"].editor
        self.assertIsInstance(fe, FilmEditor)
        self.assertEqual(set(fe.pt_vars), {pt_id, pkl.id})
        self.assertTrue(fe.pt_vars[pkl.id].get())
        fe.pt_vars[pkl.id].set(False)
        fe._pt_toggled(pkl.id)
        self.assertEqual(set(fe.pt_vars), {pt_id})
        w.pages["products"].save_button.invoke()
        self.assertEqual(w.pages["products"].problems_label.cget("text"), "")
        saved = from_json(Film, store.read_json(hx_share.record_rel_for(Film, film_id)))
        self.assertEqual(saved.print_text_ids, [pt_id])

    def test_company_questions(self):
        services = self.services
        w = AdminWindow(self.root, services, page="company")
        page = w.pages["company"]
        form = page.editor
        self.assertEqual(page.working.id, "company")
        self.assertEqual(form.member_labels["admin_group"].cget("text"), "You are in this group.")
        self.assertEqual(form.countries_tree.item("0", "values")[:2], ("USA", "US, U.S.A., United States"))
        # A new country that a line code contains: a question that needs an answer
        form.country_name_var.set("L1")
        form.country_aliases_var.set(" , ")
        form.add_country_button.invoke()
        self.assertEqual([c.name for c in form.countries][-1], "L1")
        self.assertEqual(form.countries[-1].aliases, [])
        self.ask.return_value = False
        page.save_button.invoke()
        msg = self.ask.call_args[0][1]
        self.assertIn("these records contain words about origin", msg.text)
        self.assertIn("The code of Line 1 now contains words about origin: L1.", msg.values["list"])
        self.assertTrue(page.dirty)
        # Leaving the administrators group asks too
        form.countries.pop()
        form._show_countries()
        page.fields["admin_group_sid"].set("S-1-5-21-0")
        self.assertEqual(form.member_labels["admin_group"].cget("text"), "You aren't in this group.")
        page.save_button.invoke()
        self.assertEqual(self.ask.call_args[0][1].text, "You aren't in {group}. After saving, you won't be able to "
                                                        "change the MapleJet settings. Save anyway?")
        self.assertEqual(self.ask.call_args[1], {"yes": "Save anyway", "no": "Go back"})
        page.fields["admin_group_sid"].set(page.working.admin_group_sid)
        page.fields["lot_max_digits"].set("9")
        self.ask.return_value = True
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saved Company.")
        company = from_json(Company, self.admin_store().read_json("config/hx/company.json"))
        self.assertEqual(company.lot_max_digits, 9)
        # Look up: Windows doesn't know the group here
        self.patch(hx_ui_admin.hx_win, "lookup_group_sid", return_value=None)
        form.admin_lookup_button.invoke()
        self.assertEqual(hx_ui_common.text(self.tell.call_args[0][1]),
                         "Windows couldn't find a group called Stripes Administrators.")

    def test_machine_placement_question_and_adjustment(self):
        services, mid, film_id = self.services, self.mid(), self.world.ids["film"]
        w = AdminWindow(self.root, services, page="machines", record_id=mid)
        page = w.pages["machines"]
        form = page.editor
        self.assertEqual(form.printer_box.get(), f"Printer 1 (127.0.0.1)")
        self.assertIn("Delay: each film's delay", form.placement_hint.cget("text"))
        self.assertFalse(enabled(form.calibrate_button))
        form.fields["placement"].set("encoder")
        self.assertIn("Encoder: each film's print position", form.placement_hint.cget("text"))
        self.ask.return_value = False
        page.save_button.invoke()
        self.assertTrue(self.ask.call_args[0][1].text.startswith("Change the placement mode of {machine}?"))
        self.assertTrue(page.dirty)
        self.ask.return_value = True
        page.revert_button.invoke()
        self.assertEqual(page.fields["placement"].get(), "delay")
        # The machine + film adjustment dialog
        dialog = AdjustmentDialog(self.root, services, machine_id=mid, film_id=film_id)
        self.assertEqual(dialog.value_caption.cget("text"), "Delay adjustment (ms)")
        self.assertEqual(dialog.mode_label.cget("text"), "Delay machine")
        dialog.value_var.set("+20")
        self.assertEqual(dialog.working_label.cget("text"), "480 + 20 = 500 ms = 500000 µs")
        dialog.value_var.set("20000")
        dialog.save_button.invoke()
        self.assertEqual(dialog.problems_label.cget("text"), "The delay adjustment must be between -10000 and 10000 "
                                                             "ms.")
        dialog.value_var.set("20")
        dialog.save_button.invoke()
        self.assertFalse(alive(dialog))
        self.reload()
        adj = services.data.current.adjustment(mid, film_id)
        self.assertEqual((adj.delay_ms, adj.position_mm, adj.updated_by), (20.0, 0.0, "EXAMPLE\\jsmith"))
        adjustments = w.pages["adjustments"]
        self.assertEqual(adjustments.tree.item(adj.id, "values")[3], "+20 ms")
        films = w.pages["products"]
        w.select("products", film_id)
        self.assertEqual(films.editor.adjust_tree.item(mid, "values")[2], "+20 ms")


    def test_language_switch(self):
        services, film_id, pko = self.services, self.world.ids["film"], self.world.ids["print_text"]
        w = AdminWindow(self.root, services, page="print_texts", record_id=pko)
        editor = w.pages["print_texts"].editor
        editor.items_tree.selection_set("i1")
        self.settle()
        w.select("products", film_id)
        test = TestPrintWindow(self.root, services, film_id=film_id, machine_id=self.mid())
        bags = [(b, b.get()) for win in (w, test) for b in descendants(win) if isinstance(b, hx_ui_common.BagText)]
        # The seeded print text's lot has no gap, so it follows the origin straight on
        self.assertEqual(editor.preview.get().split("\n")[1], "Product of USA, Canada, and MexicoLot 88888888")
        self.assertEqual(editor.lengths_label.cget("text"), "Longest line: 46 characters.")
        i18n.set_language("es")
        for window in (w, test):
            window.retranslate()
        self.assertEqual(w.title(), "Administración de MapleJet")
        self.assertEqual(w.notebook.tab(w.pages["products"], "text"), "Productos y películas")
        self.assertEqual(w.pages["lines"].tree.heading("cartridge", "text"), "Cartucho")
        self.assertEqual(w.pages["lines"].tree.item(self.world.ids["line"], "values")[2], "Plástico")
        self.assertEqual(w.pages["products"].editor.fields["status"].get(), "Activo")
        self.assertEqual(editor.items_tree.item("i2", "values")[1], "País de origen")
        self.assertEqual(test.estimate_label.cget("text"),
                         "5 pasos · unas 30 bolsas. Cada paso dura 5 bolsas, contadas por la impresora.")
        self.assertEqual([(b, b.get()) for b, _ in bags], bags)
        i18n.set_language("en")
        for window in (w, test):
            window.retranslate()
        self.assertEqual(w.notebook.tab(w.pages["products"], "text"), "Products and films")
        self.assertEqual(editor.items_tree.item("i2", "values")[1], "Country of origin")


    def test_test_print_hint_only_while_the_button_is_disabled(self):
        """ui.md §11.7.5: "Save the film first." says why Test print... is disabled, so it shows only then."""
        services, film_id = self.services, self.world.ids["film"]
        w = AdminWindow(self.root, services, page="products", record_id=film_id)
        page = w.pages["products"]
        editor = page.editor
        self.assertTrue(enabled(editor.test_button))
        self.assertFalse(shown(editor.test_hint))
        page.fields["artwork_revision"].set("Rev C2")
        self.assertFalse(enabled(editor.test_button))
        self.assertTrue(shown(editor.test_hint))
        self.assertEqual(editor.test_hint.cget("text"), "Save the film first.")
        page.save_button.invoke()
        self.assertEqual(page.status_label.cget("text"), "Saved EXCO1 · Rev C2.")
        self.assertTrue(enabled(editor.test_button))
        self.assertFalse(shown(editor.test_hint))
        # A new film was never saved
        page.new_version_button.invoke()
        self.assertIs(page.editor, editor)
        self.assertFalse(enabled(editor.test_button))
        self.assertTrue(shown(editor.test_hint))
        self.settle()
        self.assertGreater(editor.test_hint.winfo_x(), editor.test_button.winfo_x())     # beside the button

    def test_films_by_code_then_revision(self):
        """Every admin list of films is in the Send window's order: by code, then artwork revision."""
        services, product_id, film_id = self.services, self.world.ids["product"], self.world.ids["film"]
        store = self.admin_store()
        base = services.data.current.films[film_id]                    # EXCO1 · Rev C
        for n, (code, revision) in enumerate((("EXCO10", "Rev A"), ("EXCO2", "Rev B"), ("EXCO2", "Rev 10"),
                                              ("EXCO2", "Rev 9"))):
            hx_share.write_record(store, dataclasses.replace(base, id=f"fm_00000000a{n}", code=code,
                                                             artwork_revision=revision, status="development",
                                                             print_text_ids=[], default_print_text_id=""))
        self.bump_changes(store)
        self.reload()
        names = ["EXCO1 · Rev C", "EXCO2 · Rev 9", "EXCO2 · Rev 10", "EXCO2 · Rev B", "EXCO10 · Rev A"]
        ids = [film_id, "fm_00000000a3", "fm_00000000a2", "fm_00000000a1", "fm_00000000a0"]
        w = AdminWindow(self.root, services, page="products", record_id=product_id)
        page = w.pages["products"]
        self.assertEqual(list(page.tree.get_children(product_id)), ids)
        self.assertEqual(page.editor.films_label.cget("text"), "\n".join(names))
        self.assertEqual(list(w.pages["adjustments"].film_filter.cget("values")), ["All films"] + names)
        dialog = AdjustmentDialog(self.root, services, machine_id=self.mid(), film_id=film_id)
        self.assertEqual(list(dialog.film_box.cget("values")), names)
        dialog.destroy()
        w.select("print_texts", self.world.ids["print_text"])
        self.assertEqual(list(w.pages["print_texts"].editor.film_vars), ids)


class LayoutTests(UiCase):
    """The admin window at its own size, and wider: screenshots of the finished app showed the Lines,
    Machines and film forms with no fields at all (the field column squeezed to nothing) and the
    print-text designer's hints running together off the right edge."""
    FIELDS = ("TEntry", "TCombobox", "TSpinbox")
    TEXTS = ("TLabel", "TCheckbutton", "TRadiobutton", "TButton")
    CODE_HINT = "Up to 3 capital letters or digits, used in the printer's message name."
    REVISION_HINT = "Goes up on every save. The send log records which one printed."

    def steady(self, *widgets, timeout_s=3.0):
        """Pump until the window has its size and the widgets' sizes have stopped changing: wrapped
        hints take a few layout passes to settle."""
        deadline = time.monotonic() + timeout_s
        last, same = None, 0
        while same < 5 and time.monotonic() < deadline:
            self.root.update()
            self.services.bus.pump()
            now = [(w.winfo_width(), w.winfo_height()) for w in widgets]
            same = same + 1 if now == last else 0
            last = now
            time.sleep(0.01)

    def resize(self, w, size):
        w.geometry(f"{size}+0+0")
        deadline = time.monotonic() + 3.0
        while f"{w.winfo_width()}x{w.winfo_height()}" != size and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.steady(w)
        return f"{w.winfo_width()}x{w.winfo_height()}" == size

    def open_at(self, size):
        w = AdminWindow(self.root, self.services)
        self.addCleanup(lambda: alive(w) and w.destroy())
        w.maxsize(4000, 4000)           # Tk on Windows caps a window at the screen's size unless told otherwise
        w.deiconify()
        if not self.resize(w, size):
            self.skipTest(f"this screen can't show a {size} window")
        return w

    def check_editor(self, w, page_key, record_id="", item=""):
        """Every field of the editor is shown at the width it asks for (so at least 120 px, or all of
        its own width when it asks for less), and nothing in it is cut off or past its right edge."""
        w.select(page_key, record_id)
        page = w.pages[page_key]
        form = page.editor
        if item:
            form.items_tree.selection_set(item)
        self.steady(form, page.editor_scroller.canvas)
        where = f"{page_key} {type(form).__name__} {item}".strip()
        canvas = page.editor_scroller.canvas
        right = canvas.winfo_rootx() + canvas.winfo_width()
        self.assertEqual(page.editor_xbar.winfo_manager(), "", f"{where} scrolls sideways")
        fields = [c for c in descendants(form) if c.winfo_class() in self.FIELDS]
        self.assertTrue(any(f.winfo_width() >= 120 for f in fields), f"{where}: no field of 120 px")
        for field in fields:
            name = f"{where}: {field.winfo_class()} {field}"
            width, asked = field.winfo_width(), field.winfo_reqwidth()
            self.assertTrue(field.winfo_ismapped(), f"{name} isn't shown")
            self.assertGreaterEqual(width, min(120, asked), f"{name} is squeezed to {width} px")
            self.assertGreaterEqual(width, asked - 1, f"{name} is {width} px, not the {asked} px it asks for")
            self.assertLessEqual(field.winfo_rootx() + width, right, f"{name} runs past the editor's right edge")
        for widget in descendants(form):
            if widget.winfo_class() not in self.TEXTS or not widget.winfo_ismapped():
                continue
            name = f"{where}: {widget.cget('text')!r}"
            self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth() - 1, f"{name} is cut off")
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right,
                                 f"{name} runs past the editor's right edge")
        return form

    def check_window(self, size):
        ids = self.world.ids
        w = self.open_at(size)
        try:
            for page_key, record_id, item in (("company", "", ""), ("lines", ids["line"], ""),
                                              ("machines", ids["machine1"], ""), ("printers", ids["printer1"], ""),
                                              ("products", ids["product"], ""), ("products", ids["film"], ""),
                                              ("print_texts", ids["print_text"], ""),
                                              ("print_texts", ids["print_text"], "i1"),
                                              ("print_texts", ids["print_text"], "i3")):
                self.check_editor(w, page_key, record_id, item)
        finally:
            w.destroy()

    def test_editors_fit_the_window(self):
        for size, language in (("1180x820", "en"), ("1400x820", "en"), ("1180x820", "es")):
            with self.subTest(size=size, language=language):
                i18n.set_language(language)
                self.check_window(size)
        i18n.set_language("en")

    def test_print_text_hints_beside_their_own_fields(self):
        """The Code hint and the revision hint each sit on their own field's row, apart and whole."""
        w = self.open_at("1180x820")
        editor = self.check_editor(w, "print_texts", self.world.ids["print_text"])
        hints = {widget.cget("text"): widget for widget in descendants(editor) if widget.winfo_class() == "TLabel"}
        code_hint, revision_hint = hints[self.CODE_HINT], hints[self.REVISION_HINT]
        code_entry = next(e for e in descendants(editor)
                          if e.winfo_class() == "TEntry" and str(e.cget("textvariable")) == str(editor.fields["code"]))
        self.assertEqual(code_hint.grid_info()["row"], code_entry.grid_info()["row"])
        self.assertEqual(revision_hint.grid_info()["row"], editor.revision_text.grid_info()["row"])
        self.assertLessEqual(code_hint.winfo_y() + code_hint.winfo_height(), revision_hint.winfo_y())

    def test_narrow_editor_scrolls_sideways(self):
        """Below FORM_MIN_PX the editor keeps that width, with a sideways scrollbar, instead of
        squeezing its fields; wide again, the scrollbar goes."""
        w = self.open_at("1180x820")
        w.select("machines", self.world.ids["machine1"])
        page = w.pages["machines"]
        self.steady(page.editor)
        self.assertEqual(page.editor_xbar.winfo_manager(), "")
        w.minsize(400, 400)
        self.assertTrue(self.resize(w, "700x820"))
        self.assertEqual(page.editor_xbar.winfo_manager(), "pack")
        self.assertGreaterEqual(page.editor.winfo_width(), hx_ui_admin.FORM_MIN_PX - 8)
        name = next(e for e in descendants(page.editor) if e.winfo_class() == "TEntry")
        self.assertGreaterEqual(name.winfo_width(), name.winfo_reqwidth() - 1)
        page.editor_scroller.canvas.xview_moveto(0.5)
        self.steady(page.editor)
        self.assertGreater(page.editor_scroller.canvas.xview()[0], 0.0)
        self.assertTrue(self.resize(w, "1180x820"))
        self.assertEqual(page.editor_xbar.winfo_manager(), "")
        self.assertEqual(page.editor_scroller.canvas.xview()[0], 0.0)


class TestPrintTests(UiCase):
    def test_test_print_window(self):
        services, mid, film_id = self.services, self.mid(), self.world.ids["film"]
        fake = self.world.printers[0]
        with self.assertRaises(ValueError):
            TestPrintWindow(self.root, services, film_id=film_id, kind="spacing")
        w = TestPrintWindow(self.root, services, film_id=film_id, machine_id=mid)
        self.assertEqual(w.title(), "Test print")
        self.assertIn("Test print: EXCO1 · Rev C (Example Co 2lb Onion)", label_texts(w))
        self.assertEqual((w.start_entry.get(), w.stop_entry.get(), w.step_entry.get()), ("440", "520", "20"))
        self.assertEqual(w.vary_label.cget("text"), "Delay (ms)")
        self.assertEqual(w.estimate_label.cget("text"),
                         "5 steps · about 30 bags. Each step runs for 5 bags, counted by the printer.")
        self.assertFalse(enabled(w.start_button))              # empty film not confirmed yet
        self.assertEqual(w.empty_film_check.cget("text"), "Line 1 Bagger 1 is running empty film, with no product.")
        w.empty_film_check.invoke()
        self.assertTrue(enabled(w.start_button))
        w.bold_vars[2].set(True)
        w.recompute()
        self.assertTrue(w.estimate_label.cget("text").startswith("6 steps · about 36 bags."))
        w.bold_vars[2].set(False)
        w.step_var.set("x")
        self.assertEqual(w.problems_label.cget("text"), "Delay (ms): type a number.")
        self.assertFalse(enabled(w.start_button))
        w.step_var.set("20")
        w.start_button.invoke()
        self.assertTrue(self.ask.call_args[0][1].text.startswith("Start the test print on {machine}?"))
        self.assertEqual(self.ask.call_args[0][1].values["bags"], 30)
        self.assertTrue(w.running_now)
        self.assertTrue(shown(w.panel))
        self.assertFalse(enabled(w.machine_box))
        self.assertIn(f"test:{w.run_plan.run_id}", hx_ui_common._running)
        self.ask.return_value = False                # keep it running
        w.close()
        self.assertEqual(self.ask.call_args[0][1].text, "Stop the test print?")
        self.assertEqual(self.ask.call_args[1], {"yes": "Stop test", "no": "Keep it running"})
        self.assertTrue(alive(w))
        self.pump(lambda: not w.running_now, printing=fake)
        self.assertEqual(w.result_label.cget("text"), "Finished: 5 of 5 steps.")
        self.assertEqual(w.cleared_label.cget("text"), "Printing is off and slots 9 and 10 are empty. Line 1 Bagger 1 "
                                                       "needs a normal Send before production.")
        self.assertEqual((fake.slots[9].items, fake.slots[10].items, fake.printing), ([], [], False))
        self.assertEqual(len(w.steps_tree.get_children()), 5)
        self.assertEqual(w.steps_tree.item(w.steps_tree.get_children()[0], "values")[1:3], ("D440", "440 ms"))
        self.assertEqual(w.panel.status_label.cget("text"), "Printing is off and slots 9 and 10 are empty.")
        self.assertNotIn(f"test:{w.run_plan.run_id}", hx_ui_common._running)
        w.film_button.invoke()
        self.assertEqual(services.nav.calls[-1], ("open_admin", "products", film_id))
        self.assertTrue(enabled(w.machine_box))

    def test_test_print_not_stopped(self):
        services = self.make("akim", admin=True, sender=False)
        self.services = services
        mid, film_id = self.mid(), self.world.ids["film"]
        calls = []

        class Run:
            def __init__(self, plan):
                self.plan = plan

            def run(self, on_event, cancel=None):
                on_event(TestFinished(result=TestResult(
                    run_id=self.plan.run_id, kind="test", steps_done=1, of=5, ok=False, cleared=False,
                    printing_off=False, msg=Msg("Stripes couldn't stop printing on {machine}. Stop it at the printer "
                                                "with its Print On/Off button now.", {"machine": "Line 1 Bagger 1"}))))

            def end_again(self, on_event):
                calls.append(threading.current_thread().name)
                on_event(TestFinished(result=TestResult(run_id=self.plan.run_id, kind="test", steps_done=1, of=5,
                                                        ok=False, cleared=True, printing_off=True)))

        services.test_run = lambda plan: Run(plan)
        w = TestPrintWindow(self.root, services, film_id=film_id, machine_id=mid)
        w.empty_film_check.invoke()
        w.start_button.invoke()
        self.pump(lambda: w.red_page is not None)
        self.settle()
        self.assertTrue(w.attributes("-fullscreen"))
        self.assertTrue(w.attributes("-topmost"))
        texts = label_texts(w.red_page)
        self.assertIn("Stop it at the printer now", texts)
        self.assertIn("Stripes couldn't stop printing on Line 1 Bagger 1.", texts)
        self.assertIsNotNone(w.end_try_button)
        w.end_try_button.invoke()
        self.pump(lambda: w.red_page is None)
        self.assertEqual(calls, [f"hx-test-{w.run_plan.run_id}"])
        self.settle()
        self.assertFalse(w.attributes("-fullscreen"))
        self.assertEqual(w.cleared_label.cget("text"), "Printing is off and slots 9 and 10 are empty. Line 1 Bagger 1 "
                                                       "needs a normal Send before production.")
        self.assertEqual(hx_ui_common.running(), [])

    def test_test_print_try_again_fails(self):
        """ui.md §12.5 item 5 and §2.6 rule 2: when Try again's thread raises, the red page stays (printing
        isn't known to be off), says why and offers Try again again."""
        services = self.services
        mid, film_id = self.mid(), self.world.ids["film"]
        calls = []

        class Run:
            def __init__(self, plan):
                self.plan = plan

            def run(self, on_event, cancel=None):
                on_event(TestFinished(result=TestResult(run_id=self.plan.run_id, kind="test", steps_done=1, of=5,
                                                        ok=False, cleared=False, printing_off=False,
                                                        msg=Msg("Printing could not be turned off."))))

            def end_again(self, on_event):
                calls.append(1)
                if len(calls) == 1:
                    raise NotAllowed(Msg("Only Stripes Administrators can do this."))
                on_event(TestFinished(result=TestResult(run_id=self.plan.run_id, kind="test", steps_done=1, of=5,
                                                        ok=False, cleared=True, printing_off=True)))

        services.test_run = lambda plan: Run(plan)
        w = TestPrintWindow(self.root, services, film_id=film_id, machine_id=mid)
        w.empty_film_check.invoke()
        w.start_button.invoke()
        self.pump(lambda: w.red_page is not None)
        w.end_try_button.invoke()
        self.assertFalse(enabled(w.end_try_button))
        self.pump(lambda: not w.running_now)
        self.settle()
        self.assertIsNotNone(w.red_page)
        self.assertIn("Only Stripes Administrators can do this.", label_texts(w.red_page))
        self.assertTrue(enabled(w.end_try_button))
        self.assertEqual(hx_ui_common.running(), [])
        # Try again again: this time printing is off, and the window returns to the finished page
        w.end_try_button.invoke()
        self.pump(lambda: w.red_page is None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(w.cleared_label.cget("text"), "Printing is off and slots 9 and 10 are empty. Line 1 Bagger 1 "
                                                       "needs a normal Send before production.")

    def test_calibration_refuses_delay_machine(self):
        services, mid = self.services, self.mid()
        w = CalibrationWindow(self.root, services, machine_id=mid)
        self.assertEqual(self.tell.call_args[0][1].text, "Delay machines aren't calibrated.")
        self.assertFalse(alive(w))
        self.assertIsNone(services.nav.window(f"calibration:{mid}"))


class CalibrationTests(UiCase):
    printers = [{"placement": "encoder"}]

    def setUp(self):
        super().setUp()
        # The machine before calibration: no head distance and no Calibration record
        store = self.admin_store()
        rel = hx_share.record_rel_for(Machine, self.mid())
        machine = from_json(Machine, store.read_json(rel))
        hx_share.write_record(store, dataclasses.replace(machine, head_distance_mm=None, revision=machine.revision + 1))
        self.bump_changes(store)
        self.reload()
        self.assertIsNone(self.services.data.current.machines[self.mid()].head_distance_mm)
        self.assertEqual(self.services.data.current.calibrations, {})

    def print_marks(self, w, button):
        fake = self.world.printers[0]
        button.invoke()
        self.assertTrue(w.running_now)
        self.pump(lambda: not w.running_now, printing=fake)

    def test_calibration_window(self):
        services, mid, fake = self.services, self.mid(), self.world.printers[0]
        w = CalibrationWindow(self.root, services, machine_id=mid)
        self.assertEqual(w.title(), "Calibrate Line 1 Bagger 1")
        self.assertEqual(w.film_box.get(), "EXCO1 · Rev C (610 mm)")
        w.empty_film_check.invoke()
        self.assertFalse(enabled(w.print_spacing_button) or enabled(w.print_head_button))
        self.assertEqual(w.spacing_problem.cget("text"), "Save the rough distance first.")
        w.rough_var.set("400")
        self.assertFalse(enabled(w.print_head_button))         # typed, not saved
        w.save_rough_button.invoke()
        self.assertFalse(enabled(w.print_head_button))         # saved, but no DataUpdate shows it yet
        self.reload()
        self.assertEqual(w.rough_status.cget("text"), "Saved: 400 mm.")
        self.assertTrue(enabled(w.print_spacing_button) and enabled(w.print_head_button))
        self.assertEqual(w.spacing_text.cget("text"),
                         "Stripes prints two marks 300 mm apart, at 365 mm and 55 mm after the trigger.")
        self.assertEqual(w.head_text.cget("text"), "Stripes prints one mark 515 mm after the trigger.")
        self.assertEqual([s.position_mm for s in w.spacing_plan.steps], [365.0, 55.0])
        self.assertFalse(enabled(w.head_entry) or enabled(w.mark_entries[0]) or enabled(w.mark_entries[1]))
        # The head mark
        seen = []
        real = hx_message.head_distance_from_print

        def spy(sent, measured, length, rough):
            seen.append((sent, measured, length, rough))
            return real(sent, measured, length, rough)
        self.patch(hx_message, "head_distance_from_print", new=spy)
        self.print_marks(w, w.print_head_button)
        self.assertEqual(fake.printed[-1]["position_mm"], 515)
        self.assertEqual(w.run_label.cget("text"), "Finished: 1 of 1 steps.")
        self.assertTrue(enabled(w.head_entry))
        w.head_measured_var.set("317")
        self.assertEqual(w.head_result.cget("text"), "Head distance: 412 mm")
        self.assertEqual(seen[-1], (515, 317.0, 610.0, 400.0))
        # The two marks
        printed = len(fake.printed)
        self.print_marks(w, w.print_spacing_button)
        self.assertEqual(w.run_label.cget("text"), "Finished: 2 of 2 steps.")
        self.assertEqual({p["position_mm"] for p in fake.printed[printed:]}, {365, 55})
        self.assertTrue(enabled(w.mark_entries[0]) and enabled(w.mark_entries[1]))
        w.mark_a_var.set("167")
        w.mark_b_var.set("467.5")
        self.assertEqual(w.spacing_result.cget("text"),
                         "Gap: 300.5 mm. The gap is within 2 mm: the wheel size is right.")
        self.assertTrue(enabled(w.save_button))
        w.save_button.invoke()
        self.assertEqual(w.save_status.cget("text"), "Saved. The head distance of Line 1 Bagger 1 is now 412 mm.")
        store = self.admin_store()
        cal = from_json(Calibration, store.read_json(hx_share.record_rel_for(Calibration, mid)))
        self.assertEqual((cal.head_print_sent_mm, cal.head_print_measured_mm, cal.head_distance_mm, cal.head_by),
                         (515, 317.0, 412.0, "EXAMPLE\\jsmith"))
        self.assertEqual((cal.spacing_measured_mm, cal.rough_head_distance_mm), (300.5, 400.0))
        machine = from_json(Machine, store.read_json(hx_share.record_rel_for(Machine, mid)))
        self.assertEqual(machine.head_distance_mm, 412.0)
        self.reload()
        self.assertIn("Last head distance: 412 mm by EXAMPLE\\jsmith at", w.history_label.cget("text"))
        # A new rough distance: the marks must be printed again
        w.rough_var.set("450")
        self.assertFalse(enabled(w.print_head_button))
        w.save_rough_button.invoke()
        self.reload()
        self.assertEqual((w.head_measured_var.get(), w.head_result.cget("text")), ("", ""))
        self.assertIsNone(w.head_run)
        self.assertFalse(enabled(w.head_entry))
        self.assertEqual(w.head_text.cget("text"), "Stripes prints one mark 465 mm after the trigger.")
        self.print_marks(w, w.print_head_button)
        self.assertEqual(fake.printed[-1]["position_mm"], 465)
        w.head_measured_var.set("317")
        self.assertEqual(seen[-1][0], 465)
        self.assertEqual(w.head_result.cget("text"), "Head distance: 462 mm")
        # A gap that's too long: the wheel size to set, in whole mm
        self.print_marks(w, w.print_spacing_button)
        w.mark_a_var.set("167")
        w.mark_b_var.set("473")
        self.assertEqual(w.spacing_result.cget("text"), "Gap: 306 mm. Correct the wheel size on the printer to 57 mm "
                                                        "and set Expected wheel size on this machine's page to 57 mm, "
                                                        "then print the marks again.")
        # The printer's wheel changed but the machine record didn't: the next print fails its check
        fake.encoder["ENWHD"] = 57
        self.print_marks(w, w.print_head_button)
        self.assertTrue(w.run_label.cget("text").startswith("Stopped after 0 of 1 steps: Line 1 Bagger 1's encoder "
                                                            "settings changed: wheel 57 mm"))


if __name__ == "__main__":
    unittest.main()
