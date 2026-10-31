"""MapleJet sending: the Send pipeline, the sender's answer, Stop, Resume, Set clocks and the test print.

Everything here runs on worker threads, never the Tk thread, and reports through on_event (the UI
passes bus.post). A Send, for each machine at the same time and one request at a time to each
printer:

- takes the printer's lock and waits for the PC polling it to step aside (during the countdown);
- checks the machine (UID, encoder, fonts, ink, which slot is printing) and sets its clock;
- builds the new message in the idle slot (9 or 10, never the one selected) and reads it back;
- records "switch pending", switches, and reads the running message back twice.

A machine stops at the first step that fails and nothing is retried, switched back or turned off by
itself; the other machines carry on. Country of origin: no machine is switched to a message whose
idle-slot read-back didn't match what was sent, and with a held lock nothing is switched unless
"switch pending" was recorded on the share first.

PrinterActions does Stop, Resume and Set clocks; TestPrintRun runs a test print or a calibration
run on one machine under its lock and, once it has checked the printer's UID, always ends with
printing off and both slots emptied.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import math
import secrets
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from hx_core import SYSTEM_CLOCK, CancelToken, Clock, Msg
from hx_message import (AMBER_NOTES, CLOCK_SET_TOLERANCE_S, NOTE_TEXTS, STEP_TEXTS, build_message, cleared,
                        clock_command, compare, date_differences, empty_message_commands, expected_settings,
                        fingerprint, idle_slot, merge_plan, origin_from_design, result_colour, resume_mismatch,
                        resume_refusal, test_step_plan)
from hx_model import (AMBER, GREEN, RED, SLOTS, STEP_BUILD, STEP_CHECK_CHOICE, STEP_CHECK_MACHINE, STEP_CONFIRM_1,
                      STEP_CONFIRM_2, STEP_LOCK, STEP_OPERATOR, STEP_SET_CLOCK, STEP_SWITCH, STEP_VERIFY_IDLE,
                      STEP_WAIT_POLLER, TIMINGS, ActionEvent, ActionFinished, ActionResult, AuthFailed, CheckFailed,
                      Command, Difference, ExpectedState, FailureInfo, ForbiddenCommand, HxError, LineStatus,
                      LockHeld, LockLost, MachineResult, MessageDesign, NotAllowed, PlanError, PrinterError,
                      ResumeCheck, RunningMessage, SendEvent, SendFinished, SendRequest, SendResult, ShareError,
                      ShareUnavailable, TestEvent, TestFinished, TestResult, Timings, WrongPrinter, from_json,
                      new_id, same_user, to_json)
from hx_protocol import clock_from, worst_case_s
from hx_time import to_local
from i18n import N_, add_spanish, tr

ES = {
    "The templates changed since you checked them. Look at the Send window again.":
        "Las plantillas cambiaron desde que las revisó. Vuelva a mirar la ventana de envío.",
    "{line} is being sent by {user}.": "{user} está enviando a {line}.",
    "{machine} is running a test print by {user}.": "{user} está haciendo una impresión de prueba en {machine}.",
    "{machine} is being changed by {user}.": "{user} está haciendo cambios en {machine}.",
    "Waited {seconds} s for the PC polling {machine} to step aside. Try again.":
        "Se esperó {seconds} s a que la PC que monitorea {machine} se apartara. Inténtelo de nuevo.",
    "{machine}'s encoder settings changed: wheel {wheel} mm, {ppr} pulses per turn. Every print would move. "
    "Ask an administrator.":
        "La configuración del encoder de {machine} cambió: rueda de {wheel} mm, {ppr} pulsos por vuelta. Todas las "
        "impresiones se moverían. Avise a un administrador.",
    "{machine}'s printer has fonts that differ from Stripes' standard list.":
        "La impresora de {machine} tiene fuentes distintas de la lista estándar de Stripes.",
    "{machine} reports no ink.": "{machine} no informa ninguna tinta.",
    "{machine} reports ink \"{ink}\", which isn't on Stripes' ink list.":
        "{machine} informa la tinta \"{ink}\", que no está en la lista de tintas de Stripes.",
    "{machine} is set for black ink; this film needs white.":
        "{machine} está configurada para tinta negra; esta película necesita tinta blanca.",
    "{machine} is set for white ink; this film needs black.":
        "{machine} está configurada para tinta blanca; esta película necesita tinta negra.",
    "{machine} is set for paper cartridges; this film needs plastic.":
        "{machine} está configurada para cartuchos de papel; esta película necesita plástico.",
    "{machine} is set for plastic cartridges; this film needs paper.":
        "{machine} está configurada para cartuchos de plástico; esta película necesita papel.",
    "{machine}'s clock reads {printer_time} after it was set to {expected}.":
        "El reloj de {machine} marca {printer_time} después de ajustarlo a {expected}.",
    "The message read back from {machine} differs from what was sent ({n} differences).":
        "El mensaje leído de {machine} difiere de lo enviado ({n} diferencias).",
    "The message read back from {machine} differs from what was sent (1 difference).":
        "El mensaje leído de {machine} difiere de lo enviado (1 diferencia).",
    "The date {machine} would print differs from the date Stripes expects.":
        "La fecha que imprimiría {machine} difiere de la fecha que espera Stripes.",
    "{machine} is printing slot {got}, not slot {slot}.": "{machine} está imprimiendo la ranura {got}, no la ranura {slot}.",
    "{machine} didn't turn printing on.": "{machine} no activó la impresión.",
    "Stripes had an internal error at this step: {error}": "Stripes tuvo un error interno en este paso: {error}",
    "This machine may be printing the new message, the old one, or nothing. Check the bags now.":
        "Esta máquina puede estar imprimiendo el mensaje nuevo, el anterior o nada. Revise las bolsas ahora.",
    "Stripes can't find this Send yet. Try again in a minute.":
        "Stripes todavía no encuentra este envío. Inténtelo de nuevo en un minuto.",
    "Only {sender} can confirm this Send.": "Solo {sender} puede confirmar este envío.",
    "Your answer was logged, but Stripes couldn't update the monitor for {machines}.":
        "Su respuesta quedó registrada, pero Stripes no pudo actualizar el monitor de {machines}.",
    "Your answer was logged, but Stripes couldn't update the monitor.":
        "Su respuesta quedó registrada, pero Stripes no pudo actualizar el monitor.",
    "Unknown machine": "Máquina desconocida",
    "Stripes couldn't record the switch on the server, so it didn't switch.":
        "Stripes no pudo registrar el cambio en el servidor, así que no cambió de mensaje.",
    "Only Stripes MapleJet Senders can stop, resume or set clocks.":
        "Solo los miembros de Stripes MapleJet Senders pueden detener, reanudar o ajustar relojes.",
    "Stripes couldn't stop printing on {machine}. Stop it at the printer with its Print On/Off button now.":
        "Stripes no pudo detener la impresión en {machine}. Deténgala ahora en la impresora con su botón Print On/Off.",
    "Stripes can't reach the server, so it can't check the last confirmed Send.":
        "Stripes no puede conectarse con el servidor, así que no puede revisar el último envío confirmado.",
    "A test print needs the server, so that nobody else uses the printer meanwhile.":
        "Una impresión de prueba necesita el servidor, para que nadie más use la impresora mientras tanto.",
    "{machine}'s ink doesn't match the film: the marks may not show.":
        "La tinta de {machine} no coincide con la película: puede que las marcas no se vean.",
    "No prints for {seconds} s, so the test was stopped.":
        "No hubo impresiones durante {seconds} s, así que se detuvo la prueba.",
    "The test ran past its time limit, so it was stopped.": "La prueba superó su tiempo límite, así que se detuvo.",
    "Stopped by {user}.": "Detenida por {user}.",
    "Slot {slot} on {machine} couldn't be emptied.": "No se pudo vaciar la ranura {slot} de {machine}.",
    "{machine} has had a Send or another test since this test, so Stripes didn't change it.":
        "{machine} tuvo un envío u otra prueba después de esta prueba, así que Stripes no la cambió.",
    "The bag counter setting changed since this test was set up. Set it up again.":
        "La configuración del contador de bolsas cambió desde que se preparó esta prueba. Vuelva a prepararla.",
}
add_spanish(ES)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

ACTION_STOP = "stop"                              # action steps (MachineResult.step of an ActionResult)
ACTION_RESUME = "resume"
LOG_TYPES = {"send": "hx-sends", "send_switch": "hx-sends", "send_confirm": "hx-sends",
             "send_leave": "hx-sends", "stop": "hx-sends", "resume": "hx-sends", "clock_set": "hx-sends",
             "test_start": "hx-admin", "test_step": "hx-admin", "test_end": "hx-admin",
             "internal_error": "hx-events"}

# MachineResult.notes, in the order they are listed
NOTE_ORDER = ("pc_time", "no_lock", "local_copy", "ink_mismatch", "film_stop", "cancelled")
READBACK_STEPS = (STEP_VERIFY_IDLE, STEP_CONFIRM_1, STEP_CONFIRM_2)
LOG_SEARCH_S = 400 * 86_400.0                     # how far back the send_switch entries are looked for
TRACEBACK_MAX = 4_000                             # internal_error keeps this much of the traceback
_WEB_CALLS = ("load_slot", "fonts", "firmware")   # their deadline is also their own worst case (protocol.md §8.3)

_ONLY_SENDERS = N_("Only Stripes MapleJet Senders can send.")                      # hx_message's text
_ONLY_ACTIONS = N_("Only Stripes MapleJet Senders can stop, resume or set clocks.")
_ONLY_ADMINS = N_("Only Stripes Administrators can run test prints.")              # hx_message's text
_NOT_LOADED = N_("The MapleJet settings haven't loaded yet.")                      # hx_share's text
_AFTER_SWITCH = N_("This machine may be printing the new message, the old one, or nothing. Check the bags now.")
_STOP_AT_PRINTER = N_("Stripes couldn't stop printing on {machine}. Stop it at the printer with its Print On/Off "
                      "button now.")
_NEEDS_SERVER = N_("A test print needs the server, so that nobody else uses the printer meanwhile.")
_COUNTER_CHANGED = N_("The bag counter setting changed since this test was set up. Set it up again.")
_NO_SERVER = N_("Stripes can't reach the server.")                                 # hx_share's text
_UNKNOWN_MACHINE = N_("Unknown machine")          # a machine missing from the plan: texts never show an id
_NOT_UPDATED = N_("Your answer was logged, but Stripes couldn't update the monitor for {machines}.")
_NOT_UPDATED_UNNAMED = N_("Your answer was logged, but Stripes couldn't update the monitor.")
_CANT_CHECK = N_("Stripes can't reach the server, so it can't check the last confirmed Send.")


# --------------------------------------------------------------------------
# The red window's rows, and the send log
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class FailureRow:
    """One machine in the red window (send.md §12.2)."""
    machine_id: str
    machine_name: str
    line_name: str
    step: str                                     # STEP_*
    step_text: Msg
    msg: Msg | None
    after_switch: bool
    after_switch_text: Msg | None                 # "This machine may be printing the new message, ..."
    previous_name: str                            # what it was printing before this Send ("" if unknown)
    previous_origin: str                          # bag text
    previous_preview: bytes                       # BMP, b"" if unknown
    previous_confirmed: bool = False              # previous is the last Send's confirmed message
    differences: list[Difference] = field(default_factory=list)


def failure_rows(result: SendResult, plan) -> list[FailureRow]:
    """One row per machine that failed, in result order; a cancelled machine has none. Called by the
    red window on the Tk thread: a machine missing from the plan (it can't be, but a failure must
    still show) is "Unknown machine" in the screen's language, never its id."""
    plans = {mp.machine.id: mp for mp in plan.machines}
    rows = []
    for r in result.machines:
        if r.ok or "cancelled" in r.notes:
            continue
        mp = plans.get(r.machine_id)
        differences = []
        if mp is not None and r.step in READBACK_STEPS and r.readbacks and r.slot in SLOTS:
            try:
                differences = compare(build_message(mp, r.slot), r.readbacks[-1])
            except ValueError:
                differences = []
        previous = r.previous
        rows.append(FailureRow(
            machine_id=r.machine_id, machine_name=mp.machine.name if mp else tr(_UNKNOWN_MACHINE),
            line_name=mp.line.name if mp else "", step=r.step,
            step_text=STEP_TEXTS.get(r.step, Msg(r.step)), msg=r.msg, after_switch=r.after_switch,
            after_switch_text=Msg(_AFTER_SWITCH) if r.after_switch else None,
            previous_name=previous.name if previous else "", previous_origin=previous.origin_line if previous else "",
            previous_preview=r.previous_preview or b"", previous_confirmed=r.previous_confirmed,
            differences=differences))
    return rows


def is_cancelled(result: SendResult) -> bool:
    """Every machine carries the note "cancelled": nothing was sent, and no red window opens."""
    return bool(result.machines) and all("cancelled" in r.notes for r in result.machines)


def design_to_log(design: MessageDesign) -> dict:
    """to_json without the picture's bytes, plus preview_b64 (the BMP in base64)."""
    d = to_json(dataclasses.replace(design, preview_bmp=b""))
    d.pop("preview_bmp", None)
    d["preview_b64"] = base64.b64encode(design.preview_bmp or b"").decode("ascii")
    return d


def design_from_log(d: dict) -> MessageDesign:
    d = dict(_log_shape(d, dict, "readback"))
    picture = _log_shape(d.pop("preview_b64", "") or "", str, "preview_b64")
    d.pop("preview_bmp", None)
    design = from_json(MessageDesign, d)
    design.preview_bmp = base64.b64decode(picture)
    return design


def _result_to_log(r: MachineResult) -> dict:
    d = to_json(dataclasses.replace(r, readbacks=[], previous_preview=b""))
    d.pop("previous_preview", None)
    d["readbacks"] = [design_to_log(x) for x in r.readbacks]
    d["previous_preview_b64"] = base64.b64encode(r.previous_preview or b"").decode("ascii")
    return d


def _result_from_log(d: dict) -> MachineResult:
    d = dict(_log_shape(d, dict, "result"))
    readbacks = [design_from_log(x) for x in _log_shape(d.pop("readbacks", None) or [], list, "readbacks")]
    picture = _log_shape(d.pop("previous_preview_b64", "") or "", str, "previous_preview_b64")
    d.pop("previous_preview", None)
    r = from_json(MachineResult, d)
    r.readbacks = readbacks
    r.previous_preview = base64.b64decode(picture)
    return r


def _log_shape(value, kind, where):
    """A list, object or text read back from a log entry: ValueError when it is anything else (a damaged
    entry), never the TypeError iterating or indexing it would raise."""
    if not isinstance(value, kind):
        raise ValueError(f"{where}: expected {kind.__name__}, got {type(value).__name__}")
    return value


def _log_number(value, where):
    """A time read back from a log entry. Anything but a finite number is a damaged entry: ValueError, as
    hx_model.from_json treats a damaged file, never OverflowError (a 400-digit int) or TypeError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: not a number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{where}: number out of range") from None
    if not math.isfinite(number):
        raise ValueError(f"{where}: not a finite number")
    return number


def result_from_log(entry: dict) -> SendResult:
    """The "send" log entry back into a SendResult, pictures included (§17.2). ValueError (or KeyError)
    for a damaged entry."""
    data_as_of = entry.get("data_as_of")
    return SendResult(
        send_id=entry["send_id"], sender=entry.get("sender", ""), computer=entry.get("computer", ""),
        started_at=_log_number(entry.get("started_at") or 0.0, "started_at"),
        finished_at=_log_number(entry.get("finished_at") or 0.0, "finished_at"),
        machines=[_result_from_log(_log_shape(m, dict, "machine")["result"])
                  for m in _log_shape(entry.get("machines", []), list, "machines")],
        time_source=entry.get("time_source", "pc"), from_local_copy=bool(entry.get("from_local_copy", False)),
        data_as_of=_log_number(data_as_of, "data_as_of") if data_as_of is not None else None,
        log_name=entry.get("log_name", ""), log_queued=False)


def request_from_log(entry: dict) -> SendRequest:
    """The Send's request, for "Fix and send again"."""
    return from_json(SendRequest, entry["request"])


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

class _Failed(HxError):
    """A step failed with a message of its own (and maybe a note for the result)."""

    def __init__(self, msg, *, note="", differences=None, step=""):
        super().__init__(msg, step=step)
        self.note = note
        self.differences = list(differences) if differences else []


class _Stop(Exception):
    """The test print's cancel token fired."""


class _Early(Exception):
    """A test print can't take the printer (the lock or the poller): it ends before any request."""

    def __init__(self, msg):
        super().__init__(msg.english())
        self.msg = msg


def _ordered(notes):
    """Notes without repeats, in NOTE_ORDER (unknown ones last, as given)."""
    seen = list(dict.fromkeys(notes))
    known = [n for n in NOTE_ORDER if n in seen]
    return known + [n for n in seen if n not in NOTE_ORDER]


def _amber(notes):
    return [n for n in _ordered(notes) if n in AMBER_NOTES]


def _safe_post(on_event, event):
    try:
        on_event(event)
    except Exception:
        traceback.print_exc()


def _holder_msg(error, *, line_name, machine_name):
    """What the operator sees when someone else holds the printer's lock (send.md §10.3)."""
    info = getattr(error, "info", None)
    purpose = info.purpose if info is not None else ""
    user = info.user if info is not None else ""
    if purpose == "send":
        return Msg(N_("{line} is being sent by {user}."), {"line": line_name, "user": user})
    if purpose in ("test", "calibration"):
        return Msg(N_("{machine} is running a test print by {user}."), {"machine": machine_name, "user": user})
    if purpose in ("stop", "resume", "clock"):
        return Msg(N_("{machine} is being changed by {user}."), {"machine": machine_name, "user": user})
    return error.msg


def _waited_msg(machine_name, timings):
    return Msg(N_("Waited {seconds} s for the PC polling {machine} to step aside. Try again."),
               {"seconds": round(2 * timings.stale_s), "machine": machine_name})


def _stop_failed_msg(machine_name):
    return Msg(_STOP_AT_PRINTER, {"machine": machine_name})


def _internal_msg(error):
    # Only the class name: the text could hold bag text
    return Msg(N_("Stripes had an internal error at this step: {error}"), {"error": type(error).__name__})


def _msg_json(msg):
    return msg.to_json() if msg is not None else None


def _running_message(design, origin_line):
    return RunningMessage(slot=design.slot, name=design.name, fingerprint=fingerprint(design),
                          origin_line=origin_line, read_at=design.read_at,
                          preview_sha256=hashlib.sha256(design.preview_bmp or b"").hexdigest())


def _was_confirmed(before, previous):
    """previous is the last Send's confirmed message (send.md §10.5 item 7)."""
    return (before is not None and previous is not None and before.mode == "running" and before.confirmed
            and before.fingerprint == previous.fingerprint)


def _readback_problem(built, design, read_local, machine_name):
    """CheckFailed when the read-back differs from what was sent, the rendered dates included."""
    diffs = compare(built, design)
    dates = date_differences(built, design, read_local)
    if not diffs and not dates:
        return None
    if not diffs:
        msg = Msg(N_("The date {machine} would print differs from the date Stripes expects."),
                  {"machine": machine_name})
    elif len(diffs) + len(dates) == 1:
        msg = Msg(N_("The message read back from {machine} differs from what was sent (1 difference)."),
                  {"machine": machine_name})
    else:
        msg = Msg(N_("The message read back from {machine} differs from what was sent ({n} differences)."),
                  {"machine": machine_name, "n": len(diffs) + len(dates)})
    return CheckFailed(msg, differences=diffs + dates)


def _ink_problem(machine, film, ink_name, inks):
    """(msg, is a mismatch) for an ink that doesn't suit the film, else None."""
    on = {"machine": machine.name}
    if ink_name is None:
        return Msg(N_("{machine} reports no ink."), on), False
    ink = next((i for i in inks if i.name == ink_name), None)
    if ink is None:
        return Msg(N_("{machine} reports ink \"{ink}\", which isn't on Stripes' ink list."),
                   {"machine": machine.name, "ink": ink_name}), False
    if ink.colour != film.ink_colour:
        if ink.colour == "black":
            return Msg(N_("{machine} is set for black ink; this film needs white."), on), True
        return Msg(N_("{machine} is set for white ink; this film needs black."), on), True
    if ink.cartridge != film.cartridge:
        if ink.cartridge == "paper":
            return Msg(N_("{machine} is set for paper cartridges; this film needs plastic."), on), True
        return Msg(N_("{machine} is set for plastic cartridges; this film needs paper."), on), True
    return None


def _check_printer(client, plan, company, guard, on_ink, on_uid=None):
    """send.md §10.5 items 2 to 7 on one printer: (printing, the message printing now). guard(call) runs
    before every request and gives its deadline; on_ink(msg, mismatch) decides what an ink problem does;
    on_uid() is called once the UID has been checked."""
    machine, film = plan.machine, plan.film
    client.check_uid(deadline=guard("check_uid"))
    if on_uid is not None:
        on_uid()
    if machine.placement == "encoder" and machine.encoder_wheel_mm is not None and machine.encoder_ppr is not None:
        wheel = client.read("ENWHD", deadline=guard("read"))
        ppr = client.read("ENPPR", deadline=guard("read"))
        if wheel != round(machine.encoder_wheel_mm) or ppr != machine.encoder_ppr:
            raise _Failed(Msg(N_("{machine}'s encoder settings changed: wheel {wheel} mm, {ppr} pulses per turn. "
                                 "Every print would move. Ask an administrator."),
                              {"machine": machine.name, "wheel": wheel, "ppr": ppr}))
    fonts = client.fonts(deadline=guard("fonts"))
    if list(fonts) != list(company.standard_fonts):
        raise _Failed(Msg(N_("{machine}'s printer has fonts that differ from Stripes' standard list."),
                          {"machine": machine.name}))
    problem = _ink_problem(machine, film, client.read("INKT", deadline=guard("read")), company.inks)
    if problem is not None:
        on_ink(*problem)
    printing = client.read("PRIN", deadline=guard("read")) == 1
    current = client.load_slot(0, deadline=guard("load_slot"))
    return printing, current


def _clock_set(client, guard, time, reading, machine_name, record, *, before):
    """send.md §9.5: the TD from the reading, then DATE and TIME read back within the tolerance (one
    re-read for a pair read across midnight). Fills record for the log; raises on any failure."""
    if before:
        try:
            d0 = client.read("DATE", deadline=guard("read"))
            t0 = client.read("TIME", deadline=guard("read"))
            record["before"] = clock_from(d0, t0).strftime("%Y-%m-%d %H:%M:%S")
        except PrinterError:
            record["before"] = None     # only for the log
    # The guard first, then the time: a wait in the guard never makes the TD late
    deadline = guard("command")
    local = time.now_from(reading).local
    cmd = clock_command(local)
    sent = (local + timedelta(seconds=30)).replace(second=0, microsecond=0)
    record.update({"sent": sent.strftime("%Y-%m-%d %H:%M"), "source": reading.source, "server": reading.server,
                   "offset_s": round(reading.offset_s, 3)})
    client.command(cmd, deadline=deadline)
    for attempt in (1, 2):
        d = client.read("DATE", deadline=guard("read"))
        t = client.read("TIME", deadline=guard("read"))
        printer = clock_from(d, t)
        expected = time.now_from(reading).local
        difference = (printer - expected).total_seconds()
        record.update({"after": printer.strftime("%Y-%m-%d %H:%M:%S"), "difference_s": round(difference, 1)})
        if abs(difference) <= CLOCK_SET_TOLERANCE_S:
            return record
    raise CheckFailed(Msg(N_("{machine}'s clock reads {printer_time} after it was set to {expected}."),
                          {"machine": machine_name, "printer_time": printer.strftime("%Y-%m-%d %H:%M:%S"),
                           "expected": expected.strftime("%Y-%m-%d %H:%M")}),
                      differences=[Difference("clock", "DATE TIME", expected.strftime("%Y-%m-%d %H:%M"),
                                              printer.strftime("%Y-%m-%d %H:%M:%S"))],
                      step=STEP_SET_CLOCK)


def _write_line_flag(store, line_id, send_id, **fields):
    """A partial line-status update; when send_id is given, only for that Send's file. Best effort."""
    try:
        status = store.read_line_status(line_id)
        if send_id:
            if status is None or status.send_id != send_id:
                return
        elif status is None:
            status = LineStatus(line_id=line_id)
        store.write_line_status(dataclasses.replace(status, **fields))
    except (ShareError, ValueError) as e:
        print(f"Could not update the line status of {line_id}: {e}", file=sys.stderr)


def _log_internal(data, *, where, ref_key, ref, machine_id, step, error, at):
    entry = {"type": "internal_error", "where": where, ref_key: ref, "machine_id": machine_id, "step": step,
             "error": type(error).__name__, "traceback": traceback.format_exc()[-TRACEBACK_MAX:], "at": at}
    try:
        data.append_log("hx-events", entry)
    except Exception:
        traceback.print_exc()


class _Worker:
    """Shared by the three classes: the services they talk through and the poller handshake."""

    def __init__(self, *, store, data, clients, time, identity, clock: Clock = SYSTEM_CLOCK,
                 timings: Timings = TIMINGS):
        self.store = store
        self.data = data
        self.clients = clients
        self.time = time
        self.identity = identity
        self.clock = clock
        self.timings = timings

    def _current(self):
        current = self.data.current
        if current is None:
            raise ShareError(Msg(_NOT_LOADED))
        return current

    def _now(self):
        """The time server's time when a reading is at hand, else this PC's (never waits)."""
        try:
            return self.time.cached().utc
        except Exception:
            return self.clock.time()

    def _guard(self, lock, *, cancel=None):
        """The check before every printer request: the lock (with the call's worst case) and the cancel.
        guard(call) returns the deadline to give that call (send.md §10.5): the lock's lease end, so a
        call that passed this check and then waited (the gate, a frozen PC) is refused instead of sent
        after another PC may have taken the printer; for a web call, also no later than its own worst
        case from now. None for an Autopilot call without a lock on the share (local_only)."""
        def guard(call):
            if cancel is not None and cancel.cancelled:
                raise _Stop()
            budget = worst_case_s(call, self.timings)
            lock.check(budget_s=budget)
            lease = lock.lease_end()
            if call in _WEB_CALLS:
                own = self.clock.monotonic() + budget
                return own if lease is None else min(lease, own)
            return lease
        return guard

    @staticmethod
    def _lease_ran_out(lock, error):
        """After a printer call failed: the LockLost to report instead, when the lock ran out meanwhile (a
        call refused at its lease deadline says only "busy"); else None."""
        if lock is None or not isinstance(error, PrinterError):
            return None
        try:
            lock.check()
        except LockLost as lost:
            return lost
        return None

    def _handshake(self, lock, machine_id, deadline, abort=None):
        """send.md §10.4 step 2: "ready", "share_down", "timeout" or "cancelled". Raises LockLost, or a
        ShareError other than ShareUnavailable from the poll file."""
        interval = self.timings.lock_refresh_s / 20
        while True:
            if abort is not None and abort.cancelled:
                return "cancelled"
            lock.check()
            try:
                answer = self.store.check_poller_paused(machine_id, lock.token)
                if answer in ("stale", "missing"):
                    if self.store.check_poller_paused(machine_id, lock.token) != "polling":
                        return "ready"
                elif answer == "paused":
                    return "ready"
            except ShareUnavailable:
                return "share_down"
            if self.clock.monotonic() >= deadline:
                return "timeout"
            self.clock.sleep(interval, abort)

    def _poller_steps_aside(self, lock, machine_id):
        """The handshake without a countdown (Stop, Resume, Set clocks, the test print): "ready",
        "share_down" or "timeout", re-checked once right before the first request."""
        deadline = self.clock.monotonic() + 2 * self.timings.stale_s
        answer = self._handshake(lock, machine_id, deadline)
        while answer == "ready":
            try:
                if self.store.check_poller_paused(machine_id, lock.token) != "polling":
                    return "ready"
            except ShareUnavailable:
                return "share_down"
            answer = self._handshake(lock, machine_id, deadline)
        return answer

    def _read_expected(self, machine_id):
        """The machine's expected state under a held lock, or a fresh one when there is none."""
        state = self.store.read_expected(machine_id)
        return state if state is not None else ExpectedState(machine_id=machine_id)


# --------------------------------------------------------------------------
# The Send pipeline
# --------------------------------------------------------------------------

class _Run:
    """One run of SendPipeline.run, shared by its threads."""

    def __init__(self, send_id, plan, current, on_event):
        self.send_id = send_id
        self.plan = plan
        self.current = current
        self.company = current.company
        self.on_event = on_event
        self.reading = None
        self.go = threading.Event()     # set at "go", or when the Send is cancelled
        self.cancelled = False
        self.abort = CancelToken()      # cancels the machine threads' handshake waits


class _Machine:
    """One machine of a Send, as its thread works through the steps."""

    def __init__(self, plan):
        self.plan = plan
        self.id = plan.machine.id
        self.name = plan.machine.name
        self.lock = None
        self.held = False               # the lock is on the share ("held"), not "local_only"
        self.local_only = False
        self.notes = []
        self.before = None              # the expected state before this Send
        self.before_read = False        # before came from the share, not this PC's memory
        self.expected_before = None     # {"send_id", "counter"} of before, for the log and record_offline
        self.share_failed = False       # the share failed during the handshake
        self.client = None
        self.built = None
        self.idle = None                # the idle-slot read-back
        self.pending = None             # the "switch pending" state this Send wrote
        self.clock = None               # the clock set's record
        self.differences = []
        self.thread = None
        self.result = MachineResult(machine_id=plan.machine.id, colour=RED, ok=False, step=STEP_LOCK,
                                    origin_line=plan.origin_line)

    def note(self, name):
        if name not in self.notes:
            self.notes.append(name)


class SendPipeline(_Worker):
    """The Send steps for every machine of a plan, the sender's answer, Leave it and the after-outage
    record. Runs on worker threads only; each machine of a Send has its own thread."""

    # ---- run

    def run(self, plan, on_event: Callable[[object], None], *, cancel: CancelToken | None = None) -> SendResult:
        """Steps STEP_CHECK_CHOICE .. STEP_CONFIRM_2 for every machine. Raises only PlanError and
        NotAllowed (nothing sent); a machine's failure is a red MachineResult."""
        send_id = plan.send_id
        started_wall = self.clock.time()

        # check_choice (§10.2)
        ok, why = self.data.can_send()
        if not ok:
            raise PlanError(why, problems=[why])
        current = self.data.current
        if current is None or not self.identity.is_sender(current.company):
            raise NotAllowed(Msg(_ONLY_SENDERS))
        _safe_post(on_event, SendEvent(send_id=send_id, step=STEP_CHECK_CHOICE, status="running",
                                       at=self.clock.time()))
        fresh = merge_plan(plan.request, current, self.identity)
        if not self._same_plan(plan, fresh):
            m = Msg(N_("The templates changed since you checked them. Look at the Send window again."))
            raise PlanError(m, problems=[m])
        company = current.company
        self.time.set_hosts([company.time_server, company.time_server_fallback])
        _safe_post(on_event, SendEvent(send_id=send_id, step=STEP_CHECK_CHOICE, status="ok", at=self.clock.time()))

        run = _Run(send_id, fresh, current, on_event)
        machines = [_Machine(mp) for mp in fresh.machines]
        # lock and the countdown (§10.3): the Send window counts down from this event's time
        self._event(run, "", STEP_LOCK, "running")
        start = self.clock.monotonic()
        try:
            self._lock_all(run, machines)
            going = [m for m in machines if m.lock is not None]
            for m in going:
                m.thread = threading.Thread(target=self._machine, args=(run, m), daemon=True,
                                            name=f"hx-send-{send_id}-{m.id}")
                m.thread.start()
            run.reading = self.time.now(fresh=True)
            cancelled = self._countdown(start, cancel)
        except BaseException:
            run.cancelled = True
            run.abort.cancel()
            run.go.set()
            for m in machines:
                if m.thread is not None:
                    m.thread.join()
                if m.lock is not None:
                    m.lock.release()
            raise
        if cancelled:
            return self._cancelled(run, machines, started_wall)

        # Go (§10.3 step 8): from here the cancel token is ignored
        if run.reading.source == "pc":
            self._event(run, "", STEP_SET_CLOCK, "warning", NOTE_TEXTS["pc_time"])
        for m in going:
            if run.reading.source == "pc":
                m.note("pc_time")
            if fresh.from_local_copy:
                m.note("local_copy")
            if m.plan.position.warning is not None:
                m.note("film_stop")
        self._event(run, "", STEP_LOCK, "ok")
        run.go.set()
        for m in going:
            m.thread.join()
        return self._finish(run, machines, started_wall)

    def _same_plan(self, old, fresh):
        """§10.2 step 5: the same machines in the same order, each with the same content code, name,
        settings and origin line as the sender saw."""
        if [mp.machine.id for mp in old.machines] != [mp.machine.id for mp in fresh.machines]:
            return False
        for a, b in zip(old.machines, fresh.machines):
            try:
                x, y = build_message(a, 9), build_message(b, 9)
            except ValueError:
                return False
            if (x.content_code, x.name, x.expected_settings, a.origin_line) != \
                    (y.content_code, y.name, y.expected_settings, b.origin_line):
                return False
        return True

    def _event(self, run, machine_id, step, status, msg=None, design=None):
        _safe_post(run.on_event, SendEvent(send_id=run.send_id, machine_id=machine_id, step=step, status=status,
                                           msg=msg, design=design, at=self.clock.time()))

    def _at(self, run):
        """Times written by the time server's clock when the reading is at hand."""
        offset = run.reading.offset_s if run.reading is not None else 0.0
        return self.clock.time() + offset

    def _lock_all(self, run, machines):
        """§10.3 steps 2 and 3: every printer's lock, line by line; then each machine's state before."""
        by_line = {}
        for m in machines:
            by_line.setdefault(m.plan.line.id, []).append(m)
        for line_id, members in by_line.items():
            taken = []
            failure = None
            for m in members:
                self._event(run, m.id, STEP_LOCK, "running")
                lock = self.store.lock(m.plan.printer.id, purpose="send", run_id=run.send_id, line_id=line_id,
                                       machine_id=m.id)
                try:
                    state = lock.acquire()
                except LockHeld as e:
                    failure = _holder_msg(e, line_name=m.plan.line.name, machine_name=m.name)
                    break
                except ShareError as e:
                    failure = e.msg
                    break
                taken.append(m)
                m.lock = lock
                if state == "held":
                    m.held = True
                    lock.start_keeping()
                    self._event(run, m.id, STEP_LOCK, "ok")
                else:
                    m.local_only = True
                    m.note("no_lock")
                    self._event(run, m.id, STEP_LOCK, "warning", NOTE_TEXTS["no_lock"])
            if failure is not None:
                # A lock held on any printer of a line stops the whole line
                for m in taken:
                    m.lock.release()
                    m.lock = None
                for m in members:
                    self._red(run, m, STEP_LOCK, failure)
        for m in machines:
            if m.lock is None:
                continue
            try:
                before = self.store.read_expected(m.id)
                m.before, m.before_read = before, True
                m.expected_before = {"send_id": before.send_id if before else "",
                                     "counter": before.counter if before else 0}
            except ShareUnavailable:
                m.note("no_lock")
                m.before = self.store.cached_expected(m.id)
                m.expected_before = ({"send_id": m.before.send_id, "counter": m.before.counter}
                                     if m.before is not None else None)
            except ShareError as e:
                # FormatTooNew ("Update Stripes") or a share that refuses: don't send to it
                m.lock.release()
                m.lock = None
                self._red(run, m, STEP_LOCK, e.msg)

    def _red(self, run, m, step, msg):
        m.result.ok = False
        m.result.colour = RED
        m.result.step = step
        m.result.msg = msg
        self._event(run, m.id, step, "failed", msg)

    def _countdown(self, start, cancel):
        """§10.3 step 6: True when the Send was cancelled before "go"."""
        while True:
            if cancel is not None and cancel.cancelled:
                return True
            left = self.timings.countdown_s - (self.clock.monotonic() - start)
            if left <= 0:
                break
            if not self.clock.sleep(min(0.25, left), cancel):
                return True
        return cancel is not None and cancel.cancelled

    def _cancelled(self, run, machines, started_wall):
        """§10.3 step 7: nothing was sent; nothing is written."""
        run.cancelled = True
        run.abort.cancel()
        run.go.set()
        for m in machines:
            if m.thread is not None:
                m.thread.join()
            if m.lock is not None:
                m.lock.release()
        for m in machines:
            r = m.result
            if r.ok or r.msg is None:
                r.ok, r.colour, r.step, r.msg = False, RED, STEP_LOCK, NOTE_TEXTS["cancelled"]
                self._event(run, m.id, STEP_LOCK, "skipped", NOTE_TEXTS["cancelled"])
            m.note("cancelled")
            r.notes = _ordered(m.notes)
        offset = run.reading.offset_s if run.reading is not None else 0.0
        result = SendResult(send_id=run.send_id, sender=self.identity.user, computer=self.identity.computer,
                            started_at=started_wall + offset, finished_at=self.clock.time() + offset,
                            machines=[m.result for m in machines],
                            time_source=run.reading.source if run.reading is not None else "pc",
                            from_local_copy=run.plan.from_local_copy, data_as_of=run.plan.data_as_of)
        _safe_post(run.on_event, SendFinished(result=result))
        return result

    # ---- one machine's thread

    def _machine(self, run, m):
        try:
            try:
                if self._wait_poller(run, m):
                    self._steps(run, m)
            except Exception as e:
                traceback.print_exc()
                step = m.result.step
                _log_internal(self.data, where="send", ref_key="send_id", ref=run.send_id, machine_id=m.id,
                              step=step, error=e, at=self._at(run))
                self._fail(run, m, step, _internal_msg(e))
        finally:
            if m.lock is not None:
                m.lock.release()

    def _wait_poller(self, run, m):
        """§10.4. True when the machine may make its first request (after "go")."""
        step = STEP_WAIT_POLLER
        m.result.step = step
        self._event(run, m.id, step, "running")
        deadline = self.clock.monotonic() + 2 * self.timings.stale_s
        answer = "ready"
        if m.local_only:
            self._event(run, m.id, step, "skipped")
        else:
            try:
                answer = self._handshake(m.lock, m.id, deadline, run.abort)
            except LockLost as e:
                return self._fail_before_go(run, m, step, e.msg, write=False)
            except ShareError as e:
                return self._fail_before_go(run, m, step, e.msg)
            if answer == "cancelled":
                return False
            if answer == "timeout":
                return self._fail_before_go(run, m, step, _waited_msg(m.name, self.timings))
            if answer == "share_down":
                m.note("no_lock")
                m.share_failed = True
            self._event(run, m.id, step, "ok")
        run.go.wait()
        if run.cancelled:
            return False
        if m.local_only or m.share_failed:
            return True
        # Right before the first request: a poller that has just started is noticed here
        try:
            while True:
                try:
                    if self.store.check_poller_paused(m.id, m.lock.token) != "polling":
                        return True
                except ShareUnavailable:
                    m.note("no_lock")
                    return True
                answer = self._handshake(m.lock, m.id, deadline)
                if answer == "share_down":
                    m.note("no_lock")
                    return True
                if answer == "timeout":
                    self._fail(run, m, step, _waited_msg(m.name, self.timings))
                    return False
        except LockLost as e:
            self._fail(run, m, step, e.msg, write=False)
            return False
        except ShareError as e:
            self._fail(run, m, step, e.msg)
            return False

    def _fail_before_go(self, run, m, step, msg, *, write=True):
        """A handshake failure during the countdown: written only once the Send goes on."""
        self._red(run, m, step, msg)
        self._previous_from_poll(m)
        run.go.wait()
        if write and not run.cancelled:
            self._write_failure(run, m, step, msg)
        return False

    def _steps(self, run, m):
        steps = ((STEP_CHECK_MACHINE, self._check_machine), (STEP_SET_CLOCK, self._set_clock),
                 (STEP_BUILD, self._build), (STEP_VERIFY_IDLE, self._verify_idle), (STEP_SWITCH, self._switch),
                 (STEP_CONFIRM_1, self._confirm), (STEP_CONFIRM_2, self._confirm))
        for step, work in steps:
            m.result.step = step
            if step == STEP_CONFIRM_2:
                self.clock.sleep(self.timings.confirm_gap_s)
            self._event(run, m.id, step, "running")
            try:
                work(run, m, step)
            except LockLost as e:
                # The lock is someone else's now: nothing more to this printer, nothing written
                self._fail(run, m, step, e.msg, write=False)
                return
            except ForbiddenCommand:
                raise
            except HxError as e:
                lost = self._lease_ran_out(m.lock, e)
                if lost is not None:
                    self._fail(run, m, step, lost.msg, write=False)
                    return
                if isinstance(e, _Failed) and e.note:
                    m.note(e.note)
                self._fail(run, m, step, e.msg, differences=getattr(e, "differences", None))
                return
        self._live(run, m)

    def _fail(self, run, m, step, msg, *, write=True, differences=None):
        if differences:
            m.differences = list(differences)
        self._red(run, m, step, msg)
        if step in (STEP_WAIT_POLLER, STEP_CHECK_MACHINE):
            self._previous_from_poll(m)
        if write:
            self._write_failure(run, m, step, msg)

    def _previous_from_poll(self, m):
        """§10.5: before the slot 0 read, what the machine was last seen printing (best effort)."""
        if m.result.previous is not None:
            return
        try:
            poll = self.store.read_poll(m.id)
            if poll is not None and poll.running is not None:
                m.result.previous = copy.deepcopy(poll.running)
                m.result.previous_preview = self.store.read_preview(m.id) or b""
        except (ShareError, ValueError):
            return
        m.result.previous_confirmed = _was_confirmed(m.before, m.result.previous)

    def _guard_for(self, m):
        return self._guard(m.lock)

    # ---- the steps (§10.5 to §10.10)

    def _check_machine(self, run, m, step):
        plan = m.plan
        m.client = self.clients.client(plan.printer)

        def on_ink(msg, mismatch):
            raise _Failed(msg, note="ink_mismatch" if mismatch else "")
        printing, current = _check_printer(m.client, plan, run.company, self._guard_for(m), on_ink)
        r = m.result
        r.slot = idle_slot(printing, current.slot)
        origin = origin_from_design(current)
        if not origin and m.before is not None and m.before.fingerprint and \
                m.before.fingerprint == fingerprint(current):
            origin = m.before.origin_line
        r.previous = _running_message(current, origin)
        r.previous_preview = current.preview_bmp or b""
        r.previous_confirmed = _was_confirmed(m.before, r.previous)
        counter = run.company.bag_counter
        if counter != "none":
            deadline = self._guard_for(m)("read")
            try:
                r.counts_before = m.client.read(counter, deadline=deadline)
            except PrinterError:
                r.counts_before = None      # only for the log
        self._event(run, m.id, step, "ok", design=current)

    def _set_clock(self, run, m, step):
        m.clock = {"before": None}
        _clock_set(m.client, self._guard_for(m), self.time, run.reading, m.name, m.clock, before=False)
        if run.reading.source == "pc":
            self._event(run, m.id, step, "warning", NOTE_TEXTS["pc_time"])
        else:
            self._event(run, m.id, step, "ok")

    def _build(self, run, m, step):
        built = build_message(m.plan, m.result.slot)
        m.built = built
        m.result.message_name, m.result.content_code = built.name, built.content_code
        guard = self._guard_for(m)
        for cmd in built.commands:
            m.client.command(cmd, deadline=guard("command"))
        self._event(run, m.id, step, "ok")

    def _read_local(self, run, design):
        return to_local(design.read_at + run.reading.offset_s, offset_fn=self.time.offset_fn)

    def _verify_idle(self, run, m, step):
        d = m.client.load_slot(m.result.slot, deadline=self._guard_for(m)("load_slot"))
        m.result.readbacks.append(d)
        problem = _readback_problem(m.built, d, self._read_local(run, d), m.name)
        if problem is not None:
            raise problem
        m.idle = d
        self._event(run, m.id, step, "ok", design=d)

    def _switch(self, run, m, step):
        r, plan = m.result, m.plan
        if m.held:
            previous = r.previous
            base = copy.deepcopy(m.before) if m.before is not None else ExpectedState(machine_id=m.id)
            state = dataclasses.replace(
                base, mode="switch_pending", printing_expected=True, send_id=run.send_id,
                sender=self.identity.user, message_name=m.built.name, content_code=m.built.content_code,
                fingerprint=fingerprint(m.idle), slot=r.slot, origin_line=plan.origin_line,
                previous_message_name=previous.name if previous else "",
                previous_fingerprint=previous.fingerprint if previous else "", result="", failure=None,
                notes=_amber(m.notes), confirmed=False, confirmed_by="", confirmed_at=None, rejected=False,
                resume_blocked=False, left_by="", left_at=None, test=None, stop_reason="", stopped_by="",
                stopped_at=None)
            m.lock.check()
            try:
                self.store.write_expected(state)
            except ShareError:
                raise _Failed(Msg(N_("Stripes couldn't record the switch on the server, so it didn't switch.")))
            m.pending = state
        previous = r.previous
        self.data.append_send_log({
            "type": "send_switch", "send_id": run.send_id, "machine_id": m.id, "line_id": plan.line.id,
            "printer_id": plan.printer.id, "slot": r.slot, "message_name": m.built.name,
            "content_code": m.built.content_code, "origin_line": plan.origin_line,
            "previous_slot": previous.slot if previous else None,
            "previous_message_name": previous.name if previous else "",
            "previous_fingerprint": previous.fingerprint if previous else "", "at": self._at(run)})
        guard = self._guard_for(m)
        deadline = guard("command")
        r.after_switch = True       # from here the machine may be printing the new message
        m.client.command(Command.set("PRINTMSG", r.slot), deadline=deadline)
        m.client.command(Command.set("PRINT", 1), deadline=guard("command"))
        self._event(run, m.id, step, "ok")

    def _confirm(self, run, m, step):
        r = m.result
        guard = self._guard_for(m)
        d = m.client.load_slot(0, deadline=guard("load_slot"))
        r.readbacks.append(d)
        if d.slot != r.slot:
            raise _Failed(Msg(N_("{machine} is printing slot {got}, not slot {slot}."),
                              {"machine": m.name, "got": d.slot, "slot": r.slot}),
                          differences=[Difference("slot", "MSNO", str(r.slot), str(d.slot))])
        problem = _readback_problem(m.built, d, self._read_local(run, d), m.name)
        if problem is not None:
            raise problem
        if m.client.read("PRIN", deadline=guard("read")) != 1:
            raise _Failed(Msg(N_("{machine} didn't turn printing on."), {"machine": m.name}))
        self._event(run, m.id, step, "ok", design=d)

    def _live(self, run, m):
        """§10.10 after confirm_2: live and checked three times, waiting for the sender."""
        r = m.result
        last = r.readbacks[-1]
        r.fingerprint = fingerprint(last)
        counter = run.company.bag_counter
        if counter != "none":
            try:
                r.counts_after = m.client.read(counter, deadline=self._guard_for(m)("read"))
            except (PrinterError, LockLost):
                r.counts_after = None       # only for the log
        r.ok, r.colour, r.step, r.msg = True, AMBER, STEP_CONFIRM_2, None
        if m.held:
            state = dataclasses.replace(m.pending, mode="running", fingerprint=r.fingerprint, result=AMBER,
                                        failure=None, previous_message_name="", previous_fingerprint="")
            try:
                m.lock.check()
                self.store.write_expected(state)
            except ShareError as e:
                # E still says "switch pending": record_offline writes it once the share is back
                print(f"Could not record {m.id} as live: {e}", file=sys.stderr)
                m.note("no_lock")
        m.lock.release()
        self._event(run, m.id, STEP_OPERATOR, "running")

    def _write_failure(self, run, m, step, msg):
        """The rows "Failed before the switch" and "Failed after the switch" (§14), only with a held lock."""
        if not m.held:
            return
        try:
            m.lock.check()
            failure = FailureInfo(step=step, msg=msg, after_switch=m.result.after_switch, at=self._at(run))
            if m.result.after_switch:
                if m.pending is None:
                    return
                state = dataclasses.replace(m.pending, result=RED, failure=failure)
            else:
                # The old expectation stays (message, fingerprint, confirmation): what is still printing
                base = m.before if m.before_read else self.store.read_expected(m.id)
                base = copy.deepcopy(base) if base is not None else ExpectedState(machine_id=m.id)
                state = dataclasses.replace(base, send_id=run.send_id, sender=self.identity.user, result=RED,
                                            failure=failure, notes=_amber(m.notes), rejected=False)
            self.store.write_expected(state)
        except ShareError as e:
            print(f"Could not record the failure of {m.id}: {e}", file=sys.stderr)

    # ---- finishing (§10.11)

    def _finish(self, run, machines, started_wall):
        plan, offset = run.plan, run.reading.offset_s
        for m in machines:
            if not m.result.ok and m.result.previous is None:
                self._previous_from_poll(m)
            m.result.notes = _ordered(m.notes)
            m.result.colour = AMBER if m.result.ok else RED
        result = SendResult(send_id=run.send_id, sender=self.identity.user, computer=self.identity.computer,
                            started_at=started_wall + offset, finished_at=self.clock.time() + offset,
                            machines=[m.result for m in machines], time_source=run.reading.source,
                            from_local_copy=plan.from_local_copy, data_as_of=plan.data_as_of)
        by_line = {}
        for m in machines:
            by_line.setdefault(m.plan.line.id, []).append(m)
        for line_id, members in by_line.items():
            if all(not m.result.ok and m.result.step == STEP_LOCK for m in members):
                continue        # nothing happened at its printers
            mp = members[0].plan
            status = LineStatus(line_id=line_id, send_id=run.send_id, sent_at=result.started_at,
                                sender=self.identity.user, product_id=mp.product.id if mp.product else "",
                                film_id=mp.film.id, film_revision=mp.film.revision,
                                print_text_id=mp.print_text.id if mp.print_text else "",
                                print_text_revision=mp.print_text.revision if mp.print_text else 0,
                                lot=mp.lot, countries=list(mp.countries), origin_line=mp.origin_line,
                                machines={m.id: m.result.colour for m in members}, confirmed=False,
                                confirmed_by="", stopped_needs_send=False)
            try:
                self.store.write_line_status(status)
            except (ShareError, ValueError) as e:
                print(f"Could not write the line status of {line_id}: {e}", file=sys.stderr)
        try:
            result.log_name, result.log_queued = self.data.append_send_log(self._send_entry(run, machines, result))
        except (ShareError, ValueError) as e:
            traceback.print_exc()
            print(f"Could not save the send log of {run.send_id}: {e}", file=sys.stderr)
        _safe_post(run.on_event, SendFinished(result=result))
        return result

    def _unticked(self, run, line_id, sent_ids):
        out = []
        for machine in run.current.line_machines(line_id):
            if machine.id in sent_ids:
                continue
            origin, printing = "", None
            try:
                poll = self.store.read_poll(machine.id)
            except (ShareError, ValueError):
                poll = None
            if poll is not None:
                printing = poll.printing
                unchecked = poll.running_unchecked or (poll.state is not None and poll.state.code == "message_unchecked")
                if poll.running is not None and not unchecked:
                    origin = poll.running.origin_line
            out.append({"machine_id": machine.id, "origin_line": origin, "printing": printing})
        return out

    def _send_entry(self, run, machines, result):
        """The send log's main entry (§17.2): everything needed to rebuild the result window."""
        plan, reading = run.plan, run.reading
        sent_ids = {m.id for m in machines}
        lines = []
        for choice in plan.request.lines:
            mp = next((x for x in plan.machines if x.line.id == choice.line_id), None)
            if mp is None:
                continue
            lines.append({
                "line_id": mp.line.id, "line_name": mp.line.name, "line_code": mp.line.code,
                "product": ({"id": mp.product.id, "name": mp.product.name, "customer": mp.product.customer,
                             "revision": mp.product.revision} if mp.product else None),
                "film": {"id": mp.film.id, "code": mp.film.code, "artwork_revision": mp.film.artwork_revision,
                         "revision": mp.film.revision},
                "print_text": ({"id": mp.print_text.id, "code": mp.print_text.code, "name": mp.print_text.name,
                                "revision": mp.print_text.revision} if mp.print_text else None),
                "lot": mp.lot, "countries": list(mp.countries), "origin_line": mp.origin_line,
                "unticked": self._unticked(run, mp.line.id, sent_ids)})
        rows = []
        for m in machines:
            mp, built = m.plan, m.built
            rows.append({
                "machine_id": m.id, "machine_name": mp.machine.name, "machine_code": mp.machine.code,
                "line_id": mp.line.id,
                "printer": {"id": mp.printer.id, "name": mp.printer.name, "host": mp.printer.host,
                            "uid": mp.printer.uid},
                "placement": mp.machine.placement, "bag_text": mp.bag_text, "position": to_json(mp.position),
                "settings": to_json(mp.settings), "slot": m.result.slot, "message_name": m.result.message_name,
                "content_code": m.result.content_code,
                "commands": [c.xml for c in built.commands] if built else [],
                "expected_items": ([[it.tag, [[k, v] for k, v in it.pairs]] for it in built.expected_items]
                                   if built else []),
                "expected_settings": built.expected_settings if built else expected_settings(mp.settings),
                "clock": m.clock, "warnings": [w.to_json() for w in mp.warnings],
                "differences": [[d.where, d.field, d.sent, d.read] for d in m.differences],
                "expected_before": m.expected_before, "result": _result_to_log(m.result)})
        return {"type": "send", "send_id": run.send_id, "sender": result.sender, "computer": result.computer,
                "started_at": result.started_at, "finished_at": result.finished_at,
                "time_source": reading.source, "time_server": reading.server,
                "time_offset_s": round(reading.offset_s, 3), "from_local_copy": plan.from_local_copy,
                "data_as_of": plan.data_as_of, "request": to_json(plan.request), "lines": lines, "machines": rows}

    # ---- the sender's answer (§11)

    @staticmethod
    def _is_pending(state, send_id):
        return (state is not None and state.send_id == send_id and state.mode == "running" and not state.confirmed
                and not state.rejected and state.failure is None)

    def _pending_states(self, send_id, entry):
        """machine id -> its expected state, for the machines still waiting for this Send's answer."""
        out = {}
        if entry is not None:
            for row in entry.get("machines", []):
                if (row.get("result") or {}).get("ok"):
                    state = self.store.read_expected(row["machine_id"])     # share errors are raised
                    if self._is_pending(state, send_id):
                        out[row["machine_id"]] = state
            return out
        candidates = []
        try:
            names = self.store.list_logs("hx-sends", since=self.clock.time() - LOG_SEARCH_S,
                                         entry_type="send_switch", ref=send_id)
            for name in names:
                switch = self.store.read_log("hx-sends", name)
                if switch is not None and switch.get("send_id") == send_id:
                    candidates.append(switch.get("machine_id"))
        except ShareUnavailable:
            raise
        except (ShareError, ValueError):
            candidates = []
        if not candidates:
            current = self.data.current
            if current is not None:
                active = sorted((m for m in current.machines.values() if m.active), key=lambda m: (m.name, m.id))
                candidates = [m.id for m in active]
        for machine_id in dict.fromkeys(candidates):
            if not isinstance(machine_id, str):
                continue
            try:
                state = self.store.read_expected(machine_id)
            except ShareUnavailable:
                raise
            except (ShareError, ValueError):
                continue        # e.g. a file a newer Stripes wrote: not pending for this Send
            if self._is_pending(state, send_id):
                out[machine_id] = state
        return out

    def pending_confirmation(self, send_id: str) -> list[str]:
        """Blocking: the machines whose expected state still waits for this Send's answer (§11.3)."""
        return list(self._pending_states(send_id, self.data.find_send(send_id)))

    def _machine_name(self, machine_id, entry_rows=None):
        """The machine's name from the Send's log entry or the current records; "" when neither knows it."""
        for row in entry_rows or []:
            if row.get("machine_id") == machine_id and row.get("machine_name"):
                return row["machine_name"]
        current = self.data.current
        machine = current.machines.get(machine_id) if current is not None else None
        return machine.name if machine is not None else ""

    def _not_updated(self, machine_ids, entry_rows=None):
        """The ShareError for machines whose expected state couldn't be written: by name, and without
        names when one of them isn't known any more (texts never show an id)."""
        names = [self._machine_name(mid, entry_rows) for mid in machine_ids]
        if all(names):
            return ShareError(Msg(_NOT_UPDATED, {"machines": ", ".join(names)}))
        return ShareError(Msg(_NOT_UPDATED_UNNAMED))

    def _under_lock(self, send_id, machine_id, line_id, printer_id, change):
        """§11.2 step 5: take the printer's lock (no printer request), read E, and when change(E) gives
        a new state write it. (outcome, state read) with outcome "written", "left" or "error"."""
        try:
            lock = self.store.lock(printer_id, purpose="send", run_id=send_id, line_id=line_id, machine_id=machine_id)
            state = lock.acquire()
        except (ShareError, ValueError):
            return "error", None
        try:
            if state != "held":
                return "error", None
            lock.start_keeping()
            current = self.store.read_expected(machine_id)
            new = change(current)
            if new is None:
                return "left", current
            lock.check()
            self.store.write_expected(new)
            return "written", new
        except (ShareError, ValueError):
            return "error", None
        finally:
            lock.release()

    def confirm(self, send_id: str, machine_ids: list[str], everything_right: bool) -> None:
        """The sender's answer: logged first, then each machine's expected state (only where it still
        belongs to this Send) and the line status. Raises NotAllowed for anyone but the sender."""
        entry = self.data.find_send(send_id)
        current = self.data.current
        live = {}           # machine id -> (line id, printer id, message name, fingerprint)
        rows = []
        if entry is not None:
            sender = entry.get("sender", "")
            rows = entry.get("machines", [])
            for row in rows:
                r = row.get("result") or {}
                if r.get("ok"):
                    live[row["machine_id"]] = (row.get("line_id", ""), (row.get("printer") or {}).get("id", ""),
                                               r.get("message_name", ""), r.get("fingerprint", ""))
            line_ids = [line.get("line_id") for line in entry.get("lines", [])]
        else:
            pending = self._pending_states(send_id, None)
            if not pending:
                raise ShareError(Msg(N_("Stripes can't find this Send yet. Try again in a minute.")))
            sender = next(iter(pending.values())).sender
            for machine_id, state in pending.items():
                machine = current.machines.get(machine_id) if current is not None else None
                if machine is None or not same_user(state.sender, sender):
                    continue
                live[machine_id] = (machine.line_id, machine.printer_id, state.message_name, state.fingerprint)
            line_ids = list(dict.fromkeys(v[0] for v in live.values()))
        if not same_user(self.identity.user, sender):
            raise NotAllowed(Msg(N_("Only {sender} can confirm this Send."), {"sender": sender}))
        targets = [mid for mid in dict.fromkeys(machine_ids) if mid in live]
        at = self._now()
        self.data.append_send_log({
            "type": "send_confirm", "send_id": send_id, "sender": sender, "everything_right": bool(everything_right),
            "machine_ids": targets,
            "machines": [{"machine_id": mid, "message_name": live[mid][2], "fingerprint": live[mid][3]}
                         for mid in targets], "at": at})

        def change(state):
            if not self._is_pending(state, send_id):
                return None     # a later Send, Stop or test has replaced it
            if everything_right:
                return dataclasses.replace(state, confirmed=True, confirmed_by=self.identity.user, confirmed_at=at,
                                           result=result_colour(True, state.notes))
            return dataclasses.replace(state, rejected=True, resume_blocked=True, result=RED)

        colours, confirmed, failed = {}, set(), []

        def is_confirmed(mid):
            if mid in confirmed:
                return True
            try:
                state = self.store.read_expected(mid)
            except (ShareError, ValueError):
                return False
            return state is not None and state.send_id == send_id and state.confirmed and not state.rejected

        for mid in targets:
            line_id, printer_id = live[mid][0], live[mid][1]
            outcome, state = self._under_lock(send_id, mid, line_id, printer_id, change)
            if outcome == "error":
                failed.append(mid)
            elif outcome == "written":
                colours[mid] = state.result
                if state.confirmed:
                    confirmed.add(mid)
        for line_id in dict.fromkeys(line_ids):
            if not line_id:
                continue
            try:
                status = self.store.read_line_status(line_id)
            except (ShareError, ValueError) as e:
                print(f"Could not read the line status of {line_id}: {e}", file=sys.stderr)
                continue
            if status is None or status.send_id != send_id:
                continue
            if entry is not None:
                went_live = [row["machine_id"] for row in rows
                             if row.get("line_id") == line_id and (row.get("result") or {}).get("ok")]
            else:
                went_live = [mid for mid, colour in status.machines.items() if colour != RED]
            machines = dict(status.machines)
            for mid, colour in colours.items():
                if mid in machines or live.get(mid, ("",))[0] == line_id:
                    machines[mid] = colour
            fields = {"machines": machines}
            if everything_right and went_live and all(is_confirmed(mid) for mid in went_live):
                fields.update(confirmed=True, confirmed_by=self.identity.user)
            try:
                self.store.write_line_status(dataclasses.replace(status, **fields))
            except (ShareError, ValueError) as e:
                print(f"Could not write the line status of {line_id}: {e}", file=sys.stderr)
        if failed:
            raise self._not_updated(failed, rows)

    def leave_it(self, send_id: str, machine_ids: list[str]) -> None:
        """Leave it: the machines stay red with their failure; logged, and a new reread_token asks the
        poller to load what is really printing."""
        current = self._current()
        if not self.identity.is_sender(current.company):
            raise NotAllowed(Msg(_ONLY_SENDERS))
        token = secrets.token_hex(8)
        at = self._now()
        ids = list(dict.fromkeys(machine_ids))
        self.data.append_send_log({"type": "send_leave", "send_id": send_id, "machine_ids": ids,
                                   "reread_token": token, "at": at})

        def change(state):
            if state is None or state.send_id != send_id:
                return None
            return dataclasses.replace(state, left_by=self.identity.user, left_at=at, reread_token=token)

        failed = []
        for mid in ids:
            machine = current.machines.get(mid)
            if machine is None:
                failed.append(mid)
                continue
            outcome, _ = self._under_lock(send_id, mid, machine.line_id, machine.printer_id, change)
            if outcome == "error":
                failed.append(mid)
        if failed:
            raise self._not_updated(failed)

    # ---- after an outage (§14.1)

    def record_offline(self, entry: dict) -> list[str]:
        """Writes the expected state a Send made without the share would have written, where nobody
        changed it since the Send began. Returns the machine ids written; share errors are raised after
        the machines already done. A machine skipped for now (LockHeld, or a local_only lock) raises
        too, after the others: the caller keeps the Send and tries again next time (ui.md §15.3)."""
        send_id = entry["send_id"]
        written, errors = [], []
        for row in entry.get("machines", []):
            r = row.get("result") or {}
            if not r.get("ok") or "no_lock" not in (r.get("notes") or []):
                continue
            machine_id = row["machine_id"]
            before = row.get("expected_before")

            def change(state, row=row, r=r, before=before):
                own = (state is not None and state.send_id == send_id and state.mode == "switch_pending"
                       and state.failure is None)
                unchanged = before is not None and (
                    (state is None and before.get("counter") == 0)
                    or (state is not None and state.counter == before.get("counter")
                        and state.send_id == before.get("send_id")))
                if not (own or unchanged):
                    return None     # a later Send, Stop or test replaced it
                base = copy.deepcopy(state) if state is not None else ExpectedState(machine_id=machine_id)
                return dataclasses.replace(
                    base, mode="running", printing_expected=True, send_id=send_id, sender=entry.get("sender", ""),
                    message_name=r.get("message_name", ""), content_code=r.get("content_code", ""),
                    fingerprint=r.get("fingerprint", ""), slot=r.get("slot"), origin_line=r.get("origin_line", ""),
                    previous_message_name="", previous_fingerprint="", result=AMBER, failure=None,
                    notes=_amber(r.get("notes") or []), confirmed=False, confirmed_by="", confirmed_at=None,
                    rejected=False, resume_blocked=False, left_by="", left_at=None, test=None, stop_reason="",
                    stopped_by="", stopped_at=None)

            printer_id = (row.get("printer") or {}).get("id", "")
            try:
                lock = self.store.lock(printer_id, purpose="send", run_id=send_id, line_id=row.get("line_id", ""),
                                       machine_id=machine_id)
                state = lock.acquire()
            except LockHeld as e:
                errors.append(e)    # someone is working on that printer: tried again next time
                continue
            except (ShareError, ValueError) as e:
                errors.append(e)
                continue
            try:
                if state != "held":
                    errors.append(ShareUnavailable(Msg(_NO_SERVER)))     # tried again next time
                    continue
                current = self.store.read_expected(machine_id)
                new = change(current)
                if new is not None:
                    lock.check()
                    self.store.write_expected(new)
                    written.append(machine_id)
            except ShareError as e:
                errors.append(e)
            finally:
                lock.release()
        if errors:
            first = errors[0]
            raise first if isinstance(first, ShareError) else ShareError(Msg(str(first)))
        return written


# --------------------------------------------------------------------------
# Stop, Resume and Set clocks (§13)
# --------------------------------------------------------------------------

class _Action:
    """One machine of a Stop, Resume or Set clocks."""

    def __init__(self, machine, line, printer):
        self.machine = machine
        self.line = line
        self.printer = printer
        self.id = machine.id
        self.lock = None
        self.held = False
        self.notes = []
        self.result = MachineResult(machine_id=machine.id, colour=RED, ok=False, step=STEP_LOCK)
        self.extra = {}             # for the log: printing_after, clock, design

    def note(self, name):
        if name not in self.notes:
            self.notes.append(name)

    def done(self, step):
        r = self.result
        r.ok, r.step, r.msg = True, step, None
        r.notes = _ordered(self.notes)
        r.colour = AMBER if any(n in AMBER_NOTES for n in r.notes) else GREEN

    def fail(self, step, msg):
        r = self.result
        r.ok, r.colour, r.step, r.msg = False, RED, step, msg
        r.notes = _ordered(self.notes)


class PrinterActions(_Worker):
    """Stop printing, Resume printing and Set clocks, one thread per machine, each under its printer's
    lock. Senders only."""

    def _company(self):
        current = self._current()
        if not self.identity.is_sender(current.company):
            raise NotAllowed(Msg(_ONLY_ACTIONS))
        return current

    def _each(self, kind, current, machine_ids, on_event, action_id, work):
        """Runs work(action, post) for each machine on its own thread; the ActionResult in order."""
        actions = []
        for machine_id in dict.fromkeys(machine_ids):
            machine = current.machines[machine_id]
            actions.append(_Action(machine, current.lines.get(machine.line_id), current.printers[machine.printer_id]))

        def post(a, status, msg=None, design=None):
            _safe_post(on_event, ActionEvent(action_id=action_id, kind=kind, machine_id=a.id, status=status, msg=msg,
                                             design=design, at=self.clock.time()))

        def body(a):
            post(a, "running")
            try:
                work(a, post)
            except Exception as e:
                traceback.print_exc()
                _log_internal(self.data, where="action", ref_key="action_id", ref=action_id, machine_id=a.id,
                              step=a.result.step, error=e, at=self._now())
                a.fail(a.result.step, _internal_msg(e))
            finally:
                if a.lock is not None:
                    a.lock.release()
            r = a.result
            post(a, "failed" if not r.ok else ("warning" if r.colour == AMBER else "ok"), r.msg,
                 a.extra.get("design"))

        threads = [threading.Thread(target=body, args=(a,), daemon=True, name=f"hx-action-{action_id}-{a.id}")
                   for a in actions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return ActionResult(action_id=action_id, kind=kind, machines=[a.result for a in actions]), actions

    def _take(self, a, purpose, action_id):
        """The printer's lock and the poller handshake. True when the machine may make requests;
        otherwise a has failed. A local_only lock is a.held False."""
        line_name = a.line.name if a.line is not None else a.machine.name
        a.lock = self.store.lock(a.printer.id, purpose=purpose, run_id=action_id, line_id=a.machine.line_id,
                                 machine_id=a.id)
        try:
            state = a.lock.acquire()
        except LockHeld as e:
            a.fail(STEP_LOCK, _holder_msg(e, line_name=line_name, machine_name=a.machine.name))
            return False
        except ShareError as e:
            a.fail(STEP_LOCK, e.msg)
            return False
        if state != "held":
            a.note("no_lock")
            return True
        a.held = True
        a.lock.start_keeping()
        a.result.step = STEP_WAIT_POLLER
        try:
            answer = self._poller_steps_aside(a.lock, a.id)
        except ShareError as e:
            a.fail(STEP_WAIT_POLLER, e.msg)
            return False
        if answer == "timeout":
            a.fail(STEP_WAIT_POLLER, _waited_msg(a.machine.name, self.timings))
            return False
        if answer == "share_down":
            a.note("no_lock")
        return True

    def _write(self, a, change):
        """An expected-state row, only with a held lock; best effort."""
        if not a.held:
            return
        try:
            a.lock.check()
            state = change(self._read_expected(a.id))
            if state is not None:
                self.store.write_expected(state)
        except ShareError as e:
            print(f"Could not record {a.id}: {e}", file=sys.stderr)

    # ---- Stop (§13.2)

    def stop(self, machine_ids: list[str], on_event: Callable[[object], None], *, reason: str = "user",
             send_id: str = "", action_id: str = "") -> ActionResult:
        current = self._company()
        action_id = action_id or new_id("ac")
        user = self.identity.user

        def work(a, post):
            if not self._take(a, "stop", action_id):
                return
            guard = self._guard(a.lock)
            try:
                a.result.step = STEP_CHECK_MACHINE
                client = self.clients.client(a.printer)
                client.check_uid(deadline=guard("check_uid"))
                a.result.step = ACTION_STOP
                client.command(Command.set("PRINT", 0), deadline=guard("command"))
                after = client.read_twice("PRIN", deadline=guard("read_twice"))
                a.extra["printing_after"] = after
                if after != 0:
                    raise _Failed(_stop_failed_msg(a.machine.name))
            except LockLost:
                a.fail(a.result.step, _stop_failed_msg(a.machine.name))
                return
            except ForbiddenCommand:
                raise
            except HxError:
                a.fail(a.result.step, _stop_failed_msg(a.machine.name))
                mode = "stop_requested_at_printer"
            else:
                a.done(ACTION_STOP)
                mode = "stopped_by_user"
            at = self._now()

            def change(state):
                return dataclasses.replace(
                    state, mode=mode, printing_expected=False,
                    stop_reason="test" if state.stop_reason == "test" else reason, stopped_by=user, stopped_at=at,
                    resume_blocked=state.resume_blocked or reason == "rejected")
            self._write(a, change)

        result, actions = self._each("stop", current, machine_ids, on_event, action_id, work)
        if reason == "rejected" and send_id:
            for line_id in dict.fromkeys(a.machine.line_id for a in actions):
                _write_line_flag(self.store, line_id, send_id, stopped_needs_send=True)
        self._log(on_event, result, {
            "type": "stop", "action_id": action_id, "reason": reason, "send_id": send_id, "at": self._now(),
            "machines": [{"machine_id": a.id, "ok": a.result.ok, "colour": a.result.colour, "step": a.result.step,
                          "msg": _msg_json(a.result.msg), "printing_after": a.extra.get("printing_after"),
                          "notes": list(a.result.notes)} for a in actions]})
        return result

    def _log(self, on_event, result, entry):
        try:
            self.data.append_send_log(entry)
        except (ShareError, ValueError) as e:
            print(f"Could not log {entry['type']} {entry['action_id']}: {e}", file=sys.stderr)
        _safe_post(on_event, ActionFinished(result=result))

    # ---- Resume (§13.3 to §13.5)

    def check_resume(self, machine_id: str) -> ResumeCheck:
        """Blocking, for the Resume dialog: the message on the printer, and whether it may print again."""
        current = self._company()
        machine = current.machines[machine_id]
        try:
            expected = self.store.read_expected(machine_id)
        except ShareUnavailable:
            return ResumeCheck(machine_id=machine_id, design=None, origin_line="", allowed=False, msg=Msg(_CANT_CHECK))
        except ShareError as e:
            return ResumeCheck(machine_id=machine_id, design=None, origin_line="", allowed=False, msg=e.msg)
        refusal = resume_refusal(expected, machine)
        if refusal is not None:
            return ResumeCheck(machine_id=machine_id, design=None,
                               origin_line=expected.origin_line if expected else "", allowed=False, msg=refusal)
        a = _Action(machine, current.lines.get(machine.line_id), current.printers[machine.printer_id])
        try:
            if not self._take(a, "resume", new_id("ac")):
                return ResumeCheck(machine_id=machine_id, design=None, origin_line="", allowed=False, msg=a.result.msg)
            if not a.held:
                return ResumeCheck(machine_id=machine_id, design=None, origin_line="", allowed=False,
                                   msg=Msg(_CANT_CHECK))
            guard = self._guard(a.lock)
            try:
                client = self.clients.client(a.printer)
                client.check_uid(deadline=guard("check_uid"))
                design = client.load_slot(0, deadline=guard("load_slot"))
            except ForbiddenCommand:
                raise
            except HxError as e:
                e = self._lease_ran_out(a.lock, e) or e
                return ResumeCheck(machine_id=machine_id, design=None, origin_line="", allowed=False, msg=e.msg)
        finally:
            if a.lock is not None:
                a.lock.release()
        mismatch = resume_mismatch(expected, design, machine)
        # The expected origin is shown only for the message it belongs to
        origin = origin_from_design(design) or (expected.origin_line if mismatch is None else "")
        return ResumeCheck(machine_id=machine_id, design=design, origin_line=origin, allowed=mismatch is None,
                           msg=mismatch)

    def resume(self, machine_ids: list[str], on_event: Callable[[object], None], *,
               action_id: str = "") -> ActionResult:
        current = self._company()
        action_id = action_id or new_id("ac")

        def work(a, post):
            if not self._take(a, "resume", action_id):
                return
            if not a.held:
                a.fail(STEP_LOCK, Msg(_CANT_CHECK))
                return
            a.result.step = STEP_CHECK_MACHINE
            try:
                expected = self.store.read_expected(a.id)
            except ShareError as e:
                a.fail(STEP_CHECK_MACHINE, e.msg)
                return
            refusal = resume_refusal(expected, a.machine)
            if refusal is not None:
                a.fail(STEP_CHECK_MACHINE, refusal)
                return
            guard = self._guard(a.lock)
            try:
                client = self.clients.client(a.printer)
                client.check_uid(deadline=guard("check_uid"))
                design = client.load_slot(0, deadline=guard("load_slot"))
                a.extra["design"] = design
                r = a.result
                r.readbacks, r.slot, r.message_name, r.fingerprint = [design], design.slot, design.name, \
                    fingerprint(design)
                mismatch = resume_mismatch(expected, design, a.machine)
                r.origin_line = origin_from_design(design) or (expected.origin_line if mismatch is None else "")
                if mismatch is not None:
                    a.fail(STEP_CHECK_MACHINE, mismatch)
                    return
                a.result.step = ACTION_RESUME
                client.command(Command.set("PRINT", 1), deadline=guard("command"))
                if client.read_twice("PRIN", deadline=guard("read_twice")) != 1:
                    raise _Failed(Msg(N_("{machine} didn't turn printing on."), {"machine": a.machine.name}))
            except ForbiddenCommand:
                raise
            except HxError as e:
                a.fail(a.result.step, (self._lease_ran_out(a.lock, e) or e).msg)
                return
            notes = list(expected.notes)
            a.done(ACTION_RESUME)
            a.result.colour = result_colour(expected.confirmed, notes)

            def change(state):
                return dataclasses.replace(state, mode="running", printing_expected=True, stop_reason="",
                                           stopped_by="", stopped_at=None)
            self._write(a, change)

        result, actions = self._each("resume", current, machine_ids, on_event, action_id, work)
        self._log(on_event, result, {
            "type": "resume", "action_id": action_id, "at": self._now(),
            "machines": [{"machine_id": a.id, "ok": a.result.ok, "colour": a.result.colour, "step": a.result.step,
                          "msg": _msg_json(a.result.msg), "message_name": a.result.message_name,
                          "fingerprint": a.result.fingerprint, "origin_line": a.result.origin_line,
                          "design": design_to_log(a.extra["design"]) if a.extra.get("design") else None}
                         for a in actions]})
        return result

    # ---- Set clocks (§13.6)

    def set_clocks(self, machine_ids: list[str], on_event: Callable[[object], None], *,
                   action_id: str = "") -> ActionResult:
        current = self._company()
        action_id = action_id or new_id("ac")
        company = current.company
        self.time.set_hosts([company.time_server, company.time_server_fallback])
        reading = self.time.now(fresh=True)

        def work(a, post):
            if reading.source == "pc":
                a.note("pc_time")
            if not self._take(a, "clock", action_id):
                return
            guard = self._guard(a.lock)
            record = {"before": None}
            a.extra["clock"] = record
            try:
                a.result.step = STEP_CHECK_MACHINE
                client = self.clients.client(a.printer)
                client.check_uid(deadline=guard("check_uid"))
                a.result.step = STEP_SET_CLOCK
                _clock_set(client, guard, self.time, reading, a.machine.name, record, before=True)
            except ForbiddenCommand:
                raise
            except HxError as e:
                a.fail(a.result.step, (self._lease_ran_out(a.lock, e) or e).msg)
                return
            a.done(STEP_SET_CLOCK)
            if reading.source != "server":
                return

            def change(state):
                if state.counter == 0 and state.mode == "none" and not state.send_id:
                    return None         # nothing recorded for this machine yet
                notes = [n for n in state.notes if n != "pc_time"]
                result = state.result
                if state.mode == "running" and result in (GREEN, AMBER):
                    result = result_colour(state.confirmed, notes)
                return dataclasses.replace(state, notes=notes, result=result)
            self._write(a, change)

        result, actions = self._each("clock", current, machine_ids, on_event, action_id, work)
        self._log(on_event, result, {
            "type": "clock_set", "action_id": action_id, "at": self._now(), "time_source": reading.source,
            "time_server": reading.server, "time_offset_s": round(reading.offset_s, 3),
            "machines": [{"machine_id": a.id, "ok": a.result.ok, "colour": a.result.colour, "step": a.result.step,
                          "msg": _msg_json(a.result.msg), "clock": a.extra.get("clock")} for a in actions]})
        return result


# --------------------------------------------------------------------------
# Test print and calibration (§15, §16)
# --------------------------------------------------------------------------

class TestPrintRun(_Worker):
    """One test print or calibration run on one machine, under its printer's lock for the whole run.
    Every step is built, checked in the idle slot, switched and read back twice, like a Send. Once it
    has the printer it always ends with printing off and slots 9 and 10 overwritten with empty
    messages.

    TestResult.printing_off (a bool, architecture §5.9): True when the end procedure read printing off
    twice, and also when nothing of this test can be printing: the run sent nothing to the printer
    (it couldn't take the lock, the poller didn't step aside, the UID or the share failed first, the
    plan no longer matched the company's bag counter), or a Try again found a later Send or test on
    the machine. False when printing couldn't be confirmed off, or the run lost the printer mid-test:
    only then does the window show its "Stop it at the printer now" page (ui.md §12.5 item 5)."""

    def __init__(self, *, store, data, clients, time, identity, plan, clock: Clock = SYSTEM_CLOCK,
                 timings: Timings = TIMINGS, ranges=None):
        super().__init__(store=store, data=data, clients=clients, time=time, identity=identity, clock=clock,
                         timings=timings)
        self.plan = plan
        # What the administrator asked for, for the test_start entry: given here, or the plan's own
        # once TestPlan carries it
        self.ranges = ranges if ranges is not None else getattr(plan, "ranges", None)
        self.steps_done = 0
        self._expected = None           # the "Test running" state written
        self._uid_ok = False            # check_uid passed in this run; nothing is written before that
        self._on_event = None
        self._index = 0                 # the step being worked on, for the events
        self._step_plans = None

    # ---- events and small helpers

    def _post(self, status, index=0, msg=None, design=None, bags=None):
        _safe_post(self._on_event, TestEvent(run_id=self.plan.run_id, step_index=index, of=len(self.plan.steps),
                                             status=status, msg=msg, design=design, bags=bags, at=self.clock.time()))

    def _result(self, ok, msg, *, cleared_=False, printing_off=False):
        return TestResult(run_id=self.plan.run_id, kind=self.plan.kind, steps_done=self.steps_done,
                          of=len(self.plan.steps), ok=ok, msg=msg, cleared=cleared_, printing_off=printing_off)

    def _finished(self, result):
        _safe_post(self._on_event, TestFinished(result=result))
        return result

    def _untouched(self, reason, msg, *, printing_off=True):
        """The run, or a Try again, ends without sending anything to the printer: no rows are written
        and the test_end entry is logged. printing_off is True: this run changed nothing there, so
        nothing of it is printing and the window mustn't send anyone to the printer. A Try again that
        can't run passes False, because the test before it may still be printing."""
        self._post("failed", self._index, msg)
        self._log_end(reason, False, msg, printing_off, {"9": False, "10": False})
        return self._finished(self._result(False, msg, printing_off=printing_off))

    def _uid_checked(self):
        self._uid_ok = True

    def _test_state(self, before, previous):
        """The machine's expected state under this test at step 0 (the row "Test running")."""
        user = self.identity.user
        test = {"run_id": self.plan.run_id, "kind": self.plan.kind, "by": user, "step": 0, "of": len(self.plan.steps)}
        return dataclasses.replace(
            before, mode="test_running", printing_expected=True, send_id="", sender=user, message_name="",
            content_code="", slot=None, fingerprint="", origin_line="",
            previous_message_name=previous.name if previous is not None else before.message_name,
            previous_fingerprint=previous.fingerprint if previous is not None else before.fingerprint,
            result="", failure=None, notes=[], confirmed=False, confirmed_by="", confirmed_at=None, rejected=False,
            resume_blocked=True, left_by="", left_at=None, test=test, stop_reason="")

    def _admin(self):
        current = self._current()
        if not self.identity.is_admin(current.company):
            raise NotAllowed(Msg(_ONLY_ADMINS))
        return current

    def _lock(self, purpose):
        """The printer's lock and the handshake: the held lock, else raises _Early."""
        base = self.plan.base
        lock = self.store.lock(base.printer.id, purpose=purpose, run_id=self.plan.run_id, line_id=base.line.id,
                               machine_id=base.machine.id)
        try:
            state = lock.acquire()
        except LockHeld as e:
            raise _Early(_holder_msg(e, line_name=base.line.name, machine_name=base.machine.name))
        except ShareError as e:
            raise _Early(e.msg)
        if state != "held":
            lock.release()
            raise _Early(Msg(_NEEDS_SERVER))
        lock.start_keeping()
        try:
            answer = self._poller_steps_aside(lock, base.machine.id)
        except ShareError as e:
            lock.release()
            raise _Early(e.msg)
        if answer != "ready":
            lock.release()
            raise _Early(_waited_msg(base.machine.name, self.timings) if answer == "timeout" else Msg(_NEEDS_SERVER))
        return lock

    def _log(self, entry):
        self.data.append_log("hx-admin", entry)

    def _write_expected(self, lock, state):
        lock.check()
        self.store.write_expected(state)
        self._expected = state

    # ---- run (§15.4)

    def run(self, on_event: Callable[[object], None], *, cancel: CancelToken | None = None) -> TestResult:
        self._on_event = on_event
        current = self._admin()
        company = current.company
        purpose = "test" if self.plan.kind == "test" else "calibration"
        # Timed or counted comes from the plan, the counter from the company (§15.2 item 7, §15.4).
        # If the company's bag counter changed since plan_test they disagree: a counted plan would
        # read "none" mid-test. Refused here, before the lock, so nothing is sent
        if self.plan.timed != (company.bag_counter == "none"):
            return self._untouched("failed", Msg(_COUNTER_CHANGED))
        try:
            lock = self._lock(purpose)
        except _Early as e:
            return self._untouched("failed", e.msg)
        try:
            return self._run_locked(lock, company, cancel)
        finally:
            lock.release()

    def _run_locked(self, lock, company, cancel):
        plan, base = self.plan, self.plan.base
        machine, n = base.machine, len(plan.steps)
        guard = self._guard(lock, cancel=cancel)
        user = self.identity.user
        stopped = Msg(N_("Stopped by {user}."), {"user": user})
        client = None

        # Machine check (§15.4 item 3). Nothing is written yet, but Stop test or a failed check still
        # runs the end procedure (§15.4, architecture §7.5 rule 3). LockLost, WrongPrinter, AuthFailed
        # and a share failure end the run at once with nothing changed
        try:
            if cancel is not None and cancel.cancelled:
                raise _Stop()
            client = self.clients.client(base.printer)

            def on_ink(msg, mismatch):
                if not mismatch:
                    raise _Failed(msg)
                # Empty film: only a warning
                self._post("warning", 0, Msg(N_("{machine}'s ink doesn't match the film: the marks may not show."),
                                             {"machine": machine.name}))
            printing, current = _check_printer(client, base, company, guard, on_ink, on_uid=self._uid_checked)
            before = self._read_expected(machine.id)
        except _Stop:
            return self._end(client, lock, "cancelled", stopped)
        except (LockLost, WrongPrinter, AuthFailed, ShareError) as e:
            # The lock isn't ours, the address isn't this printer, or the share can't record the end
            return self._untouched("failed", e.msg)
        except ForbiddenCommand:
            raise
        except HxError as e:
            lost = self._lease_ran_out(lock, e)
            if lost is not None:
                return self._untouched("failed", lost.msg)
            self._post("failed", 0, e.msg)
            return self._end(client, lock, "failed", e.msg)
        previous = _running_message(current, origin_from_design(current))
        counter = company.bag_counter

        # Start (§15.4 item 4). Nothing changes on the printer before "Test running" is on the share
        try:
            self._write_expected(lock, self._test_state(before, previous))
            lock.update(step=0, of=n)
            self._log({
                "type": "test_start", "run_id": plan.run_id, "kind": plan.kind, "machine_id": machine.id,
                "machine_name": machine.name, "printer_id": base.printer.id, "film_id": base.film.id,
                "film_code": base.film.code, "film_revision": base.film.revision,
                "ranges": to_json(self.ranges) if self.ranges is not None else None,
                "steps": [to_json(s) for s in plan.steps], "bags_per_step": plan.bags_per_step, "timed": plan.timed,
                "counter_tag": counter, "previous": {"slot": previous.slot, "name": previous.name,
                                                     "fingerprint": previous.fingerprint}, "at": self._now()})
        except (ShareError, ValueError) as e:
            msg = e.msg if isinstance(e, HxError) else Msg(str(e))
            if self._expected is None or isinstance(e, LockLost):
                return self._untouched("failed", msg)
            self._post("failed", 0, msg)
            return self._end(client, lock, "failed", msg)

        try:
            self._steps(client, lock, guard, company, cancel, printing, current)
        except _Stop:
            return self._end(client, lock, "cancelled", Msg(N_("Stopped by {user}."), {"user": user}))
        except _NoPrints:
            return self._end(client, lock, "no_prints", Msg(N_("No prints for {seconds} s, so the test was stopped."),
                                                            {"seconds": round(self.timings.test_no_print_s)}))
        except _TimeLimit:
            return self._end(client, lock, "time_limit", Msg(N_("The test ran past its time limit, so it was stopped.")))
        except (LockLost, WrongPrinter, AuthFailed) as e:
            # Stop talking to that printer at once: printing has to be switched off there
            return self._lost(lock, e)
        except ForbiddenCommand:
            raise
        except HxError as e:
            lost = self._lease_ran_out(lock, e)
            if lost is not None:
                return self._lost(lock, lost)
            self._post("failed", self._index, e.msg)
            return self._end(client, lock, "failed", e.msg)
        except Exception as e:
            traceback.print_exc()
            _log_internal(self.data, where="test", ref_key="run_id", ref=plan.run_id, machine_id=machine.id,
                          step=f"step {self._index}", error=e, at=self._now())
            msg = _internal_msg(e)
            self._post("failed", self._index, msg)
            return self._end(client, lock, "failed", msg)
        return self._end(client, lock, "done", None)

    def _steps(self, client, lock, guard, company, cancel, printing, current):
        plan, base = self.plan, self.plan.base
        machine, n = base.machine, len(plan.steps)
        self._index = 1
        slot = idle_slot(printing, current.slot)
        self._post("building", 1)
        built = self._build(client, guard, plan.steps[0], slot)
        k = 1
        while True:
            d2 = self._switch(client, guard, cancel, built)
            switched = self.clock.monotonic()
            count_start = None
            if not plan.timed:
                count_start = client.read(company.bag_counter, deadline=guard("read"))
            step_started_wall = self._now()
            step_started = self.clock.monotonic()
            test = dict(self._expected.test or {}, step=k)
            self._write_expected(lock, dataclasses.replace(
                self._expected, message_name=built.name, content_code=built.content_code, slot=built.slot,
                fingerprint=fingerprint(d2), test=test))
            lock.update(step=k, of=n)
            self._post("printing", k, design=d2, bags=None if plan.timed else 0)
            # Build ahead: the next step goes into the other slot while this one prints
            next_built = None
            if k < n:
                self._index = k + 1
                self._post("building", k + 1)
                next_built = self._build(client, guard, plan.steps[k], SLOTS[0] if built.slot == SLOTS[1] else SLOTS[1])
                self._index = k
            if plan.timed:
                bags, count_end = None, None
                self._wait_timed(lock, cancel, step_started, switched)
            else:
                bags, count_end = self._count(guard, cancel, client, company.bag_counter, count_start, k,
                                              since=switched)
            self._post("ok", k, bags=bags)
            self.steps_done = k
            step = plan.steps[k - 1]
            step_plan = self._step_plans[k - 1]
            self._log({
                "type": "test_step", "run_id": plan.run_id, "kind": plan.kind, "machine_id": machine.id,
                "film_id": base.film.id, "index": k, "of": n, "label": step.label, "slot": built.slot,
                "message_name": built.name, "content_code": built.content_code,
                "position": to_json(step_plan.position), "settings": to_json(step_plan.settings),
                "fonts": [list(f) for f in step.fonts], "counter_tag": company.bag_counter,
                "count_start": count_start, "count_end": count_end, "bags": bags, "started_at": step_started_wall,
                "ended_at": self._now(), "running": design_to_log(d2)})
            if k == n:
                return
            built = next_built
            k += 1
            self._index = k

    def _build(self, client, guard, step, slot):
        """Build one step's message into slot and check it there (§10.7, §10.8)."""
        if self._step_plans is None:
            self._step_plans = [test_step_plan(self.plan.base, s) for s in self.plan.steps]
        built = build_message(self._step_plans[step.index - 1], slot)
        for cmd in built.commands:
            client.command(cmd, deadline=guard("command"))
        d = client.load_slot(slot, deadline=guard("load_slot"))
        self._check_readback(built, d)
        return built

    def _check_readback(self, built, design):
        reading = self.time.cached()
        local = to_local(design.read_at + reading.offset_s, offset_fn=self.time.offset_fn)
        problem = _readback_problem(built, design, local, self.plan.base.machine.name)
        if problem is not None:
            raise problem

    def _switch(self, client, guard, cancel, built):
        """PRINTMSG, PRINT 1 and the two running checks of a Send (§10.10); the second read-back."""
        name = self.plan.base.machine.name
        client.command(Command.set("PRINTMSG", built.slot), deadline=guard("command"))
        client.command(Command.set("PRINT", 1), deadline=guard("command"))
        last = None
        for check in (1, 2):
            if check == 2 and not self.clock.sleep(self.timings.confirm_gap_s, cancel):
                raise _Stop()
            d = client.load_slot(0, deadline=guard("load_slot"))
            if d.slot != built.slot:
                raise _Failed(Msg(N_("{machine} is printing slot {got}, not slot {slot}."),
                                  {"machine": name, "got": d.slot, "slot": built.slot}))
            self._check_readback(built, d)
            if client.read("PRIN", deadline=guard("read")) != 1:
                raise _Failed(Msg(N_("{machine} didn't turn printing on."), {"machine": name}))
            last = d
        return last

    def _count(self, guard, cancel, client, counter, start, k, *, since):
        """§15.4 item 6.2: the step's bags from the counter; (bags, last reading). The no-print limit
        runs from the step's switch (since) or the counter's last rise."""
        need, limit = self.plan.bags_per_step, self.timings.test_no_print_s
        bags, value = 0, start
        last_rise = since
        while bags < need:
            if not self.clock.sleep(self.timings.second_reading_s, cancel):
                raise _Stop()
            value = client.read(counter, deadline=guard("read"))
            if value < start:
                start = value       # the counter was reset: a new start
            n = value - start
            if n != bags:
                if n > bags:
                    last_rise = self.clock.monotonic()
                bags = n
                self._post("printing", k, bags=bags)
            if bags < need and self.clock.monotonic() - last_rise >= limit:
                raise _NoPrints()
        return bags, value

    def _wait_timed(self, lock, cancel, step_started, switched):
        """Timed steps (no counter counts bags): seconds_per_bag x bags_per_step. The time limit is the
        step's time plus test_no_print_s from its own switch, like the no-print limit of counted steps.
        A limit for the whole test from step 1's switch would leave out every step's switch and running
        checks (a few seconds each), so a long test that runs well would be stopped."""
        machine = self.plan.base.machine
        duration = machine.seconds_per_bag * self.plan.bags_per_step
        limit = duration + self.timings.test_no_print_s
        while True:
            now = self.clock.monotonic()
            if now - switched >= limit:
                raise _TimeLimit()
            left = duration - (now - step_started)
            if left <= 0:
                return
            if not self.clock.sleep(min(left, self.timings.second_reading_s), cancel):
                raise _Stop()
            lock.check()

    # ---- the end procedure (§15.5)

    def _lost(self, lock, error):
        """LockLost, WrongPrinter or AuthFailed once the test has started: no more requests; printing
        must be switched off at the printer."""
        machine = self.plan.base.machine
        msg = _stop_failed_msg(machine.name)
        self._post("failed", self._index, error.msg)
        if not isinstance(error, LockLost):
            self._finish_rows(lock, printing_off=False)
        self._log_end("failed", False, msg, False, {"9": False, "10": False})
        return self._finished(self._result(False, msg))

    def _end(self, client, lock, reason, msg):
        """Printing off (read twice), both slots emptied when it is off, the rows and the log."""
        machine = self.plan.base.machine
        guard = self._guard(lock)
        if not self._uid_ok:
            # Stopped before the machine check got as far as the UID: check it first, as Try again
            # does, so nothing is ever written to another printer. If it can't be checked, nothing is sent
            try:
                if client is None:
                    client = self.clients.client(self.plan.base.printer)
                client.check_uid(deadline=guard("check_uid"))
                self._uid_ok = True
            except ForbiddenCommand:
                raise
            except HxError as e:
                return self._untouched(reason, (self._lease_ran_out(lock, e) or e).msg)
        self._post("stopping", self._index)
        printing_off, lost = False, False
        slots = {"9": False, "10": False}
        try:
            client.command(Command.set("PRINT", 0), deadline=guard("command"))
            printing_off = client.read_twice("PRIN", deadline=guard("read_twice")) == 0
        except LockLost:
            lost = True
        except ForbiddenCommand:
            raise
        except HxError as e:
            # A call refused at its lease deadline: the printer may be another PC's now (send.md §10.5)
            lost = self._lease_ran_out(lock, e) is not None
            printing_off = False
        if printing_off and not lost:
            for slot in SLOTS:
                try:
                    for cmd in empty_message_commands(slot):
                        client.command(cmd, deadline=guard("command"))
                    slots[str(slot)] = cleared(client.load_slot(slot, deadline=guard("load_slot")))
                except LockLost:
                    lost = True
                    break
                except ForbiddenCommand:
                    raise
                except HxError as e:
                    slots[str(slot)] = False
                    if self._lease_ran_out(lock, e) is not None:
                        lost = True
                        break
        both = all(slots.values())
        if not lost:
            self._finish_rows(lock, printing_off=printing_off)
        done = reason in ("done", "retried")
        if not printing_off:
            msg = _stop_failed_msg(machine.name)
        elif not both and msg is None:
            slot = next(s for s in ("9", "10") if not slots[s])
            msg = Msg(N_("Slot {slot} on {machine} couldn't be emptied."), {"slot": slot, "machine": machine.name})
        ok = reason == "done" and printing_off and both and self.steps_done == len(self.plan.steps)
        self._log_end(reason, ok, msg, printing_off, slots, cleared_=both)
        self._post("cleared" if both else "failed", self._index, None if both and done else msg)
        return self._finished(self._result(ok, msg, cleared_=both, printing_off=printing_off))

    def _finish_rows(self, lock, *, printing_off):
        """The rows "Test finished" (or "Test not stopped") and the line status "Test finished"."""
        base = self.plan.base
        user, at = self.identity.user, self._now()
        try:
            lock.check()
            # Ended before "Test running" was written (a failed machine check): this test's row all the same
            state = self._expected if self._expected is not None else \
                self._test_state(self._read_expected(base.machine.id), None)
            test = dict(state.test or {"run_id": self.plan.run_id, "kind": self.plan.kind, "by": user,
                                       "of": len(self.plan.steps)}, step=self.steps_done, ended_at=at)
            self._write_expected(lock, dataclasses.replace(
                state, mode="test_finished" if printing_off else "stop_requested_at_printer", printing_expected=False,
                message_name="", content_code="", fingerprint="", slot=None, resume_blocked=True, stop_reason="test",
                stopped_by=user, stopped_at=at, test=test))
        except ShareError as e:
            print(f"Could not record the end of test {self.plan.run_id}: {e}", file=sys.stderr)
            if isinstance(e, LockLost):
                return          # the printer may be another PC's now: write nothing more (send.md §10.5)
        try:
            lock.check()
        except LockLost:
            return
        _write_line_flag(self.store, base.line.id, "", stopped_needs_send=True)

    def _log_end(self, reason, ok, msg, printing_off, slots, *, cleared_=False):
        base = self.plan.base
        try:
            self._log({"type": "test_end", "run_id": self.plan.run_id, "kind": self.plan.kind,
                       "machine_id": base.machine.id, "film_id": base.film.id, "steps_done": self.steps_done,
                       "of": len(self.plan.steps), "ok": ok, "reason": reason, "msg": _msg_json(msg),
                       "printing_off": printing_off, "cleared": cleared_, "slots": dict(slots), "at": self._now()})
        except (ShareError, ValueError) as e:
            print(f"Could not log the end of test {self.plan.run_id}: {e}", file=sys.stderr)

    def end_again(self, on_event: Callable[[object], None]) -> TestResult:
        """Only the end procedure, under a new lock: the administrator's Try again when printing
        couldn't be confirmed off. When it can't run, printing_off stays False (the test may still be
        printing), except after a later Send or test on the machine: then it is True and nothing is
        sent, because this test is no longer what the machine prints. (No expected state at all means
        this test never wrote "Test running", so it never switched the printer either.)"""
        self._on_event = on_event
        self._admin()
        base = self.plan.base
        try:
            lock = self._lock("test")
        except _Early as e:
            return self._untouched("retried", e.msg, printing_off=False)
        try:
            try:
                # Only while the machine's expected state is still this test's: a later Send or test
                # must never be stopped or emptied by this Try again
                state = self.store.read_expected(base.machine.id)
                if state is None or (state.test or {}).get("run_id") != self.plan.run_id:
                    return self._untouched("retried", Msg(N_("{machine} has had a Send or another test since this "
                                                             "test, so Stripes didn't change it."),
                                                          {"machine": base.machine.name}))
                self._expected = state
                client = self.clients.client(base.printer)
                client.check_uid(deadline=self._guard(lock)("check_uid"))
                self._uid_ok = True
            except ForbiddenCommand:
                raise
            except HxError as e:
                return self._untouched("retried", e.msg, printing_off=False)
            return self._end(client, lock, "retried", None)
        finally:
            lock.release()


class _NoPrints(Exception):
    """The counter didn't rise for test_no_print_s."""


class _TimeLimit(Exception):
    """A timed test ran past its time limit."""
