"""Tests for the non-UI parts of stripes. Run: python -m unittest -v"""

import os
import re
import shutil
import tempfile
import socket
import threading
import types
import unittest
from unittest import mock

import label_design as ld
import stripes as zl

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

    def __init__(self, hs_reply=HS_READY, config_report=b"\x02\x03"):
        self.hs_reply = hs_reply
        self.config_report = config_report
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
        self.assertEqual(pallets.expected["media"], "mark")
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


class SpecCheckTests(unittest.TestCase):
    def test_parse_report(self):
        self.assertEqual(zl.parse_config_report(ZD_REPORT), {
            "darkness": 10.0, "speed": 6.0, "print_mode": "tear-off", "media": "gap", "print_width": 406})

    def test_unknown_format_means_no_checks(self):
        self.assertEqual(zl.parse_config_report("garbage\nmore garbage"), {})

    def test_differences(self):
        actual = zl.parse_config_report(ZD_REPORT)
        same = {"print_width": 406, "media": "gap", "print_mode": "tear-off", "darkness": 10, "speed": 6,
                "sensor": "transmissive"}
        self.assertEqual(zl.spec_differences(same, actual), [])
        diffs = zl.spec_differences({"darkness": 15, "print_mode": "peel-off", "print_width": 609}, actual)
        self.assertEqual(diffs, ["Darkness is 10 (spec says 15)", "Print mode is tear-off (spec says peel-off)",
                                 "Print width is 406 (spec says 609)"])

    def test_check_printer_over_network(self):
        fake = FakePrinter(config_report=ZD_REPORT.encode())
        self.addCleanup(fake.close)
        p = zl.Printer(name="n", host="127.0.0.1", port=fake.port, expected={"darkness": 12, "media": "gap"})
        self.assertEqual(zl.check_printer(p), ([], ["Darkness is 10 (spec says 12)"]))
        p.expected = {}
        self.assertEqual(zl.check_printer(p), ([], None))  # no spec, no check


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.exe = os.path.join(self.dir, "Stripes.exe")
        with open(self.exe, "wb") as f:
            f.write(b"old version")

    def fake_github(self, files):
        def fetch(url, timeout=60):
            for suffix, data in files.items():
                if url.endswith(suffix):
                    return data
            raise OSError(f"404 {url}")
        return fetch

    def test_versions(self):
        self.assertEqual(zl.parse_version("v1.10.2"), (1, 10, 2))
        self.assertIsNone(zl.parse_version("dev-abc1234"))
        fetch = self.fake_github({"/latest/download/version.txt": b"v1.10.0\n"})
        self.assertEqual(zl.check_for_update("v1.9.9", fetch), "v1.10.0")  # not a text comparison
        self.assertIsNone(zl.check_for_update("v1.10.0", fetch))
        self.assertIsNone(zl.check_for_update("v2.0.0", fetch))
        self.assertIsNone(zl.check_for_update("dev-abc1234", fetch))  # development builds never update

    def test_install_swaps_the_exe(self):
        import hashlib
        new = b"new version"
        fetch = self.fake_github({"/download/v1.1.0/Stripes.exe": new,
                                  "/download/v1.1.0/Stripes.exe.sha256": hashlib.sha256(new).hexdigest().encode()})
        zl.install_update("v1.1.0", self.exe, fetch)
        with open(self.exe, "rb") as f:
            self.assertEqual(f.read(), new)
        olds = [n for n in os.listdir(self.dir) if n.startswith("Stripes.exe.old-")]
        self.assertEqual(len(olds), 1)  # the running copy, moved aside
        zl.remove_old_versions(self.exe)
        self.assertEqual(os.listdir(self.dir), ["Stripes.exe"])

    def test_damaged_download_leaves_current_copy(self):
        fetch = self.fake_github({"/download/v1.1.0/Stripes.exe": b"truncated",
                                  "/download/v1.1.0/Stripes.exe.sha256": b"0" * 64})
        with self.assertRaises(ValueError):
            zl.install_update("v1.1.0", self.exe, fetch)
        with open(self.exe, "rb") as f:
            self.assertEqual(f.read(), b"old version")
        self.assertEqual(os.listdir(self.dir), ["Stripes.exe"])

    def test_writable_folder(self):
        self.assertTrue(zl.folder_is_writable(self.dir))
        self.assertFalse(zl.folder_is_writable(os.path.join(self.dir, "missing")))


class DeploymentLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def log(self, computer="ARROW01", user="John"):
        return zl.DeploymentLog(self.dir, computer, user)

    def test_install_update_heartbeat_close(self):
        log = self.log()
        self.assertTrue(log.started("v1.2.0", r"C:\Users\John\AppData\Local\Programs\Stripes\Stripes.exe"))
        r = log.read()
        self.assertEqual((r["computer"], r["user"], r["version"], r["running"]), ("ARROW01", "John", "v1.2.0", True))
        self.assertEqual([e["event"] for e in r["events"]], ["Installed (v1.2.0)"])
        first_seen = r["first_seen"]

        log.started("v1.2.0", "x")  # plain restart: no new event
        log.write("Downloaded v1.3.0; finishes when Stripes restarts", pending_update="v1.3.0")
        log.started("v1.3.0", "x")
        r = log.read()
        self.assertEqual(r["first_seen"], first_seen)
        self.assertEqual((r["version"], r["pending_update"]), ("v1.3.0", None))
        self.assertEqual((r["last_update"]["from"], r["last_update"]["to"]), ("v1.2.0", "v1.3.0"))
        self.assertEqual([e["event"] for e in r["events"]][-1], "Updated v1.2.0 -> v1.3.0")

        self.assertTrue(zl.is_open_now(r))
        log.write(running=False, last_closed=zl._now())
        self.assertFalse(zl.is_open_now(log.read()))

    def test_stale_heartbeat_is_not_open(self):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(hours=2)).isoformat(sep=" ", timespec="seconds")
        self.assertFalse(zl.is_open_now({"running": True, "last_seen": old}))

    def test_repeated_update_errors_logged_once(self):
        log = self.log()
        log.update_failed("No answer")
        log.update_failed("No answer")
        log.update_failed("Something else")
        self.assertEqual(len(log.read()["events"]), 2)

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

    def test_no_status_folder_means_no_writes(self):
        log = zl.DeploymentLog(os.path.join(self.dir, "missing"), "PC", "U")
        self.assertFalse(log.started("v1.2.0", "x"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "missing")))

    def test_status_folder_sits_next_to_config(self):
        config = os.path.join(self.dir, "Stripes", "config")
        with mock.patch.dict(os.environ, {"STRIPES_STATUS": "", "STRIPES_CONFIG": config}):
            self.assertEqual(zl.status_folder(), os.path.join(self.dir, "Stripes", "status"))


if __name__ == "__main__":
    unittest.main()
