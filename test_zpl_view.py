"""Tests for zpl_view (the ZPL tab's preview). Run: python -m unittest -v"""

import unittest

import label_design as ld
import zpl_view as zv

TWO_BY_ONE = ld.Label(406, 203, 8)
THREE_BY_THREE = ld.Label(610, 610, 8)


class ZplViewTests(unittest.TestCase):
    def test_stripes_own_zpl_previews_the_same(self):
        design = ld.Design([ld.Element("text", "Packed ^ 10_21"), ld.Element("barcode", "ABC-123"),
                            ld.Element("qr", "https://example.com")])
        rendered = ld.render(design, THREE_BY_THREE)
        view = zv.parse(ld.build_zpl(rendered), THREE_BY_THREE)
        self.assertEqual((view.errors, view.warnings, view.settings), ([], [], []))
        self.assertEqual([(m.kind, m.x, m.y, m.w, m.h, m.text) for m in rendered.marks],
                         [(m.kind, m.x, m.y, m.w, m.h, m.text) for m in view.marks])

    def test_hand_written_label(self):
        zpl = ("^XA\n^LH10,5\n^CF0,40\n^FO20,20^FDLot 26294^FS\n"
               "^FO20,80^GB300,4,4^FS\n"
               "^FT20,190^A0N,30,30^FDBest by^FS\n"
               "^BY2^FO200,100^BCN,50,Y,N,N^FD12345^FS\n^XZ")
        view = zv.parse(zpl, THREE_BY_THREE)
        self.assertEqual(view.errors, [])
        lot, line, best, bars, human = view.marks
        self.assertEqual((lot.kind, lot.x, lot.y, lot.text_h), ("text", 30, 25, 40))  # ^LH added
        self.assertEqual((line.kind, line.w, line.h, line.module), ("box", 300, 4, 4))
        self.assertEqual((best.y, best.text_h), (195 - 30, 30))  # ^FT is the baseline
        self.assertEqual((bars.kind, bars.y, bars.h, bars.module), ("bars", 105, 50, 2))
        self.assertEqual(human.text, "12345")  # interpretation line asked for (Y)
        self.assertEqual(len(view.fields), 4)

    def test_field_block_wraps_and_aligns(self):
        view = zv.parse("^XA^FO0,0^A0N,40,40^FB400,3,0,C^FDOne two three four five six^FS^XZ", THREE_BY_THREE)
        self.assertGreater(len(view.marks), 1)
        self.assertTrue(all(m.align == "center" and m.w == 400 for m in view.marks))

    def test_centered_field_block_shown_where_it_prints(self):
        view = zv.parse("^XA^FO100,0^A0N,200,200^FB400,1,0,C^FDg^FS^XZ", THREE_BY_THREE)
        self.assertEqual(view.marks[0].x, 70)  # 15% of 200 left: the printer's offset
        # a label from the other tabs is nudged to make up for it, so it shows centered again
        r = ld.render(ld.Design([ld.Element("text", "g", align="center")]), THREE_BY_THREE)
        shown = zv.parse(ld.build_zpl(r), THREE_BY_THREE).marks[0]
        self.assertEqual((shown.x, shown.w), (r.marks[0].x, r.marks[0].w))

    def test_settings_commands_are_flagged(self):
        view = zv.parse("^XA^MD10^PW406^FO0,0^FDx^FS^XZ~JUS^XA^XZ", TWO_BY_ONE)
        codes = [c for c, _ in view.settings]
        self.assertEqual(codes, ["^MD", "^PW", "~JU"])
        self.assertIn("Only the first label", view.warnings[0])

    def test_unknown_and_missing_xa(self):
        self.assertEqual(zv.parse("hello", TWO_BY_ONE).errors, ["ZPL labels start with ^XA and end with ^XZ."])
        view = zv.parse("^XA^FO0,0^ZZ1^FDx^FS^XZ", TWO_BY_ONE)
        self.assertTrue(any("^ZZ" in w for w in view.warnings))

    def test_field_hex_and_qr_prefix(self):
        view = zv.parse("^XA^FO0,0^FH^FDa_5Eb^FS^FO0,50^BQN,2,3^FDMA,hello^FS^XZ", THREE_BY_THREE)
        text, qr = view.marks
        self.assertEqual(text.text, "a^b")
        self.assertEqual(qr.pattern, ld.qr_matrix("hello"))
        self.assertEqual(qr.module, 3)

    def test_graphic_with_run_length_codes(self):
        # 16 dots wide (2 bytes a row), 3 rows: black row, white row (","), repeat of previous (":")
        self.assertEqual(zv.decode_gf_hex("FFFF,:", 2), [b"\xff\xff", b"\x00\x00", b"\x00\x00"])
        self.assertEqual(zv.decode_gf_hex("I0F", 2), [b"\x00\x0f"])  # I = repeat 3 times
        view = zv.parse("^XA^FO10,10^GFA,6,6,2,FFFF,:^FS^XZ", TWO_BY_ONE)
        image = view.marks[0]
        self.assertEqual((image.kind, image.w, image.h), ("image", 16, 3))
        self.assertEqual(image.bitmap.getpixel((0, 0)), 0)      # black prints
        self.assertEqual(image.bitmap.getpixel((0, 1)), 255)

    def test_origin_span_points_at_the_numbers(self):
        zpl = "^XA^FO 25,30,0^FDx^FS^XZ"
        f = zv.parse(zpl, TWO_BY_ONE).fields[0]
        self.assertEqual(f.origin, (25, 30))
        self.assertEqual(zpl[f.origin_span[0]:f.origin_span[1]], "25,30")
        self.assertEqual(zpl[f.source_span[0]:f.source_span[1]], "^FO 25,30,0^FDx^FS")

    def test_copies(self):
        self.assertEqual(zv.with_copies("^XA^FDx^FS^XZ", 3), "^XA^FDx^FS^PQ3\n^XZ")
        self.assertEqual(zv.with_copies("^XA^PQ2^XZ", 3), "^XA^PQ2^XZ")  # its own ^PQ wins
        self.assertEqual(zv.with_copies("^XA^XZ", 1), "^XA^XZ")


if __name__ == "__main__":
    unittest.main()
