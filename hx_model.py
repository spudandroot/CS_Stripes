"""MapleJet Hx data: constants, timings, errors, JSON mapping and every shared record.

Pure data, no file, network, Windows or Tk work. Every other hx_ module uses these
types: the records administrators edit (company, lines, machines, printers,
products, films, print texts), a Send's request and plan, the printer commands and
the read-back, the status files, results, bus events and the views the UI shows.

Records are stored as JSON with their field names as keys (to_json / from_json).
IDs never change; names are only for display. Times are epoch seconds from the
writer's own clock, for display and logs only: staleness is judged elsewhere, by
counters on the reader's own monotonic clock.
"""

from __future__ import annotations

import copy
import dataclasses
import re
import secrets
import types
import typing
from dataclasses import dataclass, field

from hx_core import Msg

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

FORMAT = 1                     # format version written into every v2 file (storage.md)
SLOTS = (9, 10)                # the only slots Stripes writes
DEFAULT_IDLE_SLOT = 9          # the idle slot when neither 9 nor 10 is printing
MESSAGE_NAME_MAX = 20
CODE_MAX = 3                   # line, machine and print-text codes
FILM_CODE_MAX = 6              # send.md may lower this to fit the name format, never raise it
FONT_INDEXES = range(1, 7)     # FONTIND 1..6
SIZE_RANGE = range(1, 11)      # SIZE as sent; read back one lower (0..9)
BOLD_RANGE = WIDTH_RANGE = range(1, 11)
RESOLUTIONS = (0, 1, 2, 3)     # RESMOD: UltraFine, Fine, Normal, Draft
DELAY_US_MAX = 120_000_000     # DELAY and DLYMM upper limits from the manual
DLYMM_MAX = 120_000_000
CANVAS = (256, 160)            # message canvas W x H
PREVIEW_SIZE = (1024, 160)     # the read-back picture
STANDARD_FONTS = ("CourierN", "Arial", "ArialNarrow", "ArialNarrowB", "TimesNewRom", "LinearBeam")
# The default Company.standard_fonts, in printer order (index 1..6). OPEN: exact spelling as read
ALLOWED_WRITE_TAGS = ("CRTMSG", "CTEXT", "CTDAT", "CPPARM", "SAVEMSG", "PRINTMSG", "PRINT", "TD")
# The only write commands Stripes ever sends (whitelist; protocol.md enforces, tests assert)
FORBIDDEN_QUERY_TAGS = ("CPPARM", "ODPARAM", "RS232")   # multi-value queries that hang ~4 s
BEACON_SUPPORTED = False       # OPEN: no beacon query or command is used (BEAC returns -1)

GREEN, AMBER, RED, GREY, BLUE = "green", "amber", "red", "grey", "blue"
COLOURS = (GREEN, AMBER, RED, GREY, BLUE)   # BLUE = a Send or test in progress

# Send steps, in order (send.md defines each). Failures at or after STEP_SWITCH are "after the switch".
STEP_CHECK_CHOICE = "check_choice"   # sender and choice
STEP_LOCK = "lock"                   # countdown and printer locks
STEP_WAIT_POLLER = "wait_poller"     # poller paused for the lock, or stale
STEP_CHECK_MACHINE = "check_machine" # UID, fonts, ink, printing slot
STEP_SET_CLOCK = "set_clock"
STEP_BUILD = "build"                 # CRTMSG .. SAVEMSG into the idle slot
STEP_VERIFY_IDLE = "verify_idle"     # read-back 1: idle slot vs what was sent
STEP_SWITCH = "switch"               # switch pending recorded, PRINTMSG, PRINT 1
STEP_CONFIRM_1 = "confirm_1"         # read-back 2: running message
STEP_CONFIRM_2 = "confirm_2"         # read-back 3: running message again
STEP_OPERATOR = "operator_confirm"   # the sender's "Everything is right" / "Something's wrong"
STEPS = (STEP_CHECK_CHOICE, STEP_LOCK, STEP_WAIT_POLLER, STEP_CHECK_MACHINE, STEP_SET_CLOCK,
         STEP_BUILD, STEP_VERIFY_IDLE, STEP_SWITCH, STEP_CONFIRM_1, STEP_CONFIRM_2, STEP_OPERATOR)
AFTER_SWITCH_STEPS = (STEP_SWITCH, STEP_CONFIRM_1, STEP_CONFIRM_2, STEP_OPERATOR)

ITEM_KINDS = ("fixed", "packed_date", "best_by_date", "lot", "origin", "free_text",
              "machine_code", "line_code")
FILM_STATUSES = ("development", "active", "inactive")     # also used by print texts
CARTRIDGES = ("paper", "plastic")
INK_COLOURS = ("white", "black")
PLACEMENTS = ("encoder", "delay")
LOCK_PURPOSES = ("send", "test", "calibration", "stop", "resume", "clock")
LOG_KINDS = ("hx-sends", "hx-events", "hx-admin")


# --------------------------------------------------------------------------
# Timings
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Timings:
    """Every wait in one place, so tests run the real code faster with TIMINGS.scaled(0.05)."""
    http_timeout_s: float = 2.0         # every printer request
    read_retry_s: float = 0.5           # the one automatic re-ask of a read ("within a second")
    idle_warmup_s: float = 3.0          # a write after this much quiet is preceded by a warm-up read
    gate_wait_s: float = 30.0           # longest wait for the in-process per-printer gate
    web_repeat_max: int = 5             # sme_repeat attempts per Load
    web_repeat_window_s: float = 3.0    # ... or this long, whichever comes first
    poll_fast_s: float = 5.0            # printing on/off, message name, print counter
    poll_slow_s: float = 60.0           # ink, head temp, date, time, running-message Load
    second_reading_s: float = 1.0       # the confirming reading before a state change or alert
    stale_s: float = 15.0               # status counters and heartbeats
    takeover_s: tuple = (15.0, 20.0)    # poller rank 1 and rank 2 take over after this staleness
    not_monitored_s: float = 30.0       # line greyed out as NOT MONITORED
    lock_stale_s: float = 60.0          # printer lock abandoned when its counter is unchanged this long
    lock_refresh_s: float = 5.0         # a holder bumps its lock counter this often
    heartbeat_s: float = 5.0
    printer_reach_window_s: float = 86_400.0   # a PC gets printers only if it reached one this recently
    share_timeout_s: float = 5.0        # a share operation gives up after this
    share_retry_s: float = 30.0         # an unreachable share is retried this often
    data_refresh_s: float = 60.0        # templates and config reloaded from the share
    local_copy_max_age_s: float = 86_400.0     # Send works from the local copy this long
    standalone_poll_s: float = 30.0     # share down: monitor PCs poll this often, random offset
    refresh_now_min_s: float = 10.0     # "Refresh now" at most this often per PC
    backoff_s: tuple = (5.0, 10.0, 30.0, 60.0)  # an unreachable printer is retried after these
    unreachable_after: int = 3          # failures in a row before a printer counts as unreachable
    countdown_s: float = 5.0            # the Send window countdown
    confirm_gap_s: float = 2.0          # between the two running-message reads (send.md may tune)
    clock_warn_s: float = 120.0         # printer clock this far off raises a warning
    test_no_print_s: float = 60.0       # test print: printing off after this long without a print
    watch_s: float = 5.0                # status watcher reads the status folder this often
    silent_window_s: float = 300.0      # "printing bags in the last few minutes" (states.md may tune)

    def scaled(self, factor):
        """Every *_s field multiplied by factor (tuples element by element); counts unchanged."""
        if not factor > 0:
            raise ValueError("Timings.scaled needs a factor above 0")
        changes = {}
        for f in dataclasses.fields(self):
            if not f.name.endswith("_s"):
                continue
            value = getattr(self, f.name)
            if isinstance(value, tuple):
                changes[f.name] = tuple(v * factor for v in value)
            else:
                changes[f.name] = value * factor
        return dataclasses.replace(self, **changes)


TIMINGS = Timings()


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class HxError(Exception):
    """Every failure the hx modules expect. msg is what to tell the operator (English, N_-marked);
    step is a STEP_* constant when raised inside a Send, else ""."""

    def __init__(self, msg, *, step=""):
        if not isinstance(msg, Msg):
            msg = Msg(str(msg))
        super().__init__(msg)
        self.msg = msg
        self.step = step

    def __str__(self):
        return self.msg.english()


class PrinterError(HxError):
    """Talking to a printer failed."""


class NoReply(PrinterError):
    """Timeout, refused, or connection closed without a reply."""


class PrinterBusy(PrinterError):
    """Reply 7 (busy) or 6 (timeout), or the in-process gate timed out."""


class AuthFailed(PrinterError):
    """401 after a fresh login, or the token can't be unscrambled."""


class BadReply(PrinterError):
    """A reply failed a sense check: out of range, an error code, wrong shape."""

    def __init__(self, msg, *, tag="", reply="", step=""):
        super().__init__(msg, step=step)
        self.tag = tag
        self.reply = reply


class CommandRefused(PrinterError):
    """A write was answered with something other than "0"."""

    def __init__(self, msg, *, tag="", code=0, step=""):
        super().__init__(msg, step=step)
        self.tag = tag
        self.code = code


class WrongPrinter(PrinterError):
    """The printer's UID differs from the record."""

    def __init__(self, msg, *, expected=0, got="", step=""):
        super().__init__(msg, step=step)
        self.expected = expected
        self.got = got


class WebReadFailed(PrinterError):
    """A Load never got its own TN/XID in time, or the reply didn't parse."""


class ForbiddenCommand(HxError):
    """Programming error: a write not on the whitelist, or a forbidden query.
    Never caught by hx code; tests assert it can't happen."""

    def __init__(self, msg, *, tag="", step=""):
        super().__init__(msg, step=step)
        self.tag = tag


class ShareError(HxError):
    """Something on the share went wrong."""


class ShareUnavailable(ShareError):
    """The share didn't answer within share_timeout_s, or is marked down."""


class LockHeld(ShareError):
    """Another live holder has the printer lock."""

    def __init__(self, msg, *, info=None, step=""):
        super().__init__(msg, step=step)
        self.info = info


class LockLost(ShareError):
    """Our lock file now names someone else."""


class FormatTooNew(ShareError):
    """A file written by a newer Stripes: "Update Stripes"."""

    def __init__(self, msg, *, path="", found=0, step=""):
        super().__init__(msg, step=step)
        self.path = path
        self.found = found


class RevisionConflict(ShareError):
    """Admin save: the record changed since it was loaded."""

    def __init__(self, msg, *, current=0, step=""):
        super().__init__(msg, step=step)
        self.current = current


class PlanError(HxError):
    """The choice can't be sent; nothing has been written. problems holds every problem found."""

    def __init__(self, msg, *, problems=None, step=""):
        super().__init__(msg, step=step)
        self.problems = list(problems) if problems else [self.msg]


class CheckFailed(HxError):
    """A read-back differs from what was sent."""

    def __init__(self, msg, *, differences=None, step=""):
        super().__init__(msg, step=step)
        self.differences = list(differences) if differences else []


class NotAllowed(HxError):
    """Wrong group, or not the person who sent it."""


class TimeServerError(HxError):
    """SNTP got no valid answer (TimeSource catches it)."""


# --------------------------------------------------------------------------
# JSON mapping
# --------------------------------------------------------------------------

def to_json(obj):
    """Any dataclass here (or a Msg) as plain JSON data, recursively, in field order.
    Tuples and frozensets become lists; bytes are left as they are (callers store them as base64)."""
    if isinstance(obj, Msg):
        return obj.to_json()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    raise TypeError(f"to_json needs a dataclass, not {type(obj).__name__}")


def _plain(value):
    if isinstance(value, Msg):
        return value.to_json()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_json(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


_HINTS = {}     # class -> its resolved type hints (from_json is called for every status file read)


def _hints(cls):
    hints = _HINTS.get(cls)
    if hints is None:
        hints = _HINTS[cls] = typing.get_type_hints(cls)
    return hints


def from_json(cls, d):
    """Build cls from a dict, recursively by its type hints. Unknown keys are ignored and missing
    keys take their defaults. Raises ValueError for a value of the wrong type (architecture §5.3)
    or a missing required field."""
    if cls is Msg:
        return Msg.from_json(d)
    if not (dataclasses.is_dataclass(cls) and isinstance(cls, type)):
        raise TypeError(f"from_json needs a dataclass, not {cls!r}")
    if not isinstance(d, dict):
        raise ValueError(f"{cls.__name__}: expected an object, got {type(d).__name__}")
    hints = _hints(cls)
    values = {}
    for f in dataclasses.fields(cls):
        if not f.init:
            continue
        if f.name in d:
            values[f.name] = _convert(hints[f.name], d[f.name], f"{cls.__name__}.{f.name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise ValueError(f"{cls.__name__}.{f.name}: missing")
    return cls(**values)


def _wrong(where, hint, value):
    name = getattr(hint, "__name__", None) or str(hint)
    return ValueError(f"{where}: expected {name}, got {type(value).__name__}")


def _convert(hint, value, where):
    """One value by its type hint, with the §5.3 rules."""
    if hint is typing.Any or hint is object:
        return value
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    if origin is typing.Union or origin is types.UnionType:
        if value is None:
            if type(None) in args:
                return None
            raise _wrong(where, hint, value)
        options = [a for a in args if a is not type(None)]
        if len(options) == 1:
            return _convert(options[0], value, where)   # "X | None": X's own error, with its detail
        for arg in options:
            try:
                return _convert(arg, value, where)
            except ValueError:
                pass
        raise _wrong(where, hint, value)

    if value is None:
        raise _wrong(where, hint, value)      # None only for "X | None" hints

    if hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _wrong(where, hint, value)
        return float(value)
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _wrong(where, hint, value)
        return value
    if hint is bool or hint is str:
        if not isinstance(value, hint):
            raise _wrong(where, hint, value)
        return value
    if hint is bytes:
        # JSON never holds bytes; this is for to_json output kept in memory
        if not isinstance(value, (bytes, bytearray)):
            raise _wrong(where, hint, value)
        return bytes(value)
    if hint is Msg:
        try:
            return Msg.from_json(value)
        except ValueError as e:
            raise ValueError(f"{where}: {e}") from None

    # Plain containers keep their contents as they are
    if hint is dict:
        if not isinstance(value, dict):
            raise _wrong(where, hint, value)
        return value
    if hint is list:
        if not isinstance(value, list):
            raise _wrong(where, hint, value)
        return value
    if hint is tuple:
        if not isinstance(value, (list, tuple)):
            raise _wrong(where, hint, value)
        return tuple(value)

    if origin is list:
        if not isinstance(value, (list, tuple)):
            raise _wrong(where, hint, value)
        (item,) = args
        return [_convert(item, v, f"{where}[{i}]") for i, v in enumerate(value)]
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise _wrong(where, hint, value)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_convert(args[0], v, f"{where}[{i}]") for i, v in enumerate(value))
        if len(value) != len(args):
            raise ValueError(f"{where}: expected {len(args)} values, got {len(value)}")
        return tuple(_convert(a, v, f"{where}[{i}]") for i, (a, v) in enumerate(zip(args, value)))
    if origin is frozenset or origin is set:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise _wrong(where, hint, value)
        (item,) = args
        return origin(_convert(item, v, f"{where}[]") for v in value)
    if origin is dict:
        if not isinstance(value, dict):
            raise _wrong(where, hint, value)
        key_hint, item = args
        out = {}
        for k, v in value.items():
            if not isinstance(k, str) and key_hint is str:
                raise ValueError(f"{where}: keys must be text")
            out[k] = _convert(item, v, f"{where}[{k!r}]")
        return out

    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        if not isinstance(value, dict):
            raise _wrong(where, hint, value)
        return from_json(hint, value)

    raise _wrong(where, hint, value)


def new_id(prefix):
    """A new record id: "fm_3f9a1c22b0"."""
    return f"{prefix}_{secrets.token_hex(5)}"


def adjustment_id(machine_id, film_id):
    return f"{machine_id}__{film_id}"


def same_user(a, b):
    """Windows user names compare case-insensitively."""
    return (a or "").lower() == (b or "").lower()


def _natural_key(name):
    # "Bagger 2" before "Bagger 10": digit runs as numbers, the rest casefolded
    return [(0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
            for part in re.split(r"(\d+)", name or "") if part]


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class Identity:
    """Who is running this copy of Stripes, from the Windows sign-in (hx_win.current_identity)."""
    user: str                    # "EXAMPLE\\jsmith", as Windows reports it (NameSamCompatible)
    computer: str                # COMPUTERNAME
    sids: frozenset[str]         # enabled group SIDs from the sign-in token
    pid: int

    def __post_init__(self):
        if not isinstance(self.sids, frozenset):
            object.__setattr__(self, "sids", frozenset(self.sids))

    @property
    def key(self):
        """Heartbeat key: computer, user and process, safe as a file name."""
        return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{self.computer}_{self.user}_{self.pid}")

    def _in_group(self, sid):
        # An empty group SID in the company settings means nobody is in that group
        sid = (sid or "").strip().upper()
        return bool(sid) and sid in {s.upper() for s in self.sids}

    def is_admin(self, company):
        return self._in_group(company.admin_group_sid)

    def is_sender(self, company):
        return self._in_group(company.sender_group_sid)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class Record:
    """The base fields of every stored record. revision goes up by one on every save."""
    id: str
    revision: int = 0
    updated_by: str = ""
    updated_at: float | None = None


@dataclass(kw_only=True)
class Country:
    name: str                    # exactly as printed: "USA"
    aliases: list[str] = field(default_factory=list)   # refused in other text: "US", "U.S.A.", "United States"
    active: bool = True


@dataclass(kw_only=True)
class Ink:
    name: str                    # exactly as the ink-name query reports it
    cartridge: str               # CARTRIDGES
    colour: str                  # INK_COLOURS


@dataclass(kw_only=True)
class Company(Record):           # id "company"
    time_server: str = ""                   # "time.example.local"
    time_server_fallback: str = ""          # "192.0.2.123"
    countries: list[Country] = field(default_factory=list)
    inks: list[Ink] = field(default_factory=list)
    lot_min_digits: int = 4
    lot_max_digits: int = 8
    free_text_max: int = 30
    admin_group: str = "Stripes Administrators"      # display name
    admin_group_sid: str = ""
    sender_group: str = "Stripes MapleJet Senders"
    sender_group_sid: str = ""
    standard_fonts: list[str] = field(default_factory=lambda: list(STANDARD_FONTS))
    bag_counter: str = "PMCNT"   # OPEN: which counter counts bags: "PMCNT", "MSCNT" or "none" (time-based)


@dataclass(kw_only=True)
class Line(Record):
    name: str                    # "Line 1"
    code: str                    # printed code, 1..CODE_MAX characters
    cartridge: str               # CARTRIDGES: only films of this type are offered
    notify_user: str = ""        # "EXAMPLE\\jsmith"
    active: bool = True


@dataclass(kw_only=True)
class Machine(Record):
    name: str                    # "Line 1 Bagger 1"
    code: str                    # printed code, 1..CODE_MAX characters
    line_id: str
    printer_id: str
    placement: str               # PLACEMENTS
    head_distance_mm: float | None = None   # encoder only; set from calibration. None: can't send (OPEN)
    encoder_wheel_mm: float | None = None   # encoder only: expected wheel size, checked daily
    encoder_ppr: int | None = None          # encoder only: expected pulses per turn, checked daily
    print_speed: int | None = None          # PRNSPD as sent. OPEN: units (cm/min assumed) and range;
                                            # None: can't send until an administrator sets it
    rotate: bool = False
    mirror: bool = False
    seconds_per_bag: float = 2.4            # timed test steps when no counter counts bags (OPEN)
    active: bool = True


@dataclass(kw_only=True)
class Calibration(Record):       # id = machine_id; the evidence behind Machine.head_distance_mm
    machine_id: str
    spacing_sent_mm: int = 300
    spacing_measured_mm: float | None = None
    spacing_by: str = ""
    spacing_at: float | None = None
    rough_head_distance_mm: float | None = None    # tape measure, to pick the right bag length
    head_print_sent_mm: int | None = None
    head_print_measured_mm: float | None = None    # measured from the bag's bottom edge
    head_distance_mm: float | None = None          # result; saving copies it to the machine
    head_by: str = ""
    head_at: float | None = None


@dataclass(kw_only=True)
class Printer(Record):
    name: str                    # display: "Printer 1"
    host: str                    # fixed IP address: "192.0.2.10"
    port: int = 80               # always 80 in production; tests point it at a fake printer
    uid: int                     # 1109; checked on every poll and Send
    token: str = ""              # the scrambled token (protocol.md); "" until a password is set
    active: bool = True


@dataclass(kw_only=True)
class Product(Record):
    name: str                    # "Example Co 2lb Onion"
    customer: str = ""           # "Example Co"
    active: bool = True


@dataclass(kw_only=True)
class FileRef:
    kind: str                    # "front_photo" | "back_photo" | "artwork" | "spec" | "other"
    name: str                    # the original file name, for display
    path: str                    # relative to v2\templates, forward slashes
    sha256: str
    size: int
    added_by: str = ""
    added_at: float | None = None


@dataclass(kw_only=True)
class Film(Record):
    product_id: str
    code: str                    # [A-Z0-9], 1..FILM_CODE_MAX, used in the message name
    artwork_revision: str = ""   # "Rev C"
    status: str = "development"  # FILM_STATUSES; only "active" is offered at Send
    files: list[FileRef] = field(default_factory=list)
    side: str = "back"           # "front" | "back": which side gets the code (an instruction)
    cartridge: str = "plastic"   # CARTRIDGES; must match the line's
    ink_colour: str = "black"    # INK_COLOURS; checked against the printer's ink
    length_mm: float | None = None        # cut-off length; required for encoder machines
    position_mm: float | None = None      # from the bag's bottom edge; required for encoder machines
    delay_ms: float | None = None         # after the trigger; required for delay machines
    print_length_mm: float | None = None  # OPEN: for the film-stop warning; None: no warning possible
    font: int = 2                # index into Company.standard_fonts, FONT_INDEXES
    size: int = 4                # SIZE_RANGE, as sent
    resolution: int = 0          # RESOLUTIONS
    bold: int = 1                # BOLD_RANGE
    width: int = 1               # WIDTH_RANGE
    origin_preprinted: bool = False
    preprinted_origin: str = ""  # bag text, only when origin_preprinted: "USA"
    print_text_ids: list[str] = field(default_factory=list)   # print texts allowed with this film
    default_print_text_id: str = ""


@dataclass(kw_only=True)
class PrintTextItem:
    id: str                      # unique within its print text: "i1", "i2", ...
    kind: str                    # ITEM_KINDS
    before: str = ""             # bag text before the data; must be "" for "origin"
    after: str = ""              # bag text after the data; must be "" for "origin"
    text: str = ""               # "fixed" only
    date_format: str = ""        # "packed_date"/"best_by_date": a hx_message.DATE_FORMATS key
    offset: int = 0              # "best_by_date" only
    offset_unit: str = "days"    # "days" | "months" | "years"
    label: str = ""              # "free_text": the question shown to the sender
    max_len: int = 0             # "free_text": 1..Company.free_text_max
    required: bool = True        # "free_text"
    placement: str = "right"     # "right" of or "below" the previous item; ignored for the first
    gap: int = 0                 # extra space before the item, in canvas units (OPEN: units)


@dataclass(kw_only=True)
class PrintText(Record):
    name: str                    # "Packed on + origin"
    code: str                    # 1..CODE_MAX characters, used in the message name
    status: str = "development"  # FILM_STATUSES
    items: list[PrintTextItem] = field(default_factory=list)


@dataclass(kw_only=True)
class MachineFilmAdjustment(Record):    # id = adjustment_id(machine_id, film_id)
    machine_id: str
    film_id: str
    position_mm: float = 0.0     # encoder machines; ignored in a test print
    delay_ms: float = 0.0        # delay machines; ignored in a test print
    # updated_by / updated_at are "who set it and when"


@dataclass(kw_only=True)
class HxData:
    """One consistent set of config and templates, from the share or the local copy. Treated as
    immutable: DataService swaps in a new one, never edits it."""
    company: Company
    lines: dict[str, Line] = field(default_factory=dict)
    machines: dict[str, Machine] = field(default_factory=dict)
    printers: dict[str, Printer] = field(default_factory=dict)
    adjustments: dict[str, MachineFilmAdjustment] = field(default_factory=dict)   # by adjustment_id
    calibrations: dict[str, Calibration] = field(default_factory=dict)            # by machine_id
    products: dict[str, Product] = field(default_factory=dict)
    films: dict[str, Film] = field(default_factory=dict)
    print_texts: dict[str, PrintText] = field(default_factory=dict)
    source: str = "share"        # "share" | "local"
    loaded_at: float = 0.0
    reached_at: float | None = None   # when this PC last reached the share (the 24-hour rule)

    def line_machines(self, line_id):
        """The line's active machines, by name ("Bagger 2" before "Bagger 10")."""
        found = [m for m in self.machines.values() if m.line_id == line_id and m.active]
        return sorted(found, key=lambda m: (_natural_key(m.name), m.id))

    def adjustment(self, machine_id, film_id):
        return self.adjustments.get(adjustment_id(machine_id, film_id))


# --------------------------------------------------------------------------
# Send request and plan
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class LineChoice:
    line_id: str
    machine_ids: list[str]       # the ticked machines of that line
    product_id: str
    film_id: str
    print_text_id: str
    lot: str = ""
    countries: list[str] = field(default_factory=list)       # Country.name values, in the order picked
    free_text: dict[str, str] = field(default_factory=dict)  # PrintTextItem.id -> value


@dataclass(kw_only=True)
class SendRequest:
    send_id: str                 # new_id("sd")
    sender: str                  # Identity.user
    computer: str
    created_at: float
    lines: list[LineChoice]


@dataclass(kw_only=True)
class PrintSettings:             # every print setting Stripes sends (the CPPARM of the new message)
    encoder: bool                # ENCST
    position_mm: int = 0         # DLYMM, encoder machines
    delay_us: int = 0            # DELAY, delay machines
    speed: int                   # PRNSPD
    rotate: bool = False         # ROTAT
    mirror: bool = False         # MIRR
    resolution: int = 0          # RESMOD
    bold: int = 1                # BOLD
    width: int = 1               # WDTH
    quantity: int = 1            # QUANT
    invert: bool = False         # REVRS
    auto_repeat: bool = False    # AUTREP


@dataclass(kw_only=True)
class PositionWorking:           # how the position or delay was worked out, for the log and the window
    placement: str               # PLACEMENTS
    film_value: float            # Film.position_mm or Film.delay_ms (or the test step's value)
    adjustment: float            # mm or ms; 0 in a test print
    head_distance_mm: float | None = None
    film_length_mm: float | None = None
    sent: int                    # DLYMM in mm, or DELAY in microseconds
    wrapped: bool = False
    text: str                    # English: "(150 - 412 + 0) mod 610 = 348 mm"
    warning: Msg | None = None   # the film-stop warning


@dataclass(kw_only=True)
class TestStep:
    index: int                   # 1-based
    label: str                   # bag text printed with the step: "P150 | B1" or "D480 | B1"
    position_mm: float | None = None
    delay_ms: float | None = None
    bold: int
    width: int
    resolution: int
    fonts: list[tuple[int, int]] = field(default_factory=list)    # (font index, size) side by side


@dataclass(kw_only=True)
class MachinePlan:
    """Everything needed to build one machine's message, merged from every level. Records are copies."""
    machine: Machine
    line: Line
    printer: Printer
    product: Product | None      # None in a test print
    film: Film
    print_text: PrintText | None # None in a test print
    adjustment: MachineFilmAdjustment | None
    lot: str = ""
    countries: list[str] = field(default_factory=list)
    free_text: dict[str, str] = field(default_factory=dict)
    origin_line: str = ""        # bag text: "Product of USA and Canada", "USA (pre-printed)" or ""
    bag_text: str = ""           # one-line rendering of what will print today, for windows and logs
    settings: PrintSettings
    position: PositionWorking
    warnings: list[Msg] = field(default_factory=list)
    test_step: TestStep | None = None   # set only in a test print or calibration


@dataclass(kw_only=True)
class SendPlan:
    send_id: str
    request: SendRequest
    machines: list[MachinePlan]
    from_local_copy: bool = False
    data_as_of: float | None = None     # HxData.reached_at, shown as "templates as of <time>"

    def restricted(self, machine_ids):
        """Try again: the same choices for these machines only, under a new send_id. Both machines
        and request.lines[i].machine_ids are filtered, and lines left empty are dropped (send.md §4.5)."""
        keep = set(machine_ids)
        send_id = new_id("sd")
        request = copy.deepcopy(self.request)
        request.send_id = send_id
        lines = []
        for choice in request.lines:
            choice.machine_ids = [m for m in choice.machine_ids if m in keep]
            if choice.machine_ids:
                lines.append(choice)
        request.lines = lines
        machines = [copy.deepcopy(p) for p in self.machines if p.machine.id in keep]
        return SendPlan(send_id=send_id, request=request, machines=machines,
                        from_local_copy=self.from_local_copy, data_as_of=self.data_as_of)


@dataclass(kw_only=True)
class TestRanges:
    vary: str                    # "position" (encoder machines) or "delay" (delay machines)
    start: float                 # mm or ms
    stop: float
    step: float
    bold: list[int] = field(default_factory=list)         # extra values to try; [] keeps the film's
    width: list[int] = field(default_factory=list)
    resolution: list[int] = field(default_factory=list)
    fonts: list[tuple[int, int]] = field(default_factory=list)   # side by side; [] = the film's font and size


@dataclass(kw_only=True)
class TestPlan:
    run_id: str                  # new_id("tp")
    kind: str                    # "test" | "spacing" | "head_distance"
    base: MachinePlan            # built from the machine and film with adjustments ignored
    steps: list[TestStep]
    bags_per_step: int = 5
    timed: bool = False          # True when Company.bag_counter == "none"


# --------------------------------------------------------------------------
# Commands and read-back
# --------------------------------------------------------------------------

def _xml_escape(text):
    # & first, then < and >; nothing else (protocol.md §4.1)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True)
class Command:
    """One Autopilot command: the outer tag and the whole APCMD content."""
    tag: str                     # outer tag: "CRTMSG", "PRIN", ...
    xml: str                     # the whole APCMD content: "<PRINT>1</PRINT>" or "<PRIN/>"
    write: bool                  # True for setting and design commands (answer must be "0")

    @staticmethod
    def query(tag):
        return Command(tag, f"<{tag}/>", False)

    @staticmethod
    def set(tag, value):
        return Command(tag, f"<{tag}>{_xml_escape(str(value))}</{tag}>", True)

    @staticmethod
    def block(tag, children):
        """<TAG><A>1</A><B>x</B></TAG>; values as str(), XML-escaped."""
        inner = "".join(f"<{child}>{_xml_escape(str(value))}</{child}>" for child, value in children)
        return Command(tag, f"<{tag}>{inner}</{tag}>", True)


@dataclass(kw_only=True)
class DesignItem:
    tag: str                     # "CTEXT", "CTDAT", "CLOT", ... as in the FCDOC
    pairs: list[tuple[str, str]] # every child tag and its text, in reply order (tags can repeat, e.g. LANG)

    def value(self, tag, default=None):
        """The text of the last child with this tag, or default."""
        for child, text in reversed(self.pairs):
            if child == tag:
                return text
        return default


@dataclass(kw_only=True)
class MessageDesign:
    """The printer's own copy of one stored message, from the web Load."""
    slot: int                    # MSNO of the reply: the real slot, never 0
    name: str                    # MSGNAM ("" when unnamed)
    empty: bool                  # the reply had no FCDOC
    items: list[DesignItem] = field(default_factory=list)
    settings: dict[str, str] = field(default_factory=dict)  # the message's CPPARM: child tag -> text
    width: int | None = None     # W
    height: int | None = None    # H
    black_pixels: int = 0        # BPIX
    preview_bmp: bytes = b""     # the decoded 1-bit 1024x160 BMP
    read_at: float = 0.0
    tn: str = ""                 # the request numbers this reply matched
    xid: int = 0


@dataclass(kw_only=True)
class BuiltMessage:
    """What build_commands sends, and what the read-back must show."""
    slot: int
    name: str
    content_code: str
    commands: list[Command]      # CRTMSG, one per item, CPPARM, SAVEMSG <slot>
    expected_items: list[DesignItem]   # as sent (compare applies the read-back rules)
    expected_settings: dict[str, str]  # every CPPARM value the read-back must show, incl. printer defaults
    origin_line: str
    bag_text: str


@dataclass(frozen=True)
class Difference:
    where: str                   # "item 2 (CTEXT)", "print settings", "name", "slot"
    field: str                   # "TXT", "SIZE", "PRNSPD", ...
    sent: str
    read: str


@dataclass(frozen=True)
class MessageName:
    film_code: str
    print_text_code: str
    print_text_revision: int
    content_code: str
    slot: int


# --------------------------------------------------------------------------
# Status files (storage.md owns the files; states.md and send.md what goes in them)
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class LockInfo:                  # status\locks\<printer_id>.lock
    printer_id: str
    token: str                   # random hex, new for every acquisition
    user: str
    computer: str
    pid: int
    purpose: str                 # LOCK_PURPOSES
    line_id: str = ""
    machine_id: str = ""
    run_id: str = ""             # send_id, test run_id or action id
    counter: int = 0             # +1 at every refresh; staleness is judged from this only
    acquired_at: float = 0.0     # display only
    note: dict = field(default_factory=dict)    # e.g. {"step": 12, "of": 40} during a test print


@dataclass(kw_only=True)
class HeartbeatInfo:             # status\pollers\<key>.json
    key: str                     # Identity.key
    computer: str
    user: str
    pid: int
    version: str
    counter: int = 0             # +1 every heartbeat_s
    written_at: float = 0.0
    can_poll: bool = False       # reached any printer within printer_reach_window_s
    last_printer_reach: float | None = None
    polling: list[str] = field(default_factory=list)    # printer ids this PC is polling now
    monitor_open: bool = False
    floor_board: bool = False


@dataclass(kw_only=True)
class RunningMessage:
    slot: int
    name: str
    fingerprint: str             # hx_message.fingerprint of the read-back
    origin_line: str             # bag text read back ("" if the message has no origin item)
    read_at: float
    preview_sha256: str = ""     # of status\machines\<id>.preview.bmp


@dataclass(kw_only=True)
class MachineSnapshot:           # the poller's judgement (states.md defines the codes)
    code: str
    colour: str                  # COLOURS
    headline: Msg
    since: float
    detail: list[Msg] = field(default_factory=list)


@dataclass(kw_only=True)
class Alert:
    id: str                      # stable while the alert lasts; also the ack file name
    machine_id: str
    line_id: str
    kind: str                    # states.md list
    sound: bool                  # False: warning without sound
    msg: Msg
    started_at: float
    recipients: list[str] = field(default_factory=list)   # "user:<DOMAIN\\name>", "floor", "admins", "monitor"
    active: bool = True
    # added by states.md §11.2:
    colour: str = ""             # RED, AMBER or "" (doesn't colour the machine)
    ref: str = ""                # the send_id or run_id the alert belongs to; "" for time-based ids
    acked_by: str = ""
    acked_at: float | None = None
    ended_at: float | None = None
    waiting: list[str] = field(default_factory=list)      # recipients with no live heartbeat yet


@dataclass(kw_only=True)
class Ack:                       # status\alerts\<alert_id>.json, written once
    alert_id: str
    by: str
    computer: str
    at: float


@dataclass(kw_only=True)
class PollStatus:                # status\machines\<machine_id>.poll.json; one writer: the assigned poller
    machine_id: str
    printer_id: str
    poller: str                  # writer's Identity.key
    counter: int = 0             # +1 every cycle
    written_at: float = 0.0
    paused_for_lock: str = ""    # LockInfo.token it stepped aside for; "" while polling
    yielded_to: str = ""         # heartbeat key it handed the printer to
    reachable: bool = False
    failures: int = 0            # failed requests in a row
    last_answer_at: float | None = None
    printing: bool | None = None
    message_name: str | None = None
    bag_count: int | None = None         # the Company.bag_counter value
    bags_recent: int | None = None       # prints within silent_window_s
    ink_name: str | None = None
    ink_level: int | None = None
    head_temp_c: int | None = None
    clock_offset_s: float | None = None  # printer clock minus time-server time
    running: RunningMessage | None = None
    daily: dict = field(default_factory=dict)    # last daily check (states.md)
    state: MachineSnapshot | None = None
    alerts: list[Alert] = field(default_factory=list)
    foreign_client: bool = False # "Something else is talking to printer X"
    # added by states.md §11.2:
    problem: str = ""            # "" | "wrong_printer" | "login_failed"
    expected_counter: int = 0    # ExpectedState.counter at the last judgement (0: there was no E)
    reread_seen: str = ""        # the last ExpectedState.reread_token acted on
    unreachable_since: float | None = None   # wall time reachable last became False; None while reachable
    running_ok_at: float | None = None       # wall time of the last successful running-message Load
    running_unchecked: bool = False          # committed fact: the running message can't be read
    dst: dict = field(default_factory=dict)  # the daylight-saving clock set of the current change
    standalone: bool = False     # True only in Poller.local_statuses() in standalone mode


@dataclass(kw_only=True)
class FailureInfo:
    step: str                    # STEP_*
    msg: Msg
    after_switch: bool
    at: float


@dataclass(kw_only=True)
class ExpectedState:             # status\machines\<machine_id>.expected.json; written under the printer lock
    machine_id: str
    counter: int = 0
    written_at: float = 0.0
    written_by: str = ""
    computer: str = ""
    mode: str = "none"           # "none" | "switch_pending" | "running" | "stopped_by_user" |
                                 # "stop_requested_at_printer" | "test_running" | "test_finished"
    printing_expected: bool = False
    send_id: str = ""
    message_name: str = ""
    content_code: str = ""
    fingerprint: str = ""
    slot: int | None = None
    origin_line: str = ""        # bag text
    previous_message_name: str = ""   # while switch_pending: what was printing before
    previous_fingerprint: str = ""
    result: str = ""             # the last Send's colour for this machine: GREEN, AMBER or RED
    failure: FailureInfo | None = None
    notes: list[str] = field(default_factory=list)   # amber reasons: "pc_time", "no_lock", "local_copy"
    confirmed: bool = False
    confirmed_by: str = ""
    confirmed_at: float | None = None
    rejected: bool = False
    resume_blocked: bool = False
    left_by: str = ""            # "Leave it"
    left_at: float | None = None
    reread_token: str = ""       # a new value asks the poller to load the running message now
    test: dict | None = None     # {"run_id": ..., "kind": ..., "by": ..., "step": 12, "of": 40}
    # added by send.md §1.4:
    sender: str = ""             # who made the Send (or test) this state belongs to; written_by is only
                                 # the last writer (a confirm, Leave it or clock set doesn't change it)
    stop_reason: str = ""        # "user" | "rejected" | "test"; "" while not stopped
    stopped_by: str = ""
    stopped_at: float | None = None


@dataclass(kw_only=True)
class LineStatus:                # status\lines\<line_id>.json; informational: the line's last Send
    line_id: str
    counter: int = 0
    written_at: float = 0.0
    written_by: str = ""
    send_id: str = ""
    sent_at: float | None = None
    product_id: str = ""
    film_id: str = ""
    film_revision: int = 0
    print_text_id: str = ""
    print_text_revision: int = 0
    lot: str = ""
    countries: list[str] = field(default_factory=list)
    origin_line: str = ""
    machines: dict[str, str] = field(default_factory=dict)   # machine_id -> that machine's result colour
    confirmed: bool = False
    confirmed_by: str = ""
    stopped_needs_send: bool = False
    # added by send.md §1.4:
    sender: str = ""             # who made the Send this file describes


# --------------------------------------------------------------------------
# Results and events
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class MachineResult:
    machine_id: str
    colour: str                  # AMBER (live, checked, not yet confirmed by the sender) or RED
    ok: bool                     # went live and both running-message reads passed
    step: str                    # the last step reached; the failed step when not ok
    msg: Msg | None = None
    after_switch: bool = False
    slot: int | None = None
    message_name: str = ""
    content_code: str = ""
    fingerprint: str = ""
    origin_line: str = ""
    readbacks: list[MessageDesign] = field(default_factory=list)   # idle slot, running 1, running 2
    previous: RunningMessage | None = None   # what it was printing just before this Send
    previous_preview: bytes = b""
    previous_confirmed: bool = False         # previous is the last Send's confirmed message (send.md §10.5)
    counts_before: int | None = None
    counts_after: int | None = None
    notes: list[str] = field(default_factory=list)   # "pc_time", "no_lock", "local_copy", "ink_mismatch", "film_stop"


@dataclass(kw_only=True)
class SendResult:
    send_id: str
    sender: str
    computer: str
    started_at: float
    finished_at: float
    machines: list[MachineResult]
    time_source: str             # "server" | "pc"
    from_local_copy: bool = False
    data_as_of: float | None = None
    log_name: str = ""           # file name in logs\hx-sends, or in the PC's queue
    log_queued: bool = False


@dataclass(kw_only=True)
class ActionResult:              # Stop, Resume, Set clocks
    action_id: str
    kind: str                    # "stop" | "resume" | "clock"
    machines: list[MachineResult]


@dataclass(kw_only=True)
class ResumeCheck:
    machine_id: str
    design: MessageDesign | None
    origin_line: str
    allowed: bool
    msg: Msg | None = None       # why not


@dataclass(kw_only=True)
class TestResult:
    run_id: str
    kind: str
    steps_done: int
    of: int
    ok: bool
    msg: Msg | None = None       # why it stopped early
    cleared: bool = False        # slots 9 and 10 were overwritten with empty messages
    printing_off: bool = False   # the end procedure read printing off twice (send.md §15.5)


# Events: posted on the EventBus. All plain data; never Tk objects.

@dataclass(kw_only=True)
class SendEvent:
    send_id: str
    machine_id: str = ""         # "" for whole-send events
    step: str                    # STEP_*
    status: str                  # "running" | "ok" | "failed" | "skipped" | "warning"
    msg: Msg | None = None
    design: MessageDesign | None = None
    at: float = 0.0


@dataclass(kw_only=True)
class SendFinished:
    result: SendResult


@dataclass(kw_only=True)
class ActionEvent:
    action_id: str
    kind: str                    # "stop" | "resume" | "clock"
    machine_id: str
    status: str                  # as SendEvent
    msg: Msg | None = None
    design: MessageDesign | None = None
    at: float = 0.0


@dataclass(kw_only=True)
class ActionFinished:
    result: ActionResult


@dataclass(kw_only=True)
class TestEvent:
    run_id: str
    step_index: int
    of: int
    status: str                  # "building" | "printing" | "ok" | "failed" | "stopping" | "cleared"
    msg: Msg | None = None
    design: MessageDesign | None = None
    bags: int | None = None
    at: float = 0.0


@dataclass(kw_only=True)
class TestFinished:
    result: TestResult


@dataclass(kw_only=True)
class BoardUpdate:
    board: BoardView


@dataclass(kw_only=True)
class DataUpdate:
    data: HxData


@dataclass(kw_only=True)
class ShareHealth:
    ok: bool
    last_reached: float | None


@dataclass(kw_only=True)
class PollerUpdate:
    mode: str                    # "share" | "standalone" | "idle"
    polling: list[str] = field(default_factory=list)    # printer ids
    next_check_s: float | None = None   # standalone: seconds to the next check


# --------------------------------------------------------------------------
# Views (computed by hx_state; the UI shows them as they are)
# --------------------------------------------------------------------------

@dataclass(kw_only=True)
class MachineView:
    machine_id: str
    line_id: str
    name: str                    # the record's name, not translated
    colour: str                  # COLOURS
    code: str                    # state code (states.md)
    headline: Msg
    details: list[Msg] = field(default_factory=list)
    origin_line: str = ""        # bag text: what it is confirmed (or last read) printing
    message_name: str = ""
    checked_age_s: float | None = None   # "checked N s ago"; None when never read
    monitored: bool = False
    printing: bool | None = None
    alerts: list[Alert] = field(default_factory=list)   # active ones, with acks applied
    unacked_sound: bool = False
    can_stop: bool = False
    can_resume: bool = False
    needs_send: bool = False
    confirm_pending_by: str = "" # sender who still has to confirm ("Send not confirmed")
    confirm_send_id: str = ""
    test: dict | None = None     # {"by": ..., "step": 12, "of": 40}
    has_preview: bool = False


@dataclass(kw_only=True)
class LineView:
    line_id: str
    name: str
    colour: str                  # its worst machine
    headline: Msg
    machines: list[MachineView]
    origins_differ: bool = False
    monitored: bool = False
    checked_age_s: float | None = None


@dataclass(kw_only=True)
class BoardView:
    lines: list[LineView]
    share_ok: bool
    standalone: bool = False     # share down: this PC's own checks
    next_check_s: float | None = None
    generated_at: float = 0.0


@dataclass(kw_only=True)
class FloorRow:                  # one big row per machine
    line_id: str
    machine_id: str
    colour: str
    line_name: str
    machine_name: str
    origin_line: str             # bag text
    status: Msg
    checked_age_s: float | None
