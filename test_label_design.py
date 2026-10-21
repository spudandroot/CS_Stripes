"""Tests for label_design. Run: python -m unittest -v"""

import os
import re
import tempfile
import unittest

import label_design as ld

TWO_BY_ONE = ld.Label(406, 203, 8)
THREE_BY_THREE = ld.Label(610, 610, 8)


def text(value, **kw):
    return ld.Element("text", value, **kw)


class BarcodeTests(unittest.TestCase):
    def test_code128_table_is_well_formed(self):
        self.assertEqual(len(ld._C128), 107)
        for i, pattern in enumerate(ld._C128[:-1]):
            self.assertEqual(sum(map(int, pattern)), 11, i)
        self.assertEqual(sum(map(int, ld._C128[-1])), 13)  # stop
        self.assertEqual(len(set(ld._C128)), 107)

    def test_code128_digits_use_subset_c(self):
        values = ld._code128_values("123456")
        self.assertEqual(values[:4], [105, 12, 34, 56])
        self.assertEqual(values[4], (105 + 1 * 12 + 2 * 34 + 3 * 56) % 103)
        self.assertEqual(values[-1], 106)

    def test_code128_mixed(self):
        values = ld._code128_values("AB12")  # short digit run at the end stays in B
        self.assertEqual(values[:5], [104, 33, 34, 17, 18])
        values = ld._code128_values("LOT123456")  # long run switches to C
        self.assertEqual(values[:4], [104, 44, 47, 52])
        self.assertEqual(values[4:8], [99, 12, 34, 56])
        modules = ld.barcode_modules("code128", "LOT123456")
        self.assertEqual(len(modules), 11 * (len(values) - 1) + 13)
        self.assertTrue(modules[0] and modules[-1])  # starts and ends with a bar

    def test_code39(self):
        for c, pattern in ld._C39.items():
            self.assertEqual((len(pattern), pattern.count("w")), (9, 3), c)
        self.assertEqual(len(ld.barcode_modules("code39", "abc")), 5 * 16 - 1)

    def test_ean_and_upc(self):
        self.assertEqual(ld.ean_check_digit("400638133393"), "1")
        self.assertEqual(ld.ean_check_digit("003600029145"), "2")
        self.assertEqual(len(ld.barcode_modules("ean13", "400638133393")), 95)
        self.assertEqual(len(ld.barcode_modules("upca", "03600029145")), 95)
        self.assertEqual(ld.barcode_human_text("upca", "03600029145"), "036000291452")

    def test_problems(self):
        self.assertIsNone(ld.barcode_problem("code128", "Lot #42-A"))
        self.assertIsNone(ld.barcode_problem("ean13", "4006381333931"))
        self.assertIsNone(ld.barcode_problem("upca", "036000291452"))
        self.assertIn("check digit", ld.barcode_problem("ean13", "4006381333930"))
        self.assertIn("12 digits", ld.barcode_problem("ean13", "123"))
        self.assertIn("Code 39", ld.barcode_problem("code39", "a_b"))
        self.assertIn("Code 128", ld.barcode_problem("code128", "café"))
        self.assertEqual(ld.barcode_problem("code128", ""), "Barcode is empty.")


class LayoutTests(unittest.TestCase):
    def assert_inside(self, design, label):
        m = label.margin
        for e in design.elements:
            self.assertGreaterEqual(e.x, m)
            self.assertGreaterEqual(e.y, m)
            self.assertLessEqual(e.x + e.w, label.width - m)
            self.assertLessEqual(e.y + e.h, label.height - m + 1)

    def test_text_lines_share_one_size(self):
        d = ld.Design([text("Navel Oranges"), text("Case 40 lb"), text("Lot 2026-117")])
        r = ld.render(d, TWO_BY_ONE)
        self.assertEqual(r.warnings + r.errors, [])
        self.assert_inside(d, TWO_BY_ONE)
        self.assertEqual(len({mk.text_h for mk in r.marks}), 1)

    def test_per_line_size_and_alignment(self):
        d = ld.Design([text("BIG", size_pt=28, align="left"), text("small", size_pt=9, align="right")])
        r = ld.render(d, THREE_BY_THREE)
        big, small = r.marks
        self.assertEqual(big.text_h, int(28 / 72 * 203.2))
        self.assertEqual(small.text_h, int(9 / 72 * 203.2))
        zpl = ld.build_zpl(r)
        self.assertIn(f"^A0N,{big.text_h},{big.text_h}^FB", zpl)
        self.assertRegex(zpl, r"\^FB\d+,1,0,L,0\^FH\^FDBIG")
        self.assertRegex(zpl, r"\^FB\d+,1,0,R,0\^FH\^FDsmall")

    def test_mixed_items_stack_without_overlap(self):
        d = ld.Design([text("Navel Oranges"), ld.Element("barcode", "LOT123456"),
                       ld.Element("qr", "https://example.com/lot/123456"), text("Packed 10/21/2026")])
        r = ld.render(d, THREE_BY_THREE)
        self.assertEqual(r.errors, [])
        self.assert_inside(d, THREE_BY_THREE)
        boxes = [(e.y, e.y + e.h) for e in d.elements]
        for (a_top, a_bottom), (b_top, _) in zip(boxes, boxes[1:]):
            self.assertLessEqual(a_bottom, b_top)
        kinds = [mk.kind for mk in r.marks]
        self.assertEqual(kinds, ["text", "bars", "text", "qr", "text"])

    def test_too_much_text_warns(self):
        d = ld.Design([text("Line", size_pt=40) for _ in range(5)])
        self.assertTrue(any("taller" in w or "edge" in w for w in ld.render(d, TWO_BY_ONE).warnings))

    def test_manual_layout_is_kept(self):
        d = ld.Design([ld.Element("qr", "ABC", x=10, y=10, w=150, h=150, align="left"),
                       text("Beside the QR", x=170, y=40, w=220, h=60)], auto_layout=False)
        r = ld.render(d, TWO_BY_ONE)
        self.assertEqual((d.elements[0].x, d.elements[1].x), (10, 170))
        qr, words = r.marks
        self.assertEqual((qr.x, qr.module), (10, 150 // 21))
        self.assertEqual(words.x, 170)


    def test_tighten_keeps_everything_in_place(self):
        d = ld.Design([text("Navel Oranges", size_pt=24, align="left"), ld.Element("barcode", "LOT123"),
                       ld.Element("qr", "ABC", align="right"), text("Fit me")])
        before = ld.render(d, THREE_BY_THREE)
        ld.tighten(d, before)
        d.auto_layout = False
        after = ld.render(d, THREE_BY_THREE)

        def visible(r):  # where things appear: text by its estimated drawn width, not its box
            out = []
            for m in r.marks:
                if m.kind == "text":
                    w = min(m.w, int(ld.text_em(m.text) * m.text_h * ld.WIDTH_SAFETY) + 4)
                    out.append((m.kind, ld._place(m.x, m.w, w, m.align), m.y, w, m.text_h))
                else:
                    out.append((m.kind, m.x, m.y, m.w, m.h, m.module))
            return out
        self.assertEqual(visible(after), visible(before))
        qr = d.elements[2]
        self.assertEqual((qr.w, qr.h), (before.marks[3].w, before.marks[3].h))  # box = the QR itself
        self.assertLess(d.elements[0].w, THREE_BY_THREE.width - 2 * THREE_BY_THREE.margin)


class ZplTests(unittest.TestCase):
    def test_no_printer_settings_sent(self):
        d = ld.Design([text("Hello"), ld.Element("barcode", "123456789012", symbology="ean13"),
                       ld.Element("qr", "HELLO")])
        zpl = ld.build_zpl(ld.render(d, THREE_BY_THREE), copies=3)
        self.assertTrue(zpl.startswith("^XA\n") and zpl.endswith("^PQ3\n^XZ\n"))
        for setting in ("^PW", "^LL", "^CI", "^MN", "^MT", "^MD", "~SD", "^PR", "^JUS", "^LH", "^LS"):
            self.assertNotIn(setting, zpl)

    def test_barcode_commands(self):
        cases = {
            "code128": ("Lot_7", r"\^BY\d+\^BCN,\d+,N,N,N,A\^FH\^FDLot_5F7\^FS"),
            "code39": ("ab-12", r"\^BY\d+,3\^B3N,N,\d+,N,N\^FH\^FDAB-12\^FS"),
            "ean13": ("4006381333931", r"\^BY\d+\^BEN,\d+,N,N\^FH\^FD400638133393\^FS"),
            "upca": ("03600029145", r"\^BY\d+\^BUN,\d+,N,N,N\^FH\^FD03600029145\^FS"),
        }
        for symbology, (value, pattern) in cases.items():
            d = ld.Design([ld.Element("barcode", value, symbology=symbology)])
            r = ld.render(d, THREE_BY_THREE)
            self.assertEqual(r.errors, [], symbology)
            zpl = ld.build_zpl(r)
            self.assertRegex(zpl, pattern)
            human = [mk for mk in r.marks if mk.kind == "text"][0].text
            self.assertEqual(human, ld.barcode_human_text(symbology, value))

    def test_qr_command(self):
        r = ld.render(ld.Design([ld.Element("qr", "https://example.com/a^b")]), THREE_BY_THREE)
        self.assertRegex(ld.build_zpl(r), r"\^BQN,2,\d+\^FH\^FDMA,https://example.com/a_5Eb\^FS")

    def test_invalid_barcode_is_an_error_not_printed(self):
        r = ld.render(ld.Design([ld.Element("barcode", "12", symbology="ean13")]), TWO_BY_ONE)
        self.assertEqual(len(r.errors), 1)
        self.assertNotIn("^BE", ld.build_zpl(r))

    def test_shift_and_border(self):
        r = ld.render(ld.Design([text("Hi")]), TWO_BY_ONE)
        zpl = ld.build_zpl(r, shift_x=10, shift_y=-5, border=(406, 203))
        self.assertIn("^FO10,0^GB406,203,3^FS", zpl)
        self.assertIn(f"^FO{r.marks[0].x + 10},{r.marks[0].y - 5}^A0N", zpl)


class PictureTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (200, 100), (0, 0, 0, 0))  # transparent background
        ImageDraw.Draw(img).rectangle((50, 25, 149, 74), fill=(0, 0, 0, 255))
        fd, self.path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        img.save(self.path)
        self.addCleanup(os.remove, self.path)

    def test_picture_prints_as_graphic(self):
        png = ld.load_picture(self.path)
        d = ld.Design([ld.Element("image", image_png=png)])
        r = ld.render(d, TWO_BY_ONE)
        mk = r.marks[0]
        self.assertAlmostEqual(mk.w / mk.h, 2.0, delta=0.05)  # aspect ratio kept
        zpl = ld.build_zpl(r)
        m = re.search(r"\^GFA,(\d+),(\d+),(\d+),([0-9A-F]+)\^FS", zpl)
        total, _, row_bytes, data = int(m[1]), int(m[2]), int(m[3]), m[4]
        self.assertEqual(row_bytes, (mk.w + 7) // 8)
        self.assertEqual((total, len(data)), (row_bytes * mk.h, total * 2))
        # transparent edges are white (00), the middle square is black (FF)
        self.assertTrue(data.startswith("00"))
        middle = data[(mk.h // 2) * row_bytes * 2:(mk.h // 2 + 1) * row_bytes * 2]
        self.assertIn("FF", middle)


class DesignStorageTests(unittest.TestCase):
    def test_round_trip(self):
        d = ld.Design([text("A", size_pt=12, align="left"), ld.Element("qr", "Q")], auto_layout=False)
        again = ld.Design.from_dict(d.to_dict())
        self.assertEqual(again, d)

    def test_old_recent_label_format(self):
        d = ld.Design.from_dict({"text": "One\nTwo\n", "size_pt": None, "align": "left", "emphasize": True})
        self.assertEqual([(e.kind, e.value, e.align) for e in d.elements],
                         [("text", "One", "left"), ("text", "Two", "left")])

    def test_from_text_trims_blank_edges(self):
        self.assertEqual([e.value for e in ld.Design.from_text("\n\nOne\n\nTwo\n\n").elements],
                         ["One", "", "Two"])


if __name__ == "__main__":
    unittest.main()
