"""MapleJet machine states: judging printer readings, alerts, views and the floor board.

Pure: no file, network, Windows or Tk work. The poller (hx_poller) hands judge() one
cycle's printer reading with the machine's expected state, the stale lock it polls
past and the records; judge() turns them into a state code and the machine's active
alerts, and applies the two-reading rule: a reading that would change the state or
raise an alert is read again a second later and must agree.

The status watcher overlays what needs no printer reading (a fresh lock, staleness,
an expected state the poller hasn't judged yet, acknowledgements) with
machine_view() and board_view(). The UI shows the views as they are and never
decides a code, colour, button or sound itself.

Every Msg made here holds record names, user and computer names and numbers only,
never bag text: the origin line travels in its own field.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field

import hx_message
from hx_core import Msg
from hx_model import (AMBER, BLUE, GREEN, GREY, RED, TIMINGS, Ack, Alert, BoardView, Company, ExpectedState,
                      FloorRow, HxData, Identity, Line, LineStatus, LineView, LockInfo, Machine, MachineSnapshot,
                      MachineView, PollStatus, RunningMessage, Timings, same_user)
from i18n import N_, add_spanish

# --------------------------------------------------------------------------
# State codes (states.md §2)
# --------------------------------------------------------------------------

STATE_CODES = {
    # view codes: machine_view and board_view only, never written in a poll status
    "send_in_progress": BLUE,
    "switch_pending": BLUE,
    "test_running": BLUE,
    "action_in_progress": BLUE,
    "checking": GREY,
    "not_monitored": GREY,          # or the last known red or amber (§5.2)
    "update_stripes": GREY,
    "not_set_up": GREY,
    # judge and poller codes
    "internal_error": RED,
    "wrong_printer": RED,
    "login_failed": RED,
    "test_orphaned": RED,
    "test_interrupted": RED,
    "interrupted_unknown": RED,
    "interrupted_old": RED,
    "interrupted_new": AMBER,
    "stop_failed": RED,
    "stop_requested": RED,
    "failed_after_switch": RED,
    "failed_before_switch": RED,
    "left_red": RED,
    "rejected": RED,
    "printing_on_unexpected": RED,
    "unreachable_printing": RED,
    "printing_off": RED,
    "drift": RED,
    "unknown_while_down": AMBER,
    "ink_out": RED,
    "cartridge_error": RED,
    "stopped_needs_send": RED,
    "test_finished": RED,
    "off": GREY,
    "not_printing": AMBER,
    "message_unchecked": AMBER,
    "unconfirmed": AMBER,
    "confirmed_with_notes": AMBER,
    "not_from_stripes": AMBER,
    "stopped": GREY,
    "idle": GREY,
    "running_confirmed": GREEN,
}

PRECEDENCE = ("wrong_printer", "login_failed",
              "test_orphaned", "test_interrupted",
              "interrupted_unknown", "interrupted_old", "interrupted_new",
              "stop_failed", "stop_requested",
              "failed_after_switch", "left_red", "failed_before_switch",
              "rejected", "printing_on_unexpected",
              "unreachable_printing", "printing_off", "drift", "unknown_while_down",
              "ink_out", "cartridge_error",
              "stopped_needs_send", "test_finished",
              "off", "not_printing", "message_unchecked",
              "unconfirmed", "confirmed_with_notes", "not_from_stripes",
              "stopped", "idle", "running_confirmed")

BLUE_CODES = ("send_in_progress", "switch_pending", "test_running", "action_in_progress")
VIEW_CODES = BLUE_CODES + ("checking", "not_monitored", "update_stripes", "not_set_up")
NEEDS_SEND_CODES = ("failed_before_switch", "failed_after_switch", "left_red", "rejected", "stopped_needs_send",
                    "test_finished", "test_interrupted", "test_orphaned", "interrupted_old", "interrupted_unknown",
                    "drift", "printing_on_unexpected", "not_from_stripes", "idle")
WORST_ORDER = (RED, AMBER, BLUE, GREY, GREEN)
INK_NOT_DETECTED, INK_NORMAL, INK_LOW, INK_CRITICAL, INK_NONE = 0, 1, 2, 3, 4

# The lock purposes that make a machine blue with their own headline
_ACTION_PURPOSES = ("stop", "resume", "clock")

# --------------------------------------------------------------------------
# Texts (§10)
# --------------------------------------------------------------------------

STATE_TEXTS = {
    "send_in_progress": N_("Sending: {user} on {computer}"),
    "switch_pending": N_("Switching to the new message: {user} on {computer}"),
    "test_running": N_("TEST by {user}, step {step} of {of}"),
    "test_running:nostep": N_("TEST by {user}"),
    "action_in_progress:stop": N_("Stopping printing: {user}"),
    "action_in_progress:resume": N_("Resuming printing: {user}"),
    "action_in_progress:clock": N_("Setting the clock: {user}"),
    "action_in_progress:other": N_("Printer in use: {user} on {computer}"),
    "checking": N_("Checking..."),
    "not_monitored": N_("NOT MONITORED"),
    "update_stripes": N_("Update Stripes to see this machine"),
    "not_set_up": N_("No printer set up"),
    "internal_error": N_("Stripes hit a problem checking this machine"),
    "wrong_printer": N_("Wrong printer at this address"),
    "login_failed": N_("Stripes can't log in to this printer"),
    "test_orphaned": N_("Test message still printing"),
    "test_interrupted": N_("Test stopped on {computer}: needs a Send"),
    "interrupted_unknown": N_("Send interrupted on {computer}: check the bags now"),
    "interrupted_old": N_("Send interrupted on {computer}"),
    "interrupted_new": N_("Send interrupted, not confirmed"),
    "stop_failed": N_("Still printing: stop it at the printer now"),
    "stop_requested": N_("Stop it at the printer: press Print On/Off"),
    "failed_after_switch": N_("Send failed after the switch: check the bags now"),
    "failed_before_switch": N_("Send failed: still printing the previous message"),
    "left_red": N_("Left red by {user}"),
    "rejected": N_("Rejected by {user}"),
    "printing_on_unexpected": N_("Printing was switched on at the printer"),
    "unreachable_printing": N_("Not answering while printing bags"),
    "printing_off": N_("Printing was switched off at the printer"),
    "drift": N_("Not printing what Stripes sent"),
    "unknown_while_down": N_("Can't check the message while the server is down"),
    "ink_out": N_("Out of ink"),
    "cartridge_error": N_("Cartridge not detected"),
    "stopped_needs_send": N_("Stopped, needs a Send"),
    "test_finished": N_("Stopped, needs a Send"),
    "off": N_("Printer off"),
    "not_printing": N_("Printing is off"),
    "message_unchecked": N_("Can't read the message on this printer"),
    "unconfirmed": N_("Send not confirmed"),
    "confirmed_with_notes": N_("Printing, not fully checked"),
    "not_from_stripes": N_("Not sent from Stripes yet"),
    "stopped": N_("Stopped by {user}"),
    "idle": N_("Not printing"),
    "running_confirmed": N_("Printing"),
}

# Alert texts that depend on the case
LOW_INK_TEXTS = {
    INK_LOW: N_("Low ink on {machine}"),
    INK_CRITICAL: N_("Very low ink on {machine}"),
    INK_NONE: N_("No ink on {machine}"),
    INK_NOT_DETECTED: N_("No cartridge detected on {machine}"),
}
SETUP_TEXTS = {
    "wrong_printer": N_("Another printer answers at the address of {machine}"),
    "login_failed": N_("Stripes can't log in to the printer of {machine}"),
}

# Details, lines and the board (§10.3)
T_STARTED_BY = N_("Started by {user}.")
T_STARTED_NEEDS_SEND = N_("Started by {user}. This machine needs a new Send.")
T_AFTER_TEST = N_("After a test by {user}.")
T_CONFIRMED_BY = N_("Confirmed by {user}.")
T_WAITING_CONFIRM = N_("Waiting for {user} to confirm.")
T_LAST_READ = N_("Message last read {minutes} min ago.")
T_NOT_READ_YET = N_("Stripes hasn't been able to read the message yet.")
T_NOT_ANSWERING = N_("Not answering for {minutes} min.")
T_UNFINISHED = {
    "stop": N_("A Stop on {computer} didn't finish."),
    "resume": N_("A Resume on {computer} didn't finish."),
    "clock": N_("A clock set on {computer} didn't finish."),
}
T_MESSAGE_IN_SLOT = N_("Message {name} in slot {slot}.")
T_PC_CLOCK = N_("Compared with this PC's clock.")
T_WAITING_SIGN_IN = N_("Waiting for {user} to sign in.")
T_NO_MACHINES = N_("No machines")
T_MACHINE_STATE = N_("{machine}: {state}")
T_ORIGINS_DIFFER = N_("Different origins on this line")
T_NOT_CHECKED = N_("Not checked yet")
T_CHECKED_S = N_("checked {seconds} s ago")
T_CHECKED_MIN = N_("checked {minutes} min ago")
T_BANNER_STANDALONE = N_("Server unreachable: showing this PC's own checks · next check in {seconds} s")
T_BANNER_DOWN = N_("Server unreachable: this PC isn't checking any printers.")

ES = {
    # state headlines
    "Sending: {user} on {computer}": "Enviando: {user} en {computer}",
    "Switching to the new message: {user} on {computer}": "Cambiando al mensaje nuevo: {user} en {computer}",
    "TEST by {user}, step {step} of {of}": "PRUEBA de {user}, paso {step} de {of}",
    "TEST by {user}": "PRUEBA de {user}",
    "Stopping printing: {user}": "Deteniendo la impresión: {user}",
    "Resuming printing: {user}": "Reanudando la impresión: {user}",
    "Setting the clock: {user}": "Ajustando el reloj: {user}",
    "Printer in use: {user} on {computer}": "Impresora en uso: {user} en {computer}",
    "Checking...": "Revisando...",
    "NOT MONITORED": "SIN MONITOREO",
    "Update Stripes to see this machine": "Actualice Stripes para ver esta máquina",
    "No printer set up": "No hay una impresora configurada",
    "Stripes hit a problem checking this machine": "Stripes tuvo un problema al revisar esta máquina",
    "Wrong printer at this address": "Impresora equivocada en esta dirección",
    "Stripes can't log in to this printer": "Stripes no puede iniciar sesión en esta impresora",
    "Test message still printing": "El mensaje de prueba sigue imprimiendo",
    "Test stopped on {computer}: needs a Send": "Prueba detenida en {computer}: necesita un envío",
    "Send interrupted on {computer}: check the bags now":
        "Envío interrumpido en {computer}: revise las bolsas ahora",
    "Send interrupted on {computer}": "Envío interrumpido en {computer}",
    "Send interrupted, not confirmed": "Envío interrumpido, no confirmado",
    "Still printing: stop it at the printer now": "Sigue imprimiendo: deténgala en la impresora ahora",
    "Stop it at the printer: press Print On/Off": "Deténgala en la impresora: presione Print On/Off",
    "Send failed after the switch: check the bags now":
        "El envío falló después del cambio: revise las bolsas ahora",
    "Send failed: still printing the previous message": "El envío falló: sigue imprimiendo el mensaje anterior",
    "Left red by {user}": "Dejada en rojo por {user}",
    "Rejected by {user}": "Rechazado por {user}",
    "Printing was switched on at the printer": "Se encendió la impresión en la impresora",
    "Not answering while printing bags": "No responde y estaba imprimiendo bolsas",
    "Printing was switched off at the printer": "Se apagó la impresión en la impresora",
    "Not printing what Stripes sent": "No imprime lo que envió Stripes",
    "Can't check the message while the server is down":
        "No se puede revisar el mensaje mientras el servidor no responde",
    "Out of ink": "Sin tinta",
    "Cartridge not detected": "Cartucho no detectado",
    "Stopped, needs a Send": "Detenida, necesita un envío",
    "Printer off": "Impresora apagada",
    "Printing is off": "La impresión está apagada",
    "Can't read the message on this printer": "No se puede leer el mensaje de esta impresora",
    "Send not confirmed": "Envío no confirmado",
    "Printing, not fully checked": "Imprimiendo, sin revisión completa",
    "Not sent from Stripes yet": "Todavía no se envió desde Stripes",
    "Stopped by {user}": "Detenida por {user}",
    "Not printing": "No está imprimiendo",
    "Printing": "Imprimiendo",
    # alert messages
    "Send failed on {machine}": "El envío falló en {machine}",
    "{user} said something's wrong on {machine}": "{user} indicó que algo está mal en {machine}",
    "Send interrupted on {machine} ({computer})": "Envío interrumpido en {machine} ({computer})",
    "Stop failed on {machine}: stop it at the printer": "No se pudo detener {machine}: deténgala en la impresora",
    "{machine} isn't printing what Stripes sent": "{machine} no imprime lo que envió Stripes",
    "Printing was switched off on {machine}": "Se apagó la impresión en {machine}",
    "Printing was switched on at the printer on {machine}":
        "Se encendió la impresión en la impresora de {machine}",
    "{machine} is out of ink": "{machine} se quedó sin tinta",
    "Cartridge not detected on {machine}": "Cartucho no detectado en {machine}",
    "{machine} stopped answering while printing bags": "{machine} dejó de responder mientras imprimía bolsas",
    "Test message still printing on {machine}": "El mensaje de prueba sigue imprimiendo en {machine}",
    "Encoder settings changed on {machine}": "Cambió la configuración del encoder en {machine}",
    "Low ink on {machine}": "Poca tinta en {machine}",
    "Very low ink on {machine}": "Muy poca tinta en {machine}",
    "No ink on {machine}": "Sin tinta en {machine}",
    "No cartridge detected on {machine}": "No se detecta cartucho en {machine}",
    "Printer clock on {machine} is {minutes} min off":
        "El reloj de la impresora de {machine} tiene {minutes} min de diferencia",
    "Something else is talking to printer {printer}": "Algo más se está comunicando con la impresora {printer}",
    "Another printer answers at the address of {machine}": "Otra impresora responde en la dirección de {machine}",
    "Stripes can't log in to the printer of {machine}": "Stripes no puede iniciar sesión en la impresora de {machine}",
    "Printer {printer} doesn't have the standard fonts": "La impresora {printer} no tiene las fuentes estándar",
    "Stripes can't read the message on {machine}": "Stripes no puede leer el mensaje de {machine}",
    "{machine} is printing a message that wasn't sent from Stripes":
        "{machine} está imprimiendo un mensaje que no se envió desde Stripes",
    # details, lines and the board
    "Started by {user}.": "Iniciado por {user}.",
    "Started by {user}. This machine needs a new Send.": "Iniciado por {user}. Esta máquina necesita un nuevo envío.",
    "After a test by {user}.": "Después de una prueba de {user}.",
    "Confirmed by {user}.": "Confirmado por {user}.",
    "Waiting for {user} to confirm.": "Esperando la confirmación de {user}.",
    "Message last read {minutes} min ago.": "Mensaje leído por última vez hace {minutes} min.",
    "Stripes hasn't been able to read the message yet.": "Stripes todavía no ha podido leer el mensaje.",
    "Not answering for {minutes} min.": "No responde desde hace {minutes} min.",
    "A Stop on {computer} didn't finish.": "Una detención en {computer} no terminó.",
    "A Resume on {computer} didn't finish.": "Una reanudación en {computer} no terminó.",
    "A clock set on {computer} didn't finish.": "Un ajuste de reloj en {computer} no terminó.",
    "Message {name} in slot {slot}.": "Mensaje {name} en la posición {slot}.",
    "Compared with this PC's clock.": "Comparado con el reloj de esta PC.",
    "Waiting for {user} to sign in.": "Esperando a que {user} inicie sesión.",
    "No machines": "Sin máquinas",
    "{machine}: {state}": "{machine}: {state}",
    "Different origins on this line": "Orígenes distintos en esta línea",
    "Not checked yet": "Todavía sin revisar",
    "checked {seconds} s ago": "revisada hace {seconds} s",
    "checked {minutes} min ago": "revisada hace {minutes} min",
    "Server unreachable: showing this PC's own checks · next check in {seconds} s":
        "Servidor inaccesible: se muestran las revisiones de esta PC · próxima revisión en {seconds} s",
    "Server unreachable: this PC isn't checking any printers.":
        "Servidor inaccesible: esta PC no está revisando ninguna impresora.",
}
add_spanish(ES)

# --------------------------------------------------------------------------
# Alert kinds (§4.1)
# --------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AlertKind:
    kind: str
    sound: bool
    colour: str                       # RED, AMBER or "" (doesn't colour the machine)
    roles: tuple[str, ...]            # "sender", "notify", "last_sender", "floor", "admins", "monitor"
    text: str                         # the N_ text of Alert.msg (low_ink and setup_problem pick by case)
    ref_from: str = ""                # "send_id", "run_id" or "" (time-based id)


_FLOOR_ROLES = ("notify", "last_sender", "floor")

ALERT_KINDS = {k.kind: k for k in (
    AlertKind(kind="send_failed", sound=True, colour=RED, roles=("sender", "notify"),
              text=N_("Send failed on {machine}"), ref_from="send_id"),
    AlertKind(kind="send_rejected", sound=True, colour=RED, roles=("sender", "notify"),
              text=N_("{user} said something's wrong on {machine}"), ref_from="send_id"),
    # the colour is the code's: RED, or AMBER for interrupted_new
    AlertKind(kind="send_interrupted", sound=True, colour=RED, roles=("sender", "notify"),
              text=N_("Send interrupted on {machine} ({computer})"), ref_from="send_id"),
    AlertKind(kind="stop_failed", sound=True, colour=RED, roles=("last_sender", "notify", "floor"),
              text=N_("Stop failed on {machine}: stop it at the printer")),
    AlertKind(kind="drift", sound=True, colour=RED, roles=_FLOOR_ROLES,
              text=N_("{machine} isn't printing what Stripes sent")),
    AlertKind(kind="printing_off", sound=True, colour=RED, roles=_FLOOR_ROLES,
              text=N_("Printing was switched off on {machine}")),
    AlertKind(kind="printing_on", sound=True, colour=RED, roles=_FLOOR_ROLES,
              text=N_("Printing was switched on at the printer on {machine}")),
    AlertKind(kind="ink_out", sound=True, colour=RED, roles=_FLOOR_ROLES, text=N_("{machine} is out of ink")),
    AlertKind(kind="cartridge_error", sound=True, colour=RED, roles=_FLOOR_ROLES,
              text=N_("Cartridge not detected on {machine}")),
    AlertKind(kind="went_silent", sound=True, colour=RED, roles=_FLOOR_ROLES,
              text=N_("{machine} stopped answering while printing bags")),
    AlertKind(kind="test_still_printing", sound=True, colour=RED, roles=("notify", "floor"),
              text=N_("Test message still printing on {machine}"), ref_from="run_id"),
    AlertKind(kind="encoder_changed", sound=True, colour=RED, roles=_FLOOR_ROLES,
              text=N_("Encoder settings changed on {machine}")),
    AlertKind(kind="low_ink", sound=False, colour=AMBER, roles=_FLOOR_ROLES, text=LOW_INK_TEXTS[INK_LOW]),
    AlertKind(kind="clock_off", sound=False, colour=AMBER, roles=_FLOOR_ROLES,
              text=N_("Printer clock on {machine} is {minutes} min off")),
    AlertKind(kind="foreign_client", sound=False, colour="", roles=("admins", "monitor"),
              text=N_("Something else is talking to printer {printer}")),
    AlertKind(kind="setup_problem", sound=False, colour="", roles=("admins", "notify"),
              text=SETUP_TEXTS["wrong_printer"]),
    AlertKind(kind="fonts_differ", sound=False, colour="", roles=("admins", "monitor"),
              text=N_("Printer {printer} doesn't have the standard fonts")),
    AlertKind(kind="message_unreadable", sound=False, colour=AMBER, roles=("notify", "admins", "monitor"),
              text=N_("Stripes can't read the message on {machine}")),
    AlertKind(kind="not_from_stripes", sound=False, colour="", roles=("notify",),
              text=N_("{machine} is printing a message that wasn't sent from Stripes")),
)}

_ROLE_CONSTANTS = {"floor": "floor", "admins": "admins", "monitor": "monitor"}

# --------------------------------------------------------------------------
# Readings, facts and judgements
# --------------------------------------------------------------------------


@dataclass(kw_only=True)
class Reading:
    """One poll cycle's facts. A field that wasn't read this cycle is None."""
    at: float                    # poller's clock.time()
    reachable: bool
    printing: bool | None = None
    message_name: str | None = None      # "" for an unnamed message
    bag_count: int | None = None
    ink_name: str | None = None
    ink_level: int | None = None
    head_temp_c: int | None = None
    clock_offset_s: float | None = None
    running: RunningMessage | None = None   # set on cycles that loaded the running message
    nonsense: int = 0            # replies thrown away this cycle
    second: bool = False         # this is the confirming second reading
    # added by states.md §11.1:
    failures: int = 0                     # failed requests in a row (§6.6)
    problem: str = ""                     # "" | "wrong_printer" | "login_failed"
    bags_recent: int | None = None        # §6.7
    foreign_client: bool | None = None    # §6.8; None: not computed (carry prev)
    running_unchecked: bool | None = None # §6.5 message check; None: not computed (carry prev)
    daily: dict | None = None             # a daily check completed this cycle (§6.10)
    running_reset: bool = False           # R no longer describes the printer and no Load succeeded
    acked: dict[str, Ack] = field(default_factory=dict)   # acks found for prev's active alerts
    standalone: bool = False
    pending_code: str = ""                # second reading: the first reading's candidate code
    pending_alerts: tuple[str, ...] = ()  # ... and its "kind:ref" alert keys, sorted
    clock_source: str = ""                # TimeReading.source the clock offset was taken against


@dataclass(kw_only=True)
class Facts:
    """What the printer showed (§3.2): judged facts are committed by judge(), display facts are the latest."""
    reachable: bool
    problem: str = ""
    printing: bool | None = None
    running: RunningMessage | None = None
    ink_level: int | None = None
    clock_offset_s: float | None = None
    daily: dict = field(default_factory=dict)
    foreign_client: bool = False
    running_unchecked: bool = False
    failures: int = 0
    message_name: str | None = None
    bag_count: int | None = None
    bags_recent: int | None = None
    ink_name: str | None = None
    head_temp_c: int | None = None
    clock_source: str = ""                # display: "pc" when the offset is against this PC's clock


_JUDGED = ("reachable", "problem", "printing", "running", "ink_level", "clock_offset_s", "daily",
           "foreign_client", "running_unchecked")
_DISPLAY = ("failures", "message_name", "bag_count", "bags_recent", "ink_name", "head_temp_c", "clock_source")


@dataclass(frozen=True, kw_only=True)
class AlertNeed:
    kind: str
    ref: str = ""
    colour: str
    msg: Msg
    roles: tuple[str, ...]


@dataclass(kw_only=True)
class Outcome:
    code: str
    values: dict = field(default_factory=dict)       # headline values
    details: list[Msg] = field(default_factory=list)
    alerts: list[AlertNeed] = field(default_factory=list)

    def key(self) -> tuple:
        return self.code, frozenset(f"{a.kind}:{a.ref}" for a in self.alerts)


@dataclass(kw_only=True)
class Judgement:
    state: MachineSnapshot | None        # None only while nothing has ever been committed for the machine
    alerts: list[Alert]          # the machine's full active alert list after this reading
    recheck: list[str] = field(default_factory=list)   # items to read again after second_reading_s
    read_running_now: bool = False
    # added by states.md §11.1:
    facts: Facts | None = None            # what to write in P (§3.8)
    candidate: MachineSnapshot | None = None
    candidate_alerts: tuple[str, ...] = ()   # sorted "kind:ref" keys of the candidate
    changed: bool = False                 # the committed code differs from prev's
    started: list[Alert] = field(default_factory=list)
    ended: list[Alert] = field(default_factory=list)
    committed: bool = True                # False: prev was kept (waiting for, or refused by, a second reading)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def norm_name(name: str | None) -> str:
    """None, "None" and whitespace are an unnamed message: ""; anything else stripped."""
    if name is None:
        return ""
    name = str(name).strip()
    return "" if name == "None" else name


def matches_expected(running: RunningMessage | None, *, name: str, slot: int | None, fingerprint: str) -> bool:
    """R is the expected message: the slot (when given), the name and the content (when known)."""
    if running is None:
        return False
    if slot is not None and running.slot != slot:
        return False
    if norm_name(running.name) != norm_name(name):
        return False
    if fingerprint and running.fingerprint != fingerprint:
        return False
    return True


def known_drift(prev: PollStatus | None) -> bool:
    """P says the printer runs something other than E: the code drift, or an active drift alert (§3.4 C11c)."""
    if prev is None:
        return False
    return ((prev.state is not None and prev.state.code == "drift")
            or any(a.active and a.kind == "drift" for a in prev.alerts))


def was_printing(prev: PollStatus | None, company: Company) -> bool:
    """The printer was printing bags just before it stopped answering (§3.7)."""
    if prev is None or prev.printing is not True:
        return False
    return company.bag_counter == "none" or (prev.bags_recent or 0) > 0


def alert_id(machine_id: str, kind: str, *, ref: str = "", at: float = 0.0) -> str:
    """mc_5d1e2f3a4b.drift.1793275205123 (start in ms), or with a ref
    mc_5d1e2f3a4b.send_failed.sd_8a7b6c5d4e.1793275205123.

    A ref-keyed id carries the start time too, when given: the same test or Send can raise its alert
    again (printing switched back on after test_still_printing ended), and the id must be new for each
    occurrence, because ack files are never removed and an old ack must never silence a new alert
    (§4.2, storage.md §5.6 rule 5). judge() always passes it; without it the id is the ref form alone."""
    if ref:
        base = f"{machine_id}.{kind}.{re.sub(r'[^A-Za-z0-9_-]', '_', ref)[:24]}"
        return f"{base}.{int(at * 1000)}" if at else base
    return f"{machine_id}.{kind}.{int(at * 1000)}"


def resolve_recipients(roles: tuple[str, ...], *, sender: str, notify: str, last_sender: str) -> list[str]:
    """Roles into Alert.recipients (§4.3); users de-duplicated by same_user, keeping the first spelling."""
    out, users = [], []
    for role in roles:
        if role in _ROLE_CONSTANTS:
            entry = _ROLE_CONSTANTS[role]
            if entry not in out:
                out.append(entry)
            continue
        name = {"sender": sender, "notify": notify, "last_sender": last_sender}.get(role, "")
        if not name or any(same_user(name, u) for u in users):
            continue
        users.append(name)
        out.append("user:" + name)
    return out


def can_acknowledge(identity: Identity, company: Company) -> bool:
    """Stripes MapleJet Senders and Stripes Administrators may acknowledge any alert."""
    return identity.is_sender(company) or identity.is_admin(company)


def natural_key(name: str) -> list:
    """"Line 2" before "Line 10": digit runs as ints, the rest casefolded."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
            for part in re.split(r"(\d+)", name or "") if part]


def worst(colours) -> str:
    """RED, AMBER, BLUE, GREY, GREEN, worst first; an empty list is GREY."""
    found = [c for c in colours if c in WORST_ORDER]
    if not found:
        return GREY
    return min(found, key=WORST_ORDER.index)


def _rm_key(r):
    # What a Load says about the running message; read_at and the picture's hash change by themselves
    if r is None:
        return None
    return r.slot, norm_name(r.name), r.fingerprint, r.origin_line


def same_running(a: RunningMessage | None, b: RunningMessage | None) -> bool:
    """Two Loads read the same running message (slot, name, content and origin; not read_at)."""
    return _rm_key(a) == _rm_key(b)


def _sender(e):
    if e is None:
        return ""
    return e.sender or e.written_by


def _test_by(e):
    if e is not None and isinstance(e.test, dict):
        by = e.test.get("by")
        if isinstance(by, str) and by:
            return by
    return ""


def _minutes(at, since):
    if since is None:
        return None
    return int(max(0.0, at - since) // 60)


# --------------------------------------------------------------------------
# Facts
# --------------------------------------------------------------------------

def prev_facts(prev: PollStatus | None) -> Facts:
    """The facts from P alone: what was last confirmed. Facts(reachable=False) when there is no P."""
    if prev is None:
        return Facts(reachable=False)
    return Facts(reachable=prev.reachable, problem=prev.problem, printing=prev.printing, running=prev.running,
                 ink_level=prev.ink_level, clock_offset_s=prev.clock_offset_s, daily=dict(prev.daily or {}),
                 foreign_client=prev.foreign_client, running_unchecked=prev.running_unchecked,
                 failures=prev.failures, message_name=prev.message_name, bag_count=prev.bag_count,
                 bags_recent=prev.bags_recent, ink_name=prev.ink_name, head_temp_c=prev.head_temp_c)


def facts_from(prev: PollStatus | None, reading: Reading) -> Facts:
    """Each fact takes the reading's value when it isn't None, else prev's (§3.2)."""
    old = prev_facts(prev)

    def pick(new, before):
        return before if new is None else new

    if reading.running is not None:
        running = reading.running
    elif reading.running_reset:
        running = None
    else:
        running = old.running
    # A standalone check that got replies but no MSGNAM keeps no name from an earlier check: it never
    # loads the message, so this check's name is all it knows about what prints (C12)
    if reading.standalone and reading.reachable and reading.problem == "":
        message_name = reading.message_name
    else:
        message_name = pick(reading.message_name, old.message_name)
    return Facts(
        reachable=bool(reading.reachable),
        problem=reading.problem if reading.reachable else old.problem,
        printing=pick(reading.printing, old.printing),
        running=running,
        ink_level=pick(reading.ink_level, old.ink_level),
        clock_offset_s=pick(reading.clock_offset_s, old.clock_offset_s),
        daily=dict(reading.daily) if reading.daily is not None else old.daily,
        foreign_client=pick(reading.foreign_client, old.foreign_client),
        running_unchecked=pick(reading.running_unchecked, old.running_unchecked),
        failures=reading.failures,
        message_name=message_name,
        bag_count=pick(reading.bag_count, old.bag_count),
        bags_recent=pick(reading.bags_recent, old.bags_recent),
        ink_name=pick(reading.ink_name, old.ink_name),
        head_temp_c=pick(reading.head_temp_c, old.head_temp_c),
        clock_source=reading.clock_source)


def _same_fact(name, a, b):
    if name == "running":
        return _rm_key(a) == _rm_key(b)
    return a == b


def _judged_differences(old: Facts, new: Facts) -> set:
    return {n for n in _JUDGED if not _same_fact(n, getattr(old, n), getattr(new, n))}


def _clock_off(offset, timings):
    return offset is not None and abs(offset) > timings.clock_warn_s


def evidence(old: Facts, new: Facts, timings: Timings = TIMINGS) -> list[str]:
    """The items the poller reads again before a change commits (§3.8)."""
    items = []

    def add(item):
        if item not in items:
            items.append(item)

    if old.reachable != new.reachable:
        add("reach")
    if old.problem != new.problem:
        if "wrong_printer" in (old.problem, new.problem):
            add("uid")
        if "login_failed" in (old.problem, new.problem):
            add("login")
    if old.printing != new.printing:
        add("PRIN")
    # A running message that couldn't be read is no reading to confirm; a Load that read something
    # different is, and needs a second Load to agree
    if new.running is not None and _rm_key(old.running) != _rm_key(new.running):
        add("load")
    if old.ink_level != new.ink_level:
        add("INKL")
    if _clock_off(old.clock_offset_s, timings) != _clock_off(new.clock_offset_s, timings):
        add("clock")
    old_values = (old.daily or {}).get("values") or {}
    new_values = (new.daily or {}).get("values") or {}
    for tag in ("ENWHD", "ENPPR"):          # the daily values a state depends on (C24)
        if new_values.get(tag) is not None and old_values.get(tag) != new_values.get(tag):
            add(f"daily:{tag}")
    new_fonts = (new.daily or {}).get("fonts")
    if new_fonts and (old.daily or {}).get("fonts") != new_fonts:
        add("fonts")
    return items


# --------------------------------------------------------------------------
# Classifying (§3.4 to §3.6, §3.9)
# --------------------------------------------------------------------------

_NO_EXPECTED_ID = "mc_0000000000"


def _encoder_changed(machine, daily):
    if machine.placement != "encoder" or not isinstance(daily, dict):
        return False
    values = daily.get("values") or {}
    kept = daily.get("encoder_ref") or {}
    refs = {"ENWHD": int(round(machine.encoder_wheel_mm)) if machine.encoder_wheel_mm is not None else None,
            "ENPPR": machine.encoder_ppr}
    for tag in ("ENWHD", "ENPPR"):
        ref = refs[tag] if refs[tag] is not None else kept.get(tag)
        got = values.get(tag)
        if ref is not None and got is not None and got != ref:
            return True
    return False


def _fonts_differ(daily, company):
    fonts = daily.get("fonts") if isinstance(daily, dict) else None
    return isinstance(fonts, list) and bool(fonts) and list(fonts) != list(company.standard_fonts)


def _interrupted(e, lock, *, on, trusted, running):
    """The interrupted-Send code (§3.6), or None when no Send was interrupted."""
    send_lock = lock is not None and lock.purpose == "send"
    sid = lock.run_id if send_lock else e.send_id
    same = e.send_id == sid
    if send_lock:
        if same and (e.failure is not None or e.rejected or (e.mode == "running" and e.confirmed)):
            return None, sid
    elif not (e.mode == "switch_pending" and e.failure is None):
        return None, sid
    if not same:
        return "interrupted_old", sid
    if e.mode == "switch_pending":
        if on and matches_expected(running, name=e.message_name, slot=e.slot, fingerprint=e.fingerprint):
            return "interrupted_new", sid
        if on and running is not None and matches_expected(running, name=e.previous_message_name, slot=None,
                                                           fingerprint=e.previous_fingerprint):
            return "interrupted_old", sid
        return "interrupted_unknown", sid
    if e.mode == "running" and not e.confirmed and not e.rejected:
        if on and matches_expected(running, name=e.message_name, slot=e.slot, fingerprint=e.fingerprint):
            return "interrupted_new", sid
        return "interrupted_unknown", sid
    return "interrupted_unknown", sid


def classify(facts: Facts, prev: PollStatus | None, expected: ExpectedState | None, lock: LockInfo | None,
             machine: Machine, line: Line, data: HxData, *, acked: dict[str, Ack] | None = None,
             standalone: bool = False, at: float = 0.0, timings: Timings = TIMINGS) -> Outcome:
    """Every condition of §3.4; the code by precedence (§3.5) and every alert need."""
    F = facts
    e = expected if expected is not None else ExpectedState(machine_id=machine.id or _NO_EXPECTED_ID)
    lk = lock
    mode = e.mode
    company = data.company
    printer = data.printers.get(machine.printer_id)
    printer_name = printer.name if printer is not None else machine.name
    trusted = F.reachable and F.problem == ""
    on = trusted and F.printing is True
    off_read = trusted and F.printing is False
    running = F.running
    failure = e.failure
    after_switch = bool(failure is not None and failure.after_switch)
    names_message = bool(e.message_name or e.fingerprint)

    acked_ids = set(acked or {})
    prev_active = []
    if prev is not None:
        acked_ids |= {a.id for a in prev.alerts if a.acked_by}
        prev_active = [a for a in prev.alerts if a.active]

    def prev_alert(kind, ref=None):
        for a in prev_active:
            if a.kind == kind and (ref is None or a.ref == ref):
                return a
        return None

    def is_acked(alert):
        return alert is not None and alert.id in acked_ids

    codes = set()
    needs = []

    def need(kind, *, ref="", values=None, text=None, colour=None):
        k = ALERT_KINDS[kind]
        if any(n.kind == kind and n.ref == ref for n in needs):
            return
        msg = Msg(text or k.text, dict(values or {"machine": machine.name}))
        needs.append(AlertNeed(kind=kind, ref=ref, colour=k.colour if colour is None else colour, msg=msg,
                               roles=k.roles))

    def carry(kind):
        # §3.7: while the facts can't be trusted an alert that needs a fresh reading stays, unless acknowledged
        for a in prev_active:
            if a.kind == kind and not is_acked(a) and not any(n.kind == kind and n.ref == a.ref for n in needs):
                needs.append(AlertNeed(kind=kind, ref=a.ref, colour=a.colour, msg=a.msg, roles=ALERT_KINDS[kind].roles))

    # C1, C2: another printer, or a refused login
    if F.reachable and F.problem == "wrong_printer":
        codes.add("wrong_printer")
        need("setup_problem", text=SETUP_TEXTS["wrong_printer"])
    if F.reachable and F.problem == "login_failed":
        codes.add("login_failed")
        need("setup_problem", text=SETUP_TEXTS["login_failed"])

    # C3, C3b: a test whose PC stopped
    if not standalone and ((lk is not None and lk.purpose in ("test", "calibration")) or mode == "test_running"):
        ref = lk.run_id if lk is not None and lk.purpose in ("test", "calibration") else \
            str((e.test or {}).get("run_id") or "")
        still = prev_alert("test_still_printing", ref)
        if on or (not trusted and prev is not None and prev.printing is True and not is_acked(still)):
            codes.add("test_orphaned")
            need("test_still_printing", ref=ref)
        else:
            codes.add("test_interrupted")

    # C4: an interrupted Send
    interrupted = None
    if not standalone:
        interrupted, sid = _interrupted(e, lk, on=on, trusted=trusted, running=running)
        if interrupted is not None:
            codes.add(interrupted)
            computer = lk.computer if lk is not None and lk.purpose == "send" else e.computer
            need("send_interrupted", ref=sid, values={"machine": machine.name, "computer": computer},
                 colour=STATE_CODES[interrupted])

    # C5: a Stop that couldn't switch printing off
    if mode == "stop_requested_at_printer":
        if on:
            codes.add("stop_failed")
            need("stop_failed")
        elif not trusted or F.printing is None:
            codes.add("stop_requested")
            need("stop_failed")

    # C6: a failed Send
    if failure is not None:
        codes.add("left_red" if e.left_by else ("failed_after_switch" if after_switch else "failed_before_switch"))
        need("send_failed", ref=e.send_id)

    # C7, C7b: the sender said something's wrong
    if e.rejected and failure is None:
        if mode == "running":
            codes.add("rejected")
        need("send_rejected", ref=e.send_id, values={"machine": machine.name, "user": _sender(e)})

    # C8: printing switched on while Stripes expects it off
    if trusted:
        if on and mode in ("stopped_by_user", "test_finished"):
            codes.add("printing_on_unexpected")
            need("printing_on")
    else:
        carry("printing_on")

    # C9: went silent while printing bags
    silent_alert = prev_alert("went_silent")
    silent = False
    if not F.reachable and not is_acked(silent_alert):
        if silent_alert is not None or (prev is not None and prev.reachable and was_printing(prev, company)):
            silent = True
            codes.add("unreachable_printing")
            need("went_silent")

    # C10, C10b: printing read off while it should be on
    if trusted:
        if off_read and mode == "running" and not e.rejected and not after_switch:
            seen_on = prev is not None and prev.reachable and prev.printing is True and prev.problem == ""
            if prev_alert("printing_off") is not None or seen_on:
                codes.add("printing_off")
                need("printing_off")
            elif failure is None:
                codes.add("not_printing")
    else:
        carry("printing_off")

    # C11: drift
    drift_e = mode == "running" and not e.rejected and not after_switch and names_message
    known = drift_e and known_drift(prev) and prev.expected_counter == e.counter
    if not standalone:
        if trusted:
            if (running is not None and drift_e
                    and not matches_expected(running, name=e.message_name, slot=e.slot, fingerprint=e.fingerprint)):
                codes.add("drift")
                need("drift")
            elif running is None and known:
                # R unknown (a required Load failed: a worker start, the share back, a paused tick): only a
                # Load can end a known drift. Its alert stays, the same id and acks (owner decision, 31 Oct);
                # C11b gives message_unchecked with the origin unknown, and the red alert keeps it red
                need("drift")
        else:
            carry("drift")

    # C11c (owner decision, 31 Oct): a drift known from the share stays in standalone mode. Standalone
    # checks never load the message, so they can't see a drift end, and its name alone (C12) can't tell a
    # same-name drift from E: only a share-mode judgement clears it. Same alert, same acks, R's origin.
    # A MSGNAM that no longer names R's message means something else was selected at the printer, and an
    # R that is unknown (a share-mode Load failed) names nothing: still never green, at best amber
    # unknown_while_down, with the drift alert open
    if standalone and known:
        if trusted:
            unknown = running is None or (F.message_name is not None
                                          and norm_name(F.message_name) != norm_name(running.name))
            codes.add("unknown_while_down" if unknown else "drift")
        need("drift")

    # C11d (owner decision, 31 Oct): share mode's message check had failed when the share went down
    # (F.running_unchecked, carried from the seed, §6.12 item 3; standalone never computes it). Standalone
    # checks never load the message, so they can't do better: at best amber unknown_while_down, the origin
    # unknown, its warning kept
    if standalone and F.running_unchecked:
        if trusted and mode == "running" and not e.rejected:
            codes.add("unknown_while_down")
        if prev_alert("message_unreadable") is not None:
            need("message_unreadable")

    # C11b: the running message can't be read
    if not standalone:
        if trusted:
            if mode in ("running", "switch_pending") and not e.rejected and (running is None or F.running_unchecked):
                codes.add("message_unchecked")
                if F.running_unchecked:
                    need("message_unreadable")
        else:
            carry("message_unreadable")

    # C12: standalone, and the message on record can't be checked. Also while printing hasn't been
    # read since the outage began (a busy or nonsense PRIN at every check), and when this check read
    # no message name (a busy or nonsense MSGNAM; facts_from keeps none from an earlier check):
    # standalone checks never load the message, so without those readings nothing stands behind
    # green "Printing" and E's origin
    if standalone:
        name_differs = (names_message and trusted and F.message_name is not None
                        and norm_name(F.message_name) != norm_name(e.message_name))
        name_unread = names_message and trusted and F.message_name is None
        unread = trusted and mode == "running" and failure is None and not e.rejected and F.printing is None
        if name_differs or name_unread or unread or mode in ("switch_pending", "test_running"):
            codes.add("unknown_while_down")

    # C13 to C15: ink
    if trusted:
        if on and F.ink_level == INK_NONE:
            codes.add("ink_out")
            need("ink_out")
        if on and F.ink_level == INK_NOT_DETECTED:
            codes.add("cartridge_error")
            need("cartridge_error")
        level = F.ink_level
        if level in (INK_LOW, INK_CRITICAL) or (level in (INK_NOT_DETECTED, INK_NONE) and not on):
            need("low_ink", text=LOW_INK_TEXTS[level])
    else:
        carry("ink_out")
        carry("cartridge_error")
        carry("low_ink")

    # C16: rejected and stopped
    stopped_off = mode == "stopped_by_user" or (mode == "stop_requested_at_printer" and off_read)
    if e.rejected and not on and stopped_off:
        codes.add("stopped_needs_send")

    # C17: stopped after a test
    if not on and (mode == "test_finished" or (e.stop_reason == "test" and stopped_off)):
        codes.add("test_finished")

    # C18: off
    if not F.reachable and not silent:
        codes.add("off")

    # C19: running
    if mode == "running" and failure is None and not e.rejected and not off_read and (running is not None or standalone):
        if not e.confirmed:
            codes.add("unconfirmed")
        elif e.notes:
            codes.add("confirmed_with_notes")
        else:
            codes.add("running_confirmed")

    # C20: stopped by a user
    if not e.rejected and e.stop_reason != "test" and not on and stopped_off:
        codes.add("stopped")

    # C21: never sent from Stripes
    if mode == "none":
        if on:
            codes.add("not_from_stripes")
            need("not_from_stripes")
        else:
            codes.add("idle")
    if not trusted:
        carry("not_from_stripes")

    # C22: the printer clock
    clock_alert = False
    if trusted:
        if _clock_off(F.clock_offset_s, timings):
            minutes = max(2, int(round(abs(F.clock_offset_s) / 60.0)))
            need("clock_off", values={"machine": machine.name, "minutes": minutes})
            clock_alert = True
    else:
        carry("clock_off")

    # C23: something else is talking to the printer
    if F.foreign_client:
        need("foreign_client", values={"printer": printer_name})

    # C24, C25: the daily check
    if _encoder_changed(machine, F.daily):
        need("encoder_changed")
    if _fonts_differ(F.daily, company):
        need("fonts_differ", values={"printer": printer_name})

    code = next((c for c in PRECEDENCE if c in codes), "internal_error")
    values, details = _headline(code, F, prev, e, expected, lk, at=at, clock_alert=clock_alert)
    return Outcome(code=code, values=values, details=details, alerts=needs)


def _run_lock(code, lk):
    """The stale lock of the run an interrupted code is about: a test's for a test, a Send's for a Send."""
    if lk is None:
        return None
    if code == "test_interrupted":
        return lk if lk.purpose in ("test", "calibration") else None
    return lk if lk.purpose == "send" else None


def _code_values(code, e, lk):
    """The headline values of a judged code (§3.9)."""
    if code == "stopped":
        return {"user": e.stopped_by or e.written_by}
    if code == "left_red":
        return {"user": e.left_by}
    if code == "rejected":
        return {"user": _sender(e)}
    if code in ("test_interrupted", "interrupted_old", "interrupted_unknown"):
        run = _run_lock(code, lk)
        return {"computer": run.computer if run is not None else e.computer}
    return {}


def _headline(code, F, prev, e, expected, lk, *, at, clock_alert):
    values = _code_values(code, e, lk)
    details = []
    run = _run_lock(code, lk)
    started_by = (run.user if run is not None else "") or _sender(e)
    if code in ("test_interrupted", "interrupted_old", "interrupted_unknown") and started_by:
        details.append(Msg(T_STARTED_BY, {"user": started_by}))
    if code == "test_finished":
        by = _test_by(e) or _sender(e)
        if by:
            details.append(Msg(T_AFTER_TEST, {"user": by}))
    if code in ("failed_before_switch", "failed_after_switch", "left_red") and e.failure is not None:
        details.append(e.failure.msg)
    if code in ("running_confirmed", "confirmed_with_notes") and e.confirmed_by:
        details.append(Msg(T_CONFIRMED_BY, {"user": e.confirmed_by}))
    if code == "confirmed_with_notes":
        for note in e.notes:
            if note in hx_message.NOTE_TEXTS:
                details.append(hx_message.NOTE_TEXTS[note])
    if code == "unconfirmed" or (code == "interrupted_new" and e.mode == "running"):
        if _sender(e):
            details.append(Msg(T_WAITING_CONFIRM, {"user": _sender(e)}))
    if code == "interrupted_new" and e.mode == "switch_pending" and started_by:
        details.append(Msg(T_STARTED_NEEDS_SEND, {"user": started_by}))
    if code == "message_unchecked":
        ok_at = prev.running_ok_at if prev is not None else None
        if ok_at is not None:
            details.append(Msg(T_LAST_READ, {"minutes": max(1, _minutes(at, ok_at))}))
        else:
            details.append(Msg(T_NOT_READ_YET))
    if code in ("off", "unreachable_printing") and prev is not None:
        minutes = _minutes(at, prev.unreachable_since)
        if minutes is not None and minutes >= 1:
            details.append(Msg(T_NOT_ANSWERING, {"minutes": minutes}))
    if lk is not None and lk.purpose in T_UNFINISHED:
        details.append(Msg(T_UNFINISHED[lk.purpose], {"computer": lk.computer}))
    if F.running is not None and norm_name(F.running.name):
        details.append(Msg(T_MESSAGE_IN_SLOT, {"name": norm_name(F.running.name), "slot": F.running.slot}))
    if clock_alert and F.clock_source == "pc":
        details.append(Msg(T_PC_CLOCK))
    return values, details


def _snapshot(outcome, since):
    return MachineSnapshot(code=outcome.code, colour=STATE_CODES[outcome.code],
                           headline=Msg(STATE_TEXTS[outcome.code], dict(outcome.values)), since=since,
                           detail=list(outcome.details))


def expected_code(expected: ExpectedState) -> str | None:
    """The code E alone gives, before the poller has judged it (§5.1); None when only the poller can tell."""
    e = expected
    if e.failure is not None:
        return "left_red" if e.left_by else ("failed_after_switch" if e.failure.after_switch else "failed_before_switch")
    if e.rejected:
        if e.mode == "running":
            return "rejected"
        if e.mode == "stopped_by_user":
            return "stopped_needs_send"
        if e.mode == "stop_requested_at_printer":
            return "stop_requested"
    if e.mode == "running":
        if not e.confirmed:
            return "unconfirmed"
        return "confirmed_with_notes" if e.notes else "running_confirmed"
    if e.mode == "stopped_by_user":
        return "test_finished" if e.stop_reason == "test" else "stopped"
    if e.mode == "stop_requested_at_printer":
        return "stop_requested"
    if e.mode == "test_finished":
        return "test_finished"
    return None


# --------------------------------------------------------------------------
# judge (§3.8, §4.2)
# --------------------------------------------------------------------------

def _prev_key(prev):
    if prev is None:
        return "", frozenset()
    return (prev.state.code if prev.state is not None else "",
            frozenset(f"{a.kind}:{a.ref}" for a in prev.alerts if a.active))


def _with_ack(alert, acked):
    ack = acked.get(alert.id)
    if ack is not None and not alert.acked_by:
        return dataclasses.replace(alert, acked_by=ack.by, acked_at=ack.at)
    return alert


def _keep_facts(prev, new):
    kept = prev_facts(prev)
    for name in _DISPLAY:
        setattr(kept, name, getattr(new, name))
    return kept


def _alerts(prev, outcome, reading, expected, lock, machine, line, acked):
    """§4.2: continue, start and end alerts from the outcome's needs."""
    prev_active = {}
    if prev is not None:
        for a in prev.alerts:
            if a.active:
                prev_active.setdefault(f"{a.kind}:{a.ref}", a)
    alerts, started, wanted = [], [], set()
    sender_e = _sender(expected)
    last_sender = expected.sender if expected is not None else ""
    for n in outcome.alerts:
        k = f"{n.kind}:{n.ref}"
        wanted.add(k)
        old = prev_active.get(k)
        if old is not None:
            alerts.append(_with_ack(dataclasses.replace(old, msg=n.msg, colour=n.colour), acked))
            continue
        kind = ALERT_KINDS[n.kind]
        send_lock = lock is not None and lock.purpose == "send" and lock.user
        sender = lock.user if n.kind == "send_interrupted" and send_lock else sender_e
        new = Alert(id=alert_id(machine.id, n.kind, ref=n.ref, at=reading.at),
                    machine_id=machine.id, line_id=machine.line_id, kind=n.kind, sound=kind.sound, msg=n.msg,
                    started_at=reading.at,
                    recipients=resolve_recipients(n.roles, sender=sender, notify=line.notify_user,
                                                  last_sender=last_sender),
                    active=True, colour=n.colour, ref=n.ref)
        new = _with_ack(new, acked)
        alerts.append(new)
        started.append(new)
    ended = [_with_ack(dataclasses.replace(a, active=False, ended_at=reading.at), acked)
             for k, a in prev_active.items() if k not in wanted]
    return alerts, started, ended


def judge(prev: PollStatus | None, reading: Reading, expected: ExpectedState | None, lock: LockInfo | None,
          machine: Machine, line: Line, data: HxData, timings: Timings = TIMINGS) -> Judgement:
    """The poller's only decision: the committed state, alerts and facts after this reading (§3.8)."""
    acked = dict(reading.acked or {})
    kw = dict(acked=acked, standalone=reading.standalone, at=reading.at, timings=timings)
    f_new = facts_from(prev, reading)
    out_new = classify(f_new, prev, expected, lock, machine, line, data, **kw)
    new_key = out_new.key()
    commit, recheck = False, []
    if new_key == _prev_key(prev):
        commit = True
    else:
        f_old = prev_facts(prev)
        out_old = classify(f_old, prev, expected, lock, machine, line, data, **kw)
        differ = _judged_differences(f_old, f_new)
        if out_old.key() == new_key:
            commit = True                       # E, a lock or an ack caused it
        elif differ and differ <= {"foreign_client", "running_unchecked"}:
            commit = True                       # each already sums many requests
        elif not reading.second:
            recheck = evidence(f_old, f_new, timings)
            if reading.standalone and (f_new.message_name is None
                                       or norm_name(f_old.message_name) != norm_name(f_new.message_name)):
                recheck.append("MSGNAM")        # standalone checks judge the message by its name (C12)
            if not recheck:
                commit = True
        elif (reading.pending_code, frozenset(reading.pending_alerts)) == new_key:
            commit = True

    read_now = (reading.message_name is not None and reading.running is None and f_new.running is not None
                and norm_name(reading.message_name) != norm_name(f_new.running.name))
    prev_state = prev.state if prev is not None else None
    if commit:
        since = prev_state.since if prev_state is not None and prev_state.code == out_new.code else reading.at
        alerts, started, ended = _alerts(prev, out_new, reading, expected, lock, machine, line, acked)
        return Judgement(state=_snapshot(out_new, since), alerts=alerts, read_running_now=read_now, facts=f_new,
                         changed=(prev_state.code if prev_state is not None else "") != out_new.code,
                         started=started, ended=ended, committed=True)
    alerts = [_with_ack(a, acked) for a in (prev.alerts if prev is not None else []) if a.active]
    judgement = Judgement(state=prev_state, alerts=alerts, read_running_now=read_now,
                          facts=_keep_facts(prev, f_new), committed=False)
    if recheck:
        judgement.recheck = recheck
        judgement.candidate = _snapshot(out_new, reading.at)
        judgement.candidate_alerts = tuple(sorted(new_key[1]))
    return judgement


# --------------------------------------------------------------------------
# Views (§5)
# --------------------------------------------------------------------------

def _fresh(lock, age, timings):
    return lock is not None and age is not None and age < timings.lock_stale_s


def _monitored_limit(poll, timings):
    """How long a status counter may stand still before NOT MONITORED (§5.1 steps 1 and 2.4).

    A standalone check moves its status only every standalone_poll_s (§6.12 item 2), so with the share
    limit every on-time check would briefly show NOT MONITORED. A standalone status is NOT MONITORED
    once its next check is not_monitored_s late."""
    if poll is not None and poll.standalone:
        return timings.standalone_poll_s + timings.not_monitored_s
    return timings.not_monitored_s


def _lock_view(lock, expected):
    """A fresh lock's blue code, headline and test values (§5.1 step 2.1)."""
    user = {"user": lock.user, "computer": lock.computer}
    if lock.purpose == "send":
        code = "switch_pending" if expected is not None and expected.mode == "switch_pending" else "send_in_progress"
        return code, Msg(STATE_TEXTS[code], user), None
    if lock.purpose in ("test", "calibration"):
        note = lock.note if isinstance(lock.note, dict) else {}
        source = note if "step" in note else ((expected.test or {}) if expected is not None else {})
        step, of = source.get("step"), source.get("of")
        test = {"by": lock.user, "step": step, "of": of}
        if step is None:
            return "test_running", Msg(STATE_TEXTS["test_running:nostep"], {"user": lock.user}), test
        return ("test_running", Msg(STATE_TEXTS["test_running"], {"user": lock.user, "step": step, "of": of}),
                test)
    key = f"action_in_progress:{lock.purpose}" if lock.purpose in _ACTION_PURPOSES else "action_in_progress:other"
    return "action_in_progress", Msg(STATE_TEXTS[key], user), None


def _alert_order(a):
    return (not a.sound, a.started_at)


def machine_view(machine: Machine, poll: PollStatus | None, poll_age_s: float | None,
                 expected: ExpectedState | None, lock: LockInfo | None, lock_age_s: float | None,
                 acks: dict[str, Ack], timings: Timings = TIMINGS) -> MachineView:
    """P's judgement overlaid with a fresh lock, staleness, an unjudged E and acknowledgements (§5.1)."""
    acks = acks or {}
    limit = _monitored_limit(poll, timings)
    monitored = poll is not None and poll_age_s is not None and poll_age_s < limit
    headline = None
    from_poll = False
    test = None
    if _fresh(lock, lock_age_s, timings):
        code, headline, test = _lock_view(lock, expected)
    elif poll is not None and poll.state is not None and poll.state.code == "update_stripes":
        code = "update_stripes"
    elif poll is None:
        code = "checking" if poll_age_s is None or poll_age_s < timings.not_monitored_s else "not_monitored"
    elif poll_age_s is None or poll_age_s >= limit:
        # A poll status whose age this PC doesn't know yet is as good as unwatched
        code = "not_monitored" if poll_age_s is not None else "checking"
    elif (expected is not None and expected.counter != poll.expected_counter
          and expected_code(expected) is not None):
        code = expected_code(expected)
        headline = Msg(STATE_TEXTS[code], _code_values(code, expected, None))
    elif poll.state is not None:
        code = poll.state.code
        headline = poll.state.headline
        from_poll = True
    else:
        code = "checking"
    if code in STATE_CODES and STATE_CODES[code] == GREEN and _r_predates(poll, expected):
        # The poller hasn't judged E yet and R isn't E's message (a Send just made, confirmed before the
        # poller's first Load commits): green needs a Load since E changed (§15 decision 36), so amber
        code = "confirmed_with_notes" if expected.confirmed else "unconfirmed"
        headline, from_poll = None, False
    if code not in STATE_CODES:
        code = "update_stripes"         # a code from a newer Stripes
        headline, from_poll = None, False
    if headline is None:
        headline = Msg(STATE_TEXTS[code])

    # Colour (§5.2)
    colour = STATE_CODES[code]
    if code == "not_monitored":
        known = [poll.state.colour] if poll is not None and poll.state is not None else []
        if expected is not None and expected_code(expected) is not None:
            known.append(STATE_CODES[expected_code(expected)])
        last = worst(known) if known else GREY
        colour = last if last in (RED, AMBER) else GREY

    alerts = []
    if poll is not None and code not in ("update_stripes",):
        for a in poll.alerts:
            if a.active:
                alerts.append(_with_ack(a, acks))
    details = []
    replaced = None
    red = sorted((a for a in alerts if a.colour == RED), key=_alert_order)
    amber = sorted((a for a in alerts if a.colour == AMBER), key=_alert_order)
    if red and colour != RED:
        colour, replaced = RED, red[0]
    elif amber and colour in (GREEN, GREY):
        colour, replaced = AMBER, amber[0]
    if replaced is not None:
        details.append(headline)
        headline = replaced.msg
    if from_poll:
        details.extend(poll.state.detail)
    for a in alerts:
        if a is not replaced:
            details.append(a.msg)
    for a in alerts:
        for name in a.waiting:
            details.append(Msg(T_WAITING_SIGN_IN, {"user": name}))

    blue = code in BLUE_CODES
    e = expected
    reachable = poll is not None and poll.reachable
    can_stop = not blue and monitored and reachable and poll.printing is True
    can_resume = (not blue and monitored and reachable and poll.printing is False
                  and code in ("stopped", "not_printing", "printing_off")
                  and e is not None and bool(e.send_id) and e.confirmed and not e.rejected
                  and not e.resume_blocked and e.failure is None)
    needs_send = code in NEEDS_SEND_CODES or (code == "interrupted_new" and e is not None
                                              and e.mode == "switch_pending")
    confirm_by = confirm_id = ""
    if (not blue and code not in ("update_stripes", "not_set_up") and e is not None and e.mode == "running"
            and not e.confirmed and not e.rejected and e.failure is None and e.send_id):
        confirm_by, confirm_id = _sender(e), e.send_id
    if test is None and code in ("test_orphaned", "test_interrupted", "test_finished") and e is not None \
            and isinstance(e.test, dict):
        test = {"by": e.test.get("by", ""), "step": e.test.get("step"), "of": e.test.get("of")}

    running = poll.running if poll is not None else None
    if running is not None and norm_name(running.name):
        message_name = norm_name(running.name)
    else:
        message_name = norm_name(poll.message_name) if poll is not None else ""
    # "Checking...": nothing judged yet since a long gap (§6.3 step 1), or a status of unknown age. What
    # P last said prints isn't shown as printing (owner-approved divergence, 31 Oct)
    checking = code == "checking"
    return MachineView(machine_id=machine.id, line_id=machine.line_id, name=machine.name, colour=colour, code=code,
                       headline=headline, details=details,
                       origin_line="" if checking else effective_origin(poll, expected),
                       message_name=message_name, checked_age_s=poll_age_s, monitored=monitored,
                       printing=poll.printing if monitored and not checking else None, alerts=alerts,
                       unacked_sound=any(a.sound and not a.acked_by for a in alerts),
                       can_stop=can_stop, can_resume=can_resume, needs_send=needs_send,
                       confirm_pending_by=confirm_by, confirm_send_id=confirm_id, test=test,
                       has_preview=running is not None and bool(running.preview_sha256))


def _r_predates(poll: PollStatus | None, expected: ExpectedState | None) -> bool:
    """E changed since the poller's last judgement (E.counter != P.expected_counter) and P's running
    message isn't E's: R was read before E was written, so it may not be what prints now (§5.1 step 2.5)."""
    return (poll is not None and expected is not None and expected.counter != poll.expected_counter
            and not matches_expected(poll.running, name=expected.message_name, slot=expected.slot,
                                     fingerprint=expected.fingerprint))


def effective_origin(poll: PollStatus | None, expected: ExpectedState | None) -> str:
    """The origin line to show as printing (§5.3); "" when it isn't known."""
    if poll is not None and (poll.running_unchecked
                             or (poll.state is not None and poll.state.code == "message_unchecked")):
        return ""
    if poll is not None and not poll.standalone and poll.state is not None and poll.state.code == "unknown_while_down":
        return ""                              # a standalone check's finding carried into share mode (§6.3 step 1)
    if poll is not None and poll.running is None and known_drift(poll):
        return ""                              # a known drift whose R is unknown (a Load failed), never E's origin
    if (poll is None or poll.running is None) and expected is not None and expected.mode == "switch_pending":
        return ""
    r = poll.running if poll is not None else None
    if r is not None and _r_predates(poll, expected):
        return ""                              # R predates an E the poller hasn't judged yet
    if r is not None and poll.standalone and (poll.message_name is None
                                              or norm_name(poll.message_name) != norm_name(r.name)):
        return ""                              # a drift's R carried into standalone (C11c), and MSGNAM isn't R's
    if (r is not None and expected is not None and expected.fingerprint != "" and r.origin_line == ""
            and matches_expected(r, name=expected.message_name, slot=expected.slot,
                                 fingerprint=expected.fingerprint)):
        return expected.origin_line            # pre-printed: the fingerprint proves it is the content sent
    if r is not None:
        return r.origin_line
    if (expected is not None and poll is not None and poll.message_name is not None
            and norm_name(poll.message_name) != norm_name(expected.message_name)):
        return ""
    if (expected is not None and poll is not None and poll.standalone and poll.reachable and poll.problem == ""
            and poll.message_name is None):
        return ""                              # a standalone check that couldn't read the message name
    return expected.origin_line if expected is not None else ""


def line_view(line: Line, machines: list[MachineView], status: LineStatus | None) -> LineView:
    """The line's colour is its worst machine's; status (the last Send) never changes a colour (§5.4)."""
    machines = list(machines)
    origins = {m.origin_line for m in machines if m.printing is True}
    differ = len(origins) > 1
    base = worst([m.colour for m in machines]) if machines else GREY
    colour, raised = base, False
    if differ and colour == GREEN:
        colour, raised = AMBER, True
    ages = [m.checked_age_s for m in machines if m.checked_age_s is not None]
    if not machines:
        headline = Msg(T_NO_MACHINES)
    elif not any(m.monitored for m in machines):
        headline = Msg(STATE_TEXTS["not_monitored"])
    elif len({m.code for m in machines}) == 1 and not raised:
        headline = machines[0].headline
    elif raised:
        headline = Msg(T_ORIGINS_DIFFER)
    else:
        m = next(m for m in machines if m.colour == base)
        headline = Msg(T_MACHINE_STATE, {"machine": m.name, "state": m.headline})
    return LineView(line_id=line.id, name=line.name, colour=colour, headline=headline, machines=machines,
                    origins_differ=differ, monitored=bool(machines) and all(m.monitored for m in machines),
                    checked_age_s=max(ages) if ages else None)


def _not_set_up(machine, data):
    printer = data.printers.get(machine.printer_id)
    return printer is None or not printer.active or printer.token == ""


def board_view(data: HxData, polls: dict[str, PollStatus], poll_ages: dict[str, float],
               expected: dict[str, ExpectedState], locks: dict[str, LockInfo], lock_ages: dict[str, float],
               lines: dict[str, LineStatus], acks: dict[str, Ack], *, share_ok: bool, standalone: bool, now: float,
               timings: Timings = TIMINGS) -> BoardView:
    """Every active line by name, its active machines by name (§5.5)."""
    out = []
    for line in sorted((ln for ln in data.lines.values() if ln.active), key=lambda ln: (natural_key(ln.name), ln.id)):
        views = []
        for m in sorted(data.line_machines(line.id), key=lambda m: (natural_key(m.name), m.id)):
            if _not_set_up(m, data):
                views.append(MachineView(machine_id=m.id, line_id=line.id, name=m.name, colour=GREY, code="not_set_up",
                                         headline=Msg(STATE_TEXTS["not_set_up"])))
                continue
            views.append(machine_view(m, polls.get(m.id), poll_ages.get(m.id), expected.get(m.id),
                                      locks.get(m.printer_id), lock_ages.get(m.printer_id), acks, timings))
        out.append(line_view(line, views, lines.get(line.id)))
    return BoardView(lines=out, share_ok=share_ok, standalone=standalone, next_check_s=None, generated_at=now)


def floor_board_rows(board: BoardView) -> list[FloorRow]:
    """One row per machine in the board's fixed order (§5.6)."""
    rows = []
    for line in board.lines:
        for m in line.machines:
            colour, status = m.colour, m.headline
            if line.origins_differ and m.printing is True and colour == GREEN:
                colour, status = AMBER, Msg(T_ORIGINS_DIFFER)
            rows.append(FloorRow(line_id=line.line_id, machine_id=m.machine_id, colour=colour, line_name=line.name,
                                 machine_name=m.name, origin_line=m.origin_line, status=status,
                                 checked_age_s=m.checked_age_s))
    return rows


def sounding_alerts(board: BoardView, identity: Identity, *, floor_board: bool) -> list[Alert]:
    """Unacknowledged sound alerts this PC should ring for, oldest first (§4.4)."""
    found = {}
    if board is None:
        return []
    for line in board.lines:
        for m in line.machines:
            for a in m.alerts:
                if not a.active or not a.sound or a.acked_by or a.id in found:
                    continue
                mine = any(r.startswith("user:") and same_user(r[5:], identity.user) for r in a.recipients)
                if mine or (floor_board and "floor" in a.recipients):
                    found[a.id] = a
    return sorted(found.values(), key=lambda a: (a.started_at, a.id))


def checked_text(age_s: float | None) -> Msg:
    if age_s is None:
        return Msg(T_NOT_CHECKED)
    age_s = max(0.0, age_s)
    if age_s < 120:
        return Msg(T_CHECKED_S, {"seconds": int(age_s)})
    return Msg(T_CHECKED_MIN, {"minutes": int(age_s // 60)})


def board_banner(board: BoardView) -> Msg | None:
    if board.standalone:
        return Msg(T_BANNER_STANDALONE, {"seconds": int(board.next_check_s or 0)})
    if not board.share_ok:
        return Msg(T_BANNER_DOWN)
    return None
