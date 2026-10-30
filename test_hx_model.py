"""Tests for hx_model: JSON mapping, identity, timings, errors and the record helpers.
Run: python -m unittest -v"""

import dataclasses
import json
import re
import unittest

import hx_model as m
from hx_core import Msg

T0 = 1_793_275_200.0
MSG = Msg("{machine} is set for {ink} ink", {"machine": "Line 1 Bagger 1", "ink": Msg("black")})
ADMIN_SID = "S-1-5-21-1000000001-1000000002-1000000003-2101"
SENDER_SID = "S-1-5-21-1000000001-1000000002-1000000003-2102"


def examples():
    """One instance of every hx_model dataclass, with every field away from its default where it has
    one (so a round trip proves each field is carried). Bytes stay empty: JSON can't hold them."""
    company = m.Company(
        id="company", revision=3, updated_by="EXAMPLE\\jsmith", updated_at=T0 - 3600,
        time_server="time.example.local", time_server_fallback="192.0.2.123",
        countries=[m.Country(name="USA", aliases=["US", "U.S.A.", "United States"], active=False)],
        inks=[m.Ink(name="Example Black Plastic", cartridge="plastic", colour="black")],
        lot_min_digits=5, lot_max_digits=7, free_text_max=20, admin_group="Example Admins",
        admin_group_sid=ADMIN_SID, sender_group="Example Senders", sender_group_sid=SENDER_SID,
        standard_fonts=["Arial", "CourierN"], bag_counter="MSCNT")
    line = m.Line(id="ln_1a2b3c4d5e", revision=2, updated_by="EXAMPLE\\jsmith", updated_at=T0, name="Line 1",
                  code="L1", cartridge="plastic", notify_user="EXAMPLE\\jsmith", active=False)
    machine = m.Machine(id="mc_5d1e2f3a4b", revision=5, updated_by="EXAMPLE\\akim", updated_at=T0,
                        name="Line 1 Bagger 1", code="B1", line_id=line.id, printer_id="pr_9c8b7a6d5e",
                        placement="encoder", head_distance_mm=412.0, encoder_wheel_mm=56.0, encoder_ppr=5000,
                        print_speed=2400, rotate=True, mirror=True, seconds_per_bag=3.0, active=False)
    calibration = m.Calibration(id=machine.id, revision=1, updated_by="EXAMPLE\\jsmith", updated_at=T0,
                                machine_id=machine.id, spacing_sent_mm=250, spacing_measured_mm=300.5,
                                spacing_by="EXAMPLE\\jsmith", spacing_at=T0 - 60, rough_head_distance_mm=400.0,
                                head_print_sent_mm=515, head_print_measured_mm=317.0, head_distance_mm=412.0,
                                head_by="EXAMPLE\\akim", head_at=T0)
    printer = m.Printer(id="pr_9c8b7a6d5e", revision=1, updated_by="EXAMPLE\\jsmith", updated_at=T0,
                        name="Printer 1", host="192.0.2.10", port=8080, uid=1109,
                        token="SCRAMBLED-TOKEN-EXAMPLE", active=False)
    product = m.Product(id="pd_7e6f5a4b3c", revision=1, updated_by="EXAMPLE\\jsmith", updated_at=T0,
                        name="Example Co 2lb Onion", customer="Example Co", active=False)
    file_ref = m.FileRef(kind="front_photo", name="front.jpg",
                         path="films/fm_3f9a1c22b0/files/0e475da7_front.jpg", sha256="0e475da7" * 8,
                         size=2481337, added_by="EXAMPLE\\jsmith", added_at=T0)
    film = m.Film(id="fm_3f9a1c22b0", revision=4, updated_by="EXAMPLE\\jsmith", updated_at=T0,
                  product_id=product.id, code="EXON2", artwork_revision="Rev C", status="active", files=[file_ref],
                  side="front", cartridge="paper", ink_colour="white", length_mm=610.0, position_mm=150.0,
                  delay_ms=480.0, print_length_mm=40.0, font=3, size=5, resolution=1, bold=2, width=2,
                  origin_preprinted=True, preprinted_origin="USA", print_text_ids=["pt_0b1c2d3e4f"],
                  default_print_text_id="pt_0b1c2d3e4f")
    item = m.PrintTextItem(id="i1", kind="best_by_date", before="Best by ", after=".", text="x",
                           date_format="MM/DD/YY", offset=2, offset_unit="months", label="Which?", max_len=10,
                           required=False, placement="below", gap=4)
    print_text = m.PrintText(id="pt_0b1c2d3e4f", revision=7, updated_by="EXAMPLE\\jsmith", updated_at=T0,
                             name="Packed on + origin", code="PKO", status="active",
                             items=[item, m.PrintTextItem(id="i2", kind="origin")])
    adjustment = m.MachineFilmAdjustment(id=m.adjustment_id(machine.id, film.id), revision=2,
                                         updated_by="EXAMPLE\\jsmith", updated_at=T0, machine_id=machine.id,
                                         film_id=film.id, position_mm=-3.0, delay_ms=20.0)
    data = m.HxData(company=company, lines={line.id: line}, machines={machine.id: machine},
                    printers={printer.id: printer}, adjustments={adjustment.id: adjustment},
                    calibrations={machine.id: calibration}, products={product.id: product},
                    films={film.id: film}, print_texts={print_text.id: print_text}, source="local",
                    loaded_at=T0, reached_at=T0 - 100)
    choice = m.LineChoice(line_id=line.id, machine_ids=[machine.id], product_id=product.id, film_id=film.id,
                          print_text_id=print_text.id, lot="4417", countries=["USA", "Canada"],
                          free_text={"i3": "Fresh"})
    request = m.SendRequest(send_id="sd_8a7b6c5d4e", sender="EXAMPLE\\jsmith", computer="FLOOR01",
                            created_at=T0, lines=[choice])
    settings = m.PrintSettings(encoder=True, position_mm=348, delay_us=480000, speed=2400, rotate=True,
                               mirror=True, resolution=1, bold=2, width=2, quantity=2, invert=True,
                               auto_repeat=True)
    position = m.PositionWorking(placement="encoder", film_value=150.0, adjustment=-3.0, head_distance_mm=412.0,
                                 film_length_mm=610.0, sent=345, wrapped=True,
                                 text="(150 - 412 - 3) mod 610 = 345 mm", warning=MSG)
    step = m.TestStep(index=1, label="P150 | B1", position_mm=150.0, delay_ms=480.0, bold=1, width=1,
                      resolution=0, fonts=[(2, 3), (2, 4)])
    machine_plan = m.MachinePlan(machine=machine, line=line, printer=printer, product=product, film=film,
                                 print_text=print_text, adjustment=adjustment, lot="4417", countries=["USA"],
                                 free_text={"i3": "Fresh"}, origin_line="Product of USA",
                                 bag_text="Packed on 10/29/26 Product of USA", settings=settings,
                                 position=position, warnings=[MSG], test_step=step)
    plan = m.SendPlan(send_id=request.send_id, request=request, machines=[machine_plan], from_local_copy=True,
                      data_as_of=T0 - 100)
    ranges = m.TestRanges(vary="position", start=100.0, stop=200.0, step=25.0, bold=[1, 2], width=[1, 3],
                          resolution=[0, 2], fonts=[(2, 3), (3, 4)])
    test_plan = m.TestPlan(run_id="tp_1a2b3c4d5e", kind="spacing", base=machine_plan, steps=[step],
                           bags_per_step=3, timed=True)
    design_item = m.DesignItem(tag="CTEXT", pairs=[("TXT", "Packed on "), ("LANG", "1"), ("LANG", "2")])
    design = m.MessageDesign(slot=9, name="EXON2PKO7AB12X9", empty=True, items=[design_item],
                             settings={"PRNSPD": "2400", "ENCST": "1"}, width=256, height=160, black_pixels=1234,
                             read_at=T0, tn="42", xid=7)
    built = m.BuiltMessage(slot=9, name="EXON2PKO7AB12X9", content_code="AB12",
                           commands=[m.Command.set("CRTMSG", "EXON2PKO7AB12X9"), m.Command.query("PRIN")],
                           expected_items=[design_item], expected_settings={"ENCST": "1"},
                           origin_line="Product of USA", bag_text="Packed on 10/29/26 Product of USA")
    running = m.RunningMessage(slot=10, name="EXON2PKO7AB12XA", fingerprint="fd2e" * 16,
                               origin_line="Product of USA and Canada", read_at=T0, preview_sha256="85f0" * 16)
    snapshot = m.MachineSnapshot(code="printing", colour=m.GREEN, headline=Msg("Printing"), since=T0,
                                 detail=[MSG])
    alert = m.Alert(id="drift_mc_5d1e2f3a4b_1", machine_id=machine.id, line_id=line.id, kind="drift", sound=True,
                    msg=MSG, started_at=T0, recipients=["user:EXAMPLE\\jsmith", "floor"], active=False,
                    colour=m.RED, ref="sd_8a7b6c5d4e", acked_by="EXAMPLE\\akim", acked_at=T0 + 5,
                    ended_at=T0 + 60, waiting=["user:EXAMPLE\\akim"])
    lock = m.LockInfo(printer_id=printer.id, token="a3f9c2e17b4d8e01", user="EXAMPLE\\jsmith", computer="FLOOR01",
                      pid=4242, purpose="test", line_id=line.id, machine_id=machine.id, run_id="tp_1a2b3c4d5e",
                      counter=4, acquired_at=T0, note={"step": 12, "of": 40})
    heartbeat = m.HeartbeatInfo(key="FLOOR01_EXAMPLE_jsmith_4242", computer="FLOOR01", user="EXAMPLE\\jsmith",
                                pid=4242, version="v2.0.0", counter=3311, written_at=T0, can_poll=True,
                                last_printer_reach=T0 - 1, polling=[printer.id], monitor_open=True,
                                floor_board=True)
    poll = m.PollStatus(machine_id=machine.id, printer_id=printer.id, poller="FLOOR01_EXAMPLE_jsmith_4242",
                        counter=18734, written_at=T0, paused_for_lock="a3f9c2e17b4d8e01",
                        yielded_to="FLOOR02_EXAMPLE_akim_77", reachable=True, failures=2, last_answer_at=T0 - 1,
                        printing=True, message_name="EXON2PKO7AB12X9", bag_count=436512, bags_recent=118,
                        ink_name="Example Black Plastic", ink_level=3, head_temp_c=26, clock_offset_s=1.4,
                        running=running, daily={"fonts": ["Arial"], "at": T0}, state=snapshot, alerts=[alert],
                        foreign_client=True, problem="wrong_printer", expected_counter=42, reread_seen="tok1",
                        unreachable_since=T0 - 30, running_ok_at=T0 - 35, running_unchecked=True,
                        dst={"change": T0 - 600, "done": True}, standalone=True)
    failure = m.FailureInfo(step=m.STEP_SWITCH, msg=MSG, after_switch=True, at=T0)
    expected = m.ExpectedState(machine_id=machine.id, counter=42, written_at=T0, written_by="EXAMPLE\\akim",
                               computer="FLOOR02", mode="switch_pending", printing_expected=True,
                               send_id="sd_8a7b6c5d4e", message_name="EXON2PKO7AB12X9", content_code="AB12",
                               fingerprint="fd2e" * 16, slot=9, origin_line="Product of USA",
                               previous_message_name="OLD", previous_fingerprint="0a0b" * 16, result=m.AMBER,
                               failure=failure, notes=["pc_time", "no_lock"], confirmed=True,
                               confirmed_by="EXAMPLE\\jsmith", confirmed_at=T0 + 1, rejected=True,
                               resume_blocked=True, left_by="EXAMPLE\\jsmith", left_at=T0 + 2, reread_token="tok1",
                               test={"run_id": "tp_1a2b3c4d5e", "kind": "test", "by": "EXAMPLE\\jsmith",
                                     "step": 12, "of": 40},
                               sender="EXAMPLE\\jsmith", stop_reason="rejected", stopped_by="EXAMPLE\\jsmith",
                               stopped_at=T0 + 3)
    line_status = m.LineStatus(line_id=line.id, counter=9, written_at=T0, written_by="EXAMPLE\\jsmith",
                               send_id="sd_8a7b6c5d4e", sent_at=T0 - 9, product_id=product.id, film_id=film.id,
                               film_revision=4, print_text_id=print_text.id, print_text_revision=7, lot="4417",
                               countries=["USA", "Canada"], origin_line="Product of USA and Canada",
                               machines={machine.id: m.AMBER}, confirmed=True, confirmed_by="EXAMPLE\\jsmith",
                               stopped_needs_send=True, sender="EXAMPLE\\jsmith")
    machine_result = m.MachineResult(machine_id=machine.id, colour=m.RED, ok=False, step=m.STEP_CONFIRM_1, msg=MSG,
                                     after_switch=True, slot=9, message_name="EXON2PKO7AB12X9", content_code="AB12",
                                     fingerprint="fd2e" * 16, origin_line="Product of USA", readbacks=[design],
                                     previous=running, previous_confirmed=True, counts_before=100,
                                     counts_after=120, notes=["film_stop"])
    send_result = m.SendResult(send_id="sd_8a7b6c5d4e", sender="EXAMPLE\\jsmith", computer="FLOOR01",
                               started_at=T0, finished_at=T0 + 9, machines=[machine_result], time_source="pc",
                               from_local_copy=True, data_as_of=T0 - 100, log_name="send~sd_8a7b6c5d4e.json",
                               log_queued=True)
    action_result = m.ActionResult(action_id="ac_1a2b3c4d5e", kind="stop", machines=[machine_result])
    test_result = m.TestResult(run_id="tp_1a2b3c4d5e", kind="test", steps_done=3, of=4, ok=True, msg=MSG,
                               cleared=True, printing_off=True)
    machine_view = m.MachineView(machine_id=machine.id, line_id=line.id, name="Line 1 Bagger 1", colour=m.AMBER,
                                 code="not_confirmed", headline=Msg("Send not confirmed"), details=[MSG],
                                 origin_line="Product of USA", message_name="EXON2PKO7AB12X9", checked_age_s=3.0,
                                 monitored=True, printing=True, alerts=[alert], unacked_sound=True, can_stop=True,
                                 can_resume=True, needs_send=True, confirm_pending_by="EXAMPLE\\jsmith",
                                 confirm_send_id="sd_8a7b6c5d4e", test={"by": "EXAMPLE\\jsmith", "step": 1, "of": 4},
                                 has_preview=True)
    line_view = m.LineView(line_id=line.id, name="Line 1", colour=m.AMBER, headline=Msg("Send not confirmed"),
                           machines=[machine_view], origins_differ=True, monitored=True, checked_age_s=3.0)
    board = m.BoardView(lines=[line_view], share_ok=False, standalone=True, next_check_s=18.0, generated_at=T0)
    found = [
        company, line, machine, calibration, printer, product, file_ref, film, item, print_text, adjustment,
        data, choice, request, settings, position, step, machine_plan, plan, ranges, test_plan, design_item,
        design, built, running, snapshot, alert, lock, heartbeat, poll, failure, expected, line_status,
        machine_result, send_result, action_result, test_result, machine_view, line_view, board,
        company.countries[0], company.inks[0],
        m.Record(id="company", revision=1, updated_by="EXAMPLE\\jsmith", updated_at=T0),
        dataclasses.replace(m.TIMINGS.scaled(0.5), web_repeat_max=3, unreachable_after=4),
        m.Identity(user="EXAMPLE\\jsmith", computer="FLOOR01", sids=frozenset({ADMIN_SID, "S-1-1-0"}), pid=4242),
        m.Command("PRINT", "<PRINT>1</PRINT>", True),
        m.Difference("item 2 (CTEXT)", "SIZE", "4", "2"),
        m.MessageName("EXON2", "PKO", 7, "AB12", 9),
        m.Ack(alert_id=alert.id, by="EXAMPLE\\akim", computer="FLOOR02", at=T0 + 5),
        m.ResumeCheck(machine_id=machine.id, design=design, origin_line="Product of USA", allowed=False, msg=MSG),
        m.SendEvent(send_id="sd_8a7b6c5d4e", machine_id=machine.id, step=m.STEP_BUILD, status="failed", msg=MSG,
                    design=design, at=T0),
        m.SendFinished(result=send_result),
        m.ActionEvent(action_id="ac_1a2b3c4d5e", kind="resume", machine_id=machine.id, status="ok", msg=MSG,
                      design=design, at=T0),
        m.ActionFinished(result=action_result),
        m.TestEvent(run_id="tp_1a2b3c4d5e", step_index=2, of=4, status="printing", msg=MSG, design=design,
                    bags=5, at=T0),
        m.TestFinished(result=test_result),
        m.BoardUpdate(board=board),
        m.DataUpdate(data=data),
        m.ShareHealth(ok=False, last_reached=T0),
        m.PollerUpdate(mode="standalone", polling=[printer.id], next_check_s=18.0),
        m.FloorRow(line_id=line.id, machine_id=machine.id, colour=m.RED, line_name="Line 1",
                   machine_name="Line 1 Bagger 1", origin_line="Product of USA", status=Msg("Drift"),
                   checked_age_s=None),
    ]
    return {type(x): x for x in found}


def model_dataclasses():
    return {obj for obj in vars(m).values()
            if isinstance(obj, type) and dataclasses.is_dataclass(obj) and obj.__module__ == "hx_model"}


def without_bytes(value):
    """to_json output with bytes values left out, as callers do before writing JSON."""
    if isinstance(value, dict):
        return {k: without_bytes(v) for k, v in value.items() if not isinstance(v, bytes)}
    if isinstance(value, list):
        return [without_bytes(v) for v in value]
    return value


def via_json(obj):
    return json.loads(json.dumps(without_bytes(m.to_json(obj)), allow_nan=False))


def film_dict(**changes):
    d = {"id": "fm_3f9a1c22b0", "product_id": "pd_7e6f5a4b3c", "code": "EXON2"}
    d.update(changes)
    return d


class RoundTripTests(unittest.TestCase):
    def test_every_dataclass_has_an_example(self):
        self.assertEqual({c.__name__ for c in model_dataclasses()} - {c.__name__ for c in examples()}, set())

    def test_examples_leave_no_field_at_its_default(self):
        bytes_fields = {("MessageDesign", "preview_bmp"), ("MachineResult", "previous_preview")}
        for cls, obj in examples().items():
            for f in dataclasses.fields(cls):
                if (cls.__name__, f.name) in bytes_fields:
                    continue
                if f.default is not dataclasses.MISSING:
                    default = f.default
                elif f.default_factory is not dataclasses.MISSING:
                    default = f.default_factory()
                else:
                    continue
                self.assertNotEqual(getattr(obj, f.name), default, f"{cls.__name__}.{f.name}")

    def test_round_trip_through_json(self):
        for cls, obj in examples().items():
            with self.subTest(cls=cls.__name__):
                self.assertEqual(m.from_json(cls, via_json(obj)), obj)

    def test_bytes_round_trip_in_memory(self):
        bmp = b"BM" + bytes(range(256))
        design = m.MessageDesign(slot=9, name="", empty=False, preview_bmp=bmp)
        result = m.MachineResult(machine_id="mc_5d1e2f3a4b", colour=m.RED, ok=False, step=m.STEP_BUILD,
                                 readbacks=[design], previous_preview=bmp)
        self.assertEqual(m.from_json(m.MachineResult, m.to_json(result)), result)
        with self.assertRaises(TypeError):
            json.dumps(m.to_json(design))      # callers store pictures as base64 themselves
        with self.assertRaises(ValueError):
            m.from_json(m.MessageDesign, {"slot": 9, "name": "", "empty": False, "preview_bmp": "Qk0="})

    def test_to_json_shape(self):
        film = examples()[m.Film]
        d = m.to_json(film)
        self.assertEqual(list(d), [f.name for f in dataclasses.fields(m.Film)])   # field order
        self.assertEqual(list(d)[:4], ["id", "revision", "updated_by", "updated_at"])
        self.assertEqual(d["files"][0]["kind"], "front_photo")
        step = m.to_json(examples()[m.TestStep])
        self.assertEqual(step["fonts"], [[2, 3], [2, 4]])                       # tuples become lists
        self.assertEqual(m.to_json(examples()[m.Timings])["takeover_s"], [7.5, 10.0])
        ident = m.to_json(examples()[m.Identity])
        self.assertEqual(ident["sids"], sorted([ADMIN_SID, "S-1-1-0"]))
        self.assertNotIn("key", ident)
        self.assertEqual(m.to_json(MSG), MSG.to_json())
        self.assertEqual(m.to_json(examples()[m.MachineSnapshot])["headline"], {"text": "Printing", "values": {}})
        with self.assertRaises(TypeError):
            m.to_json({"not": "a dataclass"})

    def test_storage_example_files_load(self):
        line = m.from_json(m.Line, {"format_version": 1, "file_type": "line", "id": "ln_1a2b3c4d5e",
                                    "revision": 2, "updated_by": "EXAMPLE\\jsmith", "updated_at": 1793268000.0,
                                    "name": "Line 1", "code": "L1", "cartridge": "plastic",
                                    "notify_user": "EXAMPLE\\jsmith", "active": True})
        self.assertEqual((line.id, line.revision, line.code), ("ln_1a2b3c4d5e", 2, "L1"))
        machine = m.from_json(m.Machine, {"id": "mc_5d1e2f3a4b", "name": "Line 1 Bagger 1", "code": "B1",
                                          "line_id": "ln_1a2b3c4d5e", "printer_id": "pr_9c8b7a6d5e",
                                          "placement": "delay", "head_distance_mm": None, "print_speed": 2400,
                                          "seconds_per_bag": 2.4})
        self.assertIsNone(machine.head_distance_mm)
        self.assertEqual(machine.print_speed, 2400)
        self.assertIsNone(machine.encoder_ppr)
        self.assertTrue(machine.active)


class TypeRuleTests(unittest.TestCase):
    def assertRefused(self, cls, d, where):
        with self.assertRaises(ValueError) as caught:
            m.from_json(cls, d)
        self.assertIn(where, str(caught.exception))

    def test_unknown_keys_ignored_and_missing_keys_take_defaults(self):
        film = m.from_json(m.Film, film_dict(format_version=1, file_type="film", colour_of_the_sky="blue"))
        self.assertEqual(film, m.Film(id="fm_3f9a1c22b0", product_id="pd_7e6f5a4b3c", code="EXON2"))
        self.assertEqual(film.status, "development")
        self.assertEqual(film.files, [])
        self.assertIsNone(film.length_mm)
        company = m.from_json(m.Company, {"id": "company"})
        self.assertEqual(company.standard_fonts, list(m.STANDARD_FONTS))
        self.assertEqual(company.bag_counter, "PMCNT")
        company.standard_fonts.append("Extra")                # each record gets its own list
        self.assertEqual(m.Company(id="company").standard_fonts, list(m.STANDARD_FONTS))

    def test_missing_required_field(self):
        self.assertRefused(m.Film, {"id": "fm_3f9a1c22b0", "code": "EXON2"}, "Film.product_id")
        self.assertRefused(m.Printer, {"id": "pr_1", "name": "Printer 1", "host": "192.0.2.10"}, "Printer.uid")

    def test_not_an_object(self):
        for bad in (None, [], "film", 5):
            with self.assertRaises(ValueError):
                m.from_json(m.Film, bad)

    def test_rule_1_int_for_float(self):
        film = m.from_json(m.Film, film_dict(length_mm=610, position_mm=150, delay_ms=480.5))
        self.assertEqual(film.length_mm, 610.0)
        self.assertIs(type(film.length_mm), float)
        self.assertIs(type(film.position_mm), float)
        adj = m.from_json(m.MachineFilmAdjustment, {"id": "a", "machine_id": "mc", "film_id": "fm",
                                                    "position_mm": -3, "delay_ms": 20})
        self.assertEqual((adj.position_mm, adj.delay_ms), (-3.0, 20.0))
        self.assertIs(type(adj.delay_ms), float)

    def test_rule_2_bool_is_not_a_number(self):
        self.assertRefused(m.Film, film_dict(length_mm=True), "Film.length_mm")
        self.assertRefused(m.Film, film_dict(font=True), "Film.font")
        self.assertRefused(m.Film, film_dict(font=2.0), "Film.font")        # an int field takes only ints
        self.assertRefused(m.Film, film_dict(font="2"), "Film.font")
        self.assertRefused(m.Film, film_dict(length_mm="610"), "Film.length_mm")
        self.assertRefused(m.LockInfo, {"printer_id": "pr", "token": "t", "user": "u", "computer": "c",
                                        "pid": 1, "purpose": "send", "counter": False}, "LockInfo.counter")

    def test_bool_fields_take_only_bools(self):
        self.assertRefused(m.Line, {"id": "ln", "name": "Line 1", "code": "L1", "cartridge": "paper",
                                    "active": 1}, "Line.active")
        self.assertRefused(m.Film, film_dict(origin_preprinted="no"), "Film.origin_preprinted")

    def test_rule_3_str_only(self):
        self.assertRefused(m.Line, {"id": "ln", "name": 5, "code": "L1", "cartridge": "paper"}, "Line.name")
        self.assertRefused(m.Film, film_dict(code=["EXON2"]), "Film.code")

    def test_rule_4_lists_and_tuples(self):
        step = m.from_json(m.TestStep, {"index": 1, "label": "P150 | B1", "bold": 1, "width": 1,
                                        "resolution": 0, "fonts": [[2, 3], [2, 4]]})
        self.assertEqual(step.fonts, [(2, 3), (2, 4)])
        self.assertIsInstance(step.fonts[0], tuple)
        item = m.from_json(m.DesignItem, {"tag": "CTEXT", "pairs": [["TXT", "Lot "], ["LANG", "1"]]})
        self.assertEqual(item.pairs, [("TXT", "Lot "), ("LANG", "1")])
        self.assertIsInstance(item.pairs[1], tuple)
        timings = m.from_json(m.Timings, {"takeover_s": [1, "anything"]})   # a bare tuple hint isn't checked
        self.assertEqual(timings.takeover_s, (1, "anything"))
        self.assertIsInstance(timings.takeover_s, tuple)
        self.assertRefused(m.TestStep, {"index": 1, "label": "x", "bold": 1, "width": 1, "resolution": 0,
                                        "fonts": [[2, "3"]]}, "TestStep.fonts[0][1]")
        self.assertRefused(m.TestStep, {"index": 1, "label": "x", "bold": 1, "width": 1, "resolution": 0,
                                        "fonts": [[2, 3, 4]]}, "TestStep.fonts[0]")
        self.assertRefused(m.TestStep, {"index": 1, "label": "x", "bold": 1, "width": 1, "resolution": 0,
                                        "fonts": "2,3"}, "TestStep.fonts")
        self.assertRefused(m.HeartbeatInfo, {"key": "k", "computer": "c", "user": "u", "pid": 1, "version": "v",
                                             "polling": ["pr_1", 2]}, "HeartbeatInfo.polling[1]")

    def test_rule_4_list_and_dict_elements(self):
        self.assertRefused(m.Company, {"id": "company", "countries": [{"name": 5}]}, "Country.name")
        self.assertRefused(m.Company, {"id": "company", "countries": {"name": "USA"}}, "Company.countries")
        status = m.from_json(m.LineStatus, {"line_id": "ln", "machines": {"mc_1": "amber", "mc_2": "red"}})
        self.assertEqual(status.machines, {"mc_1": "amber", "mc_2": "red"})
        self.assertRefused(m.LineStatus, {"line_id": "ln", "machines": {"mc_1": 5}}, "LineStatus.machines")
        data = m.from_json(m.HxData, {"company": {"id": "company"},
                                      "lines": {"ln_1": {"id": "ln_1", "name": "Line 1", "code": "L1",
                                                         "cartridge": "paper"}}})
        self.assertIsInstance(data.lines["ln_1"], m.Line)
        self.assertRefused(m.HxData, {"company": {"id": "company"}, "lines": {"ln_1": {"id": "ln_1"}}},
                           "Line.name")

    def test_rule_5_none_only_for_optional_fields(self):
        film = m.from_json(m.Film, film_dict(length_mm=None, print_length_mm=None))
        self.assertIsNone(film.length_mm)
        poll = m.from_json(m.PollStatus, {"machine_id": "mc", "printer_id": "pr", "poller": "k",
                                          "running": None, "printing": None, "state": None})
        self.assertIsNone(poll.running)
        self.assertRefused(m.Film, film_dict(code=None), "Film.code")
        self.assertRefused(m.Line, {"id": "ln", "name": "Line 1", "code": "L1", "cartridge": "paper",
                                    "notify_user": None}, "Line.notify_user")
        self.assertRefused(m.PollStatus, {"machine_id": "mc", "printer_id": "pr", "poller": "k",
                                          "alerts": None}, "PollStatus.alerts")
        self.assertRefused(m.PollStatus, {"machine_id": "mc", "printer_id": "pr", "poller": "k",
                                          "daily": None}, "PollStatus.daily")

    def test_rule_6_nested_dataclasses_and_msgs(self):
        poll = m.from_json(m.PollStatus, {
            "machine_id": "mc", "printer_id": "pr", "poller": "k",
            "running": {"slot": 9, "name": "N", "fingerprint": "f", "origin_line": "Product of USA",
                        "read_at": 1793275170},
            "state": {"code": "printing", "colour": "green", "since": 1, "detail": [],
                      "headline": {"text": "{machine} is set for {ink} ink",
                                   "values": {"machine": "Line 1 Bagger 1", "ink": {"text": "black", "values": {}}}}}})
        self.assertIsInstance(poll.running, m.RunningMessage)
        self.assertEqual(poll.running.read_at, 1793275170.0)
        self.assertEqual(poll.state.headline, MSG)
        self.assertRefused(m.PollStatus, {"machine_id": "mc", "printer_id": "pr", "poller": "k",
                                          "running": "slot 9"}, "PollStatus.running")
        self.assertRefused(m.PollStatus, {"machine_id": "mc", "printer_id": "pr", "poller": "k",
                                          "running": {"slot": 9}}, "RunningMessage.name")
        self.assertRefused(m.Ack, {"alert_id": "a", "by": "b", "computer": "c", "at": "now"}, "Ack.at")
        self.assertRefused(m.FailureInfo, {"step": "build", "msg": "Broken", "after_switch": False, "at": 1.0},
                           "FailureInfo.msg")
        self.assertRefused(m.FailureInfo, {"step": "build", "msg": {"text": 5}, "after_switch": False, "at": 1.0},
                           "FailureInfo.msg")

    def test_rule_7_plain_dict_and_list_keep_their_contents(self):
        daily = {"fonts": ["Arial", 2, None], "encoder": {"ENWHD": 56, "ok": True}, "at": 1.5}
        poll = m.from_json(m.PollStatus, {"machine_id": "mc", "printer_id": "pr", "poller": "k", "daily": daily})
        self.assertEqual(poll.daily, daily)
        lock = m.from_json(m.LockInfo, {"printer_id": "pr", "token": "t", "user": "u", "computer": "c",
                                        "pid": 1, "purpose": "test", "note": {"step": 12, "of": 40}})
        self.assertEqual(lock.note, {"step": 12, "of": 40})
        expected = m.from_json(m.ExpectedState, {"machine_id": "mc", "test": {"run_id": "tp", "step": 1}})
        self.assertEqual(expected.test, {"run_id": "tp", "step": 1})
        self.assertIsNone(m.from_json(m.ExpectedState, {"machine_id": "mc", "test": None}).test)
        # The container itself must still be the right kind
        self.assertRefused(m.PollStatus, {"machine_id": "mc", "printer_id": "pr", "poller": "k", "daily": []},
                           "PollStatus.daily")

    def test_rule_8_errors_name_class_and_field(self):
        with self.assertRaises(ValueError) as caught:
            m.from_json(m.Film, film_dict(length_mm=True))
        self.assertIn("Film.length_mm", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            m.from_json(m.Machine, {"id": "mc", "name": "x", "code": "B1", "line_id": "ln", "printer_id": "pr",
                                    "placement": "delay", "encoder_ppr": 5000.5})
        self.assertIn("Machine.encoder_ppr", str(caught.exception))

    def test_identity_from_json(self):
        ident = m.from_json(m.Identity, {"user": "EXAMPLE\\jsmith", "computer": "FLOOR01",
                                         "sids": ["S-1-1-0", "S-1-1-0"], "pid": 7})
        self.assertEqual(ident.sids, frozenset({"S-1-1-0"}))

    def test_msg_from_json_directly(self):
        self.assertEqual(m.from_json(Msg, MSG.to_json()), MSG)


class HelperTests(unittest.TestCase):
    def test_new_id(self):
        ids = {m.new_id("fm") for _ in range(100)}
        self.assertEqual(len(ids), 100)
        for i in ids:
            self.assertRegex(i, r"^fm_[0-9a-f]{10}$")
        self.assertRegex(m.new_id("sd"), r"^sd_[0-9a-f]{10}$")

    def test_adjustment_id(self):
        self.assertEqual(m.adjustment_id("mc_5d1e2f3a4b", "fm_3f9a1c22b0"), "mc_5d1e2f3a4b__fm_3f9a1c22b0")

    def test_same_user(self):
        self.assertTrue(m.same_user("EXAMPLE\\jsmith", "example\\JSmith"))
        self.assertFalse(m.same_user("EXAMPLE\\jsmith", "EXAMPLE\\akim"))
        self.assertTrue(m.same_user("", ""))

    def test_line_machines(self):
        def machine(mid, name, line="ln_1", active=True):
            return m.Machine(id=mid, name=name, code="B1", line_id=line, printer_id="pr", placement="delay",
                             active=active)

        machines = [machine("mc_1", "Line 1 Bagger 10"), machine("mc_2", "Line 1 Bagger 2"),
                    machine("mc_3", "line 1 bagger 3"), machine("mc_4", "Line 1 Bagger 1", active=False),
                    machine("mc_5", "Line 2 Bagger 1", line="ln_2")]
        data = m.HxData(company=m.Company(id="company"), machines={x.id: x for x in machines})
        self.assertEqual([x.id for x in data.line_machines("ln_1")], ["mc_2", "mc_3", "mc_1"])
        self.assertEqual([x.id for x in data.line_machines("ln_2")], ["mc_5"])
        self.assertEqual(data.line_machines("ln_9"), [])

    def test_adjustment_lookup(self):
        adj = m.MachineFilmAdjustment(id=m.adjustment_id("mc_1", "fm_1"), machine_id="mc_1", film_id="fm_1",
                                      position_mm=-3.0)
        data = m.HxData(company=m.Company(id="company"), adjustments={adj.id: adj})
        self.assertIs(data.adjustment("mc_1", "fm_1"), adj)
        self.assertIsNone(data.adjustment("mc_1", "fm_2"))

    def test_send_plan_restricted(self):
        ex = examples()
        base = ex[m.MachinePlan]

        def plan_for(machine_id, line_id):
            p = m.MachinePlan(**{f.name: getattr(base, f.name) for f in dataclasses.fields(m.MachinePlan)})
            p.machine = dataclasses.replace(base.machine, id=machine_id, line_id=line_id)
            return p

        request = m.SendRequest(send_id="sd_0000000001", sender="EXAMPLE\\jsmith", computer="FLOOR01",
                                created_at=T0, lines=[
                                    m.LineChoice(line_id="ln_1", machine_ids=["mc_1", "mc_2"], product_id="pd",
                                                 film_id="fm", print_text_id="pt", lot="4417"),
                                    m.LineChoice(line_id="ln_2", machine_ids=["mc_3"], product_id="pd",
                                                 film_id="fm", print_text_id="pt", lot="5000")])
        plan = m.SendPlan(send_id="sd_0000000001", request=request,
                          machines=[plan_for("mc_1", "ln_1"), plan_for("mc_2", "ln_1"), plan_for("mc_3", "ln_2")],
                          from_local_copy=True, data_as_of=T0 - 100)
        again = plan.restricted(["mc_2"])
        self.assertRegex(again.send_id, r"^sd_[0-9a-f]{10}$")
        self.assertNotEqual(again.send_id, plan.send_id)
        self.assertEqual(again.request.send_id, again.send_id)
        self.assertEqual([p.machine.id for p in again.machines], ["mc_2"])
        self.assertEqual([(c.line_id, c.machine_ids) for c in again.request.lines], [("ln_1", ["mc_2"])])
        self.assertEqual((again.request.created_at, again.request.sender, again.request.computer),
                         (T0, "EXAMPLE\\jsmith", "FLOOR01"))
        self.assertEqual((again.from_local_copy, again.data_as_of), (True, T0 - 100))
        self.assertEqual(again.machines[0], plan.machines[1])          # a copy, nothing changed
        self.assertIsNot(again.machines[0], plan.machines[1])
        # The original plan is untouched
        self.assertEqual(plan.send_id, "sd_0000000001")
        self.assertEqual(plan.request.send_id, "sd_0000000001")
        self.assertEqual([c.machine_ids for c in plan.request.lines], [["mc_1", "mc_2"], ["mc_3"]])
        self.assertEqual(len(plan.machines), 3)
        both = plan.restricted(["mc_3", "mc_1"])                       # the plan's order is kept
        self.assertEqual([p.machine.id for p in both.machines], ["mc_1", "mc_3"])
        self.assertEqual([c.machine_ids for c in both.request.lines], [["mc_1"], ["mc_3"]])


class IdentityTests(unittest.TestCase):
    def test_key(self):
        ident = m.Identity(user="EXAMPLE\\jsmith", computer="FLOOR01", sids=frozenset(), pid=4242)
        self.assertEqual(ident.key, "FLOOR01_EXAMPLE_jsmith_4242")
        odd = m.Identity(user="EXAMPLE\\j smith@x//y", computer="FLOOR-02.a", sids=frozenset(), pid=7)
        self.assertEqual(odd.key, "FLOOR-02.a_EXAMPLE_j_smith_x_y_7")    # each run becomes one "_"

    def test_sids_become_a_frozenset(self):
        ident = m.Identity(user="u", computer="c", sids=["S-1-1-0", "S-1-1-0"], pid=1)
        self.assertEqual(ident.sids, frozenset({"S-1-1-0"}))
        hash(ident)     # usable as a key
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ident.user = "someone"

    def test_roles(self):
        company = m.Company(id="company", admin_group_sid=ADMIN_SID, sender_group_sid=SENDER_SID)
        admin = m.Identity(user="EXAMPLE\\akim", computer="FLOOR02", sids=frozenset({ADMIN_SID}), pid=1)
        sender = m.Identity(user="EXAMPLE\\jsmith", computer="FLOOR01", sids=frozenset({SENDER_SID, "S-1-1-0"}),
                            pid=2)
        both = m.Identity(user="EXAMPLE\\both", computer="FLOOR01", sids=frozenset({ADMIN_SID, SENDER_SID}), pid=3)
        nobody = m.Identity(user="EXAMPLE\\guest", computer="FLOOR01", sids=frozenset({"S-1-1-0"}), pid=4)
        self.assertEqual((admin.is_admin(company), admin.is_sender(company)), (True, False))
        self.assertEqual((sender.is_admin(company), sender.is_sender(company)), (False, True))
        self.assertEqual((both.is_admin(company), both.is_sender(company)), (True, True))
        self.assertEqual((nobody.is_admin(company), nobody.is_sender(company)), (False, False))
        lower = m.Identity(user="u", computer="c", sids=frozenset({ADMIN_SID.lower()}), pid=5)
        self.assertTrue(lower.is_admin(company))       # SIDs compare without case

    def test_empty_group_sid_means_nobody(self):
        company = m.Company(id="company")
        odd = m.Identity(user="u", computer="c", sids=frozenset({"", "S-1-1-0"}), pid=1)
        self.assertFalse(odd.is_admin(company))
        self.assertFalse(odd.is_sender(company))


class TimingsTests(unittest.TestCase):
    def test_defaults(self):
        t = m.TIMINGS
        self.assertEqual((t.http_timeout_s, t.stale_s, t.lock_stale_s, t.takeover_s), (2.0, 15.0, 60.0, (15.0, 20.0)))
        self.assertEqual((t.web_repeat_max, t.unreachable_after), (5, 3))
        self.assertEqual(t.backoff_s, (5.0, 10.0, 30.0, 60.0))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            t.stale_s = 1.0

    def test_scaled(self):
        fast = m.TIMINGS.scaled(0.05)
        for f in dataclasses.fields(m.Timings):
            normal, scaled = getattr(m.TIMINGS, f.name), getattr(fast, f.name)
            if f.name.endswith("_s") and isinstance(normal, tuple):
                self.assertEqual(scaled, tuple(v * 0.05 for v in normal), f.name)
            elif f.name.endswith("_s"):
                self.assertAlmostEqual(scaled, normal * 0.05, msg=f.name)
            else:
                self.assertEqual(scaled, normal, f.name)       # counts unchanged
        self.assertEqual(fast.takeover_s, (0.75, 1.0))
        self.assertEqual(fast.web_repeat_max, 5)
        self.assertEqual(fast.unreachable_after, 3)
        self.assertEqual(m.TIMINGS.stale_s, 15.0)              # the original is untouched
        self.assertEqual(m.TIMINGS.scaled(1.0), m.TIMINGS)
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                m.TIMINGS.scaled(bad)


class ErrorTests(unittest.TestCase):
    def test_message_and_step(self):
        e = m.NoReply(MSG, step=m.STEP_BUILD)
        self.assertEqual(str(e), "Line 1 Bagger 1 is set for black ink")
        self.assertEqual(str(e), e.msg.english())
        self.assertEqual(e.step, m.STEP_BUILD)
        self.assertEqual(m.HxError(MSG).step, "")
        self.assertIsInstance(e, m.PrinterError)
        self.assertIsInstance(e, m.HxError)

    def test_hierarchy(self):
        for cls in (m.NoReply, m.PrinterBusy, m.AuthFailed, m.BadReply, m.CommandRefused, m.WrongPrinter,
                    m.WebReadFailed):
            self.assertTrue(issubclass(cls, m.PrinterError), cls)
        for cls in (m.ShareUnavailable, m.LockHeld, m.LockLost, m.FormatTooNew, m.RevisionConflict):
            self.assertTrue(issubclass(cls, m.ShareError), cls)
        for cls in (m.PrinterError, m.ShareError, m.ForbiddenCommand, m.PlanError, m.CheckFailed, m.NotAllowed,
                    m.TimeServerError):
            self.assertTrue(issubclass(cls, m.HxError), cls)
        self.assertFalse(issubclass(m.ForbiddenCommand, m.PrinterError))

    def test_extra_attributes(self):
        e = m.BadReply(MSG, tag="PRIN", reply="7", step=m.STEP_CHECK_MACHINE)
        self.assertEqual((e.tag, e.reply, e.step), ("PRIN", "7", m.STEP_CHECK_MACHINE))
        e = m.CommandRefused(MSG, tag="SAVEMSG", code=3)
        self.assertEqual((e.tag, e.code), ("SAVEMSG", 3))
        e = m.WrongPrinter(MSG, expected=1109, got="1110")
        self.assertEqual((e.expected, e.got), (1109, "1110"))
        self.assertEqual(m.ForbiddenCommand(MSG, tag="SETPPARAM").tag, "SETPPARAM")
        info = examples()[m.LockInfo]
        self.assertIs(m.LockHeld(MSG, info=info).info, info)
        e = m.FormatTooNew(MSG, path="status/locks/pr_1.lock", found=2)
        self.assertEqual((e.path, e.found), ("status/locks/pr_1.lock", 2))
        self.assertEqual(m.RevisionConflict(MSG, current=5).current, 5)
        diff = m.Difference("name", "MSGNAM", "A", "B")
        self.assertEqual(m.CheckFailed(MSG, differences=[diff]).differences, [diff])
        self.assertEqual(m.CheckFailed(MSG).differences, [])

    def test_plan_error_problems(self):
        problems = [Msg("First problem"), Msg("Second problem")]
        e = m.PlanError(problems[0], problems=problems)
        self.assertEqual(e.problems, problems)
        self.assertEqual(e.msg, problems[0])
        self.assertEqual(m.PlanError(MSG).problems, [MSG])

    def test_raise_and_catch(self):
        with self.assertRaises(m.PrinterError):
            raise m.PrinterBusy(Msg("Printer 1 is busy."))


class CommandTests(unittest.TestCase):
    def test_query_set_block(self):
        self.assertEqual(m.Command.query("PRIN"), m.Command("PRIN", "<PRIN/>", False))
        self.assertEqual(m.Command.set("PRINT", 1), m.Command("PRINT", "<PRINT>1</PRINT>", True))
        self.assertEqual(m.Command.set("SAVEMSG", 9).xml, "<SAVEMSG>9</SAVEMSG>")
        self.assertEqual(m.Command.set("CRTMSG", "").xml, "<CRTMSG></CRTMSG>")
        cmd = m.Command.block("TD", [("DAY", 29), ("MNTH", 10), ("YEAR", 2026)])
        self.assertEqual(cmd, m.Command("TD", "<TD><DAY>29</DAY><MNTH>10</MNTH><YEAR>2026</YEAR></TD>", True))

    def test_escaping(self):
        self.assertEqual(m.Command.block("CTEXT", [("TXT", "R&D <1>")]).xml,
                         "<CTEXT><TXT>R&amp;D &lt;1&gt;</TXT></CTEXT>")
        self.assertEqual(m.Command.set("CRTMSG", "A&B").xml, "<CRTMSG>A&amp;B</CRTMSG>")
        self.assertEqual(m.Command.set("CRTMSG", "&lt;").xml, "<CRTMSG>&amp;lt;</CRTMSG>")
        self.assertEqual(m.Command.set("CRTMSG", "Año \"5\" 'x'").xml, "<CRTMSG>Año \"5\" 'x'</CRTMSG>")

    def test_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            m.Command.query("PRIN").xml = "<PRINT>1</PRINT>"

    def test_design_item_value(self):
        item = m.DesignItem(tag="CTEXT", pairs=[("LANG", "1"), ("TXT", "Lot "), ("LANG", "2")])
        self.assertEqual(item.value("LANG"), "2")          # the last occurrence
        self.assertEqual(item.value("TXT"), "Lot ")
        self.assertIsNone(item.value("SIZE"))
        self.assertEqual(item.value("SIZE", "3"), "3")


class ConstantTests(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(len(set(m.STEPS)), len(m.STEPS))
        self.assertEqual(m.AFTER_SWITCH_STEPS, m.STEPS[m.STEPS.index(m.STEP_SWITCH):])
        self.assertEqual(m.STEPS[0], m.STEP_CHECK_CHOICE)
        self.assertEqual(m.STEPS[-1], m.STEP_OPERATOR)

    def test_values(self):
        self.assertEqual(m.FORMAT, 1)
        self.assertEqual(m.SLOTS, (9, 10))
        self.assertIn(m.DEFAULT_IDLE_SLOT, m.SLOTS)
        self.assertEqual(list(m.SIZE_RANGE), list(range(1, 11)))
        self.assertEqual(list(m.FONT_INDEXES), [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(m.STANDARD_FONTS), len(m.FONT_INDEXES))
        self.assertEqual(m.COLOURS, ("green", "amber", "red", "grey", "blue"))
        self.assertNotIn("SETPPARAM", m.ALLOWED_WRITE_TAGS)
        self.assertNotIn("CLOT", m.ALLOWED_WRITE_TAGS)
        self.assertFalse(m.BEACON_SUPPORTED)
        self.assertEqual(m.LOG_KINDS, ("hx-sends", "hx-events", "hx-admin"))
        for name in ("ITEM_KINDS", "FILM_STATUSES", "CARTRIDGES", "INK_COLOURS", "PLACEMENTS", "LOCK_PURPOSES"):
            values = getattr(m, name)
            self.assertIsInstance(values, tuple, name)
            self.assertEqual(len(set(values)), len(values), name)

    def test_defaults_follow_the_constants(self):
        film = m.Film(id="fm", product_id="pd", code="X")
        self.assertIn(film.status, m.FILM_STATUSES)
        self.assertIn(film.cartridge, m.CARTRIDGES)
        self.assertIn(film.ink_colour, m.INK_COLOURS)
        self.assertIn(film.font, m.FONT_INDEXES)
        self.assertIn(film.size, m.SIZE_RANGE)
        self.assertIn(film.resolution, m.RESOLUTIONS)
        self.assertEqual(m.ExpectedState(machine_id="mc").mode, "none")


if __name__ == "__main__":
    unittest.main()
