"""Tests for hx_ui_common (the shared MapleJet Tk layer). Run: xvfb-run -a python -m unittest -v

The Tk tests need a display; they skip themselves when Tk can't start.
"""

import io
import json
import os
import random
import shutil
import struct
import tempfile
import threading
import time
import tkinter as tk
import unittest
import wave
from dataclasses import dataclass
from tkinter import ttk
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import hx_share
import hx_state
import hx_ui_common as common
import i18n
from hx_core import EventBus, Msg
from hx_model import AMBER, BLUE, GREEN, GREY, RED, Company, HxData, Identity, NoReply
from i18n import N_

HERE = os.path.dirname(os.path.abspath(__file__))

SENDERS = "S-1-5-21-1000-2000-3000-1101"
ADMINS = "S-1-5-21-1000-2000-3000-1102"

# deploy.md §5.7: an all-white 1-bit 1024 x 160 BMP with the header the printer sends
TEST_BMP = (b"BM" + struct.pack("<IHHI", 20542, 0, 0, 62)
            + struct.pack("<IiiHHIIiiII", 40, 1024, 160, 1, 1, 0, 20480, 0, 0, 2, 0)
            + bytes([0, 0, 0, 0, 255, 255, 255, 0]) + b"\xff" * 20480)


def png_bytes(size=(40, 20), colour=(200, 30, 30)):
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def identity(*sids, user="EXAMPLE\\jsmith"):
    return Identity(user=user, computer="FLOOR01", sids=frozenset(sids), pid=1234)


def services_for(ident=None, company=None, *, loaded=True):
    """Just enough HxServices for these tests."""
    current = HxData(company=company or Company(id="company")) if loaded else None
    return SimpleNamespace(bus=EventBus(), identity=ident or identity(), data=SimpleNamespace(current=current),
                           nav=SimpleNamespace(windows={}))


def in_worker(func):
    """Run func on a worker thread; returns what it raised (or None)."""
    raised = []

    def run():
        try:
            func()
        except BaseException as e:
            raised.append(e)

    t = threading.Thread(target=run, name="test-worker")
    t.start()
    t.join(5)
    return raised[0] if raised else None


@dataclass
class Ping:
    n: int = 0


# --------------------------------------------------------------------------
# No Tk needed
# --------------------------------------------------------------------------

class TextTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_colours(self):
        self.assertEqual(common.COLOUR_HEX, {GREEN: "#15803d", AMBER: "#b45309", RED: "#b91c1c",
                                             GREY: "#6b7280", BLUE: "#2563eb"})
        self.assertEqual((common.RED_BG, common.RED_FG), (common.COLOUR_HEX[RED], "#ffffff"))

    def test_text(self):
        self.assertEqual(common.text(None), "")
        self.assertEqual(common.text(Msg(N_("Lot"))), "Lot")
        problem = Msg(N_("Stripes hit a problem: {error}"), {"error": Msg(N_("Lot"))})
        self.assertEqual(common.text(problem), "Stripes hit a problem: Lot")
        i18n.set_language("es")
        self.assertEqual(common.text(problem), "Stripes tuvo un problema: Lote")    # nested Msg translated
        # A badly made Msg never stops the UI: the text is shown unfilled
        self.assertEqual(common.text(Msg(N_("Stripes hit a problem: {error}"), {"other": 1})),
                         "Stripes tuvo un problema: {error}")

    def test_when(self):
        self.assertEqual(common.when(None), "")
        epoch = 1_793_275_200.0
        self.assertEqual(common.when(epoch), time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)))
        self.assertRegex(common.when(epoch), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_join_names(self):
        self.assertEqual(common.join_names([]), "")
        self.assertEqual(common.join_names(["Line 1"]), "Line 1")
        self.assertEqual(common.join_names(["Line 1", "Line 2"]), "Line 1 and Line 2")
        self.assertEqual(common.join_names(["A", "B", "C"]), "A, B and C")
        i18n.set_language("es")
        self.assertEqual(common.join_names(["A", "B", "C"]), "A, B y C")

    def test_catalogue_registered(self):
        for english, spanish in common.ES.items():
            self.assertEqual(i18n.ES[english], spanish)
        i18n.set_language("es")
        self.assertEqual(i18n.tr("Only Stripes Administrators can do this."),
                         "Solo los miembros de Stripes Administrators pueden hacer esto.")


class AlertWavTests(unittest.TestCase):
    def test_alert_wav(self):
        # ui.md §9.4: the committed file is alert_wav_bytes(), within 1 per sample
        path = os.path.join(HERE, common.ALERT_WAV)
        with wave.open(path, "rb") as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()),
                             (22050, 1, 2, 66150))
            committed = w.readframes(w.getnframes())
        with wave.open(io.BytesIO(common.alert_wav_bytes()), "rb") as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()),
                             (22050, 1, 2, 66150))
            made = w.readframes(w.getnframes())
        a = struct.unpack(f"<{len(committed) // 2}h", committed)
        b = struct.unpack(f"<{len(made) // 2}h", made)
        self.assertEqual(len(a), len(b))
        self.assertLessEqual(max(abs(x - y) for x, y in zip(a, b)), 1)
        # Only the RIFF, fmt and data chunks: 44 header bytes
        self.assertEqual(os.path.getsize(path), 44 + 66150 * 2)

    def test_alert_wav_shape(self):
        with wave.open(io.BytesIO(common.alert_wav_bytes()), "rb") as w:
            raw = w.readframes(w.getnframes())
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)

        def part(t0, t1):
            return samples[round(t0 * 22050):round(t1 * 22050)]

        self.assertEqual(samples[0], 0)
        self.assertGreater(max(part(0.0, 0.35)), 10000)          # tone A
        self.assertGreater(max(part(0.40, 0.80)), 10000)         # tone B
        self.assertEqual(set(part(0.351, 0.399)), {0})           # the gap
        self.assertEqual(set(part(0.80, 3.0)), {0})              # silence until the loop starts again
        self.assertLessEqual(max(abs(s) for s in samples), round(0.45 * 32767))
        # The fade in: 1 ms in, the envelope is at most 0.1
        self.assertLessEqual(max(abs(s) for s in part(0.0, 0.001)), round(0.1 * 0.45 * 32767))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stripes-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        paths = hx_share.HxPaths(share_root=None, roaming=os.path.join(self.tmp, "roaming"),
                                 local=os.path.join(self.tmp, "local"), machine=os.path.join(self.tmp, "machine"))
        self.local = hx_share.LocalStore(paths)
        self.file = os.path.join(paths.roaming, "maplejet.json")

    def test_user_settings(self):
        # defaults, with no file at all
        s = common.HxSettings.load(self.local)
        self.assertEqual(s, common.HxSettings())
        self.assertEqual((s.floor_board_at_startup, s.floor_board_screen, s.floor_board_size), (False, 0, "full"))
        self.assertIsNone(s.size_tuple())
        # save then load round-trips
        s = common.HxSettings(floor_board_at_startup=True, floor_board_screen=2, floor_board_size="1280x720")
        s.save(self.local)
        back = common.HxSettings.load(self.local)
        self.assertEqual(back, s)
        self.assertEqual(back.size_tuple(), (1280, 720))
        self.assertEqual(self.local.read_user_json("maplejet.json"),
                         {"floor_board_at_startup": True, "floor_board_screen": 2, "floor_board_size": "1280x720"})

    def test_bad_values_fall_back(self):
        for at_startup, screen, size in ((1, -1, "huge"), ("yes", True, "10x10"), (None, 1.0, "639x360"),
                                         ([], "1", "800x359"), (0, None, "1280x720 "), (False, [], 1280)):
            with self.subTest(at_startup=at_startup, screen=screen, size=size):
                self.local.write_user_json("maplejet.json", {"floor_board_at_startup": at_startup,
                                                             "floor_board_screen": screen,
                                                             "floor_board_size": size})
                s = common.HxSettings.load(self.local)
                self.assertEqual(s.floor_board_screen, 0)
                self.assertEqual(s.floor_board_size, "full")
                self.assertIs(s.floor_board_at_startup, at_startup if type(at_startup) is bool else False)
        # The smallest window is allowed
        self.local.write_user_json("maplejet.json", {"floor_board_size": "640x360", "floor_board_screen": 0})
        self.assertEqual(common.HxSettings.load(self.local).size_tuple(), (640, 360))

    def test_damaged_file(self):
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(common.HxSettings.load(self.local), common.HxSettings())
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        self.assertEqual(common.HxSettings.load(self.local), common.HxSettings())

    def test_size_tuple_of_a_bad_size(self):
        self.assertIsNone(common.HxSettings(floor_board_size="10x10").size_tuple())
        self.assertIsNone(common.HxSettings(floor_board_size="big").size_tuple())


class ThreadTests(unittest.TestCase):
    def tearDown(self):
        common.STRICT_THREADS = False
        for key in ("t1", "t2"):
            common.mark_done(key)

    def test_check_tk_thread(self):
        self.assertTrue(common.on_tk_thread())
        self.assertFalse(in_worker(lambda: self.assertTrue(not common.on_tk_thread())))
        common.STRICT_THREADS = False
        self.assertIsNone(in_worker(common.check_tk_thread))
        common.STRICT_THREADS = True
        common.check_tk_thread()        # fine on the Tk (main) thread
        self.assertIsInstance(in_worker(common.check_tk_thread), RuntimeError)

    def run_target(self, target, ref="sd_1"):
        services = services_for()
        got = []
        services.bus.subscribe(common.WorkFailed, got.append)
        thread = common.start_thread(services, f"hx-send-{ref}", ref, target)
        self.assertEqual(thread.name, f"hx-send-{ref}")
        self.assertTrue(thread.daemon)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        services.bus.pump()
        return got

    def test_start_thread_hx_error(self):
        msg = Msg(N_("Stripes hit a problem: {error}"), {"error": "x"})

        def fail():
            raise NoReply(msg)

        self.assertEqual(self.run_target(fail), [common.WorkFailed(ref="sd_1", msg=msg)])

    def test_start_thread_other_error(self):
        def fail():
            raise ValueError("boom")

        with mock.patch.object(common.traceback, "print_exc") as printed:
            got = self.run_target(fail, ref="ac_2")
        printed.assert_called_once()
        self.assertEqual(got, [common.WorkFailed(ref="ac_2", msg=Msg("Stripes hit a problem: {error}",
                                                                      {"error": "ValueError: boom"}))])

    def test_start_thread_success_posts_nothing(self):
        ran = []
        self.assertEqual(self.run_target(lambda: ran.append(threading.current_thread().name)), [])
        self.assertEqual(ran, ["hx-send-sd_1"])

    def test_running(self):
        first, second = Msg(N_("Lot")), Msg(N_("Film"))
        common.mark_running("t1", first)
        common.mark_running("t2", second)
        self.assertEqual(common.running()[-2:], [first, second])
        common.mark_done("t1")
        common.mark_done("t1")          # twice is fine
        self.assertNotIn(first, common.running())
        self.assertIn(second, common.running())
        common.mark_done("t2")
        self.assertNotIn(second, common.running())

    def test_sync_background(self):
        got = []
        common.SyncBackground().run(lambda: 42, lambda result, error: got.append((result, error)))
        error = ValueError("no")

        def fail():
            raise error

        common.SyncBackground().run(fail, lambda result, error: got.append((result, error)))
        self.assertEqual(got, [(42, None), (None, error)])


class RolesTests(unittest.TestCase):
    def test_roles(self):
        company = Company(id="company", sender_group_sid=SENDERS, admin_group_sid=ADMINS)
        self.assertEqual(common.roles(services_for(identity(SENDERS), company, loaded=False)), common.Roles())
        cases = (((), common.Roles()),
                 ((SENDERS,), common.Roles(sender=True, acknowledge=True)),
                 ((ADMINS.lower(),), common.Roles(admin=True, acknowledge=True)),
                 ((SENDERS, ADMINS), common.Roles(sender=True, admin=True, acknowledge=True)))
        for sids, expected in cases:
            with self.subTest(sids=sids):
                services = services_for(identity(*sids), company)
                self.assertEqual(common.roles(services), expected)
                self.assertEqual(common.roles(services).acknowledge,
                                 hx_state.can_acknowledge(services.identity, company))
        # Empty group SIDs in the company settings mean nobody has the role
        self.assertEqual(common.roles(services_for(identity(SENDERS), Company(id="company"))), common.Roles())


class PictureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stripes-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_bmp_to_image(self):
        img = common.bmp_to_image(TEST_BMP)
        self.assertEqual(img.size, (1024, 160))
        self.assertEqual(img.convert("L").getextrema(), (255, 255))    # all white
        with self.assertRaises(Exception):
            common.bmp_to_image(b"not a picture")

    def test_png_preview(self):
        path = os.path.join(self.tmp, "front.jpg")
        Image.new("RGB", (1200, 800), (10, 120, 200)).save(path, "JPEG")
        png = common.png_preview(path)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(png)) as img:
            self.assertEqual(img.size, (480, 320))
            self.assertEqual(img.mode, "RGB")

    def test_png_preview_turns_by_exif(self):
        path = os.path.join(self.tmp, "phone.jpg")
        exif = Image.Exif()
        exif[0x0112] = 6                # taken turned: show it rotated
        Image.new("RGB", (600, 300), (0, 0, 0)).save(path, "JPEG", exif=exif)
        with Image.open(io.BytesIO(common.png_preview(path))) as img:
            self.assertEqual(img.size, (240, 480))

    def test_png_preview_not_a_picture(self):
        path = os.path.join(self.tmp, "spec.pdf")
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4 not a picture")
        self.assertIsNone(common.png_preview(path))
        self.assertIsNone(common.png_preview(os.path.join(self.tmp, "missing.png")))

    def test_png_preview_shrinks_until_it_fits(self):
        path = os.path.join(self.tmp, "noise.png")
        rng = random.Random(7)
        Image.frombytes("RGB", (600, 600), rng.randbytes(600 * 600 * 3)).save(path, "PNG")
        sizes = {}
        for side in (480, 360, 240):
            out = io.BytesIO()
            with Image.open(path) as img:
                img.convert("RGB").resize((side, side)).save(out, "PNG", optimize=True)
            sizes[side] = len(out.getvalue())
        # Room for the 360 px preview but not the 480 px one
        with mock.patch.object(hx_share, "FILM_PREVIEW_MAX_BYTES", (sizes[480] + sizes[360]) // 2):
            with Image.open(io.BytesIO(common.png_preview(path))) as img:
                self.assertEqual(img.size, (360, 360))
        with mock.patch.object(hx_share, "FILM_PREVIEW_MAX_BYTES", 100):
            self.assertIsNone(common.png_preview(path))


# --------------------------------------------------------------------------
# Tk
# --------------------------------------------------------------------------

class TkCase(unittest.TestCase):
    root = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest(f"Tk can't start: {e}")
        cls.root.withdraw()
        common.setup_styles(cls.root)

    @classmethod
    def tearDownClass(cls):
        for window in common.open_windows():
            window.destroy()
        cls.root.destroy()

    def setUp(self):
        common.STRICT_THREADS = True
        self.services = services_for()

    def tearDown(self):
        common.STRICT_THREADS = False
        i18n.set_language("en")
        for window in common.open_windows():
            window.destroy()
        for child in self.root.winfo_children():
            child.destroy()

    def pump(self, until, timeout_s=10):
        deadline = time.monotonic() + timeout_s
        while not until():
            if time.monotonic() > deadline:
                self.fail("timed out")
            self.root.update()
            self.services.bus.pump()
            time.sleep(0.01)

    def settle(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            self.services.bus.pump()
            time.sleep(0.01)


class StyleAndTextsTests(TkCase):
    def test_setup_styles(self):
        style = ttk.Style(self.root)
        style.configure("Warn.TLabel", foreground="#123456")      # as stripes.App would have it
        common.setup_styles(self.root)
        common.setup_styles(self.root)
        self.assertEqual(str(style.lookup("Warn.TLabel", "foreground")), "#123456")    # not redefined
        self.assertIn("bold", str(style.lookup("Hx.Big.TButton", "font")))
        self.assertEqual(str(style.lookup("Hx.Card.TFrame", "background")), common.CARD_BG)
        self.assertEqual(str(style.lookup("Hx.Small.TLabel", "foreground")), common.MUTED_FG)
        for name in ("Ok.TLabel", "Bad.TLabel", "Muted.TLabel"):
            self.assertTrue(style.lookup(name, "foreground"), name)

    def test_fonts(self):
        self.assertIn(common.bag_font_family(self.root), common.BAG_FONTS + ("TkDefaultFont",))
        self.assertIn(common.mono_font_family(self.root), common.MONO_FONTS + ("TkFixedFont",))

    def test_texts(self):
        texts = common.Texts()
        frame = ttk.Frame(self.root)
        label = ttk.Label(frame)
        button = ttk.Button(frame)
        entry = ttk.Entry(frame)
        notebook = ttk.Notebook(frame)
        page = ttk.Frame(notebook)
        notebook.add(page)
        tree = ttk.Treeview(frame, columns=("line",))
        window = tk.Toplevel(self.root)
        texts.set(label, N_("Lot"))
        texts.set(button, N_("You can refresh once every {seconds} s."), seconds=10)
        texts.msg(entry, Msg(N_("Stripes hit a problem: {error}"), {"error": Msg(N_("Film"))}), option="text")
        texts.title(window, N_("Floor board"))
        texts.tab(notebook, page, N_("Machines"))
        texts.heading(tree, "line", N_("Line"))
        self.assertEqual(label.cget("text"), "Lot")
        self.assertEqual(button.cget("text"), "You can refresh once every 10 s.")
        self.assertEqual(window.title(), "Floor board")

        i18n.set_language("es")
        texts.apply()
        self.assertEqual(label.cget("text"), "Lote")
        self.assertEqual(button.cget("text"), "Puede actualizar una vez cada 10 s.")
        self.assertEqual(str(entry.cget("text")), "Stripes tuvo un problema: Película")
        self.assertEqual(window.title(), "Tablero de planta")
        self.assertEqual(notebook.tab(page, "text"), "Máquinas")
        self.assertEqual(tree.heading("line", "text"), "Línea")

        # Setting a widget again replaces its entry; forget() stops the updates
        texts.set(label, N_("Film"))
        texts.forget(button)
        i18n.set_language("en")
        texts.apply()
        self.assertEqual(label.cget("text"), "Film")
        self.assertEqual(button.cget("text"), "Puede actualizar una vez cada 10 s.")
        # A widget that is gone is dropped quietly
        label.destroy()
        texts.apply()
        self.assertEqual(window.title(), "Floor board")

    def test_texts_off_the_tk_thread(self):
        label = ttk.Label(self.root)
        texts = common.Texts()
        self.assertIsInstance(in_worker(lambda: texts.set(label, N_("Lot"))), RuntimeError)
        self.assertEqual(label.cget("text"), "")

    def test_choice_box(self):
        box = common.ChoiceBox(self.root, [("encoder", N_("Encoder")), ("delay", N_("Delay"))])
        self.assertEqual(str(box.cget("state")), "readonly")
        self.assertEqual(box.key(), "")
        box.set_key("delay")
        self.assertEqual((box.key(), box.get()), ("delay", "Delay"))
        i18n.set_language("es")
        box.retranslate()
        self.assertEqual((box.key(), box.get()), ("delay", "Retardo"))
        self.assertEqual(list(box.cget("values")), ["Encoder", "Retardo"])
        box.set_choices([("plastic", N_("Plastic")), ("delay", N_("Delay"))])
        self.assertEqual((box.key(), box.get()), ("delay", "Retardo"))     # still chosen
        box.set_choices([("plastic", N_("Plastic"))])
        self.assertEqual((box.key(), box.get()), ("", ""))
        box.set_key("plastic")
        box.set_key("nothing")
        self.assertEqual(box.key(), "")

    def test_choice_box_with_values(self):
        # A choice whose text has values (the screen list of ui.md §14) is given as a Msg and
        # shown again in the new language; any translated text with values will do here
        choices = [("0", Msg(N_("You can refresh once every {seconds} s."), {"seconds": 5})),
                   ("1", N_("Delay"))]
        box = common.ChoiceBox(self.root, choices)
        box.set_key("0")
        self.assertEqual((box.key(), box.get()), ("0", "You can refresh once every 5 s."))
        i18n.set_language("es")
        box.retranslate()
        self.assertEqual((box.key(), box.get()), ("0", "Puede actualizar una vez cada 5 s."))
        self.assertEqual(list(box.cget("values")), ["Puede actualizar una vez cada 5 s.", "Retardo"])
        box.set_key("1")
        self.assertEqual(box.key(), "1")

    def test_record_box(self):
        box = common.RecordBox(self.root)
        self.assertEqual(box.record_id(), "")
        # A record named like an English text is data: it stays as it is in Spanish
        box.set_records([("pt_1", "Lot"), ("m_1", "Bagger"), ("m_2", "Bagger")], none_text=N_("Machines"))
        self.assertEqual(box.record_id(), "")
        box.set_record_id("")
        self.assertEqual((box.current(), box.get()), (0, "Machines"))
        i18n.set_language("es")
        box.retranslate()
        self.assertEqual(list(box.cget("values")), ["Máquinas", "Lot", "Bagger", "Bagger"])
        self.assertEqual((box.record_id(), box.get()), ("", "Máquinas"))
        box.set_record_id("m_2")        # two records with one name
        self.assertEqual((box.record_id(), box.get()), ("m_2", "Bagger"))
        box.retranslate()
        self.assertEqual(box.record_id(), "m_2")
        box.set_records([("m_2", "Bagger 2"), ("m_3", "Bagger 3")])
        self.assertEqual((box.record_id(), box.get()), ("m_2", "Bagger 2"))   # kept
        box.set_records([("m_3", "Bagger 3")])
        self.assertEqual((box.record_id(), box.get()), ("", ""))
        box.set_record_id("missing")
        self.assertEqual(box.record_id(), "")


class BagTextTests(TkCase):
    def test_bag_text(self):
        bag = common.BagText(self.root, size=20, bold=True)
        self.assertTrue(bag.hx_no_translate)
        self.assertEqual(bag.get(), "")
        self.assertEqual(int(bag.cget("highlightthickness")), 0)
        self.assertEqual(bag.coords(bag.text_item), [2.0, 2.0])
        self.assertEqual(bag.itemcget(bag.text_item, "anchor"), "nw")
        self.assertIn("bold", bag.itemcget(bag.text_item, "font"))
        empty_height = int(bag.cget("height"))
        origin = "  Product of USA and Canada "
        bag.set(origin)
        self.assertEqual(bag.get(), origin)                 # kept exactly, spaces too
        self.assertEqual(bag.itemcget(bag.text_item, "text"), origin)
        x1, y1, x2, y2 = bag.bbox(bag.text_item)
        self.assertEqual(int(bag.cget("height")), y2 - y1 + 4)
        self.assertEqual(int(bag.cget("width")), x2 - x1 + 4)
        self.assertEqual(int(bag.cget("height")), empty_height)     # an empty text keeps one line's height
        bag.set("")
        self.assertEqual(int(bag.cget("height")), empty_height)
        self.assertEqual(bag.get(), "")

    def test_wrapping(self):
        bag = common.BagText(self.root, size=12, wrap_px=120, foreground="#ffffff", background=common.RED_BG)
        self.assertEqual(int(bag.cget("width")), 124)
        self.assertEqual(bag.itemcget(bag.text_item, "fill"), "#ffffff")
        self.assertEqual(bag.cget("background"), common.RED_BG)
        bag.set("Product of USA")
        one_line = int(bag.cget("height"))
        bag.set("Product of USA, Canada, and Mexico Lot 88888888")
        self.assertGreater(int(bag.cget("height")), one_line)
        self.assertEqual(int(bag.cget("width")), 124)       # the width is fixed when wrapping
        bag.set_colours(foreground=common.TEXT_FG, background=common.CARD_BG)
        self.assertEqual(bag.itemcget(bag.text_item, "fill"), common.TEXT_FG)
        self.assertEqual(bag.cget("background"), common.CARD_BG)

    def test_strict_threads(self):
        bag = common.BagText(self.root)
        bag.set("Product of USA")
        self.assertIsInstance(in_worker(lambda: bag.set("Product of Canada")), RuntimeError)
        self.assertEqual(bag.get(), "Product of USA")
        view = common.PictureView(self.root)
        self.assertIsInstance(in_worker(lambda: view.set_bmp(TEST_BMP)), RuntimeError)
        self.assertIsNone(view.bmp)

    def test_bag_text_survives_the_language_switch(self):
        window = common.HxWindow(self.root, self.services, title=N_("Floor board"))
        bag = common.BagText(window)
        bag.pack()
        label = ttk.Label(window)
        label.pack()
        window.texts.set(label, N_("(origin unknown)"))
        bag.set("Product of USA")
        i18n.set_language("es")
        window.retranslate()
        self.assertEqual(label.cget("text"), "(origen desconocido)")
        self.assertEqual(bag.get(), "Product of USA")
        self.assertEqual(bag.itemcget(bag.text_item, "text"), "Product of USA")
        self.assertEqual(window.title(), "Tablero de planta")


class PictureViewTests(TkCase):
    def items(self, view, kind):
        return [i for i in view.find_all() if view.type(i) == kind]

    def test_empty_and_broken(self):
        view = common.PictureView(self.root)
        self.assertTrue(view.hx_no_translate)
        self.assertEqual((int(view.cget("width")), int(view.cget("height"))), (512, 82))
        for bmp in (None, b""):
            view.set_bmp(bmp)
            self.assertEqual(view.bmp, bmp)
            boxes = self.items(view, "rectangle")
            self.assertEqual(len(boxes), 1)
            self.assertEqual(view.itemcget(boxes[0], "fill"), "#e5e7eb")
            self.assertEqual(view.itemcget(boxes[0], "outline"), common.RULE_FG)
            self.assertEqual(self.items(view, "image"), [])
            self.assertEqual(self.items(view, "text"), [])
        view.set_bmp(b"BM not really a picture")         # never raises
        self.assertEqual(view.bmp, b"BM not really a picture")
        boxes = self.items(view, "rectangle")
        self.assertEqual(view.itemcget(boxes[0], "outline"), common.COLOUR_HEX[RED])
        self.assertEqual(self.items(view, "image"), [])

    def test_picture(self):
        view = common.PictureView(self.root, width=256)
        self.assertEqual(int(view.cget("height")), 42)
        view.set_bmp(TEST_BMP)
        self.assertIs(view.bmp, TEST_BMP)
        self.assertIsNotNone(view._photo)
        self.assertEqual(len(self.items(view, "image")), 1)
        border = self.items(view, "rectangle")
        self.assertEqual(view.itemcget(border[0], "outline"), "#9ca3af")
        self.assertEqual((view._photo.width(), view._photo.height()), (254, 40))
        # Enlarged too, on a wide canvas
        wide = common.PictureView(self.root, width=1400)
        wide.set_bmp(TEST_BMP)
        self.assertEqual(wide._photo.width(), 1398)

    def test_redraws_on_configure(self):
        window = tk.Toplevel(self.root)
        window.geometry("700x300+0+0")
        view = common.PictureView(window, width=300)
        view.pack(fill="both", expand=True)
        view.set_bmp(TEST_BMP)
        self.pump(lambda: view.winfo_width() == 700)
        self.pump(lambda: view._photo.width() == 698)
        self.assertEqual(view._photo.height(), round(698 * 160 / 1024))

    def test_photo_from_png(self):
        photo = common.photo_from_png(self.root, png_bytes((40, 20)))
        self.assertEqual((photo.width(), photo.height()), (40, 20))
        small = common.photo_from_png(self.root, png_bytes((200, 100)), height=96)
        self.assertEqual((small.width(), small.height()), (192, 96))
        with self.assertRaises(Exception):
            common.photo_from_png(self.root, b"not a png")


class WindowTests(TkCase):
    def test_flags_title_and_order(self):
        first = common.HxWindow(self.root, self.services, title=N_("Floor board"))
        second = common.HxWindow(self.root)
        self.assertTrue(first.hx_window and first.hx_no_translate)
        self.assertEqual(first.title(), "Floor board")
        self.assertIs(first.services, self.services)
        self.assertIsInstance(first.texts, common.Texts)
        self.assertEqual(common.open_windows(), [first, second])
        first.destroy()
        first.destroy()             # twice is fine
        self.assertEqual(common.open_windows(), [second])

    def test_subscribe_and_later(self):
        window = common.HxWindow(self.root, self.services)
        got, fired = [], []
        window.subscribe(Ping, got.append)
        window.later(10, lambda: fired.append("soon"))
        window.later(60_000, lambda: fired.append("late"))
        self.services.bus.post(Ping(1))
        self.pump(lambda: got and fired)
        self.assertEqual((got, fired), ([Ping(1)], ["soon"]))
        self.assertEqual(len(window._hx_timers), 1)          # the fired timer is forgotten
        window.destroy()
        self.assertEqual(window._hx_timers, set())
        self.services.bus.post(Ping(2))
        self.settle(0.05)
        self.assertEqual(got, [Ping(1)])                     # unsubscribed
        self.assertEqual(self.root.tk.splitlist(self.root.tk.call("after", "info")), ())   # long timer cancelled

    def test_subscribe_callbacks_run_checked(self):
        window = common.HxWindow(self.root, self.services)
        window.subscribe(Ping, lambda e: None)
        # The bus pump belongs to the Tk thread; a pump from a worker would run the callback there
        self.services.bus.post(Ping())
        with mock.patch.object(common.traceback, "print_exc"), \
                mock.patch("hx_core.traceback.print_exc") as printed:
            self.assertIsNone(in_worker(self.services.bus.pump))
        printed.assert_called_once()        # the callback raised RuntimeError, which the pump reports

    def test_key_registration(self):
        class Monitor(common.HxWindow):
            key = "monitor"

        class Result(common.HxWindow):
            def __init__(self, master, services, send_id):
                self.key = f"result:{send_id}"
                super().__init__(master, services)

        windows = self.services.nav.windows
        monitor = Monitor(self.root, self.services)
        result = Result(self.root, self.services, "sd_1")
        self.assertEqual(windows, {"monitor": monitor, "result:sd_1": result})
        monitor.destroy()
        self.assertEqual(windows, {"result:sd_1": result})
        # Destroyed through its master (the App quitting) is the same
        holder = tk.Toplevel(self.root)
        inside = Monitor(holder, self.services)
        self.assertIs(windows["monitor"], inside)
        holder.destroy()
        self.assertEqual(windows, {"result:sd_1": result})
        self.assertNotIn(inside, common.open_windows())
        # Destroyed by Tk alone (no Python destroy() call): still cleaned up
        got = []
        lone = Monitor(self.root, self.services)
        lone.subscribe(Ping, got.append)
        lone.later(60_000, lambda: got.append("late"))
        lone.tk.call("destroy", lone._w)
        self.assertEqual(windows, {"result:sd_1": result})
        self.assertEqual(lone._hx_timers, set())
        self.services.bus.post(Ping())
        self.services.bus.pump()
        self.assertEqual(got, [])
        # No key, no services: nothing to register
        common.HxWindow(self.root)
        self.assertEqual(len(windows), 1)

    def test_retranslate(self):
        refreshed = []

        class Window(common.HxWindow):
            def refresh_texts(self):
                refreshed.append(i18n.language())

        window = Window(self.root, self.services, title=N_("Floor board"))
        frame = ttk.Frame(window)
        frame.pack()
        box = common.ChoiceBox(frame, [("white", N_("White")), ("black", N_("Black"))])
        box.set_key("black")
        records = common.RecordBox(frame)
        records.set_records([("l1", "Line 1")], none_text=N_("Machines"))
        records.set_record_id("")
        other = common.HxWindow(window, self.services)       # a window of its own does its own
        other_box = common.ChoiceBox(other, [("white", N_("White"))])
        other_box.set_key("white")
        i18n.set_language("es")
        window.retranslate()
        self.assertEqual(refreshed, ["es"])
        self.assertEqual((box.get(), box.key()), ("Negra", "black"))
        self.assertEqual(records.get(), "Máquinas")
        self.assertEqual(window.title(), "Tablero de planta")
        self.assertEqual(other_box.get(), "White")

    def test_close_box_calls_close(self):
        closed = []

        class Busy(common.HxWindow):
            def close(self):
                closed.append(True)

        busy = Busy(self.root, self.services)
        busy.tk.call(busy.protocol("WM_DELETE_WINDOW"))
        self.assertEqual(closed, [True])
        self.assertTrue(busy.winfo_exists())
        plain = common.HxWindow(self.root, self.services)
        plain.tk.call(plain.protocol("WM_DELETE_WINDOW"))
        self.assertFalse(plain.winfo_exists())

    def test_show(self):
        # Without a window manager (xvfb) Tk never reports -topmost back, so the calls are watched
        window = common.HxWindow(self.root, self.services)
        window.withdraw()
        with mock.patch.object(window, "_raise_topmost", wraps=window._raise_topmost) as raised:
            window.show()
            self.assertEqual(window.state(), "normal")
            self.assertEqual(raised.call_args_list, [mock.call(True)])
            window.show()           # again within the 200 ms: still one timer
            self.pump(lambda: raised.call_count == 2, timeout_s=2)
            self.assertEqual(raised.call_args_list, [mock.call(True), mock.call(False)])
        self.assertFalse(window._hx_topmost)
        # A window that is topmost for good (the red window) stays topmost
        red = common.HxWindow(self.root, self.services)
        red.attributes("-fullscreen", False, "-topmost", True)
        self.assertTrue(red._hx_topmost)
        red.attributes("-topmost")              # a read changes nothing
        self.assertTrue(red._hx_topmost)
        with mock.patch.object(red, "_raise_topmost") as raised:
            red.show()
            self.settle(0.3)
        raised.assert_not_called()
        # Made topmost for good while show()'s 200 ms run: kept
        late = common.HxWindow(self.root, self.services)
        with mock.patch.object(late, "_raise_topmost") as raised:
            late.show()
            late.attributes("-topmost", 1)
            self.settle(0.3)
        self.assertEqual(raised.call_args_list, [mock.call(True)])
        late.attributes("-topmost", "0")
        self.assertFalse(late._hx_topmost)

    def test_geometry_is_centred(self):
        window = common.HxWindow(self.root, self.services)
        window.geometry("300x200")      # the main window is hidden: centred on the screen
        window.update_idletasks()
        x = (window.winfo_screenwidth() - 300) // 2
        y = (window.winfo_screenheight() - 200) // 2
        self.assertEqual(window.wm_geometry(), f"300x200+{x}+{y}")
        placed = common.HxWindow(self.root, self.services)
        placed.geometry("320x240+5+6")  # a position given is kept
        placed.update_idletasks()
        self.assertEqual(placed.geometry(), "320x240+5+6")
        # Centred on a visible main window
        main = tk.Toplevel(self.root)
        main.geometry("600x400+100+50")
        self.pump(lambda: main.winfo_viewable() and main.winfo_width() == 600)
        child = common.HxWindow(main, self.services)
        child.geometry("200x100")
        child.update_idletasks()
        self.assertEqual(child.geometry(),
                         f"200x100+{main.winfo_rootx() + 200}+{main.winfo_rooty() + 150}")

    def test_photo_window(self):
        window = common.PhotoWindow(self.root, self.services, png=png_bytes((300, 200)), caption="EXCO1 Rev C")
        self.assertEqual(window.title(), "EXCO1 Rev C")
        self.assertEqual(window.caption_label.cget("text"), "EXCO1 Rev C")
        self.assertEqual((window.photo.width(), window.photo.height()), (300, 200))
        self.pump(lambda: window.winfo_viewable())
        DialogTests.press_escape(window)
        self.assertFalse(window.winfo_exists())
        broken = common.PhotoWindow(self.root, self.services, png=b"junk", caption="EXCO1")
        self.assertIsNone(broken.photo)
        self.assertEqual(broken.picture_label.cget("text"), "No photo")
        i18n.set_language("es")
        broken.retranslate()
        self.assertEqual(broken.picture_label.cget("text"), "Sin foto")
        self.assertEqual(broken.close_button.cget("text"), "Cerrar")
        self.assertEqual(broken.caption_label.cget("text"), "EXCO1")    # data


class DialogTests(TkCase):
    def dialog(self):
        found = [w for w in self.root.winfo_children() if isinstance(w, common._AskDialog)]
        return found[0] if found else None

    def answer_with(self, action):
        started = time.monotonic()
        self.texts_seen = None

        def go():
            dialog = self.dialog()
            if dialog is None or not dialog.winfo_viewable():
                if time.monotonic() - started > 5:
                    if dialog is not None:
                        dialog.destroy()     # never hang the suite: fail instead
                    return
                self.root.after(10, go)
                return
            self.seen = dialog
            self.texts_seen = (dialog.question_label.cget("text"), dialog.yes_button.cget("text"),
                               dialog.no_button.cget("text"), str(dialog.yes_button.cget("style")))
            action(dialog)
            self.root.after(2000, lambda: dialog.winfo_exists() and dialog.destroy())

        self.root.after(10, go)

    @staticmethod
    def press_escape(widget):
        # Tk sends key presses to the focus window, as a real key press would go
        widget.focus_force()
        widget.update()
        widget.focus_get().event_generate("<Escape>")

    def test_ask(self):
        question = Msg(N_("Stripes couldn't stop printing on {machines}."), {"machines": "Line 1 Bagger 1"})
        self.answer_with(lambda d: d.yes_button.invoke())
        self.assertTrue(common.ask(self.root, question, yes=N_("Stop printing"), no=N_("Go back")))
        self.assertTrue(self.seen.hx_window)
        self.assertEqual(self.texts_seen, ("Stripes couldn't stop printing on Line 1 Bagger 1.", "Stop printing",
                                           "Go back", "Hx.Main.TButton"))
        self.assertFalse(self.seen.winfo_exists())

        i18n.set_language("es")
        self.answer_with(lambda d: d.no_button.invoke())
        self.assertFalse(common.ask(self.root, question, yes=N_("Stop printing"), no=N_("Go back")))
        self.assertEqual(self.texts_seen, ("Stripes no pudo detener la impresión en Line 1 Bagger 1.",
                                           "Detener la impresión", "Volver", "Hx.Main.TButton"))

    def test_escape_and_close_box_mean_no(self):
        question = Msg(N_("Lot"))
        started = time.monotonic()
        self.answer_with(self.press_escape)
        self.assertFalse(common.ask(self.root, question, yes=N_("Save"), no=N_("Go back")))
        self.assertLess(time.monotonic() - started, 1.5)     # closed by Escape, not by the safety timer
        started = time.monotonic()
        self.answer_with(lambda d: d.tk.call(d.protocol("WM_DELETE_WINDOW")))
        self.assertFalse(common.ask(self.root, question, yes=N_("Save"), no=N_("Go back")))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(self.texts_seen)

    def test_tell(self):
        msg = Msg(N_("Only Stripes Administrators can do this."))
        for kind, name in (("info", "showinfo"), ("warning", "showwarning"), ("error", "showerror"),
                           ("other", "showinfo")):
            with self.subTest(kind=kind), mock.patch.object(common.messagebox, name) as shown:
                common.tell(self.root, msg, kind=kind)
                shown.assert_called_once_with("Stripes", "Only Stripes Administrators can do this.",
                                              parent=self.root)
        i18n.set_language("es")
        with mock.patch.object(common.messagebox, "showinfo") as shown:
            common.tell(self.root, msg)
        shown.assert_called_once_with("Stripes", "Solo los miembros de Stripes Administrators pueden hacer esto.",
                                      parent=self.root)


class ScrollerTests(TkCase):
    def test_scroller(self):
        window = tk.Toplevel(self.root)
        window.geometry("300x200+0+0")
        scroller = common.Scroller(window)
        scroller.pack(fill="both", expand=True)
        self.assertEqual(scroller.inner.cget("background"), common.PAGE_BG)
        labels = [ttk.Label(scroller.inner, text=f"Line {n}") for n in range(60)]
        for label in labels:
            label.pack(anchor="w")
        tree = ttk.Treeview(scroller.inner)
        tree.pack()
        self.pump(lambda: scroller.can_scroll())
        self.assertIs(common._wheel_target(labels[3]), scroller)
        self.assertIsNone(common._wheel_target(tree))        # it scrolls itself
        self.assertEqual(scroller.inner.winfo_width(), scroller.canvas.winfo_width())   # inner fills the width
        # The wheel over a label scrolls the Scroller under the pointer
        self.pump(lambda: labels[2].winfo_viewable())
        x, y = labels[2].winfo_rootx() + 2, labels[2].winfo_rooty() + 2
        common._on_wheel(SimpleNamespace(widget=labels[2], x_root=x, y_root=y, num=5, delta=0))
        self.assertGreater(scroller.canvas.yview()[0], 0.0)
        common._on_wheel(SimpleNamespace(widget=labels[2], x_root=x, y_root=y, num="??", delta=120))
        self.assertEqual(scroller.canvas.yview()[0], 0.0)

    def test_nested_scrollers(self):
        window = tk.Toplevel(self.root)
        window.geometry("300x200+0+0")
        outer = common.Scroller(window)
        outer.pack(fill="both", expand=True)
        inner = common.Scroller(outer.inner, background=common.CARD_BG)
        inner.pack(fill="x")
        short = ttk.Label(inner.inner, text="Line 1")
        short.pack()
        for n in range(60):
            ttk.Label(outer.inner, text=f"Line {n}").pack()
        self.pump(lambda: outer.can_scroll())
        self.assertFalse(inner.can_scroll())
        self.assertIs(common._wheel_target(short), outer)    # the inner one has nothing to scroll

    def wheel_down(self, widget):
        # A real wheel event through the bindings: <MouseWheel> as on Windows, then the
        # <Button-5> of X11 (each platform's ttk binds the one it gets)
        x, y = widget.winfo_rootx() + 3, widget.winfo_rooty() + 3
        widget.event_generate("<MouseWheel>", delta=-120, x=3, y=3, rootx=x, rooty=y, when="now")
        widget.event_generate("<Button-5>", x=3, y=3, rootx=x, rooty=y, when="now")
        self.root.update()

    def test_wheel_over_value_widgets_scrolls_the_page(self):
        # The wheel over a combobox or spinbox inside a Scroller moves the page and never
        # changes the value (a print text, a film side or a gap), and fires no selection
        window = tk.Toplevel(self.root)
        window.geometry("300x200+0+0")
        scroller = common.Scroller(window)
        scroller.pack(fill="both", expand=True)
        record_box = common.RecordBox(scroller.inner)
        record_box.set_records([("pt_1", "Packed on + origin"), ("pt_2", "Origin only")])
        record_box.set_record_id("pt_1")
        choice_box = common.ChoiceBox(scroller.inner, [("encoder", N_("Encoder")), ("delay", N_("Delay"))])
        choice_box.set_key("encoder")
        spinbox = ttk.Spinbox(scroller.inner, from_=0, to=50)
        spinbox.set("4")
        tk_spinbox = tk.Spinbox(scroller.inner, from_=0, to=50)
        tk_spinbox.delete(0, "end")
        tk_spinbox.insert(0, "4")
        widgets = [record_box, choice_box, spinbox, tk_spinbox]
        for widget in widgets:
            widget.pack(anchor="w")
        for n in range(80):
            ttk.Label(scroller.inner, text=f"Line {n}").pack(anchor="w")
        selected = []
        record_box.bind("<<ComboboxSelected>>", lambda e: selected.append(record_box.record_id()))
        choice_box.bind("<<ComboboxSelected>>", lambda e: selected.append(choice_box.key()))
        self.pump(lambda: scroller.can_scroll() and all(w.winfo_viewable() for w in widgets))
        for widget in widgets:
            self.assertIs(common._wheel_target(widget), scroller)
            before = scroller.canvas.canvasy(0)
            self.wheel_down(widget)
            self.assertGreater(scroller.canvas.canvasy(0), before, widget.winfo_class())
            scroller.canvas.yview_moveto(0.0)
            self.root.update()
        self.assertEqual((record_box.record_id(), choice_box.key()), ("pt_1", "encoder"))
        self.assertEqual((spinbox.get(), tk_spinbox.get()), ("4", "4"))
        self.assertEqual(selected, [])

    def test_wheel_outside_a_scroller_is_unchanged(self):
        # The comboboxes of the Zebra tab keep Tk's own wheel binding: it still steps the value
        window = tk.Toplevel(self.root)
        window.geometry("300x200+0+0")
        common.Scroller(window)                 # the wheel bindings are in place
        box = ttk.Combobox(window, values=["A", "B", "C"], state="readonly")
        box.current(0)
        box.pack()
        self.pump(lambda: box.winfo_viewable())
        self.wheel_down(box)
        self.assertGreater(box.current(), 0)

    def test_scrollbar_wheel_scrolls_once(self):
        # A scrollbar's own wheel binding already scrolls what it controls
        window = tk.Toplevel(self.root)
        window.geometry("300x200+0+0")
        scroller = common.Scroller(window)
        scroller.pack(fill="both", expand=True)
        labels = [ttk.Label(scroller.inner, text=f"Line {n}") for n in range(60)]
        for label in labels:
            label.pack(anchor="w")
        self.pump(lambda: scroller.can_scroll())
        self.assertIs(common._wheel_target(labels[0]), scroller)
        self.assertIsNone(common._wheel_target(scroller.scrollbar))


if __name__ == "__main__":
    unittest.main()
