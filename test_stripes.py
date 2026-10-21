"""Tests for the non-UI parts of stripes. Run: python -m unittest -v"""

import re
import socket
import sys
import threading
import types
import unittest
from unittest import mock

import stripes as zl

ZD420 = zl.Printer(name="Packing", host="127.0.0.1", model="ZD420", width_in=2, height_in=1, dpi=203)
ZT410 = zl.Printer(name="Pallets", host="127.0.0.1", model="ZT410", width_in=3, height_in=3, dpi=300)

HS_READY = b"\x02030,0,0,0203,000,0,0,0,000,0,0,0\x03\r\n\x02000,0,0,0,1,2,6,0,00000000,1,000\x03\r\n\x021234,0\x03\r\n"
HS_PROBLEMS = b"\x02030,1,1,0203,000,0,0,0,000,0,0,0\x03\r\n\x02000,0,1,0,1,2,6,0,00000000,1,000\x03\r\n\x021234,0\x03\r\n"


def fields_of(zpl):
    return re.findall(r"\^FO(\d+),(\d+)\^A0N,(\d+),(\d+)\^FB(\d+),1,0,([LCR]),0\^FH\^FD(.*?)\^FS", zpl)


class LayoutTests(unittest.TestCase):
    def test_label_sizes_in_dots(self):
        self.assertEqual(zl.label_dots(ZD420), (406, 203))
        self.assertEqual(zl.label_dots(ZT410), (914, 914))

    def test_auto_text_stays_inside_label(self):
        for printer in (ZD420, ZT410):
            for text in ("A", "Tomatoes", "Roma Tomatoes\nLot 2026-117\nPacked 10/21/2026",
                         "WWWWWWWWWWWWWWWWWWWWWWWWWWWWWW", "1\n2\n3\n4\n5\n6\n7\n8"):
                lay = zl.layout_label(text, printer)
                self.assertEqual(lay.warnings, [], (printer.name, text))
                last = lay.lines[-1]
                self.assertGreaterEqual(lay.lines[0].y, lay.margin)
                self.assertLessEqual(last.y + last.h, lay.height - lay.margin + 1)
                for ln in lay.lines:
                    self.assertLessEqual(zl.text_em(ln.text) * zl.WIDTH_SAFETY * ln.h,
                                         lay.inner_width + 0.5)

    def test_auto_lines_share_one_size_unless_emphasized(self):
        lay = zl.layout_label("Short\nA much longer second line", ZT410)
        self.assertEqual(lay.lines[0].h, lay.lines[1].h)
        lay = zl.layout_label("Big\nsmall\nsmall", ZT410, emphasize_first=True)
        self.assertGreater(lay.lines[0].h, lay.lines[1].h)

    def test_blank_edges_trimmed_and_inner_blank_kept(self):
        lay = zl.layout_label("\n\nOne\n\nTwo\n\n", ZD420)
        self.assertEqual([ln.text for ln in lay.lines], ["One", "", "Two"])

    def test_fixed_size_and_warnings(self):
        lay = zl.layout_label("Hi", ZD420, size_pt=36)
        self.assertEqual(lay.lines[0].h, int(36 / 72 * 8 * 25.4))
        self.assertEqual(lay.warnings, [])
        lay = zl.layout_label("This line is far too long for a small label", ZD420, size_pt=36)
        self.assertTrue(any("too wide" in w for w in lay.warnings))
        lay = zl.layout_label("1\n2\n3\n4", ZD420, size_pt=36)
        self.assertTrue(any("too tall" in w for w in lay.warnings))

    def test_empty(self):
        self.assertEqual(zl.layout_label("  \n\n", ZD420).lines, [])


class ZplTests(unittest.TestCase):
    def test_structure(self):
        zpl = zl.build_zpl(zl.layout_label("Line one\nLine two", ZD420), ZD420, "center", 3)
        self.assertTrue(zpl.startswith("^XA\n^CI28\n^PW406\n^LL203\n"))
        self.assertTrue(zpl.endswith("^PQ3\n^XZ\n"))
        f = fields_of(zpl)
        self.assertEqual([x[6] for x in f], ["Line one", "Line two"])
        self.assertTrue(all(x[5] == "C" and x[4] == str(406 - 2 * 12) for x in f))

    def test_alignment(self):
        lay = zl.layout_label("x", ZD420)
        for align, code in (("left", "L"), ("center", "C"), ("right", "R")):
            self.assertEqual(fields_of(zl.build_zpl(lay, ZD420, align))[0][5], code)

    def test_special_characters_escaped(self):
        zpl = zl.build_zpl(zl.layout_label("A^B~C_D 50°F", ZD420), ZD420)
        self.assertIn("^FDA_5EB_7EC_5FD 50°F^FS", zpl)
        self.assertEqual(zpl.count("^XA"), 1)

    def test_shift_and_border(self):
        p = zl.Printer(name="x", host="h", width_in=2, height_in=1, shift_x=10, shift_y=-5)
        lay = zl.layout_label("Hi", p)
        zpl = zl.build_zpl(lay, p, border=True)
        self.assertIn("^FO10,0^GB406,203,3^FS", zpl)
        x, y = map(int, fields_of(zpl)[0][:2])
        self.assertEqual((x, y), (lay.margin + 10, lay.lines[0].y - 5))

    def test_blank_lines_not_printed(self):
        zpl = zl.build_zpl(zl.layout_label("a\n\nb", ZD420), ZD420)
        self.assertEqual(len(fields_of(zpl)), 2)


class FakePrinter:
    """Listens like a Zebra on port 9100: records data, answers ~HS."""

    def __init__(self, hs_reply=HS_READY):
        self.hs_reply = hs_reply
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
                self.received.append(data)

    def close(self):
        self.sock.close()


class NetworkTests(unittest.TestCase):
    def test_print_over_network(self):
        fake = FakePrinter()
        self.addCleanup(fake.close)
        p = zl.Printer(name="n", host="127.0.0.1", port=fake.port)
        zpl = zl.build_zpl(zl.layout_label("Hello", p), p, copies=2)
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


class CompanyListTests(unittest.TestCase):
    def load(self, content, encoding="utf-8"):
        import tempfile, os
        d = tempfile.mkdtemp()
        path = os.path.join(d, "printers.json")
        with open(path, "w", encoding=encoding) as f:
            f.write(content)
        with mock.patch.dict(os.environ, {"STRIPES_COMPANY": path}):
            return zl.load_company_printers()

    def test_example_printers_json(self):
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "printers.example.json")
        with mock.patch.dict(os.environ, {"STRIPES_COMPANY": path}):
            printers, _, problems = zl.load_company_printers()
        self.assertEqual(problems, [])
        by_name = {p.name: p for p in printers}
        packing, pallets = by_name["Packing ZD420"], by_name["Pallets ZT410"]
        self.assertEqual((packing.host, zl.label_dots(packing)), ("192.0.2.10", (406, 203)))
        self.assertEqual((pallets.host, zl.label_dots(pallets)), ("192.0.2.20", (610, 610)))
        self.assertTrue(all(p.port == 9100 and not p.windows_name for p in printers))

    def test_notepad_bom_and_bad_entries(self):
        printers, _, problems = self.load(
            '{"printers": [{"name": "A", "host": "192.0.2.1"}, {"name": "B", "host": "192.0.2.2", "dpi": 200},'
            ' {"name": "C"}, {"name": "A", "host": "192.0.2.3"}, {"nme": "typo"}]}', encoding="utf-8-sig")
        self.assertEqual([p.name for p in printers], ["A"])
        self.assertEqual(len(problems), 4)

    def test_unreadable_file_reported(self):
        printers, _, problems = self.load("{not json")
        self.assertEqual(printers, [])
        self.assertIn("Could not read", problems[0])

    def test_missing_file_is_fine(self):
        import os
        with mock.patch.dict(os.environ, {"STRIPES_COMPANY": "/nonexistent/printers.json"}):
            self.assertEqual(zl.load_company_printers(), ([], None, []))

    def test_company_printers_win_name_clashes(self):
        company = [zl.Printer(name="Shop", host="192.0.2.214")]
        personal = [zl.Printer(name="Shop", host="1.2.3.4"), zl.Printer(name="Mine", host="5.6.7.8")]
        merged = zl.merge_printers(company, personal)
        self.assertEqual([(p.name, p.host) for p in merged], [("Shop", "192.0.2.214"), ("Mine", "5.6.7.8")])


if __name__ == "__main__":
    unittest.main()
