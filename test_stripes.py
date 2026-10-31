"""Tests for stripes: the Zebra side, and how the window hosts the MapleJet tab (the v2 folders,
the two tabs, the shortcuts, the language switch, the tray and quitting). Run: python -m unittest -v

The window tests need a display (on Linux: xvfb-run -a python -m unittest -v); they skip
themselves when Tk can't start.
"""

import gc
import json
import os
import re
import shutil
import tempfile
import socket
import sys
import threading
import time
import tkinter as tk
import types
import unittest
from dataclasses import asdict, replace
from tkinter import ttk
from unittest import mock

import hx_ui
import hx_ui_common
import hx_ui_monitor
import hx_ui_send
import hx_win
import i18n
import label_design as ld
import stripes as zl
from hx_core import FakeClock, Msg
from hx_fake import FakeWorld
from hx_model import (AMBER, GREEN, STEP_CONFIRM_2, TIMINGS, BoardUpdate, BoardView, Identity, LineChoice, LineView,
                      MachineResult, MachineView, SendRequest, SendResult, new_id)
from hx_protocol import GateRegistry, release_key

ZD420 = zl.Printer(name="Packing", host="127.0.0.1", model="ZD420", width_in=2, height_in=1, dpi=203)
ZT410 = zl.Printer(name="Pallets", host="127.0.0.1", model="ZT410", width_in=3, height_in=3, dpi=300)

HS_READY = b"\x02030,0,0,0203,000,0,0,0,000,0,0,0\x03\r\n\x02000,0,0,0,1,2,6,0,00000000,1,000\x03\r\n\x021234,0\x03\r\n"
HS_PROBLEMS = b"\x02030,1,1,0203,000,0,0,0,000,0,0,0\x03\r\n\x02000,0,1,0,1,2,6,0,00000000,1,000\x03\r\n\x021234,0\x03\r\n"


def fields_of(zpl):
    return re.findall(r"\^FO(\d+),(\d+)\^A0N,(\d+),(\d+)\^FB(\d+),1,0,([LCR]),0\^FH\^FD(.*?)\^FS", zpl)


class PrinterLabelTests(unittest.TestCase):
    def test_label_sizes_in_dots(self):
        self.assertEqual(zl.label_dots(ZD420), (406, 203))
        self.assertEqual(zl.label_dots(ZT410), (914, 914))
        self.assertEqual(zl.printer_label(ZT410).dpmm, 12)

    def test_typed_text_stays_inside_label(self):
        for printer in (ZD420, ZT410):
            label = zl.printer_label(printer)
            for text in ("A", "Tomatoes", "Roma Tomatoes\nLot 2026-117\nPacked 10/21/2026",
                         "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWW", "1\n2\n3\n4\n5\n6\n7\n8"):
                r = ld.render(ld.Design.from_text(text), label)
                self.assertEqual(r.warnings + r.errors, [], (printer.name, text))
                for mk in r.marks:
                    self.assertGreaterEqual(mk.y, label.margin)
                    self.assertLessEqual(mk.y + mk.h, label.height - label.margin + 1)
                    self.assertLessEqual(ld.text_em(mk.text) * ld.WIDTH_SAFETY * mk.h, mk.w + 0.5)

    def test_special_characters_escaped(self):
        zpl = ld.build_zpl(ld.render(ld.Design.from_text("A^B~C_D 50°F"), zl.printer_label(ZD420)))
        self.assertIn("^FDA_5EB_7EC_5FD 50°F^FS", zpl)
        self.assertEqual(zpl.count("^XA"), 1)
        self.assertTrue(zpl.startswith("^XA\n^CI28\n"))  # UTF-8 only when the text needs it
        plain = ld.build_zpl(ld.render(ld.Design.from_text("Plain"), zl.printer_label(ZD420)))
        self.assertNotIn("^CI", plain)


class FakePrinter:
    """Listens like a Zebra on port 9100: records data, answers ~HS."""

    def __init__(self, hs_reply=HS_READY, config_report=b"\x02\x03", sgd=None):
        self.hs_reply = hs_reply
        self.config_report = config_report
        self.sgd = sgd or {}
        self.received = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if data == b"~HS":
                        conn.sendall(self.hs_reply)
                        break
                    if data == b"^XA^HH^XZ":
                        conn.sendall(self.config_report)
                        break
                    if data.startswith(b"! U1 getvar") and data.endswith(b"\r\n"):
                        names = re.findall(rb'getvar "([^"]+)"', data)
                        conn.sendall(b"".join(b'"' + self.sgd.get(n.decode(), "?").encode() + b'"' for n in names))
                        break
                self.received.append(data)

    def close(self):
        self.sock.close()


class NetworkTests(unittest.TestCase):
    def test_print_over_network(self):
        fake = FakePrinter()
        self.addCleanup(fake.close)
        p = zl.Printer(name="n", host="127.0.0.1", port=fake.port)
        zpl = ld.build_zpl(ld.render(ld.Design.from_text("Hello"), zl.printer_label(p)), copies=2)
        zl.send_label(p, zpl.encode("utf-8"))
        for _ in range(50):
            if fake.received:
                break
            threading.Event().wait(0.02)
        self.assertEqual(fake.received[0].decode("utf-8"), zpl)

    def test_status_ready_and_problems(self):
        for reply, expected in ((HS_READY, []),
                                (HS_PROBLEMS, ["Out of labels", "Paused", "Print head open"])):
            fake = FakePrinter(reply)
            self.addCleanup(fake.close)
            p = zl.Printer(name="n", host="127.0.0.1", port=fake.port)
            self.assertEqual(zl.printer_problems(p), expected)

    def test_unreachable_message(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with self.assertRaises(OSError) as cm:
            zl.send_raw("127.0.0.1", port, b"x", timeout=10)  # Windows retries a refused connect for ~2s
        self.assertIn("refused", zl.describe_error(cm.exception))


class FakeWin32Print(types.SimpleNamespace):
    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4

    def __init__(self):
        super().__init__(calls=[], status=0)

    def EnumPrinters(self, flags, name, level):
        self.calls.append(("EnumPrinters", flags, level))
        return [
            {"pPrinterName": "Microsoft Print to PDF", "pDriverName": "Microsoft Print To PDF", "pPortName": "PORTPROMPT:"},
            {"pPrinterName": "ZDesigner ZT410-300dpi ZPL", "pDriverName": "ZDesigner ZT410-300dpi ZPL", "pPortName": "IP_192.0.2.41"},
            {"pPrinterName": "Packing Line", "pDriverName": "ZDesigner ZD420-203dpi ZPL", "pPortName": "Port_A"},
        ]

    def OpenPrinter(self, name):
        self.calls.append(("OpenPrinter", name))
        return "handle"

    def StartDocPrinter(self, h, level, info):
        self.calls.append(("StartDocPrinter", info))

    def StartPagePrinter(self, h):
        self.calls.append(("StartPagePrinter",))

    def WritePrinter(self, h, data):
        self.calls.append(("WritePrinter", data))

    def EndPagePrinter(self, h):
        self.calls.append(("EndPagePrinter",))

    def EndDocPrinter(self, h):
        self.calls.append(("EndDocPrinter",))

    def ClosePrinter(self, h):
        self.calls.append(("ClosePrinter",))

    def GetPrinter(self, h, level):
        return {"Status": self.status}


class WindowsPrinterTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeWin32Print()
        patcher = mock.patch.object(zl, "win32print", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        ports = mock.patch.object(zl, "_port_addresses", return_value={"Port_A": "192.0.2.20"})
        ports.start()
        self.addCleanup(ports.stop)

    def test_list_guesses_models_and_ips(self):
        found = zl.list_windows_printers()
        by_name = {p.name: (p, z) for p, z in found}
        self.assertEqual([z for _, z in found], [True, True, False])  # Zebras first

        zd, zebra = by_name["Packing Line"]
        self.assertTrue(zebra)
        self.assertEqual((zd.model, zd.dpi, zd.width_in, zd.height_in, zd.host, zd.windows_name),
                         ("ZD420", 203, 2.0, 1.0, "192.0.2.20", "Packing Line"))
        zt, _ = by_name["ZDesigner ZT410-300dpi ZPL"]
        self.assertEqual((zt.model, zt.dpi, zt.width_in, zt.height_in, zt.host),
                         ("ZT410", 300, 3.0, 3.0, "192.0.2.41"))
        pdf, zebra = by_name["Microsoft Print to PDF"]
        self.assertFalse(zebra)
        self.assertEqual(pdf.model, "Other")

    def test_print_goes_through_spooler_as_raw(self):
        p = zl.Printer(name="Packing", host="192.0.2.20", windows_name="Packing Line")
        zl.send_label(p, b"^XA^XZ")
        self.assertEqual(self.fake.calls, [
            ("OpenPrinter", "Packing Line"),
            ("StartDocPrinter", ("Stripes label", None, "RAW")),
            ("StartPagePrinter",),
            ("WritePrinter", b"^XA^XZ"),
            ("EndPagePrinter",),
            ("EndDocPrinter",),
            ("ClosePrinter",),
        ])

    def test_status_from_spooler_when_no_ip(self):
        p = zl.Printer(name="Packing", windows_name="Packing Line")
        self.fake.status = 0x80 | 0x10
        self.assertEqual(zl.printer_problems(p), ["Offline", "Out of labels"])
        self.fake.status = 0
        self.assertEqual(zl.printer_problems(p), [])

    def test_missing_pywin32(self):
        with mock.patch.object(zl, "win32print", None):
            with self.assertRaises(RuntimeError):
                zl.list_windows_printers()


class SettingsTests(unittest.TestCase):
    def test_round_trip(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "settings.json")
            with mock.patch.dict(os.environ, {"STRIPES_SETTINGS": path}):
                s = {"printers": [ZD420, zl.Printer(name="W", windows_name="Packing Line")],
                     "last_printer": "W", "recent": [{"text": "a", "size_pt": None, "align": "center",
                                                      "emphasize": False}]}
                zl.save_settings(s)
                loaded = zl.load_settings()
        self.assertEqual(loaded["printers"], s["printers"])
        self.assertEqual(loaded["last_printer"], "W")
        self.assertEqual(loaded["recent"], s["recent"])


EXAMPLE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "config")


class CompanyConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = os.path.join(self.tmp, "cache")
        env = mock.patch.dict(os.environ, {"STRIPES_CACHE": self.cache})
        env.start()
        self.addCleanup(env.stop)

    def folder(self, printers=None, stock=None, encoding="utf-8"):
        d = tempfile.mkdtemp(dir=self.tmp)
        for name, content in (("printers.json", printers), ("stock.json", stock)):
            if content is not None:
                with open(os.path.join(d, name), "w", encoding=encoding) as f:
                    f.write(content)
        return d

    def load(self, folder):
        with mock.patch.dict(os.environ, {"STRIPES_CONFIG": folder}):
            return zl.load_company_config()

    def test_example_config(self):
        cfg = self.load(EXAMPLE_CONFIG)
        self.assertEqual(cfg.problems, [])
        self.assertEqual([s.id for s in cfg.stock], ["2x1-gap", "3x3-gap", "3x3-mark"])
        by_name = {p.name: p for p in cfg.printers}
        packing, pallets = by_name["Packing ZD420"], by_name["Pallets ZT410"]
        # label size comes from the printer's stock
        self.assertEqual((packing.stock, zl.label_dots(packing)), ("2x1-gap", (406, 203)))
        self.assertEqual((pallets.stock, zl.label_dots(pallets)), ("3x3-mark", (610, 610)))
        self.assertTrue(all(p.port == 9100 and not p.windows_name for p in cfg.printers))

    def test_notepad_bom_and_bad_entries(self):
        d = self.folder(
            printers='{"printers": [{"name": "A", "host": "192.0.2.1", "stock": "s1"},'
                     ' {"name": "B", "host": "192.0.2.2", "dpi": 200}, {"name": "C"},'
                     ' {"name": "A", "host": "192.0.2.3"}, {"nme": "typo"}, "junk",'
                     ' {"name": "D", "host": "192.0.2.4", "stock": "nope"}]}',
            stock='{"stock": [{"id": "s1", "name": "One", "width_in": 4, "height_in": 6},'
                  ' {"id": "s2", "name": "Bad", "width_in": 0, "height_in": 1},'
                  ' {"id": "s3", "name": "Bad media", "width_in": 1, "height_in": 1, "media": "foil"}]}',
            encoding="utf-8-sig")
        cfg = self.load(d)
        self.assertEqual([p.name for p in cfg.printers], ["A", "D"])
        self.assertEqual(zl.label_dots(cfg.printers[0]), (813, 1219))
        self.assertEqual(cfg.printers[1].stock, "")  # unknown stock dropped, printer kept
        self.assertEqual([s.id for s in cfg.stock], ["s1"])
        self.assertEqual(len(cfg.problems), 8)

    def test_stock_file_optional(self):
        cfg = self.load(self.folder(printers='{"printers": [{"name": "A", "host": "192.0.2.1"}]}'))
        self.assertEqual((cfg.problems, cfg.stock, [p.name for p in cfg.printers]), ([], [], ["A"]))

    def test_server_down_uses_saved_copy(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        for name in ("printers.json", "stock.json"):
            shutil.copy(os.path.join(EXAMPLE_CONFIG, name), d)
        online = self.load(d)
        self.assertIsNone(online.cached_at)
        self.assertTrue(os.path.exists(os.path.join(self.cache, "stock.json")))

        os.remove(os.path.join(d, "printers.json"))  # server unreachable / file gone
        offline = self.load(d)
        self.assertIsNotNone(offline.cached_at)
        self.assertEqual([p.name for p in offline.printers], [p.name for p in online.printers])
        self.assertEqual(len(offline.stock), 3)
        self.assertIn("using the copy saved", offline.problems[0])

    def test_server_down_and_no_saved_copy(self):
        cfg = self.load(os.path.join(self.tmp, "missing"))
        self.assertEqual(cfg.printers, [])
        self.assertIn("Couldn't read the company settings", cfg.problems[0])

    def test_no_config_anywhere(self):
        with mock.patch.object(zl, "configured_folder", return_value=None), \
                mock.patch.object(zl, "local_folders", return_value=[self.tmp]):
            cfg = zl.load_company_config()
        self.assertEqual((cfg.printers, cfg.stock, cfg.problems), ([], [], []))

    def test_company_printers_win_name_clashes(self):
        company = [zl.Printer(name="Shop", host="192.0.2.214")]
        personal = [zl.Printer(name="Shop", host="1.2.3.4"), zl.Printer(name="Mine", host="5.6.7.8")]
        merged = zl.merge_printers(company, personal)
        self.assertEqual([(p.name, p.host) for p in merged], [("Shop", "192.0.2.214"), ("Mine", "5.6.7.8")])


ZD_REPORT = """\x02
   PRINTER CONFIGURATION

Zebra Technologies
ZTC ZD420-203dpi ZPL
  +10.0               DARKNESS
  6.0 IPS             PRINT SPEED
  +000                TEAR OFF ADJUST
  TEAR OFF            PRINT MODE
  GAP/NOTCH           MEDIA TYPE
  TRANSMISSIVE        SENSOR SELECT
  406                 PRINT WIDTH
  0217                LABEL LENGTH
  39.0IN   988MM      MAXIMUM LENGTH
FIRMWARE IN THIS PRINTER IS COPYRIGHTED
\x03"""


HS_BUSY = (b"\x02030,0,0,0217,002,0,0,0,000,0,0,0\x03\r\n"
           b"\x02000,0,0,0,1,1,6,1,00000024,1,003\x03\r\n\x021234,0\x03\r\n")


class MonitorTests(unittest.TestCase):
    def test_status_details(self):
        d = zl.parse_host_status_details(HS_BUSY)
        self.assertEqual((d["label_length_dots"], d["formats_waiting"], d["labels_remaining"]), (217, 2, 24))
        self.assertEqual((d["thermal_transfer"], d["print_mode"], d["label_waiting"]), (True, "Peel-off", True))
        self.assertEqual(zl.status_problems(d), [])
        self.assertEqual(zl.parse_host_status(HS_PROBLEMS), ["Out of labels", "Paused", "Print head open"])

    def test_config_lines(self):
        lines = dict(zl.parse_config_lines(ZD_REPORT))
        self.assertEqual(lines["DARKNESS"], "+10.0")
        self.assertEqual(lines["LABEL LENGTH"], "0217")
        self.assertEqual(lines["MAXIMUM LENGTH"], "39.0IN   988MM")
        self.assertNotIn("PRINTER CONFIGURATION", lines)

    def test_sgd(self):
        fake = FakePrinter(sgd={"device.unique_id": "D2J193208123", "odometer.total_label_count": "10563"})
        self.addCleanup(fake.close)
        info = zl.query_sgd("127.0.0.1", fake.port, ["device.unique_id", "odometer.total_label_count", "nope"])
        self.assertEqual(info, {"device.unique_id": "D2J193208123", "odometer.total_label_count": "10563",
                                "nope": None})

    def test_inspect_printer(self):
        fake = FakePrinter(hs_reply=HS_BUSY, config_report=ZD_REPORT.encode(),
                           sgd={"odometer.total_label_count": "19259"})
        self.addCleanup(fake.close)
        p = zl.Printer(name="Gary", host="127.0.0.1", port=fake.port, model="ZD420")
        snap = zl.inspect_printer(p)
        self.assertTrue(snap.reachable)
        self.assertEqual(snap.state, ("Printing", "busy"))
        self.assertEqual(snap.activity, "Printing: 24 labels left in this job")
        self.assertEqual(snap.info["odometer.total_label_count"], "19259")
        self.assertIn(("PRINT WIDTH", "406"), snap.report)

    def test_label_length(self):
        p = zl.Printer(name="Alaine", host="x", model="ZD420", dpi=203)
        stock = zl.Stock("2x1-gap", "2 x 1 in", 2, 1, "gap")
        self.assertIsNone(zl.check_label_length(p, stock, 217))  # 1.07 in: 1 in label plus its gap
        self.assertIsNone(zl.check_label_length(p, stock, 196))  # 0.97 in: calibration wobble
        self.assertEqual(zl.check_label_length(p, stock, 320), "Labels loaded are 1.57 in long, not 2 x 1 in")
        self.assertIsNotNone(zl.check_label_length(p, stock, 180))  # 0.89 in: too short
        self.assertIsNone(zl.check_label_length(p, None, 320))  # no stock: nothing to compare
        self.assertIsNone(zl.check_label_length(p, stock, None))  # printer didn't say

    def test_unreachable_and_non_link_os(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        snap = zl.inspect_printer(zl.Printer(name="Off", host="127.0.0.1", port=port))
        self.assertEqual(snap.state, ("Not reachable", "off"))
        self.assertIn("refused", snap.error)
        self.assertFalse(zl.is_link_os(zl.Printer(name="x", model="Other")))  # never sent SGD


class CheckPrinterTests(unittest.TestCase):
    def test_check_printer_over_network(self):
        fake = FakePrinter(hs_reply=HS_BUSY)
        self.addCleanup(fake.close)
        p = zl.Printer(name="n", host="127.0.0.1", port=fake.port)
        self.assertEqual(zl.check_printer(p), ([], None))  # no stock: status only
        self.assertEqual(zl.check_printer(p, zl.Stock("3x3", "3 x 3 in", 3, 3, "gap")),
                         ([], "Labels loaded are 1.07 in long, not 3 x 3 in"))


class SingleInstanceTests(unittest.TestCase):
    def test_second_start_reaches_the_first(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "instance.txt")
        self.assertFalse(zl.SingleInstance(path).signal_running())  # nothing running yet
        got, arrived = [], threading.Event()
        first = zl.SingleInstance(path)
        first.listen(lambda message: (got.append(message), arrived.set()))
        self.addCleanup(first.close)
        self.assertTrue(zl.SingleInstance(path).signal_running("show"))
        self.assertTrue(arrived.wait(2))
        self.assertEqual(got, ["show"])

    def test_wrong_key_or_stale_file_is_ignored(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "instance.txt")
        got = []
        first = zl.SingleInstance(path)
        first.listen(got.append)
        self.addCleanup(first.close)
        port = open(path).read().split()[0]
        with open(path, "w") as f:
            f.write(f"{port} not-the-key")
        self.assertFalse(zl.SingleInstance(path).signal_running())
        first.close()
        with open(path, "w") as f:
            f.write("1 stale")  # left over after a crash: nothing listens there
        self.assertFalse(zl.SingleInstance(path).signal_running())
        self.assertEqual(got, [])


class PreviewTextTests(unittest.TestCase):
    def test_drawn_like_font_0(self):
        font = zl.find_preview_font()
        if not font:
            self.skipTest("no preview font on this machine")
        img = zl.text_image("H", 200, font)
        left, top, right, bottom = img.getchannel("A").point(lambda a: 255 if a > 128 else 0).getbbox()
        self.assertLessEqual(top, 3)  # capitals start at the top of the field...
        self.assertAlmostEqual(bottom, 150, delta=4)  # ...and sit on the baseline at 75%
        self.assertEqual(img.width, round(ld.printed_em("H") * 200))  # the printer's condensed width
        img = zl.text_image("g", 200, font)
        bottom = img.getchannel("A").point(lambda a: 255 if a > 128 else 0).getbbox()[3]
        self.assertTrue(170 < bottom < img.height)  # the descender shows, not cut off


class ExactPreviewTests(unittest.TestCase):
    def test_labelary_error_is_reported(self):
        with mock.patch.object(zl.sys, "platform", "win32"), \
                mock.patch.object(zl, "wininet_post", return_value=(429, b"")) as post:
            with self.assertRaisesRegex(OSError, "429"):
                zl.labelary_png("^XA^XZ", ZT410)
        host, path, data, headers = post.call_args.args
        self.assertEqual((host, path, data), ("api.labelary.com", "/v1/printers/12dpmm/labels/3x3/0/", b"^XA^XZ"))
        self.assertIn("Accept: image/png\r\n", headers)

    @unittest.skipUnless(sys.platform == "win32", "WinINet is Windows only")
    def test_wininet_post(self):
        import http.server

        class Echo(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200 if self.path == "/echo" else 404)
                self.end_headers()
                self.wfile.write((self.headers.get("Accept") or "").encode() + b" " + body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Echo)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        status, body = zl.wininet_post("127.0.0.1", "/echo", b"x" * 100000, "Accept: image/png\r\n",
                                       port=port, secure=False)
        self.assertEqual((status, body), (200, b"image/png " + b"x" * 100000))  # read in several chunks
        self.assertEqual(zl.wininet_post("127.0.0.1", "/nope", b"", "", port=port, secure=False)[0], 404)
        server.shutdown()
        server.server_close()
        with self.assertRaises(OSError):  # nothing listening any more
            zl.wininet_post("127.0.0.1", "/echo", b"", "", port=port, secure=False, timeout_ms=2000)


class VersionTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(zl.parse_version("v1.10.2"), (1, 10, 2))
        self.assertIsNone(zl.parse_version("dev-abc1234"))
        self.assertIsNone(zl.parse_version("v1.4.0-beta.2"))  # betas are gone
        self.assertLess(zl.parse_version("v1.9.9"), zl.parse_version("v1.10.0"))  # not a text comparison

    def test_old_copies_cleaned_up(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        exe = os.path.join(d, "Stripes.exe")
        for name in ("Stripes.exe", "Stripes.exe.old-123", "Stripes.exe.new"):
            open(os.path.join(d, name), "w").close()
        zl.remove_old_versions(exe)
        self.assertEqual(os.listdir(d), ["Stripes.exe"])


class DeploymentLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def log(self, computer="FLOOR01", user="John"):
        return zl.DeploymentLog(self.dir, computer, user)

    def test_install_update_heartbeat_close(self):
        log = self.log()
        self.assertTrue(log.started("v1.2.0", r"C:\Users\John\AppData\Local\Programs\Stripes\Stripes.exe"))
        r = log.read()
        self.assertEqual((r["computer"], r["user"], r["version"], r["running"]), ("FLOOR01", "John", "v1.2.0", True))
        self.assertEqual([e["event"] for e in r["events"]], ["Installed (v1.2.0)"])
        first_seen = r["first_seen"]

        log.started("v1.2.0", "x")  # plain restart: no new event
        log.started("v1.3.0", "x")  # Group Policy copied a new exe
        r = log.read()
        self.assertEqual(r["first_seen"], first_seen)
        self.assertEqual(r["version"], "v1.3.0")
        self.assertEqual((r["last_update"]["from"], r["last_update"]["to"]), ("v1.2.0", "v1.3.0"))
        self.assertEqual([e["event"] for e in r["events"]][-1], "Updated v1.2.0 -> v1.3.0")

        self.assertTrue(zl.is_open_now(r))
        log.write(running=False, last_closed=zl._now())
        self.assertFalse(zl.is_open_now(log.read()))

    def test_self_update_fields_dropped(self):
        log = self.log()
        with open(log.path, "w", encoding="utf-8") as f:  # written by a build that updated itself
            f.write('{"version": "v1.4.0-beta.2", "beta": true, "pending_update": "v1.4.0",'
                    ' "last_update_error": {"message": "offline"}}')
        log.started("v1.5.0", "x")
        r = log.read()
        self.assertEqual(r["version"], "v1.5.0")
        self.assertFalse({"beta", "pending_update", "last_update_error"} & set(r))

    def test_stale_heartbeat_is_not_open(self):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(hours=2)).isoformat(sep=" ", timespec="seconds")
        self.assertFalse(zl.is_open_now({"running": True, "last_seen": old}))

    def test_events_are_capped(self):
        log = self.log()
        for i in range(zl.DeploymentLog.MAX_EVENTS + 10):
            log.write(f"event {i}")
        events = log.read()["events"]
        self.assertEqual(len(events), zl.DeploymentLog.MAX_EVENTS)
        self.assertEqual(events[-1]["event"], f"event {zl.DeploymentLog.MAX_EVENTS + 9}")

    def test_read_all_skips_junk(self):
        self.log("PC1", "Ann").started("v1.2.0", "x")
        self.log("PC2", "Bob/Smith").started("v1.3.0", "x")  # odd characters are made safe for file names
        with open(os.path.join(self.dir, "junk.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(self.dir, "other.txt"), "w") as f:
            f.write("ignored")
        records = zl.read_deployment(self.dir)
        self.assertEqual(sorted(r["computer"] for r in records), ["PC1", "PC2"])
        self.assertIn("PC2_Bob_Smith.json", os.listdir(self.dir))

    def test_first_start_carries_the_old_record_over(self):
        # The MapleJet version writes to v2\status\deployment, where nothing is on its first start:
        # it showed every upgraded PC as a fresh install (install date reset, no last update, no history)
        old = os.path.join(self.dir, "status")
        new = os.path.join(self.dir, "v2", "status", "deployment")
        os.makedirs(old)
        os.makedirs(new)
        before = {"computer": "FLOOR01", "user": "jsmith", "version": "v1.4.0", "first_seen": "2025-01-10 08:00:00",
                  "last_seen": "2026-10-29 16:00:00",
                  "last_update": {"from": "v1.3.0", "to": "v1.4.0", "at": "2026-03-01 08:00:00"},
                  "events": [{"at": "2025-01-10 08:00:00", "event": "Installed (v1.3.0)"},
                             {"at": "2026-03-01 08:00:00", "event": "Updated v1.3.0 -> v1.4.0"}],
                  "pending_update": "v1.4.1"}
        old_file = os.path.join(old, "FLOOR01_jsmith.json")
        with open(old_file, "w", encoding="utf-8") as f:
            json.dump(before, f)
        with open(old_file, "rb") as f:
            old_bytes = f.read()
        log = zl.DeploymentLog(new, "FLOOR01", "jsmith", old_folder=old)
        self.assertTrue(log.started("v2.0.0", "x"))
        r = log.read()
        self.assertEqual((r["version"], r["first_seen"]), ("v2.0.0", "2025-01-10 08:00:00"))
        self.assertEqual((r["last_update"]["from"], r["last_update"]["to"]), ("v1.4.0", "v2.0.0"))
        self.assertEqual([e["event"] for e in r["events"]],
                         ["Installed (v1.3.0)", "Updated v1.3.0 -> v1.4.0", "Updated v1.4.0 -> v2.0.0"])
        self.assertNotIn("pending_update", r)
        shown = zl.read_all_deployment([new, old])
        self.assertEqual([(s["version"], s["first_seen"]) for s in shown], [("v2.0.0", "2025-01-10 08:00:00")])
        # The old folder is only read
        self.assertEqual(os.listdir(old), ["FLOOR01_jsmith.json"])
        with open(old_file, "rb") as f:
            self.assertEqual(f.read(), old_bytes)
        # Later starts use this version's record only: the old one can change and it isn't read again
        with open(old_file, "w", encoding="utf-8") as f:
            json.dump(dict(before, version="v1.4.2", first_seen="2026-10-30 08:00:00"), f)
        log.started("v2.0.0", "x")
        log.started("v2.0.1", "x")
        r = log.read()
        self.assertEqual((r["first_seen"], r["last_update"]["from"]), ("2025-01-10 08:00:00", "v2.0.0"))
        self.assertEqual([e["event"] for e in r["events"]][-1], "Updated v2.0.0 -> v2.0.1")
        # No old record, a damaged one, or the same folder twice: a fresh install as before
        for name, content in (("PC2", None), ("PC3", "{not json"), ("PC4", "[1, 2]")):
            if content is not None:
                with open(os.path.join(old, f"{name}_jsmith.json"), "w", encoding="utf-8") as f:
                    f.write(content)
            fresh = zl.DeploymentLog(new, name, "jsmith", old_folder=old)
            fresh.started("v2.0.0", "x")
            self.assertEqual([e["event"] for e in fresh.read()["events"]], ["Installed (v2.0.0)"], name)
            self.assertNotIn("last_update", fresh.read())
        self.assertIsNone(zl.DeploymentLog(new, "PC5", "jsmith", old_folder=new + os.sep).old_path)

    OLD_RECORD = {"computer": "FLOOR01", "user": "jsmith", "version": "v1.4.0", "first_seen": "2025-01-10 08:00:00",
                  "last_seen": "2026-10-29 16:00:00",
                  "last_update": {"from": "v1.3.0", "to": "v1.4.0", "at": "2026-03-01 08:00:00"},
                  "events": [{"at": "2025-01-10 08:00:00", "event": "Installed (v1.3.0)"}]}

    def upgrade_folders(self):
        """(new folder, old folder) with this PC and user's record in the old one."""
        old = os.path.join(self.dir, "Share", "Stripes", "status")
        new = os.path.join(self.dir, "Share", "Stripes", "v2", "status", "deployment")
        os.makedirs(old)
        os.makedirs(new)
        with open(os.path.join(old, "FLOOR01_jsmith.json"), "w", encoding="utf-8") as f:
            json.dump(self.OLD_RECORD, f)
        return new, old

    def assert_carried(self, r, version="v2.0.0"):
        self.assertEqual((r["version"], r["first_seen"]), (version, "2025-01-10 08:00:00"))
        self.assertEqual((r["last_update"]["from"], r["last_update"]["to"]), ("v1.4.0", version))
        self.assertEqual([e["event"] for e in r["events"]], ["Installed (v1.3.0)", f"Updated v1.4.0 -> {version}"])

    def test_first_start_carries_over_after_the_share_was_down(self):
        # Stripes starts at sign-in before the share is reachable: the start writes nothing, the
        # heartbeat and the config write then make a record with no version. The next start still
        # carries the old record over (it used to log a fresh install, dated today, no history)
        new, old = self.upgrade_folders()
        share = os.path.dirname(old)
        os.rename(share, share + ".away")
        log = zl.DeploymentLog(new, "FLOOR01", "jsmith", old_folder=old)
        self.assertFalse(log.started("v2.0.0", "x"))
        os.rename(share + ".away", share)
        self.assertTrue(log.write(running=True))                  # App._heartbeat
        self.assertTrue(log.write(config="server", printers=3))   # App._config_loaded
        self.assertNotIn("version", log.read())
        self.assertTrue(zl.DeploymentLog(new, "FLOOR01", "jsmith", old_folder=old).started("v2.0.0", "x"))
        r = log.read()
        self.assert_carried(r)
        self.assertEqual((r["config"], r["printers"]), ("server", 3))

    def test_config_write_during_the_first_start(self):
        # App._config_loaded writes on its own worker thread. If it lands while started() is
        # deciding, the start must still carry the old record over (the decision used to be made
        # outside the lock, and the write then dropped the install date and history)
        new, old = self.upgrade_folders()
        log = zl.DeploymentLog(new, "FLOOR01", "jsmith", old_folder=old)
        real_open, other = open, []

        def racing_open(path, *args, **kwargs):
            if path == log.old_path and not other:
                other.append(threading.Thread(target=lambda: log.write(config="server", printers=3)))
                other[0].start()
                other[0].join(0.5)      # it gets its turn now, unless started() holds the lock
            return real_open(path, *args, **kwargs)

        with mock.patch.object(zl, "open", racing_open, create=True):
            self.assertTrue(log.started("v2.0.0", "x"))
        other[0].join(5)
        r = log.read()
        self.assert_carried(r)
        self.assertEqual((r["config"], r["printers"]), ("server", 3))

    def test_unreadable_record_is_not_a_first_start(self):
        # A share hiccup on the one read of this version's record: nothing is written (it used to
        # take the old v1.4.0 record as the previous version and log a false "Updated v1.4.0 ->
        # v2.0.1" over the real last update). A damaged record starts again without the old one.
        new, old = self.upgrade_folders()
        log = zl.DeploymentLog(new, "FLOOR01", "jsmith", old_folder=old)
        log.started("v2.0.0", "x")
        log.started("v2.0.1", "x")
        with open(log.path, "rb") as f:
            before = f.read()
        self.assertEqual(json.loads(before)["last_update"]["from"], "v2.0.0")
        real_open = open

        def flaky_open(path, mode="r", *args, **kwargs):
            if path == log.path and "r" in mode:
                raise OSError("The specified network name is no longer available")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(zl, "open", flaky_open, create=True):
            self.assertFalse(log.started("v2.0.1", "x"))
            self.assertFalse(log.started("v2.0.2", "x"))
            self.assertFalse(log.write(running=True))
        with open(log.path, "rb") as f:
            self.assertEqual(f.read(), before)
        log.started("v2.0.2", "x")                  # the share is back
        r = log.read()
        self.assertEqual(r["first_seen"], "2025-01-10 08:00:00")
        self.assertEqual((r["last_update"]["from"], r["last_update"]["to"]), ("v2.0.1", "v2.0.2"))
        self.assertEqual([e["event"] for e in r["events"]][-1], "Updated v2.0.1 -> v2.0.2")

        with open(log.path, "w", encoding="utf-8") as f:
            f.write('{"version": "v2.0.2", "first_s')
        log.started("v2.0.2", "x")
        r = log.read()
        self.assertEqual([e["event"] for e in r["events"]], ["Installed (v2.0.2)"])
        self.assertNotIn("last_update", r)
        self.assertNotEqual(r["first_seen"], "2025-01-10 08:00:00")

    def test_no_status_folder_means_no_writes(self):
        log = zl.DeploymentLog(os.path.join(self.dir, "missing"), "PC", "U")
        self.assertFalse(log.started("v1.2.0", "x"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "missing")))

    def test_status_folder_sits_next_to_config(self):
        # This version reports in under v2; the older versions' folder is only read (architecture §10.5)
        config = os.path.join(self.dir, "Stripes", "config")
        with mock.patch.dict(os.environ, {"STRIPES_STATUS": "", "STRIPES_CONFIG": config, "STRIPES_V2": ""}):
            self.assertEqual(zl.status_folder(), os.path.join(self.dir, "Stripes", "v2", "status", "deployment"))
            self.assertEqual(zl.old_status_folder(), os.path.join(self.dir, "Stripes", "status"))


# --------------------------------------------------------------------------
# The v2 folders (architecture §10.5)
# --------------------------------------------------------------------------

_STRIPES_VARIABLES = ("STRIPES_SETTINGS", "STRIPES_CACHE", "STRIPES_STATUS", "STRIPES_CONFIG", "STRIPES_V2",
                      "STRIPES_V2_ROAMING", "STRIPES_V2_LOCAL", "STRIPES_V2_MACHINE")


def isolated_profile(test, tmp):
    """APPDATA and LOCALAPPDATA in a temp folder, and none of Stripes' own overrides, until the
    test ends."""
    env = {"APPDATA": os.path.join(tmp, "Roaming"), "LOCALAPPDATA": os.path.join(tmp, "Local")}
    patcher = mock.patch.dict(os.environ, env)
    patcher.start()
    test.addCleanup(patcher.stop)  # puts back the whole environment, the removed overrides too
    for name in _STRIPES_VARIABLES:
        os.environ.pop(name, None)
    return env


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def snapshot(folder):
    """Every file under a folder with its bytes, to show that nothing was written there."""
    found = {}
    for here, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(here, name)
            with open(path, "rb") as f:
                found[os.path.relpath(path, folder)] = f.read()
    return found


class V2PathTests(unittest.TestCase):
    """This version keeps its own files under v2 folders, which the older versions never read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = isolated_profile(self, self.tmp)
        self.roaming, self.local = env["APPDATA"], env["LOCALAPPDATA"]

    def test_settings_cache_and_instance_under_v2(self):
        self.assertEqual(zl.settings_path(), os.path.join(self.roaming, "Stripes", "v2", "settings.json"))
        self.assertEqual(zl.old_settings_path(), os.path.join(self.roaming, "Stripes", "settings.json"))
        self.assertEqual(zl.cache_folder(), os.path.join(self.local, "Stripes", "v2", "config-cache"))
        self.assertEqual(zl.SingleInstance().path, os.path.join(self.local, "Stripes", "v2", "instance.txt"))
        # The v2 overrides move them, and the older overrides still win
        with mock.patch.dict(os.environ, {"STRIPES_V2_ROAMING": os.path.join(self.tmp, "r"),
                                          "STRIPES_V2_LOCAL": os.path.join(self.tmp, "l")}):
            self.assertEqual(zl.settings_path(), os.path.join(self.tmp, "r", "settings.json"))
            self.assertEqual(zl.cache_folder(), os.path.join(self.tmp, "l", "config-cache"))
            self.assertEqual(zl.SingleInstance().path, os.path.join(self.tmp, "l", "instance.txt"))
            with mock.patch.dict(os.environ, {"STRIPES_SETTINGS": os.path.join(self.tmp, "s.json"),
                                              "STRIPES_CACHE": os.path.join(self.tmp, "c", "cache")}):
                self.assertEqual(zl.settings_path(), os.path.join(self.tmp, "s.json"))
                self.assertEqual(zl.cache_folder(), os.path.join(self.tmp, "c", "cache"))
                self.assertEqual(zl.SingleInstance().path, os.path.join(self.tmp, "c", "instance.txt"))
        self.assertEqual(zl.old_settings_path(), os.path.join(self.roaming, "Stripes", "settings.json"))

    def test_settings_copied_once(self):
        old = zl.old_settings_path()
        self.assertFalse(zl.copy_old_settings())                 # nothing to bring along
        self.assertFalse(os.path.exists(zl.settings_path()))
        write_json(old, {"printers": [asdict(ZD420)], "last_printer": "Packing", "language": "es"})
        with open(old, "rb") as f:
            old_bytes = f.read()
        self.assertTrue(zl.copy_old_settings())
        loaded = zl.load_settings()
        self.assertEqual((loaded["printers"], loaded["last_printer"], loaded["language"]), ([ZD420], "Packing", "es"))
        # Only once: changes are saved under v2, and today's file is never written
        zl.save_settings(dict(loaded, language="en"))
        self.assertFalse(zl.copy_old_settings())
        self.assertEqual(zl.load_settings()["language"], "en")
        with open(old, "rb") as f:
            self.assertEqual(f.read(), old_bytes)
        write_json(old, {"printers": [], "language": "es"})      # the older version, still in use, saves
        self.assertFalse(zl.copy_old_settings())
        self.assertEqual(zl.load_settings()["printers"], [ZD420])

    def test_no_copy_with_settings_override(self):
        write_json(zl.old_settings_path(), {"language": "es"})
        mine = os.path.join(self.tmp, "mine.json")
        with mock.patch.dict(os.environ, {"STRIPES_SETTINGS": mine}):
            self.assertFalse(zl.copy_old_settings())
            self.assertFalse(os.path.exists(mine))
        self.assertFalse(os.path.exists(zl.settings_path()))

    def test_saved_config_copied_once(self):
        # The older version's copy of the company printers and stock, so the first start of this
        # version lists them (and can select the one used last) before the server has been read
        old, new = zl.old_cache_folder(), zl.cache_folder()
        self.assertEqual(old, os.path.join(self.local, "Stripes", "config-cache"))
        self.assertFalse(zl.copy_old_cache())                    # nothing to bring along
        self.assertFalse(os.path.exists(new))
        shutil.copytree(EXAMPLE_CONFIG, old)
        saved = 1_700_000_000
        for name in os.listdir(old):
            os.utime(os.path.join(old, name), (saved, saved))
        before = snapshot(old)
        self.assertTrue(zl.copy_old_cache())
        self.assertEqual(snapshot(new), {k: v for k, v in before.items() if k in ("printers.json", "stock.json")})
        cached = zl.load_cached_config("config")
        self.assertEqual(([p.name for p in cached.printers], len(cached.stock)),
                         (["Packing ZD420", "Pallets ZT410"], 3))
        # "saved {when}" is when the older version saved it, not when it was copied
        self.assertEqual([os.path.getmtime(os.path.join(new, n)) for n in ("printers.json", "stock.json")],
                         [saved, saved])
        # Only once: this version's own copy is never replaced, and the older one is never written
        write_json(os.path.join(old, "printers.json"), {"printers": []})   # the older version, still in use, saves
        self.assertFalse(zl.copy_old_cache())
        self.assertEqual(len(zl.load_cached_config("config").printers), 2)
        # A server without stock.json: this version's copy has none, and the old one doesn't come back
        os.remove(os.path.join(new, "stock.json"))
        self.assertFalse(zl.copy_old_cache())
        self.assertFalse(os.path.exists(os.path.join(new, "stock.json")))
        self.assertEqual(snapshot(old)["stock.json"], before["stock.json"])

    def test_no_cache_copy_with_cache_override(self):
        shutil.copytree(EXAMPLE_CONFIG, zl.old_cache_folder())
        mine = os.path.join(self.tmp, "mine")
        with mock.patch.dict(os.environ, {"STRIPES_CACHE": mine}):
            self.assertFalse(zl.copy_old_cache())
            self.assertFalse(os.path.exists(mine))
        self.assertFalse(os.path.exists(zl.cache_folder()))

    def test_deployment_log_folders(self):
        share = os.path.join(self.tmp, "Share", "Stripes")
        new, old = os.path.join(share, "v2", "status", "deployment"), os.path.join(share, "status")
        with mock.patch.object(zl, "configured_folder", return_value=None):
            self.assertEqual((zl.status_folder(), zl.old_status_folder()), (None, None))
        os.environ["STRIPES_CONFIG"] = os.path.join(share, "config")
        self.assertEqual((zl.status_folder(), zl.old_status_folder()), (new, old))
        with mock.patch.dict(os.environ, {"STRIPES_V2": os.path.join(self.tmp, "elsewhere")}):
            self.assertEqual(zl.status_folder(), os.path.join(self.tmp, "elsewhere", "status", "deployment"))
        with mock.patch.dict(os.environ, {"STRIPES_STATUS": os.path.join(self.tmp, "st")}):
            self.assertEqual(zl.status_folder(), os.path.join(self.tmp, "st"))
        # Written only to the new folder, and only once the folder script has made it
        os.makedirs(old)
        self.assertFalse(zl.DeploymentLog(zl.status_folder(), "FLOOR01", "jsmith").started("v2.0.0", "x"))
        os.makedirs(new)
        self.assertTrue(zl.DeploymentLog(zl.status_folder(), "FLOOR01", "jsmith").started("v2.0.0", "x"))
        self.assertEqual((os.listdir(new), os.listdir(old)), (["FLOOR01_jsmith.json"], []))

    def test_deployment_window_reads_both_folders(self):
        new, old = os.path.join(self.tmp, "new"), os.path.join(self.tmp, "old")

        def record(folder, computer, user, version, seen):
            write_json(os.path.join(folder, f"{computer}_{user}.json"),
                       {"computer": computer, "user": user, "version": version, "last_seen": seen})

        record(old, "FLOOR01", "jsmith", "v1.4.0", "2026-10-28 08:00:00")
        record(old, "FLOOR02", "akim", "v1.4.0", "2026-10-29 09:00:00")
        record(new, "FLOOR01", "JSmith", "v2.0.0", "2026-10-30 07:00:00")
        found = zl.read_all_deployment([new, old, os.path.join(self.tmp, "missing")])
        self.assertEqual([(r["computer"], r["version"]) for r in found], [("FLOOR01", "v2.0.0"), ("FLOOR02", "v1.4.0")])
        # The older version started again after the new one: the newer record wins
        record(old, "FLOOR01", "jsmith", "v1.4.0", "2026-10-30 08:00:00")
        found = zl.read_all_deployment([new, old])
        self.assertEqual([(r["computer"], r["version"]) for r in found], [("FLOOR01", "v1.4.0"), ("FLOOR02", "v1.4.0")])


class ZebraConfigTests(unittest.TestCase):
    r"""The Zebra printers and stock come from v2\config once the folder script has copied them,
    and from today's folder (never written) until then."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        isolated_profile(self, self.tmp)
        share = os.path.join(self.tmp, "Share", "Stripes")
        self.config, self.v2_config = os.path.join(share, "config"), os.path.join(share, "v2", "config")
        shutil.copytree(EXAMPLE_CONFIG, self.config)
        os.environ["STRIPES_CONFIG"] = self.config
        os.environ["STRIPES_CACHE"] = os.path.join(self.tmp, "cache")

    def test_read_from_v2_config_with_fallback(self):
        before = snapshot(self.config)
        # Before the folder script: today's folder
        self.assertEqual(zl.zebra_config_folder(), self.config)
        cfg = zl.load_company_config()
        self.assertEqual((cfg.source, cfg.problems), (self.config, []))
        self.assertIn("Packing ZD420", [p.name for p in cfg.printers])
        # stock.json alone isn't enough: printers.json decides
        os.makedirs(self.v2_config)
        shutil.copy(os.path.join(self.config, "stock.json"), self.v2_config)
        self.assertEqual(zl.zebra_config_folder(), self.config)
        # Copied (and changed there since): v2\config is read
        write_json(os.path.join(self.v2_config, "printers.json"),
                   {"printers": [{"name": "Line 1 Labels", "host": "192.0.2.30", "stock": "2x1-gap"}]})
        self.assertEqual(zl.zebra_config_folder(), self.v2_config)
        cfg = zl.load_company_config()
        self.assertEqual((cfg.source, [p.name for p in cfg.printers], cfg.problems),
                         (self.v2_config, ["Line 1 Labels"], []))
        self.assertEqual(zl.label_dots(cfg.printers[0]), (406, 203))   # its stock from v2\config\stock.json
        self.assertEqual([p.name for p in zl.load_cached_config(self.config).printers], ["Line 1 Labels"])
        # STRIPES_V2 moves the v2 folder
        with mock.patch.dict(os.environ, {"STRIPES_V2": os.path.join(self.tmp, "nowhere")}):
            self.assertEqual(zl.zebra_config_folder(), self.config)
        self.assertEqual(snapshot(self.config), before)


# --------------------------------------------------------------------------
# The tray menu (pystray faked, so it runs anywhere)
# --------------------------------------------------------------------------

class TrayTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_menu_has_maplejet_monitor(self):
        class MenuItem:
            def __init__(self, text, action, **kw):
                self.text, self.action = text, action

        class Menu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        class Icon:
            def __init__(self, name, image, title, menu):
                image.load()            # as pystray does
                self.menu = menu

            def run_detached(self):
                pass

        pystray = types.ModuleType("pystray")
        pystray.MenuItem, pystray.Menu, pystray.Icon = MenuItem, Menu, Icon
        calls = []
        app = types.SimpleNamespace(call=calls.append, show="show", show_monitor="printer monitor",
                                    show_hx_monitor="maplejet monitor", quit_app="quit")
        with mock.patch.dict(sys.modules, {"pystray": pystray}):
            tray = zl.Tray(app)
        items = tray.icon.menu.items

        def labels():
            return ["-" if item is Menu.SEPARATOR else item.text(item) for item in items]

        self.assertEqual(labels(), ["Open Stripes", "Printer monitor", "MapleJet monitor", "-", "Quit Stripes"])
        items[2].action(tray.icon, items[2])
        self.assertEqual(calls, ["maplejet monitor"])   # handed to the window's thread
        i18n.set_language("es")
        self.assertEqual(labels()[1:3], ["Monitor de impresoras", "Monitor MapleJet"])


# --------------------------------------------------------------------------
# The window (needs a display)
# --------------------------------------------------------------------------

def tk_or_skip():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        raise unittest.SkipTest(f"Tk can't start: {e}")
    root.withdraw()
    return root


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def inside(widget, container):
    return str(widget).startswith(str(container) + ".")


def alive(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def cancel_timers(app):
    """Stop the window's own timers before it goes, so none fires into the next test's Tk."""
    for timer in app.tk.splitlist(app.tk.call("after", "info")):
        app.tk.call("after", "cancel", timer)


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TkCase(unittest.TestCase):
    """One withdrawn root per class. Garbage is collected on the Tk thread only, as in the app
    while the MapleJet services run (architecture §7.1 rule 3)."""
    root = None
    _gc_was_enabled = True

    @classmethod
    def setUpClass(cls):
        cls.root = tk_or_skip()
        cls._gc_was_enabled = gc.isenabled()
        gc.disable()

    @classmethod
    def tearDownClass(cls):
        for window in hx_ui_common.open_windows():
            window.destroy()
        gc.collect()
        cls.root.destroy()
        gc.collect()
        if cls._gc_was_enabled:
            gc.enable()

    def setUp(self):
        self.addCleanup(gc.collect)     # registered first, so it runs after every other cleanup

    def tearDown(self):
        i18n.set_language("en")
        for window in hx_ui_common.open_windows():
            window.destroy()
        for child in self.root.winfo_children():
            child.destroy()


class ShortcutGuardTests(TkCase):
    def test_zebra_shortcut_guard(self):
        # ui.md §17.2: App._zebra_shortcut with a stand-in for the App holding the two top tabs
        tabs = ttk.Notebook(self.root)
        zebra, maplejet = ttk.Frame(tabs), ttk.Frame(tabs)
        tabs.add(zebra, text="  Zebra  ")
        tabs.add(maplejet, text="  MapleJet  ")
        fake_self = types.SimpleNamespace(top_tabs=tabs, zebra_tab=zebra, hx_tab=maplejet)
        simple_box, stock_box, send_button = tk.Text(zebra), ttk.Combobox(zebra), ttk.Button(maplejet)
        printed = []

        def press(widget):
            printed.clear()
            zl.App._zebra_shortcut(fake_self, types.SimpleNamespace(widget=widget), lambda: printed.append(widget))
            return printed == [widget]

        tabs.select(zebra)
        self.assertTrue(press(simple_box))
        self.assertTrue(press(f"{stock_box}.popdown.f.l"))     # its drop-down list, known only by its path
        self.assertTrue(press(tk.Toplevel(self.root)))         # a Zebra window, as before
        tabs.select(maplejet)
        self.assertFalse(press(simple_box))
        self.assertFalse(press(send_button))
        # Never from a MapleJet window or its question, whichever tab is selected
        tabs.select(zebra)
        window = hx_ui_common.HxWindow(self.root)
        entry, choice = ttk.Entry(window), ttk.Combobox(ttk.Frame(window))
        dialog = hx_ui_common._AskDialog(self.root, "Quit anyway?", "Quit anyway", "Keep Stripes open")
        for widget in (window, entry, f"{choice}.popdown.f.l", dialog, dialog.yes_button):
            self.assertFalse(press(widget), widget)
        dialog.destroy()
        window.destroy()


class RetranslateTests(TkCase):
    def test_marked_widgets_are_skipped(self):
        class Marked(ttk.Frame):
            hx_no_translate = True

        frame = ttk.Frame(self.root)
        plain = ttk.Label(frame, text="Simple")
        inner = ttk.Label(Marked(frame), text="Simple")
        i18n.set_language("es")
        zl.App._retranslate(self.root)
        self.assertEqual((plain.cget("text"), inner.cget("text")), ("Sencillo", "Simple"))
        i18n.set_language("en")
        zl.App._retranslate(self.root)
        self.assertEqual(plain.cget("text"), "Simple")


class BackgroundTests(TkCase):
    """The Zebra side, every MapleJet window and the tray (App.call) share one Background
    (architecture §10.1 item 2), so one failing done() mustn't stop the others' results (§8 rule 1)."""

    def test_a_failing_callback_doesnt_stop_the_rest(self):
        bg = zl.Background(self.root)
        self.addCleanup(cancel_timers, self.root)
        cancel_timers(self.root)            # the test runs the first round itself
        got = []

        def broken(result, error):
            # e.g. a MapleJet window's done() touching a widget destroyed meanwhile
            raise tk.TclError('invalid command name ".!sendwindow.!label"')

        bg.results.put((broken, 1, None))
        bg.results.put((lambda result, error: got.append(result), "printer ready", None))
        zl.App.call(types.SimpleNamespace(bg=bg), lambda: got.append("Quit Stripes"))   # a tray menu item
        with mock.patch("traceback.print_exc") as print_exc:
            bg._poll()
        self.assertEqual(got, ["printer ready", "Quit Stripes"])
        print_exc.assert_called_once_with()                 # printed, not swallowed
        self.assertEqual(len(self.root.tk.splitlist(self.root.tk.call("after", "info"))), 1)   # polls on
        # And through the real loop: a worker's result after the failure still arrives
        def pump_until(ready):
            deadline = time.monotonic() + 5.0
            while not ready() and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)

        with mock.patch("traceback.print_exc") as print_exc:
            bg.run(lambda: 1, broken)
            pump_until(lambda: print_exc.called)
            bg.run(lambda: "config loaded", lambda result, error: got.append(result))
            pump_until(lambda: len(got) > 2)
        print_exc.assert_called_once_with()
        self.assertEqual(got[2:], ["config loaded"])

    def test_polls_on_whatever_a_callback_raises(self):
        bg = zl.Background(self.root)
        self.addCleanup(cancel_timers, self.root)
        cancel_timers(self.root)

        def interrupted(result, error):
            raise KeyboardInterrupt

        bg.results.put((interrupted, None, None))
        with self.assertRaises(KeyboardInterrupt):
            bg._poll()
        self.assertEqual(len(self.root.tk.splitlist(self.root.tk.call("after", "info"))), 1)


# ui.md §17.1's scaled timings, with printer and share timeouts that leave room for a busy machine
SCALED = replace(TIMINGS.scaled(0.05), http_timeout_s=0.5, web_repeat_window_s=0.5, share_timeout_s=1.0,
                 lock_stale_s=30.0)


def machine_view(mid, line_id, origin):
    return MachineView(machine_id=mid, line_id=line_id, name="Line 1 Bagger 1", colour=GREEN, code="printing",
                       headline=Msg("Printing"), origin_line=origin, printing=True, monitored=True, checked_age_s=2.0)


def board_of(line_id, *machines):
    return BoardView(lines=[LineView(line_id=line_id, name="Line 1", colour=GREEN, headline=Msg("Printing"),
                                     machines=list(machines), monitored=True, checked_age_s=2.0)],
                     share_ok=True, generated_at=time.time())


class BagTextTests(TkCase):
    """ui.md §17.1: a FakeWorld, the v2 folders in temp folders, services from make_services, strict
    threads, and tell/ask patched on hx_ui_common."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="stripes_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.world = FakeWorld(self.tmp, printers=[{}], timings=SCALED)
        self.addCleanup(self.world.close)
        env = {"STRIPES_V2": self.world.share_root}
        for name, folder in (("STRIPES_V2_ROAMING", "roaming"), ("STRIPES_V2_LOCAL", "local"),
                             ("STRIPES_V2_MACHINE", "machine")):
            env[name] = os.path.join(self.tmp, "pc", folder)
            os.makedirs(env[name])
        for patcher in (mock.patch.dict(os.environ, env), mock.patch.object(hx_ui_common, "tell"),
                        mock.patch.object(hx_ui_common, "ask", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        hx_ui_common.STRICT_THREADS = True
        self.addCleanup(setattr, hx_ui_common, "STRICT_THREADS", False)
        identity = self.world.identity("jsmith", admin=True, sender=True)
        self.services = hx_ui.make_services(self.root, config_path=None, bg=hx_ui_common.SyncBackground(),
                                            version="test", timings=SCALED, identity=identity,
                                            fs=self.world.fs.view(), gates=GateRegistry(), key=release_key())
        self.services.data.run_once()
        self.services.bus.pump()

    def test_bag_text_survives_spanish(self):
        services, ids = self.services, self.world.ids
        nav, lid, mid = services.nav, ids["line"], ids["machine1"]
        board = board_of(lid, machine_view(mid, lid, "Product of USA"))
        services.watcher.latest = board
        # The main window's two tabs: a Zebra label, and the MapleJet tab with its line card
        tabs = ttk.Notebook(self.root)
        zebra = ttk.Frame(tabs)
        tabs.add(zebra, text="  Zebra  ")
        zebra_label = ttk.Label(zebra, text="Simple")
        tab = hx_ui.MapleJetTab(tabs, services)
        tabs.add(tab, text="  MapleJet  ")
        services.bus.post(BoardUpdate(board=board))
        services.bus.pump()
        # A prefilled Send window with its trial plan, a result window of a made-up result, the floor board
        choice = LineChoice(line_id=lid, machine_ids=[mid], product_id=ids["product"], film_id=ids["film"],
                            print_text_id=ids["print_text"], lot="4417", countries=["USA", "Canada"])
        nav.open_send(prefill=SendRequest(send_id=new_id("sd"), sender=services.identity.user,
                                          computer=services.identity.computer, created_at=time.time(),
                                          lines=[choice]))
        send = nav.window("send")
        self.assertIsNotNone(send.blocks[lid].trial)
        self.assertEqual(send.send_button.cget("text"), "Send to 1 machine")
        now = time.time()
        result = hx_ui_send.ResultWindow(self.root, services, SendResult(
            send_id=new_id("sd"), sender=services.identity.user, computer=services.identity.computer,
            started_at=now, finished_at=now, time_source="server",
            machines=[MachineResult(machine_id=mid, colour=AMBER, ok=True, step=STEP_CONFIRM_2,
                                    origin_line="Product of USA and Canada")]))
        with mock.patch.object(hx_win, "monitors", return_value=[]):
            nav.open_floor_board()

        def bag_texts():
            out = []
            for widget in (tab, send, result):
                for bag in descendants(widget):
                    if isinstance(bag, hx_ui_common.BagText):
                        out += [bag.get(), bag.itemcget(bag.text_item, "text")]
            floor = nav.window("floor")      # the floor board may rebuild itself
            return out + [floor.canvas.itemcget(i, "text") for i in floor.origin_items.values()]

        before = bag_texts()
        self.assertGreaterEqual(before.count("Product of USA and Canada"), 4)   # the Send and result windows
        self.assertGreaterEqual(before.count("Product of USA"), 3)              # the card and the floor board
        i18n.set_language("es")
        zl.App._retranslate(self.root)
        hx_ui.language_changed(services)
        self.assertEqual(bag_texts(), before)
        self.assertEqual(send.send_button.cget("text"), "Enviar a 1 máquina")
        self.assertEqual((zebra_label.cget("text"), tab.monitor_button.cget("text")), ("Sencillo", "Monitor MapleJet"))
        self.assertEqual([tabs.tab(t, "text") for t in tabs.tabs()], ["  Zebra  ", "  MapleJet  "])
        i18n.set_language("en")
        zl.App._retranslate(self.root)
        hx_ui.language_changed(services)
        self.assertEqual(send.send_button.cget("text"), "Send to 1 machine")
        self.assertEqual(bag_texts(), before)


class FakeTray:
    def __init__(self, app):
        self.app, self.stopped, self.notes = app, 0, []

    def notify(self, text):
        self.notes.append(text)

    def stop(self):
        self.stopped += 1


class AppTests(unittest.TestCase):
    """The real window, built as it is at start-up, with the MapleJet services made but not started
    (their threads are tested in test_hx_ui.py) and the profile in temp folders."""
    _gc_was_enabled = True

    @classmethod
    def setUpClass(cls):
        tk_or_skip().destroy()
        cls._gc_was_enabled = gc.isenabled()
        gc.disable()                    # collected on the Tk thread only, after each test

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        if cls._gc_was_enabled:
            gc.enable()

    def setUp(self):
        self.addCleanup(gc.collect)
        self.tmp = tempfile.mkdtemp(prefix="stripes_app_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        isolated_profile(self, self.tmp)
        os.environ["STRIPES_V2_MACHINE"] = os.path.join(self.tmp, "machine")
        # The older version's settings, with one printer of the user's own: brought along at the first start
        self.old_settings = zl.old_settings_path()
        write_json(self.old_settings, {"printers": [asdict(zl.Printer(name="Packing", host="127.0.0.1",
                                                                      port=closed_port()))],
                                       "last_printer": "Packing"})
        self.events, self.made, self.bound = [], [], {}
        self.config = None                  # no ConfigPath on this PC, unless a test sets one
        real_bind_all = tk.Misc.bind_all

        def bind_all(widget, *args, **kw):
            self.bound[args[0] if args else kw.get("sequence")] = args[1] if len(args) > 1 else kw.get("func")
            return real_bind_all(widget, *args, **kw)

        def start_services(root, *, config_path, bg, version):
            self.events.append(("start_services", hasattr(root, "top_tabs"), bg is getattr(root, "bg", None),
                                config_path))
            services = hx_ui.make_services(root, config_path=config_path, bg=bg, version=version, clock=FakeClock(),
                                           identity=Identity(user="EXAMPLE\\jsmith", computer="FLOOR01",
                                                             sids=frozenset(), pid=os.getpid()),
                                           gates=GateRegistry(), key=release_key())
            self.made.append(services)
            return services

        def startup(root, services):
            self.events.append(("startup", hasattr(root, "tray") and hasattr(root, "deployment"), services))

        for target, name, value in ((zl.App, "bind_all", bind_all), (hx_ui, "start_services", start_services),
                                    (hx_ui, "startup", startup), (zl, "Tray", FakeTray),
                                    (zl, "configured_folder", lambda: self.config), (zl, "local_folders", lambda: []),
                                    (zl, "VERSION", "dev"), (zl, "HX_KEYED", False),
                                    (zl.App, "_first_run", mock.Mock()), (hx_win, "stop_alert", mock.Mock())):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = zl.App(start_hidden=True)
        self.addCleanup(self.close_app)
        self.app.update_idletasks()

    def close_app(self):
        i18n.set_language("en")
        if alive(self.app):
            cancel_timers(self.app)
            self.app.destroy()

    def with_config_folder(self):
        """The window again, on a PC whose ConfigPath points at a copy of the example config.
        Returns the share's Stripes folder (the config folder's parent)."""
        share = os.path.join(self.tmp, "Share", "Stripes")
        shutil.copytree(EXAMPLE_CONFIG, os.path.join(share, "config"))
        self.close_app()
        self.config, self.events, self.made = os.path.join(share, "config"), [], []
        self.app = zl.App(start_hidden=True)
        self.app.update_idletasks()
        return share

    def pump_until(self, ready):
        """Let Background deliver its workers' results (bounded; each wait is short)."""
        deadline = time.monotonic() + 5.0
        while not ready() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        return ready()

    def header_texts(self):
        return [w.cget("text") for w in descendants(self.app)
                if isinstance(w, ttk.Label) and not inside(w, self.app.top_tabs)]

    def test_start_up_order(self):
        app = self.app
        # The services before the window is built (with its Background), the floor board last
        self.assertEqual(self.events, [("start_services", False, True, None), ("startup", True, app.hx)])
        self.assertIs(app.hx, self.made[0])
        self.assertIsNone(app.config_folder)
        # The older version's settings were brought along before they were read
        self.assertEqual(app.printer_var.get(), "Packing")
        with open(self.old_settings, "rb") as old, open(zl.settings_path(), "rb") as new:
            self.assertEqual(new.read(), old.read())

    def test_both_sides_get_the_config_folder(self):
        # architecture §10.1 item 2: the MapleJet services get the ConfigPath and find v2 next to it,
        # and the Zebra printers are read from the same folder, in the background
        share = self.with_config_folder()
        app = self.app
        self.assertEqual(self.events[0], ("start_services", False, True, self.config))
        self.assertEqual((app.config_folder, app.hx.paths.share_root), (self.config, os.path.join(share, "v2")))
        self.assertTrue(self.pump_until(lambda: app.cfg.printers))
        self.assertEqual(app.cfg.source, self.config)

    def first_start_after_upgrade(self, own_printer=True):
        """The profile as the older version leaves it: its saved copy of the company printers, and
        settings whose printer used last is a company one (with a printer of the user's own or not)."""
        self.close_app()
        os.remove(zl.settings_path())       # brought along by setUp's window
        self.assertFalse(os.path.exists(os.path.join(zl.cache_folder(), "printers.json")))
        shutil.copytree(EXAMPLE_CONFIG, zl.old_cache_folder())
        own = [asdict(zl.Printer(name="Packing", host="127.0.0.1", port=closed_port()))] if own_printer else []
        write_json(self.old_settings, {"printers": own, "last_printer": "Pallets ZT410"})

    def test_first_start_keeps_the_company_printer_used_last(self):
        # architecture §10.5: the older version's saved copy comes along, so the printer used last is
        # selected straight away and stays selected once the server has been read
        self.first_start_after_upgrade()
        self.with_config_folder()
        app = self.app
        self.assertEqual(app.printer_var.get(), "Pallets ZT410")
        self.assertTrue(self.pump_until(lambda: app.cfg.cached_at is None))
        self.assertEqual(app.cfg.source, self.config)
        self.assertEqual(app.printer_var.get(), "Pallets ZT410")
        self.assertIn("Packing", [p.name for p in app.all_printers()])

    def test_first_start_with_the_server_down_lists_the_saved_printers(self):
        self.first_start_after_upgrade(own_printer=False)
        self.config = os.path.join(self.tmp, "Share", "Stripes", "config")   # unreachable
        self.events, self.made = [], []
        self.app = zl.App(start_hidden=True)
        app = self.app
        self.assertTrue(self.pump_until(lambda: app.cfg.problems))
        self.assertIn("using the copy saved", app.cfg.problems[0])
        self.assertEqual(([p.name for p in app.all_printers()], app.printer_var.get()),
                         (["Packing ZD420", "Pallets ZT410"], "Pallets ZT410"))
        app.update()
        zl.App._first_run.assert_not_called()    # printers to list, so no Welcome box (_maybe_first_run)

    def test_first_start_keeps_the_deployment_history(self):
        # The app gives its DeploymentLog the older versions' folder, so the upgrade shows as one
        share = os.path.join(self.tmp, "Share", "Stripes")
        new, old = os.path.join(share, "v2", "status", "deployment"), os.path.join(share, "status")
        os.makedirs(new)                            # the folder script has run
        name = os.path.basename(zl.DeploymentLog(new).path)
        write_json(os.path.join(old, name), {"computer": "FLOOR01", "user": "jsmith", "version": "v1.4.0",
                                             "first_seen": "2025-01-10 08:00:00", "last_seen": "2026-10-29 16:00:00",
                                             "events": [{"at": "2025-01-10 08:00:00", "event": "Installed (v1.4.0)"}]})
        self.with_config_folder()
        app = self.app
        self.assertTrue(self.pump_until(lambda: os.path.exists(os.path.join(new, name))))
        r = app.deployment.read()
        self.assertEqual((r["version"], r["first_seen"], (r.get("last_update") or {}).get("from")),
                         ("dev", "2025-01-10 08:00:00", "v1.4.0"))
        self.assertEqual([e["event"] for e in r["events"]], ["Installed (v1.4.0)", "Updated v1.4.0 -> dev"])
        self.assertEqual(app.deployment.old_path, os.path.join(old, name))
        self.assertEqual(os.listdir(old), [name])       # only read

    def test_deployment_status_lists_both_folders(self):
        # architecture §10.5 item 4: the rollout view reads this version's folder and the older versions' one
        share = self.with_config_folder()
        app = self.app
        new, old = os.path.join(share, "v2", "status", "deployment"), os.path.join(share, "status")
        for folder, computer, user, version, seen in ((new, "FLOOR01", "jsmith", "v2.0.0", "2026-10-30 07:00:00"),
                                                      (old, "FLOOR02", "akim", "v1.4.0", "2026-10-29 09:00:00")):
            write_json(os.path.join(folder, f"{computer}_{user}.json"),
                       {"computer": computer, "user": user, "version": version, "last_seen": seen})
        shown = []

        def wait_window(widget, window=None):
            if isinstance(window, zl.DeploymentWindow):
                self.pump_until(lambda: len(window.tree.get_children()) == 2)
                rows = [window.tree.set(row, "computer") for row in window.tree.get_children()]
                shown.append((window.folders, rows))
                window.destroy()

        def no_grab(widget):
            pass                            # nothing to grab in a test, and nothing waits

        with mock.patch.object(zl.PrinterDialog, "wait_window", wait_window), \
                mock.patch.object(zl.PrinterDialog, "grab_set", no_grab), \
                mock.patch.object(zl.DeploymentWindow, "grab_set", no_grab):
            dialog = zl.PrinterDialog(app)
            buttons = [w for w in descendants(dialog)
                       if isinstance(w, ttk.Button) and w.cget("text") == "Deployment status..."]
            self.assertEqual(len(buttons), 1)
            buttons[0].invoke()
        dialog.destroy()
        self.assertEqual(shown, [([new, old], ["FLOOR01", "FLOOR02"])])

    def test_hiding_to_the_tray_leaves_the_maplejet_side_running(self):
        # ui.md §3.4: hiding to the tray stops no Send, poll or alert sound, and the MapleJet
        # monitor (a window of its own) stays open
        app, hx = self.app, self.app.hx
        app.tray = FakeTray(app)
        hx.nav.sounder = mock.Mock()        # as start_services leaves it
        app.deiconify()
        app.show_hx_monitor()
        monitor = hx.nav.window("monitor")
        hx_win.stop_alert.reset_mock()
        with mock.patch.object(hx_ui, "stop_services") as stop_services, \
                mock.patch.object(hx_ui, "can_quit") as can_quit, \
                mock.patch.object(hx.poller, "stop") as poller_stop, \
                mock.patch.object(hx.watcher, "stop") as watcher_stop, \
                mock.patch.object(hx.data, "stop") as data_stop:
            app.hide_to_tray()
            app.update_idletasks()
            self.assertEqual(app.wm_state(), "withdrawn")
            app.deiconify()
            app.hide_to_tray()
        for stopped in (stop_services, can_quit, poller_stop, watcher_stop, data_stop, hx.nav.sounder.stop,
                        hx_win.stop_alert):
            stopped.assert_not_called()
        self.assertIs(hx.nav.window("monitor"), monitor)
        self.assertEqual(monitor.wm_state(), "normal")
        self.assertEqual(len(app.tray.notes), 1)        # the hint, the first time only
        self.assertTrue(zl.load_settings()["tray_hint_shown"])

    def test_two_tabs(self):
        app, tabs = self.app, self.app.top_tabs
        self.assertEqual([tabs.tab(t, "text") for t in tabs.tabs()], ["  Zebra  ", "  MapleJet  "])
        self.assertEqual(tabs.tabs(), (str(app.zebra_tab), str(app.hx_tab)))
        self.assertEqual(str(tabs.select()), str(app.zebra_tab))       # the Zebra tab at start
        self.assertIsInstance(app.hx_tab, hx_ui.MapleJetTab)
        self.assertIs(app.hx.nav.tab, app.hx_tab)
        # Everything below the header is on the Zebra tab ...
        for widget in (app.printer_box, app.printer_status, app.stock_box, app.size_label, app.tabs, app.simple_box,
                       app.recent_box, app.preview_frame, app.canvas, app.print_btn, app.status):
            self.assertTrue(inside(widget, app.zebra_tab), widget)
        self.assertFalse(inside(app.update_label, tabs))
        # ... packed as before: Print and the status line stay visible in a short window
        top, stock_row, status, bottom, body = app.zebra_tab.pack_slaves()
        self.assertEqual((top, stock_row, body), (app.printer_box.master, app.stock_box.master,
                                                  app.preview_frame.master))
        self.assertEqual((status, bottom), (app.status, app.print_btn.master))
        self.assertEqual((status.pack_info()["side"], bottom.pack_info()["side"]), ("bottom", "bottom"))
        # The header
        self.assertIn("Zebra labels and MapleJet bag printing", self.header_texts())
        self.assertEqual(app.update_label.cget("text"), "Version dev (development key)")
        self.assertEqual((app.title(), app.minsize()), ("Stripes dev", (900, 640)))

    def test_tab_switch_shows_and_hides_the_maplejet_tab(self):
        app, nav = self.app, self.app.hx.nav
        app.top_tabs.select(app.hx_tab)
        app.update()
        self.assertTrue(nav.tab_shown)
        app.top_tabs.select(app.zebra_tab)
        app.update()
        self.assertFalse(nav.tab_shown)

    def test_shortcuts_print_only_on_the_zebra_tab(self):
        app = self.app
        self.assertTrue(app.simple_box.bind("<Control-Return>"))      # the Simple box's own binding stays
        with mock.patch.object(app, "print_label") as print_label:
            for sequence in ("<Control-p>", "<Control-Return>"):
                press = self.bound[sequence]
                print_label.reset_mock()
                press(types.SimpleNamespace(widget=app.simple_box))
                self.assertEqual(print_label.call_count, 1, sequence)
                app.top_tabs.select(app.hx_tab)
                press(types.SimpleNamespace(widget=app.hx_tab.send_button))
                app.top_tabs.select(app.zebra_tab)
                window = hx_ui_common.HxWindow(app, app.hx)
                press(types.SimpleNamespace(widget=ttk.Entry(window)))
                window.destroy()
                self.assertEqual(print_label.call_count, 1, sequence)

    def test_tray_opens_the_maplejet_monitor(self):
        app = self.app
        app.show_hx_monitor()
        window = app.hx.nav.window("monitor")
        self.assertIsInstance(window, hx_ui_monitor.MonitorWindow)
        self.assertIs(window.master, app)              # its own window: it shows while Stripes is in the tray
        app.show_hx_monitor()
        self.assertEqual([w for w in hx_ui_common.open_windows() if isinstance(w, hx_ui_monitor.MonitorWindow)],
                         [window])

    def test_language_switch(self):
        app = self.app
        with open(self.old_settings, "rb") as f:
            old = f.read()
        with mock.patch.object(hx_ui, "language_changed", wraps=hx_ui.language_changed) as changed:
            app.v_language.set("es")
            app._language_changed()
        changed.assert_called_once_with(app.hx)
        self.assertIn("Etiquetas Zebra e impresión de bolsas MapleJet", self.header_texts())
        self.assertEqual(app.update_label.cget("text"), "Versión dev (clave de desarrollo)")
        self.assertEqual(app.print_btn.cget("text"), i18n.ES["Print"])
        self.assertEqual(app.hx_tab.monitor_button.cget("text"), "Monitor MapleJet")
        self.assertEqual([app.top_tabs.tab(t, "text") for t in app.top_tabs.tabs()], ["  Zebra  ", "  MapleJet  "])
        self.assertEqual(zl.load_settings()["language"], "es")       # saved under v2 ...
        with open(self.old_settings, "rb") as f:
            self.assertEqual(f.read(), old)                          # ... never in the older version's file
        app.v_language.set("en")
        app._language_changed()
        self.assertEqual(app.update_label.cget("text"), "Version dev (development key)")
        self.assertEqual(app.hx_tab.monitor_button.cget("text"), "MapleJet monitor")

    def test_quit_asks_once_then_stops_the_services(self):
        app, hx = self.app, self.app.hx
        app.tray = FakeTray(app)
        folder = os.path.join(self.tmp, "deployment")
        os.makedirs(folder)
        app.deployment = zl.DeploymentLog(folder, "FLOOR01", "jsmith")
        app.deployment.started("dev", "x")
        stopped = []
        with mock.patch.object(hx_ui, "can_quit", return_value=False) as can_quit, \
                mock.patch.object(hx_ui, "stop_services",
                                  side_effect=lambda services, timeout_s: (stopped.append((services, timeout_s,
                                                                                           alive(app))),
                                                                           cancel_timers(app))):
            app.quit_app()                 # a Send is running, and the person keeps Stripes open
            can_quit.assert_called_once_with(app, hx)
            self.assertTrue(alive(app))
            self.assertEqual((app.tray.stopped, stopped), (0, []))
            can_quit.reset_mock()
            can_quit.return_value = True
            app.quit_app()
        can_quit.assert_called_once_with(app, hx)          # asked once per quit
        self.assertEqual(stopped, [(hx, 2.0, True)])       # before the window goes
        self.assertEqual(app.tray.stopped, 1)
        self.assertFalse(alive(app))
        self.assertFalse(app.deployment.read()["running"])

    def test_close_box_without_tray(self):
        app, hx = self.app, self.app.hx
        expected = "hide_to_tray" if sys.platform == "win32" else "_on_close"   # the tray is only on Windows
        self.assertTrue(app.protocol("WM_DELETE_WINDOW").endswith(expected))
        app.tray = None
        with mock.patch.object(hx_ui, "can_quit", return_value=False) as can_quit, \
                mock.patch.object(hx_ui, "stop_services", side_effect=lambda *a, **kw: cancel_timers(app)) \
                as stop_services:
            app._on_close()
            self.assertTrue(alive(app))
            stop_services.assert_not_called()
            can_quit.return_value = True
            app._on_close()
        self.assertEqual(can_quit.call_count, 2)
        stop_services.assert_called_once_with(hx, timeout_s=2.0)
        self.assertFalse(alive(app))


class SelfCheckOrderTests(unittest.TestCase):
    """deploy.md §1.3 item 3: the key first, before Tk starts; the MapleJet checks last, inside the try."""
    _gc_was_enabled = True

    @classmethod
    def setUpClass(cls):
        tk_or_skip().destroy()
        cls._gc_was_enabled = gc.isenabled()
        gc.disable()                    # the self-check's Tk root is collected here, on the Tk thread

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        if cls._gc_was_enabled:
            gc.enable()

    def setUp(self):
        self.addCleanup(gc.collect)

    def test_key_first_maplejet_last(self):
        order = []
        with mock.patch.object(zl, "check_release_key", side_effect=lambda v, k: order.append(("key", v, k))), \
                mock.patch.object(hx_ui, "self_check", side_effect=lambda root: order.append(("hx", root.winfo_exists()))):
            zl._self_check()
        self.assertEqual(order, [("key", zl.VERSION, zl.HX_KEYED), ("hx", 1)])
        # A release without the key fails before Tk starts
        with mock.patch.object(zl, "VERSION", "v2.0.0"), mock.patch.object(zl, "HX_KEYED", False), \
                mock.patch.object(zl.tk, "Tk") as make_root, mock.patch.object(hx_ui, "self_check") as hx_check:
            with self.assertRaisesRegex(RuntimeError, "STRIPES_HX_KEY"):
                zl._self_check()
        make_root.assert_not_called()
        hx_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
