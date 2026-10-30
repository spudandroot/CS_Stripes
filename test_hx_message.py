"""Tests for hx_message: origin wording, the text builder, validation and plan merge, print
position and delay, message names, the read-back compare, fingerprint, clock command, test plans
and the calibration maths. Run: python -m unittest -v"""

import copy
import dataclasses
import hashlib
import os
import random
import tempfile
import time
import unittest
from datetime import date, datetime

import hx_fake
import hx_message as hm
from hx_core import Msg
from hx_model import (AMBER, GREEN, STEPS, Calibration, Company, Country, DesignItem, Difference, ExpectedState,
                      FailureInfo, Film, HxData, Identity, Ink, Line, LineChoice, Machine, MachineFilmAdjustment,
                      MessageDesign, MessageName, NotAllowed, PlanError, PrintSettings, Printer, PrintText,
                      PrintTextItem, Product, SendRequest, TestRanges, adjustment_id)

HERE = os.path.dirname(os.path.abspath(__file__))
T0 = 1_793_275_200.0             # 29 Oct 2026 12:00 UTC (08:00 EDT)
ADMIN_SID, SENDER_SID = hx_fake.ADMIN_SID, hx_fake.SENDER_SID
SENDER = Identity(user="EXAMPLE\\jsmith", computer="FLOOR01", sids=frozenset([SENDER_SID]), pid=1)
ADMIN = Identity(user="EXAMPLE\\akim", computer="FLOOR02", sids=frozenset([ADMIN_SID]), pid=2)
NOBODY = Identity(user="EXAMPLE\\akim", computer="FLOOR02", sids=frozenset(), pid=3)

GOLDEN_DELAY = [
    "<CRTMSG>EXCO1-PKO01-MN6D-L9</CRTMSG>",
    "<CTDAT><OBJID>10001</OBJID><POS>0,0</POS><MARG>0,0</MARG><FONTIND>2</FONTIND><SIZE>4</SIZE><PREF>Packed on "
    "</PREF><FRMT>M/D/y</FRMT><DAY>0</DAY><MON>0</MON><YEAR>0</YEAR></CTDAT>",
    "<CTEXT><OBJID>10002</OBJID><XRID>10001</XRID><YRID>10001</YRID><SXAS>1</SXAS><SYAS>0</SYAS><MARG>0,0</MARG>"
    "<FONTIND>2</FONTIND><SIZE>4</SIZE><TXT>Product of USA and Canada</TXT></CTEXT>",
    "<CTEXT><OBJID>10003</OBJID><XRID>10002</XRID><YRID>10002</YRID><SXAS>0</SXAS><SYAS>1</SYAS><MARG>0,0</MARG>"
    "<FONTIND>2</FONTIND><SIZE>4</SIZE><TXT>Lot 4417</TXT></CTEXT>",
    "<CPPARM><ENCST>0</ENCST><DELAY>480000</DELAY><PRNSPD>2400</PRNSPD><ROTAT>0</ROTAT><MIRR>0</MIRR><RESMOD>0"
    "</RESMOD><BOLD>1</BOLD><WDTH>1</WDTH><QUANT>1</QUANT><REVRS>0</REVRS><AUTREP>0</AUTREP></CPPARM>",
    "<SAVEMSG>9</SAVEMSG>",
]
GOLDEN_ENCODER = (["<CRTMSG>EXCO1-PKO01-5E11-W9</CRTMSG>"] + GOLDEN_DELAY[1:4]
                  + [GOLDEN_DELAY[4].replace("<ENCST>0</ENCST><DELAY>480000</DELAY>",
                                             "<ENCST>1</ENCST><DLYMM>348</DLYMM>")] + GOLDEN_DELAY[5:])


# merge_plan dates the bag text by this PC's own time zone (send.md §3.5), and T0 is already 30 Oct
# at UTC+12 and beyond. So these tests run on Eastern time wherever the zone can be set (not on
# Windows, whose CI runs on UTC).
_saved_tz = []


def setUpModule():
    if hasattr(time, "tzset"):
        _saved_tz.append(os.environ.get("TZ"))
        os.environ["TZ"] = "EST5EDT,M3.2.0,M11.1.0"
        time.tzset()


def tearDownModule():
    if _saved_tz:
        old = _saved_tz.pop()
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# --------------------------------------------------------------------------
# The FakeWorld seed, built by hand (protocol.md §16.2), plus a second line
# --------------------------------------------------------------------------

def make_company(**kw):
    values = dict(id="company", revision=1, time_server="time.example.local", time_server_fallback="192.0.2.123",
                  countries=[Country(name="USA", aliases=["US", "U.S.A.", "United States"]),
                             Country(name="Canada", aliases=["CAN"]), Country(name="Mexico", aliases=["MEX"])],
                  inks=[Ink(name="Example Black", cartridge="plastic", colour="black"),
                        Ink(name="Example White", cartridge="plastic", colour="white"),
                        Ink(name="Example Paper Black", cartridge="paper", colour="black")],
                  admin_group_sid=ADMIN_SID, sender_group_sid=SENDER_SID)
    values.update(kw)
    return Company(**values)


def seeded_print_text(**kw):
    values = dict(id="pt_0000000001", revision=1, name="Packed on + origin", code="PKO", status="active",
                  items=[PrintTextItem(id="i1", kind="packed_date", date_format="MM/DD/YY", before="Packed on "),
                         PrintTextItem(id="i2", kind="origin", placement="below"),
                         PrintTextItem(id="i3", kind="lot", before="Lot ", placement="right")])
    values.update(kw)
    return PrintText(**values)


def seed():
    """Line 1 with a delay machine (Bagger 1) and an encoder machine (Bagger 2), Line 2 with one delay
    machine; the seeded product, film EXCO1 and print text PKO."""
    lines = {"ln_0000000001": Line(id="ln_0000000001", revision=1, name="Line 1", code="L1", cartridge="plastic",
                                   notify_user="EXAMPLE\\jsmith"),
             "ln_0000000002": Line(id="ln_0000000002", revision=1, name="Line 2", code="L2", cartridge="plastic")}
    printers = {f"pr_000000000{n}": Printer(id=f"pr_000000000{n}", revision=1, name=f"Printer {n}",
                                            host=f"192.0.2.1{n - 1}", uid=1109, token="SCRAMBLED-EXAMPLE")
                for n in (1, 2, 3)}
    machines = {
        "mc_0000000001": Machine(id="mc_0000000001", revision=1, name="Line 1 Bagger 1", code="B1",
                                 line_id="ln_0000000001", printer_id="pr_0000000001", placement="delay",
                                 print_speed=2400),
        "mc_0000000002": Machine(id="mc_0000000002", revision=1, name="Line 1 Bagger 2", code="B2",
                                 line_id="ln_0000000001", printer_id="pr_0000000002", placement="encoder",
                                 print_speed=2400, head_distance_mm=412.0, encoder_wheel_mm=56.0, encoder_ppr=5000),
        "mc_0000000003": Machine(id="mc_0000000003", revision=1, name="Line 2 Bagger 1", code="B3",
                                 line_id="ln_0000000002", printer_id="pr_0000000003", placement="delay",
                                 print_speed=2400),
    }
    product = Product(id="pd_0000000001", revision=1, name="Example Co 2lb Onion", customer="Example Co")
    pt = seeded_print_text()
    film = Film(id="fm_0000000001", revision=1, product_id=product.id, code="EXCO1", artwork_revision="Rev C",
                status="active", side="back", cartridge="plastic", ink_colour="black", length_mm=610.0,
                position_mm=150.0, delay_ms=480.0, font=2, size=4, resolution=0, bold=1, width=1,
                print_text_ids=[pt.id], default_print_text_id=pt.id)
    return HxData(company=make_company(), lines=lines, machines=machines, printers=printers,
                  products={product.id: product}, films={film.id: film}, print_texts={pt.id: pt},
                  loaded_at=T0, reached_at=T0 - 60)


L1, L2 = "ln_0000000001", "ln_0000000002"
M1, M2, M3 = "mc_0000000001", "mc_0000000002", "mc_0000000003"
FILM, PT, PRODUCT = "fm_0000000001", "pt_0000000001", "pd_0000000001"


def choice(**kw):
    values = dict(line_id=L1, machine_ids=[M1, M2], product_id=PRODUCT, film_id=FILM, print_text_id=PT,
                  lot="4417", countries=["USA", "Canada"])
    values.update(kw)
    return LineChoice(**values)


def request(*choices, send_id="sd_0000000001"):
    return SendRequest(send_id=send_id, sender=SENDER.user, computer=SENDER.computer, created_at=T0,
                       lines=list(choices) or [choice()])


def plan_for(data=None, *choices):
    return hm.merge_plan(request(*choices), data or seed(), SENDER)


def golden(machine_id=M1, data=None):
    """The golden example's MachinePlan for one machine (§6.4)."""
    return next(p for p in plan_for(data).machines if p.machine.id == machine_id)


def problems_of(fn, *args):
    try:
        fn(*args)
    except PlanError as e:
        return [p.text for p in e.problems]
    return []


def texts(msgs):
    return [m.text for m in msgs]


def verbose_readback(built, *, date_text="10/29/26", read_at=T0):
    """A MessageDesign shaped like the fake's verbose render of built (protocol.md §15.4): SIZE one
    lower, the printer's defaults, BOUND, LANG and OBJTYP, a rendered date."""
    items = []
    for it in built.expected_items:
        s = dict(it.pairs)
        is_date = it.tag == "CTDAT"
        first = ([("FRMT", s["FRMT"]), ("AMPM", "\xff"), ("DAY", s["DAY"]), ("DWEEK", "0"), ("HOUR", "0"),
                  ("HORFLG", "1"), ("LANG", "17742"), ("MIN", "0"), ("MON", s["MON"]), ("SEC", "0"),
                  ("YEAR", s["YEAR"])] if is_date else [])
        pairs = first + [
            ("LANG", "4"), ("TXT", date_text if is_date else s["TXT"]), ("FONTIND", s["FONTIND"]),
            ("SIZE", str(int(s["SIZE"]) - 1)), ("POS", s.get("POS", "0,0")), ("BOUND", "0,0,350,36"),
            ("POSF", s.get("POSF", "")), ("PREF", s.get("PREF", "")), ("ROTAT", "0"), ("WCOEFF", "0"),
            ("GROUP", "0"), ("OBJTYP", "1" if is_date else "0"), ("COLR", "0"), ("XRID", s.get("XRID", "0")),
            ("YRID", s.get("YRID", "0")), ("SXAS", s.get("SXAS", "0")), ("SYAS", s.get("SYAS", "0")),
            ("OBJID", s["OBJID"]), ("RP", "0"), ("MARG", s["MARG"])]
        items.append(DesignItem(tag=it.tag, pairs=pairs))
    settings = dict(hx_fake.FAKE_DEFAULT_SETTINGS)
    settings.update(built.expected_settings)
    return MessageDesign(slot=built.slot, name=built.name, empty=False, items=items, settings=settings, width=256,
                         height=160, black_pixels=12600, preview_bmp=hx_fake.bmp_1bit([(0, 0, 350, 36)]),
                         read_at=read_at, tn="326548", xid=4)


def set_pair(design, n, field, value):
    """Item n (1-based) of design with field set to value (every occurrence), or added at the end."""
    item = design.items[n - 1]
    if any(k == field for k, _ in item.pairs):
        item.pairs = [(k, value if k == field else v) for k, v in item.pairs]
    else:
        item.pairs.append((field, value))


# --------------------------------------------------------------------------
# Origin wording (§2)
# --------------------------------------------------------------------------

class OriginTests(unittest.TestCase):
    def test_phrase(self):
        self.assertEqual(hm.origin_phrase(["USA"]), "Product of USA")
        self.assertEqual(hm.origin_phrase(["USA", "Canada"]), "Product of USA and Canada")
        self.assertEqual(hm.origin_phrase(["USA", "Canada", "Mexico"]), "Product of USA, Canada, and Mexico")
        self.assertEqual(hm.origin_phrase(["A", "B", "C", "D"]), "Product of A, B, C, and D")
        self.assertTrue(hm.origin_phrase(["Canada"]).startswith(hm.ORIGIN_PREFIX))
        with self.assertRaises(ValueError):
            hm.origin_phrase([])

    def test_words(self):
        company = make_company()
        found = {
            "Grown in Florida": ["Grown in"],
            "PRODUCT-OF the farm": ["Product of"],
            "MadeIn": ["Made in"],
            "Hécho en México": ["Hecho en", "Mexico"],
            "U.S.A. grown": ["USA", "US", "U.S.A."],
            "U S A": ["USA", "US", "U.S.A."],
            "united-states": ["United States"],
            "O R I G I N": ["Origin"],
            "Bring us joy": ["US"],
            "Can be recycled": ["CAN"],
            "Productofthefarm": ["Product of"],
        }
        for text, words in found.items():
            self.assertEqual(hm.origin_words_in(text, company), words, text)
        for text in ("Packed on ", "Lot ", "Best by ", "Original recipe", "Origins", "Canadian", "Fresh & Crisp",
                     "", "   ", "B1", "L1"):
            self.assertEqual(hm.origin_words_in(text, company), [], text)
        # A country that is no longer offered is still an origin word
        company.countries[2].active = False
        self.assertEqual(hm.origin_words_in("Hecho en México", company), ["Hecho en", "Mexico"])
        self.assertEqual(hm.origin_words_in("mex", company), ["MEX"])

    def test_tokens(self):
        self.assertEqual(hm.origin_tokens("U.S.A. grown"), (["u", "s", "a", "grown"], ["usa", "grown"], "usagrown"))
        self.assertEqual(hm.origin_tokens("Hécho_en"), (["hecho", "en"], ["hechoen"], "hechoen"))

    def test_characters(self):
        where = Msg("Item {n}", {"n": 1})
        (problem,) = hm.bag_text_problems("A<B>", where)
        self.assertEqual(problem.text, "{where} contains characters Stripes can't send to a printer: {codes}.")
        self.assertEqual(problem.values, {"where": where, "codes": "U+003C, U+003E"})
        self.assertNotIn("<", problem.english())
        self.assertEqual(hm.bag_text_problems("<<a>", where)[0].values["codes"], "U+003C, U+003E")
        self.assertEqual(hm.bag_text_problems("ñ°é&", where), [])
        self.assertEqual(hm.bag_text_problems(" ~\xa0\xff", where), [])
        self.assertEqual(hm.bag_text_problems("a\tb", where)[0].values["codes"], "U+0009")
        self.assertEqual(hm.bag_text_problems("5€", where)[0].values["codes"], "U+20AC")
        self.assertEqual(hm.bag_text_problems("a\nb\x7f", where)[0].values["codes"], "U+000A, U+007F")

    def test_change_text(self):
        self.assertEqual(hm.origin_change_text("Product of USA", "Product of USA and Canada"),
                         "USA → USA and Canada")
        self.assertEqual(hm.origin_change_text("USA (pre-printed)", "Product of USA"), "USA (pre-printed) → USA")
        self.assertEqual(hm.origin_change_text("Product of USA", "Product of USA"), "")
        self.assertEqual(hm.origin_change_text("", "Product of USA"), "USA")
        self.assertEqual(hm.origin_change_text("Product of USA", "USA (pre-printed)"), "USA → USA (pre-printed)")


ORIGIN_WORDS_TEXT = "{where} contains words about origin: {words}. Origin goes only in the origin line."


def origin_msg(where, words):
    return Msg(ORIGIN_WORDS_TEXT, {"where": where, "words": words})


def item_where(n):
    return Msg("Item {n}", {"n": n})


class JoinedOriginTests(unittest.TestCase):
    """Origin words made by pieces that are clean on their own: the text before or after an item
    with its value, and items printed next to each other (plan: no other text can carry origin
    words). Regression tests for the review of WP4."""

    def with_items(self, *extra, data=None):
        data = data or seed()
        data.print_texts[PT].items += list(extra)
        return data

    def test_free_text_completes_a_phrase(self):
        data = self.with_items(PrintTextItem(id="i4", kind="free_text", label="Farm?", max_len=20, before="Grown ",
                                             placement="right", gap=4))
        # Saving is fine: "Grown " alone is clean and the value isn't known yet
        self.assertEqual(hm.validate_print_text(data.print_texts[PT], data.company), [])
        problems = hm.validate_choice(choice(free_text={"i4": "in Florida"}), data, SENDER)
        self.assertEqual(problems, [origin_msg(Msg('The free text "{label}"', {"label": "Farm?"}), "Grown in")])
        with self.assertRaises(PlanError):
            hm.merge_plan(request(choice(free_text={"i4": "in Florida"})), data, SENDER)
        # Punctuation and run-together text are caught too; a clean value still sends
        for value in ("IN Florida", "-in Florida", "in"):
            self.assertIn(ORIGIN_WORDS_TEXT, texts(hm.validate_choice(choice(free_text={"i4": value}), data, SENDER)))
        plan = hm.merge_plan(request(choice(free_text={"i4": "Fresh"})), data, SENDER)
        self.assertEqual(hm.build_message(plan.machines[0], 9).expected_items[-1].value("TXT"), "Grown Fresh")

    def test_free_text_makes_a_second_product_of_line(self):
        data = seed()
        pt = data.print_texts[PT]
        pt.items.insert(0, PrintTextItem(id="i0", kind="free_text", label="Grade?", max_len=20, before="Product "))
        pt.items[1].placement = "below"
        self.assertEqual(hm.validate_print_text(pt, data.company), [])
        self.assertEqual(problems_of(hm.merge_plan, request(choice(free_text={"i0": "of Mars"})), data, SENDER),
                         [ORIGIN_WORDS_TEXT])
        # A plan changed after the merge can't be built, so origin_from_design never meets a second
        # "Product of" line made by Stripes
        plan = hm.merge_plan(request(choice(free_text={"i0": "No. 1"})), data, SENDER).machines[0]
        self.assertEqual(hm.origin_from_design(verbose_readback(hm.build_message(plan, 9))),
                         "Product of USA and Canada")
        plan.free_text["i0"] = "of Mars"
        with self.assertRaises(ValueError):
            hm.build_message(plan, 9)

    def test_fixed_item_with_its_text_around_it(self):
        company = make_company()
        for before, text, after, words in (("Product ", "of Mars", "", "Product of"), ("", "Made", " in Peru", "Made in"),
                                           ("Grown", "In", "", "Grown in"), ("Hecho", " en", "", "Hecho en"),
                                           ("O", "rig", "in", "Origin")):
            pt = seeded_print_text()
            pt.items.append(PrintTextItem(id="i4", kind="fixed", before=before, text=text, after=after,
                                          placement="below"))
            self.assertEqual(hm.validate_print_text(pt, company), [origin_msg(item_where(4), words)], text)
            self.assertEqual(hm.origin_conflicts(dataclasses.replace(seed(), print_texts={pt.id: pt}), company),
                             [Msg("Print text {name} now contains words about origin: {words}.",
                                  {"name": pt.name, "words": words})])
        # A piece with origin words on its own is reported once, for that piece
        pt = seeded_print_text()
        pt.items.append(PrintTextItem(id="i4", kind="fixed", before="Grown in ", text="Fresh", placement="below"))
        self.assertEqual(hm.validate_print_text(pt, company),
                         [origin_msg(Msg("The text before item {n}", {"n": 4}), "Grown in")])

    def test_codes_complete_a_phrase(self):
        data = seed()
        data.lines[L1].code = "IN"
        data.print_texts[PT].items.append(PrintTextItem(id="i4", kind="line_code", before="Made ", placement="below"))
        self.assertEqual(hm.validate_print_text(data.print_texts[PT], data.company), [])
        self.assertEqual(hm.validate_line(data.lines[L1], data), [])
        self.assertEqual(hm.validate_choice(choice(), data, SENDER), [origin_msg(item_where(4), "Made in")])
        with self.assertRaises(PlanError):
            hm.merge_plan(request(), data, SENDER)
        # Line 2's code is clean
        self.assertEqual(hm.validate_choice(choice(line_id=L2, machine_ids=[M3]), data, SENDER), [])
        # Each machine prints its own code: only Bagger 2's makes "Grown in"
        data = seed()
        data.machines[M2].code = "IN"
        data.print_texts[PT].items.append(PrintTextItem(id="i4", kind="machine_code", before="Grown ",
                                                        placement="below"))
        self.assertEqual(hm.validate_choice(choice(), data, SENDER), [origin_msg(item_where(4), "Grown in")])
        self.assertEqual(hm.validate_choice(choice(machine_ids=[M1]), data, SENDER), [])

    def test_items_next_to_each_other(self):
        company = make_company()

        def problems(*extra):
            pt = seeded_print_text()
            pt.items = pt.items + list(extra)
            return hm.validate_print_text(pt, company)
        made = PrintTextItem(id="i4", kind="fixed", text="Made", placement="below")
        # Side by side with a gap, run together, on the next line, and a date's text before it
        for second in (PrintTextItem(id="i5", kind="fixed", text="in Peru", placement="right", gap=4),
                       PrintTextItem(id="i5", kind="fixed", text="In", placement="right"),
                       PrintTextItem(id="i5", kind="fixed", text="in Peru", placement="below"),
                       PrintTextItem(id="i5", kind="best_by_date", date_format="MM/YY", offset=1, before="in ",
                                     placement="right", gap=4)):
            self.assertEqual(problems(made, second), [origin_msg(item_where(5), "Made in")], second)
        # An optional free text between them prints nothing when left empty
        note = PrintTextItem(id="i6", kind="free_text", label="Note?", max_len=10, required=False, placement="right")
        self.assertEqual(problems(made, note, PrintTextItem(id="i5", kind="fixed", text="in Peru", placement="right",
                                                            gap=4)),
                         [origin_msg(item_where(6), "Made in")])
        # A required free text, a date, a lot or the origin phrase between them keeps them apart
        for between in (dataclasses.replace(note, required=True),
                        PrintTextItem(id="i6", kind="packed_date", date_format="MM/DD/YY", placement="right"),
                        PrintTextItem(id="i6", kind="lot", placement="right", gap=4)):
            self.assertEqual(problems(made, between, PrintTextItem(id="i5", kind="fixed", text="in Peru",
                                                                   placement="right", gap=4)), [], between)
        pt = seeded_print_text()
        pt.items = [made, PrintTextItem(id="i2", kind="origin", placement="right", gap=4),
                    PrintTextItem(id="i3", kind="fixed", text="in bags", placement="right", gap=4)]
        self.assertEqual(hm.validate_print_text(pt, company), [])
        # and so does the phrase itself at Send, when it is joined with the text next to it
        data = seed()
        data.print_texts[PT].items = pt.items
        self.assertEqual(hm.validate_choice(choice(), data, SENDER), [])
        self.assertEqual(plan_for(data).machines[0].bag_text, "Made Product of USA and Canada in bags")
        # A print text that does this isn't offered at Send
        data = seed()
        data.print_texts[PT].items += [made, PrintTextItem(id="i5", kind="fixed", text="in Peru", placement="right",
                                                           gap=4)]
        self.assertEqual(hm.print_texts_for_film(data.films[FILM], data), [])
        # A new country makes words of items that were clean
        data = seed()
        data.print_texts[PT].items += [PrintTextItem(id="i4", kind="fixed", text="New", placement="below"),
                                       PrintTextItem(id="i5", kind="fixed", text="Zealand", placement="right", gap=4)]
        self.assertEqual(hm.origin_conflicts(data, data.company), [])
        company = copy.deepcopy(data.company)
        company.countries.append(Country(name="New Zealand", aliases=["NZ"]))
        self.assertEqual(hm.origin_conflicts(data, company),
                         [Msg("Print text {name} now contains words about origin: {words}.",
                              {"name": "Packed on + origin", "words": "New Zealand"})])

    def test_free_text_next_to_an_item(self):
        data = self.with_items(PrintTextItem(id="i4", kind="fixed", text="Made", placement="below"),
                               PrintTextItem(id="i5", kind="free_text", label="Where?", max_len=20, required=False,
                                             placement="right", gap=4))
        self.assertEqual(hm.validate_print_text(data.print_texts[PT], data.company), [])
        self.assertEqual(hm.validate_choice(choice(free_text={"i5": "in Peru"}), data, SENDER),
                         [origin_msg(Msg('The free text "{label}"', {"label": "Where?"}), "Made in")])
        self.assertEqual(hm.validate_choice(choice(free_text={"i5": "fresh"}), data, SENDER), [])
        self.assertEqual(hm.validate_choice(choice(), data, SENDER), [])

    def test_clean_designs_stay_clean(self):
        company = make_company()
        pt = seeded_print_text()
        pt.items = [PrintTextItem(id="i1", kind="packed_date", date_format="MM/DD/YY", before="Packed on "),
                    PrintTextItem(id="i2", kind="origin", placement="below"),
                    PrintTextItem(id="i3", kind="lot", before="Lot ", placement="right", gap=4),
                    PrintTextItem(id="i4", kind="best_by_date", date_format="MON DD YYYY", offset=10,
                                  before="Best by ", placement="below"),
                    PrintTextItem(id="i5", kind="fixed", text="Grade A", placement="right", gap=4),
                    PrintTextItem(id="i6", kind="line_code", before="Line ", placement="below"),
                    PrintTextItem(id="i7", kind="machine_code", placement="right", gap=4),
                    PrintTextItem(id="i8", kind="free_text", label="Note?", max_len=20, required=False,
                                  placement="right", gap=4),
                    PrintTextItem(id="i9", kind="fixed", text="Keep cool", placement="below")]
        self.assertEqual(hm.validate_print_text(pt, company), [])
        data = seed()
        data.print_texts = {pt.id: pt}
        self.assertEqual(hm.origin_conflicts(data, company), [])
        for note in ("", "Fresh & Crisp", "Original recipe"):
            self.assertEqual(hm.validate_choice(choice(free_text={"i8": note}), data, SENDER), [], note)
        plan = hm.merge_plan(request(choice(free_text={"i8": "Sweet"})), data, SENDER)
        self.assertEqual(plan.machines[0].bag_text, "Packed on 10/29/26 / Product of USA and Canada Lot 4417 / "
                                                    "Best by NOV 08 2026 Grade A / Line L1 B1 Sweet / Keep cool")
        hm.build_message(plan.machines[0], 9)

    def with_neighbour(self, item, *countries):
        """The seeded print text with item right after the origin item, in place of the lot."""
        data = seed()
        data.company.countries += [Country(name=name) for name in countries]
        pt = data.print_texts[PT]
        pt.items = [pt.items[0], pt.items[1], item]
        return data

    def test_text_right_of_the_origin_makes_another_country(self):
        # "Product of Niger" with "ia" right of it, no gap, prints "Product of Nigeria" while Stripes
        # records "Product of Niger" (send.md §2.2: the phrase is the only text that names a country)
        ia = PrintTextItem(id="i4", kind="fixed", text="ia", placement="right")
        data = self.with_neighbour(ia, "Niger", "Nigeria")
        # Saving can't tell: the countries are chosen at Send
        self.assertEqual(hm.validate_print_text(data.print_texts[PT], data.company), [])
        self.assertEqual(hm.validate_choice(choice(countries=["Niger"]), data, SENDER),
                         [origin_msg(item_where(3), "Nigeria")])
        self.assertEqual(problems_of(hm.merge_plan, request(choice(countries=["Niger"])), data, SENDER),
                         [ORIGIN_WORDS_TEXT])
        # A country already in the phrase, made again by the text right of it
        self.assertEqual(hm.validate_choice(choice(countries=["Nigeria", "Niger"]), data, SENDER),
                         [origin_msg(item_where(3), "Nigeria")])
        # Names of more than one word, and the same text after a gap
        data = self.with_neighbour(dataclasses.replace(ia, text="n Republic"), "Dominica", "Dominican Republic")
        self.assertEqual(hm.validate_choice(choice(countries=["Dominica"]), data, SENDER),
                         [origin_msg(item_where(3), "Dominican Republic")])
        data = self.with_neighbour(dataclasses.replace(ia, gap=4), "Niger", "Nigeria")
        plan = plan_for(data, choice(countries=["Niger"])).machines[0]
        self.assertEqual(plan.bag_text, "Packed on 10/29/26 / Product of Niger ia")

    def test_free_text_right_of_the_origin_makes_another_country(self):
        grade = PrintTextItem(id="i4", kind="free_text", label="Grade?", max_len=10, placement="right")
        data = self.with_neighbour(grade, "Niger", "Nigeria")
        self.assertEqual(hm.validate_print_text(data.print_texts[PT], data.company), [])
        where = Msg('The free text "{label}"', {"label": "Grade?"})
        for countries in (["Niger"], ["USA", "Niger"], ["Nigeria", "Niger"]):
            self.assertEqual(hm.validate_choice(choice(countries=countries, free_text={"i4": "ia"}), data, SENDER),
                             [origin_msg(where, "Nigeria")], countries)
            with self.assertRaises(PlanError):
                hm.merge_plan(request(choice(countries=countries, free_text={"i4": "ia"})), data, SENDER)
        self.assertEqual(hm.validate_choice(choice(countries=["Niger"], free_text={"i4": "No. 1"}), data, SENDER), [])
        # A plan changed after the merge can't be built: build_message knows only the chosen countries,
        # so it catches a chosen country made again
        plan = plan_for(data, choice(countries=["Nigeria", "Niger"], free_text={"i4": "No. 1"})).machines[0]
        self.assertEqual(hm.origin_from_design(verbose_readback(hm.build_message(plan, 9))),
                         "Product of Nigeria and Niger")
        plan.free_text["i4"] = "ia"
        with self.assertRaises(ValueError):
            hm.build_message(plan, 9)

    def test_text_joined_to_the_origin_without_a_new_word_still_sends(self):
        # The FakeWorld print text has the lot right of the origin with gap 0 (send.md §3.5)
        plan = plan_for().machines[0]
        self.assertEqual(plan.bag_text, "Packed on 10/29/26 / Product of USA and CanadaLot 4417")
        hm.build_message(plan, 9)
        # A lot, a code or punctuation next to the phrase makes no origin word
        for item in (PrintTextItem(id="i4", kind="machine_code", placement="right"),
                     PrintTextItem(id="i4", kind="fixed", text=", grown and packed by Example Co", placement="right"),
                     PrintTextItem(id="i4", kind="fixed", text="-grown", placement="right")):
            data = self.with_neighbour(item, "Niger", "Nigeria")
            self.assertEqual(hm.validate_choice(choice(countries=["Niger"]), data, SENDER), [], item)
            hm.build_message(plan_for(data, choice(countries=["Niger"])).machines[0], 9)


class BuilderTests(unittest.TestCase):
    def test_date_formats(self):
        day = date(2026, 10, 29)
        expected = {"MM/DD/YY": "10/29/26", "MM/DD/YYYY": "10/29/2026", "DD/MM/YY": "29/10/26",
                    "DD/MM/YYYY": "29/10/2026", "YYYY-MM-DD": "2026-10-29", "MM-DD-YY": "10-29-26",
                    "MMDDYY": "102926", "MON DD YYYY": "OCT 29 2026", "DD MON YY": "29 OCT 26",
                    "MON YYYY": "OCT 2026", "MM/YY": "10/26"}
        self.assertEqual(list(hm.DATE_FORMATS), list(expected))
        for key, shown in expected.items():
            self.assertEqual(hm.render_date(hm.DATE_FORMATS[key], day), shown, key)
        self.assertEqual(hm.render_date("Y/M/D", date(2027, 1, 31), add_months=1), "2027/02/28")
        self.assertEqual(hm.render_date("Y/M/D", date(2028, 1, 31), add_months=1), "2028/02/29")
        self.assertEqual(hm.render_date("Y/M/D", date(2028, 2, 29), add_years=1), "2029/02/28")
        self.assertEqual(hm.render_date("Y/M/D", date(2022, 4, 10), add_years=1, add_months=3, add_days=5),
                         "2023/07/15")
        self.assertEqual(hm.render_date("Y/M/D", date(2026, 12, 31), add_days=1), "2027/01/01")
        self.assertEqual(hm.render_date("Y/M/D", date(2026, 11, 30), add_months=14), "2028/01/30")
        self.assertEqual(hm.render_date("D.M.y", datetime(2026, 10, 29, 23, 59)), "29.10.26")
        with self.assertRaises(ValueError):
            hm.render_date("j", day)
        with self.assertRaises(ValueError):
            hm.render_date("M/D H", day)

    def placed(self, items, free_text=None):
        """design_parts of the golden plan with another print text (no origin item)."""
        plan = golden()
        plan.print_text = PrintText(id="pt_x", name="Test", code="TT", status="active", items=items)
        plan.free_text = free_text or {}
        parts, _ = hm.design_parts(plan)
        return [dict(children) for _, children in parts], parts

    def test_placement(self):
        items = [PrintTextItem(id="i1", kind="fixed", text="A", placement="below", gap=9),
                 PrintTextItem(id="i2", kind="fixed", text="B", placement="right", gap=2),
                 PrintTextItem(id="i3", kind="fixed", text="C", placement="below", gap=3),
                 PrintTextItem(id="i4", kind="fixed", text="D", placement="right"),
                 PrintTextItem(id="i5", kind="fixed", text="E", placement="below")]
        got, parts = self.placed(items)
        self.assertEqual([d["OBJID"] for d in got], ["10001", "10002", "10003", "10004", "10005"])
        # The first item is absolute; its placement and gap are ignored
        self.assertEqual(parts[0], ("CTEXT", [("OBJID", "10001"), ("POS", "0,0"), ("MARG", "0,0"), ("FONTIND", "2"),
                                              ("SIZE", "4"), ("TXT", "A")]))
        rel = lambda d: (d["XRID"], d["YRID"], d["SXAS"], d["SYAS"], d["MARG"])
        self.assertEqual(rel(got[1]), ("10001", "10001", "0", "1", "2,0"))     # right of item 1
        self.assertEqual(rel(got[2]), ("10001", "10002", "1", "0", "0,3"))     # below, lined up with item 1
        self.assertEqual(rel(got[3]), ("10003", "10003", "0", "1", "0,0"))     # right of item 3
        self.assertEqual(rel(got[4]), ("10003", "10004", "1", "0", "0,0"))     # below, at item 4's line start
        for d in got[1:]:
            self.assertNotIn("POS", d)
        self.assertEqual(list(dict(parts[1][1])), ["OBJID", "XRID", "YRID", "SXAS", "SYAS", "MARG", "FONTIND",
                                                   "SIZE", "TXT"])

    def test_optional_free_text_left_out(self):
        items = [PrintTextItem(id="i1", kind="fixed", text="A"),
                 PrintTextItem(id="i2", kind="free_text", label="Note?", max_len=10, required=False),
                 PrintTextItem(id="i3", kind="fixed", text="C", placement="right")]
        got, _ = self.placed(items)
        self.assertEqual([(d["OBJID"], d["TXT"]) for d in got], [("10001", "A"), ("10002", "C")])
        self.assertEqual(got[1]["XRID"], "10001")
        got, _ = self.placed(items, {"i2": " x "})
        self.assertEqual([d["TXT"] for d in got], ["A", "x", "C"])
        self.assertEqual(got[2]["XRID"], "10002")

    def test_item_kinds(self):
        plan = golden()
        plan.print_text = PrintText(id="pt_x", name="Test", code="TT", status="active", items=[
            PrintTextItem(id="i1", kind="machine_code", before="M", after="."),
            PrintTextItem(id="i2", kind="line_code", placement="right", gap=1),
            PrintTextItem(id="i3", kind="best_by_date", date_format="DD MON YY", offset=14, before="Best by ",
                          after=" or sooner", placement="right"),
            PrintTextItem(id="i4", kind="best_by_date", date_format="MON YYYY", offset=2, offset_unit="months",
                          placement="below"),
            PrintTextItem(id="i5", kind="best_by_date", date_format="MM/YY", offset=1, offset_unit="years",
                          placement="right")])
        parts, _ = hm.design_parts(plan)
        self.assertEqual(dict(parts[0][1])["TXT"], "MB1.")
        self.assertEqual(dict(parts[1][1])["TXT"], "L1")
        third = parts[2][1]
        self.assertEqual(third[-6:], [("PREF", "Best by "), ("POSF", " or sooner"), ("FRMT", "D m y"), ("DAY", "14"),
                                      ("MON", "0"), ("YEAR", "0")])
        self.assertEqual(dict(parts[3][1])["MON"], "2")
        self.assertEqual((dict(parts[4][1])["YEAR"], dict(parts[4][1])["DAY"]), ("1", "0"))
        self.assertNotIn("PREF", dict(parts[3][1]))
        self.assertEqual(hm._plan_bag_text(plan, date(2026, 10, 29)),
                         "MB1. L1Best by 12 NOV 26 or sooner / DEC 202610/27")

    def test_print_text_rules(self):
        company = make_company()
        self.assertEqual(hm.validate_print_text(seeded_print_text(), company), [])

        def item(**kw):
            values = dict(id="i9", kind="fixed", text="Fresh")
            values.update(kw)
            return PrintTextItem(**values)

        def with_items(*extra):
            pt = seeded_print_text()
            pt.items = pt.items + list(extra)
            return pt
        cases = [
            (seeded_print_text(name=""), "The name must be 1 to {n} characters."),
            (seeded_print_text(name="x" * 61), "The name must be 1 to {n} characters."),
            (seeded_print_text(code="pko"), "The print text code must be 1 to 3 capital letters or digits."),
            (seeded_print_text(code="PKOX"), "The print text code must be 1 to 3 capital letters or digits."),
            (seeded_print_text(code="TST"), "TST is kept for test prints."),
            (seeded_print_text(status="draft"), "The status isn't valid."),
            (seeded_print_text(items=[]), "The print text has no items."),
            (seeded_print_text(items=[item(id=f"i{n}") for n in range(100)]),
             "A print text can have at most {n} items."),
            (with_items(item(id="i1")), "Item ids must be unique."),
            (with_items(item(id="x9")), "Item ids must be unique."),
            (with_items(item(kind="barcode")), "Item {n} has an unknown kind."),
            (with_items(item(kind="origin")), "A print text can have only one origin item."),
            (with_items(item(placement="left")), "Item {n} must be placed right of or below the previous item."),
            (with_items(item(gap=51)), "The gap before item {n} must be 0 to {max}."),
            (with_items(item(gap=-1)), "The gap before item {n} must be 0 to {max}."),
            (with_items(item(before="Grown in ")), "{where} contains words about origin: {words}. Origin goes only in "
                                                   "the origin line."),
            (with_items(item(after="<")), "{where} contains characters Stripes can't send to a printer: {codes}."),
            (with_items(item(text="Made in USA")), "{where} contains words about origin: {words}. Origin goes only "
                                                   "in the origin line."),
            (seeded_print_text(items=[PrintTextItem(id="i1", kind="origin", after=" only")]),
             "The origin item can't have text before or after it."),
            (with_items(item(text="")), "Item {n} needs its fixed text."),
            (with_items(item(kind="packed_date", date_format="")), "Item {n} needs a date format."),
            (with_items(item(kind="packed_date", date_format="Y")), "Item {n} needs a date format."),
            (with_items(item(kind="packed_date", date_format="MM/YY", before="x" * 101)),
             "{where} is too long: at most {n} characters."),
            (with_items(item(kind="packed_date", date_format="MM/YY", offset=1)),
             "The packed-on date can't have an offset."),
            (with_items(item(kind="best_by_date", date_format="MM/YY", offset=0)),
             "The best-by offset of item {n} must be 1 to 999 days."),
            (with_items(item(kind="best_by_date", date_format="MM/YY", offset=1000, offset_unit="months")),
             "The best-by offset of item {n} must be 1 to 999 months."),
            (with_items(item(kind="best_by_date", date_format="MM/YY", offset=100, offset_unit="years")),
             "The best-by offset of item {n} must be 1 to 99 years."),
            (with_items(item(kind="free_text", label=" ", max_len=10)), "Item {n} needs a question for the sender."),
            (with_items(item(kind="free_text", label="Note?", max_len=0)),
             "The free text of item {n} must allow 1 to {max} characters."),
            (with_items(item(kind="free_text", label="Note?", max_len=31)),
             "The free text of item {n} must allow 1 to {max} characters."),
            (with_items(item(text="x" * 251)), "{where} is too long: at most {n} characters."),
            (with_items(item(kind="lot", before="x" * 243)), "{where} is too long: at most {n} characters."),
            (with_items(item(kind="machine_code", before="x" * 248)), "{where} is too long: at most {n} characters."),
        ]
        for pt, text in cases:
            problems = hm.validate_print_text(pt, company)
            self.assertIn(text, texts(problems), (pt.items[-1] if pt.items else pt, problems))
        # The values say where
        (p,) = [p for p in hm.validate_print_text(with_items(item(before="Grown in ")), company)]
        self.assertEqual(p.values["where"], Msg("The text before item {n}", {"n": 4}))
        self.assertEqual(p.values["words"], "Grown in")
        (p,) = hm.validate_print_text(with_items(item(text="x" * 251)), company)
        self.assertEqual(p.values, {"where": Msg("Item {n}", {"n": 4}), "n": 250})
        # Limits that just fit
        self.assertEqual(hm.validate_print_text(with_items(item(text="x" * 250), item(id="i8", kind="lot",
                                                                                      before="x" * 242)), company), [])
        # The origin item must fit with every active country
        many = make_company(countries=[Country(name=f"Country{n:02d}") for n in range(30)])
        self.assertIn("{where} is too long: at most {n} characters.", texts(hm.validate_print_text(seeded_print_text(),
                                                                                                    many)))

    def test_bag_text(self):
        self.assertEqual(golden().bag_text, "Packed on 10/29/26 / Product of USA and CanadaLot 4417")
        data = seed()
        data.print_texts[PT].items[2].gap = 4
        self.assertEqual(golden(data=data).bag_text, "Packed on 10/29/26 / Product of USA and Canada Lot 4417")


# --------------------------------------------------------------------------
# Validation and plan merge (§4)
# --------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def test_merge(self):
        data = seed()
        data.adjustments[adjustment_id(M1, FILM)] = MachineFilmAdjustment(
            id=adjustment_id(M1, FILM), machine_id=M1, film_id=FILM, delay_ms=20.0)
        req = request(choice(lot=" 4417 "))
        plan = hm.merge_plan(req, data, SENDER)
        self.assertEqual((plan.send_id, plan.request, plan.from_local_copy, plan.data_as_of),
                         (req.send_id, req, False, T0 - 60))
        self.assertEqual([p.machine.id for p in plan.machines], [M1, M2])
        delay, encoder = plan.machines
        for p in plan.machines:
            for name, record in (("machine", data.machines[p.machine.id]), ("line", data.lines[L1]),
                                 ("printer", data.printers[p.machine.printer_id]), ("product", data.products[PRODUCT]),
                                 ("film", data.films[FILM]), ("print_text", data.print_texts[PT])):
                self.assertEqual(getattr(p, name), record, name)
                self.assertIsNot(getattr(p, name), record, name)
            self.assertEqual(p.lot, "4417")
            self.assertEqual(p.countries, ["USA", "Canada"])
            self.assertEqual(p.free_text, {})
            self.assertEqual(p.origin_line, "Product of USA and Canada")
            self.assertEqual(p.bag_text, "Packed on 10/29/26 / Product of USA and CanadaLot 4417")
            self.assertIsNone(p.test_step)
        self.assertEqual(delay.adjustment, data.adjustments[adjustment_id(M1, FILM)])
        self.assertIsNot(delay.adjustment, data.adjustments[adjustment_id(M1, FILM)])
        self.assertIsNone(encoder.adjustment)
        self.assertEqual(delay.settings, PrintSettings(encoder=False, delay_us=500000, speed=2400))
        self.assertEqual(encoder.settings, PrintSettings(encoder=True, position_mm=348, speed=2400))
        self.assertEqual(delay.position.text, "480 + 20 = 500 ms = 500000 µs")
        self.assertEqual(encoder.position.text, "(150 - 412 + 0) mod 610 = 348 mm")
        self.assertEqual(delay.warnings, [])
        self.assertEqual(texts(encoder.warnings),
                         ["Print length unknown: Stripes can't check that the print ends before the film stops."])
        # Deterministic
        self.assertEqual(hm.merge_plan(req, data, SENDER), plan)
        # The local copy
        data.source = "local"
        self.assertTrue(hm.merge_plan(req, data, SENDER).from_local_copy)

    def test_merge_free_text(self):
        data = seed()
        data.print_texts[PT].items += [
            PrintTextItem(id="i4", kind="free_text", label="Grade?", max_len=10, placement="right", gap=2),
            PrintTextItem(id="i5", kind="free_text", label="Note?", max_len=10, required=False, placement="right")]
        p = hm.merge_plan(request(choice(free_text={"i4": "  No. 1 "})), data, SENDER).machines[0]
        self.assertEqual(p.free_text, {"i4": "No. 1"})
        self.assertEqual(p.bag_text, "Packed on 10/29/26 / Product of USA and CanadaLot 4417 No. 1")

    def test_preprinted(self):
        data = seed()
        film = data.films[FILM]
        film.origin_preprinted, film.preprinted_origin = True, "USA"
        pt = seeded_print_text(id="pt_0000000002", name="Packed on", code="PK",
                               items=[seeded_print_text().items[0]])
        data.print_texts[pt.id] = pt
        film.print_text_ids, film.default_print_text_id = [pt.id], pt.id
        p = hm.merge_plan(request(choice(print_text_id=pt.id, countries=["Atlantis"])), data, SENDER).machines[0]
        self.assertEqual(p.origin_line, "USA (pre-printed)")
        self.assertEqual(p.countries, [])
        self.assertEqual(p.lot, "")
        self.assertEqual(p.bag_text, "Packed on 10/29/26")
        self.assertEqual(hm.origin_from_design(verbose_readback(hm.build_message(p, 9))), "")
        # The print text with an origin item can't be used with it
        problems = problems_of(hm.merge_plan, request(choice()), data, SENDER)
        self.assertIn("Print text {print_text} can't be used with film {film}.", problems)

    def test_not_a_sender(self):
        with self.assertRaises(NotAllowed) as cm:
            hm.merge_plan(request(), seed(), NOBODY)
        self.assertEqual(str(cm.exception), "Only Stripes MapleJet Senders can send.")
        with self.assertRaises(NotAllowed):
            hm.merge_plan(request(), seed(), ADMIN)

    def test_problems(self):
        def check(text, change=None, *, choices=None):
            data = seed()
            choices = choices or [choice()]
            if change:
                change(data, choices)
            with self.assertRaises(PlanError) as cm:
                hm.merge_plan(request(*choices), data, SENDER)
            self.assertIn(text, texts(cm.exception.problems), text)
            self.assertEqual(cm.exception.msg, cm.exception.problems[0])
            return cm.exception

        def setattr_(path, value):
            def change(data, choices):
                kind, rid, field = path
                setattr(getattr(data, kind)[rid], field, value)
            return change

        def edit(**kw):
            def change(data, choices):
                for k, v in kw.items():
                    setattr(choices[0], k, v)
            return change

        with self.assertRaises(PlanError) as cm:
            hm.merge_plan(SendRequest(send_id="sd_1", sender=SENDER.user, computer="FLOOR01", created_at=T0, lines=[]),
                          seed(), SENDER)
        self.assertEqual(texts(cm.exception.problems), ["Choose at least one line."])
        check("{line} is in this Send twice.", choices=[choice(), choice(machine_ids=[M1])])
        check("{machine} is in this Send twice.", choices=[choice(), choice(line_id=L2, machine_ids=[M1])])
        check("{machine} and {other} use the same printer.", setattr_(("machines", M3, "printer_id"), "pr_0000000001"),
              choices=[choice(), choice(line_id=L2, machine_ids=[M3])])
        # §4.3, in order
        check("A line in this Send no longer exists or isn't active.", edit(line_id="ln_missing"))
        check("A line in this Send no longer exists or isn't active.", setattr_(("lines", L1, "active"), False))
        check("Tick at least one machine on {line}.", edit(machine_ids=[]))
        check("{machine} isn't an active machine on {line}.", edit(machine_ids=[M3]))
        check("{machine} isn't an active machine on {line}.", setattr_(("machines", M2, "active"), False))
        check("{machine} isn't an active machine on {line}.", edit(machine_ids=["mc_missing"]))
        check("{machine} is in this Send twice.", edit(machine_ids=[M1, M1]))
        check("Choose a product for {line}.", edit(product_id=""))
        check("The product chosen for {line} isn't active.", setattr_(("products", PRODUCT, "active"), False))
        check("The product chosen for {line} isn't active.", edit(product_id="pd_missing"))
        check("Choose a film for {line}.", edit(film_id=""))
        film_text = ("Film {film} can't be sent to {line}: it must be active, belong to the product and use the "
                     "line's cartridge type.")
        e = check(film_text, setattr_(("films", FILM, "status"), "development"))
        self.assertIn(Msg(film_text, {"film": "EXCO1 · Rev C", "line": "Line 1"}), e.problems)
        check(film_text, setattr_(("films", FILM, "cartridge"), "paper"))
        check(film_text, setattr_(("films", FILM, "product_id"), "pd_other"))
        check("Choose a print text for {line}.", edit(print_text_id=""))
        check("Print text {print_text} can't be used with film {film}.",
              setattr_(("print_texts", PT, "status"), "inactive"))
        check("Print text {print_text} can't be used with film {film}.", edit(print_text_id="pt_missing"))
        check("Enter the lot for {line}.", edit(lot="  "))
        e = check("The lot for {line} must be {min} to {max} digits.", edit(lot="123"))
        self.assertIn(Msg("The lot for {line} must be {min} to {max} digits.", {"line": "Line 1", "min": 4, "max": 8}),
                      e.problems)
        check("The lot for {line} must be {min} to {max} digits.", edit(lot="44a7"))
        check("The lot for {line} must be {min} to {max} digits.", edit(lot="123456789"))
        check("Choose at least one country of origin for {line}.", edit(countries=[]))
        check("{country} isn't on the country list.", edit(countries=["Atlantis"]))
        check("{country} isn't on the country list.", edit(countries=["usa"]))
        check("{country} is chosen twice.", edit(countries=["USA", "USA"]))

        def inactive_mexico(data, choices):
            data.company.countries[2].active = False
            choices[0].countries = ["USA", "Mexico"]
        check("{country} isn't on the country list.", inactive_mexico)

        def free(value, required=True):
            def change(data, choices):
                data.print_texts[PT].items.append(PrintTextItem(id="i4", kind="free_text", label="Grade?", max_len=10,
                                                                required=required, placement="right"))
                choices[0].free_text = {"i4": value}
            return change
        e = check('Fill in "{label}" for {line}.', free("   "))
        self.assertIn(Msg('Fill in "{label}" for {line}.', {"label": "Grade?", "line": "Line 1"}), e.problems)
        check('"{label}" can be at most {n} characters.', free("x" * 11))
        e = check("{where} contains words about origin: {words}. Origin goes only in the origin line.",
                  free("Grown in US"))
        (origin,) = [p for p in e.problems if p.text.startswith("{where} contains words")]
        self.assertEqual(origin.values, {"where": Msg('The free text "{label}"', {"label": "Grade?"}),
                                         "words": "Grown in, US"})
        self.assertNotIn("Grown in US", repr(e.problems))       # the sender's text is never in a Msg
        check("{where} contains characters Stripes can't send to a printer: {codes}.", free("A<1>"))
        # 7. The records again
        check("{where} contains words about origin: {words}. Origin goes only in the origin line.",
              setattr_(("machines", M1, "code"), "US"))
        check("The cartridge type isn't valid.", setattr_(("lines", L1, "cartridge"), "glass"))
        # 8. Per machine
        check("The printer of {machine} isn't set up: it is missing, not active or has no saved password.",
              setattr_(("printers", "pr_0000000001", "token"), ""))
        check("The printer of {machine} isn't set up: it is missing, not active or has no saved password.",
              setattr_(("printers", "pr_0000000001", "active"), False))
        check("{machine} has no print speed. An administrator must set it.",
              setattr_(("machines", M1, "print_speed"), None))
        check("{machine} isn't calibrated: it has no head distance.",
              setattr_(("machines", M2, "head_distance_mm"), None))
        check("Film {film} has no length, which encoder machines need.", setattr_(("films", FILM, "length_mm"), None))
        check("Film {film} has no print position, which encoder machines need.",
              setattr_(("films", FILM, "position_mm"), None))
        check("Film {film} has no delay, which delay machines need.", setattr_(("films", FILM, "delay_ms"), None))

        def big_adjustment(data, choices):
            data.adjustments[adjustment_id(M1, FILM)] = MachineFilmAdjustment(
                id=adjustment_id(M1, FILM), machine_id=M1, film_id=FILM, delay_ms=-500.0)
        e = check("The delay works out at {ms} ms, outside 0 to 120000 ms.", big_adjustment)
        self.assertIn(Msg("The delay works out at {ms} ms, outside 0 to 120000 ms.", {"ms": "-20"}), e.problems)
        # Every problem is listed once
        e = check("Film {film} has no length, which encoder machines need.", setattr_(("films", FILM, "length_mm"),
                                                                                     None), choices=[choice()])
        self.assertEqual(len(e.problems), len(set(map(repr, e.problems))))

    def test_validate_choice(self):
        data = seed()
        self.assertEqual(hm.validate_choice(choice(), data, SENDER), [])
        self.assertEqual(hm.validate_choice(choice(line_id=L2, machine_ids=[M3]), data, NOBODY), [])

    def test_offers(self):
        data = seed()
        base = data.films[FILM]
        extra = {
            "fm_0000000002": dataclasses.replace(base, id="fm_0000000002", code="EXCO2", artwork_revision="Rev D"),
            "fm_0000000003": dataclasses.replace(base, id="fm_0000000003", code="EXCO3", status="inactive"),
            "fm_0000000004": dataclasses.replace(base, id="fm_0000000004", code="EXCO4", product_id="pd_other"),
            "fm_0000000005": dataclasses.replace(base, id="fm_0000000005", code="EXCO5", cartridge="paper"),
            "fm_0000000006": dataclasses.replace(base, id="fm_0000000006", code="EXCO0", artwork_revision="Rev C"),
        }
        data.films.update(extra)
        got = hm.films_for_line(data.lines[L1], PRODUCT, data)
        self.assertEqual([f.code for f in got], ["EXCO0", "EXCO1", "EXCO2"])
        # Print texts: the film's order, active ones, the origin rule
        no_origin = seeded_print_text(id="pt_0000000002", name="Packed on", code="PK", items=[
            seeded_print_text().items[0]])
        second = seeded_print_text(id="pt_0000000003", name="Second", code="SC")
        inactive = seeded_print_text(id="pt_0000000004", name="Old", code="OLD", status="inactive")
        broken = seeded_print_text(id="pt_0000000005", name="Broken", code="BR")
        broken.items[0].before = "Grown in "
        for pt in (no_origin, second, inactive, broken):
            data.print_texts[pt.id] = pt
        film = data.films[FILM]
        film.print_text_ids = [second.id, no_origin.id, inactive.id, broken.id, PT, "pt_missing"]
        self.assertEqual([p.id for p in hm.print_texts_for_film(film, data)], [second.id, PT])
        film.origin_preprinted, film.preprinted_origin = True, "USA"
        self.assertEqual([p.id for p in hm.print_texts_for_film(film, data)], [no_origin.id])

    def test_conflicts(self):
        data = seed()
        self.assertEqual(hm.origin_conflicts(data, data.company), [])
        company = copy.deepcopy(data.company)
        company.countries[0].aliases.append("LOT")
        company.countries[1].aliases.append("L1")
        conflicts = hm.origin_conflicts(data, company)
        self.assertEqual(conflicts, [
            Msg("Print text {name} now contains words about origin: {words}.",
                {"name": "Packed on + origin", "words": "LOT"}),
            Msg("The code of {name} now contains words about origin: {words}.", {"name": "Line 1", "words": "L1"})])
        data.company = company
        problems = problems_of(hm.merge_plan, request(), data, SENDER)
        self.assertIn("{where} contains words about origin: {words}. Origin goes only in the origin line.", problems)
        self.assertIn("Print text {print_text} can't be used with film {film}.", problems)

    def test_restricted(self):
        data = seed()
        req = request(choice(), choice(line_id=L2, machine_ids=[M3]))
        plan = hm.merge_plan(req, data, SENDER)
        self.assertEqual(len(plan.machines), 3)
        data.source = "local"
        plan.from_local_copy = True
        small = plan.restricted([M3])
        self.assertEqual([p.machine.id for p in small.machines], [M3])
        self.assertEqual(small.machines[0], plan.machines[2])
        self.assertEqual([(c.line_id, c.machine_ids) for c in small.request.lines], [(L2, [M3])])
        self.assertEqual(small.send_id, small.request.send_id)
        self.assertNotEqual(small.send_id, plan.send_id)
        self.assertEqual((small.request.created_at, small.from_local_copy, small.data_as_of),
                         (plan.request.created_at, plan.from_local_copy, plan.data_as_of))
        self.assertEqual(hm.merge_plan(small.request, data, SENDER).machines, small.machines)

    def test_records(self):
        data = seed()
        company = data.company
        self.assertEqual(hm.validate_company(company), [])
        country_text = "Country names must be unique, not empty and at most {n} characters."
        cases = [
            (dict(countries=company.countries + [Country(name="usa")]), country_text),
            (dict(countries=company.countries + [Country(name=" ")]), country_text),
            (dict(countries=company.countries + [Country(name="x" * 31)]), country_text),
            (dict(countries=[Country(name="USA", aliases=[""])]), country_text),
            (dict(countries=[Country(name="USA\t")]), "{where} contains characters Stripes can't send to a printer: "
                                                      "{codes}."),
            (dict(countries=company.countries + [Country(name="Peru", aliases=["us"])]),
             "{name} is used by more than one country."),
            (dict(countries=company.countries + [Country(name="Peru", aliases=["canada"])]),
             "{name} is used by more than one country."),
            (dict(inks=company.inks + [Ink(name="Example Black", cartridge="paper", colour="black")]),
             "Ink names must be unique, each with a cartridge type and a colour."),
            (dict(inks=[Ink(name="", cartridge="paper", colour="black")]),
             "Ink names must be unique, each with a cartridge type and a colour."),
            (dict(inks=[Ink(name="Gold", cartridge="paper", colour="gold")]),
             "Ink names must be unique, each with a cartridge type and a colour."),
            (dict(lot_min_digits=0), "The lot rules must allow 1 to 20 digits, the minimum no more than the maximum."),
            (dict(lot_min_digits=9), "The lot rules must allow 1 to 20 digits, the minimum no more than the maximum."),
            (dict(lot_max_digits=21), "The lot rules must allow 1 to 20 digits, the minimum no more than the maximum."),
            (dict(free_text_max=0), "The free-text limit must be 1 to 100 characters."),
            (dict(free_text_max=101), "The free-text limit must be 1 to 100 characters."),
            (dict(standard_fonts=["Arial"] * 5), "The standard font list must have 6 fonts."),
            (dict(standard_fonts=["Arial"] * 5 + [""]), "The standard font list must have 6 fonts."),
            (dict(bag_counter="PRCNT"), "The bag counter must be PMCNT, MSCNT or none."),
        ]
        for kw, text in cases:
            self.assertIn(text, texts(hm.validate_company(dataclasses.replace(company, **kw))), kw)
        problems = hm.validate_company(dataclasses.replace(company, countries=company.countries + [
            Country(name="Peru", aliases=["us"])]))
        # Both countries' aliases are taken
        self.assertEqual(problems, [Msg("{name} is used by more than one country.", {"name": "US"}),
                                    Msg("{name} is used by more than one country.", {"name": "us"})])

        line = data.lines[L1]
        self.assertEqual(hm.validate_line(line, data), [])
        for kw, text in [(dict(name=""), "The name must be 1 to {n} characters."),
                         (dict(name="x" * 41), "The name must be 1 to {n} characters."),
                         (dict(code="l1"), "The code must be 1 to 3 capital letters or digits."),
                         (dict(code="L123"), "The code must be 1 to 3 capital letters or digits."),
                         (dict(code="MEX"), "{where} contains words about origin: {words}. Origin goes only in "
                                            "the origin line."),
                         (dict(cartridge="glass"), "The cartridge type isn't valid.")]:
            self.assertIn(text, texts(hm.validate_line(dataclasses.replace(line, **kw), data)), kw)

        machine, encoder = data.machines[M1], data.machines[M2]
        self.assertEqual(hm.validate_machine(machine, data), [])
        self.assertEqual(hm.validate_machine(encoder, data), [])
        (p,) = hm.validate_machine(dataclasses.replace(machine, code="US"), data)
        self.assertEqual(p.values, {"where": Msg("The code of {name}", {"name": "Line 1 Bagger 1"}), "words": "US"})
        for m, kw, text in [
            (machine, dict(name=""), "The name must be 1 to {n} characters."),
            (machine, dict(name="x" * 61), "The name must be 1 to {n} characters."),
            (machine, dict(code="b1"), "The code must be 1 to 3 capital letters or digits."),
            (machine, dict(line_id="ln_missing"), "The machine's line doesn't exist."),
            (machine, dict(printer_id="pr_missing"), "The machine's printer doesn't exist."),
            (machine, dict(printer_id="pr_0000000002"), "{printer} is already used by {machine}."),
            (machine, dict(placement="manual"), "The placement must be Encoder or Delay."),
            (machine, dict(print_speed=0), "The print speed must be 1 to 2500."),
            (machine, dict(print_speed=2501), "The print speed must be 1 to 2500."),
            (machine, dict(seconds_per_bag=0.4), "The seconds per bag must be 0.5 to 60."),
            (machine, dict(seconds_per_bag=61), "The seconds per bag must be 0.5 to 60."),
            (encoder, dict(head_distance_mm=0.0), "The head distance must be more than 0 and at most {max} mm."),
            (encoder, dict(head_distance_mm=20000.5), "The head distance must be more than 0 and at most {max} mm."),
            (encoder, dict(encoder_wheel_mm=19.0), "The encoder wheel must be 20 to 150 mm."),
            (encoder, dict(encoder_ppr=0), "The pulses per turn must be 1 to 1000000."),
            (encoder, dict(encoder_ppr=1_000_001), "The pulses per turn must be 1 to 1000000."),
        ]:
            self.assertIn(text, texts(hm.validate_machine(dataclasses.replace(m, **kw), data)), kw)
        p = hm.validate_machine(dataclasses.replace(machine, printer_id="pr_0000000002"), data)[0]
        self.assertEqual(p.values, {"printer": "Printer 2", "machine": "Line 1 Bagger 2"})
        # An inactive machine may keep its printer; a head distance longer than a bag is fine
        self.assertEqual(hm.validate_machine(dataclasses.replace(machine, printer_id="pr_0000000002", active=False),
                                             data), [])
        self.assertEqual(hm.validate_machine(dataclasses.replace(encoder, head_distance_mm=1300.0, print_speed=None),
                                             data), [])

        film = data.films[FILM]
        self.assertEqual(hm.validate_film(film, data), [])
        no_origin = seeded_print_text(id="pt_0000000002", name="Packed on", code="PK",
                                      items=[seeded_print_text().items[0]])
        data.print_texts[no_origin.id] = no_origin
        data.films["fm_0000000002"] = dataclasses.replace(film, id="fm_0000000002", code="EXCO2")
        film_cases = [
            (dict(product_id="pd_missing"), "The film's product doesn't exist."),
            (dict(code="exco1"), "The film code must be 1 to 6 capital letters or digits."),
            (dict(code="EXCO123"), "The film code must be 1 to 6 capital letters or digits."),
            (dict(code="EXCO2"), "Another film already uses the code {code}."),
            (dict(status="old"), "The status isn't valid."),
            (dict(side="top"), "The side, cartridge type or ink colour isn't valid."),
            (dict(cartridge="glass"), "The side, cartridge type or ink colour isn't valid."),
            (dict(ink_colour="red"), "The side, cartridge type or ink colour isn't valid."),
            (dict(length_mm=0.0), "The film length must be more than 0 and at most {max} mm."),
            (dict(length_mm=5000.5), "The film length must be more than 0 and at most {max} mm."),
            (dict(position_mm=611.0), "The print position must be between 0 and the film length."),
            (dict(position_mm=-1.0), "The print position must be between 0 and the film length."),
            (dict(length_mm=None), "The print position must be between 0 and the film length."),
            (dict(delay_ms=120000.5), "The delay must be 0 to 120000 ms."),
            (dict(delay_ms=-1.0), "The delay must be 0 to 120000 ms."),
            (dict(print_length_mm=0.0), "The print length must be more than 0 and at most the film length."),
            (dict(print_length_mm=611.0), "The print length must be more than 0 and at most the film length."),
            (dict(font=7), "Font, size, resolution, bold or width is out of range."),
            (dict(size=11), "Font, size, resolution, bold or width is out of range."),
            (dict(resolution=4), "Font, size, resolution, bold or width is out of range."),
            (dict(bold=0), "Font, size, resolution, bold or width is out of range."),
            (dict(width=11), "Font, size, resolution, bold or width is out of range."),
            (dict(origin_preprinted=True, print_text_ids=[no_origin.id], default_print_text_id=no_origin.id),
             "Enter the pre-printed origin."),
            (dict(origin_preprinted=True, preprinted_origin="x" * 41, print_text_ids=[no_origin.id],
                  default_print_text_id=no_origin.id), "{where} is too long: at most {n} characters."),
            (dict(origin_preprinted=True, preprinted_origin="US>A", print_text_ids=[no_origin.id],
                  default_print_text_id=no_origin.id),
             "{where} contains characters Stripes can't send to a printer: {codes}."),
            (dict(print_text_ids=[PT, "pt_missing"]), "A print text allowed with this film doesn't exist."),
            (dict(origin_preprinted=True, preprinted_origin="USA"),
             "Print text {print_text} has an origin item, but this film's origin is pre-printed."),
            (dict(print_text_ids=[PT, no_origin.id]),
             "Print text {print_text} has no origin item, but this film needs one."),
            (dict(default_print_text_id=no_origin.id), "The default print text must be one of the film's print texts."),
            (dict(print_text_ids=[], default_print_text_id=""),
             "An active film needs at least one print text and a default print text."),
            (dict(default_print_text_id=""), "An active film needs at least one print text and a default print text."),
        ]
        for kw, text in film_cases:
            self.assertIn(text, texts(hm.validate_film(dataclasses.replace(film, **kw), data)), kw)
        # A film in development may have no print text yet; a pre-printed film is fine with its own texts
        self.assertEqual(hm.validate_film(dataclasses.replace(film, status="development", print_text_ids=[],
                                                              default_print_text_id=""), data), [])
        self.assertEqual(hm.validate_film(dataclasses.replace(film, origin_preprinted=True, preprinted_origin="USA",
                                                              print_text_ids=[no_origin.id],
                                                              default_print_text_id=no_origin.id), data), [])


# --------------------------------------------------------------------------
# Print position and delay (§5)
# --------------------------------------------------------------------------

class PositionTests(unittest.TestCase):
    def test_encoder(self):
        table = [(150, 412, 610, 0, 348, True), (150, 412, 610, -3, 345, True), (500, 412, 610, 0, 88, False),
                 (150, 1300, 610, 0, 70, True), (412, 412, 610, 0, 0, False), (150.5, 412, 610, 0, 349, True)]
        data = seed()
        for position, head, length, adj, sent, wrapped in table:
            self.assertEqual(hm.print_position_mm(position, head, length, adj), sent)
            machine = dataclasses.replace(data.machines[M2], head_distance_mm=float(head))
            film = dataclasses.replace(data.films[FILM], position_mm=float(position), length_mm=float(length))
            adjustment = MachineFilmAdjustment(id="x", machine_id=M2, film_id=FILM, position_mm=float(adj))
            working = hm.explain_position(machine, film, adjustment)
            self.assertEqual((working.sent, working.wrapped), (sent, wrapped), (position, head, adj))
            self.assertEqual((working.placement, working.film_value, working.adjustment, working.head_distance_mm,
                              working.film_length_mm), ("encoder", position, adj, head, length))
        self.assertIsInstance(hm.print_position_mm(150, 412, 610), int)
        self.assertEqual(hm.print_position_mm(609.6, 0.0, 610), 0)            # rounded up to the length is 0
        with self.assertRaises(ValueError):
            hm.print_position_mm(150, 412, 0)
        machine, film = data.machines[M2], data.films[FILM]
        self.assertEqual(hm.explain_position(machine, film, None).text, "(150 - 412 + 0) mod 610 = 348 mm")
        adjustment = MachineFilmAdjustment(id="x", machine_id=M2, film_id=FILM, position_mm=-3.0)
        self.assertEqual(hm.explain_position(machine, film, adjustment).text, "(150 - 412 - 3) mod 610 = 345 mm")
        working = hm.explain_position(machine, film, adjustment, override=152.5, test=True)
        self.assertEqual(working.text, "(152.5 - 412 + 0) mod 610 = 351 mm (test print: adjustment ignored)")
        self.assertEqual(working.adjustment, 0.0)
        # Missing values
        broken = dataclasses.replace(machine, head_distance_mm=None)
        no_film = dataclasses.replace(film, length_mm=None, position_mm=None)
        self.assertEqual(problems_of(hm.explain_position, broken, no_film, None),
                         ["{machine} isn't calibrated: it has no head distance.",
                          "Film {film} has no length, which encoder machines need.",
                          "Film {film} has no print position, which encoder machines need."])

    def test_delay(self):
        self.assertEqual(hm.delay_us(480, 20), 500000)
        self.assertEqual(hm.delay_us(480, 0), 480000)
        self.assertEqual(hm.delay_us(480.0005), 480001)
        self.assertEqual(hm.delay_us(120000), 120_000_000)
        with self.assertRaises(PlanError) as cm:
            hm.delay_us(10, -20)
        self.assertEqual(cm.exception.problems, [Msg("The delay works out at {ms} ms, outside 0 to 120000 ms.",
                                                     {"ms": "-10"})])
        with self.assertRaises(PlanError):
            hm.delay_us(120000.001)
        data = seed()
        machine, film = data.machines[M1], data.films[FILM]
        adjustment = MachineFilmAdjustment(id="x", machine_id=M1, film_id=FILM, delay_ms=20.0)
        working = hm.explain_position(machine, film, adjustment)
        self.assertEqual(working.text, "480 + 20 = 500 ms = 500000 µs")
        self.assertEqual((working.placement, working.sent, working.wrapped, working.head_distance_mm,
                          working.film_length_mm, working.warning), ("delay", 500000, False, None, 610.0, None))
        working = hm.explain_position(machine, film, adjustment, test=True)
        self.assertEqual(working.text, "480 + 0 = 480 ms = 480000 µs (test print: adjustment ignored)")
        self.assertEqual(working.sent, 480000)
        minus = MachineFilmAdjustment(id="x", machine_id=M1, film_id=FILM, delay_ms=-12.5)
        self.assertEqual(hm.explain_position(machine, film, minus).text, "480 - 12.5 = 467.5 ms = 467500 µs")
        self.assertEqual(problems_of(hm.explain_position, machine, dataclasses.replace(film, delay_ms=None), None),
                         ["Film {film} has no delay, which delay machines need."])
        self.assertEqual(problems_of(hm.explain_position, dataclasses.replace(machine, placement="x"), film, None),
                         ["The placement must be Encoder or Delay."])

    def test_film_stop(self):
        warning = hm.film_stop_warning(348, 300, 610)
        self.assertEqual(warning.values, {"end": "648", "length": "610"})
        self.assertIsNone(hm.film_stop_warning(348, 200, 610))
        self.assertIsNone(hm.film_stop_warning(348, None, 610))
        data = seed()
        data.films[FILM].print_length_mm = 300.0
        encoder = golden(M2, data)
        self.assertEqual(encoder.warnings, [warning])
        self.assertEqual(encoder.position.warning, warning)
        self.assertEqual(golden(M1, data).warnings, [])
        data.films[FILM].print_length_mm = 100.0
        self.assertEqual(golden(M2, data).warnings, [])
        self.assertEqual(texts(golden(M2).warnings),
                         ["Print length unknown: Stripes can't check that the print ends before the film stops."])
        self.assertEqual(golden(M1).warnings, [])


# --------------------------------------------------------------------------
# Building a message, names (§6, §7) and the clock (§9.4)
# --------------------------------------------------------------------------

class BuildTests(unittest.TestCase):
    def test_golden(self):
        delay, encoder = golden(M1), golden(M2)
        self.assertEqual([c.xml for c in hm.build_commands(delay, 9)], GOLDEN_DELAY)
        self.assertEqual([c.xml for c in hm.build_commands(encoder, 9)], GOLDEN_ENCODER)
        built = hm.build_message(delay, 9)
        self.assertEqual(built.commands, hm.build_commands(delay, 9))
        self.assertEqual([c.tag for c in built.commands], ["CRTMSG", "CTDAT", "CTEXT", "CTEXT", "CPPARM", "SAVEMSG"])
        self.assertTrue(all(c.write for c in built.commands))
        self.assertEqual((built.slot, built.name, built.content_code, built.origin_line, built.bag_text),
                         (9, "EXCO1-PKO01-MN6D-L9", "MN6D", "Product of USA and Canada", delay.bag_text))
        self.assertEqual([i.tag for i in built.expected_items], ["CTDAT", "CTEXT", "CTEXT"])
        self.assertEqual(built.expected_items[0].pairs[-4:], [("FRMT", "M/D/y"), ("DAY", "0"), ("MON", "0"),
                                                              ("YEAR", "0")])
        settings = built.expected_settings
        self.assertEqual(len(settings), 23)
        self.assertEqual((settings["DELAY"], settings["DLYMM"], settings["RESNAM"], settings["ANG"], settings["PRNSPD"],
                          settings["ENCST"]), ("480000", "0", "UltraFine", "30", "2400", "0"))
        self.assertNotIn("RESO", settings)
        self.assertEqual(hm.expected_settings(encoder.settings)["DLYMM"], "348")
        self.assertEqual(hm.expected_settings(dataclasses.replace(encoder.settings, resolution=2))["RESNAM"], "Normal")
        self.assertEqual(hm.PRINTER_DEFAULT_SETTINGS, hx_fake.FAKE_DEFAULT_SETTINGS)
        self.assertEqual(tuple(hm.RESOLUTION_NAMES.values()), hx_fake.RESOLUTION_NAMES)
        # Escaping is Command.block's: the expected items keep the raw text
        data = seed()
        data.print_texts[PT].items[2].before = "Lot & "
        built = hm.build_message(golden(M1, data), 10)
        self.assertIn("<TXT>Lot &amp; 4417</TXT>", built.commands[3].xml)
        self.assertEqual(built.expected_items[2].value("TXT"), "Lot & 4417")
        self.assertEqual(built.commands[-1].xml, "<SAVEMSG>10</SAVEMSG>")
        with self.assertRaises(ValueError):
            hm.build_message(delay, 1)
        with self.assertRaises(ValueError):
            hm.build_message(delay, 0)
        # Out-of-range values are refused, not sent
        for change in (dict(speed=2501), dict(speed=None), dict(bold=11), dict(resolution=4), dict(delay_us=-1)):
            plan = golden(M1)
            plan.settings = dataclasses.replace(plan.settings, **change)
            with self.assertRaises(ValueError, msg=change):
                hm.build_message(plan, 9)

    def test_fake_world(self):
        """The golden commands from FakeWorld's own seeded records, sent to its fake printers and read back."""
        import hx_protocol as hp
        import hx_share
        from hx_model import from_json
        from hx_time import to_local
        with tempfile.TemporaryDirectory() as tmp:
            world = hx_fake.FakeWorld(tmp, printers=[{}, {"placement": "encoder"}])
            try:
                store = world.store(world.identity("jsmith"))
                ids = world.ids

                def load(cls, rid):
                    return from_json(cls, store.read_json(hx_share.record_rel_for(cls, rid)))
                machines = [load(Machine, ids["machine1"]), load(Machine, ids["machine2"])]
                printers = [load(Printer, ids["printer1"]), load(Printer, ids["printer2"])]
                data = HxData(company=load(Company, "company"), lines={ids["line"]: load(Line, ids["line"])},
                              machines={m.id: m for m in machines}, printers={p.id: p for p in printers},
                              products={ids["product"]: load(Product, ids["product"])},
                              films={ids["film"]: load(Film, ids["film"])},
                              print_texts={ids["print_text"]: load(PrintText, ids["print_text"])})
                req = request(LineChoice(line_id=ids["line"], machine_ids=[m.id for m in machines],
                                         product_id=ids["product"], film_id=ids["film"],
                                         print_text_id=ids["print_text"], lot="4417", countries=["USA", "Canada"]))
                plan = hm.merge_plan(req, data, world.identity("jsmith"))
                self.assertEqual([c.xml for c in hm.build_commands(plan.machines[0], 9)], GOLDEN_DELAY)
                self.assertEqual([c.xml for c in hm.build_commands(plan.machines[1], 9)], GOLDEN_ENCODER)
                pool = hp.ClientPool(key=hp.release_key(), gates=hp.GateRegistry())
                for machine_plan, fake in zip(plan.machines, world.printers):
                    client = pool.client(machine_plan.printer)
                    built = hm.build_message(machine_plan, 9)
                    for cmd in built.commands:
                        client.command(cmd)
                    design = client.load_slot(9)
                    self.assertEqual(hm.compare(built, design), [])
                    self.assertEqual(hm.date_differences(built, design, to_local(design.read_at)), [])
                    self.assertEqual(hm.origin_from_design(design), "Product of USA and Canada")
                    self.assertEqual(hm.parse_message_name(design.name).slot, 9)
                    self.assertEqual(fake.violations, [])
                    self.assertEqual(fake.max_in_flight, 1)
            finally:
                world.close()

    def test_names(self):
        delay, encoder = golden(M1), golden(M2)
        self.assertEqual(hm.content_code(delay), "MN6D")
        self.assertEqual((hm.message_name(delay, 9), hm.message_name(delay, 10)),
                         ("EXCO1-PKO01-MN6D-L9", "EXCO1-PKO01-MN6D-U0"))
        self.assertEqual(hm.content_code(encoder), "5E11")
        self.assertEqual((hm.message_name(encoder, 9), hm.message_name(encoder, 10)),
                         ("EXCO1-PKO01-5E11-W9", "EXCO1-PKO01-5E11-D0"))
        other = hm.merge_plan(request(choice(lot="4418")), seed(), SENDER).machines[0]
        self.assertEqual(hm.content_code(other), "RVFJ")
        longest = hm._name("ABCDEF", "XYZ", 123, "ZZZZ", 10)
        self.assertEqual(longest, "ABCDEF-XYZ23-ZZZZ-V0")
        self.assertEqual(len(longest), 20)
        # The canonical design
        canonical = hm.canonical_design(*hm.design_parts(delay))
        self.assertEqual(len(canonical), 679)
        self.assertTrue(canonical.startswith(b'{"v":1,"items":[["CTDAT",[["OBJID","10001"],["POS","0,0"],'))
        self.assertEqual(hashlib.sha256(canonical).hexdigest(),
                         "a54cde6dbf91adca88911db3bf74c1bfa9f6ee5516f245e8bd71c13f50d3166b")
        # The content code follows anything printed, but not the slot, the name or the date
        data = seed()
        data.print_texts[PT].revision = 2
        self.assertEqual(hm.content_code(golden(M1, data)), "MN6D")
        self.assertEqual(hm.message_name(golden(M1, data), 9)[:12], "EXCO1-PKO02-")
        self.assertNotEqual(hm.content_code(hm.merge_plan(request(choice(countries=["Canada", "USA"])), seed(),
                                                          SENDER).machines[0]), "MN6D")
        # Parsing
        self.assertEqual(hm.parse_message_name("EXCO1-PKO01-MN6D-L9"), MessageName("EXCO1", "PKO", 1, "MN6D", 9))
        self.assertEqual(hm.parse_message_name("EXCO1-PKO01-5E11-D0"), MessageName("EXCO1", "PKO", 1, "5E11", 10))
        for bad in ("EXCO1-PKO01-MN6D-M9", "STRIPES TEST", "1", "", None, "None", "EXCO1-PKO01-MN6D-L0",
                    "EXCO1-PKO01-MN6D-L9 ", "exco1-PKO01-MN6D-L9", "EXCO1-PKO1-MN6D-L9"):
            self.assertIsNone(hm.parse_message_name(bad), bad)
        for letter in hm.CHECK_LETTERS:
            if letter != "L":
                self.assertIsNone(hm.parse_message_name(f"EXCO1-PKO01-MN6D-{letter}9"), letter)
        rng = random.Random(1109)
        alnum = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        for _ in range(200):
            film_code = "".join(rng.choice(alnum) for _ in range(rng.randint(1, 6)))
            code = "".join(rng.choice(alnum) for _ in range(rng.randint(1, 3)))
            revision = rng.randint(0, 999)
            content = "".join(rng.choice(hm.CONTENT_ALPHABET) for _ in range(4))
            slot = rng.choice((9, 10))
            name = hm._name(film_code, code, revision, content, slot)
            self.assertLessEqual(len(name), 20)
            self.assertGreaterEqual(len(name), 13)
            self.assertEqual(hm.parse_message_name(name), MessageName(film_code, code, revision % 100, content, slot))
        self.assertIn(hm.check_letter("EXCO1-PKO01-MN6D-9"), hm.CHECK_LETTERS)
        self.assertEqual(hm.check_letter("EXCO1-PKO01-MN6D-9"), "L")

    def test_empty_and_idle(self):
        self.assertEqual([c.xml for c in hm.empty_message_commands(10)], ["<CRTMSG></CRTMSG>", "<SAVEMSG>10</SAVEMSG>"])
        self.assertEqual([c.xml for c in hm.empty_message_commands(9)], ["<CRTMSG></CRTMSG>", "<SAVEMSG>9</SAVEMSG>"])
        with self.assertRaises(ValueError):
            hm.empty_message_commands(3)
        for printing in (True, False):
            self.assertEqual(hm.idle_slot(printing, 9), 10)
            self.assertEqual(hm.idle_slot(printing, 10), 9)
            self.assertEqual(hm.idle_slot(printing, 1), 9)
            self.assertEqual(hm.idle_slot(printing, None), 9)

    def test_clock(self):
        expected = ("<TD><DAY>29</DAY><MNTH>10</MNTH><YEAR>2026</YEAR><HOUR>8</HOUR><MIN>45</MIN><DTRH>0</DTRH>"
                    "<DTRM>0</DTRM></TD>")
        cmd = hm.clock_command(datetime(2026, 10, 29, 8, 44, 31))
        self.assertEqual((cmd.tag, cmd.xml, cmd.write), ("TD", expected, True))
        self.assertEqual(hm.clock_command(datetime(2026, 10, 29, 8, 44, 29)).xml,
                         expected.replace("<MIN>45", "<MIN>44"))
        self.assertEqual(hm.clock_command(datetime(2026, 10, 29, 8, 44, 30)).xml, expected)
        self.assertEqual(hm.clock_command(datetime(2026, 12, 31, 23, 59, 45)).xml,
                         "<TD><DAY>1</DAY><MNTH>1</MNTH><YEAR>2027</YEAR><HOUR>0</HOUR><MIN>0</MIN><DTRH>0</DTRH>"
                         "<DTRM>0</DTRM></TD>")
        self.assertEqual(hm.clock_command(datetime(2026, 10, 29, 8, 44, 29, 999999)).xml,
                         expected.replace("<MIN>45", "<MIN>44"))
        for bad in (datetime(2051, 1, 1), datetime(2050, 12, 31, 23, 59, 31), datetime(1999, 12, 31)):
            with self.assertRaises(ValueError):
                hm.clock_command(bad)
        self.assertEqual(hm.clock_command(datetime(2000, 1, 1)).xml[:40], "<TD><DAY>1</DAY><MNTH>1</MNTH><YEAR>2000")


# --------------------------------------------------------------------------
# Read-back (§8)
# --------------------------------------------------------------------------

class CompareTests(unittest.TestCase):
    def setUp(self):
        self.built = hm.build_message(golden(M1), 9)

    def readback(self, **kw):
        return verbose_readback(self.built, **kw)

    def one(self, design):
        diffs = hm.compare(self.built, design)
        self.assertEqual(len(diffs), 1, diffs)
        return diffs[0]

    def test_match(self):
        self.assertEqual(hm.compare(self.built, self.readback()), [])
        encoder = hm.build_message(golden(M2), 10)
        self.assertEqual(hm.compare(encoder, verbose_readback(encoder)), [])

    def test_rules(self):
        d = self.readback()
        set_pair(d, 2, "SIZE", "4")
        self.assertEqual(self.one(d), Difference("item 2 (CTEXT)", "SIZE", "3", "4"))
        d = self.readback()
        set_pair(d, 2, "TXT", "Product of USA and Canada ")
        self.assertEqual(self.one(d), Difference("item 2 (CTEXT)", "TXT", "Product of USA and Canada",
                                                 "Product of USA and Canada "))
        d = self.readback()
        set_pair(d, 3, "SLANT", "0")
        self.assertEqual(self.one(d), Difference("item 3 (CTEXT)", "SLANT", "(not expected)", "0"))
        d = self.readback()
        set_pair(d, 1, "WCOEFF", "1")
        self.assertEqual(self.one(d), Difference("item 1 (CTDAT)", "WCOEFF", "0", "1"))
        d = self.readback()
        del d.settings["PRNSPD"]
        self.assertEqual(self.one(d), Difference("print settings", "PRNSPD", "2400", "(missing)"))
        d = self.readback()
        d.settings["ANG"] = "31"
        self.assertEqual(self.one(d), Difference("print settings", "ANG", "30", "31"))
        d = self.readback()
        d.settings["FOO"] = "1"
        self.assertEqual(self.one(d), Difference("print settings", "FOO", "(not expected)", "1"))
        d = self.readback()
        d.black_pixels = 0
        self.assertEqual(self.one(d), Difference("picture", "BPIX", "more than 0", "0"))
        d = self.readback()
        d.name = "EXCO1-PKO01-MN6D-U0"
        self.assertEqual(self.one(d), Difference("name", "MSGNAM", "EXCO1-PKO01-MN6D-L9", "EXCO1-PKO01-MN6D-U0"))
        d = self.readback()
        d.slot = 10
        self.assertEqual(self.one(d), Difference("slot", "MSNO", "9", "10"))
        empty = MessageDesign(slot=9, name="", empty=True)
        self.assertEqual(hm.compare(self.built, empty), [Difference("message", "empty", "a message", "an empty slot")])
        # Not compared: RESO, BOUND inside the area, LANG, a relative item's POS, bookkeeping fields
        d = self.readback()
        d.settings["RESO"] = "600"
        set_pair(d, 1, "BOUND", "10,10,500,90")
        set_pair(d, 1, "LANG", "9")
        set_pair(d, 2, "POS", "0,36")
        set_pair(d, 1, "AMPM", "")
        set_pair(d, 1, "TXT", "anything")
        self.assertEqual(hm.compare(self.built, d), [])
        # More rules
        d = self.readback()
        set_pair(d, 1, "POS", "5,0")             # the first item's POS was sent
        self.assertEqual(self.one(d), Difference("item 1 (CTDAT)", "POS", "0,0", "5,0"))
        d = self.readback()
        set_pair(d, 1, "XRID", "10002")          # not sent: must be 0
        self.assertEqual(self.one(d), Difference("item 1 (CTDAT)", "XRID", "0", "10002"))
        d = self.readback()
        set_pair(d, 2, "POSF", ".")
        self.assertEqual(self.one(d), Difference("item 2 (CTEXT)", "POSF", "", "."))
        d = self.readback()
        set_pair(d, 1, "HOUR", "1")
        self.assertEqual(self.one(d), Difference("item 1 (CTDAT)", "HOUR", "0", "1"))
        d = self.readback()
        set_pair(d, 1, "BOUND", "")
        self.assertEqual(self.one(d).field, "BOUND")
        d = self.readback()
        d.items[2].pairs = [(k, v) for k, v in d.items[2].pairs if k != "MARG"]
        self.assertEqual(self.one(d), Difference("item 3 (CTEXT)", "MARG", "0,0", "(missing)"))
        d = self.readback()
        d.items[2].pairs = [(k, v) for k, v in d.items[2].pairs if k != "SIZE"]
        self.assertEqual(self.one(d), Difference("item 3 (CTEXT)", "SIZE", "3", "(missing)"))
        d = self.readback()
        d.items[0].pairs.append(("YEAR", "1"))   # every occurrence of a repeated field must pass
        self.assertEqual(self.one(d), Difference("item 1 (CTDAT)", "YEAR", "0", "1"))
        d = self.readback()
        d.width = 300
        self.assertEqual(self.one(d), Difference("message", "W", "256", "300"))
        d = self.readback()
        d.width = None                           # absent in the compact form: fine
        self.assertEqual(hm.compare(self.built, d), [])
        d = self.readback()
        d.items[1].tag = "CLOT"
        self.assertEqual(self.one(d), Difference("item 2 (CTEXT)", "tag", "CTEXT", "CLOT"))
        d = self.readback()
        d.items.pop()
        self.assertEqual(self.one(d), Difference("items", "count", "3", "2"))
        # Raw strings: an entity read back is a difference
        data = seed()
        data.print_texts[PT].items[2].before = "Lot & "
        built = hm.build_message(golden(M1, data), 9)
        d = verbose_readback(built)
        self.assertEqual(hm.compare(built, d), [])
        set_pair(d, 3, "TXT", "Lot &amp; 4417")
        self.assertEqual(len(hm.compare(built, d)), 1)

    def test_dates(self):
        d = self.readback(date_text="10/29/26")
        self.assertEqual(hm.date_differences(self.built, d, datetime(2026, 10, 29, 12, 0)), [])
        d = self.readback(date_text="10/28/26")
        self.assertEqual(hm.date_differences(self.built, d, datetime(2026, 10, 29, 12, 0)),
                         [Difference("item 1 (CTDAT)", "TXT", "10/29/26", "10/28/26")])
        d = self.readback(date_text="10/29/26")
        self.assertEqual(hm.date_differences(self.built, d, datetime(2026, 10, 30, 0, 1, 0)), [])
        self.assertEqual(hm.date_differences(self.built, d, datetime(2026, 10, 30, 0, 3, 0)),
                         [Difference("item 1 (CTDAT)", "TXT", "10/30/26", "10/29/26")])
        self.assertEqual(hm.date_differences(self.built, d, datetime(2026, 10, 29, 23, 59), slack_s=0), [])
        self.assertEqual(hm.date_differences(self.built, MessageDesign(slot=9, name="", empty=True),
                                             datetime(2026, 10, 29, 12)), [])
        # A best-by offset is rendered with it
        plan = golden(M1)
        plan.print_text.items[0] = PrintTextItem(id="i1", kind="best_by_date", date_format="DD/MM/YYYY", offset=1,
                                                 offset_unit="months", before="Best by ")
        built = hm.build_message(plan, 9)
        self.assertEqual(hm.date_differences(built, verbose_readback(built, date_text="29/11/2026"),
                                             datetime(2026, 10, 29, 12)), [])

    def test_fingerprint(self):
        import hx_protocol as hp
        with open(os.path.join(HERE, "testdata", "hx", "sme_load_slot9.bin"), "rb") as f:
            body = f.read()
        crlf = hp.parse_sme_reply(body).design
        lf = hp.parse_sme_reply(body.replace(b"\r\n", b"\n")).design
        self.assertEqual(hm.fingerprint(crlf), hm.fingerprint(lf))
        self.assertRegex(hm.fingerprint(crlf), r"^[0-9a-f]{64}$")
        self.assertEqual(hm.origin_from_design(crlf), "")

        base = self.readback()
        same = hm.fingerprint(base)
        changes_ignored = [
            lambda d: set_pair(d, 1, "TXT", "10/30/26"),
            lambda d: set_pair(d, 2, "BOUND", "0,40,360,76"),
            lambda d: set_pair(d, 3, "POS", "350,40"),          # a relative item's POS
            lambda d: setattr(d, "slot", 10),
            lambda d: setattr(d, "read_at", T0 + 60),
            lambda d: setattr(d, "preview_bmp", hx_fake.bmp_1bit([(0, 0, 10, 10)])),
            lambda d: setattr(d, "black_pixels", 1),
            lambda d: setattr(d, "tn", "1"),
        ]
        for change in changes_ignored:
            d = copy.deepcopy(base)
            change(d)
            self.assertEqual(hm.fingerprint(d), same)
        changes_counted = [
            lambda d: set_pair(d, 2, "TXT", "Product of USA"),
            lambda d: set_pair(d, 2, "SIZE", "4"),
            lambda d: d.settings.update(PRNSPD="2300"),
            lambda d: setattr(d, "name", "X"),
            lambda d: set_pair(d, 1, "POS", "1,0"),             # the first item is absolute
            lambda d: d.items.pop(),
            lambda d: d.items[0].pairs.append(("LANG", "4")),  # repeats count
        ]
        for change in changes_counted:
            d = copy.deepcopy(base)
            change(d)
            self.assertNotEqual(hm.fingerprint(d), same)
        # Settings order doesn't matter
        d = copy.deepcopy(base)
        d.settings = dict(reversed(list(d.settings.items())))
        self.assertEqual(hm.fingerprint(d), same)
        self.assertEqual(hm.origin_from_design(base), "Product of USA and Canada")
        self.assertEqual(hm.origin_from_design(MessageDesign(slot=9, name="", empty=True)), "")

    def test_cleared(self):
        self.assertTrue(hm.cleared(MessageDesign(slot=9, name="", empty=True)))
        self.assertTrue(hm.cleared(MessageDesign(slot=10, name="", empty=False, settings={"ROTAT": "0"})))
        self.assertFalse(hm.cleared(self.readback()))
        self.assertFalse(hm.cleared(MessageDesign(slot=9, name="EXCO1-PKO01-MN6D-L9", empty=False)))

    def test_print_area(self):
        d = self.readback()
        set_pair(d, 1, "BOUND", "0,0,1030,33")
        self.assertEqual(hm.compare(self.built, d), [Difference("item 1 (CTDAT)", "BOUND", "inside 1024 x 160",
                                                                "0,0,1030,33")])
        d = self.readback()
        set_pair(d, 2, "BOUND", "0,0,1030,33")
        self.assertEqual(hm.compare(self.built, d), [Difference("item 2 (CTEXT)", "BOUND", "inside 1024 x 160",
                                                                "0,0,1030,33")])
        for bound in ("0,0,200,161", "-1,0,200,30", "0,-1,200,30"):
            d = self.readback()
            set_pair(d, 2, "BOUND", bound)
            self.assertEqual(len(hm.compare(self.built, d)), 1, bound)
        d = self.readback()
        set_pair(d, 2, "BOUND", "0,0,1024,160")
        self.assertEqual(hm.compare(self.built, d), [])
        # H comes from the read-back when present
        d = self.readback()
        d.height = None
        set_pair(d, 2, "BOUND", "0,0,200,160")
        self.assertEqual(hm.compare(self.built, d), [])
        # A compact item has no BOUND at all
        d = self.readback()
        for item in d.items:
            item.pairs = [(k, v) for k, v in item.pairs if k != "BOUND"]
        self.assertEqual(hm.compare(self.built, d), [])


# --------------------------------------------------------------------------
# Resume and the result colour (§13.3, §14)
# --------------------------------------------------------------------------

class ResumeTests(unittest.TestCase):
    def test_rules(self):
        machine = seed().machines[M1]
        live = dict(machine_id=M1, mode="running", confirmed=True, message_name="EXCO1-PKO01-MN6D-L9", slot=9)
        failure = FailureInfo(step="build", msg=Msg("x"), after_switch=False, at=T0)
        cases = [
            (None, "No Send is recorded for {machine}. Send instead."),
            (dict(machine_id=M1), "No Send is recorded for {machine}. Send instead."),
            (dict(live, mode="switch_pending"), "{machine} is in the middle of a Send or a test."),
            (dict(live, mode="test_running"), "{machine} is in the middle of a Send or a test."),
            (dict(live, rejected=True, resume_blocked=True), "The last Send to {machine} was rejected. Send again "
                                                            "instead."),
            (dict(live, mode="test_finished", resume_blocked=True), "{machine} needs a Send after the test print."),
            (dict(live, mode="stopped_by_user", stop_reason="test", resume_blocked=True),
             "{machine} needs a Send after the test print."),
            (dict(live, resume_blocked=True), "{machine} needs a new Send before it can print again."),
            (dict(live, failure=failure), "{machine} needs a new Send before it can print again."),
            (dict(live, confirmed=False), "The last Send to {machine} wasn't confirmed. Send again instead."),
        ]
        for fields, text in cases:
            expected = ExpectedState(**fields) if fields is not None else None
            refusal = hm.resume_refusal(expected, machine)
            self.assertEqual(refusal, Msg(text, {"machine": "Line 1 Bagger 1"}), fields)
        for mode in ("running", "stopped_by_user", "stop_requested_at_printer"):
            self.assertIsNone(hm.resume_refusal(ExpectedState(**dict(live, mode=mode, stop_reason="user")), machine))

        built = hm.build_message(golden(M1), 9)
        design = verbose_readback(built)
        expected = ExpectedState(**dict(live, fingerprint=hm.fingerprint(design)))
        self.assertIsNone(hm.resume_mismatch(expected, design, machine))
        mismatch = Msg("The message on {machine} isn't the last confirmed Send. Send again instead.",
                       {"machine": "Line 1 Bagger 1"})
        for change in (lambda d: setattr(d, "name", "EXCO1-PKO01-MN6D-U0"), lambda d: setattr(d, "slot", 10),
                       lambda d: set_pair(d, 2, "TXT", "Product of USA")):
            d = copy.deepcopy(design)
            change(d)
            self.assertEqual(hm.resume_mismatch(expected, d, machine), mismatch)

    def test_result_colour(self):
        self.assertEqual(hm.result_colour(True, []), GREEN)
        self.assertEqual(hm.result_colour(True, ["film_stop", "ink_mismatch"]), GREEN)
        for note in hm.AMBER_NOTES:
            self.assertEqual(hm.result_colour(True, [note]), AMBER)
        self.assertEqual(hm.result_colour(False, []), AMBER)

    def test_texts(self):
        self.assertEqual(set(hm.STEP_TEXTS), set(STEPS) | {"stop", "resume"})
        self.assertEqual(set(hm.NOTE_TEXTS), {"pc_time", "no_lock", "local_copy", "ink_mismatch", "film_stop",
                                              "cancelled"})
        self.assertEqual(hm.NOTE_TEXTS["cancelled"].english(), "Cancelled. Nothing was sent.")
        import i18n
        for msg in list(hm.STEP_TEXTS.values()) + list(hm.NOTE_TEXTS.values()):
            self.assertIn(msg.text, i18n.ES)


# --------------------------------------------------------------------------
# Test print and calibration (§15, §16)
# --------------------------------------------------------------------------

class TestPlanTests(unittest.TestCase):
    def test_steps(self):
        data = seed()
        ranges = TestRanges(vary="position", start=140, stop=160, step=5, bold=[1, 2])
        plan = hm.plan_test(M2, FILM, ranges, data, ADMIN)
        self.assertEqual([s.label for s in plan.steps], ["P140 | B1", "P145 | B1", "P150 | B1", "P155 | B1",
                                                         "P160 | B1", "P150 | B1", "P150 | B2"])
        self.assertEqual([s.index for s in plan.steps], list(range(1, 8)))
        self.assertEqual([s.position_mm for s in plan.steps], [140, 145, 150, 155, 160, 150, 150])
        self.assertEqual([s.bold for s in plan.steps], [1, 1, 1, 1, 1, 1, 2])
        self.assertTrue(all(s.delay_ms is None and (s.width, s.resolution) == (1, 0) for s in plan.steps))
        self.assertEqual(hm.test_bag_estimate(plan), 42)
        self.assertEqual((plan.kind, plan.bags_per_step, plan.timed), ("test", 5, False))
        self.assertRegex(plan.run_id, r"^tp_[0-9a-f]{10}$")
        base = plan.base
        self.assertEqual((base.product, base.print_text, base.adjustment, base.origin_line, base.test_step),
                         (None, None, None, "", None))
        self.assertEqual(base.machine, data.machines[M2])
        self.assertEqual(base.position.sent, 348)
        self.assertEqual(base.settings, PrintSettings(encoder=True, position_mm=348, speed=2400))
        # Delay machines; width and resolution; a value rounded to 0.1
        plan = hm.plan_test(M1, FILM, TestRanges(vary="delay", start=470, stop=490, step=10, width=[2],
                                                 resolution=[3]), data, ADMIN)
        self.assertEqual([s.label for s in plan.steps], ["D470 | W1 | R0", "D480 | W1 | R0", "D490 | W1 | R0",
                                                         "D480 | W2 | R0", "D480 | W1 | R3"])
        self.assertEqual([s.delay_ms for s in plan.steps], [470, 480, 490, 480, 480])
        plan = hm.plan_test(M2, FILM, TestRanges(vary="position", start=150, stop=155, step=2.5), data, ADMIN)
        self.assertEqual([s.label for s in plan.steps], ["P150", "P152.5", "P155"])
        plan = hm.plan_test(M2, FILM, TestRanges(vary="position", start=0.1, stop=0.3, step=0.1), data, ADMIN)
        self.assertEqual([s.position_mm for s in plan.steps], [0.1, 0.2, 0.3])
        # The film has no position: v0 is the range's start
        data.films[FILM].position_mm = None
        plan = hm.plan_test(M2, FILM, TestRanges(vary="position", start=100, stop=110, step=10, bold=[3]), data, ADMIN)
        self.assertEqual(plan.steps[-1].label, "P100 | B3")
        self.assertEqual(plan.base.position.film_value, 100)
        data.company.bag_counter = "none"
        self.assertTrue(hm.plan_test(M2, FILM, TestRanges(vary="position", start=100, stop=100, step=1), data,
                                     ADMIN).timed)

    def test_refused(self):
        data = seed()
        ok = TestRanges(vary="position", start=140, stop=160, step=5)
        with self.assertRaises(NotAllowed) as cm:
            hm.plan_test(M2, FILM, ok, data, SENDER)
        self.assertEqual(str(cm.exception), "Only Stripes Administrators can run test prints.")
        cases = [
            (M2, dataclasses.replace(ok, vary="delay"),
             "The test must vary the print position on encoder machines and the delay on delay machines."),
            (M1, ok, "The test must vary the print position on encoder machines and the delay on delay machines."),
            (M2, TestRanges(vary="position", start=0, stop=100, step=1),
             "The test would have {n} steps; at most {max} are allowed."),
            (M2, dataclasses.replace(ok, bold=[11]), "A tested value is out of range."),
            (M2, dataclasses.replace(ok, width=[0]), "A tested value is out of range."),
            (M2, dataclasses.replace(ok, resolution=[4]), "A tested value is out of range."),
            (M2, dataclasses.replace(ok, stop=615), "A tested value is out of range."),
            (M1, TestRanges(vary="delay", start=119_990, stop=120_010, step=10), "A tested value is out of range."),
            (M2, dataclasses.replace(ok, step=0), "The test range isn't valid: check the start, stop and step."),
            (M2, dataclasses.replace(ok, start=170), "The test range isn't valid: check the start, stop and step."),
            (M2, dataclasses.replace(ok, fonts=[(2, 3)] * 7), "At most {n} fonts can be printed side by side."),
            (M2, dataclasses.replace(ok, fonts=[(7, 3)]), "A tested value is out of range."),
            (M2, dataclasses.replace(ok, fonts=[(2, 11)]), "A tested value is out of range."),
            ("mc_missing", ok, "{machine} isn't an active machine on {line}."),
        ]
        for machine_id, ranges, text in cases:
            self.assertIn(text, problems_of(hm.plan_test, machine_id, FILM, ranges, data, ADMIN), ranges)
        e = None
        try:
            hm.plan_test(M2, FILM, TestRanges(vary="position", start=0, stop=100, step=1), data, ADMIN)
        except PlanError as exc:
            e = exc
        self.assertEqual(e.msg.values, {"n": 101, "max": 100})
        # 100 steps is fine; a tiny step is refused without making millions of steps
        self.assertEqual(len(hm.plan_test(M2, FILM, TestRanges(vary="position", start=0, stop=99, step=1), data,
                                          ADMIN).steps), 100)
        self.assertIn("The test would have {n} steps; at most {max} are allowed.",
                      problems_of(hm.plan_test, M2, FILM, TestRanges(vary="position", start=0, stop=600,
                                                                     step=1e-6), data, ADMIN))
        for change, text in [
            (lambda d: setattr(d.machines[M2], "head_distance_mm", None),
             "{machine} isn't calibrated: it has no head distance."),
            (lambda d: setattr(d.films[FILM], "length_mm", None),
             "Film {film} has no length, which encoder machines need."),
            (lambda d: setattr(d.machines[M2], "print_speed", None),
             "{machine} has no print speed. An administrator must set it."),
            (lambda d: setattr(d.printers["pr_0000000002"], "token", ""),
             "The printer of {machine} isn't set up: it is missing, not active or has no saved password."),
            (lambda d: setattr(d.machines[M2], "active", False), "{machine} isn't an active machine on {line}."),
            (lambda d: setattr(d.films[FILM], "font", 9), "Font, size, resolution, bold or width is out of range."),
        ]:
            d = seed()
            change(d)
            self.assertIn(text, problems_of(hm.plan_test, M2, FILM, ok, d, ADMIN))
        self.assertIn("Choose a film for {line}.", problems_of(hm.plan_test, M2, "fm_missing", ok, data, ADMIN))
        # Any film status
        data.films[FILM].status = "development"
        self.assertEqual(len(hm.plan_test(M2, FILM, ok, data, ADMIN).steps), 5)

    def test_step_plan(self):
        data = seed()
        data.adjustments[adjustment_id(M2, FILM)] = MachineFilmAdjustment(
            id=adjustment_id(M2, FILM), machine_id=M2, film_id=FILM, position_mm=-3.0)
        plan = hm.plan_test(M2, FILM, TestRanges(vary="position", start=150, stop=150, step=1,
                                                 fonts=[(2, 3), (2, 4)]), data, ADMIN)
        (step,) = plan.steps
        self.assertEqual((step.label, step.fonts), ("P150", [(2, 3), (2, 4)]))
        mp = hm.test_step_plan(plan.base, step)
        self.assertEqual(mp.settings.position_mm, 348)
        self.assertEqual(mp.position.adjustment, 0.0)
        self.assertTrue(mp.position.text.endswith(" (test print: adjustment ignored)"))
        self.assertEqual(mp.test_step, step)
        self.assertEqual(mp.bag_text, "P150 Arial 3 Arial 4")
        self.assertIsNone(plan.base.test_step)                  # the base isn't changed
        parts, settings = hm.design_parts(mp)
        self.assertEqual(parts, [
            ("CTEXT", [("OBJID", "10001"), ("POS", "0,0"), ("MARG", "0,0"), ("FONTIND", "2"), ("SIZE", "4"),
                       ("TXT", "P150")]),
            ("CTEXT", [("OBJID", "10002"), ("XRID", "10001"), ("YRID", "10001"), ("SXAS", "0"), ("SYAS", "1"),
                       ("MARG", "8,0"), ("FONTIND", "2"), ("SIZE", "3"), ("TXT", "Arial 3")]),
            ("CTEXT", [("OBJID", "10003"), ("XRID", "10002"), ("YRID", "10002"), ("SXAS", "0"), ("SYAS", "1"),
                       ("MARG", "8,0"), ("FONTIND", "2"), ("SIZE", "4"), ("TXT", "Arial 4")])])
        self.assertEqual(settings[:2], [("ENCST", "1"), ("DLYMM", "348")])
        built = hm.build_message(mp, 10)
        name = hm.parse_message_name(built.name)
        self.assertEqual((name.film_code, name.print_text_code, name.print_text_revision, name.slot),
                         ("EXCO1", "TST", 1, 10))
        self.assertEqual(hm.compare(built, verbose_readback(built)), [])
        # Bold, width and resolution come from the step
        bold = dataclasses.replace(step, index=7, bold=3, width=2, resolution=1, fonts=[])
        mp = hm.test_step_plan(plan.base, bold)
        self.assertEqual((mp.settings.bold, mp.settings.width, mp.settings.resolution), (3, 2, 1))
        self.assertTrue(hm.build_message(mp, 9).name.startswith("EXCO1-TST07-"))
        self.assertEqual(len(hm.design_parts(mp)[0]), 1)
        # Delay machines
        plan = hm.plan_test(M1, FILM, TestRanges(vary="delay", start=470, stop=470, step=1), data, ADMIN)
        mp = hm.test_step_plan(plan.base, plan.steps[0])
        self.assertEqual((mp.settings.encoder, mp.settings.delay_us, mp.settings.position_mm), (False, 470000, 0))

    def test_calibration(self):
        data = seed()
        data.calibrations[M2] = Calibration(id=M2, machine_id=M2, rough_head_distance_mm=400.0)
        spacing = hm.plan_test(M2, FILM, TestRanges(vary="position", start=0, stop=0, step=1), data, ADMIN,
                               kind="spacing")
        self.assertEqual([(s.label, s.position_mm) for s in spacing.steps], [("SPACE A", 365), ("SPACE B", 55)])
        self.assertEqual(spacing.base.machine.head_distance_mm, 0.0)
        self.assertEqual(data.machines[M2].head_distance_mm, 412.0)          # the record isn't changed
        head = hm.plan_test(M2, FILM, TestRanges(vary="delay", start=5, stop=1, step=0), data, ADMIN,
                            kind="head_distance")
        self.assertEqual([(s.label, s.position_mm) for s in head.steps], [("HEAD", 515)])
        for plan in (spacing, head):
            self.assertEqual((plan.bags_per_step, plan.kind in ("spacing", "head_distance")), (5, True))
            for step in plan.steps:
                self.assertEqual(step.fonts, [])
                mp = hm.test_step_plan(plan.base, step)
                self.assertEqual(mp.settings.position_mm, step.position_mm)      # sent as it is
        self.assertEqual(hm.spacing_gap_mm(167, 467, 610), 300)
        self.assertEqual(hm.spacing_gap_mm(500, 190, 610), 300)
        self.assertEqual(hm.wheel_suggestion_mm(56, 300, 306), 57)
        self.assertIsInstance(hm.wheel_suggestion_mm(56, 300, 306), int)
        self.assertIsNone(hm.wheel_suggestion_mm(56, 300, 3000))
        self.assertIsNone(hm.wheel_suggestion_mm(56, 300, 10))
        self.assertEqual(hm.head_distance_from_print(348, 150, 610, 400), 412.0)
        self.assertEqual(hm.head_distance_from_print(348, 150, 610, 1000), 1022.0)
        self.assertEqual(hm.head_distance_from_print(515, 317, 610, 400), 412.0)
        with self.assertRaises(PlanError) as cm:
            hm.head_distance_from_print(348, 150, 610, 700)
        self.assertEqual(str(cm.exception), "The tape measurement is too far from the print to be sure. Measure the "
                                            "head distance again.")
        # A position that rounds up to the film length is sent as 0
        data.films[FILM].length_mm = 611.0
        data.calibrations[M2].rough_head_distance_mm = 306.0
        head = hm.plan_test(M2, FILM, TestRanges(vary="position", start=0, stop=0, step=1), data, ADMIN,
                            kind="head_distance")
        self.assertEqual(head.steps[0].position_mm, 0)
        # A machine that has never been calibrated can be
        data = seed()
        data.calibrations[M2] = Calibration(id=M2, machine_id=M2, rough_head_distance_mm=400.0)
        data.machines[M2].head_distance_mm = None
        for kind in ("spacing", "head_distance"):
            plan = hm.plan_test(M2, FILM, TestRanges(vary="position", start=0, stop=0, step=1), data, ADMIN, kind=kind)
            self.assertEqual(plan.base.machine.head_distance_mm, 0.0)
        # Refusals
        ok = TestRanges(vary="position", start=0, stop=0, step=1)
        self.assertIn("Calibration is only for encoder machines.",
                      problems_of(lambda: hm.plan_test(M1, FILM, ok, data, ADMIN, kind="spacing")))
        no_record = seed()
        self.assertIn("Measure the rough head distance first.",
                      problems_of(lambda: hm.plan_test(M2, FILM, ok, no_record, ADMIN, kind="head_distance")))
        data.films[FILM].length_mm = 349.0
        data.films[FILM].position_mm = 100.0
        self.assertEqual(problems_of(lambda: hm.plan_test(M2, FILM, ok, data, ADMIN, kind="spacing")),
                         ["This film is too short for the {mm} mm spacing check."])
        data.films[FILM].length_mm = 350.0
        self.assertEqual(len(hm.plan_test(M2, FILM, ok, data, ADMIN, kind="spacing").steps), 2)
        with self.assertRaises(ValueError):
            hm.plan_test(M2, FILM, ok, data, ADMIN, kind="other")


if __name__ == "__main__":
    unittest.main()
