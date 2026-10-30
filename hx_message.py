"""MapleJet messages: what a Send puts on a bag, and how the printer's copy is checked.

Pure: no files, network, threads, Windows or Tk, and no clock ("today" comes from the
arguments). The parts, in the order a Send uses them:

- Origin wording: the only text that may name a country is the origin phrase
  ("Product of USA and Canada"); every other bag text is checked for origin words.
- Validation of the records and of a sender's choice, and merge_plan, which turns a
  SendRequest into one MachinePlan per machine.
- Print position (encoder machines, in mm, with the wrap) and delay (in microseconds).
- The message: its items and print settings (design_parts), its content code and name,
  and the Autopilot commands that build it in an idle slot.
- The read-back: compare, the rendered dates, the fingerprint the poller watches.
- The printer clock command, test print steps and the calibration maths.

Bag text is English only and never goes through tr(). Text a sender typed (free text, a
lot) is never put into a Msg.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import unicodedata
from datetime import date, datetime, timedelta

from hx_core import Msg
from hx_model import (AMBER, BOLD_RANGE, CANVAS, CARTRIDGES, CODE_MAX, DEFAULT_IDLE_SLOT, DELAY_US_MAX, DLYMM_MAX,
                      FILM_STATUSES, FONT_INDEXES, GREEN, INK_COLOURS, ITEM_KINDS, PLACEMENTS, RESOLUTIONS,
                      SIZE_RANGE, SLOTS, STANDARD_FONTS, STEP_BUILD, STEP_CHECK_CHOICE, STEP_CHECK_MACHINE,
                      STEP_CONFIRM_1, STEP_CONFIRM_2, STEP_LOCK, STEP_OPERATOR, STEP_SET_CLOCK, STEP_SWITCH,
                      STEP_VERIFY_IDLE, STEP_WAIT_POLLER, WIDTH_RANGE, BuiltMessage, Command, DesignItem,
                      Difference, MachinePlan, MessageName, NotAllowed, PlanError, PositionWorking, PrintSettings,
                      SendPlan, TestPlan, TestStep, new_id)
from i18n import N_, add_spanish

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

ORIGIN_PREFIX = "Product of "                     # the origin item always starts with this (plan)
ORIGIN_WORDS = ("Product of", "Grown in", "Origin", "Made in", "Hecho en")
BAG_TEXT_REFUSED = "<>"                           # never sent (OPEN: escaping)
TXT_MAX = 250                                     # CTEXT TXT (manual)
AFFIX_MAX = 100                                   # CTDAT PREF and POSF (manual)
PREPRINTED_ORIGIN_MAX = 40
COUNTRY_NAME_MAX = 30
MAX_ITEMS = 99                                    # OBJIDs 10001..10099 (a technical limit only)
FIRST_OBJID = 10001
GAP_MAX = 50                                      # PrintTextItem.gap, canvas units (OPEN)
OFFSET_MAX = {"days": 999, "months": 999, "years": 99}   # CTDAT DAY, MON, YEAR (manual)
FILM_LENGTH_MAX_MM = 5000.0
HEAD_DISTANCE_MAX_MM = 20000.0
SPEED_RANGE = range(1, 2501)                      # PRNSPD (OPEN: units)
CODE_PATTERN = r"[A-Z0-9]{1,3}"                   # line, machine and print-text codes (fullmatch)
FILM_CODE_PATTERN = r"[A-Z0-9]{1,6}"
TEST_CODE = "TST"                                 # the print-text code in test and calibration names
RESERVED_CODES = (TEST_CODE,)                     # print texts can't use it
CONTENT_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base 32
CHECK_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"        # 24 letters: no I, no O
SLOT_CHARS = {9: "9", 10: "0"}
NAME_PATTERN = r"([A-Z0-9]{1,6})-([A-Z0-9]{1,3})([0-9]{2})-([0-9A-HJKMNP-TV-Z]{4})-([A-HJ-NP-Z])([90])"
# What the printer gives a message for every print setting not sent, in the printer's order
PRINTER_DEFAULT_SETTINGS = (
    ("ROTAT", "0"), ("BOLD", "1"), ("WDTH", "1"), ("QUANT", "1"), ("DELAY", "0"), ("QDELAY", "0"),
    ("AUTREP", "0"), ("REVRS", "0"), ("DRAFT", "0"), ("MIRR", "0"), ("SLANT", "0"), ("ANG", "30"),
    ("SWDLY", "0"), ("SWSPD", "250"), ("WPDLY", "0"), ("PRNSPD", "1000"), ("ENCST", "0"), ("RESMOD", "0"),
    ("DLYMM", "0"), ("QDLYMM", "0"), ("ENCO", "0"), ("RESO", "640"), ("RESNAM", "UltraFine"),
    ("ENCSCL", "100"),
)
RESOLUTION_NAMES = {0: "UltraFine", 1: "Fine", 2: "Normal", 3: "Draft"}   # OPEN: spellings of 1..3
IGNORED_SETTINGS = ("RESO",)
ITEM_ZERO_FIELDS = ("ROTAT", "WCOEFF", "GROUP", "COLR", "RP")
DATE_ZERO_FIELDS = ("HOUR", "MIN", "SEC", "DWEEK")
BOOKKEEPING_FIELDS = ("BOUND", "LANG", "AMPM", "HORFLG", "OBJTYP")
ROLLOVER = (0, 0)                                 # TD DTRH, DTRM: the date changes at midnight (OPEN)
CLOCK_SET_TOLERANCE_S = 90.0                      # printer clock vs expected, right after a TD
DATE_SLACK_S = 120.0                              # rendered dates accepted around the read time
BOUND_MAX_X = 1024                                # an item's BOUND must end by this x (OPEN)
MAX_TEST_STEPS = 100
MAX_FONT_SAMPLES = 6
TEST_SAMPLE_GAP = 8                               # canvas units between font samples (OPEN)
SPACING_TOLERANCE_MM = 2.0                        # calibration spacing check (OPEN)
AMBER_NOTES = ("pc_time", "no_lock", "local_copy")

# Builder choice -> the printer's FRMT codes (each letter is a whole field)
DATE_FORMATS = {
    "MM/DD/YY": "M/D/y",
    "MM/DD/YYYY": "M/D/Y",
    "DD/MM/YY": "D/M/y",
    "DD/MM/YYYY": "D/M/Y",
    "YYYY-MM-DD": "Y-M-D",
    "MM-DD-YY": "M-D-y",
    "MMDDYY": "MDy",
    "MON DD YYYY": "m D Y",
    "DD MON YY": "D m y",
    "MON YYYY": "m Y",
    "MM/YY": "M/y",
}

_DATE_KINDS = ("packed_date", "best_by_date")
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_ITEM_ID = re.compile(r"i[0-9]{1,3}")
_FOUR_INTS = re.compile(r"(-?\d+),(-?\d+),(-?\d+),(-?\d+)")
_DELAY_MS_MAX = DELAY_US_MAX / 1000              # 120000 ms
_PRINTER_LINE = re.compile(r"[\r\n]")

# --------------------------------------------------------------------------
# Texts (send.md §19.1)
# --------------------------------------------------------------------------

ES = {
    "Only Stripes MapleJet Senders can send.": "Solo los miembros de Stripes MapleJet Senders pueden enviar.",
    "Only Stripes Administrators can run test prints.":
        "Solo los miembros de Stripes Administrators pueden hacer impresiones de prueba.",
    "Choose at least one line.": "Elija al menos una línea.",
    "{line} is in this Send twice.": "{line} está dos veces en este envío.",
    "A line in this Send no longer exists or isn't active.": "Una línea de este envío ya no existe o no está activa.",
    "Tick at least one machine on {line}.": "Marque al menos una máquina en {line}.",
    "{machine} isn't an active machine on {line}.": "{machine} no es una máquina activa de {line}.",
    "{machine} is in this Send twice.": "{machine} está dos veces en este envío.",
    "{machine} and {other} use the same printer.": "{machine} y {other} usan la misma impresora.",
    "Choose a product for {line}.": "Elija un producto para {line}.",
    "The product chosen for {line} isn't active.": "El producto elegido para {line} no está activo.",
    "Choose a film for {line}.": "Elija una película para {line}.",
    "Film {film} can't be sent to {line}: it must be active, belong to the product and use the line's "
    "cartridge type.": "La película {film} no se puede enviar a {line}: debe estar activa, ser del producto y "
                       "usar el tipo de cartucho de la línea.",
    "Choose a print text for {line}.": "Elija un texto de impresión para {line}.",
    "Print text {print_text} can't be used with film {film}.":
        "El texto de impresión {print_text} no se puede usar con la película {film}.",
    "Enter the lot for {line}.": "Escriba el lote para {line}.",
    "The lot for {line} must be {min} to {max} digits.": "El lote para {line} debe tener de {min} a {max} dígitos.",
    "Choose at least one country of origin for {line}.": "Elija al menos un país de origen para {line}.",
    "{country} isn't on the country list.": "{country} no está en la lista de países.",
    "{country} is chosen twice.": "{country} está elegido dos veces.",
    'Fill in "{label}" for {line}.': 'Complete "{label}" para {line}.',
    '"{label}" can be at most {n} characters.': '"{label}" puede tener como máximo {n} caracteres.',
    "{where} contains words about origin: {words}. Origin goes only in the origin line.":
        "{where} contiene palabras de origen: {words}. El origen va solo en la línea de origen.",
    "{where} contains characters Stripes can't send to a printer: {codes}.":
        "{where} contiene caracteres que Stripes no puede enviar a una impresora: {codes}.",
    "{where} is too long: at most {n} characters.": "{where} es demasiado largo: como máximo {n} caracteres.",
    "The text before item {n}": "El texto antes del elemento {n}",
    "The text after item {n}": "El texto después del elemento {n}",
    "The fixed text of item {n}": "El texto fijo del elemento {n}",
    'The free text "{label}"': 'El texto libre "{label}"',
    "The code of {name}": "El código de {name}",
    "The pre-printed origin": "El origen preimpreso",
    "Country {name}": "País {name}",
    "Item {n}": "Elemento {n}",
    "The printer of {machine} isn't set up: it is missing, not active or has no saved password.":
        "La impresora de {machine} no está configurada: falta, no está activa o no tiene contraseña guardada.",
    "{machine} has no print speed. An administrator must set it.":
        "{machine} no tiene velocidad de impresión. Un administrador debe configurarla.",
    "{machine} isn't calibrated: it has no head distance.": "{machine} no está calibrada: no tiene distancia del cabezal.",
    "Film {film} has no length, which encoder machines need.":
        "La película {film} no tiene largo, y las máquinas con encoder lo necesitan.",
    "Film {film} has no print position, which encoder machines need.":
        "La película {film} no tiene posición de impresión, y las máquinas con encoder la necesitan.",
    "Film {film} has no delay, which delay machines need.":
        "La película {film} no tiene retardo, y las máquinas por retardo lo necesitan.",
    "The delay works out at {ms} ms, outside 0 to 120000 ms.":
        "El retardo resulta en {ms} ms, fuera del rango de 0 a 120000 ms.",
    "The print would still be running when the film stops ({end} of {length} mm), so the next bag would go "
    "out blank.": "La impresión seguiría en curso cuando la película se detenga ({end} de {length} mm), así que "
                  "la siguiente bolsa saldría en blanco.",
    "Print length unknown: Stripes can't check that the print ends before the film stops.":
        "Largo de impresión desconocido: Stripes no puede comprobar que la impresión termine antes de que la "
        "película se detenga.",
    "The name must be 1 to {n} characters.": "El nombre debe tener de 1 a {n} caracteres.",
    "The status isn't valid.": "El estado no es válido.",
    "The print text code must be 1 to 3 capital letters or digits.":
        "El código del texto de impresión debe tener de 1 a 3 letras mayúsculas o dígitos.",
    "TST is kept for test prints.": "TST está reservado para las impresiones de prueba.",
    "The print text has no items.": "El texto de impresión no tiene elementos.",
    "A print text can have at most {n} items.": "Un texto de impresión puede tener como máximo {n} elementos.",
    "Item ids must be unique.": "Los identificadores de los elementos deben ser únicos.",
    "Item {n} has an unknown kind.": "El elemento {n} es de un tipo desconocido.",
    "A print text can have only one origin item.": "Un texto de impresión solo puede tener un elemento de origen.",
    "Item {n} must be placed right of or below the previous item.":
        "El elemento {n} debe ir a la derecha o debajo del anterior.",
    "The gap before item {n} must be 0 to {max}.": "El espacio antes del elemento {n} debe ser de 0 a {max}.",
    "The origin item can't have text before or after it.": "El elemento de origen no puede tener texto antes ni después.",
    "Item {n} needs its fixed text.": "El elemento {n} necesita su texto fijo.",
    "Item {n} needs a date format.": "El elemento {n} necesita un formato de fecha.",
    "The packed-on date can't have an offset.": "La fecha de empaque no puede tener desfase.",
    "The best-by offset of item {n} must be 1 to 999 days.":
        "El desfase de consumo preferente del elemento {n} debe ser de 1 a 999 días.",
    "The best-by offset of item {n} must be 1 to 999 months.":
        "El desfase de consumo preferente del elemento {n} debe ser de 1 a 999 meses.",
    "The best-by offset of item {n} must be 1 to 99 years.":
        "El desfase de consumo preferente del elemento {n} debe ser de 1 a 99 años.",
    "Item {n} needs a question for the sender.": "El elemento {n} necesita una pregunta para el remitente.",
    "The free text of item {n} must allow 1 to {max} characters.":
        "El texto libre del elemento {n} debe permitir de 1 a {max} caracteres.",
    "The film code must be 1 to 6 capital letters or digits.":
        "El código de la película debe tener de 1 a 6 letras mayúsculas o dígitos.",
    "Another film already uses the code {code}.": "Otra película ya usa el código {code}.",
    "The film's product doesn't exist.": "El producto de la película no existe.",
    "The side, cartridge type or ink colour isn't valid.": "El lado, el tipo de cartucho o el color de tinta no es válido.",
    "The film length must be more than 0 and at most {max} mm.":
        "El largo de la película debe ser mayor que 0 y como máximo {max} mm.",
    "The print position must be between 0 and the film length.":
        "La posición de impresión debe estar entre 0 y el largo de la película.",
    "The delay must be 0 to 120000 ms.": "El retardo debe ser de 0 a 120000 ms.",
    "The print length must be more than 0 and at most the film length.":
        "El largo de impresión debe ser mayor que 0 y como máximo el largo de la película.",
    "Font, size, resolution, bold or width is out of range.":
        "La fuente, el tamaño, la resolución, la negrita o el ancho están fuera de rango.",
    "Enter the pre-printed origin.": "Escriba el origen preimpreso.",
    "A print text allowed with this film doesn't exist.": "Un texto de impresión permitido con esta película no existe.",
    "Print text {print_text} has an origin item, but this film's origin is pre-printed.":
        "El texto de impresión {print_text} tiene un elemento de origen, pero el origen de esta película está "
        "preimpreso.",
    "Print text {print_text} has no origin item, but this film needs one.":
        "El texto de impresión {print_text} no tiene elemento de origen, pero esta película lo necesita.",
    "An active film needs at least one print text and a default print text.":
        "Una película activa necesita al menos un texto de impresión y uno predeterminado.",
    "The default print text must be one of the film's print texts.":
        "El texto de impresión predeterminado debe ser uno de los de la película.",
    "The code must be 1 to 3 capital letters or digits.": "El código debe tener de 1 a 3 letras mayúsculas o dígitos.",
    "The cartridge type isn't valid.": "El tipo de cartucho no es válido.",
    "The machine's line doesn't exist.": "La línea de la máquina no existe.",
    "The machine's printer doesn't exist.": "La impresora de la máquina no existe.",
    "{printer} is already used by {machine}.": "{printer} ya la usa {machine}.",
    "The placement must be Encoder or Delay.": "La colocación debe ser Encoder o Retardo.",
    "The print speed must be 1 to 2500.": "La velocidad de impresión debe ser de 1 a 2500.",
    "The seconds per bag must be 0.5 to 60.": "Los segundos por bolsa deben ser de 0,5 a 60.",
    "The head distance must be more than 0 and at most {max} mm.":
        "La distancia del cabezal debe ser mayor que 0 y como máximo {max} mm.",
    "The encoder wheel must be 20 to 150 mm.": "La rueda del encoder debe medir de 20 a 150 mm.",
    "The pulses per turn must be 1 to 1000000.": "Los pulsos por vuelta deben ser de 1 a 1000000.",
    "Country names must be unique, not empty and at most {n} characters.":
        "Los nombres de país deben ser únicos, no estar vacíos y tener como máximo {n} caracteres.",
    "{name} is used by more than one country.": "{name} lo usa más de un país.",
    "Ink names must be unique, each with a cartridge type and a colour.":
        "Los nombres de tinta deben ser únicos, cada uno con tipo de cartucho y color.",
    "The lot rules must allow 1 to 20 digits, the minimum no more than the maximum.":
        "Las reglas de lote deben permitir de 1 a 20 dígitos, con el mínimo no mayor que el máximo.",
    "The free-text limit must be 1 to 100 characters.": "El límite de texto libre debe ser de 1 a 100 caracteres.",
    "The standard font list must have 6 fonts.": "La lista de fuentes estándar debe tener 6 fuentes.",
    "The bag counter must be PMCNT, MSCNT or none.": "El contador de bolsas debe ser PMCNT, MSCNT o none.",
    "Print text {name} now contains words about origin: {words}.":
        "El texto de impresión {name} ahora contiene palabras de origen: {words}.",
    "The code of {name} now contains words about origin: {words}.":
        "El código de {name} ahora contiene palabras de origen: {words}.",
    "The test must vary the print position on encoder machines and the delay on delay machines.":
        "La prueba debe variar la posición de impresión en máquinas con encoder y el retardo en máquinas por "
        "retardo.",
    "The test range isn't valid: check the start, stop and step.":
        "El rango de la prueba no es válido: revise el inicio, el final y el paso.",
    "The test would have {n} steps; at most {max} are allowed.":
        "La prueba tendría {n} pasos; se permiten como máximo {max}.",
    "A tested value is out of range.": "Un valor de la prueba está fuera de rango.",
    "At most {n} fonts can be printed side by side.": "Se pueden imprimir como máximo {n} fuentes lado a lado.",
    "Calibration is only for encoder machines.": "La calibración es solo para máquinas con encoder.",
    "Measure the rough head distance first.": "Mida primero la distancia aproximada del cabezal.",
    "This film is too short for the {mm} mm spacing check.":
        "Esta película es demasiado corta para la prueba de separación de {mm} mm.",
    "The tape measurement is too far from the print to be sure. Measure the head distance again.":
        "La medida con cinta está demasiado lejos de la impresión para estar seguros. Vuelva a medir la "
        "distancia del cabezal.",
    "No Send is recorded for {machine}. Send instead.": "No hay ningún envío registrado para {machine}. Haga un envío.",
    "{machine} is in the middle of a Send or a test.": "{machine} está en medio de un envío o de una prueba.",
    "The last Send to {machine} was rejected. Send again instead.":
        "El último envío a {machine} fue rechazado. Haga un envío nuevo.",
    "{machine} needs a Send after the test print.": "{machine} necesita un envío después de la impresión de prueba.",
    "{machine} needs a new Send before it can print again.":
        "{machine} necesita un envío nuevo antes de volver a imprimir.",
    "The last Send to {machine} wasn't confirmed. Send again instead.":
        "El último envío a {machine} no se confirmó. Haga un envío nuevo.",
    "The message on {machine} isn't the last confirmed Send. Send again instead.":
        "El mensaje en {machine} no es el último envío confirmado. Haga un envío nuevo.",
    # STEP_TEXTS
    "Checking the sender and the choice": "Revisando el remitente y la selección",
    "Counting down and locking the printer": "Cuenta atrás y bloqueo de la impresora",
    "Waiting for the monitoring PC to step aside": "Esperando a que la PC de monitoreo se aparte",
    "Checking the machine": "Revisando la máquina",
    "Setting the clock": "Ajustando el reloj",
    "Building the new message": "Creando el mensaje nuevo",
    "Checking the new message before it goes live": "Revisando el mensaje nuevo antes de activarlo",
    "Switching to the new message": "Cambiando al mensaje nuevo",
    "First check of the running message": "Primera revisión del mensaje en impresión",
    "Second check of the running message": "Segunda revisión del mensaje en impresión",
    "Waiting for the sender to confirm": "Esperando la confirmación del remitente",
    "Turning printing off": "Desactivando la impresión",
    "Turning printing on": "Activando la impresión",
    # NOTE_TEXTS
    "The time server didn't answer, so the printer clock was set from this PC's clock.":
        "El servidor de hora no respondió, así que el reloj de la impresora se ajustó con el reloj de esta PC.",
    "Sent and confirmed by the printer, but Stripes couldn't check whether anyone else was sending to this "
    "line. Check the first bags on each machine.":
        "Enviado y confirmado por la impresora, pero Stripes no pudo comprobar si alguien más estaba enviando a "
        "esta línea. Revise las primeras bolsas de cada máquina.",
    "Sent from this PC's copy of the templates, because the server couldn't be reached.":
        "Enviado con la copia de las plantillas de esta PC, porque no se pudo conectar con el servidor.",
    "The printer's ink doesn't suit this film.": "La tinta de la impresora no es adecuada para esta película.",
    "The print may still be running when the film stops.":
        "Puede que la impresión siga en curso cuando la película se detenga.",
    "Cancelled. Nothing was sent.": "Cancelado. No se envió nada.",
}
add_spanish(ES)

# note -> what it means (also used by hx_state and the result window)
NOTE_TEXTS = {
    "pc_time": Msg(N_("The time server didn't answer, so the printer clock was set from this PC's clock.")),
    "no_lock": Msg(N_("Sent and confirmed by the printer, but Stripes couldn't check whether anyone else was "
                      "sending to this line. Check the first bags on each machine.")),
    "local_copy": Msg(N_("Sent from this PC's copy of the templates, because the server couldn't be reached.")),
    "ink_mismatch": Msg(N_("The printer's ink doesn't suit this film.")),
    "film_stop": Msg(N_("The print may still be running when the film stops.")),
    "cancelled": Msg(N_("Cancelled. Nothing was sent.")),
}

# STEP_* and the action steps -> a short name
STEP_TEXTS = {
    STEP_CHECK_CHOICE: Msg(N_("Checking the sender and the choice")),
    STEP_LOCK: Msg(N_("Counting down and locking the printer")),
    STEP_WAIT_POLLER: Msg(N_("Waiting for the monitoring PC to step aside")),
    STEP_CHECK_MACHINE: Msg(N_("Checking the machine")),
    STEP_SET_CLOCK: Msg(N_("Setting the clock")),
    STEP_BUILD: Msg(N_("Building the new message")),
    STEP_VERIFY_IDLE: Msg(N_("Checking the new message before it goes live")),
    STEP_SWITCH: Msg(N_("Switching to the new message")),
    STEP_CONFIRM_1: Msg(N_("First check of the running message")),
    STEP_CONFIRM_2: Msg(N_("Second check of the running message")),
    STEP_OPERATOR: Msg(N_("Waiting for the sender to confirm")),
    "stop": Msg(N_("Turning printing off")),
    "resume": Msg(N_("Turning printing on")),
}

_BEST_BY_TEXTS = {
    "days": N_("The best-by offset of item {n} must be 1 to 999 days."),
    "months": N_("The best-by offset of item {n} must be 1 to 999 months."),
    "years": N_("The best-by offset of item {n} must be 1 to 99 years."),
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _num(x):
    """A number for people: one decimal without a trailing .0 (150.0 -> "150", -3.0 -> "-3")."""
    text = f"{x:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return "0" if text == "-0" else text


def _signed(a):
    # "+ 0", "+ 20", "- 3"
    return f"+ {_num(a)}" if a >= 0 else f"- {_num(-a)}"


def _film_name(film):
    # films have no name: people know them by code and artwork revision
    return f"{film.code} · {film.artwork_revision}" if film.artwork_revision else film.code


def _unique(msgs):
    out = []
    for m in msgs:
        if m not in out:
            out.append(m)
    return out


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _printer_ready(printer):
    return printer is not None and printer.active and printer.token != ""


def _origin_count(pt):
    return sum(1 for it in pt.items if it.kind == "origin")


def _origin_rule_ok(film, pt):
    # §2.4: pre-printed films take only print texts without origin, others exactly one origin item
    n = _origin_count(pt)
    return n == 0 if film.origin_preprinted else n == 1


def _local_date(epoch):
    t = time.localtime(epoch)
    return date(t.tm_year, t.tm_mon, t.tm_mday)


def _sorted(records):
    return sorted(records.values(), key=lambda r: (getattr(r, "name", "").casefold(), r.id))


# --------------------------------------------------------------------------
# Bag text and origin wording (§2)
# --------------------------------------------------------------------------

def bag_text_problems(text, where):
    """Characters a printer can't be sent: anything outside printable Latin-1, and < and >.
    The characters are named by their U+ codes, never shown themselves."""
    codes = []
    for c in text:
        o = ord(c)
        if (0x20 <= o <= 0x7E or 0xA0 <= o <= 0xFF) and c not in BAG_TEXT_REFUSED:
            continue
        code = f"U+{o:04X}"
        if code not in codes:
            codes.append(code)
    if not codes:
        return []
    return [Msg(N_("{where} contains characters Stripes can't send to a printer: {codes}."),
                {"where": where, "codes": ", ".join(codes)})]


def origin_phrase(countries):
    """"Product of USA", "Product of USA and Canada", "Product of USA, Canada, and Mexico"."""
    names = list(countries)
    if not names:
        raise ValueError("origin_phrase needs at least one country")
    if len(names) == 1:
        body = names[0]
    elif len(names) == 2:
        body = f"{names[0]} and {names[1]}"
    else:
        body = ", ".join(names[:-1]) + ", and " + names[-1]
    return ORIGIN_PREFIX + body


def origin_tokens(text):
    """(spaced, joined, compact): words split at punctuation, words with punctuation deleted, and
    the letters and digits only; accents dropped and case folded."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    spaced = re.findall(r"[^\W_]+", t)
    joined = re.sub(r"[^\w\s]|_", "", t).split()
    compact = re.sub(r"[\W_]+", "", t)
    return spaced, joined, compact


def _origin_terms(company):
    """ORIGIN_WORDS, then each country's name and aliases (inactive ones too), each once."""
    terms = list(ORIGIN_WORDS)
    for country in company.countries:
        terms.append(country.name)
        terms.extend(country.aliases)
    out = []
    for term in terms:
        if term not in out:
            out.append(term)
    return out


def _term_matches(term, text):
    ps, pj, pc = term
    ts, tj, tc = text
    if not pc:
        return False
    n = len(ps)
    # a: the term's words as a run of the text's words
    if any(ts[i:i + n] == ps for i in range(len(ts) - n + 1)):
        return True
    # b: punctuation removed, "U.S.A." is "usa"
    if pc in tj:
        return True
    # c: a phrase run together, "MadeIn"
    if n >= 2 and pc in tc:
        return True
    # d: spelled out in single letters, "U S A"
    if len(pc) >= 2:
        run = ""
        for word in ts + [""]:
            if len(word) == 1:
                run += word
                continue
            if len(run) >= 2 and pc in run:
                return True
            run = ""
    return False


def _words_in(texts, company):
    terms = _origin_terms(company)
    tokens = [origin_tokens(t) for t in texts if t]
    return [term for term in terms if any(_term_matches(origin_tokens(term), tok) for tok in tokens)]


def origin_words_in(text, company):
    """Origin words, country names and aliases found in text, ignoring case, accents and
    punctuation, in term order; [] when clean."""
    return _words_in([text], company)


def _origin_words_msg(where, words):
    return Msg(N_("{where} contains words about origin: {words}. Origin goes only in the origin line."),
               {"where": where, "words": ", ".join(words)})


def _too_long(where, n):
    return Msg(N_("{where} is too long: at most {n} characters."), {"where": where, "n": n})


def _text_problems(text, where, company):
    problems = bag_text_problems(text, where)
    words = origin_words_in(text, company)
    if words:
        problems.append(_origin_words_msg(where, words))
    return problems


def origin_change_text(old_origin_line, new_origin_line):
    """The Send window's highlight: "USA → USA and Canada"; "" when the origin doesn't change. Bag text."""
    def bare(s):
        s = s or ""
        return s[len(ORIGIN_PREFIX):] if s.startswith(ORIGIN_PREFIX) else s
    old, new = bare(old_origin_line), bare(new_origin_line)
    if old == new:
        return ""
    if not old:
        return new
    return f"{old} → {new}"


# --------------------------------------------------------------------------
# Dates (§3.2)
# --------------------------------------------------------------------------

def _days_in(year, month):
    first = date(year, month, 1)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (following - first).days


def _add_months(d, months):
    # the day is cut to the last day of the resulting month
    year, month0 = divmod(d.year * 12 + d.month - 1 + months, 12)
    month = month0 + 1
    return date(year, month, min(d.day, _days_in(year, month)))


def render_date(frmt, day, *, add_days=0, add_months=0, add_years=0):
    """What the printer should print for FRMT on day plus the offsets (years, then months, then days)."""
    d = date(day.year, day.month, day.day)
    d = _add_months(d, 12 * add_years)
    d = _add_months(d, add_months)
    d = d + timedelta(days=add_days)
    out = []
    for c in frmt:
        if c == "Y":
            out.append(f"{d.year:04d}")
        elif c == "y":
            out.append(f"{d.year % 100:02d}")
        elif c == "M":
            out.append(f"{d.month:02d}")
        elif c == "m":
            out.append(_MONTHS[d.month - 1])
        elif c == "D":
            out.append(f"{d.day:02d}")
        elif c in "/- .":
            out.append(c)
        else:
            raise ValueError(f"render_date: {c!r} isn't a date code Stripes uses")
    return "".join(out)


def _date_offsets(item):
    """(DAY, MON, YEAR) a date item sends; ValueError when out of range."""
    if item.kind == "packed_date":
        if item.offset != 0:
            raise ValueError("a packed-on date can't have an offset")
        return 0, 0, 0
    top = OFFSET_MAX.get(item.offset_unit)
    if top is None or not _is_int(item.offset) or not 1 <= item.offset <= top:
        raise ValueError("best-by offset out of range")
    if item.offset_unit == "days":
        return item.offset, 0, 0
    if item.offset_unit == "months":
        return 0, item.offset, 0
    return 0, 0, item.offset


# --------------------------------------------------------------------------
# Record validation (§3.6, §4.1)
# --------------------------------------------------------------------------

def _where_before(n):
    return Msg(N_("The text before item {n}"), {"n": n})


def _where_after(n):
    return Msg(N_("The text after item {n}"), {"n": n})


def _longest_txt(item, company):
    """The longest CTEXT TXT an item can make, or None for a date item."""
    affixes = len(item.before) + len(item.after)
    if item.kind == "fixed":
        return affixes + len(item.text)
    if item.kind == "lot":
        return affixes + company.lot_max_digits
    if item.kind == "free_text":
        return affixes + item.max_len
    if item.kind in ("machine_code", "line_code"):
        return affixes + CODE_MAX
    if item.kind == "origin":
        active = [c.name for c in company.countries if c.active]
        return affixes + len(origin_phrase(active)) if active else None
    return None


def validate_print_text(pt, company):
    """Every problem with a print text, in the §3.6 order; [] when it can be saved."""
    problems = []
    if not pt.name.strip() or len(pt.name) > 60:
        problems.append(Msg(N_("The name must be 1 to {n} characters."), {"n": 60}))
    if not re.fullmatch(CODE_PATTERN, pt.code):
        problems.append(Msg(N_("The print text code must be 1 to 3 capital letters or digits.")))
    elif pt.code in RESERVED_CODES:
        problems.append(Msg(N_("TST is kept for test prints.")))
    if pt.status not in FILM_STATUSES:
        problems.append(Msg(N_("The status isn't valid.")))

    items = pt.items
    if not items:
        problems.append(Msg(N_("The print text has no items.")))
    elif len(items) > MAX_ITEMS:
        problems.append(Msg(N_("A print text can have at most {n} items."), {"n": MAX_ITEMS}))
    ids = [it.id for it in items]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and _ITEM_ID.fullmatch(i) for i in ids):
        problems.append(Msg(N_("Item ids must be unique.")))
    for n, it in enumerate(items, start=1):
        if it.kind not in ITEM_KINDS:
            problems.append(Msg(N_("Item {n} has an unknown kind."), {"n": n}))
    if _origin_count(pt) > 1:
        problems.append(Msg(N_("A print text can have only one origin item.")))

    for n, it in enumerate(items, start=1):
        if it.placement not in ("right", "below"):
            problems.append(Msg(N_("Item {n} must be placed right of or below the previous item."), {"n": n}))
        if not _is_int(it.gap) or not 0 <= it.gap <= GAP_MAX:
            problems.append(Msg(N_("The gap before item {n} must be 0 to {max}."), {"n": n, "max": GAP_MAX}))
        problems += _text_problems(it.before, _where_before(n), company)
        problems += _text_problems(it.after, _where_after(n), company)
        if it.kind == "fixed":
            problems += _text_problems(it.text, Msg(N_("The fixed text of item {n}"), {"n": n}), company)
        if it.kind == "origin" and (it.before or it.after):
            problems.append(Msg(N_("The origin item can't have text before or after it.")))
        if it.kind == "fixed" and not it.text:
            problems.append(Msg(N_("Item {n} needs its fixed text."), {"n": n}))
        if it.kind in _DATE_KINDS:
            if it.date_format not in DATE_FORMATS:
                problems.append(Msg(N_("Item {n} needs a date format."), {"n": n}))
            if len(it.before) > AFFIX_MAX:
                problems.append(_too_long(_where_before(n), AFFIX_MAX))
            if len(it.after) > AFFIX_MAX:
                problems.append(_too_long(_where_after(n), AFFIX_MAX))
            if it.kind == "packed_date" and it.offset != 0:
                problems.append(Msg(N_("The packed-on date can't have an offset.")))
            if it.kind == "best_by_date":
                top = OFFSET_MAX.get(it.offset_unit)
                if top is None or not _is_int(it.offset) or not 1 <= it.offset <= top:
                    # an unknown unit gets the days text: the unit list starts with days
                    text = _BEST_BY_TEXTS.get(it.offset_unit, _BEST_BY_TEXTS["days"])
                    problems.append(Msg(text, {"n": n}))
        if it.kind == "free_text":
            if not it.label.strip():
                problems.append(Msg(N_("Item {n} needs a question for the sender."), {"n": n}))
            if not _is_int(it.max_len) or not 1 <= it.max_len <= company.free_text_max:
                problems.append(Msg(N_("The free text of item {n} must allow 1 to {max} characters."),
                                    {"n": n, "max": company.free_text_max}))
        longest = _longest_txt(it, company) if it.kind in ITEM_KINDS else None
        if longest is not None and longest > TXT_MAX:
            problems.append(_too_long(Msg(N_("Item {n}"), {"n": n}), TXT_MAX))
    return problems


def validate_company(company):
    """Every problem with the company settings; [] when they can be saved."""
    problems = []
    countries = company.countries
    terms_of = [{c.name.casefold()} | {a.casefold() for a in c.aliases} for c in countries]
    names_bad = False
    seen = set()
    for c in countries:
        key = c.name.casefold()
        if not c.name.strip() or len(c.name) > COUNTRY_NAME_MAX or key in seen:
            names_bad = True
        seen.add(key)
        if any(not a.strip() for a in c.aliases):
            names_bad = True
    if names_bad:
        problems.append(Msg(N_("Country names must be unique, not empty and at most {n} characters."),
                            {"n": COUNTRY_NAME_MAX}))
    for c in countries:
        problems += bag_text_problems(c.name, Msg(N_("Country {name}"), {"name": c.name}))
    for i, c in enumerate(countries):
        for alias in c.aliases:
            key = alias.casefold()
            if key and any(j != i and key in terms for j, terms in enumerate(terms_of)):
                problems.append(Msg(N_("{name} is used by more than one country."), {"name": alias}))

    names = [ink.name for ink in company.inks]
    if (any(not n.strip() for n in names) or len(set(names)) != len(names)
            or any(ink.cartridge not in CARTRIDGES or ink.colour not in INK_COLOURS for ink in company.inks)):
        problems.append(Msg(N_("Ink names must be unique, each with a cartridge type and a colour.")))

    lo, hi = company.lot_min_digits, company.lot_max_digits
    if not (_is_int(lo) and _is_int(hi) and 1 <= lo <= hi <= 20):
        problems.append(Msg(N_("The lot rules must allow 1 to 20 digits, the minimum no more than the maximum.")))
    if not _is_int(company.free_text_max) or not 1 <= company.free_text_max <= 100:
        problems.append(Msg(N_("The free-text limit must be 1 to 100 characters.")))
    fonts = company.standard_fonts
    if len(fonts) != 6 or any(not isinstance(f, str) or not f.strip() for f in fonts):
        problems.append(Msg(N_("The standard font list must have 6 fonts.")))
    if company.bag_counter not in ("PMCNT", "MSCNT", "none"):
        problems.append(Msg(N_("The bag counter must be PMCNT, MSCNT or none.")))
    return _unique(problems)


def origin_conflicts(data, company):
    """Warnings for a new country list: every print text, line and machine code that would now hold
    origin words. Those records can't be sent until fixed (Send checks them again)."""
    problems = []
    for pt in _sorted(data.print_texts):
        texts = []
        for it in pt.items:
            texts += [it.before, it.after]
            if it.kind == "fixed":
                texts.append(it.text)
        words = _words_in(texts, company)
        if words:
            problems.append(Msg(N_("Print text {name} now contains words about origin: {words}."),
                                {"name": pt.name, "words": ", ".join(words)}))
    for record in _sorted(data.lines) + _sorted(data.machines):
        words = origin_words_in(record.code, company)
        if words:
            problems.append(Msg(N_("The code of {name} now contains words about origin: {words}."),
                                {"name": record.name, "words": ", ".join(words)}))
    return problems


def _code_problems(code, name, company):
    if not re.fullmatch(CODE_PATTERN, code):
        return [Msg(N_("The code must be 1 to 3 capital letters or digits."))]
    words = origin_words_in(code, company)
    if words:
        return [_origin_words_msg(Msg(N_("The code of {name}"), {"name": name}), words)]
    return []


def validate_line(line, data):
    problems = []
    if not line.name.strip() or len(line.name) > 40:
        problems.append(Msg(N_("The name must be 1 to {n} characters."), {"n": 40}))
    problems += _code_problems(line.code, line.name, data.company)
    if line.cartridge not in CARTRIDGES:
        problems.append(Msg(N_("The cartridge type isn't valid.")))
    return problems


def validate_machine(machine, data):
    problems = []
    if not machine.name.strip() or len(machine.name) > 60:
        problems.append(Msg(N_("The name must be 1 to {n} characters."), {"n": 60}))
    problems += _code_problems(machine.code, machine.name, data.company)
    if machine.line_id not in data.lines:
        problems.append(Msg(N_("The machine's line doesn't exist.")))
    printer = data.printers.get(machine.printer_id)
    if printer is None:
        problems.append(Msg(N_("The machine's printer doesn't exist.")))
    elif machine.active:
        for other in _sorted(data.machines):
            if other.id != machine.id and other.active and other.printer_id == machine.printer_id:
                problems.append(Msg(N_("{printer} is already used by {machine}."),
                                    {"printer": printer.name, "machine": other.name}))
                break
    if machine.placement not in PLACEMENTS:
        problems.append(Msg(N_("The placement must be Encoder or Delay.")))
    if machine.print_speed is not None and (not _is_int(machine.print_speed)
                                            or machine.print_speed not in SPEED_RANGE):
        problems.append(Msg(N_("The print speed must be 1 to 2500.")))
    if not _is_number(machine.seconds_per_bag) or not 0.5 <= machine.seconds_per_bag <= 60:
        problems.append(Msg(N_("The seconds per bag must be 0.5 to 60.")))
    if machine.placement == "encoder":
        head = machine.head_distance_mm
        if head is not None and (not _is_number(head) or not 0 < head <= HEAD_DISTANCE_MAX_MM):
            problems.append(Msg(N_("The head distance must be more than 0 and at most {max} mm."),
                                {"max": _num(HEAD_DISTANCE_MAX_MM)}))
        wheel = machine.encoder_wheel_mm
        if wheel is not None and (not _is_number(wheel) or not 20 <= wheel <= 150):
            problems.append(Msg(N_("The encoder wheel must be 20 to 150 mm.")))
        ppr = machine.encoder_ppr
        if ppr is not None and (not _is_int(ppr) or not 1 <= ppr <= 1_000_000):
            problems.append(Msg(N_("The pulses per turn must be 1 to 1000000.")))
    return problems


def validate_film(film, data):
    problems = []
    if film.product_id not in data.products:
        problems.append(Msg(N_("The film's product doesn't exist.")))
    if not re.fullmatch(FILM_CODE_PATTERN, film.code):
        problems.append(Msg(N_("The film code must be 1 to 6 capital letters or digits.")))
    elif any(f.id != film.id and f.code == film.code for f in data.films.values()):
        problems.append(Msg(N_("Another film already uses the code {code}."), {"code": film.code}))
    if film.status not in FILM_STATUSES:
        problems.append(Msg(N_("The status isn't valid.")))
    if film.side not in ("front", "back") or film.cartridge not in CARTRIDGES or film.ink_colour not in INK_COLOURS:
        problems.append(Msg(N_("The side, cartridge type or ink colour isn't valid.")))

    length = film.length_mm
    length_ok = length is not None and _is_number(length) and 0 < length <= FILM_LENGTH_MAX_MM
    if length is not None and not length_ok:
        problems.append(Msg(N_("The film length must be more than 0 and at most {max} mm."),
                            {"max": _num(FILM_LENGTH_MAX_MM)}))
    if film.position_mm is not None and not (length_ok and _is_number(film.position_mm)
                                             and 0 <= film.position_mm <= length):
        problems.append(Msg(N_("The print position must be between 0 and the film length.")))
    if film.delay_ms is not None and not (_is_number(film.delay_ms) and 0 <= film.delay_ms <= _DELAY_MS_MAX):
        problems.append(Msg(N_("The delay must be 0 to 120000 ms.")))
    if film.print_length_mm is not None and not (length_ok and _is_number(film.print_length_mm)
                                                 and 0 < film.print_length_mm <= length):
        problems.append(Msg(N_("The print length must be more than 0 and at most the film length.")))

    if (film.font not in FONT_INDEXES or film.size not in SIZE_RANGE or film.resolution not in RESOLUTIONS
            or film.bold not in BOLD_RANGE or film.width not in WIDTH_RANGE):
        problems.append(Msg(N_("Font, size, resolution, bold or width is out of range.")))

    if film.origin_preprinted:
        where = Msg(N_("The pre-printed origin"))
        if not film.preprinted_origin.strip():
            problems.append(Msg(N_("Enter the pre-printed origin.")))
        elif len(film.preprinted_origin) > PREPRINTED_ORIGIN_MAX:
            problems.append(_too_long(where, PREPRINTED_ORIGIN_MAX))
        problems += bag_text_problems(film.preprinted_origin, where)

    missing = False
    for pt_id in film.print_text_ids:
        pt = data.print_texts.get(pt_id)
        if pt is None:
            missing = True
        elif film.origin_preprinted and _origin_count(pt):
            problems.append(Msg(N_("Print text {print_text} has an origin item, but this film's origin is "
                                   "pre-printed."), {"print_text": pt.name}))
        elif not film.origin_preprinted and not _origin_count(pt):
            problems.append(Msg(N_("Print text {print_text} has no origin item, but this film needs one."),
                                {"print_text": pt.name}))
    if missing:
        problems.append(Msg(N_("A print text allowed with this film doesn't exist.")))
    if film.default_print_text_id and film.default_print_text_id not in film.print_text_ids:
        problems.append(Msg(N_("The default print text must be one of the film's print texts.")))
    if film.status == "active" and (not film.print_text_ids or not film.default_print_text_id):
        problems.append(Msg(N_("An active film needs at least one print text and a default print text.")))
    return problems


# --------------------------------------------------------------------------
# What is offered at Send (§4.2) and the sender's choice (§4.3)
# --------------------------------------------------------------------------

def films_for_line(line, product_id, data):
    """Active films of the product with the line's cartridge type, by artwork revision and code."""
    films = [f for f in data.films.values()
             if f.product_id == product_id and f.status == "active" and f.cartridge == line.cartridge]
    return sorted(films, key=lambda f: (f.artwork_revision, f.code))


def print_texts_for_film(film, data):
    """The film's print texts, in its order, that are active, obey its origin rule and are valid."""
    out = []
    for pt_id in film.print_text_ids:
        pt = data.print_texts.get(pt_id)
        if (pt is not None and pt.status == "active" and _origin_rule_ok(film, pt)
                and not validate_print_text(pt, data.company)):
            out.append(pt)
    return out


def _free_value(choice_free_text, item):
    value = choice_free_text.get(item.id, "")
    return value.strip() if isinstance(value, str) else ""


def _present_items(pt, free_text):
    """The items that will be sent: an optional free text left empty is left out."""
    return [it for it in pt.items
            if not (it.kind == "free_text" and not it.required and not _free_value(free_text, it))]


def validate_choice(choice, data, identity):
    """Every problem with one line of a Send, in the §4.3 order; [] when it can be sent."""
    company = data.company
    line = data.lines.get(choice.line_id)
    if line is None or not line.active:
        return [Msg(N_("A line in this Send no longer exists or isn't active."))]
    on_line = {"line": line.name}
    problems = []

    # 2. Machines
    machines = []
    if not choice.machine_ids:
        problems.append(Msg(N_("Tick at least one machine on {line}."), on_line))
    active = {m.id for m in data.line_machines(line.id)}
    seen = set()
    for machine_id in choice.machine_ids:
        machine = data.machines.get(machine_id)
        name = machine.name if machine is not None else machine_id
        if machine_id in seen:
            problems.append(Msg(N_("{machine} is in this Send twice."), {"machine": name}))
            continue
        seen.add(machine_id)
        if machine_id not in active:
            problems.append(Msg(N_("{machine} isn't an active machine on {line}."), {"machine": name, **on_line}))
            continue
        machines.append(machine)

    # 3. Product, film and print text
    product = data.products.get(choice.product_id)
    if not choice.product_id:
        problems.append(Msg(N_("Choose a product for {line}."), on_line))
    elif product is None or not product.active:
        problems.append(Msg(N_("The product chosen for {line} isn't active."), on_line))
    film = data.films.get(choice.film_id)
    film_name = _film_name(film) if film is not None else choice.film_id
    if not choice.film_id:
        problems.append(Msg(N_("Choose a film for {line}."), on_line))
    elif film is None or film.id not in [f.id for f in films_for_line(line, choice.product_id, data)]:
        problems.append(Msg(N_("Film {film} can't be sent to {line}: it must be active, belong to the product and "
                               "use the line's cartridge type."), {"film": film_name, **on_line}))
    pt = data.print_texts.get(choice.print_text_id)
    if not choice.print_text_id:
        problems.append(Msg(N_("Choose a print text for {line}."), on_line))
    elif pt is None or film is None or pt.id not in [p.id for p in print_texts_for_film(film, data)]:
        problems.append(Msg(N_("Print text {print_text} can't be used with film {film}."),
                            {"print_text": pt.name if pt is not None else choice.print_text_id, "film": film_name}))

    if pt is not None:
        # 4. The lot
        if any(it.kind == "lot" for it in pt.items):
            lot = choice.lot.strip()
            lo, hi = company.lot_min_digits, company.lot_max_digits
            if not lot:
                problems.append(Msg(N_("Enter the lot for {line}."), on_line))
            elif not re.fullmatch(f"[0-9]{{{lo},{hi}}}", lot):
                problems.append(Msg(N_("The lot for {line} must be {min} to {max} digits."),
                                    {**on_line, "min": lo, "max": hi}))

    # 5. Countries
    if film is not None and not film.origin_preprinted:
        if not choice.countries:
            problems.append(Msg(N_("Choose at least one country of origin for {line}."), on_line))
        offered = {c.name for c in company.countries if c.active}
        chosen = set()
        for name in choice.countries:
            if name in chosen:
                problems.append(Msg(N_("{country} is chosen twice."), {"country": name}))
                continue
            chosen.add(name)
            if name not in offered:
                problems.append(Msg(N_("{country} isn't on the country list."), {"country": name}))

    if pt is not None:
        # 6. Free text
        for it in pt.items:
            if it.kind != "free_text":
                continue
            value = _free_value(choice.free_text, it)
            label = {"label": it.label}
            where = Msg(N_('The free text "{label}"'), label)
            if not value:
                if it.required:
                    problems.append(Msg(N_('Fill in "{label}" for {line}.'), {**label, **on_line}))
                continue
            if len(value) > it.max_len:
                problems.append(Msg(N_('"{label}" can be at most {n} characters.'), {**label, "n": it.max_len}))
            problems += _text_problems(value, where, company)
            if len(it.before) + len(value) + len(it.after) > TXT_MAX:
                problems.append(_too_long(where, TXT_MAX))
        if pt.items and not _present_items(pt, choice.free_text):
            problems.append(Msg(N_("The print text has no items.")))

    # 7. The records again
    if pt is not None:
        problems += validate_print_text(pt, company)
    problems += validate_line(line, data)
    for machine in machines:
        problems += validate_machine(machine, data)
    if film is not None:
        problems += validate_film(film, data)

    # 8. Per machine
    for machine in machines:
        on_machine = {"machine": machine.name}
        if not _printer_ready(data.printers.get(machine.printer_id)):
            problems.append(Msg(N_("The printer of {machine} isn't set up: it is missing, not active or has no "
                                   "saved password."), on_machine))
        if machine.print_speed is None:
            problems.append(Msg(N_("{machine} has no print speed. An administrator must set it."), on_machine))
        if film is None:
            continue
        on_film = {"film": film_name}
        if machine.placement == "encoder":
            if machine.head_distance_mm is None:
                problems.append(Msg(N_("{machine} isn't calibrated: it has no head distance."), on_machine))
            if film.length_mm is None:
                problems.append(Msg(N_("Film {film} has no length, which encoder machines need."), on_film))
            if film.position_mm is None:
                problems.append(Msg(N_("Film {film} has no print position, which encoder machines need."), on_film))
        elif machine.placement == "delay" and film.delay_ms is None:
            problems.append(Msg(N_("Film {film} has no delay, which delay machines need."), on_film))
        try:
            explain_position(machine, film, data.adjustment(machine.id, film.id))
        except PlanError as e:
            problems += e.problems
    return _unique(problems)


# --------------------------------------------------------------------------
# The plan (§4.4)
# --------------------------------------------------------------------------

def _settings(machine, position, *, bold, width, resolution):
    """§6.1: every print setting the new message is sent with."""
    encoder = machine.placement == "encoder"
    return PrintSettings(encoder=encoder, position_mm=position.sent if encoder else 0,
                         delay_us=0 if encoder else position.sent, speed=machine.print_speed,
                         rotate=bool(machine.rotate), mirror=bool(machine.mirror), resolution=resolution,
                         bold=bold, width=width, quantity=1, invert=False, auto_repeat=False)


def _origin_line(film, countries):
    if film.origin_preprinted:
        return f"{film.preprinted_origin} (pre-printed)"
    return origin_phrase(countries)


def _item_value(plan, item):
    """The data a CTEXT item shows (without its text before and after)."""
    kind = item.kind
    if kind == "fixed":
        return item.text
    if kind == "lot":
        return plan.lot
    if kind == "origin":
        return plan.origin_line
    if kind == "free_text":
        return _free_value(plan.free_text, item)
    if kind == "machine_code":
        return plan.machine.code
    if kind == "line_code":
        return plan.line.code
    raise ValueError(f"unknown item kind {kind!r}")


def _join_bag_text(parts):
    """§3.5: (text, placement, gap) in order -> one line; " / " before an item placed below,
    " " before one with a gap."""
    out = []
    for k, (text, placement, gap) in enumerate(parts):
        if k:
            out.append(" / " if placement == "below" else (" " if gap > 0 else ""))
        out.append(text)
    return "".join(out)


def _plan_bag_text(plan, day):
    parts = []
    for it in _present_items(plan.print_text, plan.free_text):
        if it.kind in _DATE_KINDS:
            try:
                days, months, years = _date_offsets(it)
                shown = render_date(DATE_FORMATS[it.date_format], day, add_days=days, add_months=months,
                                    add_years=years)
            except (KeyError, ValueError, OverflowError):
                shown = ""
            text = it.before + shown + it.after
        else:
            text = it.before + _item_value(plan, it) + it.after
        parts.append((text, it.placement, it.gap))
    return _join_bag_text(parts)


def merge_plan(request, data, identity):
    """The SendRequest checked and merged into one MachinePlan per machine. Raises NotAllowed for
    someone who isn't a sender, or PlanError with every problem; nothing is written anywhere.
    Deterministic: the same request and data give an equal plan."""
    company = data.company
    if not identity.is_sender(company):
        raise NotAllowed(Msg(N_("Only Stripes MapleJet Senders can send.")))
    problems = []
    if not request.lines:
        problems.append(Msg(N_("Choose at least one line.")))
    lines_seen = set()
    for choice in request.lines:
        if choice.line_id in lines_seen:
            line = data.lines.get(choice.line_id)
            problems.append(Msg(N_("{line} is in this Send twice."),
                                {"line": line.name if line is not None else choice.line_id}))
        lines_seen.add(choice.line_id)
    machines_seen = set()
    by_printer = {}
    for choice in request.lines:
        for machine_id in choice.machine_ids:
            machine = data.machines.get(machine_id)
            if machine_id in machines_seen:
                problems.append(Msg(N_("{machine} is in this Send twice."),
                                    {"machine": machine.name if machine is not None else machine_id}))
                continue
            machines_seen.add(machine_id)
            if machine is None:
                continue
            first = by_printer.setdefault(machine.printer_id, machine)
            if first is not machine:
                problems.append(Msg(N_("{machine} and {other} use the same printer."),
                                    {"machine": first.name, "other": machine.name}))
    for choice in request.lines:
        problems += validate_choice(choice, data, identity)
    problems = _unique(problems)
    if problems:
        raise PlanError(problems[0], problems=problems)

    day = _local_date(request.created_at)
    plans = []
    for choice in request.lines:
        line = data.lines[choice.line_id]
        product = data.products[choice.product_id]
        film = data.films[choice.film_id]
        pt = data.print_texts[choice.print_text_id]
        has_lot = any(it.kind == "lot" for it in pt.items)
        countries = [] if film.origin_preprinted else list(choice.countries)
        free_text = {}
        for it in _present_items(pt, choice.free_text):
            if it.kind == "free_text":
                free_text[it.id] = _free_value(choice.free_text, it)
        for machine_id in choice.machine_ids:
            machine = data.machines[machine_id]
            adjustment = data.adjustment(machine.id, film.id)
            position = explain_position(machine, film, adjustment)
            warnings = []
            if position.warning is not None:
                warnings.append(position.warning)
            elif machine.placement == "encoder" and film.print_length_mm is None:
                warnings.append(Msg(N_("Print length unknown: Stripes can't check that the print ends before "
                                       "the film stops.")))
            plan = MachinePlan(
                machine=copy.deepcopy(machine), line=copy.deepcopy(line),
                printer=copy.deepcopy(data.printers[machine.printer_id]), product=copy.deepcopy(product),
                film=copy.deepcopy(film), print_text=copy.deepcopy(pt), adjustment=copy.deepcopy(adjustment),
                lot=choice.lot.strip() if has_lot else "", countries=list(countries), free_text=dict(free_text),
                origin_line=_origin_line(film, countries),
                settings=_settings(machine, position, bold=film.bold, width=film.width, resolution=film.resolution),
                position=position, warnings=warnings, test_step=None)
            plan.bag_text = _plan_bag_text(plan, day)
            plans.append(plan)
    return SendPlan(send_id=request.send_id, request=request, machines=plans,
                    from_local_copy=(data.source == "local"), data_as_of=data.reached_at)


# --------------------------------------------------------------------------
# Print position and delay (§5)
# --------------------------------------------------------------------------

def print_position_mm(position_mm, head_distance_mm, film_length_mm, adjustment_mm=0.0):
    """(position - head distance + adjustment) mod film length, rounded half up to whole mm, in
    [0, film length): a result rounded up to the length is the same spot as 0."""
    if film_length_mm <= 0:
        raise ValueError("the film length must be more than 0")
    x = (position_mm - head_distance_mm + adjustment_mm) % film_length_mm
    sent = math.floor(x + 0.5)
    if sent >= film_length_mm:
        sent = 0
    return int(sent)


def delay_us(delay_ms, adjustment_ms=0.0):
    """round((delay + adjustment) x 1000), half up; PlanError below 0 or above DELAY_US_MAX."""
    total = delay_ms + adjustment_ms
    us = math.floor(total * 1000 + 0.5)
    if us < 0 or us > DELAY_US_MAX:
        m = Msg(N_("The delay works out at {ms} ms, outside 0 to 120000 ms."), {"ms": _num(total)})
        raise PlanError(m, problems=[m])
    return int(us)


def film_stop_warning(sent_mm, print_length_mm, film_length_mm):
    """A warning when the print would still be running when the film stops; None otherwise or when
    the print length isn't known."""
    if print_length_mm is None or film_length_mm is None:
        return None
    if sent_mm + print_length_mm > film_length_mm:
        return Msg(N_("The print would still be running when the film stops ({end} of {length} mm), so the "
                      "next bag would go out blank."),
                   {"end": _num(sent_mm + print_length_mm), "length": _num(film_length_mm)})
    return None


def _raise(problems):
    raise PlanError(problems[0], problems=problems)


def explain_position(machine, film, adjustment, *, override=None, test=False):
    """How the position (encoder) or delay (delay machines) is worked out. override replaces the
    film's value (a test step's); test=True ignores the adjustment. Missing values raise PlanError."""
    on_machine, on_film = {"machine": machine.name}, {"film": _film_name(film)}
    suffix = " (test print: adjustment ignored)" if test else ""
    if machine.placement == "encoder":
        value = override if override is not None else film.position_mm
        problems = []
        if machine.head_distance_mm is None:
            problems.append(Msg(N_("{machine} isn't calibrated: it has no head distance."), on_machine))
        if film.length_mm is None:
            problems.append(Msg(N_("Film {film} has no length, which encoder machines need."), on_film))
        elif film.length_mm <= 0:
            problems.append(Msg(N_("The film length must be more than 0 and at most {max} mm."),
                                {"max": _num(FILM_LENGTH_MAX_MM)}))
        if value is None:
            problems.append(Msg(N_("Film {film} has no print position, which encoder machines need."), on_film))
        if problems:
            _raise(problems)
        adj = 0.0 if test or adjustment is None else float(adjustment.position_mm)
        head, length = float(machine.head_distance_mm), float(film.length_mm)
        raw = value - head + adj
        sent = print_position_mm(value, head, length, adj)
        return PositionWorking(
            placement="encoder", film_value=float(value), adjustment=adj, head_distance_mm=head,
            film_length_mm=length, sent=sent, wrapped=raw < 0 or raw >= length,
            text=f"({_num(value)} - {_num(head)} {_signed(adj)}) mod {_num(length)} = {sent} mm{suffix}",
            warning=film_stop_warning(sent, film.print_length_mm, length))
    if machine.placement == "delay":
        value = override if override is not None else film.delay_ms
        if value is None:
            _raise([Msg(N_("Film {film} has no delay, which delay machines need."), on_film)])
        adj = 0.0 if test or adjustment is None else float(adjustment.delay_ms)
        sent = delay_us(value, adj)
        return PositionWorking(
            placement="delay", film_value=float(value), adjustment=adj, head_distance_mm=None,
            film_length_mm=film.length_mm, sent=sent, wrapped=False,
            text=f"{_num(value)} {_signed(adj)} = {_num(value + adj)} ms = {sent} µs{suffix}", warning=None)
    _raise([Msg(N_("The placement must be Encoder or Delay."))])


# --------------------------------------------------------------------------
# Building a message (§6)
# --------------------------------------------------------------------------

def _need(ok, what):
    if not ok:
        raise ValueError(f"build_message: {what} is out of range (validation comes first)")


def _check_text(text, limit, what):
    _need(isinstance(text, str) and len(text) <= limit and not bag_text_problems(text, ""), what)


def _check_font(font, size):
    _need(_is_int(font) and font in FONT_INDEXES and _is_int(size) and size in SIZE_RANGE, "a font or size")


def _flag(value):
    return "1" if value else "0"


def _cpparm(s):
    """The 11 CPPARM children in the order they are sent."""
    _need(_is_int(s.speed) and s.speed in SPEED_RANGE, "the print speed")
    _need(_is_int(s.resolution) and s.resolution in RESOLUTIONS, "the resolution")
    _need(_is_int(s.bold) and s.bold in BOLD_RANGE, "bold")
    _need(_is_int(s.width) and s.width in WIDTH_RANGE, "width")
    _need(_is_int(s.quantity) and 1 <= s.quantity <= 100, "the quantity")
    if s.encoder:
        _need(_is_int(s.position_mm) and 0 <= s.position_mm <= DLYMM_MAX, "the print position")
        where = ("DLYMM", str(s.position_mm))
    else:
        _need(_is_int(s.delay_us) and 0 <= s.delay_us <= DELAY_US_MAX, "the delay")
        where = ("DELAY", str(s.delay_us))
    return [("ENCST", _flag(s.encoder)), where, ("PRNSPD", str(s.speed)), ("ROTAT", _flag(s.rotate)),
            ("MIRR", _flag(s.mirror)), ("RESMOD", str(s.resolution)), ("BOLD", str(s.bold)),
            ("WDTH", str(s.width)), ("QUANT", str(s.quantity)), ("REVRS", _flag(s.invert)),
            ("AUTREP", _flag(s.auto_repeat))]


def _text_items(plan):
    """§3.1 to §3.4: one block per present item of the print text."""
    film = plan.film
    _check_font(film.font, film.size)
    pt = plan.print_text
    _need(pt is not None, "the print text")
    present = _present_items(pt, plan.free_text)
    for it in present:
        if it.kind == "free_text":
            _need(_free_value(plan.free_text, it) != "", "a required free text")
        if it.kind == "origin":
            _need(plan.origin_line == origin_phrase(plan.countries), "the origin line")
    items = []
    prev = line_start = None
    for k, it in enumerate(present):
        objid = FIRST_OBJID + k
        if prev is None:
            place, marg = [("POS", "0,0")], "0,0"
            line_start = objid
        else:
            _need(_is_int(it.gap) and 0 <= it.gap <= GAP_MAX, "a gap")
            if it.placement == "right":
                # right of the previous item, same top
                place = [("XRID", str(prev)), ("YRID", str(prev)), ("SXAS", "0"), ("SYAS", "1")]
                marg = f"{it.gap},0"
            elif it.placement == "below":
                # a new line under the previous item, lined up with the start of the previous line
                place = [("XRID", str(line_start)), ("YRID", str(prev)), ("SXAS", "1"), ("SYAS", "0")]
                marg = f"0,{it.gap}"
                line_start = objid
            else:
                raise ValueError(f"build_message: unknown placement {it.placement!r}")
        children = [("OBJID", str(objid))] + place + [("MARG", marg), ("FONTIND", str(film.font)),
                                                      ("SIZE", str(film.size))]
        if it.kind in _DATE_KINDS:
            _need(it.date_format in DATE_FORMATS, "a date format")
            days, months, years = _date_offsets(it)
            if it.before:
                _check_text(it.before, AFFIX_MAX, "the text before a date")
                children.append(("PREF", it.before))
            if it.after:
                _check_text(it.after, AFFIX_MAX, "the text after a date")
                children.append(("POSF", it.after))
            children += [("FRMT", DATE_FORMATS[it.date_format]), ("DAY", str(days)), ("MON", str(months)),
                         ("YEAR", str(years))]
            items.append(("CTDAT", children))
        else:
            txt = it.before + _item_value(plan, it) + it.after
            _check_text(txt, TXT_MAX, "a text")
            children.append(("TXT", txt))
            items.append(("CTEXT", children))
        prev = objid
    return items


def _sample_text(font, size):
    return f"{STANDARD_FONTS[font - 1]} {size}"


def _test_items(plan):
    """§15.3: the step's label, then its font samples side by side."""
    film, step = plan.film, plan.test_step
    _check_font(film.font, film.size)
    _check_text(step.label, TXT_MAX, "the test label")
    _need(len(step.fonts) <= MAX_FONT_SAMPLES, "the number of font samples")
    items = [("CTEXT", [("OBJID", str(FIRST_OBJID)), ("POS", "0,0"), ("MARG", "0,0"), ("FONTIND", str(film.font)),
                        ("SIZE", str(film.size)), ("TXT", step.label)])]
    prev = FIRST_OBJID
    for k, (font, size) in enumerate(step.fonts, start=1):
        _check_font(font, size)
        objid = FIRST_OBJID + k
        items.append(("CTEXT", [("OBJID", str(objid)), ("XRID", str(prev)), ("YRID", str(prev)), ("SXAS", "0"),
                                ("SYAS", "1"), ("MARG", f"{TEST_SAMPLE_GAP},0"), ("FONTIND", str(font)),
                                ("SIZE", str(size)), ("TXT", _sample_text(font, size))]))
        prev = objid
    return items


def design_parts(plan):
    """(item blocks, CPPARM children) of the plan's message, before naming: [(tag, [(child, value)])]
    and [(child, value)], values as unescaped strings in the order sent. ValueError for a value out
    of range (validation comes first)."""
    items = _test_items(plan) if plan.test_step is not None else _text_items(plan)
    _need(1 <= len(items) <= MAX_ITEMS, "the number of items")
    return items, _cpparm(plan.settings)


def expected_settings(settings):
    """Every CPPARM value the read-back must show: the printer's defaults with every child sent
    replacing its default, RESNAM for the resolution, and RESO left out (23 keys)."""
    expected = dict(PRINTER_DEFAULT_SETTINGS)
    expected.update(_cpparm(settings))
    expected["RESNAM"] = RESOLUTION_NAMES[settings.resolution]
    for key in IGNORED_SETTINGS:
        expected.pop(key, None)
    return expected


def build_message(plan, slot):
    """The commands that build the plan's message in slot, and what the read-back must show."""
    if slot not in SLOTS:
        raise ValueError(f"Stripes writes only slots {SLOTS}, not {slot!r}")
    items, settings = design_parts(plan)
    content = content_code(plan)
    name = message_name(plan, slot)
    commands = ([Command.set("CRTMSG", name)] + [Command.block(tag, children) for tag, children in items]
                + [Command.block("CPPARM", settings), Command.set("SAVEMSG", slot)])
    return BuiltMessage(slot=slot, name=name, content_code=content, commands=commands,
                        expected_items=[DesignItem(tag=tag, pairs=list(children)) for tag, children in items],
                        expected_settings=expected_settings(plan.settings), origin_line=plan.origin_line,
                        bag_text=plan.bag_text)


def build_commands(plan, slot):
    return build_message(plan, slot).commands


def empty_message_commands(slot):
    """The end of a test: a design with no name and no items saved over slot (no delete command)."""
    if slot not in SLOTS:
        raise ValueError(f"Stripes writes only slots {SLOTS}, not {slot!r}")
    return [Command.set("CRTMSG", ""), Command.set("SAVEMSG", slot)]


def idle_slot(printing, printing_slot):
    """The slot of SLOTS that isn't printing_slot; 9 when neither 9 nor 10 is selected. Printing on or
    off doesn't matter: Stripes alternates either way."""
    if printing_slot == 9:
        return 10
    if printing_slot == 10:
        return 9
    return DEFAULT_IDLE_SLOT


# --------------------------------------------------------------------------
# Message names and content codes (§7)
# --------------------------------------------------------------------------

def canonical_design(items, settings):
    """The exact bytes the content code is computed from: the children sent, in order, without the
    name and the slot."""
    obj = {"v": 1,
           "items": [[tag, [[child, value] for child, value in children]] for tag, children in items],
           "settings": [[child, value] for child, value in settings]}
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def content_code(plan):
    """4 characters of Crockford base 32 from the first 20 bits of the canonical design's sha256."""
    h = hashlib.sha256(canonical_design(*design_parts(plan))).digest()
    n = int.from_bytes(h[:3], "big") >> 4
    return "".join(CONTENT_ALPHABET[(n >> s) & 31] for s in (15, 10, 5, 0))


def check_letter(text):
    """The check letter of a name; text is the whole name without it."""
    return CHECK_LETTERS[int.from_bytes(hashlib.sha256(text.encode("ascii")).digest()[:4], "big") % 24]


def _name(film_code, code, revision, content, slot):
    head = f"{film_code}-{code}{revision % 100:02d}-{content}-"
    slot_char = SLOT_CHARS[slot]
    return f"{head}{check_letter(head + slot_char)}{slot_char}"


def message_name(plan, slot):
    """{film}-{print text}{revision:02d}-{content}-{check}{slot}: 13 to 20 characters."""
    if slot not in SLOTS:
        raise ValueError(f"Stripes writes only slots {SLOTS}, not {slot!r}")
    _need(bool(re.fullmatch(FILM_CODE_PATTERN, plan.film.code)), "the film code")
    if plan.test_step is not None:
        code, revision = TEST_CODE, plan.test_step.index
    else:
        code, revision = plan.print_text.code, plan.print_text.revision
        _need(bool(re.fullmatch(CODE_PATTERN, code)), "the print text code")
    return _name(plan.film.code, code, revision, content_code(plan), slot)


def parse_message_name(name):
    """The parts of one of Stripes' own names; None unless the format and the check letter are right."""
    if not isinstance(name, str):
        return None
    m = re.fullmatch(NAME_PATTERN, name)
    if m is None:
        return None
    film_code, code, revision, content, letter, slot_char = m.groups()
    if check_letter(f"{film_code}-{code}{revision}-{content}-{slot_char}") != letter:
        return None
    return MessageName(film_code=film_code, print_text_code=code, print_text_revision=int(revision),
                       content_code=content, slot=9 if slot_char == "9" else 10)


# --------------------------------------------------------------------------
# Read-back (§8)
# --------------------------------------------------------------------------

_ANY = object()
_UNEXPECTED = object()


def _wanted(field, sent, is_date):
    """What a read-back item field must be: a string, _ANY or _UNEXPECTED (protocol.md §12.2)."""
    if field == "BOUND" or field in BOOKKEEPING_FIELDS:
        return _ANY                      # BOUND's shape and area are checked separately
    if field == "TXT" and is_date:
        return _ANY                      # the date of the read: date_differences checks it
    if field == "SIZE":
        return str(int(sent["SIZE"]) - 1) if "SIZE" in sent else ""   # read back one lower
    if field in ("POS", "MARG", "OBJID"):
        return sent[field] if field in sent else _ANY
    if field in ("XRID", "YRID", "SXAS", "SYAS"):
        return sent.get(field, "0")
    if is_date and field in ("DAY", "MON", "YEAR"):
        return sent.get(field, "0")
    if field in ("TXT", "PREF", "POSF", "FONTIND", "FRMT"):
        return sent.get(field, "")
    if field in ITEM_ZERO_FIELDS or (is_date and field in DATE_ZERO_FIELDS):
        return "0"
    return _UNEXPECTED


def _item_differences(where, expected, got):
    sent = dict(expected.pairs)
    is_date = expected.tag == "CTDAT"
    diffs = []
    seen = set()
    for field, value in got.pairs:
        seen.add(field)
        if field == "BOUND":
            if not _FOUR_INTS.fullmatch(value):
                diffs.append(Difference(where, "BOUND", "x1,y1,x2,y2", value))
            continue
        want = _wanted(field, sent, is_date)
        if want is _ANY:
            continue
        if want is _UNEXPECTED:
            diffs.append(Difference(where, field, "(not expected)", value))
        elif value != want:
            diffs.append(Difference(where, field, want, value))
    for field, value in expected.pairs:
        if field not in seen:
            want = _wanted(field, sent, is_date)
            diffs.append(Difference(where, field, want if isinstance(want, str) else value, "(missing)"))
    return diffs


def compare(sent, readback):
    """Every difference between what was sent and the printer's own copy; [] when it matches. Values
    are compared as the raw strings read: no trimming, no entity decoding."""
    if readback.empty:
        return [Difference("message", "empty", "a message", "an empty slot")]
    diffs = []
    if readback.slot != sent.slot:
        diffs.append(Difference("slot", "MSNO", str(sent.slot), str(readback.slot)))
    if readback.name != sent.name:
        diffs.append(Difference("name", "MSGNAM", sent.name, readback.name))
    if readback.width is not None and readback.width != CANVAS[0]:
        diffs.append(Difference("message", "W", str(CANVAS[0]), str(readback.width)))
    if readback.height is not None and readback.height != CANVAS[1]:
        diffs.append(Difference("message", "H", str(CANVAS[1]), str(readback.height)))

    if len(sent.expected_items) != len(readback.items):
        diffs.append(Difference("items", "count", str(len(sent.expected_items)), str(len(readback.items))))
    for n, (expected, got) in enumerate(zip(sent.expected_items, readback.items), start=1):
        where = f"item {n} ({expected.tag})"
        if expected.tag != got.tag:
            diffs.append(Difference(where, "tag", expected.tag, got.tag))
            continue
        diffs += _item_differences(where, expected, got)

    for key, value in sent.expected_settings.items():
        if key not in readback.settings:
            diffs.append(Difference("print settings", key, value, "(missing)"))
        elif readback.settings[key] != value:
            diffs.append(Difference("print settings", key, value, readback.settings[key]))
    for key, value in readback.settings.items():
        if key not in sent.expected_settings and key not in IGNORED_SETTINGS:
            diffs.append(Difference("print settings", key, "(not expected)", value))

    if readback.black_pixels == 0 and any(it.value("TXT") or it.value("PREF") for it in sent.expected_items):
        diffs.append(Difference("picture", "BPIX", "more than 0", "0"))

    # The print area (OPEN: BOUND units): an item that runs off it fails before the switch
    height = readback.height if readback.height is not None else CANVAS[1]
    for n, item in enumerate(readback.items, start=1):
        for field, value in item.pairs:
            m = _FOUR_INTS.fullmatch(value) if field == "BOUND" else None
            if m is None:
                continue
            x1, y1, x2, y2 = (int(g) for g in m.groups())
            if x1 < 0 or y1 < 0 or x2 > BOUND_MAX_X or y2 > height:
                diffs.append(Difference(f"item {n} ({item.tag})", "BOUND", f"inside {BOUND_MAX_X} x {height}",
                                        value))
    return diffs


def date_differences(sent, readback, read_local, *, slack_s=DATE_SLACK_S):
    """Each CTDAT's rendered date against Stripes' own rendering for the local time of the read
    (read_local, by the time server's clock), give or take slack_s around midnight."""
    diffs = []
    for n, (expected, got) in enumerate(zip(sent.expected_items, readback.items), start=1):
        if expected.tag != "CTDAT" or got.tag != "CTDAT":
            continue
        frmt = expected.value("FRMT", "")
        offsets = dict(add_days=int(expected.value("DAY", "0")), add_months=int(expected.value("MON", "0")),
                       add_years=int(expected.value("YEAR", "0")))
        shown = [render_date(frmt, (read_local + timedelta(seconds=s)).date(), **offsets)
                 for s in (-slack_s, 0, slack_s)]
        text = got.value("TXT", "")
        if text not in shown:
            diffs.append(Difference(f"item {n} (CTDAT)", "TXT", shown[1], text))
    return diffs


def fingerprint(design):
    """sha256 hex of the design without what changes by itself: the slot, the read time and request
    numbers, the picture, every BOUND, every CTDAT's TXT and the POS of relative items."""
    items = []
    for it in design.items:
        relative = it.value("XRID", "0") not in ("", "0") or it.value("YRID", "0") not in ("", "0")
        items.append([it.tag, [[k, v] for k, v in it.pairs
                               if k != "BOUND"
                               and not (it.tag == "CTDAT" and k == "TXT")
                               and not (relative and k == "POS")]])
    obj = {"v": 1, "name": design.name, "empty": design.empty, "width": design.width,
           "height": design.height, "items": items,
           "settings": sorted([k, v] for k, v in design.settings.items())}
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def origin_from_design(design):
    """The origin line as read back: the TXT of the first CTEXT starting with ORIGIN_PREFIX, else ""."""
    for it in design.items:
        if it.tag == "CTEXT":
            txt = it.value("TXT", "") or ""
            if txt.startswith(ORIGIN_PREFIX):
                return txt
    return ""


def cleared(design):
    """The slot holds nothing that can print."""
    return design.empty or (not design.items and design.name == "")


# --------------------------------------------------------------------------
# The printer clock (§9.4)
# --------------------------------------------------------------------------

def clock_command(local):
    """TD with the date, the time rounded to the nearest minute (TD has no seconds) and the midnight
    rollover. ValueError for a year outside 2000 to 2050."""
    t = (local + timedelta(seconds=30)).replace(second=0, microsecond=0)
    if not 2000 <= t.year <= 2050:
        raise ValueError(f"the printer clock can't be set to the year {t.year}")
    return Command.block("TD", [("DAY", t.day), ("MNTH", t.month), ("YEAR", t.year), ("HOUR", t.hour),
                                ("MIN", t.minute), ("DTRH", ROLLOVER[0]), ("DTRM", ROLLOVER[1])])


# --------------------------------------------------------------------------
# Resume and the result colour (§13.3, §14)
# --------------------------------------------------------------------------

def resume_refusal(expected, machine):
    """Why Resume isn't allowed from the expected state alone; None when it is."""
    on_machine = {"machine": machine.name}
    if expected is None or expected.mode == "none":
        return Msg(N_("No Send is recorded for {machine}. Send instead."), on_machine)
    if expected.mode in ("switch_pending", "test_running"):
        return Msg(N_("{machine} is in the middle of a Send or a test."), on_machine)
    if expected.rejected:
        return Msg(N_("The last Send to {machine} was rejected. Send again instead."), on_machine)
    if expected.mode == "test_finished" or expected.stop_reason == "test":
        return Msg(N_("{machine} needs a Send after the test print."), on_machine)
    if expected.resume_blocked or expected.failure is not None:
        return Msg(N_("{machine} needs a new Send before it can print again."), on_machine)
    if not expected.confirmed:
        return Msg(N_("The last Send to {machine} wasn't confirmed. Send again instead."), on_machine)
    return None


def resume_mismatch(expected, design, machine):
    """None when the printer's running message is the last confirmed Send's."""
    if (design.name == expected.message_name and design.slot == expected.slot
            and fingerprint(design) == expected.fingerprint):
        return None
    return Msg(N_("The message on {machine} isn't the last confirmed Send. Send again instead."),
               {"machine": machine.name})


def result_colour(confirmed, notes):
    """GREEN for a confirmed Send without an amber note, else AMBER."""
    if confirmed and not any(note in AMBER_NOTES for note in notes):
        return GREEN
    return AMBER


# --------------------------------------------------------------------------
# Test print and calibration (§15, §16)
# --------------------------------------------------------------------------

def test_bag_estimate(plan):
    """Roughly how many bags a test uses: one extra bag per switch."""
    return len(plan.steps) * (plan.bags_per_step + 1)


def plan_test(machine_id, film_id, ranges, data, identity, *, kind="test"):
    """The steps of a test print ("test") or a calibration run ("spacing", "head_distance") on one
    machine. Raises NotAllowed for someone who isn't an administrator, PlanError with every problem."""
    company = data.company
    if not identity.is_admin(company):
        raise NotAllowed(Msg(N_("Only Stripes Administrators can run test prints.")))
    if kind not in ("test", "spacing", "head_distance"):
        raise ValueError(f"unknown test kind {kind!r}")
    machine = data.machines.get(machine_id)
    film = data.films.get(film_id)
    line = data.lines.get(machine.line_id) if machine is not None else None
    printer = data.printers.get(machine.printer_id) if machine is not None else None
    line_name = line.name if line is not None else ""
    problems = []
    if machine is None or not machine.active:
        problems.append(Msg(N_("{machine} isn't an active machine on {line}."),
                            {"machine": machine.name if machine is not None else machine_id, "line": line_name}))
    else:
        problems += validate_machine(machine, data)
        if not _printer_ready(printer):
            problems.append(Msg(N_("The printer of {machine} isn't set up: it is missing, not active or has no "
                                   "saved password."), {"machine": machine.name}))
        if machine.print_speed is None:
            problems.append(Msg(N_("{machine} has no print speed. An administrator must set it."),
                                {"machine": machine.name}))
    if film is None:
        problems.append(Msg(N_("Choose a film for {line}."), {"line": line_name}))
    else:
        problems += validate_film(film, data)
    if machine is None or film is None:
        _raise(_unique(problems))

    encoder = machine.placement == "encoder"
    length = film.length_mm
    on_film = {"film": _film_name(film)}
    fonts = [(f, s) for f, s in ranges.fonts] if kind == "test" else []
    if kind == "test":
        if ranges.vary != ("position" if encoder else "delay"):
            problems.append(Msg(N_("The test must vary the print position on encoder machines and the delay on "
                                   "delay machines.")))
        if encoder:
            if machine.head_distance_mm is None:
                problems.append(Msg(N_("{machine} isn't calibrated: it has no head distance."),
                                    {"machine": machine.name}))
            if length is None:
                problems.append(Msg(N_("Film {film} has no length, which encoder machines need."), on_film))
        values = []
        if not (_is_number(ranges.start) and _is_number(ranges.stop) and _is_number(ranges.step)
                and ranges.step > 0 and ranges.start <= ranges.stop):
            problems.append(Msg(N_("The test range isn't valid: check the start, stop and step.")))
        else:
            count = math.floor((ranges.stop - ranges.start) / ranges.step + 1e-9) + 1
            total = count + len(ranges.bold) + len(ranges.width) + len(ranges.resolution)
            if total > MAX_TEST_STEPS:
                problems.append(Msg(N_("The test would have {n} steps; at most {max} are allowed."),
                                    {"n": total, "max": MAX_TEST_STEPS}))
            else:
                values = [round(ranges.start + i * ranges.step, 1) for i in range(count)]
                top = length if encoder else _DELAY_MS_MAX
                if top is not None and any(not 0 <= v <= top for v in values):
                    problems.append(Msg(N_("A tested value is out of range.")))
        if (any(b not in BOLD_RANGE for b in ranges.bold) or any(w not in WIDTH_RANGE for w in ranges.width)
                or any(r not in RESOLUTIONS for r in ranges.resolution)):
            problems.append(Msg(N_("A tested value is out of range.")))
        if len(fonts) > MAX_FONT_SAMPLES:
            problems.append(Msg(N_("At most {n} fonts can be printed side by side."), {"n": MAX_FONT_SAMPLES}))
        elif any(not (_is_int(f) and f in FONT_INDEXES and _is_int(s) and s in SIZE_RANGE) for f, s in fonts):
            problems.append(Msg(N_("A tested value is out of range.")))
    else:
        calibration = data.calibrations.get(machine.id)
        rough = calibration.rough_head_distance_mm if calibration is not None else None
        spacing = calibration.spacing_sent_mm if calibration is not None else 300
        if not encoder:
            problems.append(Msg(N_("Calibration is only for encoder machines.")))
        if length is None:
            problems.append(Msg(N_("Film {film} has no length, which encoder machines need."), on_film))
        if rough is None:
            problems.append(Msg(N_("Measure the rough head distance first.")))
        if kind == "spacing" and length is not None and length < spacing + 50:
            problems.append(Msg(N_("This film is too short for the {mm} mm spacing check."), {"mm": spacing}))
    problems = _unique(problems)
    if problems:
        _raise(problems)

    def step(index, label, value, bold=film.bold, width=film.width, resolution=film.resolution):
        return TestStep(index=index, label=label, position_mm=float(value) if encoder else None,
                        delay_ms=None if encoder else float(value), bold=bold, width=width, resolution=resolution,
                        fonts=list(fonts))

    base_machine = copy.deepcopy(machine)
    if kind == "test":
        own = film.position_mm if encoder else film.delay_ms
        v0 = own if own is not None else ranges.start

        def label(v, bold, width, resolution):
            parts = [("P" if encoder else "D") + _num(v)]
            if ranges.bold:
                parts.append(f"B{bold}")
            if ranges.width:
                parts.append(f"W{width}")
            if ranges.resolution:
                parts.append(f"R{resolution}")
            return " | ".join(parts)

        settings = [(v, film.bold, film.width, film.resolution) for v in values]
        settings += [(v0, b, film.width, film.resolution) for b in ranges.bold]
        settings += [(v0, film.bold, w, film.resolution) for w in ranges.width]
        settings += [(v0, film.bold, film.width, r) for r in ranges.resolution]
        steps = [step(i, label(*s), s[0], *s[1:]) for i, s in enumerate(settings, start=1)]
    else:
        # The marks land near the middle of a bag if the rough distance is right; each position is
        # sent as it is, so the machine copy gets head distance 0
        half = length / 2
        if kind == "spacing":
            steps = [step(1, "SPACE A", print_position_mm(half - spacing / 2 - rough, 0.0, length)),
                     step(2, "SPACE B", print_position_mm(half + spacing / 2 - rough, 0.0, length))]
        else:
            steps = [step(1, "HEAD", print_position_mm(half - rough, 0.0, length))]
        base_machine.head_distance_mm = 0.0
        v0 = steps[0].position_mm

    position = explain_position(base_machine, film, None, override=v0, test=True)
    base = MachinePlan(machine=base_machine, line=copy.deepcopy(line), printer=copy.deepcopy(printer), product=None,
                       film=copy.deepcopy(film), print_text=None, adjustment=None,
                       settings=_settings(base_machine, position, bold=film.bold, width=film.width,
                                          resolution=film.resolution),
                       position=position, origin_line="", test_step=None)
    return TestPlan(run_id=new_id("tp"), kind=kind, base=base, steps=steps, bags_per_step=5,
                    timed=(company.bag_counter == "none"))


def test_step_plan(base, step):
    """The MachinePlan of one test step: the base with the step's settings, position or delay."""
    plan = copy.deepcopy(base)
    encoder = plan.machine.placement == "encoder"
    override = step.position_mm if encoder else step.delay_ms
    plan.position = explain_position(plan.machine, plan.film, None, override=override, test=True)
    plan.settings = _settings(plan.machine, plan.position, bold=step.bold, width=step.width,
                              resolution=step.resolution)
    plan.test_step = copy.deepcopy(step)
    plan.bag_text = _join_bag_text([(step.label, "right", 0)]
                                   + [(_sample_text(f, s), "right", TEST_SAMPLE_GAP) for f, s in step.fonts])
    return plan


def spacing_gap_mm(first_mm, second_mm, film_length_mm):
    """The measured gap between the two spacing marks (each measured from the bag's bottom edge)."""
    return (second_mm - first_mm) % film_length_mm


def wheel_suggestion_mm(wheel_mm, sent_gap_mm, measured_gap_mm):
    """The wheel size that would make the measured gap right, in whole mm (ENWHD is an int);
    None outside ENWHD's 20..150."""
    if not sent_gap_mm or sent_gap_mm <= 0:
        return None
    w = math.floor(wheel_mm * measured_gap_mm / sent_gap_mm + 0.5)
    return int(w) if 20 <= w <= 150 else None


def head_distance_from_print(sent_mm, measured_mm, film_length_mm, rough_mm):
    """The head distance from a print at a known position: measured - sent (mod the film length),
    with the tape measurement choosing the multiple of the length. OPEN: the method."""
    if film_length_mm <= 0:
        raise ValueError("the film length must be more than 0")
    base = (measured_mm - sent_mm) % film_length_mm
    k = max(0, round((rough_mm - base) / film_length_mm))
    h = base + k * film_length_mm
    if abs(h - rough_mm) > 0.25 * film_length_mm:
        m = Msg(N_("The tape measurement is too far from the print to be sure. Measure the head distance again."))
        raise PlanError(m, problems=[m])
    return round(h, 1)
