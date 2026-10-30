"""Tests for hx_state: the state table row by row, crash recovery, untrusted facts, the two-reading
rule, alert identity and recipients, views, lines, the board, floor rows, which alerts sound for
whom, and the texts. Pure: no clock, share or printer. Run: python -m unittest -v"""

import dataclasses
import re
import unittest

import hx_message
import hx_state as hs
import i18n
from hx_core import Msg
from hx_model import (AMBER, BLUE, GREEN, GREY, RED, TIMINGS, Ack, Alert, BoardView, Company, ExpectedState,
                      FailureInfo, HxData, Identity, Line, LineView, LockInfo, Machine, MachineSnapshot, MachineView,
                      PollStatus, Printer, RunningMessage)
from hx_state import Facts, Reading

ADMIN_SID = "S-1-5-21-1000-2000-3000-1101"
SENDER_SID = "S-1-5-21-1000-2000-3000-1102"
LINE_ID, MID, PID = "ln_1a2b3c4d5e", "mc_5d1e2f3a4b", "pr_9c8b7a6d5e"
ENC_MID, ENC_PID = "mc_6e2f3a4b5c", "pr_8b7a6d5e4f"
LINE2_ID, MID2, PID2 = "ln_2b3c4d5e6f", "mc_7f3a4b5c6d", "pr_7a6d5e4f3b"
SID, SID2 = "sd_8a7b6c5d4e", "sd_9b8c7d6e5f"
RUN_ID = "tp_1c2d3e4f5a"
JSMITH, AKIM = "EXAMPLE\\jsmith", "EXAMPLE\\akim"
ORIGIN = "Product of USA and Canada"
T0 = 1_793_275_200.0

COMPANY = Company(id="company", bag_counter="PMCNT", admin_group_sid=ADMIN_SID, sender_group_sid=SENDER_SID)
LINE = Line(id=LINE_ID, name="Line 1", code="L1", cartridge="plastic", notify_user=AKIM)
LINE2 = Line(id=LINE2_ID, name="Line 10", code="L10", cartridge="plastic", notify_user="")
PRINTER = Printer(id=PID, name="Printer 1", host="192.0.2.10", uid=1109, token="hx1-token")
ENC_PRINTER = Printer(id=ENC_PID, name="Printer 2", host="192.0.2.11", uid=1109, token="hx1-token")
PRINTER2 = Printer(id=PID2, name="Printer 3", host="192.0.2.12", uid=1109, token="hx1-token")
MACHINE = Machine(id=MID, name="Line 1 Bagger 1", code="B1", line_id=LINE_ID, printer_id=PID, placement="delay")
ENC = Machine(id=ENC_MID, name="Line 1 Bagger 2", code="B2", line_id=LINE_ID, printer_id=ENC_PID, placement="encoder",
              head_distance_mm=412.0, encoder_wheel_mm=56.0, encoder_ppr=5000)
MACHINE2 = Machine(id=MID2, name="Line 10 Bagger 1", code="B1", line_id=LINE2_ID, printer_id=PID2,
                   placement="delay")
DATA = HxData(company=COMPANY, lines={LINE_ID: LINE, LINE2_ID: LINE2},
              machines={MID: MACHINE, ENC_MID: ENC, MID2: MACHINE2},
              printers={PID: PRINTER, ENC_PID: ENC_PRINTER, PID2: PRINTER2})

OLD = RunningMessage(slot=10, name="EXCO1-PKO07-AAAA-K0", fingerprint="a" * 64, origin_line="Product of USA",
                     read_at=T0 - 100, preview_sha256="1" * 64)
NEW = RunningMessage(slot=9, name="EXCO1-PKO07-BBBB-M9", fingerprint="b" * 64, origin_line=ORIGIN,
                     read_at=T0 - 10, preview_sha256="2" * 64)


def expected(mode="running", **kw):
    e = ExpectedState(machine_id=MID, counter=5, written_by=JSMITH, computer="FLOOR01", mode=mode,
                      printing_expected=mode == "running", send_id=SID, message_name=NEW.name, content_code="BBBB",
                      fingerprint=NEW.fingerprint, slot=9, origin_line=ORIGIN, previous_message_name=OLD.name,
                      previous_fingerprint=OLD.fingerprint, confirmed=mode == "running",
                      confirmed_by=JSMITH if mode == "running" else "", sender=JSMITH)
    return dataclasses.replace(e, **kw)


def failure(after_switch=False):
    return FailureInfo(step="confirm_1" if after_switch else "verify_idle",
                       msg=Msg("The read-back differs from what was sent."), after_switch=after_switch, at=T0 - 5)


def facts(**kw):
    f = Facts(reachable=True, printing=True, running=NEW, ink_level=1, clock_offset_s=1.0, message_name=NEW.name,
              bag_count=100, bags_recent=5)
    return dataclasses.replace(f, **kw)


def snapshot(code, since=T0 - 1000):
    return MachineSnapshot(code=code, colour=hs.STATE_CODES[code], headline=Msg(hs.STATE_TEXTS.get(code, code)),
                           since=since)


def alert(kind, *, ref="", at=T0 - 500, **kw):
    k = hs.ALERT_KINDS[kind]
    a = Alert(id=hs.alert_id(MID, kind, ref=ref, at=at), machine_id=MID, line_id=LINE_ID, kind=kind, sound=k.sound,
              msg=Msg(k.text, {"machine": MACHINE.name}), started_at=at, recipients=["user:" + AKIM, "floor"],
              colour=k.colour, ref=ref)
    return dataclasses.replace(a, **kw)


def poll(code="running_confirmed", *, alerts=(), **kw):
    p = PollStatus(machine_id=MID, printer_id=PID, poller="FLOOR02_EXAMPLE_akim_4343", counter=100,
                   reachable=True, printing=True, message_name=NEW.name, bag_count=100, bags_recent=5, ink_level=1,
                   clock_offset_s=1.0, running=NEW, state=snapshot(code), alerts=list(alerts), expected_counter=5,
                   running_ok_at=T0 - 10)
    return dataclasses.replace(p, **kw)


def lock(purpose, **kw):
    l = LockInfo(printer_id=PID, token="a3f9c2e17b4d8e01", user=JSMITH, computer="FLOOR01", pid=4242,
                 purpose=purpose, line_id=LINE_ID, machine_id=MID, run_id=SID, counter=4, acquired_at=T0 - 90)
    return dataclasses.replace(l, **kw)


def classify(f, e, *, prev=None, lk=None, machine=MACHINE, standalone=False, acked=None, data=DATA):
    return hs.classify(f, prev, e, lk, machine, LINE, data, acked=acked, standalone=standalone, at=T0)


def kinds(outcome):
    return sorted(n.kind for n in outcome.alerts)


def reading(**kw):
    r = Reading(at=T0, reachable=True, printing=True, message_name=NEW.name, bag_count=105, ink_level=1,
                clock_offset_s=1.0, running=NEW)
    return dataclasses.replace(r, **kw)


def judge(prev, r, e, lk=None, machine=MACHINE):
    return hs.judge(prev, r, e, lk, machine, LINE, DATA)


def commit_second(prev, r, e, lk=None):
    """A first reading, then a second one that agrees: the change is committed."""
    first = judge(prev, r, e, lk)
    if first.committed:
        return first
    assert first.recheck, "a change that isn't committed asks for a second reading"
    second = dataclasses.replace(r, second=True, at=r.at + 1, pending_code=first.candidate.code,
                                 pending_alerts=first.candidate_alerts)
    j = judge(prev, second, e, lk)
    assert j.committed
    return j


def msgs_in(obj, found=None):
    """Every Msg inside a dataclass, list, dict or Msg (values included)."""
    found = [] if found is None else found
    if isinstance(obj, Msg):
        found.append(obj)
        for v in obj.values.values():
            msgs_in(v, found)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            msgs_in(getattr(obj, f.name), found)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            msgs_in(v, found)
    elif isinstance(obj, dict):
        for v in obj.values():
            msgs_in(v, found)
    return found


# --------------------------------------------------------------------------
# 1. Tables
# --------------------------------------------------------------------------

class TableTests(unittest.TestCase):
    def test_codes_and_colours(self):
        expected_codes = {
            "send_in_progress": BLUE, "switch_pending": BLUE, "test_running": BLUE, "action_in_progress": BLUE,
            "checking": GREY, "not_monitored": GREY, "update_stripes": GREY, "not_set_up": GREY,
            "internal_error": RED, "wrong_printer": RED, "login_failed": RED, "test_orphaned": RED,
            "test_interrupted": RED, "interrupted_unknown": RED, "interrupted_old": RED, "interrupted_new": AMBER,
            "stop_failed": RED, "stop_requested": RED, "failed_after_switch": RED, "failed_before_switch": RED,
            "left_red": RED, "rejected": RED, "printing_on_unexpected": RED, "unreachable_printing": RED,
            "printing_off": RED, "drift": RED, "unknown_while_down": AMBER, "ink_out": RED,
            "cartridge_error": RED, "stopped_needs_send": RED, "test_finished": RED, "off": GREY,
            "not_printing": AMBER, "message_unchecked": AMBER, "unconfirmed": AMBER,
            "confirmed_with_notes": AMBER, "not_from_stripes": AMBER, "stopped": GREY, "idle": GREY,
            "running_confirmed": GREEN}
        self.assertEqual(hs.STATE_CODES, expected_codes)
        self.assertEqual(len(hs.STATE_CODES), 40)

    def test_precedence_and_lists(self):
        judged = set(hs.STATE_CODES) - set(hs.VIEW_CODES)
        self.assertEqual(sorted(hs.PRECEDENCE), sorted(judged - {"internal_error"}))
        self.assertEqual(len(hs.PRECEDENCE), len(set(hs.PRECEDENCE)))
        for code in hs.NEEDS_SEND_CODES + hs.BLUE_CODES + hs.VIEW_CODES:
            self.assertIn(code, hs.STATE_CODES)
        self.assertEqual(hs.WORST_ORDER, (RED, AMBER, BLUE, GREY, GREEN))
        self.assertEqual((hs.INK_NOT_DETECTED, hs.INK_NORMAL, hs.INK_LOW, hs.INK_CRITICAL, hs.INK_NONE),
                         (0, 1, 2, 3, 4))

    def test_every_code_has_a_headline(self):
        for code in hs.STATE_CODES:
            if code == "action_in_progress":
                for purpose in ("stop", "resume", "clock", "other"):
                    self.assertIn(f"action_in_progress:{purpose}", hs.STATE_TEXTS)
            else:
                self.assertIn(code, hs.STATE_TEXTS)
        self.assertIn("test_running:nostep", hs.STATE_TEXTS)
        self.assertNotIn("action_in_progress", hs.STATE_TEXTS)

    def test_every_text_has_spanish(self):
        texts = list(hs.STATE_TEXTS.values()) + [k.text for k in hs.ALERT_KINDS.values()]
        texts += list(hs.LOW_INK_TEXTS.values()) + list(hs.SETUP_TEXTS.values())
        texts += [v for k, v in vars(hs).items() if k.startswith("T_") and isinstance(v, str)]
        texts += list(hs.T_UNFINISHED.values())
        for text in texts:
            self.assertIn(text, hs.ES, text)
            self.assertEqual(i18n.ES[text], hs.ES[text])
        # The texts already in i18n keep their Spanish; "Off" stays an auto-refresh choice
        self.assertEqual(hs.ES["Printing"], "Imprimiendo")
        self.assertEqual(hs.ES["Checking..."], "Revisando...")
        self.assertEqual(i18n.ES["Off"], "Nunca")
        self.assertEqual(hs.STATE_TEXTS["off"], "Printer off")

    def test_alert_kinds(self):
        self.assertEqual(len(hs.ALERT_KINDS), 19)
        sound = {k for k, v in hs.ALERT_KINDS.items() if v.sound}
        self.assertEqual(sound, {"send_failed", "send_rejected", "send_interrupted", "stop_failed", "drift",
                                 "printing_off", "printing_on", "ink_out", "cartridge_error", "went_silent",
                                 "test_still_printing", "encoder_changed"})
        for k in hs.ALERT_KINDS.values():
            if k.sound:                         # no sound alert is addressed to administrators
                self.assertNotIn("admins", k.roles)
        self.assertEqual(hs.ALERT_KINDS["drift"].roles, ("notify", "last_sender", "floor"))
        self.assertEqual(hs.ALERT_KINDS["stop_failed"].roles, ("last_sender", "notify", "floor"))
        self.assertEqual(hs.ALERT_KINDS["send_failed"].ref_from, "send_id")
        self.assertEqual(hs.ALERT_KINDS["test_still_printing"].ref_from, "run_id")

    def test_no_bag_text_in_any_msg(self):
        """Origin lines ("Product of USA and Canada") go everywhere in; no Msg may carry one out."""
        bag = dataclasses.replace(NEW, origin_line=ORIGIN)
        e_cases = [expected(), expected(confirmed=False), expected(notes=["pc_time", "no_lock"]),
                   expected("switch_pending"), expected(failure=failure(True), left_by=AKIM),
                   expected(rejected=True), expected("stopped_by_user", stopped_by=AKIM),
                   expected("test_finished", test={"run_id": RUN_ID, "by": AKIM, "step": 3, "of": 4}), None]
        f_cases = [facts(running=bag), facts(running=None, running_unchecked=True),
                   facts(reachable=False, running=bag), facts(problem="wrong_printer"), facts(ink_level=4),
                   facts(printing=False, ink_level=0), facts(clock_offset_s=600, clock_source="pc")]
        found = []
        for e in e_cases:
            for f in f_cases:
                for lk in (None, lock("send"), lock("stop"), lock("test", run_id=RUN_ID)):
                    prev = poll(running=bag, unreachable_since=T0 - 600)
                    found += msgs_in(classify(f, e, prev=prev, lk=lk))
                    found += msgs_in(judge(prev, reading(running=bag), e, lk))
                    found += msgs_in(hs.machine_view(MACHINE, prev, 5.0, e, lk, 70.0, {}))
                    found += msgs_in(hs.machine_view(MACHINE, prev, 5.0, e, lk, 5.0, {}))
        board = hs.board_view(DATA, {MID: poll(running=bag)}, {MID: 5.0}, {MID: expected()}, {}, {}, {}, {},
                              share_ok=True, standalone=False, now=T0)
        found += msgs_in(board) + msgs_in(hs.floor_board_rows(board))
        self.assertTrue(found)

        def values(m):
            for v in m.values.values():
                if isinstance(v, Msg):
                    yield from values(v)
                else:
                    yield v
        for m in found:
            for v in values(m):
                self.assertNotIn("Product of", str(v), m)


# --------------------------------------------------------------------------
# 2. The state table, row by row
# --------------------------------------------------------------------------

NONE_E = None
STOPPED = expected("stopped_by_user", printing_expected=False, stop_reason="user", stopped_by=AKIM)
TEST_E = expected("test_running", test={"run_id": RUN_ID, "kind": "test", "by": AKIM, "step": 3, "of": 10},
                  sender=AKIM, written_by=AKIM)

ROWS = [
    # (name, facts, E, keywords, code, alert kinds)
    ("wrong printer", facts(problem="wrong_printer"), expected(), {}, "wrong_printer", ["setup_problem"]),
    ("login failed", facts(problem="login_failed"), expected(), {}, "login_failed", ["setup_problem"]),
    ("test orphaned", facts(), TEST_E, {"lk": lock("test", run_id=RUN_ID)}, "test_orphaned",
     ["test_still_printing"]),
    ("test interrupted", facts(printing=False), TEST_E, {"lk": lock("test", run_id=RUN_ID)}, "test_interrupted", []),
    ("interrupted old", facts(running=OLD), expected("switch_pending"), {"lk": lock("send")}, "interrupted_old",
     ["send_interrupted"]),
    ("interrupted new", facts(), expected("switch_pending"), {"lk": lock("send")}, "interrupted_new",
     ["send_interrupted"]),
    ("interrupted unknown", facts(printing=False), expected("switch_pending"), {"lk": lock("send")},
     "interrupted_unknown", ["send_interrupted"]),
    ("stop failed", facts(), expected("stop_requested_at_printer"), {}, "stop_failed", ["stop_failed"]),
    ("stop requested, not read", facts(printing=None), expected("stop_requested_at_printer"), {}, "stop_requested",
     ["stop_failed"]),
    ("stop requested, not answering", facts(reachable=False), expected("stop_requested_at_printer"), {},
     "stop_requested", ["stop_failed"]),
    ("stop requested, read off", facts(printing=False), expected("stop_requested_at_printer", stopped_by=AKIM), {},
     "stopped", []),
    ("failed after switch", facts(), expected("switch_pending", failure=failure(True)), {}, "failed_after_switch",
     ["send_failed"]),
    ("failed before switch", facts(running=OLD),
     expected(failure=failure(), message_name=OLD.name, slot=OLD.slot, fingerprint=OLD.fingerprint), {},
     "failed_before_switch", ["send_failed"]),
    ("failed before switch, drifted", facts(running=NEW),
     expected(failure=failure(), message_name=OLD.name, slot=OLD.slot, fingerprint=OLD.fingerprint), {},
     "failed_before_switch", ["drift", "send_failed"]),
    ("left red", facts(), expected("switch_pending", failure=failure(True), left_by=AKIM), {}, "left_red",
     ["send_failed"]),
    ("rejected", facts(), expected(rejected=True, resume_blocked=True, confirmed=False), {}, "rejected",
     ["send_rejected"]),
    ("rejected and stopped", facts(printing=False),
     expected("stopped_by_user", rejected=True, resume_blocked=True, stop_reason="rejected", stopped_by=JSMITH), {},
     "stopped_needs_send", ["send_rejected"]),
    ("rejected, then a Send failed before the switch", facts(),
     expected(rejected=True, failure=failure(), confirmed=False), {}, "failed_before_switch", ["send_failed"]),
    ("printing on after a stop", facts(), STOPPED, {}, "printing_on_unexpected", ["printing_on"]),
    ("printing on after a test", facts(), expected("test_finished", printing_expected=False), {},
     "printing_on_unexpected", ["printing_on"]),
    ("unreachable while printing", facts(reachable=False, failures=3), expected(), {"prev": poll()},
     "unreachable_printing", ["went_silent"]),
    ("printing off after a read-on", facts(printing=False), expected(), {"prev": poll()}, "printing_off",
     ["printing_off"]),
    ("printing off, never read on", facts(printing=False), expected(), {"prev": poll(printing=False)},
     "not_printing", []),
    ("printing off, no prev", facts(printing=False), expected(), {}, "not_printing", []),
    ("drift by name", facts(running=OLD), expected(), {}, "drift", ["drift"]),
    ("drift by fingerprint, same name", facts(running=dataclasses.replace(NEW, fingerprint="c" * 64)), expected(),
     {}, "drift", ["drift"]),
    ("drift by slot", facts(running=dataclasses.replace(NEW, slot=10)), expected(), {}, "drift", ["drift"]),
    ("no drift when R is unknown", facts(running=None), expected(), {}, "message_unchecked", []),
    ("no drift when rejected", facts(running=OLD), expected(rejected=True), {}, "rejected", ["send_rejected"]),
    ("no drift after an after-switch failure", facts(running=OLD), expected(failure=failure(True)), {},
     "failed_after_switch", ["send_failed"]),
    ("unknown while down", facts(running=None, message_name=OLD.name), expected(), {"standalone": True},
     "unknown_while_down", []),
    ("unknown while down, switch pending", facts(running=None), expected("switch_pending"), {"standalone": True},
     "unknown_while_down", []),
    ("ink out", facts(ink_level=4), expected(), {}, "ink_out", ["ink_out"]),
    ("cartridge error", facts(ink_level=0), expected(), {}, "cartridge_error", ["cartridge_error"]),
    ("test finished", facts(printing=False), expected("test_finished", printing_expected=False), {},
     "test_finished", []),
    ("off", facts(reachable=False, failures=3), expected(), {"prev": poll(printing=False)}, "off", []),
    ("off, no prev", facts(reachable=False, failures=3), None, {}, "off", []),
    ("message unchecked", facts(running=None), expected(), {}, "message_unchecked", []),
    ("unconfirmed", facts(), expected(confirmed=False, confirmed_by=""), {}, "unconfirmed", []),
    ("unconfirmed, printing not read", facts(printing=None), expected(confirmed=False), {}, "unconfirmed", []),
    ("confirmed with notes", facts(), expected(notes=["pc_time"]), {}, "confirmed_with_notes", []),
    ("not from Stripes", facts(), None, {}, "not_from_stripes", ["not_from_stripes"]),
    ("not from Stripes, mode none", facts(), expected("none", send_id=""), {}, "not_from_stripes",
     ["not_from_stripes"]),
    ("stopped", facts(printing=False), STOPPED, {}, "stopped", []),
    ("idle", facts(printing=False), None, {}, "idle", []),
    ("idle, printing not read", facts(printing=None), None, {}, "idle", []),
    ("running confirmed", facts(), expected(), {}, "running_confirmed", []),
    ("running confirmed, standalone without R", facts(running=None), expected(), {"standalone": True},
     "running_confirmed", []),
    ("first-ever Send failed", facts(printing=False), expected("none", failure=failure()), {},
     "failed_before_switch", ["send_failed"]),
    ("an unknown mode", facts(), expected("from_the_future"), {}, "internal_error", []),
]


class StateTableTests(unittest.TestCase):
    def test_rows(self):
        covered = set()
        for name, f, e, kw, code, alert_kinds in ROWS:
            with self.subTest(name):
                out = classify(f, e, **kw)
                self.assertEqual(out.code, code)
                self.assertEqual(kinds(out), sorted(alert_kinds))
                covered.add(code)
        self.assertEqual(covered, set(hs.PRECEDENCE) | {"internal_error"})

    def test_first_send_failed_while_printing_keeps_the_failure(self):
        out = classify(facts(), expected("none", failure=failure()))
        self.assertEqual(out.code, "failed_before_switch")
        self.assertIn("send_failed", kinds(out))

    def test_rejected_alert_has_one_key_through_the_stop(self):
        a = classify(facts(), expected(rejected=True, confirmed=False))
        b = classify(facts(printing=False), expected("stopped_by_user", rejected=True, stop_reason="rejected"))
        self.assertEqual(a.code, "rejected")
        self.assertEqual(b.code, "stopped_needs_send")
        self.assertEqual(a.key()[1], b.key()[1])
        self.assertEqual(a.key()[1], frozenset({f"send_rejected:{SID}"}))
        self.assertEqual(a.values, {"user": JSMITH})

    def test_ink_levels(self):
        on = {0: ("cartridge_error", ["cartridge_error"]), 1: ("running_confirmed", []),
              2: ("running_confirmed", ["low_ink"]), 3: ("running_confirmed", ["low_ink"]), 4: ("ink_out", ["ink_out"])}
        off = {0: ["low_ink"], 1: [], 2: ["low_ink"], 3: ["low_ink"], 4: ["low_ink"]}
        texts = {0: "No cartridge detected on {machine}", 2: "Low ink on {machine}", 3: "Very low ink on {machine}",
                 4: "No ink on {machine}"}
        for level in range(5):
            with self.subTest(level=level):
                out = classify(facts(ink_level=level), expected())
                self.assertEqual((out.code, kinds(out)), on[level])
                out = classify(facts(ink_level=level, printing=False), STOPPED)
                self.assertEqual((out.code, kinds(out)), ("stopped", off[level]))
                if off[level]:
                    self.assertEqual(out.alerts[0].msg.text, texts[level])
                    self.assertEqual(out.alerts[0].colour, AMBER)

    def test_clock_offset(self):
        self.assertEqual(kinds(classify(facts(clock_offset_s=119), expected())), [])
        self.assertEqual(kinds(classify(facts(clock_offset_s=-119), expected())), [])
        out = classify(facts(clock_offset_s=121), expected())
        self.assertEqual(kinds(out), ["clock_off"])
        self.assertEqual(out.alerts[0].msg.values, {"machine": MACHINE.name, "minutes": 2})
        out = classify(facts(clock_offset_s=-3600, clock_source="pc"), expected())
        self.assertEqual(out.alerts[0].msg.values["minutes"], 60)
        self.assertIn(Msg(hs.T_PC_CLOCK), out.details)
        self.assertNotIn(Msg(hs.T_PC_CLOCK), classify(facts(clock_offset_s=-3600, clock_source="server"),
                                                      expected()).details)

    def test_foreign_client_encoder_and_fonts(self):
        out = classify(facts(foreign_client=True), expected())
        self.assertEqual((out.code, kinds(out)), ("running_confirmed", ["foreign_client"]))
        self.assertEqual(out.alerts[0].msg.values, {"printer": "Printer 1"})
        daily = {"values": {"ENWHD": 56, "ENPPR": 5000}, "encoder_ref": {"ENWHD": 56, "ENPPR": 5000},
                 "fonts": list(COMPANY.standard_fonts)}
        self.assertEqual(kinds(classify(facts(daily=daily), expected(), machine=ENC)), [])
        changed = dict(daily, values={"ENWHD": 60, "ENPPR": 5000})
        self.assertEqual(kinds(classify(facts(daily=changed), expected(), machine=ENC)), ["encoder_changed"])
        # A delay machine has no encoder to check
        self.assertEqual(kinds(classify(facts(daily=changed), expected())), [])
        # The machine record's values win; without them the first daily reading is the reference
        no_record = dataclasses.replace(ENC, encoder_wheel_mm=None, encoder_ppr=None)
        self.assertEqual(kinds(classify(facts(daily=dict(changed, encoder_ref={"ENWHD": 60})), expected(),
                                        machine=no_record)), [])
        self.assertEqual(kinds(classify(facts(daily=changed), expected(), machine=dataclasses.replace(
            ENC, encoder_wheel_mm=59.6))), [])
        fonts = dict(daily, fonts=["CourierN", "Arial"])
        self.assertEqual(kinds(classify(facts(daily=fonts), expected())), ["fonts_differ"])
        self.assertEqual(kinds(classify(facts(daily=dict(daily, fonts=None, errors=["fonts"])), expected())), [])


# --------------------------------------------------------------------------
# 2b. Names, never ids, in every text people read
# --------------------------------------------------------------------------

# A record id in a text: "mc_5d1e2f3a4b", "ln_missing", "sd_8a7b6c5d4e"
ID_TEXT = re.compile(r"\b(?:mc|ln|pr|fm|pt|pd|sd|tp)_\w+")
ALL_IDS = (MID, ENC_MID, MID2, LINE_ID, LINE2_ID, PID, ENC_PID, PID2, SID, SID2, RUN_ID)
ENC_DAILY = {"values": {"ENWHD": 60, "ENPPR": 5000}, "encoder_ref": {"ENWHD": 56, "ENPPR": 5000},
             "fonts": list(COMPANY.standard_fonts)}
# The alert kinds ROWS doesn't raise: (name, facts, E, keywords, alert kind)
MORE_ALERTS = [
    ("low ink", facts(ink_level=2), expected(), {}, "low_ink"),
    ("very low ink", facts(ink_level=3), expected(), {}, "low_ink"),
    ("no ink, not printing", facts(printing=False, ink_level=4), STOPPED, {}, "low_ink"),
    ("clock off", facts(clock_offset_s=600.0, clock_source="pc"), expected(), {}, "clock_off"),
    ("foreign client", facts(foreign_client=True), expected(), {}, "foreign_client"),
    ("fonts differ", facts(daily=dict(ENC_DAILY, fonts=["CourierN", "Arial"])), expected(), {}, "fonts_differ"),
    ("encoder changed", facts(daily=ENC_DAILY), expected(), {"machine": ENC}, "encoder_changed"),
    ("message unreadable", facts(running=None, running_unchecked=True), expected(), {}, "message_unreadable"),
]


def rendered(msg):
    """The Msg as an English and a Spanish screen show it, nested Msgs included."""
    def spanish(m):
        return i18n.tr(m.text, **{k: spanish(v) if isinstance(v, Msg) else v for k, v in m.values.items()})
    i18n.set_language("es")
    try:
        es = spanish(msg)
    finally:
        i18n.set_language("en")
    return [msg.english(), es]


def alerts_of(outcome, machine):
    """The Alerts judge() would start for an outcome's needs."""
    return [Alert(id=hs.alert_id(machine.id, n.kind, ref=n.ref, at=T0), machine_id=machine.id, line_id=machine.line_id,
                  kind=n.kind, sound=hs.ALERT_KINDS[n.kind].sound, msg=n.msg, started_at=T0,
                  recipients=["user:" + AKIM, "floor"], colour=n.colour, ref=n.ref, active=True)
            for n in outcome.alerts]


class NameNotIdTests(unittest.TestCase):
    """Alerts, headlines, details, line and board texts name the machine, line and printer: a screenshot
    once read "... on mc_l2b1"."""

    def assertNoIds(self, msgs, what):
        self.assertTrue(msgs, what)
        for m in msgs:
            for text in rendered(m):
                self.assertIsNone(ID_TEXT.search(text), f"{what}: {text!r}")
                for rid in ALL_IDS:
                    self.assertNotIn(rid, text, what)

    def test_no_id_in_any_rendered_alert_or_headline(self):
        cases = [(name, f, e, kw) for name, f, e, kw, _, _ in ROWS] + [(n, f, e, kw) for n, f, e, kw, _ in MORE_ALERTS]
        kinds_seen, views = set(), []
        for name, f, e, kw in cases:
            kw = dict(kw)
            machine = kw.pop("machine", MACHINE)
            out = classify(f, e, machine=machine, **kw)
            headline = Msg(hs.STATE_TEXTS[out.code], dict(out.values))
            alerts = alerts_of(out, machine)
            self.assertNoIds([headline] + out.details + [a.msg for a in alerts], name)
            for need in out.alerts:
                kinds_seen.add(need.kind)
                english = need.msg.english()
                text = need.msg.text
                if "{machine}" in text:
                    self.assertIn(machine.name, english, name)
                if "{printer}" in text:
                    self.assertIn(DATA.printers[machine.printer_id].name, english, name)
                self.assertNotIn("{", english, name)            # every blank filled
            # What the monitor card shows: the headline, or the alert that replaces it, and every detail
            snap = MachineSnapshot(code=out.code, colour=hs.STATE_CODES[out.code], headline=headline, since=T0,
                                   detail=list(out.details))
            p = poll(out.code, alerts=alerts, state=snap, machine_id=machine.id, printer_id=machine.printer_id)
            view = hs.machine_view(machine, p, 5.0, e, kw.get("lk"), 70.0, {})
            self.assertNoIds([view.headline] + view.details, name)
            views.append(view)
        self.assertEqual(kinds_seen, set(hs.ALERT_KINDS))           # every kind's text was checked

        # A line headline names its worst machine; the board and floor rows carry the views' texts
        for view in views:
            ok = hs.machine_view(ENC, poll(machine_id=ENC_MID, printer_id=ENC_PID), 5.0, expected(machine_id=ENC_MID),
                                 None, None, {})
            lv = hs.line_view(LINE, [ok, view], None)
            self.assertNoIds([lv.headline], view.code)
            if lv.headline.text == hs.T_MACHINE_STATE:
                self.assertEqual(lv.headline.values["machine"], view.name)
        board = hs.board_view(DATA, {MID: poll("drift", alerts=[alert("drift")]), ENC_MID: poll(machine_id=ENC_MID)},
                              {MID: 5.0, ENC_MID: 5.0}, {MID: expected(), ENC_MID: expected(machine_id=ENC_MID)},
                              {PID2: lock("send", printer_id=PID2, line_id=LINE2_ID, machine_id=MID2)}, {PID2: 5.0},
                              {}, {}, share_ok=True, standalone=False, now=T0)
        texts = [lv.headline for lv in board.lines] + [m.headline for lv in board.lines for m in lv.machines]
        texts += [d for lv in board.lines for m in lv.machines for d in m.details]
        texts += [r.status for r in hs.floor_board_rows(board)]
        self.assertNoIds(texts, "board")
        self.assertEqual(board.lines[0].headline.values["machine"], "Line 1 Bagger 1")

    def test_a_real_judgement_names_the_machine(self):
        # Through judge() and its second reading, as the poller runs it
        j = commit_second(poll(), reading(running=OLD), expected())
        self.assertEqual([a.kind for a in j.alerts], ["drift"])
        self.assertEqual(rendered(j.alerts[0].msg), ["Line 1 Bagger 1 isn't printing what Stripes sent",
                                                     "Line 1 Bagger 1 no imprime lo que envió Stripes"])
        self.assertEqual(j.alerts[0].machine_id, MID)              # the id stays in the record, not the text


# --------------------------------------------------------------------------
# 3. Crash recovery
# --------------------------------------------------------------------------

class CrashRecoveryTests(unittest.TestCase):
    def check(self, f, e, lk, code):
        out = classify(f, e, lk=lk)
        self.assertEqual(out.code, code)
        return out

    def test_send_stopped_before_switch_pending(self):
        # The lock's Send never wrote E: PRINTMSG was never sent, whatever prints
        for f in (facts(), facts(running=OLD), facts(printing=False), facts(reachable=False)):
            out = self.check(f, expected(), lock("send", run_id=SID2), "interrupted_old")
            self.assertIn(f"send_interrupted:{SID2}", out.key()[1])

    def test_switch_pending_rows(self):
        e, lk = expected("switch_pending"), lock("send")
        out = self.check(facts(), e, lk, "interrupted_new")
        self.assertEqual(out.alerts[0].colour, AMBER)
        self.check(facts(running=OLD), e, lk, "interrupted_old")
        self.check(facts(printing=False), e, lk, "interrupted_unknown")
        self.check(facts(running=dataclasses.replace(OLD, name="SOMETHING-ELSE")), e, lk, "interrupted_unknown")
        self.check(facts(running=None), e, lk, "interrupted_unknown")
        self.check(facts(reachable=False), e, lk, "interrupted_unknown")
        self.check(facts(problem="wrong_printer"), e, lk, "wrong_printer")
        # The new name in the expected slot with other content is neither new nor old
        self.check(facts(running=dataclasses.replace(NEW, fingerprint="c" * 64, origin_line="Product of Mexico")),
                   e, lk, "interrupted_unknown")

    def test_running_not_confirmed_rows(self):
        e, lk = expected(confirmed=False, confirmed_by=""), lock("send")
        out = self.check(facts(), e, lk, "interrupted_new")
        self.assertIn(Msg(hs.T_WAITING_CONFIRM, {"user": JSMITH}), out.details)
        self.check(facts(running=OLD), e, lk, "interrupted_unknown")
        self.check(facts(printing=False), e, lk, "interrupted_unknown")

    def test_not_interrupted(self):
        lk = lock("send")
        self.check(facts(), expected(), lk, "running_confirmed")
        out = self.check(facts(), expected(rejected=True, confirmed=False), lk, "rejected")
        self.assertEqual(kinds(out), ["send_rejected"])
        out = self.check(facts(), expected("switch_pending", failure=failure(True)), lk, "failed_after_switch")
        self.assertEqual(kinds(out), ["send_failed"])

    def test_older_failure_and_a_newer_interrupted_send(self):
        out = self.check(facts(), expected(failure=failure()), lock("send", run_id=SID2), "interrupted_old")
        self.assertEqual(kinds(out), ["send_failed", "send_interrupted"])

    def test_orphan_switch_pending_without_a_lock(self):
        e = expected("switch_pending")
        self.check(facts(), e, None, "interrupted_new")
        self.check(facts(running=OLD), e, None, "interrupted_old")
        self.check(facts(printing=False), e, None, "interrupted_unknown")
        out = self.check(facts(printing=False), e, None, "interrupted_unknown")
        self.assertEqual(out.values, {"computer": "FLOOR01"})
        self.assertEqual(out.details[0], Msg(hs.T_STARTED_BY, {"user": JSMITH}))
        out = self.check(facts(), e, None, "interrupted_new")
        self.assertIn(Msg(hs.T_STARTED_NEEDS_SEND, {"user": JSMITH}), out.details)
        # Standalone checks skip crash recovery
        self.assertEqual(classify(facts(), e, standalone=True).code, "unknown_while_down")

    def test_stale_test_lock(self):
        lk = lock("test", run_id=RUN_ID, user=AKIM, computer="FLOOR02")
        out = self.check(facts(), TEST_E, lk, "test_orphaned")
        self.assertEqual(out.key()[1], frozenset({f"test_still_printing:{RUN_ID}"}))
        out = self.check(facts(printing=False), TEST_E, lk, "test_interrupted")
        self.assertEqual(out.values, {"computer": "FLOOR02"})
        self.assertEqual(out.details[0], Msg(hs.T_STARTED_BY, {"user": AKIM}))
        # A calibration lock is a test too, also before the run wrote its E
        self.check(facts(), expected(), lock("calibration", run_id=RUN_ID), "test_orphaned")
        # E says a test is running but there is no lock at all
        self.check(facts(), TEST_E, None, "test_orphaned")
        self.check(facts(printing=False), TEST_E, None, "test_interrupted")
        # Not answering: still orphaned while it was last read printing, until acknowledged
        prev = poll(alerts=[alert("test_still_printing", ref=RUN_ID)])
        out = classify(facts(reachable=False), TEST_E, prev=prev, lk=lk)
        self.assertEqual(out.code, "test_orphaned")
        out = classify(facts(reachable=False), TEST_E, prev=prev, lk=lk,
                       acked={prev.alerts[0].id: Ack(alert_id=prev.alerts[0].id, by=AKIM, computer="FLOOR02", at=T0)})
        self.assertEqual(out.code, "test_interrupted")

    def test_stale_stop_lock_adds_the_detail(self):
        for purpose, text in (("stop", "A Stop on {computer} didn't finish."),
                              ("resume", "A Resume on {computer} didn't finish."),
                              ("clock", "A clock set on {computer} didn't finish.")):
            out = self.check(facts(printing=False), STOPPED, lock(purpose, computer="FLOOR02"), "stopped")
            self.assertIn(Msg(text, {"computer": "FLOOR02"}), out.details)
        # A Stop that switched printing off before its PC stopped may raise printing_off; accepted
        out = classify(facts(printing=False), expected(), prev=poll(), lk=lock("stop"))
        self.assertEqual(out.code, "printing_off")

    def test_interrupted_alert_recipients(self):
        lk = lock("send", user=JSMITH)
        j = commit_second(None, reading(), expected("switch_pending", sender=AKIM), lk)
        a = next(a for a in j.alerts if a.kind == "send_interrupted")
        self.assertEqual(a.recipients, ["user:" + JSMITH, "user:" + AKIM])
        self.assertTrue(a.sound)
        self.assertEqual(a.id, f"{MID}.send_interrupted.{SID}.{int((T0 + 1) * 1000)}")
        self.assertEqual(a.msg.values, {"machine": MACHINE.name, "computer": "FLOOR01"})


# --------------------------------------------------------------------------
# 4. Untrusted facts
# --------------------------------------------------------------------------

class UntrustedTests(unittest.TestCase):
    def test_drift_is_carried_while_unreachable(self):
        drift = alert("drift")
        prev = poll("drift", alerts=[drift], running=OLD, bags_recent=0, printing=False)
        r = reading(reachable=False, failures=3, printing=None, running=None, message_name=None, bag_count=None,
                    ink_level=None, clock_offset_s=None)
        j = commit_second(prev, r, expected())
        self.assertEqual(j.state.code, "off")
        self.assertEqual([(a.kind, a.id) for a in j.alerts], [("drift", drift.id)])
        self.assertEqual(j.ended, [])
        # Acknowledged while unreachable: it ends
        prev2 = dataclasses.replace(prev, state=j.state, alerts=j.alerts, reachable=False)
        ack = Ack(alert_id=drift.id, by=AKIM, computer="FLOOR02", at=T0 + 5)
        j2 = judge(prev2, dataclasses.replace(r, acked={drift.id: ack}, at=T0 + 5), expected())
        self.assertTrue(j2.committed)
        self.assertEqual(j2.alerts, [])
        self.assertEqual([(a.id, a.acked_by, a.active) for a in j2.ended], [(drift.id, AKIM, False)])

    def test_went_silent_or_off(self):
        down = facts(reachable=False, failures=3)
        self.assertEqual(classify(down, expected(), prev=poll(bags_recent=5)).code, "unreachable_printing")
        self.assertEqual(classify(down, expected(), prev=poll(bags_recent=0)).code, "off")
        self.assertEqual(classify(down, expected(), prev=poll(bags_recent=None)).code, "off")
        no_counter = dataclasses.replace(DATA, company=dataclasses.replace(COMPANY, bag_counter="none"))
        self.assertEqual(classify(down, expected(), prev=poll(bags_recent=None), data=no_counter).code,
                         "unreachable_printing")
        self.assertEqual(classify(down, expected(), prev=poll(printing=False), data=no_counter).code, "off")
        self.assertTrue(hs.was_printing(poll(bags_recent=1), COMPANY))
        self.assertFalse(hs.was_printing(None, COMPANY))
        # Once raised it stays until it answers, also with the prev now unreachable
        silent = alert("went_silent")
        prev = poll("unreachable_printing", alerts=[silent], reachable=False, bags_recent=0)
        self.assertEqual(classify(down, expected(), prev=prev).code, "unreachable_printing")
        # Acknowledged: grey off, and the alert ends
        acked = {silent.id: Ack(alert_id=silent.id, by=AKIM, computer="FLOOR02", at=T0)}
        out = classify(down, expected(), prev=prev, acked=acked)
        self.assertEqual((out.code, kinds(out)), ("off", []))
        j = judge(prev, reading(reachable=False, failures=4, printing=None, running=None, acked=acked), expected())
        self.assertEqual(j.state.code, "off")
        self.assertEqual([a.id for a in j.ended], [silent.id])

    def test_setup_problems_have_no_sound(self):
        for problem, text in (("wrong_printer", "Another printer answers at the address of {machine}"),
                              ("login_failed", "Stripes can't log in to the printer of {machine}")):
            j = commit_second(poll(), reading(problem=problem), expected())
            self.assertEqual(j.state.code, problem)
            self.assertEqual(j.state.colour, RED)
            (a,) = j.alerts
            self.assertEqual((a.kind, a.sound, a.colour, a.msg.text), ("setup_problem", False, "", text))
            self.assertEqual(a.recipients, ["admins", "user:" + AKIM])

    def test_conditions_on_e_still_apply(self):
        down = facts(reachable=False, failures=3)
        self.assertEqual(classify(down, expected(failure=failure())).code, "failed_before_switch")
        self.assertEqual(classify(down, expected("stop_requested_at_printer")).code, "stop_requested")
        self.assertEqual(classify(down, expected("test_finished")).code, "test_finished")
        self.assertEqual(classify(down, STOPPED).code, "off")


# --------------------------------------------------------------------------
# 5. The two-reading rule
# --------------------------------------------------------------------------

class TwoReadingTests(unittest.TestCase):
    def test_a_stray_prin(self):
        prev = poll()
        first = judge(prev, reading(printing=False), expected())
        self.assertFalse(first.committed)
        self.assertEqual(first.state, prev.state)
        self.assertEqual(first.recheck, ["PRIN"])
        self.assertEqual(first.candidate.code, "printing_off")
        self.assertEqual(first.candidate_alerts, ("printing_off:",))
        self.assertIs(first.facts.printing, True)       # the committed fact stays
        # The second reading says printing is on: nothing changes, no further recheck
        second = reading(printing=True, second=True, at=T0 + 1, pending_code="printing_off",
                         pending_alerts=first.candidate_alerts)
        j = judge(prev, second, expected())
        self.assertTrue(j.committed)
        self.assertEqual((j.state.code, j.recheck, j.alerts), ("running_confirmed", [], []))
        self.assertEqual(j.state.since, prev.state.since)

    def test_two_prin_zero_commit(self):
        prev = poll()
        first = judge(prev, reading(printing=False), expected())
        second = reading(printing=False, second=True, at=T0 + 1, pending_code=first.candidate.code,
                         pending_alerts=first.candidate_alerts)
        j = judge(prev, second, expected())
        self.assertTrue(j.committed and j.changed)
        self.assertEqual(j.state.code, "printing_off")
        self.assertEqual(j.state.since, T0 + 1)
        self.assertEqual([a.kind for a in j.started], ["printing_off"])
        self.assertEqual(j.alerts[0].id, f"{MID}.printing_off.{int((T0 + 1) * 1000)}")
        self.assertIs(j.facts.printing, False)

    def test_a_second_reading_that_disagrees_commits_nothing(self):
        prev = poll()
        first = judge(prev, reading(printing=False), expected())
        second = reading(printing=False, ink_level=3, second=True, at=T0 + 1, pending_code=first.candidate.code,
                         pending_alerts=first.candidate_alerts)
        j = judge(prev, second, expected())
        self.assertFalse(j.committed)
        self.assertEqual((j.state.code, j.recheck), ("running_confirmed", []))
        self.assertIs(j.facts.printing, True)

    def test_changes_from_e_commit_at_once(self):
        prev = poll("unconfirmed")
        j = judge(prev, reading(), expected())
        self.assertTrue(j.committed)
        self.assertEqual((j.state.code, j.recheck), ("running_confirmed", []))
        self.assertEqual(j.state.detail, [Msg(hs.T_CONFIRMED_BY, {"user": JSMITH}),
                                          Msg(hs.T_MESSAGE_IN_SLOT, {"name": NEW.name, "slot": 9})])

    def test_foreign_client_commits_at_once(self):
        j = judge(poll(), reading(foreign_client=True), expected())
        self.assertTrue(j.committed)
        self.assertEqual([a.kind for a in j.started], ["foreign_client"])
        self.assertEqual(j.recheck, [])

    def test_running_unchecked_commits_at_once(self):
        j = judge(poll(), reading(running=None, running_unchecked=True), expected())
        self.assertTrue(j.committed)
        self.assertEqual(j.state.code, "message_unchecked")

    def test_third_failure_asks_for_reach(self):
        r = reading(reachable=False, failures=3, printing=None, running=None, message_name=None, bag_count=None,
                    ink_level=None, clock_offset_s=None)
        j = judge(poll(bags_recent=0), r, expected())
        self.assertFalse(j.committed)
        self.assertEqual(j.recheck, ["reach"])
        self.assertEqual(j.candidate.code, "off")

    def test_since_is_kept(self):
        prev = poll()
        j = judge(prev, reading(at=T0 + 50), expected())
        self.assertEqual(j.state.since, prev.state.since)
        self.assertFalse(j.changed)

    def test_a_drifted_load_needs_a_second_load(self):
        j = judge(poll(), reading(running=OLD), expected())
        self.assertEqual(j.recheck, ["load"])
        # A Load that couldn't read anything is no evidence: amber at once
        j = judge(poll(), reading(running=None, running_reset=True), expected())
        self.assertTrue(j.committed)
        self.assertEqual(j.state.code, "message_unchecked")
        self.assertIsNone(j.facts.running)

    def test_a_new_machine_needs_two_readings(self):
        first = judge(None, reading(), expected())
        self.assertFalse(first.committed)
        self.assertIsNone(first.state)
        self.assertIn("PRIN", first.recheck)
        self.assertIn("load", first.recheck)
        j = commit_second(None, reading(), expected())
        self.assertEqual(j.state.code, "running_confirmed")

    def test_read_running_now(self):
        j = judge(poll(), reading(running=None, message_name="SOMETHING-ELSE"), expected())
        self.assertTrue(j.read_running_now)
        self.assertFalse(judge(poll(), reading(running=None), expected()).read_running_now)
        self.assertFalse(judge(poll(), reading(message_name="SOMETHING-ELSE"), expected()).read_running_now)
        self.assertFalse(judge(poll(running=None), reading(running=None, message_name="X"), expected()).read_running_now)

    def test_evidence(self):
        old = facts()
        self.assertEqual(hs.evidence(old, facts(reachable=False)), ["reach"])
        self.assertEqual(hs.evidence(old, facts(problem="wrong_printer")), ["uid"])
        self.assertEqual(hs.evidence(old, facts(problem="login_failed")), ["login"])
        self.assertEqual(hs.evidence(old, facts(ink_level=2)), ["INKL"])
        self.assertEqual(hs.evidence(old, facts(clock_offset_s=50)), [])
        self.assertEqual(hs.evidence(old, facts(clock_offset_s=150)), ["clock"])
        self.assertEqual(hs.evidence(old, facts(running=dataclasses.replace(NEW, read_at=T0))), [])
        self.assertEqual(hs.evidence(old, facts(running_unchecked=True)), [])
        daily = {"values": {"ENWHD": 60}, "fonts": ["CourierN"]}
        self.assertEqual(hs.evidence(old, facts(daily=daily)), ["daily:ENWHD", "fonts"])


# --------------------------------------------------------------------------
# 6. Alert identity
# --------------------------------------------------------------------------

class AlertIdentityTests(unittest.TestCase):
    PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

    def test_ids(self):
        self.assertEqual(hs.alert_id("mc_5d1e2f3a4b", "send_failed", ref="sd_8a7b6c5d4e"),
                         "mc_5d1e2f3a4b.send_failed.sd_8a7b6c5d4e")
        self.assertEqual(hs.alert_id("mc_5d1e2f3a4b", "drift", at=1793275205.123), "mc_5d1e2f3a4b.drift.1793275205123")
        odd = hs.alert_id(MID, "test_still_printing", ref="tp:1/2 3" + "x" * 40)
        self.assertEqual(odd, f"{MID}.test_still_printing.tp_1_2_3" + "x" * 16)
        # judge() passes the start time with a ref too, so each occurrence has its own id
        self.assertEqual(hs.alert_id("mc_5d1e2f3a4b", "send_failed", ref="sd_8a7b6c5d4e", at=1793275205.123),
                         "mc_5d1e2f3a4b.send_failed.sd_8a7b6c5d4e.1793275205123")
        for kind in hs.ALERT_KINDS:
            self.assertRegex(hs.alert_id(MID, kind, ref="sd_8a7b6c5d4e"), self.PATTERN)
            self.assertRegex(hs.alert_id(MID, kind, at=T0), self.PATTERN)
            self.assertRegex(hs.alert_id(MID, kind, ref="x" * 40, at=T0 + 0.999), self.PATTERN)

    def test_takeover_continues_the_alert(self):
        prev = poll("drift", alerts=[alert("drift", acked_by=AKIM, acked_at=T0 - 100, waiting=[JSMITH])],
                    running=OLD, poller="FLOOR03_EXAMPLE_jsmith_99")
        j = judge(prev, reading(running=OLD), expected())
        self.assertTrue(j.committed)
        (a,) = j.alerts
        self.assertEqual((a.id, a.started_at, a.recipients, a.acked_by, a.waiting),
                         (prev.alerts[0].id, prev.alerts[0].started_at, prev.alerts[0].recipients, AKIM, [JSMITH]))
        self.assertEqual((j.started, j.ended), ([], []))

    def test_new_occurrence_new_id(self):
        prev = poll()
        j1 = commit_second(prev, reading(printing=False), expected())
        first_id = j1.alerts[0].id
        p1 = dataclasses.replace(prev, state=j1.state, alerts=j1.alerts, printing=False)
        j2 = commit_second(p1, reading(printing=True, at=T0 + 60), expected())
        self.assertEqual(j2.alerts, [])
        self.assertEqual([a.id for a in j2.ended], [first_id])
        self.assertEqual(j2.ended[0].ended_at, T0 + 61)
        p2 = dataclasses.replace(p1, state=j2.state, alerts=j2.alerts, printing=True)
        j3 = commit_second(p2, reading(printing=False, at=T0 + 120), expected())
        self.assertEqual([a.kind for a in j3.started], ["printing_off"])
        self.assertNotEqual(j3.alerts[0].id, first_id)

    def test_new_occurrence_of_a_ref_alert_new_id(self):
        # test_still_printing for the same orphaned test, ended (printing read off) and raised again
        # (printing switched back on): the first occurrence's ack must not silence the second
        lk = lock("test", run_id=RUN_ID)
        test_e = expected("test_running", test={"run_id": RUN_ID, "by": AKIM, "step": 3, "of": 10})
        j1 = commit_second(poll("running_confirmed", printing=False), reading(), test_e, lk)
        (first,) = j1.alerts
        self.assertEqual((first.kind, first.ref), ("test_still_printing", RUN_ID))
        ack = Ack(alert_id=first.id, by=JSMITH, computer="FLOOR02", at=T0 + 5)
        p1 = poll(j1.state.code, alerts=[dataclasses.replace(first, acked_by=JSMITH, acked_at=T0 + 5)])
        j2 = commit_second(p1, reading(printing=False, at=T0 + 60), test_e, lk)
        self.assertEqual((j2.state.code, j2.alerts), ("test_interrupted", []))
        self.assertEqual([a.id for a in j2.ended], [first.id])
        p2 = poll(j2.state.code, alerts=[], printing=False)
        # Every ack ever found is offered (the store caches found acks for its life)
        j3 = commit_second(p2, reading(printing=True, at=T0 + 120, acked={first.id: ack}), test_e, lk)
        (second,) = j3.alerts
        self.assertEqual((j3.state.code, second.kind, second.ref), ("test_orphaned", "test_still_printing", RUN_ID))
        self.assertNotEqual(second.id, first.id)
        self.assertEqual((second.acked_by, second.acked_at), ("", None))
        self.assertEqual(second.started_at, T0 + 121)
        # The board, with the old ack still known to the watcher, rings for it
        mv = hs.machine_view(MACHINE, poll(j3.state.code, alerts=j3.alerts, expected_counter=test_e.counter), 1.0,
                             test_e, None, None, {first.id: ack})
        self.assertEqual([(a.id, a.acked_by) for a in mv.alerts], [(second.id, "")])
        floor_pc = Identity(user="EXAMPLE\\guest", computer="FLOOR01", sids=frozenset(), pid=1)
        board = board_with(*mv.alerts)
        self.assertEqual({a.id for a in hs.sounding_alerts(board, floor_pc, floor_board=True)}, {second.id})
        # The same holds for Send-keyed ids: a second send_failed for the same send_id
        fail_e = expected(failure=failure())
        j4 = commit_second(poll(), reading(), fail_e)
        (f1,) = [a for a in j4.alerts if a.kind == "send_failed"]
        j5 = commit_second(poll(j4.state.code, alerts=j4.alerts), reading(at=T0 + 200), expected())
        self.assertEqual([a.id for a in j5.ended], [f1.id])
        j6 = commit_second(poll(j5.state.code), reading(at=T0 + 300), fail_e)
        (f2,) = [a for a in j6.alerts if a.kind == "send_failed"]
        self.assertNotEqual(f2.id, f1.id)

    def test_acked_by_is_copied(self):
        drift = alert("drift")
        prev = poll("drift", alerts=[drift], running=OLD)
        ack = Ack(alert_id=drift.id, by=AKIM, computer="FLOOR02", at=T0 - 3)
        j = judge(prev, reading(running=OLD, acked={drift.id: ack}), expected())
        self.assertEqual((j.alerts[0].acked_by, j.alerts[0].acked_at), (AKIM, T0 - 3))
        # Also while a second reading is awaited
        j = judge(prev, reading(running=OLD, printing=False, acked={drift.id: ack}), expected())
        self.assertFalse(j.committed)
        self.assertEqual(j.alerts[0].acked_by, AKIM)


# --------------------------------------------------------------------------
# 7. Recipients
# --------------------------------------------------------------------------

class RecipientTests(unittest.TestCase):
    def test_roles(self):
        r = hs.resolve_recipients(("notify", "last_sender", "floor"), sender=AKIM, notify=AKIM, last_sender=JSMITH)
        self.assertEqual(r, ["user:" + AKIM, "user:" + JSMITH, "floor"])
        r = hs.resolve_recipients(("sender", "notify"), sender=JSMITH, notify="", last_sender=JSMITH)
        self.assertEqual(r, ["user:" + JSMITH])
        r = hs.resolve_recipients(("notify", "admins", "monitor"), sender="", notify=AKIM, last_sender="")
        self.assertEqual(r, ["user:" + AKIM, "admins", "monitor"])
        r = hs.resolve_recipients(("sender", "notify"), sender="example\\JSMITH", notify=JSMITH, last_sender="")
        self.assertEqual(r, ["user:example\\JSMITH"])

    def test_drift_recipients(self):
        j = commit_second(poll(), reading(running=OLD), expected())
        self.assertEqual(j.alerts[0].recipients, ["user:" + AKIM, "user:" + JSMITH, "floor"])
        no_notify = dataclasses.replace(LINE, notify_user="")
        j = hs.judge(poll(), reading(running=OLD), expected(), None, MACHINE, no_notify, DATA)
        j = hs.judge(poll(), reading(running=OLD, second=True, pending_code=j.candidate.code,
                                     pending_alerts=j.candidate_alerts), expected(), None, MACHINE, no_notify, DATA)
        self.assertEqual(j.alerts[0].recipients, ["user:" + JSMITH, "floor"])

    def test_can_acknowledge(self):
        sender = Identity(user=JSMITH, computer="FLOOR01", sids=frozenset({SENDER_SID}), pid=1)
        admin = Identity(user=AKIM, computer="FLOOR02", sids=frozenset({ADMIN_SID}), pid=1)
        nobody = Identity(user="EXAMPLE\\guest", computer="FLOOR02", sids=frozenset(), pid=1)
        self.assertTrue(hs.can_acknowledge(sender, COMPANY))
        self.assertTrue(hs.can_acknowledge(admin, COMPANY))
        self.assertFalse(hs.can_acknowledge(nobody, COMPANY))


# --------------------------------------------------------------------------
# 8. Views
# --------------------------------------------------------------------------

def view(p=None, age=5.0, e=None, lk=None, lock_age=None, acks=None, machine=MACHINE):
    return hs.machine_view(machine, p, age, e, lk, lock_age, acks or {})


class ViewTests(unittest.TestCase):
    def test_blue_codes(self):
        p = poll()
        v = view(p, e=expected(), lk=lock("send"), lock_age=5.0)
        self.assertEqual((v.code, v.colour), ("send_in_progress", BLUE))
        self.assertEqual(v.headline, Msg(hs.STATE_TEXTS["send_in_progress"], {"user": JSMITH, "computer": "FLOOR01"}))
        self.assertEqual(view(p, e=expected("switch_pending"), lk=lock("send"), lock_age=5.0).code, "switch_pending")
        v = view(p, e=TEST_E, lk=lock("test", note={"step": 12, "of": 40}, user=AKIM), lock_age=5.0)
        self.assertEqual(v.code, "test_running")
        self.assertEqual(v.headline, Msg(hs.STATE_TEXTS["test_running"], {"user": AKIM, "step": 12, "of": 40}))
        self.assertEqual(v.test, {"by": AKIM, "step": 12, "of": 40})
        v = view(p, e=expected(), lk=lock("calibration", user=AKIM), lock_age=5.0)
        self.assertEqual(v.headline, Msg(hs.STATE_TEXTS["test_running:nostep"], {"user": AKIM}))
        v = view(p, e=TEST_E, lk=lock("test", user=AKIM), lock_age=5.0)
        self.assertEqual(v.headline.values, {"user": AKIM, "step": 3, "of": 10})
        for purpose in ("stop", "resume", "clock"):
            v = view(p, e=expected(), lk=lock(purpose), lock_age=5.0)
            self.assertEqual((v.code, v.headline.text), ("action_in_progress",
                                                          hs.STATE_TEXTS[f"action_in_progress:{purpose}"]))
            self.assertFalse(v.can_stop or v.can_resume or v.confirm_pending_by)

    def test_lock_age(self):
        self.assertEqual(view(poll(), e=expected(), lk=lock("send"), lock_age=59.0).code, "send_in_progress")
        self.assertEqual(view(poll(), e=expected(), lk=lock("send"), lock_age=60.0).code, "running_confirmed")

    def test_checking_and_not_monitored(self):
        self.assertEqual(view(None, age=None).code, "checking")
        self.assertEqual(view(None, age=29.9).code, "checking")
        self.assertEqual(view(None, age=30.0).code, "not_monitored")
        v = view(poll(), age=29.9, e=expected())
        self.assertEqual((v.code, v.monitored), ("running_confirmed", True))
        v = view(poll(), age=30.0, e=expected())
        self.assertEqual((v.code, v.monitored, v.colour, v.printing), ("not_monitored", False, GREY, None))
        self.assertEqual(v.headline, Msg("NOT MONITORED"))
        self.assertEqual(view(poll(state=None), age=5.0).code, "checking")

    def test_standalone_status_allows_one_check_interval(self):
        # Standalone checks move the status only every standalone_poll_s: on-time checks never show
        # NOT MONITORED, a check not_monitored_s late does
        limit = TIMINGS.standalone_poll_s + TIMINGS.not_monitored_s
        for age in (TIMINGS.standalone_poll_s, TIMINGS.standalone_poll_s + 1.0, limit - 0.1):
            v = view(poll(standalone=True), age=age, e=expected())
            self.assertEqual((v.code, v.monitored, v.colour), ("running_confirmed", True, GREEN), age)
        v = view(poll(standalone=True), age=limit, e=expected())
        self.assertEqual((v.code, v.monitored, v.colour), ("not_monitored", False, GREY))
        self.assertEqual(view(poll("not_printing", standalone=True), age=limit).colour, AMBER)
        # A share-mode status keeps the 30 s limit
        self.assertEqual(view(poll(), age=TIMINGS.not_monitored_s, e=expected()).code, "not_monitored")

    def test_not_monitored_keeps_red_and_amber(self):
        self.assertEqual(view(poll("drift"), age=31).colour, RED)
        self.assertEqual(view(poll("unconfirmed"), age=31).colour, AMBER)
        self.assertEqual(view(poll("running_confirmed"), age=31).colour, GREY)
        self.assertEqual(view(poll("stopped"), age=31).colour, GREY)
        # E's colour counts when it is worse
        self.assertEqual(view(poll("running_confirmed"), age=31, e=expected(failure=failure())).colour, RED)

    def test_e_not_judged_yet(self):
        v = view(poll("running_confirmed", expected_counter=5), e=expected(counter=6, confirmed=False))
        self.assertEqual((v.code, v.colour), ("unconfirmed", AMBER))
        self.assertEqual(view(poll("running_confirmed", expected_counter=6), e=expected(counter=6)).code,
                         "running_confirmed")
        v = view(poll("running_confirmed", expected_counter=5), e=expected(counter=6, failure=failure(), left_by=AKIM))
        self.assertEqual((v.code, v.headline), ("left_red", Msg(hs.STATE_TEXTS["left_red"], {"user": AKIM})))
        # Only the poller can tell for a pending switch
        self.assertEqual(view(poll("running_confirmed", expected_counter=5), e=expected("switch_pending", counter=6)).code,
                         "running_confirmed")

    def test_alerts_colour_the_machine(self):
        went = alert("went_silent")
        v = view(poll("off", alerts=[went], reachable=False), e=expected())
        self.assertEqual((v.code, v.colour), ("off", RED))
        self.assertEqual(v.headline, went.msg)
        self.assertEqual(v.details[0], Msg(hs.STATE_TEXTS["off"]))
        self.assertTrue(v.unacked_sound)
        low = alert("low_ink", msg=Msg(hs.LOW_INK_TEXTS[2], {"machine": MACHINE.name}))
        v = view(poll(alerts=[low]), e=expected())
        self.assertEqual((v.colour, v.headline), (AMBER, low.msg))
        self.assertFalse(v.unacked_sound)
        v = view(poll(alerts=[low]), e=expected(), lk=lock("send"), lock_age=5.0)
        self.assertEqual(v.colour, BLUE)
        v = view(poll("stopped", alerts=[low]), e=STOPPED)
        self.assertEqual(v.colour, AMBER)
        # A warning without a colour changes nothing; it is listed among the details
        foreign = alert("foreign_client", msg=Msg(hs.ALERT_KINDS["foreign_client"].text, {"printer": "Printer 1"}))
        v = view(poll(alerts=[foreign]), e=expected())
        self.assertEqual((v.colour, v.headline), (GREEN, Msg("Printing")))
        self.assertIn(foreign.msg, v.details)
        # Acknowledged alerts still colour; acks from the share are applied
        v = view(poll("off", alerts=[went], reachable=False), e=expected(),
                 acks={went.id: Ack(alert_id=went.id, by=AKIM, computer="FLOOR02", at=T0)})
        self.assertEqual((v.colour, v.alerts[0].acked_by, v.unacked_sound), (RED, AKIM, False))
        # Waiting recipients are listed
        v = view(poll(alerts=[dataclasses.replace(alert("drift"), waiting=[JSMITH])]), e=expected())
        self.assertIn(Msg(hs.T_WAITING_SIGN_IN, {"user": JSMITH}), v.details)

    def test_buttons(self):
        v = view(poll(), e=expected())
        self.assertTrue(v.can_stop)
        self.assertFalse(v.can_resume or v.needs_send or v.confirm_pending_by)
        self.assertFalse(view(poll(), age=31, e=expected()).can_stop)
        self.assertFalse(view(poll(reachable=False), e=expected()).can_stop)
        self.assertFalse(view(poll(), e=expected(), lk=lock("send"), lock_age=5.0).can_stop)
        stopped_p = poll("stopped", printing=False)
        resumable = expected("stopped_by_user", stopped_by=AKIM, stop_reason="user", confirmed=True)
        self.assertTrue(view(stopped_p, e=resumable).can_resume)
        self.assertTrue(view(poll("not_printing", printing=False), e=expected()).can_resume)
        self.assertTrue(view(poll("printing_off", printing=False), e=expected()).can_resume)
        for refused in (dataclasses.replace(resumable, rejected=True), dataclasses.replace(resumable, resume_blocked=True),
                        dataclasses.replace(resumable, confirmed=False), dataclasses.replace(resumable, failure=failure()),
                        dataclasses.replace(resumable, send_id="")):
            self.assertFalse(view(stopped_p, e=refused).can_resume, refused)
        self.assertFalse(view(stopped_p, e=resumable, age=31).can_resume)
        self.assertFalse(view(poll("stopped", printing=True), e=resumable).can_resume)
        self.assertFalse(view(poll("idle", printing=False), e=None).can_resume)
        for code in hs.NEEDS_SEND_CODES:
            self.assertTrue(view(poll(code), e=expected()).needs_send, code)
        self.assertTrue(view(poll("interrupted_new"), e=expected("switch_pending")).needs_send)
        self.assertFalse(view(poll("interrupted_new"), e=expected(confirmed=False)).needs_send)
        v = view(poll("unconfirmed"), e=expected(confirmed=False))
        self.assertEqual((v.confirm_pending_by, v.confirm_send_id), (JSMITH, SID))
        v = view(poll("interrupted_new"), e=expected("switch_pending"))
        self.assertEqual(v.confirm_pending_by, "")
        self.assertEqual(view(poll("rejected"), e=expected(confirmed=False, rejected=True)).confirm_pending_by, "")
        self.assertEqual(view(poll(), e=expected()).confirm_pending_by, "")
        self.assertTrue(view(poll()).has_preview)
        self.assertFalse(view(poll(running=dataclasses.replace(NEW, preview_sha256=""))).has_preview)
        self.assertFalse(view(poll(running=None)).has_preview)

    def test_test_field(self):
        v = view(poll("test_finished", printing=False), e=expected("test_finished", test=TEST_E.test))
        self.assertEqual(v.test, {"by": AKIM, "step": 3, "of": 10})
        self.assertIsNone(view(poll(), e=expected()).test)

    def test_effective_origin(self):
        self.assertEqual(hs.effective_origin(poll(), expected()), ORIGIN)
        self.assertEqual(hs.effective_origin(poll(running=OLD), expected()), OLD.origin_line)
        self.assertEqual(hs.effective_origin(None, None), "")
        self.assertEqual(hs.effective_origin(None, expected()), ORIGIN)
        # Pre-printed: no origin item on the printer, the fingerprint proves the content
        pre = dataclasses.replace(NEW, origin_line="")
        e = expected(origin_line="USA (pre-printed)")
        self.assertEqual(hs.effective_origin(poll(running=pre), e), "USA (pre-printed)")
        self.assertEqual(hs.effective_origin(poll(running=pre), dataclasses.replace(e, fingerprint="")), "")
        self.assertEqual(hs.effective_origin(poll(running=dataclasses.replace(pre, fingerprint="c" * 64)), e), "")
        # A standalone status whose MSGNAM differs from E's name
        standalone = poll("unknown_while_down", running=None, message_name="SOMETHING-ELSE", standalone=True)
        self.assertEqual(hs.effective_origin(standalone, expected()), "")
        self.assertEqual(hs.effective_origin(dataclasses.replace(standalone, message_name=NEW.name), expected()), ORIGIN)
        self.assertEqual(view(poll(running=OLD), e=expected()).origin_line, OLD.origin_line)
        self.assertEqual(view(poll(running=OLD), e=expected()).message_name, OLD.name)


# --------------------------------------------------------------------------
# 9 and 10. Lines, the board and floor rows
# --------------------------------------------------------------------------

def mview(name, colour, code, *, origin=ORIGIN, printing=True, monitored=True, age=5.0):
    return MachineView(machine_id=name, line_id=LINE_ID, name=name, colour=colour, code=code,
                       headline=Msg(hs.STATE_TEXTS[code]), origin_line=origin, printing=printing, monitored=monitored,
                       checked_age_s=age)


class LineTests(unittest.TestCase):
    def test_worst(self):
        self.assertEqual(hs.worst([]), GREY)
        self.assertEqual(hs.worst([GREEN, GREY]), GREY)
        self.assertEqual(hs.worst([GREEN, BLUE, GREY]), BLUE)
        self.assertEqual(hs.worst([BLUE, AMBER]), AMBER)
        self.assertEqual(hs.worst([GREEN, RED, AMBER]), RED)
        self.assertEqual(hs.worst([GREEN]), GREEN)

    def test_line_colour_and_headline(self):
        ms = [mview("Bagger 1", GREEN, "running_confirmed"), mview("Bagger 2", RED, "drift", age=12.0)]
        lv = hs.line_view(LINE, ms, None)
        self.assertEqual((lv.colour, lv.monitored, lv.checked_age_s, lv.origins_differ), (RED, True, 12.0, False))
        self.assertEqual(lv.headline, Msg(hs.T_MACHINE_STATE, {"machine": "Bagger 2", "state": ms[1].headline}))
        same = [mview("Bagger 1", GREEN, "running_confirmed"), mview("Bagger 2", GREEN, "running_confirmed")]
        self.assertEqual(hs.line_view(LINE, same, None).headline, Msg("Printing"))
        self.assertEqual(hs.line_view(LINE, [], None).headline, Msg("No machines"))
        self.assertEqual(hs.line_view(LINE, [], None).colour, GREY)
        lost = [mview("Bagger 1", GREY, "not_monitored", monitored=False, age=40.0)]
        lv = hs.line_view(LINE, lost, None)
        self.assertEqual((lv.headline, lv.monitored), (Msg("NOT MONITORED"), False))
        # The worst machine is the first by name among equals
        two_red = [mview("Bagger 1", RED, "drift"), mview("Bagger 2", RED, "ink_out")]
        self.assertEqual(hs.line_view(LINE, two_red, None).headline.values["machine"], "Bagger 1")

    def test_origins_differ(self):
        ms = [mview("Bagger 1", GREEN, "running_confirmed"),
              mview("Bagger 2", GREEN, "running_confirmed", origin="Product of Mexico")]
        lv = hs.line_view(LINE, ms, None)
        self.assertEqual((lv.origins_differ, lv.colour, lv.headline), (True, AMBER, Msg(hs.T_ORIGINS_DIFFER)))
        stopped = [ms[0], mview("Bagger 2", GREY, "stopped", origin="Product of Mexico", printing=False)]
        lv = hs.line_view(LINE, stopped, None)
        self.assertEqual((lv.origins_differ, lv.colour), (False, GREY))
        unknown = [ms[0], mview("Bagger 2", AMBER, "message_unchecked", origin="")]
        self.assertTrue(hs.line_view(LINE, unknown, None).origins_differ)
        rows = hs.floor_board_rows(BoardView(lines=[hs.line_view(LINE, ms, None)], share_ok=True))
        self.assertEqual([(r.colour, r.status, r.origin_line) for r in rows],
                         [(AMBER, Msg(hs.T_ORIGINS_DIFFER), ORIGIN),
                          (AMBER, Msg(hs.T_ORIGINS_DIFFER), "Product of Mexico")])

    def test_natural_key(self):
        names = ["Line 10", "Line 2", "line 1", "Line 1b"]
        self.assertEqual(sorted(names, key=hs.natural_key), ["line 1", "Line 1b", "Line 2", "Line 10"])

    def test_board_and_not_set_up(self):
        m3 = Machine(id="mc_0000000003", name="Line 1 Bagger 3", code="B3", line_id=LINE_ID,
                     printer_id="pr_0000000003", placement="delay")
        m4 = Machine(id="mc_0000000004", name="Line 1 Bagger 4", code="B4", line_id=LINE_ID, printer_id=PID2,
                     placement="delay")
        m5 = Machine(id="mc_0000000005", name="Line 1 Bagger 5", code="B5", line_id=LINE_ID,
                     printer_id="pr_0000000005", placement="delay")
        off_line = Line(id="ln_0000000009", name="Line 9", code="L9", cartridge="plastic", active=False)
        data = dataclasses.replace(
            DATA, lines={**DATA.lines, off_line.id: off_line},
            machines={**DATA.machines, m3.id: m3, m4.id: m4, m5.id: m5},
            printers={**DATA.printers, PID2: dataclasses.replace(PRINTER2, active=False),
                      "pr_0000000005": dataclasses.replace(PRINTER, id="pr_0000000005", token="")})
        board = hs.board_view(data, {MID: poll()}, {MID: 3.0}, {MID: expected()}, {}, {}, {}, {},
                              share_ok=True, standalone=False, now=T0)
        self.assertEqual([lv.name for lv in board.lines], ["Line 1", "Line 10"])
        line1 = board.lines[0]
        self.assertEqual([m.name for m in line1.machines],
                         ["Line 1 Bagger 1", "Line 1 Bagger 2", "Line 1 Bagger 3", "Line 1 Bagger 4",
                          "Line 1 Bagger 5"])
        codes = [m.code for m in line1.machines]
        self.assertEqual(codes, ["running_confirmed", "checking", "not_set_up", "not_set_up", "not_set_up"])
        for m in line1.machines[2:]:
            self.assertEqual((m.colour, m.alerts, m.can_stop, m.needs_send), (GREY, [], False, False))
        self.assertEqual((board.share_ok, board.standalone, board.generated_at), (True, False, T0))
        rows = hs.floor_board_rows(board)
        self.assertEqual([r.machine_name for r in rows], [m.name for m in line1.machines] + ["Line 10 Bagger 1"])
        self.assertEqual(rows[0].checked_age_s, 3.0)
        self.assertEqual(rows[0].line_name, "Line 1")
        self.assertEqual(rows[0].origin_line, ORIGIN)


# --------------------------------------------------------------------------
# 11 and 12. Sounding and texts
# --------------------------------------------------------------------------

def board_with(*alerts_):
    m = mview("Bagger 1", RED, "drift")
    m.alerts = list(alerts_)
    return BoardView(lines=[LineView(line_id=LINE_ID, name="Line 1", colour=RED, headline=Msg("x"),
                                     machines=[m, dataclasses.replace(m, machine_id="b2")])], share_ok=True)


class SoundingTests(unittest.TestCase):
    def test_sounding(self):
        me = Identity(user="example\\AKIM", computer="FLOOR02", sids=frozenset(), pid=1)
        mine_new = alert("drift", at=T0 - 10, recipients=["user:" + AKIM])
        mine_old = alert("printing_off", at=T0 - 100, recipients=["user:" + AKIM])
        floor = alert("ink_out", at=T0 - 50, recipients=["floor"])
        warning = alert("low_ink", recipients=["user:" + AKIM, "floor"])
        acked = alert("went_silent", recipients=["user:" + AKIM], acked_by=JSMITH)
        other = alert("stop_failed", recipients=["user:" + JSMITH])
        board = board_with(mine_new, mine_old, floor, warning, acked, other)
        self.assertEqual([a.id for a in hs.sounding_alerts(board, me, floor_board=False)], [mine_old.id, mine_new.id])
        self.assertEqual([a.id for a in hs.sounding_alerts(board, me, floor_board=True)],
                         [mine_old.id, floor.id, mine_new.id])
        self.assertEqual(hs.sounding_alerts(None, me, floor_board=True), [])
        nobody = Identity(user="EXAMPLE\\guest", computer="FLOOR02", sids=frozenset(), pid=1)
        self.assertEqual(hs.sounding_alerts(board, nobody, floor_board=False), [])


class TextTests(unittest.TestCase):
    def test_checked_text(self):
        self.assertEqual(hs.checked_text(None), Msg("Not checked yet"))
        self.assertEqual(hs.checked_text(0), Msg("checked {seconds} s ago", {"seconds": 0}))
        self.assertEqual(hs.checked_text(119.9), Msg("checked {seconds} s ago", {"seconds": 119}))
        self.assertEqual(hs.checked_text(120), Msg("checked {minutes} min ago", {"minutes": 2}))
        self.assertEqual(hs.checked_text(359), Msg("checked {minutes} min ago", {"minutes": 5}))

    def test_banner(self):
        self.assertIsNone(hs.board_banner(BoardView(lines=[], share_ok=True)))
        self.assertEqual(hs.board_banner(BoardView(lines=[], share_ok=False)), Msg(hs.T_BANNER_DOWN))
        b = BoardView(lines=[], share_ok=False, standalone=True, next_check_s=17.6)
        self.assertEqual(hs.board_banner(b), Msg(hs.T_BANNER_STANDALONE, {"seconds": 17}))
        self.assertEqual(hs.board_banner(dataclasses.replace(b, next_check_s=None)).values, {"seconds": 0})

    def test_details(self):
        out = classify(facts(reachable=False, failures=3), expected(), prev=poll(bags_recent=0, unreachable_since=T0 - 130))
        self.assertIn(Msg(hs.T_NOT_ANSWERING, {"minutes": 2}), out.details)
        out = classify(facts(reachable=False, failures=3), expected(), prev=poll(bags_recent=0, unreachable_since=T0 - 30))
        self.assertNotIn("Not answering for {minutes} min.", [m.text for m in out.details])
        out = classify(facts(running=None), expected(), prev=poll(running_ok_at=T0 - 250))
        self.assertEqual(out.details, [Msg(hs.T_LAST_READ, {"minutes": 4})])
        out = classify(facts(running=None), expected(), prev=poll(running_ok_at=T0 - 5))
        self.assertEqual(out.details, [Msg(hs.T_LAST_READ, {"minutes": 1})])
        out = classify(facts(running=None), expected(), prev=None)
        self.assertEqual(out.details, [Msg(hs.T_NOT_READ_YET)])
        out = classify(facts(), expected(notes=["pc_time", "no_lock"]))
        self.assertEqual(out.details[1:3], [hx_message.NOTE_TEXTS["pc_time"], hx_message.NOTE_TEXTS["no_lock"]])
        out = classify(facts(), expected(failure=failure(True)))
        self.assertEqual(out.details[0], failure(True).msg)
        out = classify(facts(printing=False), expected("test_finished", test=TEST_E.test))
        self.assertEqual(out.details, [Msg(hs.T_AFTER_TEST, {"user": AKIM}), Msg(hs.T_MESSAGE_IN_SLOT,
                                                                                  {"name": NEW.name, "slot": 9})])


# --------------------------------------------------------------------------
# 13 to 18
# --------------------------------------------------------------------------

class MessageCheckTests(unittest.TestCase):
    def test_message_unchecked(self):
        out = classify(facts(running=None), expected())
        self.assertEqual((out.code, kinds(out)), ("message_unchecked", []))
        self.assertEqual(hs.STATE_CODES[out.code], AMBER)
        j = judge(poll(), reading(running=None, running_reset=True, running_unchecked=True), expected())
        self.assertEqual(j.state.code, "message_unchecked")
        (a,) = j.alerts
        self.assertEqual((a.kind, a.sound, a.colour), ("message_unreadable", False, AMBER))
        self.assertEqual(a.recipients, ["user:" + AKIM, "admins", "monitor"])
        p = dataclasses.replace(poll(), state=j.state, alerts=j.alerts, running=None, running_unchecked=True)
        j2 = commit_second(p, reading(running_unchecked=False), expected())
        self.assertEqual(j2.state.code, "running_confirmed")
        self.assertEqual([x.kind for x in j2.ended], ["message_unreadable"])
        self.assertEqual(classify(facts(running=None), expected(), standalone=True).code, "running_confirmed")

    def test_standalone_without_a_prin_reading(self):
        # Standalone checks never load the message: when printing hasn't been read either (a busy or
        # nonsense PRIN since the outage began), nothing stands behind green "Printing" (C12)
        out = classify(facts(running=None, printing=None), expected(), standalone=True)
        self.assertEqual((out.code, kinds(out)), ("unknown_while_down", []))
        self.assertEqual(hs.STATE_CODES[out.code], AMBER)
        self.assertEqual(classify(facts(running=None), expected(), standalone=True).code, "running_confirmed")
        # Share mode keeps "printing on or not yet read" with R known (§2.2 running_confirmed)
        self.assertEqual(classify(facts(printing=None), expected()).code, "running_confirmed")
        # Other modes, no E and an untrusted printer are as before
        stopped = expected("stopped_by_user", stopped_by=AKIM)
        self.assertEqual(classify(facts(running=None, printing=None), stopped, standalone=True).code, "stopped")
        self.assertEqual(classify(facts(running=None, printing=None), None, standalone=True).code, "idle")
        self.assertEqual(classify(facts(running=None, printing=None, reachable=False), expected(),
                                  standalone=True).code, "off")
        # The first standalone check (no prev) commits it after its second reading
        j = commit_second(None, reading(running=None, printing=None, standalone=True), expected())
        self.assertEqual(j.state.code, "unknown_while_down")

    def test_origin_unknown_while_unchecked(self):
        self.assertEqual(hs.effective_origin(poll("message_unchecked", running=None), expected()), "")
        self.assertEqual(hs.effective_origin(poll("message_unchecked"), expected()), "")
        self.assertEqual(hs.effective_origin(poll("ink_out", running_unchecked=True), expected()), "")
        self.assertEqual(hs.effective_origin(poll("ink_out"), expected()), ORIGIN)


class OriginAfterFailureTests(unittest.TestCase):
    def test_failed_content_check(self):
        e = expected("switch_pending", failure=failure(True))
        wrong = dataclasses.replace(NEW, origin_line="Product of Mexico", fingerprint="c" * 64)
        self.assertEqual(hs.effective_origin(poll("failed_after_switch", running=wrong), e), "Product of Mexico")
        for e2 in (e, dataclasses.replace(e, left_by=AKIM)):
            p = poll("failed_after_switch", running=None, message_name=e2.message_name)
            self.assertEqual(hs.effective_origin(p, e2), "")
        p = poll("interrupted_unknown", running=None, message_name=NEW.name)
        self.assertEqual(hs.effective_origin(p, expected("switch_pending")), "")
        p = poll("unknown_while_down", running=None, message_name=NEW.name, standalone=True)
        self.assertEqual(hs.effective_origin(p, expected("switch_pending")), "")
        self.assertEqual(hs.effective_origin(None, expected("switch_pending")), "")


class SenderTests(unittest.TestCase):
    def test_sender_not_last_writer(self):
        e = expected(confirmed=False, sender=JSMITH, written_by=AKIM)
        self.assertEqual(view(poll("unconfirmed"), e=e).confirm_pending_by, JSMITH)
        j = commit_second(poll(), reading(running=OLD), dataclasses.replace(e, confirmed=True))
        self.assertIn("user:" + JSMITH, j.alerts[0].recipients)
        self.assertNotIn("user:" + AKIM + "x", j.alerts[0].recipients)
        out = classify(facts(printing=False), dataclasses.replace(STOPPED, written_by=JSMITH, stopped_by=AKIM))
        self.assertEqual(out.values, {"user": AKIM})
        out = classify(facts(printing=False), dataclasses.replace(STOPPED, stopped_by=""))
        self.assertEqual(out.values, {"user": JSMITH})


class StopAfterTestTests(unittest.TestCase):
    def test_stop_reason_test(self):
        e = expected("stop_requested_at_printer", stop_reason="test", printing_expected=False)
        out = classify(facts(printing=False), e)
        self.assertEqual(out.code, "test_finished")
        self.assertIn("test_finished", hs.NEEDS_SEND_CODES)
        self.assertEqual(classify(facts(), e).code, "stop_failed")
        e2 = expected("stopped_by_user", stop_reason="test", printing_expected=False)
        self.assertEqual(classify(facts(printing=False), e2).code, "test_finished")
        self.assertEqual(hs.expected_code(e2), "test_finished")
        self.assertEqual(hs.expected_code(e), "stop_requested")
        self.assertEqual(hs.expected_code(expected("test_finished")), "test_finished")
        self.assertEqual(hs.expected_code(STOPPED), "stopped")
        self.assertIsNone(hs.expected_code(expected("switch_pending")))
        self.assertIsNone(hs.expected_code(expected("none")))
        self.assertIsNone(hs.expected_code(TEST_E))
        self.assertEqual(hs.expected_code(expected(rejected=True)), "rejected")
        self.assertEqual(hs.expected_code(dataclasses.replace(STOPPED, rejected=True)), "stopped_needs_send")
        self.assertEqual(hs.expected_code(expected(notes=["no_lock"])), "confirmed_with_notes")


class OtherLockTests(unittest.TestCase):
    def test_unknown_purposes(self):
        damaged = lock("unknown", token="damaged-0a1b2c3d", user="", computer="")
        for lk in (damaged, lock("future")):
            v = view(poll(), e=expected(), lk=lk, lock_age=1.0)
            self.assertEqual((v.code, v.colour), ("action_in_progress", BLUE))
            self.assertEqual(v.headline.text, hs.STATE_TEXTS["action_in_progress:other"])
        self.assertEqual(view(poll(), e=expected(), lk=damaged, lock_age=1.0).headline.values,
                         {"user": "", "computer": ""})


class NotFromStripesTests(unittest.TestCase):
    def test_warning(self):
        j = commit_second(poll("idle", printing=False, running=OLD), reading(running=OLD), None)
        self.assertEqual(j.state.code, "not_from_stripes")
        (a,) = j.alerts
        self.assertEqual((a.kind, a.sound, a.recipients), ("not_from_stripes", False, ["user:" + AKIM]))
        p = dataclasses.replace(poll(running=OLD), state=j.state, alerts=j.alerts)
        j2 = commit_second(p, reading(running=OLD, printing=False), None)
        self.assertEqual(j2.state.code, "idle")
        self.assertEqual([x.kind for x in j2.ended], ["not_from_stripes"])


if __name__ == "__main__":
    unittest.main()
